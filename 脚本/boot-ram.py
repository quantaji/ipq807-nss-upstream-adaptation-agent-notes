#!/usr/bin/env python3
"""Serve a RAM image by TFTP and observe the router boot it.

The modified U-Boot ARPs for 192.168.11.10 from 192.168.11.1 every 5 s and
boots the file it receives; an answered ARP without a TFTP server leaves the
router silent until a power cycle. So: open the console, start a TFTP-only
dnsmasq (bind-dynamic) and print "tftp_ready" on stderr; only then add the
address with test-network.py apply (with --start reboot it may be there
already). Once the address is on --interface and dnsmasq listens there:
  --start reboot  read the idle shell's boot_id, send one "reboot";
  --start waiting the router is powering on, waiting, or at the U-Boot prompt.
Then follow the console until a Linux prompt is followed by 3 s of silence
(input typed while the kernel prints is garbled), typing only "reset" at the
U-Boot prompt IPQ807x# (at most --uboot-resets times, then fail) or Enter for
"Please press Enter" and after each 10 s of silence (a waiting U-Boot prompt
prints nothing until a key arrives). Then read boot_id, kernel release and
root filesystem on the console, and with --ssh TARGET SOURCE INTERFACE the
boot_id over ssh (bound to SOURCE; run only while the route to TARGET leaves
through INTERFACE). No host networking, countdown interrupt, flash write or
other U-Boot command. Needs root. --output (new) keeps serial.log, tftp.log,
dnsmasq.conf and tftp/.

Stdout: one JSON object (stage, milestones, image_bytes, transferred_bytes,
served_by_dnsmasq, previous_boot_id, boot_id, kernel, root_fs, ssh_boot_id,
output_dir, error). Exit 0 = new boot_id, transferred bytes equal the image
size, dnsmasq logged the send, root is a RAM filesystem and (with --ssh) ssh
sees the same boot_id. Exit 1 otherwise; exit 2 usage error.
Self-test (no device, no root): python3 boot-ram.py --self-test
"""

import argparse
import ipaddress
import json
import os
import pwd
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab

DEFAULT_IMAGE_NAME = "openwrt-qualcommax-ipq807x-buffalo_wxr-5950ax12-initramfs-uImage.itb"
PROTECTED_INTERFACES = ("eno1", "enx224d686d5002")  # uplink and BMC; never touched
LINUX_PROMPT = re.compile(lab.LINUX_PROMPT.encode())
UBOOT_PROMPT = re.compile(rb"IPQ807x# ?$")
# (milestone, stage it sets or None, console text); each is recorded at its first appearance.
MARKS = (("tftp_request", None, re.compile(rb"TFTP from server")),
         ("transfer_done", "transfer_seen", re.compile(rb"Bytes transferred = (\d+)")),
         ("kernel_started", "kernel_started", re.compile(rb"Starting kernel|Linux version ")))
SILENT_SECONDS = 10  # TFTP transfer, countdown and kernel all print sooner; this much silence waits for a key
QUIET_SECONDS = 3  # after the Linux prompt, the console must be this quiet before identity is read
# Self-test console texts, hand-written from the 2026-10-16 serial logs' wording (not recordings).
SAMPLE_TO_ENTER = (
    "U-Boot 2016.01 (self-test sample)\nHit any key to stop autoboot:  0\n"
    "TFTP from server 192.168.11.10; our IP address is 192.168.11.1\n"
    f"Filename '{DEFAULT_IMAGE_NAME}'.\nLoad address: 0x44000000\nLoading: ##########\ndone\n"
    "Bytes transferred = 1234 (4d2 hex)\nStarting kernel ...\n\n[    0.000000] Linux version 6.18.44 (sample) #0 SMP\n"
    "Please press Enter to activate this console.\n").replace("\n", "\r\n").encode()
SAMPLE_SHELL = b"\r\nBusyBox v1.37.0 built-in shell (ash)\r\n\r\nroot@OpenWrt:~# "
SAMPLE_UBOOT_PROMPT = b"TFTP from server 192.168.11.10; our IP address is 192.168.11.1\r\nRetry count exceeded\r\nIPQ807x# "
SAMPLE_RESET_ECHO = b"reset\r\nresetting ...\r\n"
SAMPLE_PROMPT_AGAIN = b"\r\nIPQ807x# "  # what an idle U-Boot prompt prints for one Enter
SAMPLE_KERNEL_AFTER_PROMPT = b"[    8.988070] kmodloader: loading kernel modules from /etc/modules.d/*\r\n"
SAMPLE_DNSMASQ_START = "dnsmasq: started, version 2.92 DNS disabled"  # first line of a real tftp.log


def start_tftp(args):
    """Copy the image into a private TFTP root and start a TFTP-only dnsmasq."""
    root = args.output / "tftp"
    root.mkdir()
    served = root / args.filename
    shutil.copyfile(args.image, served)
    served.chmod(0o644)
    config = args.output / "dnsmasq.conf"
    config.write_text(
        "port=0\n"  # no DNS service
        f"listen-address={args.server_address}\nbind-dynamic\n"  # starts without the address; binds when it appears
        f"enable-tftp={args.interface}\ntftp-root={root}\n"
        "log-facility=-\npid-file=\n"
        f"user={pwd.getpwuid(os.geteuid()).pw_name}\n"  # keep the invoking user so the 0700 output stays readable
    )
    with (args.output / "tftp.log").open("xb") as log:
        argv = lab.in_netns(["dnsmasq", "--keep-in-foreground", "--conf-file=" + str(config)], args.netns)
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def wait_tftp(proc, args, result, started):
    """Announce tftp_ready once dnsmasq has started, then wait until it listens on the address; return an error or None."""
    server = str(args.server_address)
    while time.monotonic() < started + args.timeout:
        if proc.poll() is not None:
            return "dnsmasq exited; see tftp.log"
        if result["stage"] == "not_started":
            if "started, version" in (args.output / "tftp.log").read_text(errors="replace"):
                result["stage"] = "tftp_ready"
                print("tftp_ready", file=sys.stderr, flush=True)
        else:
            shown = lab.run(lab.in_netns(["ip", "-j", "address", "show", "dev", args.interface], args.netns), 5)
            links = json.loads(shown.stdout or "[]")
            sockets = lab.run(lab.in_netns(["ss", "-H", "-lunp", "sport = :69"], args.netns), 5).stdout
            if any(a["local"] == server for link in links for a in link.get("addr_info", [])) \
                    and f"{server}:69" in sockets and f"pid={proc.pid}," in sockets:
                result["milestones"]["listener_ready"] = round(time.monotonic() - started, 3)
                return None
        time.sleep(0.1)
    if result["stage"] == "not_started":
        return "dnsmasq did not log its start in time"
    return f"{server} on {args.interface} with this dnsmasq listening not seen in time"


def watch_boot(port, result, timeout, started, resets_allowed):
    """Follow the console from U-Boot to a quiet Linux prompt, typing only reset or Enter; return an error or None."""
    milestones = result["milestones"]
    text = bytearray()
    answered = resets = 0  # answered: text length when input was last typed; older text is never answered again
    last_byte = time.monotonic()
    until = last_byte + timeout
    while time.monotonic() < until:
        data = lab.read_serial(port, min(0.2, until - time.monotonic()))
        if data:
            text += data
            last_byte = time.monotonic()
        now = round(time.monotonic() - started, 3)
        for name, stage, pattern in MARKS:
            if name not in milestones and pattern.search(text):
                milestones[name] = now
                result["stage"] = stage or result["stage"]
        sizes = MARKS[1][2].findall(text)
        result["transferred_bytes"] = int(sizes[-1]) if sizes else None
        recent = text[answered:]
        quiet = time.monotonic() - last_byte
        if "kernel_started" in milestones and "linux_prompt" not in milestones and LINUX_PROMPT.search(recent):
            milestones["linux_prompt"] = now
            result["stage"] = "linux_prompt"
        if "linux_prompt" in milestones:
            if quiet >= QUIET_SECONDS:
                milestones["console_quiet"] = now
                return None
            continue
        if UBOOT_PROMPT.search(recent):
            if resets >= resets_allowed:
                return f"router stopped at the U-Boot prompt after {resets} resets"
            resets += 1
            answer, name = b"reset\r", "uboot_reset_sent"
        elif b"Please press Enter" in recent or quiet >= SILENT_SECONDS:
            answer, name = b"\r", "enter_sent"
        else:
            continue
        lab.write_serial(port, answer, time.monotonic() + 2)
        milestones[name] = now
        answered = len(text)
        last_byte = time.monotonic()
    if "linux_prompt" in milestones:
        return f"console did not stay quiet for {QUIET_SECONDS} s after the Linux prompt"
    return f"no Linux prompt within {timeout} s"


def read_identity(port, result):
    """Read boot_id, kernel release and root filesystem type over the console; return an error or None."""
    command = "cat /proc/sys/kernel/random/boot_id /proc/sys/kernel/osrelease; grep ' / ' /proc/mounts"
    outcome = lab.serial_command(port, command, LINUX_PROMPT, 15)
    if not outcome.completed or outcome.exit_code != 0:
        return "identity command did not complete on the console"
    lines = outcome.output.splitlines()
    result["boot_id"], result["kernel"] = lines[0].strip(), lines[1].strip()
    result["root_fs"] = lines[-1].split()[2] if len(lines) > 2 else None
    return None


def ssh_boot_id(ssh, netns, deadline):
    """Poll ssh until it answers with a boot_id or the deadline passes; return (boot_id, refusal)."""
    while time.monotonic() < deadline:
        argv, refusal = lab.ssh_argv(*ssh, netns)
        if refusal:
            return None, refusal
        answer = lab.run(argv + ["cat /proc/sys/kernel/random/boot_id"], 15)
        if answer.returncode == 0:
            return answer.stdout.strip(), None
        time.sleep(2)
    return None, None


def judge(result, ssh_wanted):
    """Return the first unmet success condition, or None when all hold."""
    conditions = (
        (result["boot_id"] is None, "boot_id not read"),
        (result["previous_boot_id"] == result["boot_id"], "boot_id did not change after reboot"),
        (result["transferred_bytes"] != result["image_bytes"],
         f"U-Boot reported {result['transferred_bytes']} bytes, image has {result['image_bytes']}"),
        (not result["served_by_dnsmasq"], "dnsmasq log does not show the file sent to the router"),
        (result["root_fs"] not in ("tmpfs", "rootfs", "ramfs"),
         f"root filesystem is {result['root_fs']}, not a RAM filesystem"),
        (ssh_wanted and result["ssh_boot_id"] != result["boot_id"], "ssh boot_id differs from the console boot_id"),
    )
    return next((reason for unmet, reason in conditions if unmet), None)


def boot(args):
    """Run the whole TFTP boot observation and return the result dict."""
    started = time.monotonic()
    result = {"stage": "not_started", "milestones": {}, "image_bytes": args.image.stat().st_size,
              "transferred_bytes": None, "served_by_dnsmasq": False, "previous_boot_id": None, "boot_id": None,
              "kernel": None, "root_fs": None, "ssh_boot_id": None, "output_dir": str(args.output), "error": None}
    args.output.mkdir(mode=0o700, parents=True)
    dnsmasq = port = None
    try:
        # The console is open before the address can appear: U-Boot prints right after its ARP is answered.
        port = lab.open_serial(args.serial, args.baud, args.output / "serial.log")
        dnsmasq = start_tftp(args)
        result["error"] = wait_tftp(dnsmasq, args, result, started)
        if result["error"]:
            return result
        if args.start == "reboot":
            if lab.console_is_booting(port, 0.5):
                result["error"] = "boot activity on console; reboot not sent"
                return result
            before = lab.serial_command(port, "cat /proc/sys/kernel/random/boot_id", LINUX_PROMPT, 10)
            if not before.completed:
                result["error"] = "no idle Linux shell on the console before reboot"
                return result
            result["previous_boot_id"] = before.output.strip()
            lab.write_serial(port, b"reboot\r", time.monotonic() + 2)
        result["error"] = watch_boot(port, result, args.timeout - (time.monotonic() - started), started,
                                     args.uboot_resets)
        if result["error"]:
            return result
        result["error"] = read_identity(port, result)
    except (OSError, TimeoutError, termios.error, ValueError) as error:
        # Boundary: host command, file and device errors are reported, not raised.
        result["error"] = str(error)
    except KeyboardInterrupt:
        result["error"] = "interrupted"
    finally:
        if dnsmasq is not None:
            lab.stop_process(dnsmasq, signal.SIGTERM, 5)
        if port is not None:
            cleanup = lab.close_serial(port)
            if cleanup and not result["error"]:
                result["error"] = "serial cleanup: " + "; ".join(cleanup)
    if result["error"]:
        return result
    log = (args.output / "tftp.log").read_text(errors="replace").splitlines()
    result["served_by_dnsmasq"] = any("sent " in line and args.filename in line and args.router_tftp_address in line
                                      for line in log)
    if args.ssh:
        result["ssh_boot_id"], result["error"] = ssh_boot_id(args.ssh, args.ssh_netns, started + args.timeout)
    result["error"] = result["error"] or judge(result, bool(args.ssh))
    result["stage"] = "identity_checked" if result["error"] is None else result["stage"]
    return result


def self_test():
    """Replay console texts through a pseudo-terminal into watch_boot, and check the waits and the judge."""
    booted = ["tftp_request", "transfer_done", "kernel_started", "enter_sent", "linux_prompt", "console_quiet"]
    after_reset = ["tftp_request", "uboot_reset_sent"] + booted[1:]
    # (name, [(delay s, console bytes)], resets allowed, timeout s, expected error, bytes the host must type,
    #  milestone order, least seconds from linux_prompt to console_quiet)
    cases = (
        ("normal boot, Enter for 'Please press Enter'", [(0.2, SAMPLE_TO_ENTER), (0.8, SAMPLE_SHELL)], 1, 6, None, b"\r",
         booted, QUIET_SECONDS),
        ("kernel text after the prompt delays the identity read",
         [(0.2, SAMPLE_TO_ENTER), (0.8, SAMPLE_SHELL), (2.1, SAMPLE_KERNEL_AFTER_PROMPT)], 1, 7, None, b"\r", booted,
         QUIET_SECONDS + 1.3),
        ("U-Boot prompt with no reset allowed fails", [(0.2, SAMPLE_UBOOT_PROMPT)], 0, 3,
         "router stopped at the U-Boot prompt after 0 resets", b"", ["tftp_request"], None),
        ("U-Boot prompt gets one reset, then the boot completes",
         [(0.2, SAMPLE_UBOOT_PROMPT), (1.2, SAMPLE_RESET_ECHO + SAMPLE_TO_ENTER), (1.8, SAMPLE_SHELL)], 1, 7, None,
         b"reset\r\r", after_reset, QUIET_SECONDS),
        ("silent console gets Enter, the prompt it shows gets reset",
         [(SILENT_SECONDS + 0.6, SAMPLE_PROMPT_AGAIN), (SILENT_SECONDS + 1.6, SAMPLE_RESET_ECHO + SAMPLE_TO_ENTER),
          (SILENT_SECONDS + 2.2, SAMPLE_SHELL)], 1, SILENT_SECONDS + 8, None, b"\rreset\r\r",
         ["enter_sent", "uboot_reset_sent", "tftp_request", "transfer_done", "kernel_started", "linux_prompt",
          "console_quiet"], QUIET_SECONDS),
    )
    checks = []
    for name, schedule, resets_allowed, timeout, expected_error, expected_typed, expected_order, least_quiet in cases:
        master, slave = os.openpty()
        port = lab.open_serial(os.ttyname(slave), 115200)
        for delay, sample in schedule:
            threading.Timer(delay, os.write, (master, sample)).start()
        result = {"stage": "tftp_ready", "milestones": {}, "transferred_bytes": None}
        error = watch_boot(port, result, timeout, time.monotonic(), resets_allowed)
        time.sleep(max(0.0, schedule[-1][0] - timeout))  # a failing case returns before the last write
        typed = b""
        while select.select([master], [], [], 0.3)[0]:  # pty delivery lags the write; read until quiet
            typed += os.read(master, 100)
        lab.close_serial(port)
        os.close(slave)
        os.close(master)
        ok = error == expected_error and typed == expected_typed and list(result["milestones"]) == expected_order
        if expected_error is None:
            waited = result["milestones"]["console_quiet"] - result["milestones"]["linux_prompt"]
            ok = ok and result["transferred_bytes"] == 1234 and waited >= least_quiet - 0.05
        checks.append((name, ok))
    complete = {"boot_id": "b", "previous_boot_id": "a", "transferred_bytes": 5, "image_bytes": 5,
                "served_by_dnsmasq": True, "root_fs": "tmpfs", "ssh_boot_id": None}
    checks.append(("judge accepts a complete boot", judge(complete, False) is None))
    checks.append(("judge rejects unchanged boot_id",
                   judge(dict(complete, boot_id="a"), False) == "boot_id did not change after reboot"))
    with tempfile.TemporaryDirectory() as scratch:
        args = argparse.Namespace(output=Path(scratch), server_address="192.0.2.1", interface="lo", netns=None, timeout=1.5)
        with (args.output / "tftp.log").open("xb") as log:
            fake = subprocess.Popen(["sh", "-c", f"echo '{SAMPLE_DNSMASQ_START}' >&2; sleep 5"], stderr=log)
        result = {"stage": "not_started", "milestones": {}}
        begun = time.monotonic()
        # 192.0.2.1 (documentation range) is never on lo, so the wait must announce tftp_ready and then give up.
        error = wait_tftp(fake, args, result, begun)
        checks.append(("dnsmasq start is seen in its log", result["stage"] == "tftp_ready"))
        checks.append(("listener wait gives up when the address never appears",
                       error.endswith("not seen in time") and time.monotonic() - begun >= 1.5))
        lab.stop_process(fake, signal.SIGTERM, 2)
        begun = time.monotonic()
        checks.append(("listener wait stops at once when dnsmasq has exited",
                       wait_tftp(fake, args, result, begun) == "dnsmasq exited; see tftp.log"
                       and time.monotonic() - begun < 1))
    begun = time.monotonic()  # 127.0.0.1 from 127.0.0.1 leaves through lo, so asking for eno2 must refuse at once
    boot_id, refusal = ssh_boot_id(("root@127.0.0.1", "127.0.0.1", "eno2"), None, begun + 5)
    checks.append(("ssh poll refuses before any ssh when the route uses another port",
                   boot_id is None and "through lo, not eno2" in (refusal or "") and time.monotonic() - begun < 1))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, run the boot observation and print the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", type=Path, help="host RAM image (FIT .itb) to serve")
    parser.add_argument("--interface", help="host TFTP interface, e.g. enxd4a2cde31c38 (required)")
    parser.add_argument("--netns", help="host network namespace holding the interface")
    parser.add_argument("--server-address", type=ipaddress.IPv4Address, default="192.168.11.10",
                        help="TFTP server IPv4; added by test-network after tftp_ready")
    parser.add_argument("--router-tftp-address", default="192.168.11.1", help="U-Boot's own IPv4 address")
    parser.add_argument("--filename", default=DEFAULT_IMAGE_NAME, help="file name U-Boot requests")
    parser.add_argument("--serial", default="/dev/ttyUSB0", help="host serial device (default /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200)")
    parser.add_argument("--start", choices=("waiting", "reboot"), help="waiting: just observe; reboot: send one reboot first")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds for the whole observation (default 180)")
    parser.add_argument("--uboot-resets", type=int, default=1,
                        help="times to type reset at the U-Boot prompt before failing (default 1)")
    parser.add_argument("--ssh", nargs=3, metavar=("TARGET", "SOURCE", "INTERFACE"),
                        help="confirm boot_id over ssh, e.g. root@192.168.1.1 192.168.1.115 eno2")
    parser.add_argument("--ssh-netns", help="host namespace for the ssh connection")
    parser.add_argument("--output", type=Path, help="new directory for logs and the served image")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not (args.image and args.interface and args.start and args.output):
        parser.error("--image, --interface, --start and --output are required")
    if args.output.exists() or args.interface in PROTECTED_INTERFACES:
        parser.error("--output must not exist; --interface must not be eno1 or enx224d686d5002")
    if not args.image.is_file() or args.timeout <= 0 or args.uboot_resets < 0:
        parser.error("--image must be a file, --timeout positive and --uboot-resets not negative")
    lab.stop_on_signals()
    result = boot(args)
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
