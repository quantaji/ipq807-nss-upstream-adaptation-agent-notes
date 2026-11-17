#!/usr/bin/env python3
"""Supervise one host PPPoE server and one bounded target client.

Run on the Linux host with Python 3, pppoe-server, pppd at /usr/sbin/pppd, ip,
ssh and getent. Local child ownership uses Linux subreaper and /proc children.
The target needs pppd, ip with JSON output, setsid, flock, mkfifo, sleep, tail
and ordinary shell tools; plugin autodetection also uses find.
Both PPP daemons need a version-matched PPPoE plugin and network privileges.
Specify plugin paths if locations cannot be uniquely derived from the actual
pppd versions. No dependencies are installed.

This is one unauthenticated IPv4 test session, without automatic redial or
default-route installation; it does not exercise PPP authentication.

The two lower interfaces must already exist and be UP on the same intended
Ethernet service, including any required VLAN. The server is local (optionally
in an existing --server-netns); the client runs on --client-ssh. Neither lower
name is the negotiated PPP interface. This tool creates no VLAN, bridge, base
route, firewall rule, qdisc or driver policy. Permit the desired data traffic
separately; PPP readiness does not prove it can pass the existing firewall.

SSH uses the invoking user's config unless --ssh-config is supplied. Provide
a host control interface/source or equivalent config binding, strict known-host
trust, and --expected-boot obtained independently from the intended target's
/proc/sys/kernel/random/boot_id. A source-bound direct IPv4 route and the boot
ID are checked at setup; mutating target commands also check the boot ID; no proxy, ambiguous destination, trust bypass or password
prompt is supported. --ssh-netns is the host control namespace, independent
of the server namespace. --connect-timeout is connection establishment only;
finite remote queries have their own bounded command deadlines.

Example (from the repository root):
  sudo python3 scripts/pppoe-session.py \\
    --server-interface "$HOST_LOWER" --client-interface "$TARGET_LOWER" \\
    --server-address "$SERVER_PPP_IP" --client-address "$CLIENT_PPP_IP" \\
    --service "$SERVICE" --ac-name "$AC_NAME" --mtu 1492 --mru 1492 \\
    --server-plugin "$HOST_PLUGIN" --client-plugin "$TARGET_PLUGIN" \\
    --client-ssh "$TARGET" --ssh-interface "$CONTROL_IF" \\
    --ssh-source "$CONTROL_IP" --ssh-known-hosts "$KNOWN_HOSTS" \\
    --expected-boot "$BOOT_ID" --ready-timeout 15 --duration 60 \\
    --output "$OUT"
Set lower names and control addresses from current interfaces, PPP addresses
to the intended peer pair, and SERVICE/AC_NAME to explicit experiment names.
Plugin paths belong to each side's installed pppd. TARGET is a trusted
[user@]host/SSH alias with noninteractive privileged access; KNOWN_HOSTS holds
its verified key. BOOT_ID is the current independently verified target boot.
OUT is a new host directory. Add explicit SSH config/identity when running
under sudo would otherwise select the wrong user's credentials.

Wait for the stdout JSON with ready=true before using the session. It reports
the actual client/server PPP names and negotiated wire SID; do not assume
ppp0. observations in OUT/result.json retain addresses, peer, MTU and session
tables. Ready means those checks agreed, not bidirectional payload or hardware
offload success. The process stays in the foreground while other commands may
use the session. --duration starts at readiness, not at program startup.
--ready-timeout bounds negotiation after launching the asynchronous client.
Timeout, early daemon exit or session loss ends supervision without redial.

Let duration expire or send SIGINT/SIGTERM (Ctrl-C) for normal teardown after
readiness. The tool stops its client/server processes, checks PPP interface
release, collects logs and removes its private target control/exit/log files and
directory. A native exit receipt and free inherited directory lock prove client release.
The independent target lifetime also bounds the client if host supervision
is lost; it is NOT evidence that cleanup was observed. Failed remote cleanup
leaves remote_cleanup/cleanup_errors explaining what remains unconfirmed.
Underlying interfaces, existing sessions, base network and global PPP
scripts/options are not modified. Host logs/options are retained.

Read OUT/result.json first: preparation, execution, observations, first_error,
cleanup and remote_cleanup; server/client logs give negotiation details.
Exit 0 means supervision ended without a recorded error and cleanup completed;
an intentional stop after ready can also exit 0. Exit 1 includes preparation,
session or cleanup failure; CLI usage errors exit 2. No traffic is generated
and no hardware function is qualified by this script."""

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import time

STOP = False


def interrupted(signum, frame):
    global STOP
    if signum in (signal.SIGINT, signal.SIGTERM):
        STOP = True


def run_command(argv, report, owned, deadline, check=True, phase_end=None):
    entry = {"argv": argv, "start_ns": time.time_ns()}
    report["commands"].append(entry)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        with selectors.PollSelector() as reads:
            reads.register(owned["wake"][0], selectors.EVENT_READ, None)
            for name in output:
                reads.register(getattr(process, name), selectors.EVENT_READ, name)
            for limit in (deadline, None):
                if limit is None:
                    ended = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if ended is not None and (ended.si_code != os.CLD_EXITED or ended.si_status != 0):
                        entry["error"] = "Command had already exited before stop: " + str(ended.si_status)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        report["cleanup_errors"].append("Command stop: " + str(error))
                end = limit if limit is not None else time.monotonic() + 2
                try:
                    while len(reads.get_map()) > 1 or process.poll() is None:
                        ready = reads.select(max(0, end - time.monotonic()))
                        if limit is not None and phase_end is not None and STOP:
                            entry["cancelled"] = "signal"
                            break
                        if time.monotonic() >= end:
                            raise TimeoutError("Command IO deadline")
                        for key, _ in ready:
                            data = os.read(key.fd, 65536)
                            if key.data is None:
                                continue
                            if data:
                                output[key.data].extend(data)
                            else:
                                reads.unregister(key.fileobj)
                    else:
                        break
                except OSError as error:
                    if limit is None:
                        report["cleanup_errors"].append("Command drain: " + str(error))
                    elif isinstance(error, TimeoutError):
                        entry["timed_out"] = True
                        if deadline == phase_end and report["preparation"] == "ready":
                            entry["cancelled"] = "duration"
                    else:
                        entry["error"] = str(error)
    except OSError as error:
        entry["error"] = str(error)
    finally:
        if process is not None:
            if process.returncode is None:
                owned["unwaited_commands"].append(process)
            entry["exit_code"] = process.returncode
            process.stdout.close()
            process.stderr.close()
        entry.update(stdout=output["stdout"].decode(errors="replace"), stderr=output["stderr"].decode(errors="replace"), end_ns=time.time_ns())
    if check and (entry.get("error") or (not entry.get("cancelled") and (entry.get("timed_out") or entry.get("exit_code") != 0))):
        raise RuntimeError("Command failed: " + shlex.join(argv))
    return entry, output["stdout"]


def resolve_ssh(args, owned, report):
    prefix = ["ip", "netns", "exec", args.ssh_netns] if args.ssh_netns else []
    base = ["ssh", "-S", "none"]
    for option in (
        "ControlMaster=no",
        "ControlPersist=no",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ConnectionAttempts=1",
        "ConnectTimeout=" + str(args.connect_timeout),
        "RequestTTY=no",
        "ClearAllForwardings=yes",
        "RemoteCommand=none",
    ):
        base += ["-o", option]
    for flag, value in (("-F", args.ssh_config), ("-l", args.ssh_user), ("-p", args.ssh_port), ("-i", args.ssh_identity)):
        if value is not None:
            base += [flag, str(value)]
    if args.ssh_known_hosts:
        base += ["-o", "UserKnownHostsFile=" + str(args.ssh_known_hosts)]
    if args.ssh_source:
        base += ["-b", args.ssh_source]
    if args.ssh_interface:
        base += ["-B", args.ssh_interface]
    effective = run_command(prefix + base + ["-G", args.ssh_target], report, owned, time.monotonic() + 10)[0]["stdout"]
    config = dict(line.split(" ", 1) for line in effective.splitlines())
    if config.get("proxycommand", "none") != "none" or config.get("proxyjump", "none") != "none":
        raise ValueError("Configured proxy cannot establish the selected direct control path")
    hostname = config["hostname"]
    # Resolve once in the actual namespace; never try several same-name devices.
    records = run_command(prefix + ["getent", "ahostsv4", hostname], report, owned, time.monotonic() + 10)[0]["stdout"].splitlines()
    destinations = {line.split()[0] for line in records if line.split()}
    if len(destinations) != 1:
        raise ValueError("SSH destination is absent or ambiguous")
    destination = destinations.pop()
    interface = args.ssh_interface or config.get("bindinterface")
    source = args.ssh_source or config.get("bindaddress")
    if interface == "none":
        interface = None
    if source in ("none", "*"):
        source = None
    addresses = json.loads(run_command(prefix + ["ip", "-j", "-4", "address", "show"], report, owned, time.monotonic() + 10)[0]["stdout"])
    choices = [(link["ifname"], address["local"]) for link in addresses for address in link["addr_info"] if address["family"] == "inet" and (not interface or interface == link["ifname"]) and (not source or source == address["local"])]
    if not (interface or source) or len(choices) != 1:
        raise ValueError("Specify an unambiguous SSH source/interface, directly or in SSH config")
    interface, source = choices[0]
    route = json.loads(run_command(prefix + ["ip", "-j", "route", "get", destination, "from", source], report, owned, time.monotonic() + 10)[0]["stdout"])
    if len(route) != 1 or route[0].get("dev") != interface:
        raise ValueError("SSH source-bound route does not use the selected control interface")
    alias = config.get("hostkeyalias") or hostname
    report["ssh"] = {
        "prefix": prefix,
        "interface": interface,
        "source": source,
        "destination": destination,
        "argv": prefix + base + ["-b", source, "-o", "Hostname=" + destination, "-o", "HostKeyAlias=" + alias, args.ssh_target],
        "route": route,
        "effective_config": effective,
    }


def prepare_ppp_options(args, owned, report):
    plugins = []
    for side, supplied in (("server", args.server_plugin), ("client", args.client_plugin)):
        if supplied:
            plugins.append(supplied)
            continue
        argv = owned["server_prefix"] + ["/usr/sbin/pppd", "--version"] if side == "server" else report["ssh"]["argv"] + ["pppd --version"]
        version, _ = run_command(argv, report, owned, time.monotonic() + 10)
        versions = set(re.findall(r"pppd version ([0-9.]+)", version["stdout"] + version["stderr"]))
        if len(versions) != 1:
            raise ValueError("Cannot identify actual pppd version: " + side)
        directory = Path("/usr/lib/pppd") / versions.pop()
        if side == "server":
            candidates = [str(path) for path in directory.iterdir() if path.name in ("pppoe.so", "rp-pppoe.so") and path.is_file()]
        else:
            listing, _ = run_command(
                report["ssh"]["argv"] + ["find " + shlex.quote(str(directory)) + " -maxdepth 1 -type f"],
                report,
                owned,
                time.monotonic() + 10,
            )
            candidates = [path for path in listing["stdout"].splitlines() if Path(path).name in ("pppoe.so", "rp-pppoe.so")]
        if len(candidates) != 1:
            raise ValueError("Supply one version-matched PPPoE plugin: " + side)
        plugins.append(candidates[0])
    base = [
        "noauth",
        "nodefaultroute",
        "noreplacedefaultroute",
        "nodefaultroute6",
        "noproxyarp",
        "noaccomp",
        "nopcomp",
        "novj",
        "novjccomp",
        "noccp",
        "nobsdcomp",
        "nodeflate",
        f"mtu {args.mtu}",
        f"mru {args.mru}",
        "nopersist",
        "debug",
        "ip-up-script /bin/true",
        "ip-down-script /bin/true",
        "ipv6-up-script /bin/true",
        "ipv6-down-script /bin/true",
    ]
    client = base + ["noipdefault", "ipcp-accept-local", "ipcp-accept-remote", "maxfail 1", "nodetach"]
    server = base + ["ip-pre-up-script /bin/true", "net-init-script /bin/true", "net-pre-up-script /bin/true"]
    log_path = str(args.output.resolve() / "server-pppd.log")
    quoted_log_path = '"' + log_path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    server += ["ifname " + owned["server_interface"], "logfile " + quoted_log_path]
    client.append("logfile " + owned["remote_directory"] + "/pppd.log")
    for name, lines in (("server", server), ("client", client)):
        (args.output / (name + ".options")).write_text("\n".join(lines) + "\n")
    report["plugins"] = dict(zip(("server", "client"), plugins))
    return plugins, [word for line in client for word in line.split()]


def read_endpoint(args, owned, report, side, deadline, phase_end=None, receipt=None):
    interface = shlex.quote(owned[side + "_interface"])
    command = (
        "printf 'SESSION\\n'\n"
        "if test -r /proc/net/pppoe; then cat /proc/net/pppoe || exit 1; fi\n"
        "printf '\\nADDRESS\\n'\n"
        f"if test -e /sys/class/net/{interface}; then\n"
        f"    ip -j address show dev {interface} || exit 1\n"
        "else\n    printf '[]\\n'\nfi\n"
    )
    if side == "client":
        logfile = shlex.quote(owned["remote_directory"] + "/pppd.log")
        command += "printf '\\nLOG\\n'\n"
        command += f"if test -f {logfile}; then tail -c +{owned['logs']['client-pppd'].tell() + 1} {logfile} || exit 1; fi"
    prefix = report["ssh"]["argv"] if side == "client" else owned["server_prefix"] + ["sh", "-c"]
    entry, raw = run_command(prefix + [command], report, owned, deadline, phase_end=phase_end)
    if side == "client":
        filename = "client-observation.txt" if receipt is None else "client-final.txt"
        (args.output / filename).write_bytes(receipt + raw if receipt is not None else raw)
    if entry.get("cancelled"):
        return None
    sessions, addresses = raw.split(b"\nADDRESS\n", 1)
    if side == "client":
        addresses, log = addresses.split(b"\nLOG\n", 1)
        owned["logs"]["client-pppd"].write(log)
        lines = (owned["log_pending"] + log).split(b"\n")
        owned["log_pending"] = b"" if receipt is not None else lines.pop()
        for line in lines:
            match = re.fullmatch(rb"PPP session is ([0-9]+)", line)
            if match is not None:
                owned["sid"] = int(match[1])
                if owned["sid"] == 0:
                    raise ValueError("Negotiated client SID is out of range")
    return sessions, json.loads(addresses)


def read_ppp_session(args, owned, report, phase_end):
    query_end = min(phase_end, time.monotonic() + 3)
    endpoints = []
    for side in ("client", "server"):
        if STOP or time.monotonic() >= query_end:
            if STOP or query_end == phase_end:
                return None
            raise TimeoutError("PPP observation query budget expired")
        if owned["server"].poll() is not None or owned["client_ssh"].poll() is not None:
            return {"state": "failed", "reason": "Owned server/client supervisor exited"}
        endpoint = read_endpoint(args, owned, report, side, query_end, phase_end=phase_end)
        if endpoint is None:
            return None
        endpoints.append(endpoint)
    client_rows = [line.split() for line in endpoints[0][0].decode().splitlines()[1:] if line.split()]
    host_rows = [line.split() for line in endpoints[1][0].decode().splitlines()[1:] if line.split()]
    client_rows = [row for row in client_rows if len(row) == 3 and row[2] == args.client_interface and row[1].lower() == owned["server_mac"].lower()]
    host_rows = [row for row in host_rows if len(row) == 3 and row[2] == args.server_interface and row[1].lower() == owned["client_mac"].lower()]
    if len(client_rows) > 1 or len(host_rows) > 1:
        return {"state": "failed", "reason": "Session association is ambiguous"}
    if len(client_rows) != 1 or len(host_rows) != 1 or any(len(item[1]) != 1 for item in endpoints):
        return {"state": "pending", "reason": "PPP session/IPCP not yet visible"}
    client_address, host_address = endpoints[0][1][0], endpoints[1][1][0]
    for address, local, peer in ((host_address, args.server_address, args.client_address), (client_address, args.client_address, args.server_address)):
        ipv4 = [item for item in address["addr_info"] if item["family"] == "inet"]
        if not ipv4:
            return {"state": "pending", "reason": "IPCP address absent"}
        if len(ipv4) != 1 or ipv4[0]["local"] != local or ipv4[0].get("address") != peer or address["mtu"] != args.mtu:
            return {"state": "failed", "reason": "Negotiated address/peer/MTU differs", "observed": address}
    if owned["sid"] is None:
        return {"state": "failed", "reason": "Negotiated client SID absent"}
    if socket.ntohs(int(host_rows[0][0], 16)) != owned["sid"]:
        return {"state": "failed", "reason": "Client/server SIDs differ"}
    if STOP or time.monotonic() >= query_end:
        if STOP or query_end == phase_end:
            return None
        raise TimeoutError("PPP observation query budget expired")
    return {"state": "ready", "sid": owned["sid"], "client": client_address, "server": host_address, "client_session": client_rows[0], "server_session": host_rows[0], "monotonic_ns": time.monotonic_ns()}


def stop_ppp(args, owned, report):
    errors = report["cleanup_errors"]
    setup = owned["remote_setup"]
    if setup is not None and not setup.get("stdout", "").startswith("CREATED\n"):
        if "exit_code" in setup and setup["exit_code"] not in (73, 125):
            errors.append("Remote setup effects unconfirmed; no acquisition receipt")
            report["remote_cleanup"] = "unconfirmed"
    if setup is not None and setup.get("stdout", "").startswith("CREATED\n"):
        directory = shlex.quote(owned["remote_directory"])
        control = f"""cd {directory} || exit 1
        exec 9<. || exit 1
        lock_error=$(flock -n 9 2>&1)
        lock_result=$?
        if test "$lock_result" -ne 0; then
            if test -n "$lock_error"; then printf '%s\\n' "$lock_error" >&2; exit 1; fi
            test -p control || exit 1
            exec 8<>control || exit 1
            printf 'TERM\\n' >&8 || exit 1
            flock 9 || exit 1
        fi
        rm -f control || exit 1
        if test -f exit; then printf 'EXIT\\n'; cat exit || exit 1
        else printf 'NO_RECEIPT\\n'; fi
        """
        try:
            entry, receipt = run_command(report["ssh"]["argv"] + [owned["boot"] + control], report, owned, time.monotonic() + 7, check=False)
            if entry.get("timed_out") or entry.get("error") or entry.get("exit_code") != 0:
                raise RuntimeError("Remote client completion unconfirmed: " + entry.get("stderr", ""))
            report["remote_cleanup"] = "observed" if receipt.startswith(b"EXIT\n") or (receipt == b"NO_RECEIPT\n" and owned["client_ssh"] is None) else "unconfirmed"
            if report["remote_cleanup"] == "unconfirmed":
                errors.append("Remote client ended without a native wait receipt")
            sessions, addresses = read_endpoint(args, owned, report, "client", time.monotonic() + 10, receipt=receipt)
            if addresses:
                errors.append("Owned client PPP interface remains")
            command = owned["boot"] + f"""cd {directory} || exit 1
            rm -f control exit pppd.log || exit 1
            rmdir {directory}
            """
            run_command(report["ssh"]["argv"] + [command], report, owned, time.monotonic() + 10)
        except (OSError, ValueError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append("Remote cleanup unconfirmed: " + str(error))
            report["remote_cleanup"] = "unconfirmed; independent target lifetime remains in force"
    children_file = Path(f"/proc/self/task/{os.getpid()}/children")
    pending = {}
    end = time.monotonic() + 3
    try:
        while True:
            children = [int(value) for value in children_file.read_text().split()]
            if not children:
                break
            now = time.monotonic()
            requested = signal.SIGKILL if now >= end else signal.SIGTERM
            for pid in children:
                ended, status = os.waitpid(pid, os.WNOHANG)
                if ended:
                    status = os.waitstatus_to_exitcode(status)
                    pending.pop(pid, None)
                    for process in (owned["client_ssh"], owned["server"], *owned["unwaited_commands"]):
                        if process is not None and process.returncode is None and process.pid == pid:
                            process.returncode = status
                    if status == -signal.SIGKILL:
                        errors.append("Forced local child termination: " + str(pid))
                    continue
                if pending.get(pid) != requested:
                    os.kill(pid, requested)
                    pending[pid] = requested
            if now >= end + 2:
                if children_file.read_text().split():
                    errors.append("Local children remain after cleanup deadline")
                break
            time.sleep(0.05)
    except OSError as error:
        errors.append("Local child cleanup: " + str(error))
    if owned["server"] is not None:
        interface = shlex.quote(owned["server_interface"])
        command = f"if test -e /sys/class/net/{interface}; then ip -j address show dev {interface} || exit 1; else printf '[]'; fi"
        try:
            entry, raw = run_command(owned["server_prefix"] + ["sh", "-c", command], report, owned, time.monotonic() + 10)
            if json.loads(raw):
                errors.append("Owned server PPP interface remains")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append("Server interface cleanup: " + str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-netns", help="Existing HOST namespace for server and its lower interface; default: current")
    parser.add_argument("--server-interface", required=True, help="Existing UP HOST Ethernet/VLAN lower, not the PPP interface to be created")
    parser.add_argument("--client-interface", required=True, help="Existing UP TARGET Ethernet/VLAN lower for the remote PPP client")
    parser.add_argument("--server-address", required=True, help="IPv4 address assigned to the HOST PPP end, without a prefix")
    parser.add_argument("--client-address", required=True, help="IPv4 address assigned to the TARGET PPP end, without a prefix")
    parser.add_argument("--service", required=True, help="Explicit PPPoE service name used by both server advertisement and client selection")
    parser.add_argument("--ac-name", required=True, help="Explicit access-concentrator name advertised by server and requested by client")
    parser.add_argument("--mtu", type=int, required=True, help="PPP MTU in bytes requested on both sides; readiness checks the actual negotiated MTU")
    parser.add_argument("--mru", type=int, required=True, help="PPP MRU in bytes requested on both sides")
    parser.add_argument("--server-plugin", help="HOST PPPoE plugin matching host pppd; omitted requires unique version-directory discovery")
    parser.add_argument("--client-plugin", help="TARGET PPPoE plugin matching target pppd; omitted requires unique version-directory discovery")
    parser.add_argument("--ready-timeout", type=float, required=True, help="Seconds to establish and verify the session after asynchronous client launch")
    parser.add_argument("--duration", type=float, required=True, help="Seconds to supervise AFTER readiness; not the total startup/cleanup time")
    parser.add_argument("--client-ssh", dest="ssh_target", required=True, help="TARGET [user@]host or SSH alias with noninteractive PPP privileges")
    parser.add_argument("--ssh-interface", help="HOST interface for SSH control; may also be the server lower, never a target interface name")
    parser.add_argument("--ssh-source", help="HOST control IPv4 source; interface/source or SSH config must identify one route")
    parser.add_argument("--ssh-netns", help="Existing HOST namespace for control SSH, independent of --server-netns")
    parser.add_argument("--ssh-config", help="Existing HOST OpenSSH config (-F); otherwise invoking user's config")
    parser.add_argument("--ssh-user", help="Target SSH username (-l); otherwise alias/config/default")
    parser.add_argument("--ssh-port", type=int, help="Target SSH TCP port (-p); otherwise SSH config/default")
    parser.add_argument("--ssh-identity", help="Existing HOST private key (-i) for noninteractive target authentication")
    parser.add_argument("--ssh-known-hosts", help="Existing HOST file containing verified target keys; strict checking is never disabled")
    parser.add_argument("--connect-timeout", type=int, default=5, help="Seconds per SSH connection establishment (default: 5), not session readiness/lifetime")
    parser.add_argument("--expected-boot", required=True, help="Current TARGET /proc/sys/kernel/random/boot_id from an independent trusted observation")
    parser.add_argument("--output", required=True, type=Path, help="NEW host directory for result.json, options and logs; retained after session cleanup")
    args = parser.parse_args()
    if not all(math.isfinite(v) for v in (args.ready_timeout, args.duration)) or args.connect_timeout <= 0:
        parser.error("Time values must be finite and connection deadlines positive")
    if args.ready_timeout <= 0 or args.duration <= 0 or min(args.mtu, args.mru) <= 0 or not re.fullmatch(r"[0-9a-f-]{36}", args.expected_boot):
        parser.error("Use positive lifetimes/MTU/MRU and the actual expected boot ID")
    if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", value) for value in (args.server_interface, args.client_interface)):
        parser.error("Invalid lower interface name")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {
        "inputs": vars(args) | {"output": str(args.output)},
        "commands": [],
        "preparation": "not_started",
        "first_error": None,
        "cleanup_errors": [],
        "observations": [],
    }
    token = secrets.token_hex(12)
    owned = {
        "server": None,
        "client_ssh": None,
        "remote_setup": None,
        "remote_directory": "/tmp/pppoe-session-" + token,
        "client_interface": "qpp" + token[:8],
        "server_interface": "qps" + token[:8],
        "logs": {},
        "log_pending": b"",
        "sid": None,
        "unwaited_commands": [],
        "wake": None,
        "server_prefix": ["ip", "netns", "exec", args.server_netns] if args.server_netns else [],
        "boot": f'test "$(cat /proc/sys/kernel/random/boot_id)" = {shlex.quote(args.expected_boot)} || exit 125\n',
    }
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGCHLD):
        signal.signal(sig, interrupted)
    try:
        owned["wake"] = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        signal.set_wakeup_fd(owned["wake"][1])
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(36, 1, 0, 0, 0) == -1:  # PR_SET_CHILD_SUBREAPER
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        resolve_ssh(args, owned, report)
        entry, raw = run_command(owned["server_prefix"] + ["ip", "-j", "link", "show", "dev", args.server_interface], report, owned, time.monotonic() + 10)
        host = json.loads(raw)[0]
        entry, raw = run_command(report["ssh"]["argv"] + [owned["boot"] + "ip -j link show dev " + shlex.quote(args.client_interface)], report, owned, time.monotonic() + 10)
        target = json.loads(raw)[0]
        if "UP" not in host["flags"] or "UP" not in target["flags"]:
            raise ValueError("PPP lower interfaces must already be administratively up")
        owned["server_mac"], owned["client_mac"] = host["address"], target["address"]
        plugins, client_options = prepare_ppp_options(args, owned, report)
        for name in ("server", "client", "client-pppd"):
            owned["logs"][name] = (args.output / (name + ".log")).open("xb")
        remote = shlex.quote(owned["remote_directory"])
        setup = owned["boot"] + f"""mkdir -m 700 {remote} || exit 73
        printf 'CREATED\\n'
        mkfifo -m 600 {remote}/control
        """
        entry, raw = run_command(report["ssh"]["argv"] + [setup], report, owned, time.monotonic() + 10, check=False)
        owned["remote_setup"] = entry
        if entry.get("error") or entry.get("timed_out") or entry.get("exit_code") != 0:
            raise RuntimeError("Remote PPP control setup failed")
        report["server_argv"] = owned["server_prefix"] + [
            "pppoe-server",
            "-F",
            "-I",
            args.server_interface,
            "-C",
            args.ac_name,
            "-S",
            args.service,
            "-L",
            args.server_address,
            "-R",
            args.client_address,
            "-N",
            "1",
            "-O",
            str(args.output.resolve() / "server.options"),
            "-X",
            str(args.output.resolve() / "server.pid"),
            "-k",
            "-g",
            plugins[0],
            "-q",
            "/usr/sbin/pppd",
        ]
        owned["server"] = subprocess.Popen(report["server_argv"], stdin=subprocess.DEVNULL, stdout=owned["logs"]["server"], stderr=subprocess.STDOUT, start_new_session=True)
        report["remote_lifetime_seconds"] = int(args.ready_timeout + args.duration + 5) + 1
        client = ["pppd", *client_options, "plugin", plugins[1], "nic-" + args.client_interface, "rp_pppoe_service", args.service, "rp_pppoe_ac", args.ac_name, "ifname", owned["client_interface"]]
        child = f"""umask 077
        trap '' HUP INT TERM USR1
        exec 9<{remote} || exit 1
        flock -n 9 || exit 1
        test -p {remote}/control || exit 1
        exec 8<>{remote}/control || exit 1
        (
            trap - USR1
            IFS= read -r request <&8 || exit 1
            kill -TERM 0
            sleep 5
            kill -KILL 0
        ) &
        (
            trap - USR1
            sleep {report["remote_lifetime_seconds"]}
            kill -TERM 0
            sleep 3
            kill -KILL 0
        ) &
        (
            trap - HUP INT TERM USR1
            exec 8>&-
            exec {shlex.join(client)}
        ) &
        wait "$!"
        result=$?
        kill -USR1 0
        wait
        printf '%s\\n' "$result" > {remote}/exit
        exit "$result"
        """
        launcher = owned["boot"] + "setsid sh -c " + shlex.quote(child) + ' &\nwait "$!"'
        owned["client_ssh"] = subprocess.Popen(report["ssh"]["argv"] + [launcher], stdin=subprocess.DEVNULL, stdout=owned["logs"]["client"], stderr=subprocess.STDOUT, start_new_session=True)
        phase_end = time.monotonic() + args.ready_timeout
        while not STOP and time.monotonic() < phase_end:
            observation = read_ppp_session(args, owned, report, phase_end)
            if observation is None:
                break
            report["observations"].append(observation)
            if observation["state"] == "failed" or (report["preparation"] == "ready" and observation["state"] != "ready"):
                raise RuntimeError(observation.get("reason", "Established PPP session lost"))
            if STOP:
                break
            if observation["state"] == "ready" and report["preparation"] != "ready":
                report["preparation"] = "ready"
                print(json.dumps({"ready": True, "sid": observation["sid"], "client": observation["client"]["ifname"], "server": observation["server"]["ifname"], "evidence": str(args.output)}), flush=True)
                phase_end = time.monotonic() + args.duration
                continue
            delay = 0.5 if report["preparation"] == "ready" else 0.1
            time.sleep(min(delay, max(0, phase_end - time.monotonic())))
        if report["preparation"] != "ready":
            raise TimeoutError("PPP preparation ended without a usable session")
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
        report["first_error"] = str(error)
        if report["preparation"] != "ready":
            report["preparation"] = "failed"
    finally:
        report["execution"] = "failed" if report["first_error"] is not None else ("stopped" if STOP else "completed")
        stop_ppp(args, owned, report)
        for handle in owned["logs"].values():
            try:
                handle.close()
            except OSError as error:
                report["cleanup_errors"].append(str(error))
        if owned["wake"] is not None:
            signal.set_wakeup_fd(-1)
            os.close(owned["wake"][0])
            os.close(owned["wake"][1])
        report["cleanup"] = "incomplete" if report["cleanup_errors"] else "complete"
        try:
            (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        except OSError as error:
            report["recording_error"] = str(error)
            print(json.dumps(report, indent=2), file=sys.stderr)
    print(json.dumps({"preparation": report["preparation"], "execution": report["execution"], "error": report["first_error"], "cleanup": report["cleanup"], "evidence": str(args.output)}))
    return int(bool(report["first_error"] or report["cleanup_errors"] or report.get("recording_error")))


if __name__ == "__main__":
    sys.exit(main())
