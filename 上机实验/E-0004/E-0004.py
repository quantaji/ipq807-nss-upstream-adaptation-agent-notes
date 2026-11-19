#!/usr/bin/env python3
"""E-0004（阶段 1 第四步：多个环和日常功能）的读数脚本。

判据在同一个文件夹的 报告.md 第 1.5 节，编号和设计 4-多环和日常功能.md 第五节一样。
一次只做一个阶段（--stage）：

  base      判据 1、2、3、4、12、13
  mtu       判据 8、9、10、11、29（8 先做）
  spread    判据 5、6、7
  updown    判据 14、15、16
  pause     判据 17
  reload    判据 18
  overload  判据 19
  load      把一个实验用的模块传到路由器上换着加载（--file 文件名），或者换回正式的（--file normal）
  buffer    判据 20、21（要先 load 缓冲实验的模块）
  txrate    发包的两个比较里的一遍（--with-lan2 是同时往 lan2 发的那一遍）
  quiet     在路由器上把发地址的服务停掉并核对（换线以前）
  swapped   换线以后轻的那几样（--ports 口=主机网卡,...；--mtu-ports 要量到上限的口）
  long      长时间的测量里的一种（--kind in、out、through、wan-in、wan-out；--seconds 默认 1800）
  misc      判据 28（要先 load 硬件报错中断的模块）
  maxframe  上机以后加的一项，不是判据，只报数：硬件真正能带多长的帧（要先 load variant-maxframe.ko）
  maxframe-back  帧长那一项做完、换回正式的模块以后：两头的 MTU 是 1500，ping 100 个回 100 个
  finish    删掉主机 br-lan 的测试地址，回读核对和开工以前一样

例子（在这个文件夹里跑，前面带 PYTHONDONTWRITEBYTECODE=1）：
  python3 E-0004.py --stage base --label 第1版-base --work 工作文件夹 \\
      --module 工作文件夹/模块-第1版.ko --output 工作文件夹/第1版/base --summary 结果-第1版-base.json

工作文件夹（--work）是上机期间放东西的地方，由跑脚本的人给，脚本里不写死：
  - 里面的 实验用的模块/ 放实验用的模块，load 阶段从这里取；
  - 脚本把开工时主机 br-lan 的样子记在里面的 主机br-lan-开工以前.txt，finish 阶段拿它来比。
它里面的东西只在上机期间用，收工以后不保留；要留下来的读数都在结果摘要里。

主机这一头做的事：
  - eno2、USB 网卡：ping、跑 iperf3；mtu、swapped 阶段临时改它们的 MTU，做完改回 1500。
    它们平时的测试地址由 脚本/test-network.py 配。
  - long 的 through：把 USB 网卡放进网络命名空间 e0004，做完放回来、把测试地址加回去。
  - br-lan（eno1 是它的成员）：加、删 192.168.13.10/24 这一个测试地址；从它 ping、跑 iperf3；
    mtu、swapped 阶段临时改 eno1 和 br-lan 的 MTU，做完改回 1500。
  - maxframe 阶段：把 eno1、br-lan、USB 网卡的 MTU 临时调到比 12262 大（不超过网卡自己的上限），做完改回 1500。
  - eno1：只读 carrier、speed 和 statistics/ 下面的数，改 MTU。
路由器这一头的改动都只在内存里。地址（MAC）的数值不显示、不写进任何文件。

结果是标准输出最后一行的一个 JSON 对象。
退出码 0 表示读完了（不等于每条判据都通过），1 表示没读完，2 表示用法错误。
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "..", "脚本")
sys.path.insert(0, SCRIPTS)
import lab

# E-0003 的读数脚本里现成的函数：ping、读收发包数、读队列计数、换模块。
_spec = importlib.util.spec_from_file_location("e0003", os.path.join(HERE, "..", "E-0003", "E-0003.py"))
e3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e3)
ask = e3.ask
read_registers = e3.read_registers

USB = e3.USB
ENO2_ADDRESS, ROUTER_LAN = e3.ENO2_ADDRESS, e3.ROUTER_LAN
USB_ADDRESS, ROUTER_USB_SIDE = e3.USB_ADDRESS, e3.ROUTER_USB_SIDE
BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE = e3.BRIDGE_ADDRESS, e3.ROUTER_BRIDGE_SIDE
# USB 网卡放进网络命名空间以后用的地址，和 eno2 同一个网段。
NETNS, USB_NETNS_ADDRESS = "e0004", "192.168.1.11"

CORES = 4
MAX_MTU = 12262
# 到上限的长帧：12262 减去 IP 头 20、ICMP 头 8。
MAX_PING = MAX_MTU - 28
FLOWS = 64
# 帧长那一项（maxframe 阶段）。实验用的模块 variant-maxframe.ko 许设的最大 MTU：
# 14 位的帧长一栏最大是 16383，减去以太网头 14、两个 VLAN 标签 8、帧尾的校验 4。
FRAME_TOP = 16383 - 26
FRAME_STEPS = (13000, 14000, 15000, 16000)
# 主机网卡 statistics/ 下面要读的数：发出去没有、收到没有、收的时候记成了哪一种错。
HOST_FRAME_ITEMS = ("tx_packets", "tx_errors", "tx_dropped", "rx_packets", "rx_errors", "rx_length_errors",
                    "rx_crc_errors", "rx_over_errors", "rx_missed_errors", "rx_dropped")
HOST_QUEUES = range(16, 16 + CORES)
OTHER_CPU_QUEUES = [q for q in list(range(0, 144)) + list(range(256, 272)) if q not in HOST_QUEUES]

EDMA = 0xb00000
RX_RINGS, FILL_RINGS = range(12, 16), range(4, 8)
# 发包的排法，main 里照 --layout 设。两种都是每个口各有自己的发包环，从 10 号往上、照口号从小到大发：
#   万兆口四个：wan、lan1 各 4 个，lan2、lan3、lan4 各 2 个，一共 14 个（10 到 23 号）。正式的模块。
#   每个口两个：五个口各 2 个（10 到 19 号）。实验用的模块 variant-perport2。
# 发完环的个数等于发包环最多的那个口的个数，用编号最大的那几个；每个口的第 n 条发送队列用第 n 个发完环。
LAYOUT = "万兆口四个"
# 这块板子的五个口，照口号 2、3、4、5、6 的次序。
PORT_ORDER = ("lan4", "lan3", "lan2", "wan", "lan1")


def port_queues(name):
    """这个口有几条发送队列，也就是有几个自己的发包环。"""
    return 4 if LAYOUT == "万兆口四个" and name in ("wan", "lan1") else 2


def port_rings(name):
    """这个口的发包环的环号。"""
    first = 10 + sum(port_queues(other) for other in PORT_ORDER[:PORT_ORDER.index(name)])
    return range(first, first + port_queues(name))


def tx_rings():
    """主机用的全部发包环的环号。"""
    return [ring for name in PORT_ORDER for ring in port_rings(name)]


def cmpl_count():
    """发完环有几个。"""
    return max(port_queues(name) for name in PORT_ORDER)


def cmpl_ring_of(ring):
    """这个发包环发完的报文登记在哪个发完环里。"""
    for name in PORT_ORDER:
        if ring in port_rings(name):
            return 8 - cmpl_count() + ring - port_rings(name)[0]
    raise ValueError(f"{ring} 号不是主机的发包环")


def cmpl_irqs():
    """发完中断的名字。"""
    return [f"edma_txcmpl{i}" for i in range(cmpl_count())]


REG_TXDESC2CMPL = EDMA + 0xc
REG_RXDESC2FILL = EDMA + 0x18
REG_QID2RID_HOST = EDMA + 0x5a000 + 4 * 2
REG_RING_ROWS = 0x400000 + 0x2a000
REG_QUEUE_COUNTER_ENABLE = 0x26000

# 路由器控制台上的几个小函数。
#   th：每个收发线程的名字和许跑的核心。 iq：每个 EDMA 中断的名字和许跑的核心。
#   cpu：四个核心各自的忙和闲的累计。
#   sn：每个核心的协议栈到现在处理过多少个包（十六进制，一行一个核心）。
#   rp：五个口"收的包转交给哪些核心"的设置（rps_cpus），0 是不转交。
ROUTER_HELPERS = (
    "th() { for c in /proc/[0-9]*/comm; do n=$(cat $c 2>/dev/null); case \"$n\" in napi/edma_*) "
    "p=${c#/proc/}; echo \"$n $(grep Cpus_allowed: /proc/${p%/comm}/status | cut -f2)\";; esac; done; }",
    "iq() { grep edma_ /proc/interrupts | while read n r; do echo \"${r##* } $(cat /proc/irq/${n%:}/smp_affinity)\"; done; }",
    "cpu() { grep '^cpu[0-9]' /proc/stat | cut -d' ' -f1-8; }",
    "sn() { cut -d' ' -f1 /proc/net/softnet_stat; }",
    "rp() { for p in lan1 lan2 lan3 lan4 wan; do echo \"$p $(cat /sys/class/net/$p/queues/rx-0/rps_cpus)\"; done; }",
    # tt：每个收发线程和每个核心的软中断线程到现在用了多少核心时间（名字、进程号、用户态的嘀嗒数、内核态的嘀嗒数）。
    "tt() { for c in /proc/[0-9]*/comm; do n=$(cat $c 2>/dev/null); case \"$n\" in napi/edma_*|ksoftirqd/*) "
    "p=${c#/proc/}; p=${p%/comm}; set -- $(cut -d')' -f2 /proc/$p/stat); echo \"$n $p ${12} ${13}\";; esac; done; }",
    # qd：三个口各自的发送队列到现在丢了多少、口自己记的发送丢了多少。
    "qd() { for d in lan2 lan3 wan; do echo \"$d $(tc -s qdisc show dev $d | grep -o 'dropped [0-9]*' | head -n 1) "
    "$(cat /sys/class/net/$d/statistics/tx_dropped)\"; done; }",
    # iq2：每个 EDMA 中断到现在一共进了多少次。
    "iq2() { grep edma_ /proc/interrupts | while read n a b c d r; do echo \"${r##* } $((a+b+c+d))\"; done; }",
    # ns：路由器自己的 TCP 计数（名字一行、数一行，共两组）。
    "ns() { grep -E '^(Tcp|TcpExt):' /proc/net/snmp /proc/net/netstat | cut -d: -f2-; }",
    # bq：五个口每条发送队列现在压着多少字节没有发完。
    "bq() { for f in /sys/class/net/lan[1-4]/queues/tx-*/byte_queue_limits/inflight "
    "/sys/class/net/wan/queues/tx-*/byte_queue_limits/inflight; do echo \"$f $(cat $f)\"; done; }",
)


def stack_cores(port, prompt, folder, name):
    """每个核心的协议栈到现在处理过多少个包（/proc/net/softnet_stat 每一行的第一栏）。"""
    return [int(line, 16) for line in ask(port, prompt, folder, name, "sn", 15).split("\n")]


def steering(port, prompt, folder, name):
    """五个口"收的包转交给哪些核心"的设置。返回 {口: 读到的字}。"""
    return dict(line.split() for line in ask(port, prompt, folder, name, "rp", 15).split("\n"))


def load_oddities(reading, port_speed, sent_rings=None):
    """很多条流的一遍读完以后，查这一遍是不是真的量到了要量的东西。返回不对劲的地方，一处一句。

    reading 里要有：iperf3、五个口的rps_cpus、四个核心的协议栈各处理了多少个包、edma_misc涨的。
    port_speed 是这个口连在多少兆。sent_rings 是路由器发的那一遍里这个口每个发包环发了多少。
    """
    odd = []
    run = reading["iperf3"]
    if not run["做完"]:
        odd.append("iperf3 没有跑完")
    rates = run["每10秒的速率"]
    if port_speed and rates and min(rates) < port_speed * 1000000 // 10:
        odd.append(f"这个口连在 {port_speed}，有一个 10 秒的速率只有 {min(rates)} 比特每秒，不到一成")
    steered = {name: value for name, value in reading["五个口的rps_cpus"].items() if int(value.replace(",", ""), 16)}
    if steered:
        odd.append(f"有的口收的包被转交给了固定的核心：{steered}")
    cores = reading["四个核心的协议栈各处理了多少个包"]
    if not all(cores):
        odd.append(f"有的核心的协议栈一个包都没有处理：{cores}")
    elif max(cores) > 0.9 * sum(cores):
        odd.append(f"协议栈处理的包九成以上压在一个核心上：{cores}")
    if sent_rings and len(sent_rings) > 1 and sum(sent_rings) and max(sent_rings) > 0.95 * sum(sent_rings):
        odd.append(f"几乎只有一个发包环在发：{sent_rings}")
    if reading["edma_misc涨的"]:
        odd.append(f"硬件报错的中断涨了 {reading['edma_misc涨的']} 次")
    if run.get("主机的进程忙到百分之几", 0) > 90:
        odd.append(f"主机这一头的 iperf3 忙到了 {run['主机的进程忙到百分之几']}%，这一遍量到的可能是主机")
    return odd


def router_stats(port, prompt, folder, name, netdev):
    """读路由器一个口 ethtool -S 的全部计数，返回 {名字: 数}。"""
    text = ask(port, prompt, folder, name, f"ethtool -S {netdev}", 30)
    stats = {}
    for line in text.split("\n"):
        key, _, value = line.strip().partition(": ")
        if value.strip().isdigit():
            stats[key] = int(value)
    return stats


def router_number(port, prompt, folder, name, command):
    """在路由器上执行一条只输出一个数的命令。"""
    return int(ask(port, prompt, folder, name, command, 20))


def host_ping(folder, name, source, target, count, size=None, interval=None, deadline=None):
    """主机从一个测试地址 ping，数据是固定的花样。返回（发了几个，回了几个）。

    deadline 是最多跑多少秒：对面一个都不回的时候，ping 自己不会按时发完，要靠它收住。
    """
    argv = ["sudo", "-n", "ping", "-n", "-q", "-c", str(count), "-W", "1", "-I", source, "-p", "a55a"]
    if deadline:
        argv += ["-w", str(deadline)]
    if size:
        argv += ["-s", str(size), "-M", "do"]
    if interval:
        argv += ["-i", str(interval)]
    _, output = e3.host(argv + [target], folder, name, timeout=count * 2 + 60)
    found = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def router_ping(port, prompt, folder, name, target, count, size=None):
    """路由器 ping 主机。返回（发了几个，回了几个）。"""
    command = f"ping -q -c {count} -W 1 " + (f"-s {size} " if size else "") + f"{target}; true"
    return e3.parse_ping(ask(port, prompt, folder, name, command, count * 2 + 60))


def iperf_servers(port, prompt, folder, tag, ports=(5201,)):
    """在路由器上起 iperf3 的服务端（先把以前的停掉）。"""
    ask(port, prompt, folder, f"{tag}-停以前的iperf3", "killall iperf3 2>/dev/null; sleep 1; true", 20)
    for number in ports:
        ask(port, prompt, folder, f"{tag}-起iperf3-{number}", f"iperf3 -s -D -p {number}", 20)
    time.sleep(1)


def iperf_argv(target, source, seconds, flows, udp=False, reverse=False, server_port=5201, length=None,
               rate="0", interval=10):
    """主机这一头 iperf3 客户端的命令。reverse 是对面发、这一头收。length 是每个包带多少字节的数据。

    rate 只管 UDP：每条流限到多少（iperf3 的写法，例如 100M），"0" 是不限速。interval 是每多少秒记一次速率。
    """
    argv = ["iperf3", "-c", target, "-B", source, "-p", str(server_port), "-P", str(flows),
            "-t", str(seconds), "-i", str(interval), "-J"]
    if udp:
        argv += ["-u", "-b", rate]
    if length:
        argv += ["-l", str(length)]
    if reverse:
        argv += ["-R"]
    return argv


def iperf_report(text, error=""):
    """把 iperf3 的 JSON 读成几样要用的数。"每10秒的速率"是每一个间隔的速率（间隔默认 10 秒），比特每秒。"""
    try:
        report = json.loads(text)
    except ValueError:
        return {"做完": False, "每10秒的速率": [], "一共字节": 0, "错误": (error or text)[-200:],
                "主机的进程忙到百分之几": 0, "路由器的进程忙到百分之几": 0, "重传": None,
                "UDP丢的": None, "UDP乱序的": None}
    rates = [round(item["sum"]["bits_per_second"]) for item in report.get("intervals", [])]
    end = report.get("end", {})
    total = end.get("sum_received", end.get("sum", {})).get("bytes", 0)
    busy = end.get("cpu_utilization_percent", {})
    streams = [stream.get("udp", {}) for stream in end.get("streams", [])]
    is_udp = any(streams)
    return {"做完": "error" not in report, "每10秒的速率": rates, "一共字节": total,
            "错误": report.get("error", ""),
            "主机的进程忙到百分之几": round(busy.get("host_total", 0), 1),
            "路由器的进程忙到百分之几": round(busy.get("remote_total", 0), 1),
            "重传": end.get("sum_sent", {}).get("retransmits"),
            "UDP丢的": sum(s.get("lost_packets", 0) for s in streams) if is_udp else None,
            "UDP乱序的": sum(s.get("out_of_order", 0) for s in streams) if is_udp else None}


def iperf(folder, name, target, source, seconds, flows, udp=False, reverse=False, netns=None, rate="0", bind_port=5201):
    """跑一遍 iperf3，把它的 JSON 存下来。返回 iperf_report 的那几样。"""
    argv = iperf_argv(target, source, seconds, flows, udp, reverse, server_port=bind_port, rate=rate)
    if netns:
        argv = ["sudo", "-n", "ip", "netns", "exec", netns] + argv
    done = subprocess.run(argv, capture_output=True, text=True, timeout=seconds + 120)
    with open(os.path.join(folder, name + ".json"), "x") as file:
        file.write(done.stdout or done.stderr)
    return iperf_report(done.stdout, done.stderr)


def queue_delta(before, after, queues):
    """这几条队列的计数各涨了多少。"""
    return {queue: after[queue] - before[queue] for queue in queues}


def host_queue_counts(port, prompt, folder, name):
    """只读主机的四条队列的计数（比读整张表快）。返回 {队列号: 包数}。"""
    words = read_registers(port, prompt, folder, name, e3.REG_QUEUE_COUNTERS + HOST_QUEUES[0] * 16, CORES * 4)
    return {queue: words[index * 4] for index, queue in enumerate(HOST_QUEUES)}


def host_pause_frames():
    """主机 eno2 收到的暂停帧数（igc 驱动的 rx_flow_control_xoff）。只读。"""
    text = subprocess.run(["ethtool", "-S", "eno2"], capture_output=True, text=True).stdout
    found = re.search(r"rx_flow_control_xoff: (\d+)", text)
    if not found:
        raise RuntimeError("主机 eno2 的计数里没有 rx_flow_control_xoff")
    return int(found.group(1))


def severe_lines(port, prompt, folder, name):
    """内核记录里警告和致命错误的行数。"""
    return router_number(port, prompt, folder, name,
                         'dmesg | grep -cE "WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error"; true')


def set_host_mtu(folder, tag, interface, mtu):
    """改主机一块网卡的 MTU。eno1 是 br-lan 的成员：加大时先改 eno1，改小时先改 br-lan。"""
    names = [interface]
    if interface == "eno1":
        names = ["eno1", "br-lan"] if mtu > 1500 else ["br-lan", "eno1"]
    for name in names:
        code, _ = e3.host(["sudo", "-n", "ip", "link", "set", "dev", name, "mtu", str(mtu)],
                          folder, f"{tag}-主机{name}的MTU改成{mtu}")
        if code:
            raise RuntimeError(f"{tag}：主机 {name} 的 MTU 没有改成")


# 路由器的防火墙里临时放行一个口的那条规则带的记号，删的时候靠它找。
FIREWALL_MARK = "E-0004"
FIREWALL_DROP_MARKED = ("for h in $(nft -a list chain inet fw4 input | grep " + FIREWALL_MARK +
                        " | sed 's/.*handle //'); do nft delete rule inet fw4 input handle $h; done")


def firewall_open(port, prompt, folder, tag, name):
    """在路由器的防火墙里临时放行从一个口进来的包（只在内存里）。先把以前留下的带记号的规则删掉。

    为什么要：单独的 LAN 口不在防火墙的任何一个区里，进来的包一律被回"目的端口不可达"；
    wan 只放行 ping 这几样，主机连路由器 wan 上的 iperf3 会被回"连接被拒绝"。
    """
    ask(port, prompt, folder, f"{tag}-防火墙里删以前留下的放行规则", FIREWALL_DROP_MARKED, 20)
    ask(port, prompt, folder, f"{tag}-防火墙里临时放行{name}",
        f"nft 'insert rule inet fw4 input iifname \"{name}\" counter accept comment \"{FIREWALL_MARK}\"'", 20)


def firewall_close(port, prompt, folder, tag):
    """把临时放行的规则删掉，读一次带记号的规则还剩几条（要是 0）。"""
    ask(port, prompt, folder, f"{tag}-防火墙里删掉临时放行的规则", FIREWALL_DROP_MARKED, 20)
    return ask(port, prompt, folder, f"{tag}-防火墙里带记号的规则还剩几条",
               f"nft -a list chain inet fw4 input | grep -c {FIREWALL_MARK}; true", 20)


def router_port_alone(port, prompt, folder, tag, name, address):
    """把路由器的一个口从软件桥里拿出来，测试地址直接加在这个口上（wan 本来就不在桥里）。

    同时在防火墙里临时放行这个口，router_port_back 再删掉。
    """
    if name != "wan":
        ask(port, prompt, folder, f"{tag}-{name}从桥里拿出来", f"ip link set {name} nomaster", 15)
        ask(port, prompt, folder, f"{tag}-桥上去掉{address}", f"ip addr del {address}/24 dev br-lan 2>/dev/null; true", 15)
    firewall_open(port, prompt, folder, tag, name)
    ask(port, prompt, folder, f"{tag}-{name}加{address}", f"ip addr replace {address}/24 dev {name}", 15)


def router_port_back(port, prompt, folder, tag, name, address):
    """把口放回软件桥，测试地址放回桥上。"""
    firewall_close(port, prompt, folder, tag)
    if name == "wan":
        return
    ask(port, prompt, folder, f"{tag}-{name}上去掉{address}", f"ip addr del {address}/24 dev {name} 2>/dev/null; true", 15)
    ask(port, prompt, folder, f"{tag}-{name}放回桥里", f"ip link set {name} master br-lan", 15)
    ask(port, prompt, folder, f"{tag}-桥上加回{address}", f"ip addr replace {address}/24 dev br-lan", 15)


def max_frame_both_ways(port, prompt, folder, tag, name, interface, source, target):
    """一个口量到上限：两头 MTU 改成 12262，主机 ping 路由器、路由器 ping 主机各 100 个，再改回 1500。"""
    router_port_alone(port, prompt, folder, tag, name, target)
    set_host_mtu(folder, tag, interface, MAX_MTU)
    ask(port, prompt, folder, f"{tag}-{name}的MTU改成上限", f"ip link set {name} mtu {MAX_MTU}", 15)
    time.sleep(2)
    if source == BRIDGE_ADDRESS:
        e3.ensure_bridge_address(folder, tag)
    try:
        inward = host_ping(folder, f"{tag}-主机ping路由器", source, target, 100, MAX_PING, 0.05)
        outward = router_ping(port, prompt, folder, f"{tag}-路由器ping主机", source, 100, MAX_PING)
    finally:
        # 边界：不管量没量成，两头的 MTU 和路由器的桥都要改回去。
        ask(port, prompt, folder, f"{tag}-{name}的MTU改回1500", f"ip link set {name} mtu 1500", 15)
        set_host_mtu(folder, tag, interface, 1500)
        router_port_back(port, prompt, folder, tag, name, target)
    return {"口": name, "主机ping路由器": list(inward), "路由器ping主机": list(outward),
            "通过": inward == (100, 100) and outward == (100, 100)}


def link_stats_check(port, prompt, folder, tag, name, interface, source, target, count=1000):
    """判据 12：主机 ping 路由器，路由器这个口的收发包数涨的，和主机网卡发、收的帧数涨的比。"""
    if source == BRIDGE_ADDRESS:
        e3.ensure_bridge_address(folder, tag)
    before = e3.router_counts(port, prompt, folder, f"{tag}-以前-路由器的收发包数")[name]
    host_before = (e3.host_stat(interface, "tx_packets"), e3.host_stat(interface, "rx_packets"))
    sent, received = host_ping(folder, f"{tag}-ping", source, target, count, None, 0.01)
    time.sleep(1)
    after = e3.router_counts(port, prompt, folder, f"{tag}-以后-路由器的收发包数")[name]
    host_after = (e3.host_stat(interface, "tx_packets"), e3.host_stat(interface, "rx_packets"))
    rx_gap = (after[0] - before[0]) - (host_after[0] - host_before[0])
    tx_gap = (after[1] - before[1]) - (host_after[1] - host_before[1])
    return {"口": name, "发了": sent, "回了": received, "路由器收包数涨": after[0] - before[0],
            "主机发帧数涨": host_after[0] - host_before[0], "路由器发包数涨": after[1] - before[1],
            "主机收帧数涨": host_after[1] - host_before[1],
            "通过": (sent, received) == (count, count) and abs(rx_gap) <= 20 and abs(tx_gap) <= 20}


PATHS = (("lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN), ("lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE),
         ("wan", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE))
# 帧长那一项量的两个口：wan 是万兆 MAC，lan3 是千兆 MAC。
# 每一行最后一样是这个口最多量到的 MTU；None 是量到两头都设得上的最大的数。
FRAME_PATHS = (("wan", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE, None),
               ("lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE, None))


def host_max_mtu(interface):
    """主机一块网卡自己许设的最大 MTU。ip 的输出里带地址，所以不存下来，只取这一个数。"""
    text = subprocess.run(["ip", "-d", "-j", "link", "show", "dev", interface], capture_output=True, text=True).stdout
    return json.loads(text)[0]["max_mtu"]


def frame_counts(port, prompt, folder, tag, name, interface):
    """读一遍"帧走到了哪里"要看的数：路由器这个口 MAC 的计数和收发环的计数、这个口丢了多少、
    主机网卡发了收了多少和记的错、内核记录里警告和致命错误的行数、edma_misc 的次数。"""
    counts = router_stats(port, prompt, folder, f"{tag}-路由器{name}的计数", name)
    for item in ("rx_dropped", "rx_errors", "tx_dropped", "tx_errors"):
        counts[f"路由器{name}的{item}"] = router_number(port, prompt, folder, f"{tag}-路由器{name}的{item}",
                                                     f"cat /sys/class/net/{name}/statistics/{item}")
    for item in HOST_FRAME_ITEMS:
        counts[f"主机{interface}的{item}"] = e3.host_stat(interface, item)
    counts["警告和致命错误的行数"] = severe_lines(port, prompt, folder, f"{tag}-警告和致命错误的行数")
    counts["edma_misc的次数"] = e3.interrupt_count(port, prompt, folder, f"{tag}-edma_misc的次数", "edma_misc")
    return counts


def frame_rise(before, after):
    """两遍 frame_counts 之间涨了的数；警告和致命错误的行数、edma_misc 的次数不管涨没涨都单独列。"""
    return {"涨了的计数": {key: after[key] - before[key] for key in after if after[key] != before[key]},
            "新增的警告和致命错误的行数": after["警告和致命错误的行数"] - before["警告和致命错误的行数"],
            "edma_misc的次数涨的": after["edma_misc的次数"] - before["edma_misc的次数"]}


def frame_mtu_set(port, prompt, folder, tag, name, interface, source, target, mtu):
    """把两头的 MTU 都设成 mtu，等到小包 ping 得通（改 MTU 以后网卡要重新连一下）。返回小包（发了几个，回了几个）。"""
    set_host_mtu(folder, tag, interface, mtu)
    now = ask(port, prompt, folder, f"{tag}-路由器{name}的MTU改成{mtu}",
              f"ip link set {name} mtu {mtu}; cat /sys/class/net/{name}/mtu", 15)
    if now.split("\n")[-1] != str(mtu):
        raise RuntimeError(f"路由器的 {name} 不收 MTU {mtu}：现在加载的是不是 variant-maxframe.ko")
    if source == BRIDGE_ADDRESS:
        e3.ensure_bridge_address(folder, tag)
    small = (0, 0)
    for attempt in range(15):
        time.sleep(2)
        small = host_ping(folder, f"{tag}-小包-第{attempt}次", source, target, 3, None, 0.2)
        if small == (3, 3):
            break
    return small


def misc_status_lines(port, prompt, folder, tag):
    """量帧长用的实验模块在硬件报错的中断来的时候记进内核记录的状态字，最后三行。"""
    return ask(port, prompt, folder, f"{tag}-硬件报错的状态字", "dmesg | grep 'misc status' | tail -n 3; true", 20).split("\n")


def frame_try(port, prompt, folder, name, interface, source, target, mtu, label=""):
    """两头的 MTU 都设成 mtu，主机 ping 路由器 100 个带满、不许分片的包。返回回了几个和前后涨了的数。

    "出了事"是先发的小包不通，或者硬件报错的中断来过。量帧长用的实验模块在这个中断来一次以后就把它关掉，
    加载模块时重新打开。所以出了事以后这个口上先不往下量，把模块重新加载一遍再接着量。
    """
    tag = f"{name}-{mtu}{label}"
    small = frame_mtu_set(port, prompt, folder, tag, name, interface, source, target, mtu)
    before = frame_counts(port, prompt, folder, f"{tag}-以前", name, interface)
    sent, received = host_ping(folder, f"{tag}-带满的包", source, target, 100, mtu - 28, 0.05)
    time.sleep(1)
    after = frame_counts(port, prompt, folder, f"{tag}-以后", name, interface)
    reading = {"口": name, "MTU": mtu, "对应整帧": mtu + 26, "先发的小包": list(small), "发了": sent, "回了": received,
               "100个回100个": (sent, received) == (100, 100)}
    reading.update(frame_rise(before, after))
    reading["出了事"] = small != (3, 3) or reading["edma_misc的次数涨的"] > 0
    if reading["edma_misc的次数涨的"]:
        reading["内核记录里硬件报错的状态字"] = misc_status_lines(port, prompt, folder, tag)
    # 量完以后 PPE 的缓冲管理里各个口还占着多少缓冲：长帧一个要占几十份，卡在里面出不来的看得到。
    reading["量完以后的PPE缓冲管理"] = bm_ports(port, prompt, folder, f"{tag}-以后")
    # 每量完一个数就接着写下来：路由器中途卡住或者重启时，量过的数还在。
    with open(os.path.join(folder, "量过的每个数.jsonl"), "a") as file:
        file.write(json.dumps(reading, ensure_ascii=False) + "\n")
    return reading


def frame_tcp(port, prompt, folder, name, interface, source, target, mtu, reverse):
    """两头的 MTU 是 mtu 的时候，64 条 TCP 流跑 30 秒。reverse 是路由器发、主机收。返回每秒的速率。"""
    way = "路由器发主机收" if reverse else "主机发路由器收"
    tag = f"{name}-{mtu}-{way}"
    before = frame_counts(port, prompt, folder, f"{tag}-以前", name, interface)
    argv = iperf_argv(target, source, 30, FLOWS, reverse=reverse)
    argv[argv.index("-i") + 1] = "1"
    done = subprocess.run(argv, capture_output=True, text=True, timeout=150)
    with open(os.path.join(folder, f"{tag}-iperf3.json"), "x") as file:
        file.write(done.stdout or done.stderr)
    time.sleep(1)
    after = frame_counts(port, prompt, folder, f"{tag}-以后", name, interface)
    try:
        report = json.loads(done.stdout)
    except ValueError:
        # 外部边界：iperf3 没有跑起来时输出的不是 JSON，照实记下它说了什么。
        report = {"error": done.stderr[-200:] or "iperf3 的输出不是 JSON"}
    reading = {"口": name, "MTU": mtu, "方向": way, "做完": "error" not in report, "错误": report.get("error", ""),
               "每秒的速率（比特每秒）": [round(item["sum"]["bits_per_second"]) for item in report.get("intervals", [])]}
    reading.update(frame_rise(before, after))
    if reading["edma_misc的次数涨的"]:
        reading["内核记录里硬件报错的状态字"] = misc_status_lines(port, prompt, folder, tag)
    return reading


def frame_limit(port, prompt, folder, name, interface, source, target, limit, low, high):
    """一个口上找最大能 100 个回 100 个的 MTU。limit 是这个口最多量到的 MTU，None 是不另外限。

    low 是以前量出来的最大的通的数，high 是最小的不通的数。low 没有给就从头量：先量 12262（这一遍不通就
    不往下量），再量 13000、14000、15000、16000 和两头都设得上的最大的数。low 给了就不从头量，接着找。
    有通有不通的，在最小的不通的数和它下面最大的通的数之间对半找，精确到 1 个字节；
    找完了，在最大能通的数上两个方向各跑 30 秒 TCP。
    哪一个数出了事（见 frame_try）就停在那里：返回里写明找到了哪两个数之间，
    把模块重新加载一遍以后带着这两个数再来。做完两头的 MTU 改回 1500。
    """
    host_top = host_max_mtu(interface)
    top = min(FRAME_TOP, host_top, limit or FRAME_TOP)
    readings, best, tcp, trouble = [], None, [], False
    router_port_alone(port, prompt, folder, f"{name}-开头", name, target)
    try:
        if low is None:
            for mtu in [MAX_MTU] + [step for step in FRAME_STEPS if step < top] + [top]:
                readings.append(frame_try(port, prompt, folder, name, interface, source, target, mtu))
                trouble = readings[-1]["出了事"]
                if trouble or not readings[0]["100个回100个"]:
                    break
            failed = [reading["MTU"] for reading in readings if not reading["100个回100个"]]
            high = min(failed) if failed else None
            passed = [reading["MTU"] for reading in readings
                      if reading["100个回100个"] and (high is None or reading["MTU"] < high)]
            low = max(passed) if passed else None
        while not trouble and low is not None and high is not None and high - low > 1:
            middle = (low + high) // 2
            readings.append(frame_try(port, prompt, folder, name, interface, source, target, middle))
            trouble = readings[-1]["出了事"]
            if readings[-1]["100个回100个"]:
                low = middle
            else:
                high = middle
        found = low is not None and (high is None or high - low <= 1)
        if found and not trouble:
            best = low
            frame_mtu_set(port, prompt, folder, f"{name}-{best}-TCP", name, interface, source, target, best)
            iperf_servers(port, prompt, folder, f"{name}-{best}-TCP")
            tcp = [frame_tcp(port, prompt, folder, name, interface, source, target, best, False),
                   frame_tcp(port, prompt, folder, name, interface, source, target, best, True)]
    finally:
        # 边界：不管量没量成，两头的 MTU 和路由器的桥都要改回去。先改主机这一头：路由器不回话时主机也要回到 1500。
        set_host_mtu(folder, f"{name}-收尾", interface, 1500)
        ask(port, prompt, folder, f"{name}-收尾-路由器{name}的MTU改回1500", f"ip link set {name} mtu 1500", 15)
        router_port_back(port, prompt, folder, f"{name}-收尾", name, target)
    return {"口": name, "主机网卡": interface, "主机网卡自己最大的MTU": host_top,
            "这个模块许设的最大MTU": FRAME_TOP, "这个口另外限的最多量到": limit, "量得到的最大MTU": top,
            "这一遍停下来是因为出了事": trouble, "最大的通的数": low, "最小的不通的数": high,
            "最大能100个回100个的MTU": best, "每个数": readings, "最大能通的数上的TCP": tcp}


def stage_base(port, prompt, args, result):
    """判据 1、2、3、4、12、13。"""
    folder, checks = args.output, []
    errors = ask(port, prompt, folder, "1-这个驱动报错的行数",
                 'dmesg | grep -E "ipq807x-eth|ethernet-switch" | grep -ciE "error|fail|timed out|cannot|warn"; true', 20)
    severe = severe_lines(port, prompt, folder, "1-严重错误的行数")
    names = sorted(ask(port, prompt, folder, "1-中断", "grep -o 'edma_[a-z]*[0-9]*' /proc/interrupts", 15).split("\n"))
    wanted = sorted([f"edma_rxdesc{i}" for i in range(CORES)] + [f"edma_rxfill{i}" for i in range(CORES)]
                    + cmpl_irqs() + ["edma_misc"])
    checks.append({"编号": "1", "看什么": "驱动加载", "通过": errors == "0" and severe == 0 and names == wanted,
                   "报错的行数": errors, "严重错误的行数": severe, "中断": names})

    rings = {}
    for ring in RX_RINGS:
        rings[f"收包环{ring}"] = read_registers(port, prompt, folder, f"2-收包环{ring}-长度", EDMA + 0x3900c + 0x1000 * ring, 1)[0] & 0xffff, \
            read_registers(port, prompt, folder, f"2-收包环{ring}-开关", EDMA + 0x39018 + 0x1000 * ring, 1)[0] & 1
    for ring in FILL_RINGS:
        rings[f"空缓冲区环{ring}"] = read_registers(port, prompt, folder, f"2-空缓冲区环{ring}", EDMA + 0x2900c + 0x1000 * ring, 5)[::4]
        rings[f"空缓冲区环{ring}"] = (rings[f"空缓冲区环{ring}"][0] & 0xffff, rings[f"空缓冲区环{ring}"][1] & 1)
    # 8 到 23 号发包环都读：主机用的那几个长度是 128、开着；主机不用的那几个没有被打开。
    for ring in range(8, 24):
        pair = read_registers(port, prompt, folder, f"2-发包环{ring}", EDMA + 0x100c + 0x1000 * ring, 2)
        rings[f"发包环{ring}"] = (pair[0] & 0xffff, pair[1] & 1)
    rings_ok = (all(rings[f"收包环{r}"] == (2048, 1) for r in RX_RINGS)
                and all(rings[f"空缓冲区环{r}"] == (2048, 1) for r in FILL_RINGS)
                and all(rings[f"发包环{r}"] == (128, 1) for r in tx_rings())
                and all(rings[f"发包环{r}"][1] == 0 for r in range(8, 24) if r not in tx_rings()))
    host_entry = read_registers(port, prompt, folder, "2-队列到环第2项", REG_QID2RID_HOST, 1)[0]
    fill_map = read_registers(port, prompt, folder, "2-收包环用哪个空缓冲区环", REG_RXDESC2FILL, 2)
    cmpl_map = read_registers(port, prompt, folder, "2-发包环用哪个发完环", REG_TXDESC2CMPL, 3)
    fill_ok = all((fill_map[1] >> ((ring % 10) * 3)) & 7 == ring - 8 for ring in RX_RINGS)
    cmpl_ok = all((cmpl_map[ring // 10] >> ((ring % 10) * 3)) & 7 == cmpl_ring_of(ring) for ring in tx_rings())
    rows = {ring: read_registers(port, prompt, folder, f"2-PPE里{ring}号环那一行", REG_RING_ROWS + ring * 0x40, 10)
            for ring in RX_RINGS}
    rows_ok = all(rows[12 + i][0] == (1 << (16 + i) if i < 3 else 0x00f80000) and not any(rows[12 + i][1:])
                  for i in range(CORES))
    counter_switch = read_registers(port, prompt, folder, "2-队列计数的开关", REG_QUEUE_COUNTER_ENABLE, 1)[0]
    # 新驱动没有写 1 到 6 号口"哈希值挑哪条队列"的表，靠的是复位以后是 0。不是 0 的话，主机发给一个口的
    # 同一条流的包会散到这个口的几条队列里。每个口读头四项。
    hash_rows = {profile: [read_registers(port, prompt, folder, f"2-{profile}号口的队列哈希表第{i}项",
                                          0x800000 + 0x30000 + (profile * 256 + i) * 0x10, 1)[0] for i in range(4)]
                 for profile in range(1, 7)}
    # 缓冲管理 15 个口的流控开关：CPU 这一侧的 0 到 7 号该是关着的（开着的话，一个口发不动会拖住所有的口）。
    bm_switch = [word & 1 for word in read_registers(port, prompt, folder, "2-缓冲管理的流控开关", PPE_BM + 0x100, 15)]
    # 服务代码 3、4 的帧从哪条队列起：该是主机的第一条（16 号，值是 0x100）。128 号队列没有人往外取，帧进去就出不来。
    service_rows = {code: read_registers(port, prompt, folder, f"2-服务代码{code}的帧从哪条队列起",
                                         0x800000 + 0x10000 + (2048 + code) * 0x10, 1)[0] for code in (3, 4)}
    checks.append({"编号": "2", "看什么": "寄存器",
                   "通过": rings_ok and host_entry == 0xfffffedc and fill_ok and cmpl_ok and rows_ok
                   and counter_switch & 4 == 4,
                   "缓冲管理15个口的流控开关": bm_switch,
                   "服务代码3和4的帧从哪条队列起（原值）": {code: f"{word:08x}" for code, word in service_rows.items()},
                   "环的长度和开关": {name: list(value) for name, value in rings.items()},
                   "队列到环第2项": f"{host_entry:08x}",
                   "收包环用哪个空缓冲区环的两个寄存器": [f"{value:08x}" for value in fill_map],
                   "发包环用哪个发完环的三个寄存器": [f"{value:08x}" for value in cmpl_map],
                   "PPE里主机四个收包环的四行的第一个字": {ring: f"{row[0]:08x}" for ring, row in rows.items()},
                   "队列计数的开关": f"{counter_switch:08x}",
                   "1到6号口的队列哈希表头四项": hash_rows})

    threads = [line.split() for line in ask(port, prompt, folder, "3-线程和许跑的核心", "th", 20).split("\n")]
    irqs = [line.split() for line in ask(port, prompt, folder, "3-中断和许跑的核心", "iq", 20).split("\n")]
    rx_masks = sorted(int(mask, 16) for name, mask in threads if name.startswith("napi/edma_rx-"))
    tx_masks = sorted(int(mask, 16) for name, mask in threads if name.startswith("napi/edma_tx-"))
    irq_ok = all(int(mask, 16) == 1 << int(name[-1]) for name, mask in irqs if name != "edma_misc")
    checks.append({"编号": "3", "看什么": "线程和绑核",
                   "通过": rx_masks == [1, 2, 4, 8] and tx_masks == [1, 2, 4, 8][:cmpl_count()] and irq_ok
                   and len(irqs) == 9 + cmpl_count(),
                   "线程": threads, "中断": irqs})

    e3.router_addresses(port, prompt, folder, "4", "wan")
    wan_speed = ask(port, prompt, folder, "4-wan的速率", "cat /sys/class/net/wan/speed", 15)
    old, ok = {}, wan_speed == "10000"
    for name, interface, source, target in PATHS:
        small = e3.ping_through(port, prompt, folder, f"4-{name}", name, interface, source, target, 100)
        big = e3.ping_through(port, prompt, folder, f"4-{name}-满长", name, interface, source, target, 100, size=1472)
        old[name] = {"小包": small, "满长": [big["发了"], big["回了"]]}
        ok = ok and (small["发了"], small["回了"]) == (100, 100) and e3.landed(small, 100) \
            and small["主机收帧数涨"] >= 100 and small["主机收错数涨"] == 0 and (big["发了"], big["回了"]) == (100, 100)
    vlan = e3.vlan_check(port, prompt, folder)
    forwarding = e3.no_forwarding(port, prompt, folder)
    fast = e3.ping_through(port, prompt, folder, "4-连着ping", "lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN, 1000, interval=0.002)
    empty = ask(port, prompt, folder, "4-空缓冲区环空了的行数", 'dmesg | grep -c "fill ring .* is empty"; true', 20)
    checks.append({"编号": "4", "看什么": "第三步的东西没有坏（E-0003 的判据 5 到 12、15）",
                   "通过": ok and vlan["通过"] and forwarding["通过"]
                   and (fast["发了"], fast["回了"]) == (1000, 1000) and empty == "0",
                   "wan的速率": wan_speed, "三个口": old, "带VLAN标签的满长帧": vlan, "两个口之间不直接转发": forwarding,
                   "连着ping": [fast["发了"], fast["回了"]], "空缓冲区环空了的行数": empty})

    stats_before = {name: router_stats(port, prompt, folder, f"13-以前-{name}", name) for name in e3.ROUTER_PORTS}
    counts_before = e3.router_counts(port, prompt, folder, "13-以前-五个口的收发包数")
    readings = [link_stats_check(port, prompt, folder, f"12-{name}", name, interface, source, target)
                for name, interface, source, target in PATHS]
    checks.append({"编号": "12", "看什么": "ip -s link 对得上", "通过": all(item["通过"] for item in readings),
                   "每个口": readings})
    stats_after = {name: router_stats(port, prompt, folder, f"13-以后-{name}", name) for name in e3.ROUTER_PORTS}
    counts_after = e3.router_counts(port, prompt, folder, "13-以后-五个口的收发包数")
    bad = {name: [stats_after[name].get(key, 0) - stats_before[name].get(key, 0)
                  for key in ("rx_fcs_error", "rx_align_error")] for name in ("lan2", "lan3", "wan")}
    ring_rise = sum(stats_after["lan2"][f"rx_ring{i}_packets"] - stats_before["lan2"][f"rx_ring{i}_packets"]
                    for i in range(CORES))
    port_rise = sum(counts_after[name][0] - counts_before[name][0] for name in e3.ROUTER_PORTS)
    # 每个口：它经自己的每个发包环发的包数加起来，和这个口发包数涨的对得上。
    sent_gap = {name: sum(stats_after[name][f"tx_ring{i}_packets"] - stats_before[name][f"tx_ring{i}_packets"]
                          for i in range(port_queues(name))) - (counts_after[name][1] - counts_before[name][1])
                for name in ("lan2", "lan3", "wan")}
    checks.append({"编号": "13", "看什么": "ethtool -S",
                   "通过": "rx_unicast" in stats_after["lan2"] and f"tx_ring{port_queues('wan') - 1}_packets" in stats_after["wan"]
                   and not any(any(values) for values in bad.values()) and abs(ring_rise - port_rise) <= 20
                   and all(abs(gap) <= 20 for gap in sent_gap.values()),
                   "每个口经各个发包环发的包数的合计减去这个口发包数涨的": sent_gap,
                   "计数的个数": len(stats_after["lan2"]), "校验错和对齐错涨的": bad,
                   "四个收包环收了多少涨的合计": ring_rise, "五个口收包数涨的合计": port_rise})
    result["判据"] = checks
    result["读数里不对劲的地方"] = ([f"有的口的队列哈希表不是 0：{hash_rows}"] if any(any(row) for row in hash_rows.values()) else [])
    if any(bm_switch[:8]):
        result["读数里不对劲的地方"].append(f"缓冲管理里 CPU 这一侧的流控开着：{bm_switch}")
    if any(word != 0x100 for word in service_rows.values()):
        result["读数里不对劲的地方"].append(f"服务代码 3、4 的帧不是从主机的 16 号队列起：{service_rows}")
    result["要看的数"] = {"缓冲管理15个口的流控开关": bm_switch, "1到6号口的队列哈希表头四项": hash_rows}


def frames_within_mtu(port, prompt, folder, tag):
    """判据 29：lan3 的 MTU 是 1500 时，路由器往主机发带到上限的数据，线上不出现比 1514 字节更长的帧。

    调用的时候 lan3 在桥里、MTU 是 1500，主机 USB 网卡的 MTU 已经改成上限（线上真有长帧的话主机收得下）。
    两种直接的读数：
      - 主机在 USB 网卡上数帧（tcpdump，只取个数，不存帧）：从路由器来的、比 1514 字节长的帧要是 0 个；
        同一段时间里发给路由器的、比 1514 字节长的帧（主机自己的回答）也数一遍，证明这个数法看得见长帧。
      - 路由器 lan3 的 MAC 按帧长分档的发送计数：1519 字节以上那一档不涨，1024 到 1518 那一档涨。
    路由器的 ping 没有"不许分片"的开关时（busybox）：数据被切成不超过 MTU 的片发出去，ping 有回答。
    有这个开关时（不是 busybox）另外带着它再发一遍：内核不肯发，线上一帧都不出，ping 回 0 个。
    """
    kind = ask(port, prompt, folder, f"{tag}-路由器的ping是哪一个程序", "readlink -f $(which ping)", 15)
    runs = [("不带不许分片", f"ping -q -c 10 -W 1 -s {MAX_PING} {USB_ADDRESS}; true")]
    if not kind.endswith("busybox"):
        runs.append(("带不许分片", f"ping -q -c 10 -W 1 -M do -s {MAX_PING} {USB_ADDRESS}; true"))
    readings = []
    for name, command in runs:
        before = router_stats(port, prompt, folder, f"{tag}-{name}-以前-lan3的计数", "lan3")
        counters = [subprocess.Popen(["sudo", "-n", "timeout", "-s", "INT", "25", "tcpdump", "-n", "-i", USB,
                                      "-w", "/dev/null", f"{side} host {ROUTER_USB_SIDE} and greater 1515"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for side in ("src", "dst")]
        try:
            time.sleep(3)
            output = ask(port, prompt, folder, f"{tag}-{name}-路由器ping主机", command, 60)
            captured = [process.communicate(timeout=60)[1] for process in counters]
        finally:
            # 边界：不管量没量成，两个数帧的进程都要收掉。
            for process in counters:
                lab.stop_process(process, 2, 5)
        after = router_stats(port, prompt, folder, f"{tag}-{name}-以后-lan3的计数", "lan3")
        counts = [re.search(r"(\d+) packets captured", text) for text in captured]
        if not all(counts):
            raise RuntimeError(f"{tag}：主机上数帧的 tcpdump 没有给出个数：{captured}")
        found = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
        readings.append({"哪一遍": name, "路由器ping主机": [int(found.group(1)), int(found.group(2))] if found else None,
                         "路由器的ping说了什么": output.split("\n")[-3:],
                         "主机数到从路由器来的比1514字节长的帧": int(counts[0].group(1)),
                         "主机数到发给路由器的比1514字节长的帧": int(counts[1].group(1)),
                         "lan3的MAC发1519字节以上的帧涨的": after["tx_1519_maxbyte"] - before["tx_1519_maxbyte"],
                         "lan3的MAC发1024到1518字节的帧涨的": after["tx_1024_1518byte"] - before["tx_1024_1518byte"]})
    plain = readings[0]
    passed = (plain["主机数到从路由器来的比1514字节长的帧"] == 0 and plain["lan3的MAC发1519字节以上的帧涨的"] == 0
              and plain["主机数到发给路由器的比1514字节长的帧"] > 0 and plain["lan3的MAC发1024到1518字节的帧涨的"] >= 80
              and plain["路由器ping主机"] == [10, 10])
    for extra in readings[1:]:
        passed = (passed and extra["主机数到从路由器来的比1514字节长的帧"] == 0
                  and extra["lan3的MAC发1519字节以上的帧涨的"] == 0 and extra["路由器ping主机"] == [10, 0])
    return {"编号": "29", "看什么": "MTU 管的是最长发多大", "通过": passed, "路由器的ping程序": kind, "每一遍": readings}


def frame_limits(port, prompt, folder, tag):
    """PPE 的帧长表里五个口各一项的第一个字：低 14 位是许收的最长帧，第 16 到 29 位是许发的最长帧。"""
    limits = {}
    for index, name in enumerate(PORT_ORDER):
        word = read_registers(port, prompt, folder, f"{tag}-{name}的帧长", 0x60000 + 0x3000 + (index + 2) * 8, 1)[0]
        limits[name] = [word & 0x3fff, (word >> 16) & 0x3fff]
    return limits


def stage_mtu(port, prompt, args, result):
    """判据 8、9、10、11、29。8 先做。"""
    folder, checks = args.output, []
    e3.router_addresses(port, prompt, folder, "8", "wan")
    first = max_frame_both_ways(port, prompt, folder, "8", "lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE)
    pieces = ask(port, prompt, folder, "8-拆开的帧每一格", 'dmesg | grep "frame over several buffers" | head -n 80; true', 60)
    checks.append({"编号": "8", "看什么": "拆开的帧拼得对", "通过": first["主机ping路由器"] == [100, 100],
                   "主机ping路由器": first["主机ping路由器"], "拆开的帧每一格": pieces.split("\n")[:80]})
    wan = max_frame_both_ways(port, prompt, folder, "9-wan", "wan", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE)
    checks.append({"编号": "9", "看什么": "MTU 到上限，两个方向", "通过": first["通过"] and wan["通过"],
                   "lan3": first, "wan": wan})

    watcher = subprocess.Popen(["sudo", "-n", "ping", "-n", "-q", "-i", "0.01", "-c", "3000", "-W", "1",
                                "-I", ENO2_ADDRESS, ROUTER_LAN], stdout=subprocess.PIPE, text=True)
    # 只看另一个口 ping 不丢，看不出驱动是不是把别的口的帧长也改了：每一次改大、改小以后各读一遍五个口的帧长表。
    limits = {"开头": frame_limits(port, prompt, folder, "10-开头")}
    try:
        for index in range(10):
            ask(port, prompt, folder, f"10-第{index}次-改大", f"ip link set lan3 mtu {MAX_MTU}", 15)
            limits[f"第{index}次改大以后"] = frame_limits(port, prompt, folder, f"10-第{index}次-改大以后")
            ask(port, prompt, folder, f"10-第{index}次-改小", "ip link set lan3 mtu 1500", 15)
            limits[f"第{index}次改小以后"] = frame_limits(port, prompt, folder, f"10-第{index}次-改小以后")
        output, _ = watcher.communicate(timeout=120)
    finally:
        # 边界：不管改没改成，旁边那一路的 ping 都要收掉。
        lab.stop_process(watcher, 2, 5)
    sent, received = e3.parse_ping(output)
    start = limits["开头"]
    others_same = all(reading[name] == start[name] for reading in limits.values() for name in PORT_ORDER if name != "lan3")
    lan3_follows = all(reading["lan3"] == ([MAX_MTU + 22] * 2 if "改大" in moment else [1522, 1522])
                       for moment, reading in limits.items() if moment != "开头")
    checks.append({"编号": "10", "看什么": "改一个口的 MTU 不影响别的口",
                   "通过": sent == received and sent >= 1000 and others_same and lan3_follows,
                   "eno2这一路发了": sent, "回了": received, "别的口的帧长表一直没有变": others_same,
                   "lan3的帧长表跟着改了": lan3_follows,
                   "五个口的帧长表（许收的最长、许发的最长）": {moment: limits[moment] for moment in
                                                   ("开头", "第0次改大以后", "第0次改小以后", "第9次改大以后", "第9次改小以后")}})

    back = host_ping(folder, "11-改回1500以后-满长", USB_ADDRESS, ROUTER_USB_SIDE, 100, 1472, 0.05)
    set_host_mtu(folder, "11", USB, MAX_MTU)
    try:
        too_long = host_ping(folder, "11-改回1500以后-到上限的长帧", USB_ADDRESS, ROUTER_USB_SIDE, 20, MAX_PING, 0.05)
        within = frames_within_mtu(port, prompt, folder, "29")
    finally:
        set_host_mtu(folder, "11", USB, 1500)
    over = lab.serial_command(port, f"ip link set lan3 mtu {MAX_MTU + 1}", prompt, 15)
    under = lab.serial_command(port, "ip link set lan3 mtu 67", prompt, 15)
    now = ask(port, prompt, folder, "11-lan3现在的MTU", "cat /sys/class/net/lan3/mtu", 15)
    # "到上限的长帧回 0 个"那一句作废了（比 MTU 长的帧照收）：回了几个照实记，不算正负。
    checks.append({"编号": "11", "看什么": "MTU 改回去；上下限",
                   "通过": back == (100, 100) and over.exit_code != 0 and under.exit_code != 0 and now == "1500",
                   "满长": list(back), "lan3的MTU是1500时到上限的长帧（照实记，不算正负）": list(too_long),
                   "设成12263的退出码": over.exit_code, "设成67的退出码": under.exit_code, "lan3现在的MTU": now})
    checks.append(within)
    result["判据"] = checks


def spread_reading(port, prompt, folder, tag, flows, reverse):
    """跑 30 秒 iperf3，读队列计数、中断次数、环的计数前后的差。"""
    queues_before = e3.queue_counters(port, prompt, folder, f"{tag}-以前-队列计数")
    irq_before = {name: e3.interrupt_count(port, prompt, folder, f"{tag}-以前-{name}", name)
                  for name in [f"edma_rxdesc{i}" for i in range(CORES)] + cmpl_irqs()}
    stats_before = {name: router_stats(port, prompt, folder, f"{tag}-以前-{name}", name) for name in ("lan2", "lan3", "wan")}
    misc_before = e3.interrupt_count(port, prompt, folder, f"{tag}-以前-edma_misc", "edma_misc")
    steered = steering(port, prompt, folder, f"{tag}-以前-rps_cpus")
    stack_before = stack_cores(port, prompt, folder, f"{tag}-以前-协议栈")
    run = iperf(folder, f"{tag}-iperf3", ROUTER_LAN, ENO2_ADDRESS, 30, flows, reverse=reverse)
    stack_after = stack_cores(port, prompt, folder, f"{tag}-以后-协议栈")
    misc_after = e3.interrupt_count(port, prompt, folder, f"{tag}-以后-edma_misc", "edma_misc")
    time.sleep(2)
    queues_after = e3.queue_counters(port, prompt, folder, f"{tag}-以后-队列计数")
    irq_after = {name: e3.interrupt_count(port, prompt, folder, f"{tag}-以后-{name}", name) for name in irq_before}
    stats_after = {name: router_stats(port, prompt, folder, f"{tag}-以后-{name}", name) for name in stats_before}
    rise = {name: {key: stats_after[name][key] - stats_before[name][key] for key in stats_after[name]
                   if key.startswith(("rx_ring", "tx_ring"))} for name in stats_before}
    return {"iperf3": run, "主机的队列涨的": queue_delta(queues_before, queues_after, HOST_QUEUES),
            "别的CPU队列里涨了的": {q: n for q, n in queue_delta(queues_before, queues_after, OTHER_CPU_QUEUES).items() if n},
            "中断涨的": {name: irq_after[name] - irq_before[name] for name in irq_before}, "环的计数涨的": rise,
            "五个口的rps_cpus": steered,
            "四个核心的协议栈各处理了多少个包": [new - old for old, new in zip(stack_before, stack_after)],
            "edma_misc涨的": misc_after - misc_before}


def spread_positive(many):
    """判据 5 的判法：四条队列、四个收包中断、四个收包环都涨了，别的 CPU 队列一条都没有涨。"""
    return (many["iperf3"]["做完"] and all(n > 0 for n in many["主机的队列涨的"].values())
            and all(many["中断涨的"][f"edma_rxdesc{i}"] > 0 for i in range(CORES))
            and all(many["环的计数涨的"]["lan2"][f"rx_ring{i}_packets"] > 0 for i in range(CORES))
            and not many["别的CPU队列里涨了的"])


def stage_spread(port, prompt, args, result):
    """判据 5、6、7。"""
    folder, checks = args.output, []
    iperf_servers(port, prompt, folder, "5")
    many = spread_reading(port, prompt, folder, "5", FLOWS, False)
    checks.append({"编号": "5", "看什么": "多条流分到四个核心；没有报文进别的队列",
                   "通过": spread_positive(many), **many})
    one = spread_reading(port, prompt, folder, "6", 1, False)
    total = sum(one["主机的队列涨的"].values())
    share = max(one["主机的队列涨的"].values()) / total if total else 0
    checks.append({"编号": "6", "看什么": "同一条流一直在一个核心上", "通过": one["iperf3"]["做完"] and share >= 0.99,
                   "涨得最多的那一条占": round(share, 4), **one})
    positions_before = ring_positions(port, prompt, folder, "7-以前")
    out = spread_reading(port, prompt, folder, "7", FLOWS, True)
    positions_after = ring_positions(port, prompt, folder, "7-以后")
    # 驱动自己数的"这个口的这个环发了多少"看不出包是不是真的进了这个口的环；硬件的位置寄存器看得出。
    moved = [ring for ring in tx_rings() if positions_after[ring] != positions_before[ring]]
    lan2 = [out["环的计数涨的"]["lan2"][f"tx_ring{i}_packets"] for i in range(port_queues("lan2"))]
    others = [out["环的计数涨的"][name][f"tx_ring{i}_packets"] for name in ("lan3", "wan") for i in range(port_queues(name))]
    checks.append({"编号": "7", "看什么": "每条发送队列都在用",
                   "通过": out["iperf3"]["做完"] and all(n > 0 for n in lan2)
                   and all(out["中断涨的"][name] > 0 for name in cmpl_irqs()[:port_queues("lan2")])
                   and all(ring in moved for ring in port_rings("lan2"))
                   and max(others) * 100 <= min(lan2), "lan2每个发包环发了多少": lan2, "硬件的位置动了的发包环": moved,
                   "lan3和wan的发包环发了多少": others, **out})
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    result["判据"] = checks
    # 判据的字面只看"涨了没有"。量到的是不是多条流真的分到了四个核心、两个发包环，另外查，查到的列出来。
    result["读数里不对劲的地方"] = ([f"第 5 条：{item}" for item in load_oddities(many, 1000)]
                             + [f"第 7 条：{item}" for item in load_oddities(out, 1000, lan2)])
    result["要看的数"] = {"第5条每10秒的速率": many["iperf3"]["每10秒的速率"],
                       "第5条四个核心的协议栈各处理了多少个包": many["四个核心的协议栈各处理了多少个包"],
                       "第7条每10秒的速率": out["iperf3"]["每10秒的速率"], "第7条lan2每个发包环发了多少": lan2}


def toggle(port, prompt, folder, tag, name, up):
    """关掉或者打开路由器的一个口。打开以后等它连上。"""
    ask(port, prompt, folder, f"{tag}-{'打开' if up else '关掉'}{name}", f"ip link set {name} {'up' if up else 'down'}", 20)
    if not up:
        return
    for _ in range(40):
        if lab.serial_command(port, f"cat /sys/class/net/{name}/carrier", prompt, 15).output.strip() == "1":
            time.sleep(3)
            return
        time.sleep(1)
    raise RuntimeError(f"{tag}：{name} 打开以后 40 秒里没有连上")


def stage_updown(port, prompt, args, result):
    """判据 14、15、16。"""
    folder, checks = args.output, []
    e3.router_addresses(port, prompt, folder, "14", "wan")
    severe_before = severe_lines(port, prompt, folder, "14-以前-警告和致命错误的行数")
    rounds = {}
    for name, interface, source, target in PATHS:
        other = PATHS[1] if name != "lan3" else PATHS[0]
        closed, others, opened = [], [], []
        for index in range(20):
            tag = f"14-{name}-第{index}遍"
            toggle(port, prompt, folder, tag, name, False)
            if source == BRIDGE_ADDRESS:
                e3.ensure_bridge_address(folder, tag)
            closed.append(host_ping(folder, f"{tag}-关着时ping这个口", source, target, 5, None, 0.2)[1])
            others.append(host_ping(folder, f"{tag}-关着时ping另一个口", other[2], other[3], 100, None, 0.01)[1])
            toggle(port, prompt, folder, tag, name, True)
            if source == BRIDGE_ADDRESS:
                e3.ensure_bridge_address(folder, tag + "-打开以后")
            opened.append(host_ping(folder, f"{tag}-打开以后ping这个口", source, target, 100, None, 0.01)[1])
        rounds[name] = {"关着时这个口回了": closed, "关着时另一个口回了": others, "打开以后这个口回了": opened}
    severe_after = severe_lines(port, prompt, folder, "14-以后-警告和致命错误的行数")
    checks.append({"编号": "14", "看什么": "关口再开",
                   "通过": severe_after == severe_before and all(
                       not any(item["关着时这个口回了"]) and all(n == 100 for n in item["关着时另一个口回了"])
                       and all(n == 100 for n in item["打开以后这个口回了"]) for item in rounds.values()),
                   "警告和致命错误的行数": [severe_before, severe_after], "每个口": rounds})

    iperf_servers(port, prompt, folder, "15")
    watcher = subprocess.Popen(["sudo", "-n", "ping", "-n", "-q", "-i", "0.01", "-c", "9000", "-W", "1",
                                "-I", USB_ADDRESS, ROUTER_USB_SIDE], stdout=subprocess.PIPE, text=True)
    flow = subprocess.Popen(iperf_argv(ROUTER_LAN, ENO2_ADDRESS, 80, FLOWS), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        for index in range(5):
            toggle(port, prompt, folder, f"15-第{index}遍", "lan2", False)
            time.sleep(10)
            toggle(port, prompt, folder, f"15-第{index}遍", "lan2", True)
        output, _ = watcher.communicate(timeout=180)
    finally:
        # 边界：两个在后面跑着的进程都要收掉。
        lab.stop_process(flow, 2, 5)
        lab.stop_process(watcher, 2, 5)
    sent, received = e3.parse_ping(output)
    iperf_servers(port, prompt, folder, "15-再起")
    again = iperf(folder, "15-最后一遍打开以后再发30秒", ROUTER_LAN, ENO2_ADDRESS, 30, FLOWS)
    severe_end = severe_lines(port, prompt, folder, "15-以后-警告和致命错误的行数")
    checks.append({"编号": "15", "看什么": "流量跑着的时候关口再开",
                   "通过": severe_end == severe_after and again["做完"] and again["一共字节"] > 0
                   and (sent - received) * 100 <= sent,
                   "警告和致命错误的行数": [severe_after, severe_end], "USB网卡这一路发了": sent, "回了": received,
                   "最后一遍打开以后再发": again})
    ask(port, prompt, folder, "16-停iperf3", "killall iperf3 2>/dev/null; sleep 3; true", 20)
    inflight = ask(port, prompt, folder, "16-每条发送队列压着多少字节",
                   "cat /sys/class/net/*/queues/tx-*/byte_queue_limits/inflight | sort -u", 20)
    dql = ask(port, prompt, folder, "16-记账对不上的行数", 'dmesg | grep -ci "dql"; true', 20)
    checks.append({"编号": "16", "看什么": "发送队列上的记账", "通过": inflight == "0" and dql == "0",
                   "压着的字节数里出现过的值": inflight.split("\n"), "内核记录里带dql的行数": dql})
    result["判据"] = checks


# 不限速地发 UDP 时每个包带的数据。第 2 版试跑读到：用 1500 字节的包把千兆线发满，路由器一个不丢全收下了，
# 没有堵。包小了每秒的帧数多得多，路由器才收不过来。
FLOOD_LENGTH = 64


def flood(port, prompt, folder, tag, seconds, sample_queues=False):
    """主机从 eno2 用 64 条 UDP 流不限速地发 seconds 秒，每个包带 64 字节的数据。

    sample_queues 是真的时候，每 10 秒读一回主机的四条队列的计数，返回这些读数。
    """
    run = subprocess.Popen(iperf_argv(ROUTER_LAN, ENO2_ADDRESS, seconds, FLOWS, udp=True, length=FLOOD_LENGTH),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = []
    started = time.monotonic()
    try:
        # 每 10 秒读一回，最后一回在流量停下以前 5 秒读。
        for index in range(seconds // 10):
            moment = started + min(10 * (index + 1), seconds - 5)
            time.sleep(max(0, moment - time.monotonic()))
            if sample_queues:
                samples.append(host_queue_counts(port, prompt, folder, f"{tag}-第{index}个10秒-主机的队列"))
        run.wait(timeout=60)
    finally:
        # 边界：发流量的进程不管怎样都要收掉。
        lab.stop_process(run, 2, 5)
    return samples


def pause_shown(text):
    """ethtool -a 的输出里，显示的收、发（RX、TX）和协商出来的收、发（RX negotiated、TX negotiated）。"""
    found = dict(re.findall(r"^(RX|TX|RX negotiated|TX negotiated):\s*(on|off)", text, re.M))
    return {"显示的收发": [found.get("RX"), found.get("TX")],
            "协商出来的收发": [found.get("RX negotiated"), found.get("TX negotiated")]}


def stage_pause(port, prompt, args, result):
    """判据 17。"""
    folder, readings = args.output, {}
    shown = ask(port, prompt, folder, "17-ethtool-a", "ethtool -a lan2", 20)
    iperf_servers(port, prompt, folder, "17")
    # 第一遍什么都不设，量的是这时候的样子（刚加载、没有人设过时是"照协商"）：协商出来路由器可以发暂停帧，
    # 它就该真的发。ethtool -a 显示的是"设成了什么"，不是协商的结果，光看显示看不出路由器照没照协商办。
    for label, command in (("量以前的样子", None), ("发暂停帧开着", "ethtool -A lan2 tx on; true"),
                           ("发暂停帧关掉", "ethtool -A lan2 tx off; true")):
        if command:
            ask(port, prompt, folder, f"17-{label}", command, 30)
            time.sleep(8)
        now = ask(port, prompt, folder, f"17-{label}-ethtool-a", "ethtool -a lan2", 20)
        before = router_stats(port, prompt, folder, f"17-{label}-以前", "lan2")
        host_before, sent_before = host_pause_frames(), e3.host_stat("eno2", "tx_packets")
        flood(port, prompt, folder, f"17-{label}", 60)
        time.sleep(2)
        after = router_stats(port, prompt, folder, f"17-{label}-以后", "lan2")
        host_after, sent_after = host_pause_frames(), e3.host_stat("eno2", "tx_packets")
        sent = sent_after - sent_before
        arrived = after["rx_unicast"] - before["rx_unicast"]
        taken = sum(after[f"rx_ring{i}_packets"] - before[f"rx_ring{i}_packets"] for i in range(CORES))
        readings[label] = {"这时ethtool -a lan2": now.split("\n"), **pause_shown(now),
                           "路由器lan2发出的暂停帧涨": after["tx_pause"] - before["tx_pause"],
                           "主机eno2收到的暂停帧涨": host_after - host_before,
                           "主机eno2发的帧涨": sent, "lan2收到的单播帧涨": arrived, "四个收包环收了多少涨的合计": taken,
                           "丢在主机到lan2的MAC之间的": sent - arrived, "丢在lan2的MAC到收包环之间的": arrived - taken,
                           "四个收包环拿不到缓冲丢掉的涨的合计": sum(after[f"rx_ring{i}_no_buffer"] - before[f"rx_ring{i}_no_buffer"]
                                                     for i in range(CORES))}
    # 开工时 lan2 协商出来的是收、发暂停帧都开着；改回这个样子。
    ask(port, prompt, folder, "17-改回收发都开着", "ethtool -A lan2 rx on tx on; true", 30)
    time.sleep(8)
    readings["做完以后ethtool -a lan2"] = ask(port, prompt, folder, "17-做完以后-ethtool-a", "ethtool -a lan2", 20).split("\n")
    ask(port, prompt, folder, "17-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    on, off = readings["发暂停帧开着"], readings["发暂停帧关掉"]
    start = readings["量以前的样子"]
    # ethtool -a 的"RX、TX"两栏是"设成了什么"，"RX negotiated、TX negotiated"两栏才是协商出来的结果。
    # 没有人设过的时候，前两栏是 off，路由器照协商的结果办：所以这一遍比的是"协商出来许不许发"和"真的发没发"。
    # 手工设过以后，两样都看：显示的设置和协商出来的一样；设成发就真的发，设成不发就真的不发。
    follows = {"量以前": (start["协商出来的收发"][1] == "on") == (start["路由器lan2发出的暂停帧涨"] > 0)
               and (start["协商出来的收发"][1] == "on") == (start["主机eno2收到的暂停帧涨"] > 0),
               "发暂停帧开着": on["路由器lan2发出的暂停帧涨"] > 0 and on["主机eno2收到的暂停帧涨"] > 0,
               "发暂停帧关掉": off["路由器lan2发出的暂停帧涨"] == 0 and off["主机eno2收到的暂停帧涨"] == 0}
    same = {"发暂停帧开着": on["显示的收发"] == on["协商出来的收发"],
            "发暂停帧关掉": off["显示的收发"] == off["协商出来的收发"]}
    result["判据"] = [{"编号": "17", "看什么": "流控",
                     "通过": all(follows.values()) and all(same.values()),
                     "路由器照协商出来的或者设进去的办了没有": follows,
                     "手工设过以后显示的和协商出来的一致不一致": same,
                     "ethtool -a lan2": shown.split("\n"), **readings}]
    result["要看的数"] = {"路由器照协商出来的或者设进去的办了没有": follows,
                       "手工设过以后显示的和协商出来的一致不一致": same,
                       "量以前显示的收发": start["显示的收发"], "量以前协商出来的收发": start["协商出来的收发"],
                       "量以前路由器发出的暂停帧涨": start["路由器lan2发出的暂停帧涨"],
                       "开着时路由器发出的暂停帧涨": on["路由器lan2发出的暂停帧涨"],
                       "关掉时路由器发出的暂停帧涨": off["路由器lan2发出的暂停帧涨"]}


def rebind(port, prompt, folder, tag):
    """卸载加载以后线程是新建的：重新跑一遍启动脚本把线程和中断绑到核心上，读回来。"""
    ask(port, prompt, folder, f"{tag}-重新跑启动脚本", "/etc/init.d/smp_affinity boot; sleep 6", 40)
    return {"线程": ask(port, prompt, folder, f"{tag}-线程和许跑的核心", "th", 20).split("\n"),
            "中断": ask(port, prompt, folder, f"{tag}-中断和许跑的核心", "iq", 20).split("\n")}


def stage_load(port, prompt, args, result):
    """换模块（E-0003 的办法），换完重新绑核。"""
    e3.stage_load(port, prompt, args, result)
    result["重新绑核以后"] = rebind(port, prompt, args.output, "换模块以后")


def stage_reload(port, prompt, args, result):
    """判据 18。"""
    folder, rounds = args.output, []
    for index in range(5):
        rounds.append(e3.switch_module(port, prompt, folder, f"18-第{index}遍", ["modprobe ipq807x-eth"]))
    bound = rebind(port, prompt, folder, "18")
    iperf_servers(port, prompt, folder, "18")
    many = spread_reading(port, prompt, folder, "18", FLOWS, False)
    ask(port, prompt, folder, "18-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    # 卸载时页池里有页收不回来，内核会记"stalled pool shutdown"，它不带 WARNING 字样，上面数不到。
    pool_lines = ask(port, prompt, folder, "18-内核记录里页池收不回来的行数", 'dmesg | grep -ciE "stalled pool|page_pool"; true', 20)
    result["判据"] = [{"编号": "18", "看什么": "卸载再加载",
                     "通过": all(item["新增的警告和致命错误的行数"] == "0" for item in rounds)
                     and len({item["tainted"] for item in rounds}) == 1
                     and spread_positive(many) and pool_lines == "0",
                     "内核记录里页池收不回来的行数": pool_lines,
                     "五遍": rounds, "重新绑核以后": bound, "最后一遍以后的判据5": many}]
    result["读数里不对劲的地方"] = [f"最后一遍以后的第 5 条：{item}" for item in load_oddities(many, 1000)]
    result["要看的数"] = {"每10秒的速率": many["iperf3"]["每10秒的速率"],
                       "四个核心的协议栈各处理了多少个包": many["四个核心的协议栈各处理了多少个包"]}


def stage_overload(port, prompt, args, result):
    """判据 19。"""
    folder = args.output
    iperf_servers(port, prompt, folder, "19")
    severe_before = severe_lines(port, prompt, folder, "19-以前-警告和致命错误的行数")
    readings = [host_queue_counts(port, prompt, folder, "19-以前-主机的队列")]
    readings += flood(port, prompt, folder, "19", 300, sample_queues=True)
    samples = [queue_delta(before, after, HOST_QUEUES) for before, after in zip(readings, readings[1:])]
    time.sleep(10)
    after = host_ping(folder, "19-停了10秒以后ping", ENO2_ADDRESS, ROUTER_LAN, 100, None, 0.01)
    severe_after = severe_lines(port, prompt, folder, "19-以后-警告和致命错误的行数")
    end = e3.queue_counters(port, prompt, folder, "19-以后-队列计数")
    ask(port, prompt, folder, "19-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    result["判据"] = [{"编号": "19", "看什么": "过载的时候不卡死，四条队列都在出",
                     "通过": all(all(n > 0 for n in rise.values()) for rise in samples) and len(samples) == 30
                     and after == (100, 100) and severe_after == severe_before,
                     "每10秒四条队列涨的": samples, "停了以后ping": list(after),
                     "警告和致命错误的行数": [severe_before, severe_after],
                     "别的CPU队列的计数里不是0的": {q: end[q] for q in OTHER_CPU_QUEUES if end[q]}}]


def fill_levels(port, prompt, folder, tag):
    """四个空缓冲区环里各有多少个缓冲（登记的位置减硬件取走的位置）。"""
    levels = []
    for ring in FILL_RINGS:
        prod, cons = read_registers(port, prompt, folder, f"{tag}-空缓冲区环{ring}的位置", EDMA + 0x29004 + 0x1000 * ring, 2)
        levels.append((prod - cons) & 2047)
    return levels


def stage_buffer(port, prompt, args, result):
    """判据 20、21。要先换上缓冲实验的模块。"""
    folder, checks = args.output, []
    param = "/sys/module/ipq807x_eth/parameters"
    levels_before = fill_levels(port, prompt, folder, "20-以前")
    before = router_stats(port, prompt, folder, "20-以前-lan2", "lan2")
    dropped_before = router_number(port, prompt, folder, "20-以前-lan2的丢包数", "cat /sys/class/net/lan2/statistics/rx_dropped")
    # 开关开着时路由器一个帧都收不下，也就不回主机问它地址的那种包；主机认不得它的地址以后 ping 会提前停，
    # 30 秒里就只发得出几个包。所以先 ping 通一次，把主机记着的这一条地址暂时定住，量完放开。命令里不带地址的数值。
    host_ping(folder, "20-以前-先ping通", ENO2_ADDRESS, ROUTER_LAN, 3, None, 0.2)
    pinned = e3.host(["sudo", "-n", "ip", "neigh", "change", ROUTER_LAN, "dev", "eno2", "nud", "permanent"],
                     folder, "20-主机记着的地址暂时定住")[0] == 0
    ask(port, prompt, folder, "20-打开开关", f"echo 1 > {param}/exp_fail_alloc", 15)
    try:
        during = host_ping(folder, "20-开关开着的30秒", ENO2_ADDRESS, ROUTER_LAN, 3000, None, 0.01, deadline=30)
        levels_during = fill_levels(port, prompt, folder, "20-开着")
    finally:
        # 边界：开关开着的时候路由器一个帧都收不下，不管量没量成都要关掉；主机定住的那一条地址也要放开。
        lab.serial_command(port, f"echo 0 > {param}/exp_fail_alloc", prompt, 15)
        e3.host(["sudo", "-n", "ip", "neigh", "change", ROUTER_LAN, "dev", "eno2", "nud", "reachable"],
                folder, "20-主机记着的地址放开")
    switch = ask(port, prompt, folder, "20-关掉开关以后读回", f"cat {param}/exp_fail_alloc", 15)
    time.sleep(10)
    after_ping = host_ping(folder, "20-关掉10秒以后ping", ENO2_ADDRESS, ROUTER_LAN, 100, None, 0.01)
    after = router_stats(port, prompt, folder, "20-以后-lan2", "lan2")
    dropped_after = router_number(port, prompt, folder, "20-以后-lan2的丢包数", "cat /sys/class/net/lan2/statistics/rx_dropped")
    levels_after = fill_levels(port, prompt, folder, "20-以后")
    empty = ask(port, prompt, folder, "20-空缓冲区环空了的行数", 'dmesg | grep -c "fill ring .* is empty"; true', 20)
    checks.append({"编号": "20", "看什么": "缓冲的实验甲：新的页拿不到",
                   "通过": after_ping == (100, 100) and levels_after == levels_before,
                   "开关开着时ping发了和回了": list(during), "主机记着的地址定住了没有": pinned,
                   "lan2的丢包数涨": dropped_after - dropped_before,
                   "拿不到缓冲丢掉的涨": sum(after[f"rx_ring{i}_no_buffer"] - before[f"rx_ring{i}_no_buffer"] for i in range(CORES)),
                   "四个空缓冲区环里的缓冲数": {"以前": levels_before, "开着时": levels_during, "以后": levels_after},
                   "空缓冲区环空了的行数": empty, "关掉开关以后读回": switch, "关掉以后ping": list(after_ping)})

    iperf_servers(port, prompt, folder, "21")
    ask(port, prompt, folder, "21-打开开关", f"echo 0 > {param}/exp_starve_ring", 15)
    run = subprocess.Popen(iperf_argv(ROUTER_LAN, ENO2_ADDRESS, 60, FLOWS), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = []
    try:
        for index in range(6):
            time.sleep(10)
            stats = router_stats(port, prompt, folder, f"21-第{index}个10秒-lan2", "lan2")
            samples.append({"四个收包环收了多少": [stats[f"rx_ring{i}_packets"] for i in range(CORES)],
                            "四个空缓冲区环里的缓冲数": fill_levels(port, prompt, folder, f"21-第{index}个10秒"),
                            "ping回了": host_ping(folder, f"21-第{index}个10秒-ping", USB_ADDRESS, ROUTER_USB_SIDE, 20, None, 0.05,
                                                deadline=10)[1]})
    finally:
        # 边界：发流量的进程要收掉，开关要关掉。
        lab.stop_process(run, 2, 5)
        lab.serial_command(port, f"echo -1 > {param}/exp_starve_ring", prompt, 15)
    time.sleep(10)
    iperf_servers(port, prompt, folder, "21-再起")
    again = iperf(folder, "21-关掉以后再发10秒", ROUTER_LAN, ENO2_ADDRESS, 10, FLOWS)
    end = router_stats(port, prompt, folder, "21-关掉以后-lan2", "lan2")
    after_ping = host_ping(folder, "21-关掉以后ping", ENO2_ADDRESS, ROUTER_LAN, 100, None, 0.01)
    recovered = after_ping == (100, 100) and all(
        end[f"rx_ring{i}_packets"] > samples[-1]["四个收包环收了多少"][i] for i in range(CORES))
    empty = ask(port, prompt, folder, "21-空缓冲区环空了的行数", 'dmesg | grep -c "fill ring .* is empty"; true', 20)
    misc = ask(port, prompt, folder, "21-硬件报错的行", 'dmesg | grep "EDMA: " | tail -n 5; true', 20)
    ask(port, prompt, folder, "21-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    result["只报数"] = [{"编号": "21", "看什么": "缓冲的实验乙：一个空缓冲区环真的空一次", "读够了六个10秒": len(samples) == 6,
                   "每10秒": samples, "关掉以后": {"ping": list(after_ping), "再发10秒": again,
                                           "四个收包环收了多少": [end[f"rx_ring{i}_packets"] for i in range(CORES)]},
                   "补上以后恢复了": recovered, "空缓冲区环空了的行数": empty, "硬件报错的行": misc.split("\n")}]
    result["判据"] = checks
    result["读数里不对劲的地方"] = [] if recovered else ["第 21 条：关掉开关以后没有恢复"]
    if during[0] < 1000:
        result["读数里不对劲的地方"].append(f"第 20 条：开关开着的 30 秒里主机只发出去 {during[0]} 个包，路由器没有真的被压到")
    result["要看的数"] = {"第20条开关开着时ping发了和回了": list(during),
                       "第20条拿不到缓冲丢掉的涨": checks[0]["拿不到缓冲丢掉的涨"], "第21条补上以后恢复了": recovered}


def cpu_busy(before, after):
    """四个核心各忙到百分之几。before、after 是路由器上 cpu 那个小函数的输出。"""
    busy = []
    for old, new in zip(before.split("\n"), after.split("\n")):
        a, b = [int(x) for x in old.split()[1:]], [int(x) for x in new.split()[1:]]
        total = sum(b) - sum(a)
        busy.append(round(100 * (total - (b[3] - a[3]) - (b[4] - a[4])) / total, 1) if total else None)
    return busy


def host_counter(interface, item):
    """主机一块网卡 statistics/ 下面的一个数；这块网卡这时不在（放进了网络命名空间）或者没有这一项，返回 None。"""
    path = f"/sys/class/net/{interface}/statistics/{item}"
    if not os.path.exists(path):
        return None
    with open(path) as file:
        return int(file.read())


# 要看的 TCP 计数。发的一头：一共重传了多少段、是哪一种重传、对面说"这一段我已经收过了"多少次；
# 收的一头：收到次序不对的段多少、回"这一段我已经收过了"多少次。
TCP_COUNTERS = ("RetransSegs", "TCPFastRetrans", "TCPSlowStartRetrans", "TCPLostRetransmit", "TCPTimeouts", "TCPLossProbes",
                "TCPSpuriousRTOs", "TCPSpuriousRtxHostQueues", "TCPDSACKRecv", "TCPSACKReorder", "TCPTSReorder",
                "TCPOFOQueue", "TCPDSACKOldSent", "TCPDSACKOfoSent")


def tcp_table(text):
    """把"名字一行、数一行"的 TCP 计数读成 {名字: 数}，只留 TCP_COUNTERS 里的。"""
    table = {}
    lines = [line.split()[1:] for line in text.split("\n") if line.strip()]
    for names, values in zip(lines[::2], lines[1::2]):
        table.update(zip(names, values))
    return {name: int(table.get(name, 0)) for name in TCP_COUNTERS}


def host_tcp_counters(netns=None):
    """主机内核的 TCP 计数（/proc/net/snmp 和 /proc/net/netstat）。netns 给了就读那个网络命名空间里的。"""
    argv = ["grep", "-hE", "^(Tcp|TcpExt):", "/proc/net/snmp", "/proc/net/netstat"]
    if netns:
        argv = ["sudo", "-n", "ip", "netns", "exec", netns] + argv
    return tcp_table(subprocess.run(argv, capture_output=True, text=True).stdout)


def tcp_rise(before, after):
    """TCP 计数涨了多少，只留涨了的。"""
    return {name: after[name] - value for name, value in before.items() if after[name] != value}


def every_counter(text):
    """把一段计数的文字读成 {名字: 数}。认三种写法：名字一行、数一行（/proc/net/netstat、snmp）；
    "名字: 数"（ethtool -S）；每个核心一行十六进制（/proc/net/softnet_stat，只取前三栏）。"""
    table, lines = {}, [line for line in text.split("\n") if line.strip()]
    index = 0
    while index < len(lines):
        fields = lines[index].split()
        following = lines[index + 1].split() if index + 1 < len(lines) else []
        if fields[0].endswith(":") and following and following[0] == fields[0] and len(fields) > 2 \
                and not fields[1].lstrip("-").isdigit():
            for name, value in zip(fields[1:], following[1:]):
                table[fields[0] + name] = int(value)
            index += 2
            continue
        if len(fields) == 2 and fields[0].endswith(":") and fields[1].lstrip("-").isdigit():
            table[fields[0][:-1]] = int(fields[1])
        elif len(fields) >= 3 and all(len(field) == 8 for field in fields[:3]):
            row = sum(1 for key in table if key.startswith("软中断核心")) // 3
            for name, value in zip(("处理了", "丢了", "没轮完"), fields[:3]):
                table[f"软中断核心{row}{name}"] = int(value, 16)
        index += 1
    return table


def diagnostics(port, prompt, folder, tag):
    """发包前后各存一份：路由器和主机两头内核的全部网络计数、路由器 wan 的发送队列、主机 eno1 网卡的全部计数。"""
    router = ask(port, prompt, folder, f"诊断-{tag}-路由器的计数",
                 "cat /proc/net/netstat /proc/net/snmp /proc/net/softnet_stat", 30)
    qdisc = ask(port, prompt, folder, f"诊断-{tag}-路由器wan的发送队列", "tc -s qdisc show dev wan", 20)
    host = e3.host(["cat", "/proc/net/netstat", "/proc/net/snmp", "/proc/net/softnet_stat"], folder, f"诊断-{tag}-主机的计数")[1]
    nic = e3.host(["ethtool", "-S", "eno1"], folder, f"诊断-{tag}-主机eno1的计数")[1]
    return {"路由器": every_counter(router), "主机": every_counter(host), "主机eno1": every_counter(nic),
            "路由器wan的发送队列": qdisc.split("\n")}


def diagnostics_rise(before, after, least=1000):
    """两次 diagnostics 之间涨了 least 以上的计数。"""
    rise = {side: {name: after[side][name] - value for name, value in before[side].items()
                   if name in after[side] and after[side][name] - value >= least}
            for side in ("路由器", "主机", "主机eno1")}
    rise["路由器wan的发送队列"] = {"以前": before["路由器wan的发送队列"], "以后": after["路由器wan的发送队列"]}
    return rise


def snapshot(port, prompt, folder, tag):
    """发流量以前、以后各读一次的一组数：核心的时间、收发线程用的时间、协议栈、中断、三个口的计数、发送丢的、主机网卡的计数。"""
    cores = [[int(x) for x in line.split()[1:]] for line in ask(port, prompt, folder, f"{tag}-核心", "cpu", 15).split("\n")]
    threads = {}
    for line in ask(port, prompt, folder, f"{tag}-线程", "tt", 20).split("\n"):
        fields = line.split()
        if len(fields) == 4:
            threads[f"{fields[0]}（进程{fields[1]}）"] = int(fields[2]) + int(fields[3])
    irqs = {}
    for line in ask(port, prompt, folder, f"{tag}-中断", "iq2", 15).split("\n"):
        fields = line.split()
        if len(fields) == 2:
            irqs[fields[0]] = int(fields[1])
    drops = {}
    for line in ask(port, prompt, folder, f"{tag}-发送丢的", "qd", 20).split("\n"):
        fields = line.split()
        drops[fields[0]] = (int(fields[2]), int(fields[3]))
    return {"核心": cores, "线程": threads, "协议栈": stack_cores(port, prompt, folder, f"{tag}-协议栈"), "中断": irqs,
            "路由器的TCP": tcp_table(ask(port, prompt, folder, f"{tag}-路由器的TCP计数", "ns", 20)),
            "口": {name: router_stats(port, prompt, folder, f"{tag}-{name}", name) for name in ("lan2", "lan3", "wan")},
            "丢的": drops,
            "主机": {nic: {item: host_counter(nic, item) for item in ("rx_packets", "rx_bytes", "rx_dropped", "rx_errors",
                                                                      "rx_missed_errors", "tx_packets")}
                    for nic in ("eno1", "eno2")}}


def snapshot_delta(before, after):
    """两次 snapshot 之间涨了多少。"""
    cores = []
    for old, new in zip(before["核心"], after["核心"]):
        rise = [b - a for a, b in zip(old, new)]
        total = sum(rise) or 1
        cores.append({"用户程序": round(100 * (rise[0] + rise[1]) / total, 1), "内核": round(100 * rise[2] / total, 1),
                      "软中断": round(100 * (rise[5] + rise[6]) / total, 1), "空闲": round(100 * (rise[3] + rise[4]) / total, 1)})
    threads = {name: round((after["线程"][name] - ticks) / 100, 2) for name, ticks in before["线程"].items()
               if name in after["线程"] and after["线程"][name] != ticks}
    ports = {}
    for name, old in before["口"].items():
        new = after["口"][name]
        ports[name] = {
            "MAC发出去的帧": sum(new.get(k, 0) - old.get(k, 0) for k in ("tx_unicast", "tx_multicast", "tx_broadcast")),
            "MAC发出去的字节": new.get("tx_bytes", 0) - old.get("tx_bytes", 0),
            "MAC收到的帧": sum(new.get(k, 0) - old.get(k, 0) for k in ("rx_unicast", "rx_multicast", "rx_broadcast")),
            "收到的暂停帧": new.get("rx_pause", 0) - old.get("rx_pause", 0),
            "发出的暂停帧": new.get("tx_pause", 0) - old.get("tx_pause", 0),
            "每个发包环发了多少": [new[f"tx_ring{i}_packets"] - old[f"tx_ring{i}_packets"] for i in range(port_queues(name))],
            "每条发送队列被停了多少次": [new[f"tx_ring{i}_stopped"] - old[f"tx_ring{i}_stopped"] for i in range(port_queues(name))],
            "发送队列丢的": after["丢的"][name][0] - before["丢的"][name][0],
            "口自己记的发送丢的": after["丢的"][name][1] - before["丢的"][name][1]}
    host = {nic: {item: (after["主机"][nic][item] - value if value is not None and after["主机"][nic][item] is not None else None)
                  for item, value in items.items()} for nic, items in before["主机"].items()}
    return {"四个核心各忙在哪里（百分之几）": cores, "线程各用了多少秒的核心时间": threads,
            "四个核心的协议栈各处理了多少个包": [b - a for a, b in zip(before["协议栈"], after["协议栈"])],
            "四个收包环收了多少": [after["口"]["lan2"][f"rx_ring{i}_packets"] - before["口"]["lan2"][f"rx_ring{i}_packets"]
                             for i in range(CORES)],
            "中断涨的": {name: after["中断"].get(name, 0) - count for name, count in before["中断"].items()},
            "edma_misc涨的": after["中断"].get("edma_misc", 0) - before["中断"].get("edma_misc", 0),
            "路由器自己的TCP计数涨的": tcp_rise(before["路由器的TCP"], after["路由器的TCP"]),
            "路由器的口": ports, "主机的网卡": host}


def sending_oddities(reading, out_port, port_speed, many_flows):
    """路由器发或者转发的一遍读完以后，查这一遍是不是真的压到了发包环上。返回不对劲的地方，一处一句。

    out_port 是包从路由器的哪个口出去。many_flows 是假的（只有一条流）时，不查"每个环都在用"那两样。
    """
    sent = reading["路由器的口"][out_port]["每个发包环发了多少"]
    odd = load_oddities(reading, port_speed, sent if many_flows else None)
    if not many_flows:
        odd = [item for item in odd if "协议栈" not in item]
    stopped = reading["路由器的口"][out_port]["每条发送队列被停了多少次"]
    if many_flows and not all(stopped):
        odd.append(f"{out_port} 有的发送队列一次都没有因为环满被停过：{stopped}")
    dropped = reading["路由器的口"][out_port]["发送队列丢的"]
    if dropped > reading["路由器的口"][out_port]["MAC发出去的帧"]:
        odd.append(f"{out_port} 的发送队列丢了 {dropped} 个，比发出去的帧还多")
    return odd


def stage_txrate(port, prompt, args, result):
    """路由器自己往 wan 发 30 秒，只报数。默认是 64 条 TCP 流、不限速。

    --flows 改流的条数；--main udp 加 --rate 是每条流限速的 UDP（只有一条流、看乱序时用）。
    --side tcp（或者 --with-lan2）：同时用另一组 TCP 流往 lan2 发；--side udp 加 --side-rate：同时往 lan2 发限速的 UDP。

    不用不限速的 UDP：路由器自己发 UDP 时，Linux 的发送队列满了就丢，发的程序不会被堵住，四个核心被它空转占满，
    收"发完"的线程轮不上，量到的是线程多久轮到一次，不是发包环。TCP 发不出去时发的程序会停下来等。
    主机回确认跟不跟得上，看主机这一头的 iperf3 忙到百分之几。
    """
    folder = args.output
    side_kind = "tcp" if args.with_lan2 else args.side
    e3.router_addresses(port, prompt, folder, "发包", "wan")
    e3.ensure_bridge_address(folder, "发包")
    firewall_open(port, prompt, folder, "发包", "wan")
    iperf_servers(port, prompt, folder, "发包", (5201, 5202))
    speed = ask(port, prompt, folder, "wan的速率", "cat /sys/class/net/wan/speed", 15)
    side = None
    if side_kind != "none":
        side = subprocess.Popen(iperf_argv(ROUTER_LAN, ENO2_ADDRESS, 60, args.side_flows, udp=side_kind == "udp", reverse=True,
                                           server_port=5202, rate=args.side_rate),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(5)
    steered = steering(port, prompt, folder, "以前-rps_cpus")
    tcp_before = host_tcp_counters()
    diag_before = diagnostics(port, prompt, folder, "以前") if args.diag else None
    before = snapshot(port, prompt, folder, "以前")
    started = time.monotonic()
    side_report = None
    try:
        run = iperf(folder, "往wan发", ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, 30, args.flows, udp=args.main == "udp",
                    reverse=True, rate=args.rate)
        after = snapshot(port, prompt, folder, "以后")
        if args.diag:
            result["诊断：前后涨了一千以上的计数"] = diagnostics_rise(diag_before, diagnostics(port, prompt, folder, "以后"))
        seconds = time.monotonic() - started
        tcp_after = host_tcp_counters()
        if side:
            side_report = iperf_report(side.communicate(timeout=90)[0])
    finally:
        if side:
            # 边界：旁边往 lan2 发的那个进程要收掉。
            lab.stop_process(side, 2, 5)
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    left = firewall_close(port, prompt, folder, "发包")
    delta = snapshot_delta(before, after)
    wan = delta["路由器的口"]["wan"]
    result.update({
        "排法": LAYOUT, "流的条数": args.flows, "往wan发的是": args.main, "同时往lan2发的是": side_kind,
        "wan的速率": speed, "两次读数之间的秒数": round(seconds, 2), "iperf3": run,
        "同时往lan2发的那一组": side_report, "五个口的rps_cpus": steered,
        "wan每秒发出去的帧": round(wan["MAC发出去的帧"] / seconds), "wan每秒发出去的字节": round(wan["MAC发出去的字节"] / seconds),
        "主机的TCP计数涨的": tcp_rise(tcp_before, tcp_after),
        **delta, "做完以后防火墙里带记号的规则还剩几条": left})
    # 发包的比较不是判据，没有"通过"。读完了不等于量对了：流量没有真的压到发包环上的那一遍，不能拿来比。
    many = args.flows >= 8 and args.main == "tcp"
    result["读数里不对劲的地方"] = sending_oddities(
        result, "wan", (int(speed) if speed.isdigit() else 10000) if args.main == "tcp" else None, many)
    result["这一遍能不能拿来比"] = not result["读数里不对劲的地方"]
    result["判据"] = []
    result["要看的数"] = {"每10秒的速率": run["每10秒的速率"], "重传": run["重传"], "UDP丢的和乱序的": [run["UDP丢的"], run["UDP乱序的"]],
                       "wan每个发包环发了多少": wan["每个发包环发了多少"], "wan每条发送队列被停了多少次": wan["每条发送队列被停了多少次"],
                       "lan2每个发包环发了多少": delta["路由器的口"]["lan2"]["每个发包环发了多少"],
                       "lan2那一组每10秒的速率": side_report["每10秒的速率"] if side_report else None,
                       "四个核心各空闲百分之几": [core["空闲"] for core in delta["四个核心各忙在哪里（百分之几）"]],
                       "主机的进程忙到百分之几": run["主机的进程忙到百分之几"],
                       "路由器自己的TCP计数涨的": delta["路由器自己的TCP计数涨的"], "主机的TCP计数涨的": result["主机的TCP计数涨的"]}


def nic_into_netns(folder, nic, address, route=None):
    """把主机的一块网卡放进网络命名空间，在里面配上地址；route 给了就在里面加这一条路由（目的网段, 下一跳）。"""
    steps = [("建命名空间", ["ip", "netns", "add", NETNS]),
             ("放进去", ["ip", "link", "set", "dev", nic, "netns", NETNS]),
             ("加地址", ["ip", "-n", NETNS, "addr", "add", f"{address}/24", "dev", nic]),
             ("打开", ["ip", "-n", NETNS, "link", "set", "dev", nic, "up"])]
    if route:
        steps.append(("加路由", ["ip", "-n", NETNS, "route", "add", route[0], "via", route[1]]))
    for name, argv in steps:
        if e3.host(["sudo", "-n"] + argv, folder, f"{nic}进命名空间-{name}")[0]:
            raise RuntimeError(f"{nic} 放进网络命名空间：{name}没有做成")


def nic_out_of_netns(folder, nic, address):
    """把网卡放回来，把它平时的测试地址加回去。"""
    e3.host(["sudo", "-n", "ip", "netns", "del", NETNS], folder, f"{nic}出命名空间-删命名空间")
    for _ in range(20):
        if os.path.exists(f"/sys/class/net/{nic}"):
            break
        time.sleep(0.5)
    # 网卡回来在 NetworkManager 看来是一块新网卡，它会自己连上、从路由器要一个地址、加一条路由。先叫它不要管这块网卡。
    e3.host(["sudo", "-n", "nmcli", "device", "set", nic, "managed", "no"], folder, f"{nic}出命名空间-NetworkManager不管它")
    # NetworkManager 可能已经抢先要来了一个地址（连带一条路由）。等它放手，把这块网卡上的地址都清掉，再加测试地址。
    time.sleep(2)
    e3.host(["sudo", "-n", "ip", "-4", "addr", "flush", "dev", nic], folder, f"{nic}出命名空间-清掉网卡上的地址")
    e3.host(["sudo", "-n", "ip", "addr", "replace", f"{address}/24", "dev", nic], folder, f"{nic}出命名空间-测试地址加回去")
    e3.host(["sudo", "-n", "ip", "link", "set", "dev", nic, "up"], folder, f"{nic}出命名空间-打开")


def wait_ping(folder, name, source, target, netns=None):
    """等到 ping 得通（网卡刚换了地方，线要重新连上），最多等 40 秒。返回通了没有。"""
    for index in range(20):
        argv = ["ping", "-n", "-q", "-c", "1", "-W", "1", "-I", source, target]
        if netns:
            argv = ["ip", "netns", "exec", netns] + argv
        if subprocess.run(["sudo", "-n"] + argv, capture_output=True, text=True).returncode == 0:
            return True
        time.sleep(1)
    return False


# 转发的三种走法：名字 -> (放进命名空间的网卡, 它在里面的地址, 里面加的路由, 服务端在不在命名空间里,
#                         服务端的地址, 客户端的地址, 是不是服务端发, 包从路由器哪个口进, 从哪个口出, 出的口的速率)
FORWARD_KINDS = {
    "bridge": (USB, USB_NETNS_ADDRESS, None, True, USB_NETNS_ADDRESS, ENO2_ADDRESS, False, "lan2", "lan3", 1000),
    # 千兆进、万兆出：发的一头是千兆网卡，速率照千兆算。
    "route-up": ("eno2", ENO2_ADDRESS, ("192.168.13.0/24", ROUTER_LAN), False, BRIDGE_ADDRESS, ENO2_ADDRESS, False,
                 "lan2", "wan", 1000),
    "route-down": ("eno2", ENO2_ADDRESS, ("192.168.13.0/24", ROUTER_LAN), False, BRIDGE_ADDRESS, ENO2_ADDRESS, True,
                   "wan", "lan2", 1000),
}


def stage_forward(port, prompt, args, result):
    """转发：包从路由器的一个口进、从另一个口出，30 秒，只报数。

    bridge：主机从 eno2 发，进 lan2，经路由器的软件桥从 lan3 出，到放进命名空间的 USB 网卡（千兆到千兆）。
    route-up：eno2 放进命名空间当发的一头，进 lan2，经路由（带地址转换）从 wan 出，到主机的 br-lan（千兆进、万兆出）。
    route-down：同一条连接上反过来，主机的 br-lan 发，进 wan，从 lan2 出（万兆进、千兆出）。
    主机自己的两块网卡之间不会走近路：一头在单独的网络命名空间里。穿没穿过路由器看进的口收的帧、出的口发的帧。
    """
    folder = args.output
    nic, address, route, server_inside, server_address, client_address, server_sends, in_port, out_port, out_speed = \
        FORWARD_KINDS[args.kind]
    e3.router_addresses(port, prompt, folder, "转发", "wan")
    e3.ensure_bridge_address(folder, "转发")
    steered = steering(port, prompt, folder, "以前-rps_cpus")
    nic_into_netns(folder, nic, address, route)
    server = None
    try:
        client_netns = None if server_inside else NETNS
        if not wait_ping(folder, "等线连上", client_address, server_address, client_netns):
            raise RuntimeError("网卡放进网络命名空间以后 40 秒里 ping 不通对面")
        argv = ["iperf3", "-s", "-1", "-B", server_address]
        if server_inside:
            argv = ["sudo", "-n", "ip", "netns", "exec", NETNS] + argv
        server = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        tcp_before = {"主机这边": host_tcp_counters(), "命名空间里": host_tcp_counters(NETNS)}
        before = snapshot(port, prompt, folder, "以前")
        started = time.monotonic()
        run = iperf(folder, "iperf3", server_address, client_address, 30, args.flows, udp=args.main == "udp",
                    reverse=server_sends, netns=client_netns, rate=args.rate)
        after = snapshot(port, prompt, folder, "以后")
        seconds = time.monotonic() - started
        tcp_after = {"主机这边": host_tcp_counters(), "命名空间里": host_tcp_counters(NETNS)}
    finally:
        # 边界：服务端要收掉，网卡不管怎样都要放回来。
        if server:
            lab.stop_process(server, 2, 5)
        nic_out_of_netns(folder, nic, address if nic == "eno2" else USB_ADDRESS)
    back = wait_ping(folder, "放回来以后等线连上", ENO2_ADDRESS if nic == "eno2" else USB_ADDRESS,
                     ROUTER_LAN if nic == "eno2" else ROUTER_USB_SIDE)
    delta = snapshot_delta(before, after)
    went_in, went_out = delta["路由器的口"][in_port]["MAC收到的帧"], delta["路由器的口"][out_port]["MAC发出去的帧"]
    result.update({
        "排法": LAYOUT, "走法": args.kind, "流的条数": args.flows, "发的是": args.main, "从哪个口进": in_port, "从哪个口出": out_port,
        "两次读数之间的秒数": round(seconds, 2), "iperf3": run, "五个口的rps_cpus": steered,
        "进的口收到的帧": went_in, "出的口发出去的帧": went_out,
        "发的一头在哪里": ("命名空间里" if server_inside else "主机这边") if server_sends else ("主机这边" if server_inside else "命名空间里"),
        "主机两头的TCP计数涨的": {side: tcp_rise(tcp_before[side], tcp_after[side]) for side in tcp_before},
        **delta, "网卡放回来以后ping得通": back})
    many = args.flows >= 8 and args.main == "tcp"
    odd = sending_oddities(result, out_port, out_speed, many)
    data_frames = run["一共字节"] // 1448
    if went_out < 0.9 * data_frames or went_in < 0.9 * data_frames:
        odd.append(f"照 iperf3 收到的字节算至少有 {data_frames} 帧数据，进的口只收到 {went_in} 帧、出的口只发出 {went_out} 帧：没有都穿过路由器")
    if not back:
        odd.append("网卡放回来以后 ping 不通路由器")
    result["读数里不对劲的地方"] = odd
    result["判据"] = []
    out = delta["路由器的口"][out_port]
    result["要看的数"] = {"每10秒的速率": run["每10秒的速率"], "重传": run["重传"], "UDP丢的和乱序的": [run["UDP丢的"], run["UDP乱序的"]],
                       "进的口收到的帧": went_in, "出的口发出去的帧": went_out,
                       "出的口每个发包环发了多少": out["每个发包环发了多少"], "出的口每条发送队列被停了多少次": out["每条发送队列被停了多少次"],
                       "四个收包环收了多少": delta["四个收包环收了多少"],
                       "四个核心各空闲百分之几": [core["空闲"] for core in delta["四个核心各忙在哪里（百分之几）"]],
                       "发的一头在哪里": result["发的一头在哪里"], "主机两头的TCP计数涨的": result["主机两头的TCP计数涨的"]}


def ring_positions(port, prompt, folder, tag):
    """主机的每个发包环硬件的两个位置：驱动登记到哪一格、硬件取到哪一格。"""
    positions = {}
    for ring in tx_rings():
        prod, cons = read_registers(port, prompt, folder, f"{tag}-发包环{ring}的位置", EDMA + 0x1004 + 0x1000 * ring, 2)
        positions[ring] = [prod & 0xffff, cons & 0xffff]
    return positions


# PPE 的缓冲管理：每个"进来的口"占着多少缓冲、现在是不是在叫这个口先别发。
PPE_BM = 0x600000


def bm_ports(port, prompt, folder, tag):
    """PPE 缓冲管理里 15 个口各占着多少缓冲、流控的状态字。

    0 到 7 号是 CPU 这一侧（主机发的包算在这几个上），8 到 13 号是 1 到 6 号口，14 号是芯片内部的。
    """
    used = read_registers(port, prompt, folder, f"{tag}-缓冲管理-各口占着的缓冲", PPE_BM + 0x1c0, 15)
    status = read_registers(port, prompt, folder, f"{tag}-缓冲管理-流控的状态字", PPE_BM + 0x140, 15)
    return {"各口占着的缓冲": used, "流控的状态字": status}


def held_bytes(port, prompt, folder, tag):
    """五个口每条发送队列现在压着多少字节没有发完。只留不是 0 的。"""
    held = {}
    for line in ask(port, prompt, folder, f"{tag}-压着的字节", "bq", 20).split("\n"):
        path, _, value = line.partition(" ")
        fields = path.split("/")
        if value.strip() not in ("", "0"):
            held[f"{fields[4]} {fields[6]}"] = int(value)
    return held


def run_output(process, seconds):
    """等一个在后面跑着的 iperf3 结束，取它的输出。等不到就收掉它，返回空的。"""
    try:
        return process.communicate(timeout=seconds)[0]
    except subprocess.TimeoutExpired:
        # 外部边界：对面断了以后 iperf3 可能一直不退出。
        lab.stop_process(process, 2, 5)
        return ""


def stage_stall(port, prompt, args, result):
    """一个口发不动的时候。路由器同时往 wan 和 lan2 各发 64 条 TCP 流 70 秒，中间把 lan2 堵住 25 秒上下再放开。

    --kind pause：主机从 eno2 连着发暂停帧压住 lan2。
    pause-two：路由器还往 lan3 发第三组 64 条流，主机同时从 eno2、USB 网卡发暂停帧，lan2、lan3 两个口一起被压住。
    pause-wan：主机从 eno1 发暂停帧压住 wan（万兆口），看 lan2 还发不发得动。
    linkdown：主机把 eno2 关掉，lan2 断线。close：路由器上把 lan2 关掉。
    看的是：堵着的时候发包环的两个位置还走不走、wan 还发不发得动；放开以后各个口通不通；
    流量都停了以后有没有哪条发送队列还压着字节。只报数。
    """
    folder, kind = args.output, args.kind
    e3.router_addresses(port, prompt, folder, "堵", "wan")
    e3.ensure_bridge_address(folder, "堵")
    firewall_open(port, prompt, folder, "堵", "wan")
    iperf_servers(port, prompt, folder, "堵", (5201, 5202, 5203))
    severe_before = severe_lines(port, prompt, folder, "以前-警告和致命错误的行数")
    before = snapshot(port, prompt, folder, "以前")
    started = time.monotonic()
    usb_run = None
    if kind == "pause-two":
        usb_run = subprocess.Popen(iperf_argv(ROUTER_USB_SIDE, USB_ADDRESS, 70, FLOWS, reverse=True, server_port=5203, interval=5),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    wan_run = subprocess.Popen(iperf_argv(ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, 70, FLOWS, reverse=True, interval=5),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lan_run = subprocess.Popen(iperf_argv(ROUTER_LAN, ENO2_ADDRESS, 70, FLOWS, reverse=True, server_port=5202, interval=5),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    blocker, second, positions, held, marks, frames_sent, buffers = None, None, {}, {}, {}, None, {}
    try:
        time.sleep(15)
        positions["堵以前"] = ring_positions(port, prompt, folder, "堵以前")
        buffers["堵以前（两个口都在发）"] = bm_ports(port, prompt, folder, "堵以前")
        marks["开始堵（从两组流开始算的秒数）"] = round(time.monotonic() - started, 1)
        if kind in ("pause", "pause-two", "pause-wan"):
            blocker = subprocess.Popen(["sudo", "-n", "python3", "-B", os.path.join(HERE, "发暂停帧.py"),
                                        "eno1" if kind == "pause-wan" else "eno2", "25"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if kind == "pause-two":
                second = subprocess.Popen(["sudo", "-n", "python3", "-B", os.path.join(HERE, "发暂停帧.py"), USB, "25"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "linkdown":
            e3.host(["sudo", "-n", "ip", "link", "set", "dev", "eno2", "down"], folder, "堵-主机关掉eno2")
        else:
            ask(port, prompt, folder, "堵-路由器关掉lan2", "ip link set lan2 down", 20)
        time.sleep(6)
        positions["堵着第一次"] = ring_positions(port, prompt, folder, "堵着第一次")
        buffers["堵着第一次"] = bm_ports(port, prompt, folder, "堵着第一次")
        held["堵着第一次"] = held_bytes(port, prompt, folder, "堵着第一次")
        ping_during = host_ping(folder, "堵着时ping-wan", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE, 20, None, 0.05)
        ping_during_lan2 = host_ping(folder, "堵着时ping-lan2", ENO2_ADDRESS, ROUTER_LAN, 20, None, 0.05)
        time.sleep(4)
        positions["堵着第二次"] = ring_positions(port, prompt, folder, "堵着第二次")
        buffers["堵着第二次"] = bm_ports(port, prompt, folder, "堵着第二次")
        held["堵着第二次"] = held_bytes(port, prompt, folder, "堵着第二次")
        lan2_during = router_stats(port, prompt, folder, "堵着-lan2", "lan2")
        if kind in ("pause", "pause-two", "pause-wan"):
            frames_sent = blocker.communicate(timeout=60)[0].strip()
        elif kind == "linkdown":
            time.sleep(4)
            e3.host(["sudo", "-n", "ip", "link", "set", "dev", "eno2", "up"], folder, "放开-主机打开eno2")
        else:
            time.sleep(4)
            toggle(port, prompt, folder, "放开", "lan2", True)
        marks["放开（从两组流开始算的秒数）"] = round(time.monotonic() - started, 1)
        wan_report = iperf_report(run_output(wan_run, 120))
        lan_report = iperf_report(run_output(lan_run, 60))
        usb_report = iperf_report(run_output(usb_run, 60)) if usb_run else None
    finally:
        # 边界：几组流、发暂停帧的进程都要收掉；主机的 eno2、路由器的 lan2 不管怎样都要打开。
        for process in (wan_run, lan_run, usb_run, blocker, second):
            if process:
                lab.stop_process(process, 2, 5)
        subprocess.run(["sudo", "-n", "ip", "link", "set", "dev", "eno2", "up"], capture_output=True)
        lab.serial_command(port, "ip link set lan2 up", prompt, 20)
    recovered = wait_ping(folder, "放开以后等lan2通", ENO2_ADDRESS, ROUTER_LAN)
    time.sleep(3)
    after = snapshot(port, prompt, folder, "以后")
    positions["放开以后"] = ring_positions(port, prompt, folder, "放开以后")
    buffers["放开以后"] = bm_ports(port, prompt, folder, "放开以后")
    e3.ensure_bridge_address(folder, "放开以后")
    pings = {name: list(host_ping(folder, f"放开以后ping-{name}", source, target, 100, None, 0.01))
             for name, _interface, source, target in PATHS}
    iperf_servers(port, prompt, folder, "放开以后", (5201, 5202))
    again = {"lan2": iperf(folder, "放开以后往lan2发10秒", ROUTER_LAN, ENO2_ADDRESS, 10, FLOWS, reverse=True, bind_port=5202),
             "wan": iperf(folder, "放开以后往wan发10秒", ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, 10, FLOWS, reverse=True)}
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; sleep 3; true", 20)
    held["流量都停了以后"] = held_bytes(port, prompt, folder, "流量都停了以后")
    timeouts = ask(port, prompt, folder, "发送队列超时的次数",
                   "cat /sys/class/net/lan2/queues/tx-*/tx_timeout /sys/class/net/wan/queues/tx-*/tx_timeout | sort -u", 20)
    watchdog = ask(port, prompt, folder, "内核记录里发送队列超时的行数", 'dmesg | grep -ci "transmit queue"; true', 20)
    dql = ask(port, prompt, folder, "内核记录里带dql的行数", 'dmesg | grep -ci "dql"; true', 20)
    severe_after = severe_lines(port, prompt, folder, "以后-警告和致命错误的行数")
    left = firewall_close(port, prompt, folder, "堵")
    delta = snapshot_delta(before, after)
    rates = wan_report["每10秒的速率"]
    result.update({
        "排法": LAYOUT, "怎样堵的": kind, "主机发了几帧暂停帧": frames_sent, "时刻": marks,
        "wan那一组每5秒的速率": rates, "wan那一组重传": wan_report["重传"],
        "lan2那一组每5秒的速率": lan_report["每10秒的速率"], "lan2那一组做完没有": lan_report["做完"],
        "lan3那一组每5秒的速率": usb_report["每10秒的速率"] if usb_report else None,
        "堵着时ping-lan2": list(ping_during_lan2),
        "wan收到的暂停帧": delta["路由器的口"]["wan"]["收到的暂停帧"],
        "发包环的两个位置（登记到、取到）": positions, "压着没发完的字节（只列不是0的）": held,
        "PPE缓冲管理": buffers,
        "堵着时ping-wan": list(ping_during),
        "堵着时lan2收到的暂停帧到这时一共": lan2_during.get("rx_pause"),
        "lan3收到的暂停帧": delta["路由器的口"]["lan3"]["收到的暂停帧"],
        "放开以后lan2通了没有": recovered, "放开以后三个口ping": pings,
        "放开以后再发10秒": {name: run["每10秒的速率"] for name, run in again.items()},
        "发送队列超时的次数里出现过的值": timeouts.split("\n"), "内核记录里发送队列超时的行数": watchdog,
        "内核记录里带dql的行数": dql, "警告和致命错误的行数": [severe_before, severe_after],
        **delta, "做完以后防火墙里带记号的规则还剩几条": left})
    odd = []
    # 堵着的那一段：从开始堵以后的第二个 5 秒算起的三个 5 秒。
    first = int(marks["开始堵（从两组流开始算的秒数）"] // 5) + 1
    blocked = rates[first:first + 3]
    lan_rates = lan_report["每10秒的速率"]
    lan_blocked = lan_rates[first:first + 3]
    if kind == "pause-wan":
        # 被压住的是 wan：它自己该发不动（不然就是万兆口没有照暂停帧办），lan2 不该受影响。
        if rates[:2] and blocked and min(blocked) > 0.5 * min(rates[:2]):
            odd.append(f"wan 被暂停帧压着的时候照样在发，万兆口没有照暂停帧办：压以前每 5 秒 {rates[:2]}，压着时 {blocked}")
        if lan_rates[:2] and lan_blocked and min(lan_blocked) < 0.1 * min(lan_rates[:2]):
            odd.append(f"wan 堵着的时候 lan2 也发不动：堵以前每 5 秒 {lan_rates[:2]}，堵着时 {lan_blocked}")
    elif rates[:2] and blocked and min(blocked) < 0.1 * min(rates[:2]):
        odd.append(f"lan2 堵着的时候 wan 也发不动：堵以前每 5 秒 {rates[:2]}，堵着时 {blocked}")
    if not recovered or any(pair != [100, 100] for pair in pings.values()):
        odd.append(f"放开以后有的口 ping 不回：lan2 通了没有 {recovered}，三个口 {pings}")
    if held["流量都停了以后"]:
        odd.append(f"流量都停了以后还有发送队列压着字节：{held['流量都停了以后']}")
    if not all(run["做完"] and run["每10秒的速率"] and run["每10秒的速率"][0] > 0 for run in again.values()):
        odd.append("放开以后再发 10 秒，有一个口发不动")
    if severe_after != severe_before or watchdog != "0" or dql != "0":
        odd.append(f"内核记录里有新的警告：{[severe_before, severe_after]}，发送队列超时 {watchdog} 行，记账 {dql} 行")
    result["读数里不对劲的地方"] = odd
    result["判据"] = []
    result["要看的数"] = {"wan那一组每5秒的速率": rates, "lan2那一组每5秒的速率": lan_rates,
                       "lan3那一组每5秒的速率": result["lan3那一组每5秒的速率"],
                       "堵着时ping-lan2": list(ping_during_lan2), "wan收到的暂停帧": result["wan收到的暂停帧"],
                       "时刻": marks, "发包环的两个位置": positions, "PPE缓冲管理": buffers,
                       "压着没发完的字节": held, "堵着时ping-wan": list(ping_during),
                       "lan2收到的暂停帧": delta["路由器的口"]["lan2"]["收到的暂停帧"],
                       "放开以后三个口ping": pings, "放开以后再发10秒": result["放开以后再发10秒"]}


def stage_mixed(port, prompt, args, result):
    """一个口发到上限的长帧的同时，另一个口发普通的包：路由器往 wan 发 64 条 TCP 流，同时 lan3 上做到上限的长帧两个方向。"""
    folder = args.output
    e3.router_addresses(port, prompt, folder, "同时", "wan")
    e3.ensure_bridge_address(folder, "同时")
    firewall_open(port, prompt, folder, "同时", "wan")
    iperf_servers(port, prompt, folder, "同时")
    before = snapshot(port, prompt, folder, "以前")
    flow = subprocess.Popen(iperf_argv(ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, 60, FLOWS, reverse=True),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(3)
        frames = max_frame_both_ways(port, prompt, folder, "同时-lan3到上限", "lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE)
        report = iperf_report(run_output(flow, 120))
    finally:
        # 边界：往 wan 发的那个进程要收掉。
        lab.stop_process(flow, 2, 5)
    after = snapshot(port, prompt, folder, "以后")
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    left = firewall_close(port, prompt, folder, "同时")
    delta = snapshot_delta(before, after)
    result.update({"排法": LAYOUT, "lan3到上限的长帧": frames, "同时往wan发的那一组": report, **delta,
                   "做完以后防火墙里带记号的规则还剩几条": left})
    result["判据"] = [{"编号": "9", "看什么": "发长帧的同时另一个口经同一组发包环发普通的包",
                     "通过": frames["通过"] and report["做完"] and all(report["每10秒的速率"]),
                     "lan3到上限的长帧": frames, "wan那一组每10秒的速率": report["每10秒的速率"]}]
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {"lan3到上限的长帧": frames, "wan那一组每10秒的速率": report["每10秒的速率"],
                       "lan3每个发包环发了多少": delta["路由器的口"]["lan3"]["每个发包环发了多少"],
                       "wan每个发包环发了多少": delta["路由器的口"]["wan"]["每个发包环发了多少"]}


def stage_swapped(port, prompt, args, result):
    """换线以后轻的那几样：ping、到上限的长帧、关口再开、统计。"""
    folder, checks = args.output, []
    left = ask(port, prompt, folder, "还在跑的发地址的进程", "pgrep -l 'dnsmasq|odhcpd' | wc -l", 15)
    if left != "0":
        raise RuntimeError("路由器上发地址的服务还在跑，没有往下做")
    sides = {"eno2": (ENO2_ADDRESS, ROUTER_LAN), USB: (USB_ADDRESS, ROUTER_USB_SIDE), "eno1": (BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE)}
    for address in (ROUTER_LAN, ROUTER_USB_SIDE, ROUTER_BRIDGE_SIDE):
        ask(port, prompt, folder, f"桥上加{address}", f"ip addr replace {address}/24 dev br-lan", 15)
    ask(port, prompt, folder, f"wan上去掉{ROUTER_BRIDGE_SIDE}", f"ip addr del {ROUTER_BRIDGE_SIDE}/24 dev wan 2>/dev/null; true", 15)
    for pair in args.ports.split(","):
        name, interface = pair.split("=")
        source, target = sides[interface]
        if interface == "eno1":
            e3.ensure_bridge_address(folder, name)
        speed = ask(port, prompt, folder, f"{name}-速率", f"cat /sys/class/net/{name}/speed", 15)
        first = host_ping(folder, f"{name}-ping", source, target, 100, None, 0.05)
        reading = {"口": name, "主机的网卡": interface, "速率": speed, "ping": list(first)}
        passed = first == (100, 100) and (name != "lan1" or speed == "10000")
        if name in (args.mtu_ports or "").split(","):
            reading["到上限"] = max_frame_both_ways(port, prompt, folder, f"{name}-上限", name, interface, source, target)
            passed = passed and reading["到上限"]["通过"]
        if name in (args.light_ports or args.mtu_ports or "").split(","):
            toggle(port, prompt, folder, f"{name}-关口再开", name, False)
            toggle(port, prompt, folder, f"{name}-关口再开", name, True)
            if interface == "eno1":
                e3.ensure_bridge_address(folder, f"{name}-再开以后")
            reading["关掉再打开以后ping"] = list(host_ping(folder, f"{name}-再开以后ping", source, target, 100, None, 0.05))
            reading["统计"] = link_stats_check(port, prompt, folder, f"{name}-统计", name, interface, source, target)
            passed = passed and reading["关掉再打开以后ping"] == [100, 100] and reading["统计"]["通过"]
        checks.append({"编号": args.number, "看什么": f"换线以后的 {name}", "通过": passed, **reading})
    result["判据"] = checks


def usb_into_netns(folder):
    """把主机的 USB 网卡放进网络命名空间，在里面配上和 eno2 同一个网段的地址。"""
    for name, argv in (("建命名空间", ["ip", "netns", "add", NETNS]),
                       ("放进去", ["ip", "link", "set", "dev", USB, "netns", NETNS]),
                       ("加地址", ["ip", "-n", NETNS, "addr", "add", f"{USB_NETNS_ADDRESS}/24", "dev", USB]),
                       ("打开", ["ip", "-n", NETNS, "link", "set", "dev", USB, "up"])):
        if e3.host(["sudo", "-n"] + argv, folder, f"USB网卡-{name}")[0]:
            raise RuntimeError(f"USB 网卡放进网络命名空间：{name}没有做成")


def usb_out_of_netns(folder):
    """把 USB 网卡放回来，把它平时的测试地址加回去（和转发那几个阶段用同一个办法）。"""
    nic_out_of_netns(folder, USB, USB_ADDRESS)


def stage_long(port, prompt, args, result):
    """长时间的测量里的一种。"""
    folder, kind, seconds = args.output, args.kind, args.seconds
    number = {"in": "24", "out": "25", "through": "26", "wan-in": "27", "wan-out": "27"}[kind]
    e3.router_addresses(port, prompt, folder, "长", "wan")
    severe_before = severe_lines(port, prompt, folder, "以前-警告和致命错误的行数")
    misc_before = e3.interrupt_count(port, prompt, folder, "以前-edma_misc", "edma_misc")
    memory_before = router_number(port, prompt, folder, "以前-可用的内存", "grep MemAvailable /proc/meminfo | tr -dc 0-9")
    # 内核记录是从开机起累计的：这一遍以前的行（比如缓冲的实验故意弄出来的）不算这一遍的，所以前后各数一次。
    empty_before = ask(port, prompt, folder, "以前-空缓冲区环空了的行数", 'dmesg | grep -c "fill ring .* is empty"; true', 20)
    speed = ask(port, prompt, folder, "wan的速率", "cat /sys/class/net/wan/speed", 15)
    server = None
    if kind == "through":
        usb_into_netns(folder)
        server = subprocess.Popen(["sudo", "-n", "ip", "netns", "exec", NETNS, "iperf3", "-s", "-1"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
    else:
        iperf_servers(port, prompt, folder, "长")
    try:
        if kind == "through":
            run = iperf(folder, "iperf3", USB_NETNS_ADDRESS, ENO2_ADDRESS, seconds, FLOWS)
        elif kind.startswith("wan"):
            e3.ensure_bridge_address(folder, "长")
            firewall_open(port, prompt, folder, "长", "wan")
            run = iperf(folder, "iperf3", ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, seconds, FLOWS, reverse=kind == "wan-out")
        else:
            run = iperf(folder, "iperf3", ROUTER_LAN, ENO2_ADDRESS, seconds, FLOWS, reverse=kind == "out")
    finally:
        if server:
            # 边界：USB 网卡不管怎样都要放回来。
            lab.stop_process(server, 2, 5)
            usb_out_of_netns(folder)
    time.sleep(5)
    target = ROUTER_BRIDGE_SIDE if kind.startswith("wan") else ROUTER_LAN
    source = BRIDGE_ADDRESS if kind.startswith("wan") else ENO2_ADDRESS
    after_ping = host_ping(folder, "跑完以后ping", source, target, 100, None, 0.05)
    severe_after = severe_lines(port, prompt, folder, "以后-警告和致命错误的行数")
    misc_after = e3.interrupt_count(port, prompt, folder, "以后-edma_misc", "edma_misc")
    memory_after = router_number(port, prompt, folder, "以后-可用的内存", "grep MemAvailable /proc/meminfo | tr -dc 0-9")
    empty = ask(port, prompt, folder, "空缓冲区环空了的行数", 'dmesg | grep -c "fill ring .* is empty"; true', 20)
    stats = router_stats(port, prompt, folder, "以后-lan2", "lan2")
    queues = e3.queue_counters(port, prompt, folder, "以后-队列计数")
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    firewall_close(port, prompt, folder, "长")
    result["判据"] = [{"编号": number, "看什么": f"长时间的测量：{kind}",
                     "通过": run["做完"] and len(run["每10秒的速率"]) >= seconds // 10 and all(run["每10秒的速率"])
                     and severe_after == severe_before and empty == empty_before and misc_after == misc_before
                     and after_ping == (100, 100) and (not kind.startswith("wan") or speed == "10000"),
                     "秒数": seconds, "wan的速率": speed, "每10秒的速率": run["每10秒的速率"], "一共字节": run["一共字节"],
                     "警告和致命错误的行数": [severe_before, severe_after], "edma_misc的次数": [misc_before, misc_after],
                     "空缓冲区环空了的行数": [empty_before, empty], "跑完以后ping": list(after_ping),
                     "路由器可用的内存（kB）": [memory_before, memory_after],
                     "每个环拿不到缓冲丢掉的": [stats[f"rx_ring{i}_no_buffer"] for i in range(CORES)],
                     "别的CPU队列的计数里不是0的": {q: queues[q] for q in OTHER_CPU_QUEUES if queues[q]},
                     "这一份摘要里判不了的": "第 27 条里可用的内存前后相差不超过 20MB 那一半：要拿第 24 条开始以前和这一条跑完以后两份摘要里的数来比"}]
    port_speed = int(speed) if kind.startswith("wan") and speed.isdigit() else 1000
    rates = run["每10秒的速率"]
    result["读数里不对劲的地方"] = ([f"有一个 10 秒的速率只有 {min(rates)} 比特每秒，不到这个口速率的一成"]
                             if rates and min(rates) < port_speed * 1000000 // 10 else [])
    if run["主机的进程忙到百分之几"] > 90:
        result["读数里不对劲的地方"].append(f"主机这一头的 iperf3 忙到了 {run['主机的进程忙到百分之几']}%")
    result["要看的数"] = {"最慢的10秒": min(rates) if rates else None, "最快的10秒": max(rates) if rates else None,
                       "edma_misc的次数": [misc_before, misc_after]}


def stage_misc(port, prompt, args, result):
    """判据 28。要先换上硬件报错中断的模块。量完路由器要重新引导。"""
    folder = args.output
    before = e3.interrupt_count(port, prompt, folder, "28-以前-edma_misc", "edma_misc")
    logged_before = router_number(port, prompt, folder, "28-以前-内核记录里EDMA的行数", 'dmesg | grep -c "EDMA: "; true')
    lab.serial_command(port, "echo 1 > /sys/module/ipq807x_eth/parameters/exp_misc_trigger", prompt, 15)
    counts = []
    for index in range(10):
        time.sleep(1)
        outcome = lab.serial_command(port, "grep edma_misc /proc/interrupts", prompt, 15)
        numbers = [int(field) for field in outcome.output.split()[1:5] if field.isdigit()] if outcome.completed else []
        counts.append(sum(numbers) if numbers else None)
    lines = lab.serial_command(port, 'dmesg | grep "EDMA: " | tail -n 20', prompt, 20)
    status = lab.serial_command(port, f"r {EDMA + 0x5c} 1", prompt, 15)
    logged = lab.serial_command(port, 'dmesg | grep -c "EDMA: "', prompt, 20)
    logged_after = int(logged.output.strip()) if logged.completed and logged.output.strip().isdigit() else logged_before
    usable = [n for n in counts if n is not None]
    result["判据"] = [{"编号": "28", "看什么": "硬件报错的中断真来一次",
                     "通过": bool(usable) and 0 < usable[-1] - before <= 20 and logged_after > logged_before,
                     "内核记录里EDMA的行数": [logged_before, logged_after],
                     "以前的次数": before, "打开开关以后每秒的次数": counts,
                     "内核记录里的行": lines.output.strip().split("\n")[-20:], "杂项状态": status.output.strip()}]


def stage_maxframe(port, prompt, args, result):
    """不是判据，只报数：硬件真正能带多长的帧。要先 load variant-maxframe.ko。

    --frame-port 只量一个口；--frame-low、--frame-high 是这个口上以前量出来的最大的通的数和最小的不通的数。
    """
    folder = args.output
    e3.router_addresses(port, prompt, folder, "开头", "wan")
    result["判据"] = []
    result["帧长的测量"] = [frame_limit(port, prompt, folder, name, interface, source, target, limit,
                                   args.frame_low, args.frame_high)
                       for name, interface, source, target, limit in FRAME_PATHS
                       if args.frame_port in (None, name)]


def jumbo_paused(port, prompt, folder, name, interface, source, target, mtu):
    """路由器用 64 条 TCP 流往这个口发长帧 45 秒，第 10 秒起主机从这块网卡发 15 秒暂停帧把这个口压住。

    读的：每 5 秒的速率；压着的时候 PPE 的缓冲管理、主机经 eno2 ping 路由器 20 个回了几个（别的口还通不通）；
    放开 10 秒以后的缓冲管理（卡在 PPE 里放不回来的缓冲看得到）；这个口收到的暂停帧涨了多少。
    """
    tag = f"{name}-{mtu}-暂停帧"
    before = frame_counts(port, prompt, folder, f"{tag}-以前", name, interface)
    run = subprocess.Popen(iperf_argv(target, source, 45, FLOWS, reverse=True, interval=5),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    blocker = None
    try:
        time.sleep(10)
        blocker = subprocess.Popen(["sudo", "-n", "python3", "-B", os.path.join(HERE, "发暂停帧.py"), interface, "15"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(6)
        during = bm_ports(port, prompt, folder, f"{tag}-压着时")
        other = host_ping(folder, f"{tag}-压着时ping另一个口", ENO2_ADDRESS, ROUTER_LAN, 20, None, 0.05)
        frames_sent = blocker.communicate(timeout=60)[0].strip()
        report = iperf_report(run_output(run, 120))
    finally:
        # 边界：发流量的、发暂停帧的进程都要收掉。
        for process in (run, blocker):
            if process:
                lab.stop_process(process, 2, 5)
    time.sleep(10)
    released = bm_ports(port, prompt, folder, f"{tag}-放开10秒以后")
    after = frame_counts(port, prompt, folder, f"{tag}-以后", name, interface)
    rise = frame_rise(before, after)
    return {"主机发了几帧暂停帧": frames_sent, "每5秒的速率": report["每10秒的速率"], "做完": report["做完"],
            "压着时的PPE缓冲管理": during, "压着时ping另一个口": list(other), "放开10秒以后的PPE缓冲管理": released,
            "这个口收到的暂停帧涨": rise.get("涨了的计数", {}).get("rx_pause"), "涨了的计数": rise.get("涨了的计数")}


def stage_jumbo(port, prompt, args, result):
    """不是判据，只报数：正式的模块，在一个 MTU（默认是许设的最大的 12262）上压流量。

    一个口两头的 MTU 都设成这个数：先 ping 100 个带满的包；再用 64 条 TCP 流两个方向各发 30 秒；
    再 ping 100 个带满的包。每一步以后读 PPE 的缓冲管理。--frame-port 选口，--jumbo-mtu 选 MTU。
    量帧长那一项只在实验用的模块上、只在最大能通的数上压过流量；正式的模块在它自己许设的 MTU 上没有压过。
    """
    folder, mtu = args.output, args.jumbo_mtu
    e3.router_addresses(port, prompt, folder, "开头", "wan")
    readings = []
    for name, interface, source, target, _limit in FRAME_PATHS:
        if args.frame_port not in (None, name):
            continue
        router_port_alone(port, prompt, folder, f"{name}-开头", name, target)
        try:
            first = frame_try(port, prompt, folder, name, interface, source, target, mtu, "-压以前")
            iperf_servers(port, prompt, folder, f"{name}-{mtu}-TCP")
            tcp = [frame_tcp(port, prompt, folder, name, interface, source, target, mtu, False),
                   frame_tcp(port, prompt, folder, name, interface, source, target, mtu, True)]
            after_load = bm_ports(port, prompt, folder, f"{name}-{mtu}-TCP以后")
            paused = jumbo_paused(port, prompt, folder, name, interface, source, target, mtu) if args.with_pause else None
            last = frame_try(port, prompt, folder, name, interface, source, target, mtu, "-压以后")
        finally:
            # 边界：不管量没量成，两头的 MTU 和路由器的桥都要改回去。先改主机这一头。
            set_host_mtu(folder, f"{name}-收尾", interface, 1500)
            ask(port, prompt, folder, f"{name}-收尾-路由器{name}的MTU改回1500", f"ip link set {name} mtu 1500", 15)
            router_port_back(port, prompt, folder, f"{name}-收尾", name, target)
        readings.append({"口": name, "MTU": mtu, "压以前ping带满的包": first, "TCP两个方向": tcp,
                         "TCP以后的PPE缓冲管理": after_load, "长帧发着的时候被暂停帧压住": paused,
                         "压以后ping带满的包": last})
    result["判据"] = []
    result["每个口"] = readings
    odd = []
    for reading in readings:
        for moment in ("压以前ping带满的包", "压以后ping带满的包"):
            if not reading[moment]["100个回100个"] or reading[moment]["出了事"]:
                odd.append(f"{reading['口']} MTU {mtu} {moment}：回了 {reading[moment]['回了']} 个，出了事 {reading[moment]['出了事']}")
        for run in reading["TCP两个方向"]:
            rates = run["每秒的速率（比特每秒）"]
            if not run["做完"] or not rates or min(rates) == 0:
                odd.append(f"{reading['口']} MTU {mtu} TCP {run['方向']}：做完 {run['做完']}，最慢的一秒 {min(rates) if rates else None}")
        held = reading["长帧发着的时候被暂停帧压住"]
        if held and (any(held["放开10秒以后的PPE缓冲管理"]["各口占着的缓冲"]) or held["压着时ping另一个口"] != [20, 20]):
            odd.append(f"{reading['口']} MTU {mtu} 被暂停帧压住：放开 10 秒以后各口还占着 {held['放开10秒以后的PPE缓冲管理']['各口占着的缓冲']}，"
                       f"压着时 ping 另一个口 {held['压着时ping另一个口']}")
    result["读数里不对劲的地方"] = odd
    result["要看的数"] = {f"{reading['口']}": {"被暂停帧压住": reading["长帧发着的时候被暂停帧压住"] and {
                                              key: reading["长帧发着的时候被暂停帧压住"][key] for key in
                                              ("每5秒的速率", "压着时ping另一个口", "这个口收到的暂停帧涨")} | {
                                              "压着时各口占着的缓冲": reading["长帧发着的时候被暂停帧压住"]["压着时的PPE缓冲管理"]["各口占着的缓冲"],
                                              "放开10秒以后各口占着的缓冲": reading["长帧发着的时候被暂停帧压住"]["放开10秒以后的PPE缓冲管理"]["各口占着的缓冲"]},
                                          "压以前回了": reading["压以前ping带满的包"]["回了"],
                                          "压以后回了": reading["压以后ping带满的包"]["回了"],
                                          "TCP每个方向最慢和最快的一秒": [[min(run["每秒的速率（比特每秒）"] or [0]), max(run["每秒的速率（比特每秒）"] or [0])]
                                                              for run in reading["TCP两个方向"]],
                                          "压以后缓冲管理各口占着的": reading["压以后ping带满的包"]["量完以后的PPE缓冲管理"]["各口占着的缓冲"]}
                       for reading in readings}


def stage_maxframe_back(port, prompt, args, result):
    """帧长那一项做完、换回正式的模块以后：两头的 MTU 都是 1500，ping 100 个回 100 个。

    顺带读一次"设成 12263 收不收"：正式的模块不收，实验用的模块收。读完不管收没收都设回 1500。
    """
    folder, readings = args.output, []
    e3.router_addresses(port, prompt, folder, "开头", "wan")
    for name, interface, source, target, _limit in FRAME_PATHS:
        if source == BRIDGE_ADDRESS:
            e3.ensure_bridge_address(folder, name)
        over = lab.serial_command(port, f"ip link set {name} mtu {MAX_MTU + 1}", prompt, 15)
        ask(port, prompt, folder, f"{name}-MTU设回1500", f"ip link set {name} mtu 1500", 15)
        host_mtu = {}
        for device in ([interface, "br-lan"] if interface == "eno1" else [interface]):
            with open(f"/sys/class/net/{device}/mtu") as file:
                host_mtu[device] = file.read().strip()
        readings.append({"口": name, "设成12263的退出码": over.exit_code,
                         "路由器这个口的MTU": ask(port, prompt, folder, f"{name}-路由器的MTU",
                                           f"cat /sys/class/net/{name}/mtu", 15),
                         "主机网卡的MTU": host_mtu,
                         "ping": list(host_ping(folder, f"{name}-ping", source, target, 100, None, 0.05))})
    result["判据"] = []
    result["换回正式的模块以后"] = readings


def finish(args):
    """删掉主机 br-lan 的测试地址，回读，和开工以前记下的比。不碰路由器。"""
    result = e3.finish(args)
    result["代号"] = "E-0004"
    return result


def host_pause_counters(interface):
    """主机一块网卡的计数里名字带 pause、xoff、xon 的那几项。只读。"""
    text = subprocess.run(["ethtool", "-S", interface], capture_output=True, text=True).stdout
    return {name.strip(): int(value) for name, value in re.findall(r"^\s*([^:\n]*(?:pause|xoff|xon)[^:\n]*):\s*(\d+)", text, re.M | re.I)}


def stage_xgpause(port, prompt, args, result):
    """不是判据，只报数：万兆口 wan 收不过来的时候发不发暂停帧。

    判据第 17 条只在千兆口 lan2 上量了发暂停帧，万兆口用的是另一个 MAC、另一段代码，没有哪一条量到它。
    主机经 eno1 用 64 条 UDP 流不限速地往路由器发 30 秒，读 wan 发出的暂停帧数、主机 eno1 的计数里带 pause 的那几项。
    """
    folder = args.output
    e3.router_addresses(port, prompt, folder, "万兆口流控", "wan")
    e3.ensure_bridge_address(folder, "万兆口流控")
    firewall_open(port, prompt, folder, "万兆口流控", "wan")
    iperf_servers(port, prompt, folder, "万兆口流控")
    shown = ask(port, prompt, folder, "ethtool-a-wan", "ethtool -a wan", 20)
    before = router_stats(port, prompt, folder, "以前-wan", "wan")
    host_before, sent_before = host_pause_counters("eno1"), e3.host_stat("eno1", "tx_packets")
    run = subprocess.Popen(iperf_argv(ROUTER_BRIDGE_SIDE, BRIDGE_ADDRESS, 30, FLOWS, udp=True, length=FLOOD_LENGTH),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        run.wait(timeout=90)
    finally:
        # 边界：发流量的进程不管怎样都要收掉。
        lab.stop_process(run, 2, 5)
    time.sleep(2)
    after = router_stats(port, prompt, folder, "以后-wan", "wan")
    host_after, sent_after = host_pause_counters("eno1"), e3.host_stat("eno1", "tx_packets")
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    firewall_close(port, prompt, folder, "万兆口流控")
    sent, arrived = sent_after - sent_before, after["rx_unicast"] - before["rx_unicast"]
    taken = sum(after[f"rx_ring{i}_packets"] - before[f"rx_ring{i}_packets"] for i in range(CORES))
    result["判据"] = []
    result.update({"ethtool -a wan": shown.split("\n"), **pause_shown(shown),
                   "路由器wan发出的暂停帧涨": after["tx_pause"] - before["tx_pause"],
                   "主机eno1的计数里带pause的那几项涨": {name: host_after[name] - host_before.get(name, 0) for name in host_after},
                   "主机eno1发的帧涨": sent, "wan收到的单播帧涨": arrived, "四个收包环收了多少涨的合计": taken,
                   "丢在主机到wan的MAC之间的": sent - arrived, "丢在wan的MAC到收包环之间的": arrived - taken})
    result["读数里不对劲的地方"] = ([] if sent > 1000000 else [f"主机这 30 秒只发出去 {sent} 帧，路由器没有真的被压到"])
    result["要看的数"] = {key: result[key] for key in ("协商出来的收发", "路由器wan发出的暂停帧涨", "主机eno1的计数里带pause的那几项涨",
                                               "丢在主机到wan的MAC之间的", "丢在wan的MAC到收包环之间的")}


def stage_cpuqueue(port, prompt, args, result):
    """不是判据，只报数：给 CPU 的帧进了主机不用的队列以后，到不到得了主机。要先 load variant-bmtune.ko。

    新驱动把"队列到环"那张表里别的队列都指到了主机的 15 号收包环，可是 PPE 里"哪些队列属于哪个收包环"的表，
    0 到 11 号、128 到 143 号队列写在固件的四个环名下，别的队列哪个环名下都不在。平时没有帧进这些队列，
    所以帧真的进去以后会怎样从来没有量到过。这里把给 CPU 的帧依次改进几条这样的队列，各 ping 20 个。
    """
    folder = args.output
    param = "/sys/module/ipq807x_eth/parameters/exp_cpu_queue"
    readings = []
    try:
        # 16 是主机自己的；24 在主机那一项里但是哪个环名下都不在；4、0、8、128 写在固件的环名下；最后改回 16。
        for base in (16, 24, 4, 0, 8, 128, 16):
            tag = f"从{base}号队列起-第{len(readings)}回"
            ask(port, prompt, folder, f"{tag}-改", f"echo {base} > {param}", 15)
            queues_before = e3.queue_counters(port, prompt, folder, f"{tag}-以前-队列计数")
            stats_before = router_stats(port, prompt, folder, f"{tag}-以前-lan2", "lan2")
            pinged = host_ping(folder, f"{tag}-ping", ENO2_ADDRESS, ROUTER_LAN, 20, None, 0.05)
            queues_after = e3.queue_counters(port, prompt, folder, f"{tag}-以后-队列计数")
            stats_after = router_stats(port, prompt, folder, f"{tag}-以后-lan2", "lan2")
            readings.append({"给CPU的帧从哪条队列起": base, "ping发了和回了": list(pinged),
                             "计数涨了的队列": {queue: queues_after[queue] - queues_before[queue] for queue in range(len(queues_after))
                                         if queues_after[queue] != queues_before[queue]},
                             "四个收包环收了多少涨的": [stats_after[f"rx_ring{i}_packets"] - stats_before[f"rx_ring{i}_packets"]
                                               for i in range(CORES)],
                             "PPE缓冲管理": bm_ports(port, prompt, folder, tag)})
    finally:
        # 边界：不管量没量成，都改回主机自己的队列。
        lab.serial_command(port, f"echo 16 > {param}", prompt, 15)
    result["判据"] = []
    result["每一回"] = readings
    result["读数里不对劲的地方"] = [f"给 CPU 的帧从 {reading['给CPU的帧从哪条队列起']} 号队列起的时候，ping 20 个回了 {reading['ping发了和回了'][1]} 个"
                             for reading in readings if reading["ping发了和回了"] != [20, 20]]
    result["要看的数"] = {f"第{index}回，从{reading['给CPU的帧从哪条队列起']}号队列起": [reading["ping发了和回了"], reading["四个收包环收了多少涨的"]]
                       for index, reading in enumerate(readings)}


def stage_limits(port, prompt, args, result):
    """不是判据：换一组 PPE 的缓冲上限，读回来核真的改进去了。要先 load variant-bmtune.ko。

    --cpu-bm "开不开流控 上限 保留的 权重"：管 PPE 给"主机发来的包"算的缓冲（缓冲管理里 CPU 这一侧的 8 个口）。
    --uni-ac "上限 权重"：管每条单播队列压到多少个缓冲就开始丢。
    单播队列的上限那一项有四个字，硬件要整项写完才收；所以写完以后从硬件里把第一个字读回来，和要写的值比。
    """
    folder = args.output
    param = "/sys/module/ipq807x_eth/parameters"
    fc, _ceiling, _react, _weight = (int(part) for part in args.cpu_bm.split())
    queue_ceiling, queue_weight = (int(part) for part in args.uni_ac.split())
    ask(port, prompt, folder, "写-CPU这一侧", f'echo "{args.cpu_bm}" > {param}/exp_cpu_bm', 15)
    ask(port, prompt, folder, "写-单播队列", f'echo "{args.uni_ac}" > {param}/exp_uni_ac', 15)
    shown = {name: ask(port, prompt, folder, f"参数读回-{name}", f"cat {param}/{name}", 15) for name in ("exp_cpu_bm", "exp_uni_ac")}
    switch = [word & 1 for word in read_registers(port, prompt, folder, "硬件读回-缓冲管理的流控开关", PPE_BM + 0x100, 15)]
    bm_words = read_registers(port, prompt, folder, "硬件读回-缓冲管理0号口的两个字", PPE_BM + 0x1000, 2)
    # 16 号是主机的第一条队列；192 号是 lan2（4 号口）的第一条单播队列，主机发给 lan2 的包进的就是它。
    queue_words = {queue: read_registers(port, prompt, folder, f"硬件读回-{queue}号队列的上限", 0x800000 + 0x48000 + queue * 0x10, 1)[0]
                   for queue in (16, 192)}
    # 第一个字：第 0 位是开关，第 17 位是"上限随剩下的缓冲变"，第 18 到 20 位是权重，第 21 到 31 位是上限。
    wanted = 1 | 1 << 17 | queue_weight << 18 | queue_ceiling << 21
    taken = all(word == wanted for word in queue_words.values()) and all(bit == fc for bit in switch[:8])
    result["判据"] = []
    result.update({"要写的": {"exp_cpu_bm": args.cpu_bm, "exp_uni_ac": args.uni_ac}, "参数读回": shown,
                   "硬件读回-缓冲管理15个口的流控开关": switch,
                   "硬件读回-缓冲管理0号口的两个字": [f"{word:08x}" for word in bm_words],
                   "硬件读回-队列的上限的第一个字": {queue: f"{word:08x}" for queue, word in queue_words.items()},
                   "队列的上限的第一个字该是": f"{wanted:08x}", "硬件里真的改进去了": taken})
    result["读数里不对劲的地方"] = [] if taken else ["写进去的上限没有在硬件里读回来"]
    result["要看的数"] = {"硬件里真的改进去了": taken, "参数读回": shown,
                       "硬件读回-队列的上限的第一个字": result["硬件读回-队列的上限的第一个字"]}


STAGES = {"jumbo": stage_jumbo, "xgpause": stage_xgpause, "cpuqueue": stage_cpuqueue, "limits": stage_limits, "base": stage_base, "mtu": stage_mtu, "spread": stage_spread, "updown": stage_updown,
          "pause": stage_pause, "reload": stage_reload, "overload": stage_overload, "load": stage_load,
          "buffer": stage_buffer, "txrate": stage_txrate, "quiet": e3.stage_quiet, "swapped": stage_swapped,
          "long": stage_long, "misc": stage_misc, "maxframe": stage_maxframe,
          "maxframe-back": stage_maxframe_back, "forward": stage_forward, "stall": stage_stall, "mixed": stage_mixed}


def main():
    """读命令行，做一个阶段，把结果打印成一行 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES) + ["finish"], help="做哪一个阶段")
    parser.add_argument("--label", required=True, help="这一次读数叫什么")
    parser.add_argument("--work", required=True, help="上机期间放东西的工作文件夹（里面有 实验用的模块/）")
    parser.add_argument("--module", help="主机上编出来的正式的 ipq807x-eth.ko，和镜像里带的那一个比（finish 不用）")
    parser.add_argument("--file", help="load 阶段：实验用的模块的文件名，或者 normal")
    parser.add_argument("--with-lan2", action="store_true", help="txrate 阶段：同时往 lan2 发")
    parser.add_argument("--ports", help="swapped 阶段：口=主机网卡，用逗号隔开，例如 lan1=eno1,lan2=enxd4a2cde31c38,lan4=eno2")
    parser.add_argument("--mtu-ports", help="swapped 阶段：要量到上限的口，用逗号隔开")
    parser.add_argument("--light-ports", help="swapped 阶段：要做关口再开和统计的口（默认和 --mtu-ports 一样）")
    parser.add_argument("--number", default="22", help="swapped 阶段：判据的编号（22 或者 23）")
    parser.add_argument("--kind", choices=["in", "out", "through", "wan-in", "wan-out", "bridge", "route-up", "route-down",
                                           "pause", "pause-two", "pause-wan", "linkdown", "close"],
                        help="long 阶段：哪一种跑法；forward 阶段：bridge、route-up、route-down；stall 阶段：pause、linkdown、close")
    parser.add_argument("--layout", choices=["万兆口四个", "每个口两个"], default="万兆口四个",
                        help="加载着的模块的发包环是哪一种排法（默认：万兆口四个，正式的模块）")
    parser.add_argument("--flows", type=int, default=FLOWS, help="txrate、forward 阶段：几条流（默认 64）")
    parser.add_argument("--main", choices=["tcp", "udp"], default="tcp", help="txrate、forward 阶段：发的是 TCP 还是限速的 UDP")
    parser.add_argument("--rate", default="100M", help="--main udp 时每条流限到多少（默认 100M）")
    parser.add_argument("--side", choices=["none", "tcp", "udp"], default="none", help="txrate 阶段：同时往 lan2 发什么")
    parser.add_argument("--side-flows", type=int, default=FLOWS, help="--side 的那一组几条流（默认 64）")
    parser.add_argument("--side-rate", default="100M", help="--side udp 时每条流限到多少（默认 100M）")
    parser.add_argument("--diag", action="store_true",
                        help="txrate 阶段：发包前后把路由器和主机两头的全部网络计数存下来，列出涨了一千以上的")
    parser.add_argument("--seconds", type=int, default=1800, help="long 阶段：跑多少秒（默认 1800）")
    parser.add_argument("--cpu-bm", default="0 250 100 4", help='limits 阶段：CPU 这一侧的"开不开流控 上限 保留的 权重"')
    parser.add_argument("--uni-ac", default="400 4", help='limits 阶段：每条单播队列的"上限 权重"')
    parser.add_argument("--frame-port", choices=[path[0] for path in FRAME_PATHS], help="maxframe 阶段：只量这一个口")
    parser.add_argument("--jumbo-mtu", type=int, default=MAX_MTU, help="jumbo 阶段：两头的 MTU 设成多少（默认 12262）")
    parser.add_argument("--with-pause", action="store_true",
                        help="jumbo 阶段：另外量一遍长帧发着的时候这个口被暂停帧压住（只在千兆口上有意义）")
    parser.add_argument("--frame-low", type=int, help="maxframe 阶段：这个口上以前量出来的最大的通的数，给了就接着找")
    parser.add_argument("--frame-high", type=int, help="maxframe 阶段：这个口上以前量出来的最小的不通的数")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    args = parser.parse_args()
    if args.stage == "load" and not args.file:
        parser.error("load 阶段要用 --file 写明换成哪一个")
    if args.stage == "swapped" and not args.ports:
        parser.error("swapped 阶段要用 --ports 写明哪个口接着哪块网卡")
    if args.stage in ("long", "forward", "stall") and not args.kind:
        parser.error("这个阶段要用 --kind 写明哪一种")
    if args.stage == "forward" and args.kind not in FORWARD_KINDS:
        parser.error("forward 阶段的 --kind 是 bridge、route-up、route-down")
    if args.stage == "stall" and args.kind not in ("pause", "pause-two", "pause-wan", "linkdown", "close"):
        parser.error("stall 阶段的 --kind 是 pause、pause-two、pause-wan、linkdown、close")
    global LAYOUT
    LAYOUT = args.layout
    if args.stage != "finish" and not args.module:
        parser.error("这个阶段要用 --module 给出编出来的正式的模块文件")

    e3.VARIANTS = os.path.join(args.work, "实验用的模块")
    e3.BRIDGE_SNAPSHOT = os.path.join(args.work, "主机br-lan-开工以前.txt")

    lab.stop_on_signals()
    if args.stage == "finish":
        result = finish(args)
    else:
        os.makedirs(args.output)
        prompt = re.compile(lab.LINUX_PROMPT.encode())
        port = lab.open_serial(args.device, args.baud, os.path.join(args.output, "串口.log"))
        level = None
        try:
            if lab.console_is_booting(port, 0.5):
                raise RuntimeError("控制台上有启动的输出，什么都没有发")
            result, level = e3.identity(port, prompt, args)
            result["代号"] = "E-0004"
            for index, helper in enumerate(ROUTER_HELPERS):
                ask(port, prompt, args.output, f"01-定义小函数-{index}", helper, 15)
            STAGES[args.stage](port, prompt, args, result)
        except (RuntimeError, ValueError, KeyError, IndexError, OSError, TimeoutError, termios.error,
                subprocess.SubprocessError) as error:
            # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
            result = {"代号": "E-0004", "这一次": args.label, "阶段": args.stage, "读完": False, "原因": str(error)}
        finally:
            if level is not None:
                lab.serial_command(port, f"dmesg -n {level}", prompt, 15)
            lab.close_serial(port)
    if result["读完"]:
        e3.e0002.write_summary(args.summary, result)
        result = {"代号": "E-0004", "这一次": args.label, "阶段": args.stage, "读完": True, "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result["判据"] if not check["通过"]],
                  "读数里不对劲的地方": result.get("读数里不对劲的地方", []),
                  "要看的数": result.get("要看的数", {})}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
