"""PPE forwarding-table rows written and read from the host through the qdx-diag "ppe" debugfs file.

A library, not a command: it has no argv and never talks to the router. Every function returns either row words (lists of
32-bit ints) or qdx-diag ppe op text ("r ADDR N" reads N words, "w ADDR V" writes one word, "wn ADDR V..." writes several
words in one go). The caller joins the ops into a router command and parses the "values=" lines that come back.

Addresses are PPE-relative byte offsets. Layouts follow qca-ssdk 446db12b, HPPE (non-APPE) branch. Block bases:
IPE_L3 0x200000, INGRESS_POLICER 0x100000 (hppe_reg_access.h:29,32), NSS_PTX_CSR 0x020000 (:50).

  table            row i at                  words  SSDK source (address/stride; row layout)
  MY_MAC_TBL       IPE_L3 + 0x0     +  8*i   2      hppe_ip_reg.h:30-32; 46-55
  L3_VP_PORT_TBL   IPE_L3 + 0x1000  + 16*i   3      hppe_ip_reg.h:1227-1230; 1403-1456
  IN_L3_IF_TBL     IPE_L3 + 0x2000  +  8*i   2      hppe_ip_reg.h:1464-1465; 1548-1574
  EG_L3_IF_TBL     PTX    + 0xe000  + 16*i   3      hppe_ip_reg.h:2025-2028; 2057-2074; base from hppe_ip.c:700-706
  IN_NEXTHOP_TBL   IPE_L3 + 0x60000 + 16*i   4      hppe_ip_reg.h:1900-1909; 1978-2015 (type 1 = VP layout)
  HOST_TBL         IPE_L3 + 0x20000 + 16*i   3      hppe_ip_reg.h:1772-1778; 1817-1828
  IN_FLOW_TBL      IPE_L3 + 0x40000 + 32*i   5      hppe_flow_reg.h:1764-1770; 1017-1095 (in_flow_tbl_0 NAT, _2 ROUTE)
  IN_FLOW_CNT_TBL  POLICER + 0x20000 + 16*i  3      hppe_flow_reg.h:2173-2175; 2189-2197; base from hppe_flow.c:36-42

  command port     OP        OP_DATA   OP_RSLT   SSDK source
  HOST             0x2004bc  0x2004c0  0x2004e8  hppe_ip_reg.h:627,696,722; fields 674-760
  IN_FLOW          0x2003b8  0x2003c0  0x20040c  hppe_flow_reg.h:270,379,431; fields 322-340,458-470

HOST and FLOW rows are not written directly: the row goes into OP_DATA, a command word into OP, and the hardware places the
row (hash add) or removes it (delete by index) and reports the outcome in OP_RSLT.

Two hardware facts every caller depends on:
  1. A multi-word row is written whole: word 0 to the last word in one "wn" op, with no read in between, then read back
     whole. A read of word 0 between the writes throws away the words already written (probe P-11).
  2. OP_RSLT is a queue, not a register: every operation pushes one result word and every read takes out the oldest one
     (SSDK polls its valid_cnt field, hppe_flow.c:2659, hppe_ip.c:5243). Read it empty before an operation, or the read
     after the operation returns an older result (probe P-01 run 9).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    """A directly addressed table: row i starts at base + stride * i and holds `words` 32-bit words."""
    base: int
    stride: int
    words: int

    def at(self, i):
        return self.base + self.stride * i

    def read(self, i):
        """ppe op reading row i whole."""
        return f"r {self.at(i):#x} {self.words}"


@dataclass(frozen=True)
class Port:
    """A command port: OP, OP_DATA and OP_RSLT addresses, where entry_index sits in OP, and its width in OP_RSLT."""
    op: int
    data: int
    rslt: int
    op_index_shift: int
    rslt_index_bits: int


MY_MAC = Table(0x200000, 8, 2)
VP_PORT = Table(0x201000, 16, 3)
IN_L3_IF = Table(0x202000, 8, 2)
EG_L3_IF = Table(0x02e000, 16, 3)
NEXTHOP = Table(0x260000, 16, 4)
HOST = Table(0x220000, 16, 3)
FLOW = Table(0x240000, 32, 5)
FLOW_CNT = Table(0x120000, 16, 3)

HOST_PORT = Port(0x2004bc, 0x2004c0, 0x2004e8, 11, 13)    # OP entry_index bits 11-23; RSLT entry_index bits 5-17
FLOW_PORT = Port(0x2003b8, 0x2003c0, 0x20040c, 12, 12)    # OP entry_index bits 12-23; RSLT flow_entry_index bits 5-16

IN_L3_IF_BOOT = [0x017705dc, 0x00000001]   # every IN_L3_IF row at boot on the Phase 2 image: mru/mtu 1500, ttl_exceed_cmd 1 (P-01 B)
DRAIN = 16                                 # OP_RSLT reads before each operation; enough in every probe run, depth not in SSDK
FLOW_SNAT, FLOW_DNAT, FLOW_ROUTE = 1, 2, 3  # fwd_type (fal_flow.h:46-54)
UDP, TCP = 2, 1                             # protocol_type (fal_flow.h:66-69)


def wn(base, values):
    """ppe op writing a whole multi-word row starting at base, with no read in between (fact 1)."""
    return f"wn {base:#x} " + " ".join(f"{v:#x}" for v in values)


def port_add(port, row, cmd=0xd):
    """ppe ops for one hash add: empty OP_RSLT (fact 2), write the row to OP_DATA, write OP, read the one new result.
    OP = cmd_id cmd, op_type 0 (add), hash_block_bitmap 3, op_mode 0 (hash) (op codes: adpt_hppe_fdb.c:30-37; SSDK sets the
    same bitmap, hppe_flow.c:2645-2650). The last op's output line is the result word for port_result()."""
    return [f"r {port.rslt:#x} 1"] * DRAIN + [wn(port.data, row), f"w {port.op:#x} {0x300 | cmd:#x}", f"r {port.rslt:#x} 1"]


def port_delete(port, index, cmd=0xd):
    """ppe op deleting row `index`: op_type 1 (delete), hash_block_bitmap 3, op_mode 1 (by index). Its result is queued too."""
    return f"w {port.op:#x} {0x720 | cmd | index << port.op_index_shift:#x}"


def port_result(port, word, cmd=0xd):
    """(ok, index) from one OP_RSLT word: ok when cmd_id echoes cmd and op_rslt (bit 4) is 0; index = entry_index bits 5 up."""
    return word & 0x1f == cmd, word >> 5 & (1 << port.rslt_index_bits) - 1


def my_mac_row(mac):
    """MY_MAC row: MAC bytes 2-5 in word 0, bytes 0-1 in word 1 bits 0-15, valid at bit 48 (packing adpt_hppe_ip.c:1232-1247)."""
    return [int.from_bytes(mac[2:], "big"), int.from_bytes(mac[:2], "big") | 1 << 16]


def vp_port_row(old, l3_if):
    """L3_VP_PORT row `old` with l3_if_valid (bit 0) set and l3_if_index (bits 1-8) = l3_if; other bits kept
    (adpt_hppe_ip.c:390-402)."""
    return [old[0] & ~0x1ff | 1 | l3_if << 1, *old[1:3]]


def in_l3_if_row(old, mac_index):
    """IN_L3_IF row `old` (normally IN_L3_IF_BOOT) plus ipv4_uc_route_en (bit 29) and MY_MAC row mac_index in mac_bitmap
    (bits 35-42)."""
    return [old[0] | 1 << 29, old[1] | 1 << mac_index << 3]


def eg_l3_if_row(mac):
    """EG_L3_IF row: source MAC packed as my_mac_row (no valid bit), PPPoE session 0 (word 1 bits 16-31), pppoe_en 0 (word 2)."""
    return [int.from_bytes(mac[2:], "big"), int.from_bytes(mac[:2], "big"), 0]


def nexthop_row(port, l3_if, mac):
    """NEXTHOP row, type 1 (VP: by port; type 0 means by VSI, fal_ip.h:276-279): port bits 1-8, post_l3_if bits 9-16,
    svid/cvid 0, destination MAC at bits 48-95 (adpt_hppe_ip.c:1438-1455), ip_addr_dnat (word 3) 0."""
    return [1 | port << 1 | l3_if << 9, int.from_bytes(mac[4:], "big") << 16, int.from_bytes(mac[:4], "big"), 0]


def host_row(ip, fwd_cmd, dst_info):
    """HOST row for an IPv4 unicast address (4 bytes): valid, key_type 0, fwd_cmd bits 3-4 (0 forward, 1 drop, 2 copy to CPU,
    3 redirect to CPU; fal_type.h:123-126), dst_info bits 6-19 (1 << 12 | n = NEXTHOP n, 2 << 12 | p = port p; fal_ip.h:97)."""
    return [1 | fwd_cmd << 3 | dst_info << 6, int.from_bytes(ip, "big"), 0]


def flow_row(host_index, ip, sport, dport, fwd_type, nexthop, vp=None, new_port=None, pri=0, host_type=0, protocol=UDP):
    """IPv4 IN_FLOW row, age 3, no source L3 interface check. The key is HOST row host_index (host_type 0: it holds the
    source address, 1: the destination; FLOW_CTRL1 key_sel picks which one the hardware uses) plus the other address `ip`
    (4 bytes), protocol and the two L4 ports. fwd_type FLOW_ROUTE: next_hop3 and port_vp1 = vp (in_flow_tbl_2);
    FLOW_SNAT/FLOW_DNAT: next_hop1/2 and the replacement L4 port new_port (in_flow_tbl_0/_3). pri = pri_profile (bit 63 and
    bits 64-67, adpt_hppe_flow.c:303-304)."""
    action = 1 | vp << 1 if fwd_type == FLOW_ROUTE else new_port          # bits 44-52 (valid + port) or 44-59 (L4 port)
    addr = int.from_bytes(ip, "big")
    return [1 | host_type << 2 | host_index << 3 | protocol << 16 | 3 << 18 | fwd_type << 29,
            nexthop | action << 12 | (pri & 1) << 31,
            (addr & 0xfffff) << 12 | pri >> 1,
            addr >> 20 | sport << 12 | (dport & 0xf) << 28,
            dport >> 4]


def flow_cnt_delta(before, after):
    """(packets, bytes) an IN_FLOW_CNT row gained between two whole-row reads: hit_pkt_counter is word 0 (32 bits),
    hit_byte_counter is word 1 plus word 2 bits 0-7 (40 bits); both wrap."""
    packets = (after[0] - before[0]) % (1 << 32)
    octets = ((after[1] | (after[2] & 0xff) << 32) - (before[1] | (before[2] & 0xff) << 32)) % (1 << 40)
    return packets, octets
