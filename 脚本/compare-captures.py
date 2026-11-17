#!/usr/bin/env python3
"""Pair probe packets between a sending-side capture and receiving-side captures.

Input: classic pcap files (tcpdump -w), Ethernet link type, microsecond or
nanosecond timestamps, either byte order. Everything is read into memory, so
use probe-sized captures. Other formats are rejected (exit 1).

Two kinds of identity:
  datagram  any frame containing a packet-probe header (b"QDXP", run, seq,
            length). The header is found by searching the frame bytes, so
            UDP, multicast, GRE, ESP, IPIP, 6in4, VLAN and PPPoE all work
            without per-encapsulation parsing. Visible payload bytes that
            differ from the probe pattern count as corrupted.
  tcp       TCP segments of connections whose payload contains a probe header
            (the sending capture picks its own connections; all receiving
            captures pick theirs together, so NAT port changes and segments
            split over several receiving ports both work), paired by
            (TCP sequence number, payload length).
            Ethernet, VLAN (0x8100/0x88a8, stacked), PPPoE session, IPv4/IPv6.

Definitions (per identity kind):
  sent        identities in --sent, first appearance time
  lost        sent identities seen in no --received file
  duplicates  received observations of sent identities minus distinct ones
  extra       identities seen only on the receiving side
  reordered   in arrival order over all --received files, first copies whose
              sending order is below the highest sending order already arrived
  delay_us    first received copy time minus sent time: min, p50, p99, max;
              pN is element floor((n-1)*N/100) of the sorted delays. Valid only
              when all captures use one clock (both ports on this host).
  per_received  for each --received file: distinct sent identities, observations
  icmp_quoted   frames on the receiving side whose IP protocol is ICMP or ICMPv6
              and that contain a probe header: an ICMP error quotes the packet it
              rejects, so these count neither as received nor as duplicates
Per file: packets, bytes, first/last time, span, average bit rate, largest
frame, largest TCP payload (P-06 segment size).

Stdout: one JSON object (datagram, tcp, icmp_quoted, files, error). Exit 0 = analysis
done (loss is an observation, not a failure); exit 1 = unreadable input;
exit 2 = usage error.

Example:
  python3 compare-captures.py --sent OUT/capture-0-eno2.pcap \\
      --received OUT/capture-1-enxd4a2cde31c38.pcap --run-id 3
Self-test (builds small pcaps in a temporary directory): --self-test
"""

import argparse
import dataclasses
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab

PROBE_HEADER = struct.Struct("!4sIIH")  # same layout as packet-probe.py
PROBE_MAGIC = b"QDXP"
PATTERN = bytes(range(256)) * 257
VLAN_TYPES = (0x8100, 0x88A8)


@dataclass
class Frame:
    time_ns: int
    data: bytes


@dataclass
class FileStats:
    path: str
    packets: int
    bytes: int
    first_ns: int | None
    last_ns: int | None
    span_s: float
    bits_per_s: float | None
    largest_frame: int
    largest_tcp_payload: int


@dataclass
class Pairing:
    sent: int = 0
    lost: int = 0
    duplicates: int = 0
    extra: int = 0
    reordered: int = 0
    corrupted: int = 0
    delay_us: dict | None = None
    per_received: list | None = None


def read_pcap(path):
    """Read every record of a classic Ethernet pcap file."""
    blob = Path(path).read_bytes()
    if len(blob) < 24:
        raise ValueError(f"{path}: too short for a pcap header")
    for order in ("<", ">"):
        magic = struct.unpack_from(order + "I", blob)[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D):
            break
    else:
        raise ValueError(f"{path}: not a classic pcap file (pcapng is not supported)")
    scale = 1 if magic == 0xA1B23C4D else 1000
    if struct.unpack_from(order + "I", blob, 20)[0] != 1:
        raise ValueError(f"{path}: link type is not Ethernet")
    frames, offset = [], 24
    while offset + 16 <= len(blob):
        seconds, fraction, captured, _ = struct.unpack_from(order + "IIII", blob, offset)
        offset += 16
        frames.append(Frame(seconds * 1_000_000_000 + fraction * scale, blob[offset:offset + captured]))
        offset += captured
    return frames


def find_probe(data):
    """Return (run, seq, corrupted) of the first probe header in the frame, or None."""
    at = data.find(PROBE_MAGIC)
    if at < 0 or at + PROBE_HEADER.size > len(data):
        return None
    _, run_id, seq, length = PROBE_HEADER.unpack_from(data, at)
    body = data[at + PROBE_HEADER.size:at + length]
    start = seq & 0xFF
    return run_id, seq, body != PATTERN[start:start + len(body)]


def ip_packet(data):
    """Return (IP protocol, TCP connection, TCP sequence, TCP payload); None, None, None, b"" where absent."""
    ethertype, at = struct.unpack_from("!H", data, 12)[0], 14
    while ethertype in VLAN_TYPES and at + 4 <= len(data):
        ethertype, at = struct.unpack_from("!H", data, at + 2)[0], at + 4
    if ethertype == 0x8864 and at + 8 <= len(data):  # PPPoE session: 6-byte header + PPP protocol
        ppp = struct.unpack_from("!H", data, at + 6)[0]
        ethertype, at = {0x0021: 0x0800, 0x0057: 0x86DD}.get(ppp, 0), at + 8
    if ethertype == 0x0800 and at + 20 <= len(data):
        protocol, header = data[at + 9], (data[at] & 0x0F) * 4
        addresses, total = data[at + 12:at + 20], struct.unpack_from("!H", data, at + 2)[0]
        transport, end = at + header, at + total
    elif ethertype == 0x86DD and at + 40 <= len(data):
        protocol, addresses = data[at + 6], data[at + 8:at + 40]
        transport, end = at + 40, at + 40 + struct.unpack_from("!H", data, at + 4)[0]
    else:
        return None, None, None, b""
    if protocol != 6 or transport + 20 > len(data):
        return protocol, None, None, b""
    ports, sequence = data[transport:transport + 4], struct.unpack_from("!I", data, transport + 4)[0]
    payload = data[transport + (data[transport + 12] >> 4) * 4:min(end, len(data))]
    return protocol, (addresses, ports), sequence, payload


def observations(frames, run_id):
    """Return a capture's datagram observations, probe-carrying TCP connections, all TCP segments and ICMP quotes."""
    datagrams, connections, segments, quoted = [], set(), [], 0
    for frame in frames:
        protocol, connection, sequence, payload = ip_packet(frame.data)
        if payload:
            segments.append((frame.time_ns, connection, (sequence, len(payload))))
        probe = find_probe(frame.data)
        if probe is None or (run_id is not None and probe[0] != run_id):
            continue
        if connection:
            connections.add(connection)
        elif protocol in (1, 58):  # an ICMP error quotes the packet it rejects; the quote is not a delivery
            quoted += 1
        else:
            datagrams.append((frame.time_ns, (probe[0], probe[1]), probe[2]))
    return datagrams, connections, segments, quoted


def probe_segments(segments, connections):
    """Keep TCP segments of the given connections as (time, identity, corrupted) observations."""
    return [(time_ns, identity, False) for time_ns, connection, identity in segments if connection in connections]


def pair(sent, received_files):
    """Compute loss, duplicates, extras, reordering and delay for one identity kind."""
    first_sent = {}
    for time_ns, identity, _ in sorted(sent, key=itemgetter(0)):
        first_sent.setdefault(identity, time_ns)
    order = {identity: index for index, identity in enumerate(first_sent)}
    result = Pairing(sent=len(first_sent), per_received=[])
    arrivals = []
    for path, observed in received_files:
        mine = [o for o in observed if o[1] in first_sent]
        result.per_received.append({"path": path, "identities": len({o[1] for o in mine}), "observations": len(mine)})
        result.extra += len({o[1] for o in observed if o[1] not in first_sent})
        result.corrupted += sum(1 for o in mine if o[2])
        arrivals += mine
    arrivals.sort(key=itemgetter(0))
    first_arrival, highest = {}, -1
    for time_ns, identity, _ in arrivals:
        if identity in first_arrival:
            continue
        first_arrival[identity] = time_ns
        if order[identity] < highest:
            result.reordered += 1
        highest = max(highest, order[identity])
    result.lost = len(first_sent) - len(first_arrival)
    result.duplicates = len(arrivals) - len(first_arrival)
    delays = sorted((first_arrival[i] - first_sent[i]) / 1000 for i in first_arrival)
    if delays:
        pick = len(delays) - 1
        result.delay_us = {"min": delays[0], "p50": delays[pick * 50 // 100], "p99": delays[pick * 99 // 100],
                           "max": delays[-1]}
    return result


def file_stats(path, frames):
    """Summarize one capture without pairing."""
    times = [f.time_ns for f in frames]
    total = sum(len(f.data) for f in frames)
    span = (max(times) - min(times)) / 1e9 if frames else 0.0
    tcp_payloads = [len(ip_packet(f.data)[3]) for f in frames]
    return FileStats(str(path), len(frames), total, min(times, default=None), max(times, default=None), span,
                     round(total * 8 / span, 1) if span > 0 else None, max((len(f.data) for f in frames), default=0),
                     max(tcp_payloads, default=0))


def compare(sent_path, received_paths, run_id):
    """Read all captures and return the result dict."""
    sent_frames = read_pcap(sent_path)
    received = [(str(p), read_pcap(p)) for p in received_paths]
    sent_datagrams, sent_connections, sent_segments, _ = observations(sent_frames, run_id)
    split = [(path, observations(frames, run_id)) for path, frames in received]
    # One connection's segments may be split over several receiving ports (P-08), so the
    # receiving side selects connections from all receiving files together.
    received_connections = set().union(*(o[1] for _, o in split))
    return {
        "datagram": dataclasses.asdict(pair(sent_datagrams, [(p, o[0]) for p, o in split])),
        "tcp": dataclasses.asdict(pair(probe_segments(sent_segments, sent_connections),
                                       [(p, probe_segments(o[2], received_connections)) for p, o in split])),
        "icmp_quoted": sum(o[3] for _, o in split),
        "files": [dataclasses.asdict(file_stats(sent_path, sent_frames))]
                 + [dataclasses.asdict(file_stats(p, f)) for p, f in received],
        "error": None,
    }


def test_frame(protocol, seq=0, size=0, sport=0, probe=False, tag=b""):
    """Self-test helper: one Ethernet/IPv4 frame; 17 carries a probe, 6 a TCP segment, else an ICMP error quoting one."""
    payload = PROBE_HEADER.pack(PROBE_MAGIC, 7, seq, 100) + PATTERN[seq & 0xFF:(seq & 0xFF) + 86]
    if protocol == 17:
        transport = struct.pack("!HHHH", 40000, 5001, 8 + len(payload), 0) + payload
    elif protocol == 6:
        data = PROBE_HEADER.pack(PROBE_MAGIC, 7, 0, 100) + PATTERN[:86] if probe else bytes(size)
        transport = struct.pack("!HHIIBBHHH", sport, 6001, seq, 1, 0x50, 0x18, 65535, 0, 0) + data
    else:
        transport = struct.pack("!BBHI", 3, 2, 0, 0) + test_frame(17, seq)[14:]  # type 3 code 2: protocol unreachable
    ip = struct.pack("!BBHHHBBH8s", 0x45, 0, 20 + len(transport), 0, 0x4000, 64, protocol, 0,
                     bytes([192, 168, 1, 115, 198, 18, 2, 2]))
    return b"\x02\x00\x00\x00\x00\x01\x02\x00\x00\x00\x00\x02" + tag + b"\x08\x00" + ip + transport


def write_test_pcaps(directory):
    """Self-test helper: write three small pcaps with known loss, duplicate, reorder, extra and TCP cases."""
    t, ms, us = 1000 * 10**9, 10**6, 10**3
    sent = [(t + s * ms, test_frame(17, s)) for s in range(10)] + [
        (t + 20 * ms, test_frame(6, 1000, 100, 41000, probe=True)), (t + 21 * ms, test_frame(6, 1100, 100, 41000)),
        (t + 22 * ms, test_frame(6, 1200, 50, 41000)), (t + 23 * ms, test_frame(6, 1250, 50, 41000)),
        (t + 24 * ms, test_frame(6, 5, 40, 50000)), (t + 25 * ms, test_frame(1))]
    arrivals_a = [(200, 0), (1200, 1), (3200, 3), (3500, 2), (4200, 4), (4300, 4), (6200, 6), (9500, 99)]
    a = [(t + when * us, test_frame(17, seq)) for when, seq in arrivals_a] + [
        (t + 20200 * us, test_frame(6, 1000, 100, 61000, probe=True)), (t + 21200 * us, test_frame(6, 1100, 100, 61000)),
        (t + 6300 * us, test_frame(1, 6))]  # the receiver rejects probe 6 with an ICMP error quoting it
    b = [(t + when * us, test_frame(17, seq)) for when, seq in ((5200, 5), (7200, 7), (8200, 8))] + [
        (t + 22200 * us, test_frame(6, 1200, 50, 61000, tag=b"\x81\x00\x00\x0a"))]
    paths = []
    for name, frames, nanoseconds, order in (("sent", sent, True, "<"), ("a", a, False, "<"), ("b", b, False, ">")):
        blob = struct.pack(order + "IHHiIII", 0xA1B23C4D if nanoseconds else 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
        for time_ns, frame in frames:
            fraction = time_ns % 10**9 if nanoseconds else time_ns % 10**9 // 1000
            blob += struct.pack(order + "IIII", time_ns // 10**9, fraction, len(frame), len(frame)) + frame
        path = Path(directory) / (name + ".pcap")
        path.write_bytes(blob)
        paths.append(path)
    return paths


def self_test():
    """Compare built-in captures against hand-computed expectations."""
    with tempfile.TemporaryDirectory(prefix="qdx-compare-") as directory:
        sent, a, b = write_test_pcaps(directory)
        result = compare(sent, [a, b], 7)
    d, t = result["datagram"], result["tcp"]
    checks = [
        ("datagram counts", (d["sent"], d["lost"], d["duplicates"], d["extra"], d["reordered"], d["corrupted"]) == (10, 1, 1, 1, 1, 0)),
        ("datagram delay", d["delay_us"] == {"min": 200.0, "p50": 200.0, "p99": 200.0, "max": 1500.0}),
        ("per received file", [(r["identities"], r["observations"]) for r in d["per_received"]] == [(6, 7), (3, 3)]),
        ("tcp counts through NAT and VLAN", (t["sent"], t["lost"], t["duplicates"], t["extra"], t["reordered"]) == (4, 1, 0, 0, 0)),
        ("tcp delay", t["delay_us"] == {"min": 200.0, "p50": 200.0, "p99": 200.0, "max": 200.0}),
        ("file stats", result["files"][0]["packets"] == 16 and result["files"][0]["largest_tcp_payload"] == 100),
        ("ICMP quote is neither received nor duplicate", result["icmp_quoted"] == 1),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in checks) else 1


def main():
    """Parse arguments, compare the captures and print the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sent", help="capture taken on the sending side")
    parser.add_argument("--received", action="append", default=[], help="capture on a receiving side (repeatable)")
    parser.add_argument("--run-id", type=int, help="only count probes of this run")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.sent or not args.received:
        parser.error("--sent and at least one --received are required")
    try:
        result = compare(args.sent, args.received, args.run_id)
    except (OSError, ValueError, struct.error) as error:
        # Boundary: unreadable or unsupported input is a reported failure, not a traceback.
        result = {"datagram": None, "tcp": None, "icmp_quoted": None, "files": [], "error": str(error)}
    return lab.print_result(result, result["error"] is None)


if __name__ == "__main__":
    sys.exit(main())
