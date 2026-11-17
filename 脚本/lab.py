"""Shared host-side helpers for the QDX experiment scripts.

Only five kinds of code live here: running commands, building ssh command
lines, serial console I/O, printing the JSON result, and signal/process
cleanup. A helper belongs here only when at least two scripts use it.
"""

import dataclasses
import fcntl
import json
import os
import re
import secrets
import select
import shlex
import signal
import subprocess
import termios
import time
import tty
from dataclasses import dataclass
from typing import BinaryIO

# Console text that means the router is booting, not sitting at a shell.
BOOT_ACTIVITY = re.compile(rb"U-Boot|Hit any key|Starting kernel|Please press Enter|IPQ807x#")

# Default prompt of an OpenWrt root shell, e.g. "root@OpenWrt:~# ".
LINUX_PROMPT = r"root@[^\n]*# ?$"


@dataclass
class CommandResult:
    argv: list
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float


def run(argv, timeout, stdin=None):
    """Run argv to completion, stdin from an open file if given; on timeout kill it and mark timed_out."""
    started = time.monotonic()
    try:
        done = subprocess.run(argv, stdin=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        # subprocess.run has already killed and reaped the child; keep any partial output.
        stdout = expired.stdout.decode(errors="replace") if expired.stdout else ""
        stderr = expired.stderr.decode(errors="replace") if expired.stderr else ""
        return CommandResult(list(argv), None, stdout, stderr, True, time.monotonic() - started)
    return CommandResult(list(argv), done.returncode, done.stdout, done.stderr, False, time.monotonic() - started)


def in_netns(argv, netns):
    """Prefix argv with 'ip netns exec NS' when a namespace is given."""
    if not netns or netns == "-":
        return list(argv)
    return ["ip", "netns", "exec", netns] + list(argv)


def ssh_argv(target, source, interface, netns=None):
    """Return (ssh prefix bound to source, None), or (None, why) when the route would not leave through interface."""
    # Without the test address on eno2 the default route reaches another 192.168.1.1 upstream that accepts root.
    host = target.rsplit("@", 1)[-1]
    route = run(in_netns(["ip", "-j", "route", "get", host, "from", source], netns), 10)
    if route.returncode != 0:
        return None, f"no route to {host} from {source}: {route.stderr.strip()}; ssh not run"
    leaves = json.loads(route.stdout)[0].get("dev")
    if leaves != interface:
        return None, f"route to {host} from {source} leaves through {leaves}, not {interface}; ssh not run"
    argv = ["ssh", "-b", source, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", target]
    return in_netns(argv, netns), None


@dataclass
class SerialPort:
    fd: int
    original: list
    log: BinaryIO | None


@dataclass
class SerialCommandResult:
    command: str
    completed: bool
    exit_code: int | None
    output: str
    may_still_run: bool
    seconds: float


def open_serial(device, baud, log_path=None):
    """Open the serial device exclusively in raw 8N1 mode."""
    speed = getattr(termios, "B" + str(baud))
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        original = termios.tcgetattr(fd)
        settings = termios.tcgetattr(fd)
        tty.cfmakeraw(settings)
        settings[2] = (settings[2] & ~termios.HUPCL) | termios.CLOCAL | termios.CREAD
        settings[4] = settings[5] = speed
        settings[6][termios.VMIN] = 0
        settings[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        log = open(log_path, "xb") if log_path else None
    except BaseException:
        # Boundary: an open file descriptor must not leak when setup fails.
        os.close(fd)
        raise
    return SerialPort(fd, original, log)


def close_serial(port):
    """Restore terminal settings, release exclusivity and close the port."""
    errors = []
    steps = (
        (termios.tcsetattr, (port.fd, termios.TCSANOW, port.original)),
        (fcntl.ioctl, (port.fd, termios.TIOCNXCL)),
        (os.close, (port.fd,)),
    )
    for action, arguments in steps:
        # Boundary: each release is attempted even if an earlier one fails.
        try:
            action(*arguments)
        except (OSError, termios.error) as error:
            errors.append(f"{action.__name__}: {error}")
    if port.log:
        port.log.close()
    return errors


def read_serial(port, wait):
    """Wait up to `wait` seconds for console bytes; log them and return them without CR."""
    readable, _, _ = select.select([port.fd], [], [], max(0.0, wait))
    if not readable:
        return b""
    try:
        data = os.read(port.fd, 65536)
    except BlockingIOError:
        return b""
    if not data:
        raise OSError("serial device returned end of file")
    if port.log:
        port.log.write(data)
        port.log.flush()
    return data.replace(b"\r", b"")


def write_serial(port, data, deadline):
    """Write all of data before the monotonic deadline; never resend."""
    view = memoryview(data)
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("serial write did not finish; nothing is resent")
        _, writable, _ = select.select([], [port.fd], [], remaining)
        if writable:
            try:
                view = view[os.write(port.fd, view):]
            except BlockingIOError:
                pass


def console_is_booting(port, quiet):
    """Read the console for `quiet` seconds and report whether boot text appeared."""
    seen = bytearray()
    until = time.monotonic() + quiet
    while time.monotonic() < until:
        seen += read_serial(port, until - time.monotonic())
    return bool(BOOT_ACTIVITY.search(seen))

def serial_command(port, command, prompt, timeout):
    """Run one shell command on the console between random markers and wait for the next prompt."""
    started = time.monotonic()
    deadline = started + timeout
    token = secrets.token_hex(8)
    left, right = token[:8], token[8:]
    # The echoed command line shows "left right" apart; only executed printf joins them.
    wire = (
        f"printf '\\n%s%s:B\\n' {left} {right}; eval {shlex.quote(command)}; "
        f"printf '\\n%s%s:E:%s\\n' {left} {right} \"$?\"\r"
    )
    write_serial(port, wire.encode(), deadline)
    begin = (token + ":B\n").encode()
    end = re.compile(rb"\n" + token.encode() + rb":E:([0-9]+)\n")
    seen = bytearray()
    while time.monotonic() < deadline:
        seen += read_serial(port, deadline - time.monotonic())
        finished = end.search(seen)
        if not finished:
            continue
        start = seen.rfind(begin, 0, finished.start())
        if start >= 0 and prompt.search(seen[finished.end():]):
            output = seen[start + len(begin):finished.start()].decode(errors="replace")
            return SerialCommandResult(command, True, int(finished[1]), output, False, time.monotonic() - started)
    return SerialCommandResult(command, False, None, seen.decode(errors="replace"), True, time.monotonic() - started)


def _raise_interrupt(signum, frame):
    """Turn SIGTERM into KeyboardInterrupt so cleanup in finally runs."""
    raise KeyboardInterrupt


def stop_on_signals():
    """Make SIGINT and SIGTERM both raise KeyboardInterrupt."""
    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGINT, signal.default_int_handler)


def stop_process(proc, sig, grace):
    """Send sig, wait up to grace seconds, then SIGKILL; return the exit code."""
    if proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return proc.returncode


def print_result(result, ok):
    """Print the result as one JSON line on stdout and return the exit code."""
    if dataclasses.is_dataclass(result):
        result = dataclasses.asdict(result)
    print(json.dumps(result, default=str), flush=True)
    return 0 if ok else 1
