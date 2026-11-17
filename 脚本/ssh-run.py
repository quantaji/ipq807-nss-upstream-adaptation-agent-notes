#!/usr/bin/env python3
"""Run one router shell command over ssh, only after lab.ssh_argv's route check.

ssh binds to SOURCE and runs only while the route to TARGET from SOURCE leaves
through INTERFACE. --stdin FILE feeds the command (otherwise /dev/null), e.g.
  ssh-run.py --ssh root@192.168.1.1 192.168.1.115 eno2 \\
      --command 'cat > /tmp/cpu-window && chmod 0755 /tmp/cpu-window' --stdin cpu-window
On timeout ssh is killed (exit_code null); the router command may still run.
Stdout: one JSON object (ran, refusal, exit_code, stdout_tail, stderr_tail,
seconds). Exit 0 = ran and exited 0; exit 1 otherwise; exit 2 usage error.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab


def ssh_run(ssh, command, stdin_path, timeout):
    """Run command over the route-checked ssh with stdin from stdin_path (or /dev/null); return the result dict."""
    prefix, refusal = lab.ssh_argv(*ssh)
    if refusal:
        return {"ran": False, "refusal": refusal, "exit_code": None, "stdout_tail": "", "stderr_tail": "", "seconds": 0.0}
    with open(stdin_path or os.devnull, "rb") as feed:
        answer = lab.run(prefix + [command], timeout, feed)
    return {"ran": True, "refusal": None, "exit_code": answer.returncode, "stdout_tail": answer.stdout[-2000:],
            "stderr_tail": answer.stderr[-2000:], "seconds": round(answer.seconds, 3)}


def self_test():
    """Check the refusal before any ssh and the usage checks."""
    wrong_port = ssh_run(["root@127.0.0.1", "127.0.0.1", "eno2"], "true", None, 5)
    usage = [subprocess.run([sys.executable, "-B", __file__] + extra, capture_output=True).returncode
             for extra in (["--command", "true"], ["--ssh", "a", "b", "c", "--command", "true", "--stdin", "/nonexistent"])]
    checks = [("route through another port is refused before ssh",
               not wrong_port["ran"] and "through lo, not eno2" in wrong_port["refusal"]),
              ("missing --ssh and missing --stdin file are usage errors", usage == [2, 2])]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, run the command and print the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ssh", nargs=3, metavar=("TARGET", "SOURCE", "INTERFACE"),
                        help="router ssh target, bound source address and the port its route must use")
    parser.add_argument("--command", help="literal router shell command")
    parser.add_argument("--stdin", help="file fed to the command's stdin")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds before ssh is killed (default 60)")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not (args.ssh and args.command) or args.timeout <= 0 or (args.stdin and not os.path.isfile(args.stdin)):
        parser.error("--ssh TARGET SOURCE INTERFACE and --command are required, --timeout positive, --stdin an existing file")
    result = ssh_run(args.ssh, args.command, args.stdin, args.timeout)
    return lab.print_result(result, result["exit_code"] == 0)


if __name__ == "__main__":
    sys.exit(main())
