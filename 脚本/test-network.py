#!/usr/bin/env python3
"""Prepare or restore addresses, routes and namespaces on two host test ports.

apply, for each given side (left, right), prepares the port:
  1. stop NetworkManager from managing it (if it was),
  2. optionally move it into a network namespace (created if absent) and
     bring it up there; otherwise bring it up if it was down and set
     arp_ignore=1, so it answers ARP only for its own addresses (U-Boot's
     TFTP ARP reaches both ports; with 0 both answer for 192.168.11.10),
then, once every port is prepared, adds each side's addresses and routes
(a route prefix may be 'default'; gateway '-' means on-link).
Every step is written to the --state file BEFORE it runs, with the commands
that undo it. If a step fails, the steps already done are undone in reverse.

restore undoes every completed step of the --state file in reverse order;
a failing undo is reported and the other steps are still undone.

Why namespaces: both ports are on this host; in one namespace the kernel
delivers traffic between its own addresses locally and nothing crosses the
router. A port alone in a namespace needs no arp_ignore. Moving a port in or
out clears its addresses and IPv4 settings and takes it down, so moving it
back also writes back its arp_ignore and, as the very last undo of that port,
raises it if it was up (NetworkManager takes a returning port as new and would
connect it as soon as it has carrier). NetworkManager gets the port back as it was; one it managed but had not connected (state 30)
comes back with runtime autoconnect off, otherwise it would connect at once
(DHCP address and default route from the router).

Refused: eno1 and enx224d686d5002 (uplink and BMC), the same port on both
sides, ports enslaved to a bridge/bond, ports carrying a default route.
Not done: MTU, VLAN, bridge, qdisc, anything on the router.

Example (step 5 of the 2026-10-16 device tests; needs root):
  sudo python3 test-network.py apply --state /tmp/net.json \\
    --left-interface eno2 --left-address 192.168.1.115/24 \\
    --left-route 198.18.2.0/24 192.168.1.1 \\
    --right-interface enxd4a2cde31c38 --right-netns qdxr \\
    --right-address 198.18.2.2/24 --right-route default 198.18.2.1
  sudo python3 test-network.py restore --state /tmp/net.json

Stdout: one JSON object (action, state, steps, error). Exit 0 = every step
(apply) or every undo (restore) succeeded; exit 1 otherwise; exit 2 usage.
Self-test (no root): python3 test-network.py --self-test
"""

import argparse
import dataclasses
import ipaddress
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab

PROTECTED_INTERFACES = ("eno1", "enx224d686d5002")  # uplink and BMC; never touched
# Self-test cases: a link state, a request, the commands apply runs and the commands restore runs (by hand).
USB = "enxd4a2cde31c38"
NS = ["ip", "-n", "qdx-right"]
SELF_TEST_PLANS = [
    ("managed eno2, host namespace, one address already present",
     dict(name="eno2", exists=True, up=True, addresses=["192.168.1.115/24"], nm_managed=True),
     ["192.168.11.10/24", "192.168.1.115/24"], [("198.18.2.0/24", "192.168.1.1")], None,
     [["nmcli", "device", "set", "eno2", "managed", "no"], ["sysctl", "-w", "net/ipv4/conf/eno2/arp_ignore=1"],
      ["ip", "address", "add", "192.168.11.10/24", "dev", "eno2"],
      ["ip", "route", "add", "198.18.2.0/24", "via", "192.168.1.1", "dev", "eno2"]],
     [["ip", "route", "del", "198.18.2.0/24", "via", "192.168.1.1", "dev", "eno2"],
      ["ip", "address", "del", "192.168.11.10/24", "dev", "eno2"], ["sysctl", "-w", "net/ipv4/conf/eno2/arp_ignore=0"],
      ["nmcli", "device", "set", "eno2", "managed", "yes"]]),
    ("unmanaged up USB port moved into a new namespace",
     dict(name=USB, exists=True, up=True, addresses=["198.18.2.2/24"], arp_ignore="2"),
     ["198.18.2.2/24"], [("192.168.1.0/24", "198.18.2.1")], "qdx-right",
     [["ip", "netns", "add", "qdx-right"], ["ip", "link", "set", USB, "netns", "qdx-right"], NS + ["link", "set", USB, "up"],
      NS + ["address", "add", "198.18.2.2/24", "dev", USB],
      NS + ["route", "add", "192.168.1.0/24", "via", "198.18.2.1", "dev", USB]],
     [NS + ["route", "del", "192.168.1.0/24", "via", "198.18.2.1", "dev", USB],
      NS + ["address", "del", "198.18.2.2/24", "dev", USB], NS + ["link", "set", USB, "netns", "1"],
      ["sysctl", "-w", f"net/ipv4/conf/{USB}/arp_ignore=2"], ["ip", "netns", "del", "qdx-right"],
      ["ip", "link", "set", USB, "up"]]),
    ("managed, disconnected, up USB port in a namespace is raised only after NetworkManager is set",
     dict(name=USB, exists=True, up=True, nm_managed=True, nm_disconnected=True), ["198.18.2.2/24"], [], "qdx-right",
     [["nmcli", "device", "set", USB, "managed", "no"], ["ip", "netns", "add", "qdx-right"],
      ["ip", "link", "set", USB, "netns", "qdx-right"], NS + ["link", "set", USB, "up"],
      NS + ["address", "add", "198.18.2.2/24", "dev", USB]],
     [NS + ["address", "del", "198.18.2.2/24", "dev", USB], NS + ["link", "set", USB, "netns", "1"],
      ["sysctl", "-w", f"net/ipv4/conf/{USB}/arp_ignore=0"], ["ip", "netns", "del", "qdx-right"],
      ["nmcli", "device", "set", USB, "autoconnect", "no", "managed", "yes"], ["ip", "link", "set", USB, "up"]]),
    ("managed but disconnected USB port comes back without autoconnect",
     dict(name=USB, exists=True, up=True, nm_managed=True, nm_disconnected=True), ["192.168.11.10/24"], [], None,
     [["nmcli", "device", "set", USB, "managed", "no"], ["sysctl", "-w", f"net/ipv4/conf/{USB}/arp_ignore=1"],
      ["ip", "address", "add", "192.168.11.10/24", "dev", USB]],
     [["ip", "address", "del", "192.168.11.10/24", "dev", USB], ["sysctl", "-w", f"net/ipv4/conf/{USB}/arp_ignore=0"],
      ["nmcli", "device", "set", USB, "autoconnect", "no", "managed", "yes"]]),
    ("down port, on-link route",
     dict(name="eno2", exists=True), ["10.0.0.1/24"], [("10.0.1.0/24", "-")], None,
     [["ip", "link", "set", "eno2", "up"], ["sysctl", "-w", "net/ipv4/conf/eno2/arp_ignore=1"],
      ["ip", "address", "add", "10.0.0.1/24", "dev", "eno2"], ["ip", "route", "add", "10.0.1.0/24", "dev", "eno2"]],
     [["ip", "route", "del", "10.0.1.0/24", "dev", "eno2"], ["ip", "address", "del", "10.0.0.1/24", "dev", "eno2"],
      ["sysctl", "-w", "net/ipv4/conf/eno2/arp_ignore=0"], ["ip", "link", "set", "eno2", "down"]]),
]
SELF_TEST_REFUSALS = [
    (dict(name="eno1", exists=True), "eno1 is a protected host interface"),
    (dict(name="enx224d686d5002", exists=True), "enx224d686d5002 is a protected host interface"),
    (dict(name="eno2", exists=True, has_default_route=True), "eno2 carries a default route"),
    (dict(name="eno2", exists=True, has_master=True), "eno2 is enslaved to another device"),
    (dict(name="eth9", exists=False), "interface eth9 not found"),
]


@dataclass
class LinkState:
    name: str
    exists: bool
    up: bool = False
    addresses: list = field(default_factory=list)
    has_master: bool = False
    has_default_route: bool = False
    nm_managed: bool = False
    nm_disconnected: bool = False  # managed but not connected (NM state 30) before apply
    netns_exists: bool = False
    arp_ignore: str = "0"  # net.ipv4.conf.<port>.arp_ignore in the host namespace before apply


@dataclass
class Step:
    description: str
    command: list
    undo: list  # commands run in order to undo this step; the first failure stops the rest
    done: bool = False
    undone: bool = False
    output: str = ""


def observe(interface, netns):
    """Read the port's current state in this namespace and whether the target namespace exists."""
    shown = lab.run(["ip", "-j", "address", "show", "dev", interface], 10)
    if shown.returncode != 0:
        return LinkState(interface, False)
    link = json.loads(shown.stdout)[0]
    routes = lab.run(["ip", "-j", "route", "show", "default", "dev", interface], 10)
    nm = ["", ""]
    if shutil.which("nmcli"):
        shown_nm = lab.run(["nmcli", "-g", "GENERAL.NM-MANAGED,GENERAL.STATE", "device", "show", interface], 10)
        nm = shown_nm.stdout.splitlines() + nm
    return LinkState(
        name=interface,
        exists=True,
        up="UP" in link.get("flags", []),
        addresses=[f"{a['local']}/{a['prefixlen']}" for a in link.get("addr_info", [])],
        has_master="master" in link,
        has_default_route=bool(json.loads(routes.stdout or "[]")),
        nm_managed=nm[0].strip() == "yes",
        nm_disconnected=nm[0].strip() == "yes" and nm[1].split(" ")[0] == "30",
        netns_exists=bool(netns) and netns in lab.run(["ip", "netns", "list"], 10).stdout.split(),
        arp_ignore=Path(f"/proc/sys/net/ipv4/conf/{interface}/arp_ignore").read_text().strip(),
    )


def refuse(state):
    """Return why this port must not be configured, or None."""
    if state.name in PROTECTED_INTERFACES:
        return f"{state.name} is a protected host interface"
    if not state.exists:
        return f"interface {state.name} not found"
    if state.has_master:
        return f"{state.name} is enslaved to another device"
    if state.has_default_route:
        return f"{state.name} carries a default route"
    return None


def plan_side(state, addresses, routes, netns):
    """Turn one side's request into port steps and address steps, each with its undo commands."""
    name = state.name
    ip = ["ip", "-n", netns] if netns else ["ip"]
    # Slash form of the sysctl name: a dot in a port name would otherwise read as a separator.
    put_back_arp = ["sysctl", "-w", f"net/ipv4/conf/{name}/arp_ignore={state.arp_ignore}"]
    prepare = []
    if state.nm_managed:
        keep_down = ["autoconnect", "no"] if state.nm_disconnected else []
        prepare.append(Step(f"release {name} from NetworkManager", ["nmcli", "device", "set", name, "managed", "no"],
                            [["nmcli", "device", "set", name] + keep_down + ["managed", "yes"]]))
    if netns:
        if not state.netns_exists:
            prepare.append(Step(f"create namespace {netns}", ["ip", "netns", "add", netns], [["ip", "netns", "del", netns]]))
        # Moving back clears the port's IPv4 settings (so arp_ignore is written back) and takes it down.
        prepare.append(Step(f"move {name} into {netns}", ["ip", "link", "set", name, "netns", netns],
                            [ip + ["link", "set", name, "netns", "1"], put_back_arp]))
        prepare.append(Step(f"bring {name} up in {netns}", ip + ["link", "set", name, "up"], []))
        if state.up:
            # Raise it last of all its undos: NetworkManager takes a returning port as new and would
            # auto-connect it the moment it has carrier, before being told autoconnect=no.
            prepare[0].undo.append(["ip", "link", "set", name, "up"])
    else:
        if not state.up:
            prepare.append(Step(f"bring {name} up", ["ip", "link", "set", name, "up"],
                                [["ip", "link", "set", name, "down"]]))
        prepare.append(Step(f"answer ARP on {name} only for its own addresses",
                            ["sysctl", "-w", f"net/ipv4/conf/{name}/arp_ignore=1"], [put_back_arp]))
    addressing = []
    present = [] if netns else state.addresses  # a move clears all addresses
    for address in addresses:
        if address not in present:
            addressing.append(Step(f"add {address} to {name}", ip + ["address", "add", address, "dev", name],
                                   [ip + ["address", "del", address, "dev", name]]))
    for prefix, gateway in routes:
        via = [] if gateway == "-" else ["via", gateway]
        addressing.append(Step(f"route {prefix} via {gateway} on {name}",
                               ip + ["route", "add", prefix] + via + ["dev", name],
                               [ip + ["route", "del", prefix] + via + ["dev", name]]))
    return prepare, addressing


def save(path, steps):
    """Write the steps to the state file through a replaced temporary file."""
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps({"steps": [dataclasses.asdict(s) for s in steps]}, indent=1) + "\n")
    temporary.replace(path)


def undo_all(path, steps):
    """Undo completed steps in reverse order; return the number of steps whose undo failed."""
    failures = 0
    for step in reversed(steps):
        if not step.done or step.undone:
            continue
        step.undone = True
        for argv in step.undo:
            answer = lab.run(argv, 30)
            step.output = (answer.stdout + answer.stderr).strip()
            if answer.returncode != 0:
                step.undone = False
                break
        failures += not step.undone
        save(path, steps)
    return failures


def apply(args):
    """Check both sides, then run the planned steps; roll back on the first failure."""
    result = {"action": "apply", "state": str(args.state), "steps": [], "error": None}
    port_steps, address_steps = [], []
    for side in ("left", "right"):
        interface, netns = getattr(args, side + "_interface"), getattr(args, side + "_netns")
        if not interface:
            continue
        state = observe(interface, netns)
        result["error"] = refuse(state)
        if result["error"]:
            return result
        addresses = [str(a) for a in getattr(args, side + "_address")]
        prepared, addressed = plan_side(state, addresses, getattr(args, side + "_route"), netns)
        port_steps += prepared
        address_steps += addressed
    done = []
    try:
        for step in port_steps + address_steps:  # no address appears before every port ignores foreign ARP
            done.append(step)
            save(args.state, done)
            answer = lab.run(step.command, 30)
            step.output = (answer.stdout + answer.stderr).strip()
            step.done = answer.returncode == 0
            save(args.state, done)
            if not step.done:
                result["error"] = f"step failed: {step.description}: {step.output}"
                break
    except KeyboardInterrupt:
        result["error"] = "interrupted"
    if result["error"]:
        failures = undo_all(args.state, done)
        if failures:
            result["error"] += f"; {failures} undo steps failed"
    result["steps"] = [dataclasses.asdict(s) for s in done]
    return result


def restore(args):
    """Undo every completed step recorded in the state file."""
    steps = [Step(**entry) for entry in json.loads(args.state.read_text())["steps"]]
    failures = undo_all(args.state, steps)
    return {"action": "restore", "state": str(args.state), "steps": [dataclasses.asdict(s) for s in steps],
            "error": f"{failures} undo steps failed" if failures else None}


def self_test():
    """Check planning, refusal and undo bookkeeping against built-in link states."""
    checks = []
    for name, state, addresses, routes, netns, commands, undo_order in SELF_TEST_PLANS:
        prepared, addressed = plan_side(LinkState(**state), addresses, routes, netns)
        steps = prepared + addressed
        checks.append((name, [s.command for s in steps] == commands
                       and [argv for s in reversed(steps) for argv in s.undo] == undo_order))
    for state, reason in SELF_TEST_REFUSALS:
        checks.append(("refuse: " + reason, refuse(LinkState(**state)) == reason))
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "state.json"
        steps = [Step("first", ["true"], [["true"]], done=True), Step("second", ["true"], [["false"], ["true"]], done=True),
                 Step("never ran", ["true"], [["true"]])]
        failures = undo_all(path, steps)
        saved = [entry["undone"] for entry in json.loads(path.read_text())["steps"]]
        checks.append(("undo runs completed steps only and records failures",
                       failures == 1 and saved == [True, False, False]))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, apply or restore, and print the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=("apply", "restore"))
    parser.add_argument("--state", type=Path, help="apply: new state file; restore: the file apply wrote")
    for side in ("left", "right"):
        parser.add_argument(f"--{side}-interface", help=f"{side} host port")
        parser.add_argument(f"--{side}-address", type=ipaddress.ip_interface, action="append", default=[],
                            help="IP/prefix to add (repeatable)")
        parser.add_argument(f"--{side}-route", nargs=2, action="append", default=[], metavar=("PREFIX", "GATEWAY"),
                            help="route to add (repeatable); PREFIX may be 'default', GATEWAY '-' means on-link")
        parser.add_argument(f"--{side}-netns", help="namespace to move the port into")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.action or not args.state:
        parser.error("action and --state are required")
    if args.action == "restore" and not args.state.is_file():
        parser.error("--state must be an existing state file")
    if args.action == "apply" and (args.state.exists() or not (args.left_interface or args.right_interface)
                                   or args.left_interface == args.right_interface):
        parser.error("apply needs a new --state file and one or two different interfaces")
    lab.stop_on_signals()
    result = apply(args) if args.action == "apply" else restore(args)
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
