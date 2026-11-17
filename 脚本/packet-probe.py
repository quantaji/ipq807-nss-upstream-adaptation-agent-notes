#!/usr/bin/env python3
"""Send identifiable probe packets, or sink them at the receiving end.

Every probe carries the same payload: struct "!4sIIH" = b"QDXP", run (u32),
seq (u32, from 0), length (u16, whole probe payload including this 14-byte
header); payload byte i after the header is (seq + i) & 0xff.
compare-captures.py finds probes in captures by this header (same constant).

send --kind:
  udp   UDP datagram; a multicast --destination (224.0.0.0/4) also sets
        IP_MULTICAST_IF to --source, IP_MULTICAST_TTL to --ttl (default 8)
        and disables local loopback.
  tcp   one TCP connection; each of --count writes is one probe of --length
        bytes. Segment boundaries on the wire are chosen by the kernel.
  gre   IPv4 protocol 47: GRE (optional --gre-key) + inner IPv4/UDP + probe
  esp   IPv4 protocol 50: SPI (--spi) + sequence (seq+1) + probe, unencrypted
  ipip  IPv4 protocol 4:  inner IPv4/UDP + probe
  6in4  IPv4 protocol 41: inner IPv6/UDP + probe (needs IPv6 --inner-* addresses)
The last four use a raw socket (root): the kernel writes the outer IPv4
header from --source/--destination. Inner addresses default to the outer ones.
--length is limited so the outer IPv4 packet fits 1500 bytes (no fragments).

Pacing: packet n is due at start + n/--rate; early sends sleep until due,
late sends go at once (none skipped). --rate 0 sends as fast as possible.
Accepted sends are socket observations, not proof of delivery.

sink --kind udp|tcp: bind --bind:--port (with --join GROUP: bind the group and
join it on the interface owning --bind), write "ready" to stderr, and receive
for --duration seconds. UDP counts probe identities; TCP counts bytes.

Run each end in its own namespace when needed: ip netns exec NS python3 ...
Stdout: one JSON object. send exit 0 = every probe accepted by the socket;
sink exit 0 = ran its duration (zero packets is still 0; loss is judged by
compare-captures). Exit 1 = failure; exit 2 = usage error.

Example (receiver first):
  ip netns exec qdx-right python3 packet-probe.py sink --kind udp \\
      --bind 198.18.2.2 --port 5001 --duration 12
  python3 packet-probe.py send --kind udp --source 192.168.1.115 \\
      --destination 198.18.2.2 --port 5001 --count 10000 --length 1000 --rate 1000
Self-test (no root, uses 127.0.0.1): python3 packet-probe.py --self-test
"""

import argparse
import errno
import ipaddress
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab

PROBE_HEADER = struct.Struct("!4sIIH")  # same layout as compare-captures.py
PROBE_MAGIC = b"QDXP"
PATTERN = bytes(range(256)) * 257  # body for seq is PATTERN[seq % 256:], long enough for 65000 bytes
RAW_PROTOCOLS = {"gre": 47, "esp": 50, "ipip": 4, "6in4": 41}
# Bytes the outer IPv4 packet adds around the probe payload, per kind.
OVERHEAD = {"udp": 28, "gre": 20 + 4 + 20 + 8, "esp": 20 + 8, "ipip": 20 + 20 + 8, "6in4": 20 + 40 + 8}


def probe_payload(run_id, seq, length):
    """Build one probe payload of exactly length bytes."""
    start = seq & 0xFF
    body = PATTERN[start:start + length - PROBE_HEADER.size]
    return PROBE_HEADER.pack(PROBE_MAGIC, run_id, seq, length) + body


def parse_probe(data):
    """Return (run, seq, length) when data starts with a probe header, else None."""
    if len(data) < PROBE_HEADER.size or not data.startswith(PROBE_MAGIC):
        return None
    _, run_id, seq, length = PROBE_HEADER.unpack_from(data)
    return run_id, seq, length


def checksum(data):
    """Internet checksum (RFC 1071) of data."""
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ipv4_header(source, destination, protocol, payload_length, ttl=64):
    """Build an IPv4 header with a valid checksum."""
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + payload_length, 0, 0x4000, ttl, protocol, 0,
                         socket.inet_aton(source), socket.inet_aton(destination))
    return header[:10] + struct.pack("!H", checksum(header)) + header[12:]


def udp_datagram(source, destination, source_port, destination_port, payload):
    """Build a UDP header plus payload with the checksum for IPv4 or IPv6 addresses."""
    length = 8 + len(payload)
    family = socket.AF_INET6 if ":" in source else socket.AF_INET
    pseudo = socket.inet_pton(family, source) + socket.inet_pton(family, destination)
    pseudo += struct.pack("!IxxxB", length, 17) if family == socket.AF_INET6 else struct.pack("!xBH", 17, length)
    header = struct.pack("!HHHH", source_port, destination_port, length, 0)
    value = checksum(pseudo + header + payload) or 0xFFFF
    return struct.pack("!HHHH", source_port, destination_port, length, value) + payload


def inner_bytes(args, payload):
    """Build what a raw socket of this kind carries after the kernel's outer IPv4 header."""
    source = args.inner_source or args.source
    destination = args.inner_destination or args.destination
    if args.kind == "esp":
        seq = struct.unpack_from("!I", payload, 8)[0]
        return struct.pack("!II", args.spi, seq + 1) + payload
    udp = udp_datagram(source, destination, args.source_port or 40000, args.port, payload)
    if args.kind == "6in4":
        return struct.pack("!IHBB16s16s", 6 << 28, len(udp), 17, 64, socket.inet_pton(socket.AF_INET6, source),
                           socket.inet_pton(socket.AF_INET6, destination)) + udp
    inner = ipv4_header(source, destination, 17, len(udp)) + udp
    if args.kind == "ipip":
        return inner
    if args.gre_key is None:
        return struct.pack("!HH", 0, 0x0800) + inner
    return struct.pack("!HHI", 0x2000, 0x0800, args.gre_key) + inner


def open_sender(args):
    """Create and connect or bind the sending socket for this kind."""
    if args.kind == "tcp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((args.source, args.source_port or 0))
        sock.settimeout(5)
        sock.connect((args.destination, args.port))
        return sock
    if args.kind == "udp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((args.source, args.source_port or 0))
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, RAW_PROTOCOLS[args.kind])
        sock.bind((args.source, 0))
    if ipaddress.IPv4Address(args.destination).is_multicast:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(args.source))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.ttl or 8)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    elif args.ttl:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, args.ttl)
    return sock


def send(args):
    """Send --count probes at --rate and return the result dict."""
    result = {"kind": args.kind, "run_id": args.run_id, "requested": args.count, "sent": 0, "send_errors": {},
              "bytes": 0, "first_send_ns": None, "last_send_ns": None, "achieved_pps": None,
              "max_lateness_us": 0, "error": None}
    errors = Counter()
    sock = None
    try:
        sock = open_sender(args)
        start = time.monotonic_ns()
        for seq in range(args.count):
            if args.rate:
                due = start + seq * 1_000_000_000 // args.rate
                ahead = due - time.monotonic_ns()
                if ahead > 0:
                    time.sleep(ahead / 1e9)
                else:
                    result["max_lateness_us"] = max(result["max_lateness_us"], -ahead // 1000)
            payload = probe_payload(args.run_id, seq, args.length)
            try:
                if args.kind == "tcp":
                    sock.sendall(payload)
                elif args.kind == "udp":
                    sock.sendto(payload, (args.destination, args.port))
                else:
                    sock.sendto(inner_bytes(args, payload), (args.destination, 0))
            except OSError as error:
                # Boundary: a refused send (e.g. ENOBUFS) is counted; the schedule goes on.
                errors[errno.errorcode.get(error.errno, str(error.errno))] += 1
                if args.kind == "tcp":
                    raise
                continue
            now = time.time_ns()
            result["first_send_ns"] = result["first_send_ns"] or now
            result["last_send_ns"] = now
            result["sent"] += 1
            result["bytes"] += len(payload)
    except OSError as error:
        result["error"] = str(error)
    except KeyboardInterrupt:
        result["error"] = "interrupted"
    finally:
        if sock is not None:
            sock.close()
    result["send_errors"] = dict(errors)
    span = (result["last_send_ns"] or 0) - (result["first_send_ns"] or 0)
    if result["sent"] > 1 and span > 0:
        result["achieved_pps"] = round((result["sent"] - 1) * 1e9 / span, 1)
    if not result["error"] and result["sent"] != result["requested"]:
        result["error"] = f"{result['requested'] - result['sent']} probes not accepted by the socket"
    return result


def sink(args):
    """Receive for --duration seconds and return the result dict."""
    result = {"kind": args.kind, "packets": 0, "bytes": 0, "connections": 0, "identities": 0,
              "duplicates": 0, "foreign": 0, "error": None}
    seen = set()
    sockets = []
    try:
        if args.kind == "udp":
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            listener.bind((args.join or args.bind, args.port))
            if args.join:
                membership = socket.inet_aton(args.join) + socket.inet_aton(args.bind)
                listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        else:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.bind, args.port))
            listener.listen(8)
        sockets.append(listener)
        print("ready", file=sys.stderr, flush=True)
        until = time.monotonic() + args.duration
        while time.monotonic() < until:
            readable, _, _ = select.select(sockets, [], [], until - time.monotonic())
            for sock in readable:
                if sock is listener and args.kind == "tcp":
                    connection, _ = listener.accept()
                    sockets.append(connection)
                    result["connections"] += 1
                    continue
                data = sock.recv(65536)
                if args.kind == "tcp" and not data:
                    sockets.remove(sock)
                    sock.close()
                    continue
                result["bytes"] += len(data)
                if args.kind == "tcp":
                    continue
                result["packets"] += 1
                probe = parse_probe(data)
                if probe is None or (args.run_id is not None and probe[0] != args.run_id):
                    result["foreign"] += 1
                elif probe[:2] in seen:
                    result["duplicates"] += 1
                else:
                    seen.add(probe[:2])
    except OSError as error:
        result["error"] = str(error)
    except KeyboardInterrupt:
        result["error"] = "interrupted"
    finally:
        for sock in sockets:
            sock.close()
    result["identities"] = len(seen)
    return result


def build_parser():
    """Build the command-line parser for send, sink and the self-test."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", choices=("send", "sink"))
    parser.add_argument("--kind", choices=("udp", "tcp", "gre", "esp", "ipip", "6in4"), default="udp")
    parser.add_argument("--source", help="send: local IPv4 address to send from")
    parser.add_argument("--destination", help="send: IPv4 unicast or multicast destination")
    parser.add_argument("--port", type=int, default=9, help="UDP/TCP port; inner UDP port for encapsulations")
    parser.add_argument("--source-port", type=int, help="send: local or inner source port")
    parser.add_argument("--count", type=int, default=1, help="send: number of probes")
    parser.add_argument("--length", type=int, default=64, help="send: probe payload bytes, header included")
    parser.add_argument("--rate", type=int, default=0, help="send: probes per second, 0 = as fast as possible")
    parser.add_argument("--run-id", type=int, help="run number written into probes (send) or filtered (sink)")
    parser.add_argument("--ttl", type=int, help="send: IPv4 TTL (multicast default 8)")
    parser.add_argument("--spi", type=int, default=1, help="send esp: SPI value")
    parser.add_argument("--gre-key", type=int, help="send gre: add a GRE key with this value")
    parser.add_argument("--inner-source", help="send encapsulations: inner source address")
    parser.add_argument("--inner-destination", help="send encapsulations: inner destination address")
    parser.add_argument("--bind", help="sink: local IPv4 address (interface address when joining)")
    parser.add_argument("--join", help="sink udp: multicast group to bind and join")
    parser.add_argument("--duration", type=float, default=10.0, help="sink: seconds to receive")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    return parser


def check_send_arguments(parser, args):
    """Reject send arguments that cannot form the requested packets."""
    if not (args.source and args.destination):
        parser.error("send needs --source and --destination")
    if args.run_id is None:
        args.run_id = 0
    if not 0 <= args.run_id < 2**32 or args.count < 1 or args.rate < 0:
        parser.error("--run-id must fit u32, --count >= 1, --rate >= 0")
    limit = 65000 if args.kind == "tcp" else 1500 - OVERHEAD[args.kind] - (4 if args.gre_key is not None else 0)
    if not PROBE_HEADER.size <= args.length <= limit:
        parser.error(f"--length must be {PROBE_HEADER.size}..{limit} for kind {args.kind}")
    if args.kind == "6in4" and ":" not in (args.inner_source or "") + (args.inner_destination or ""):
        parser.error("6in4 needs IPv6 --inner-source and --inner-destination")


def free_port():
    """Return a currently unused local TCP/UDP port number."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def loopback_pair(kind, count, length):
    """Run a sink subprocess and a send on 127.0.0.1; return (send result, sink result)."""
    port = free_port()
    sink_argv = [sys.executable, "-B", os.path.abspath(__file__), "sink", "--kind", kind, "--bind", "127.0.0.1",
                 "--port", str(port), "--duration", "3", "--run-id", "7"]
    receiver = subprocess.Popen(sink_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    receiver.stderr.readline()  # "ready"
    args = build_parser().parse_args(["send", "--kind", kind, "--source", "127.0.0.1", "--destination", "127.0.0.1",
                                      "--port", str(port), "--count", str(count), "--length", str(length),
                                      "--rate", "2000", "--run-id", "7"])
    sent = send(args)
    received = json.loads(receiver.communicate(timeout=10)[0])
    return sent, received


def self_test():
    """Check packet construction offline and a real UDP and TCP exchange on loopback."""
    checks = []
    payload = probe_payload(7, 300, 100)
    expected_body = bytes((300 + i) & 0xFF for i in range(86))
    checks.append(("probe header round trip", parse_probe(payload) == (7, 300, 100) and payload[14:] == expected_body))
    args = build_parser().parse_args(["send", "--kind", "ipip", "--source", "192.0.2.1", "--destination", "198.51.100.1",
                                      "--port", "5001"])
    inner = inner_bytes(args, payload)
    checks.append(("ipip inner IPv4 header checksum", checksum(inner[:20]) == 0 and inner[9] == 17))
    args.kind, args.gre_key = "gre", 5
    gre = inner_bytes(args, payload)
    checks.append(("gre with key", gre[:8] == struct.pack("!HHI", 0x2000, 0x0800, 5) and gre[8:28] == inner[:20]))
    args.kind = "esp"
    checks.append(("esp spi and sequence", inner_bytes(args, payload)[:8] == struct.pack("!II", 1, 301)))
    args.kind, args.inner_source, args.inner_destination = "6in4", "2001:db8::1", "2001:db8::2"
    six = inner_bytes(args, payload)
    pseudo = six[8:40] + struct.pack("!IxxxB", len(six) - 40, 17)
    checks.append(("6in4 inner UDP checksum", six[0] >> 4 == 6 and checksum(pseudo + six[40:]) == 0))
    sent, received = loopback_pair("udp", 200, 200)
    checks.append(("loopback udp", sent["sent"] == 200 and received["identities"] == 200 and received["duplicates"] == 0))
    sent, received = loopback_pair("tcp", 100, 1000)
    checks.append(("loopback tcp", sent["sent"] == 100 and received["bytes"] == 100 * 1000))
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, run send or sink, and print the JSON result."""
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.mode == "send":
        check_send_arguments(parser, args)
        lab.stop_on_signals()
        result = send(args)
    elif args.mode == "sink":
        if not args.bind or args.kind not in ("udp", "tcp") or args.duration <= 0:
            parser.error("sink needs --bind, --kind udp|tcp and a positive --duration")
        lab.stop_on_signals()
        result = sink(args)
    else:
        parser.error("choose send or sink")
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
