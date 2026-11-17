#!/usr/bin/env python3
"""Run captures, services, router commands and workloads around one time window.

Fixed order, no schedule language:
  1. start every --capture (tcpdump, one pcap per port),
  2. start every --service (local background program, e.g. packet-probe sink),
  3. start every --router command (literal router shell command over ssh),
  4. wait until all of them are ready (see below); any failure stops here,
  5. start every --run (local workload, e.g. packet-probe send),
  6. at --duration stop runs that are still going (marked stopped_by_window),
  7. wait --tail seconds, stop services, give router commands up to --tail
     seconds to end by themselves, then stop the captures.

Ready means: tcpdump printed "listening on"; a service or router command
printed a line "ready" on stderr. A router command that should be waited for
starts with 'echo ready >&2;'. cpu-window prints "ready" itself. A timed router
action is timed on the router: 'echo ready >&2; sleep 5; ip link set lan3 down'.

NETNS arguments take '-' for the current namespace. COMMAND strings of
--service/--run are split with shlex and run without a shell. Each process
writes OUTPUT/<role>-<n>.out and .err; router stdout is the sampled data.
Stopping ssh does not guarantee the router command ended: such commands are
marked may_still_run_on_router and fail the window, so a router command must
end by itself within the runs' time plus 2 * --tail. tcpdump needs root.
--router needs --ssh TARGET SOURCE INTERFACE: ssh binds to SOURCE, and nothing
starts unless the route to TARGET from SOURCE leaves through INTERFACE.

Stdout: one JSON object (window_start_ns, duration, processes, output_dir,
error). Exit 0 = all ready; every run ended by itself with status 0 inside the
window and every router command by the end of the tail; no service ended
early; every capture reported its counters with zero kernel drops. Exit 1
otherwise (a failed run or router command is named with its status and the
end of its stderr); exit 2 usage error.

Example (P-03 style; namespaces from test-network.py):
  sudo python3 window.py --output /tmp/w1 --duration 10 \\
    --capture - eno2 --capture qdx-right enxd4a2cde31c38 \\
    --service qdx-right 'python3 packet-probe.py sink --bind 198.18.2.2 --port 5001 --duration 13' \\
    --router '/tmp/cpu-window 100 105' --ssh root@192.168.1.1 192.168.1.115 eno2 \\
    --run - 'python3 packet-probe.py send --source 192.168.1.115 --destination 198.18.2.2 --port 5001 --count 800000 --length 1400 --rate 80000'
Self-test (no root, no device): python3 window.py --self-test
"""

import argparse
import dataclasses
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab


@dataclass
class Process:
    role: str
    argv: list
    stdout: str
    stderr: str
    ready_text: str | None
    ready_s: float | None = None
    returncode: int | None = None
    stopped_by_window: bool = False
    may_still_run_on_router: bool = False
    packets_captured: int | None = None
    packets_dropped: int | None = None


def start(role, index, argv, output, ready_text):
    """Start one process with stdout and stderr in files; return its record and Popen."""
    record = Process(role, argv, str(output / f"{role}-{index}.out"), str(output / f"{role}-{index}.err"), ready_text)
    with open(record.stdout, "xb") as out, open(record.stderr, "xb") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    return record, proc


def is_ready(record):
    """Tell whether the process has printed its readiness text on stderr."""
    lines = Path(record.stderr).read_text(errors="replace").splitlines()
    if record.ready_text == "ready":
        return any(line.strip() == "ready" for line in lines)
    return any(record.ready_text in line for line in lines)


def wait_ready(started, timeout, opened):
    """Wait until every started process is ready; return an error text or None."""
    until = time.monotonic() + timeout
    waiting = list(started)
    while waiting and time.monotonic() < until:
        for record, proc in list(waiting):
            if is_ready(record):
                record.ready_s = round(time.monotonic() - opened, 3)
                waiting.remove((record, proc))
            elif proc.poll() is not None:
                return f"{record.role} ended before it was ready: {shlex.join(record.argv)}"
        time.sleep(0.05)
    if waiting:
        return "not ready in time: " + ", ".join(shlex.join(r.argv) for r, _ in waiting)
    return None


def capture_counters(record):
    """Read tcpdump's final packet counters from its stderr file."""
    text = Path(record.stderr).read_text(errors="replace")
    captured = re.search(r"(\d+) packets? captured", text)
    dropped = re.search(r"(\d+) packets? dropped by kernel", text)
    record.packets_captured = int(captured[1]) if captured else None
    record.packets_dropped = int(dropped[1]) if dropped else None


def judge(records, setup_error):
    """Return the first reason the window is not complete, or None."""
    if setup_error:
        return setup_error
    for record in records:
        command = shlex.join(record.argv)
        if record.role in ("run", "router") and (record.stopped_by_window or record.returncode != 0):
            tail = Path(record.stderr).read_text(errors="replace")[-200:].strip()
            return f"{record.role} did not end by itself with status 0 (status {record.returncode}): {command}; stderr ends: {tail!r}"
        if record.role == "service" and not record.stopped_by_window:
            # A service is stopped by the window; ending on its own means it died early.
            return f"service ended before the window closed: {command}"
        if record.role == "capture" and (record.packets_captured is None or record.packets_dropped != 0):
            return f"capture counters missing or kernel drops: {command}"
    return None


def run_window(args):
    """Execute the window in fixed order and return the result dict."""
    ssh, refusal = lab.ssh_argv(*args.ssh, args.ssh_netns) if args.router else (None, None)
    if refusal:  # nothing has started yet, so nothing needs stopping
        return {"window_start_ns": None, "duration": args.duration, "processes": [], "output_dir": str(args.output),
                "error": refusal}
    args.output.mkdir(mode=0o700, parents=True)
    opened = time.monotonic()
    started, error, window_start_ns = [], None, None
    try:
        for index, (netns, interface) in enumerate(args.capture):
            pcap = args.output / f"capture-{index}-{interface}.pcap"
            argv = lab.in_netns(["tcpdump", "-n", "-U", "-i", interface, "-w", str(pcap)] + shlex.split(args.capture_filter), netns)
            started.append(start("capture", index, argv, args.output, "listening on"))
        for index, (netns, command) in enumerate(args.service):
            started.append(start("service", index, lab.in_netns(shlex.split(command), netns), args.output, "ready"))
        for index, command in enumerate(args.router):
            started.append(start("router", index, ssh + [command], args.output, "ready"))
        error = wait_ready([s for s in started if s[0].ready_text], args.ready_timeout, opened)
        if error is None:
            window_start_ns = time.time_ns()
            window_end = time.monotonic() + args.duration
            runs = [start("run", i, lab.in_netns(shlex.split(c), n), args.output, None) for i, (n, c) in enumerate(args.run)]
            started += runs
            while time.monotonic() < window_end and any(proc.poll() is None for _, proc in runs):
                time.sleep(0.05)
            for record, proc in runs:
                if proc.poll() is None:
                    record.stopped_by_window = True
                    lab.stop_process(proc, signal.SIGINT, 5)
            time.sleep(args.tail)
    except KeyboardInterrupt:
        error = "interrupted"
    except OSError as error_text:
        # Boundary: a program that cannot be started is a reported failure.
        error = str(error_text)
    finally:
        for role, sig in (("run", signal.SIGINT), ("service", signal.SIGINT), ("router", signal.SIGTERM), ("capture", signal.SIGINT)):
            if role == "router":
                until = time.monotonic() + args.tail
                while time.monotonic() < until and any(p.poll() is None for r, p in started if r.role == "router"):
                    time.sleep(0.05)
            for record, proc in started:
                if record.role == role and proc.poll() is None:
                    record.stopped_by_window = True
                    record.may_still_run_on_router = role == "router"
                    lab.stop_process(proc, sig, 5)
                if record.role == role:
                    record.returncode = proc.returncode
    for record, _ in started:
        if record.role == "capture":
            capture_counters(record)
    records = [record for record, _ in started]
    return {"window_start_ns": window_start_ns, "duration": args.duration,
            "processes": [dataclasses.asdict(r) for r in records], "output_dir": str(args.output),
            "error": judge(records, error)}


def build_parser():
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture", nargs=2, action="append", default=[], metavar=("NETNS", "IFACE"), help="port to capture")
    parser.add_argument("--capture-filter", default="not tcp port 22", help="tcpdump filter for all captures")
    parser.add_argument("--service", nargs=2, action="append", default=[], metavar=("NETNS", "COMMAND"),
                        help="local background program that prints 'ready' on stderr")
    parser.add_argument("--router", action="append", default=[], metavar="COMMAND", help="router shell command over ssh")
    parser.add_argument("--run", nargs=2, action="append", default=[], metavar=("NETNS", "COMMAND"), help="local workload")
    parser.add_argument("--duration", type=float, help="window length in seconds")
    parser.add_argument("--tail", type=float, default=1.0, help="seconds after the window before stopping (default 1)")
    parser.add_argument("--ready-timeout", type=float, default=10.0, help="seconds to wait for readiness (default 10)")
    parser.add_argument("--ssh", nargs=3, metavar=("TARGET", "SOURCE", "INTERFACE"),
                        help="router ssh target, bound source address and the port its route must use")
    parser.add_argument("--ssh-netns", help="host namespace for ssh")
    parser.add_argument("--output", type=Path, help="new directory for all files")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    return parser


def self_test():
    """Run small windows with local Python processes and check lab's process helpers."""
    python = shlex.quote(sys.executable)
    service = f"{python} -c \"import sys,time; print('ready', file=sys.stderr, flush=True); time.sleep(30)\""
    silent = f"{python} -c 'import time; time.sleep(30)'"
    quick = f"{python} -c 'print(1)'"
    checks = []
    with tempfile.TemporaryDirectory(prefix="qdx-window-") as scratch:
        base = ["--duration", "1", "--tail", "0.2", "--ready-timeout", "2", "--service", "-", service]
        good = run_window(build_parser().parse_args(base + ["--run", "-", quick, "--output", scratch + "/good"]))
        checks.append(("complete window", good["error"] is None and good["processes"][0]["ready_s"] is not None
                       and good["processes"][0]["stopped_by_window"]))
        slow = run_window(build_parser().parse_args(base + ["--run", "-", quick, "--run", "-", silent, "--output", scratch + "/slow"]))
        checks.append(("overlong run is stopped and fails the window", slow["processes"][2]["stopped_by_window"]
                       and slow["error"].startswith("run did not end")))
        unready = run_window(build_parser().parse_args(["--duration", "1", "--tail", "0", "--ready-timeout", "0.5",
                                                        "--service", "-", silent, "--output", scratch + "/unready"]))
        checks.append(("silent service fails readiness", unready["error"].startswith("not ready in time")
                       and unready["window_start_ns"] is None))
        wrong_port = run_window(build_parser().parse_args(base + ["--router", "true", "--ssh", "root@127.0.0.1", "127.0.0.1",
                                                                  "eno2", "--output", scratch + "/wrong-port"]))
        checks.append(("router route through another port starts nothing", wrong_port["processes"] == []
                       and "through lo, not eno2" in wrong_port["error"] and not Path(scratch, "wrong-port").exists()))
        Path(scratch, "router-0.err").write_text("ready\ncpu-window: cannot read /proc/stat\n")
        failed = Process("router", ["ssh", "router"], scratch + "/router-0.out", scratch + "/router-0.err", "ready", returncode=1)
        reason = judge([failed], None) or ""
        checks.append(("failed router command fails the window", "(status 1)" in reason and "/proc/stat" in reason))
    checks.append(("ssh is bound to the source on the checked port", lab.ssh_argv("root@127.0.0.1", "127.0.0.1", "lo")[0][:3]
                   == ["ssh", "-b", "127.0.0.1"]))
    slow_command = lab.run([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(5)"], timeout=0.5)
    checks.append(("lab.run marks timeout", slow_command.timed_out and slow_command.returncode is None))
    stubborn = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)"])
    time.sleep(0.3)
    checks.append(("lab.stop_process escalates to SIGKILL", lab.stop_process(stubborn, signal.SIGINT, 0.5) == -signal.SIGKILL))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, run the window and print the JSON result."""
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.duration is None or args.duration <= 0 or args.tail < 0 or not args.output:
        parser.error("--duration (positive) and --output are required; --tail must be >= 0")
    if args.output.exists() or (args.router and not args.ssh):
        parser.error("--output must not exist; --router needs --ssh TARGET SOURCE INTERFACE")
    lab.stop_on_signals()
    result = run_window(args)
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
