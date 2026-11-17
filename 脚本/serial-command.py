#!/usr/bin/env python3
"""Run one command on an already running Linux serial console.

Precondition: the console is an idle Linux root shell (for example right after
boot-ram.py reached the prompt). Before sending, the console is read for 0.5 s;
if U-Boot or kernel boot text appears, nothing is sent and the tool exits 1,
because any byte could interrupt a boot countdown.

The command is sent once between random markers, so its output and exit status
are separated from the console echo. Completion means the end marker and the
next prompt were both seen. On timeout nothing is resent and no Ctrl-C is sent:
the command may still be running on the router (may_still_run=true).

Stdout: one JSON object with command, completed, exit_code, output,
may_still_run, seconds, error. Exit 0 = completed with exit status 0;
exit 1 = anything else; exit 2 = usage error.

Example:
  python3 serial-command.py --command 'cat /proc/sys/kernel/random/boot_id'
Self-test (no device): python3 serial-command.py --self-test
"""

import argparse
import os
import re
import shlex
import sys
import termios
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab

# Self-test data: console text of a booting router, and replies of a fake shell.
BOOT_TEXT = b"U-Boot 2016.01 (Buffalo)\r\n\r\nHit any key to stop autoboot:  2 \r\n"
FAKE_SHELL = {"uname -r": ("6.18.44\n", 0), "false": ("", 1)}
MARKED_LINE = re.compile(r"printf '\\n%s%s:B\\n' (\w+) (\w+); eval (.*?); printf")


def send_one(device, baud, command, prompt, timeout, log_path):
    """Open the console, refuse on boot activity, run one command and return the result dict."""
    result = {"command": command, "completed": False, "exit_code": None, "output": "",
              "may_still_run": False, "seconds": 0.0, "error": None}
    port = None
    try:
        port = lab.open_serial(device, baud, log_path)
        if lab.console_is_booting(port, 0.5):
            result["error"] = "boot activity on console; nothing sent"
            return result
        outcome = lab.serial_command(port, command, re.compile(prompt.encode()), timeout)
        result.update(completed=outcome.completed, exit_code=outcome.exit_code, output=outcome.output,
                      may_still_run=outcome.may_still_run, seconds=round(outcome.seconds, 3))
        if not outcome.completed:
            result["error"] = "end marker and next prompt not both seen before timeout"
        elif outcome.exit_code:
            result["error"] = f"command exited {outcome.exit_code}"
    except (OSError, TimeoutError, termios.error) as error:
        # Boundary: device and write errors are reported, not raised.
        result["error"] = str(error)
    except KeyboardInterrupt:
        result["error"] = "interrupted"
        result["may_still_run"] = True
    finally:
        if port is not None:
            cleanup = lab.close_serial(port)
            if cleanup and not result["error"]:
                result["error"] = "cleanup: " + "; ".join(cleanup)
    return result


def fake_shell(master, stop):
    """Self-test helper: answer marked commands on a pty master like an OpenWrt shell; 'sleep' never answers."""
    pending = b""
    while not stop.is_set():
        try:
            pending += os.read(master, 4096)
        except OSError:
            return
        while b"\r" in pending:
            line, pending = pending.split(b"\r", 1)
            os.write(master, line + b"\r\n")
            found = MARKED_LINE.search(line.decode())
            if not found:
                continue
            left, right, command = found[1], found[2], shlex.split(found[3])[0]
            if command.startswith("sleep"):
                continue
            output, status = FAKE_SHELL[command]
            reply = f"{left}{right}:B\n{output}\n{left}{right}:E:{status}\nroot@OpenWrt:~# "
            os.write(master, reply.replace("\n", "\r\n").encode())


def self_test():
    """Check refusal on boot text, normal and failing commands, and timeout through a pseudo-terminal."""
    passed = True
    master, slave = os.openpty()
    os.write(master, BOOT_TEXT)
    refused = send_one(os.ttyname(slave), 115200, "uname -r", lab.LINUX_PROMPT, 2, None)
    ok = refused["error"] == "boot activity on console; nothing sent" and not refused["completed"]
    print(("PASS " if ok else "FAIL ") + "refuses on boot activity")
    passed &= ok
    os.close(slave)
    os.close(master)

    master, slave = os.openpty()
    stop = threading.Event()
    threading.Thread(target=fake_shell, args=(master, stop), daemon=True).start()
    normal = send_one(os.ttyname(slave), 115200, "uname -r", lab.LINUX_PROMPT, 3, None)
    ok = normal["completed"] and normal["exit_code"] == 0 and normal["output"] == "6.18.44\n" and normal["error"] is None
    print(("PASS " if ok else "FAIL ") + "runs one command")
    passed &= ok
    failing = send_one(os.ttyname(slave), 115200, "false", lab.LINUX_PROMPT, 3, None)
    ok = failing["completed"] and failing["exit_code"] == 1 and failing["error"] == "command exited 1"
    print(("PASS " if ok else "FAIL ") + "reports nonzero exit")
    passed &= ok
    hung = send_one(os.ttyname(slave), 115200, "sleep 100", lab.LINUX_PROMPT, 0.5, None)
    ok = not hung["completed"] and hung["may_still_run"]
    print(("PASS " if ok else "FAIL ") + "timeout keeps may_still_run")
    passed &= ok
    stop.set()
    os.close(slave)
    os.close(master)
    return 0 if passed else 1


def main():
    """Parse arguments, run the command and print the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="host serial device (default /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200)")
    parser.add_argument("--command", help="one router shell command, quoted for the host shell")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for completion (default 10)")
    parser.add_argument("--prompt", default=lab.LINUX_PROMPT, help="regex for the shell prompt at end of output")
    parser.add_argument("--log", help="new file to receive the raw console bytes")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.command:
        parser.error("--command is required")
    if args.timeout <= 0 or not hasattr(termios, "B" + str(args.baud)):
        parser.error("use a positive timeout and a supported baud rate")
    lab.stop_on_signals()
    result = send_one(args.device, args.baud, args.command, args.prompt, args.timeout, args.log)
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
