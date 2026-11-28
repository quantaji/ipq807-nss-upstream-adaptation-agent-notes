#!/usr/bin/env python3
"""E-0006（第四步留下的十几件事和第五步的 devlink 端口、健康报告）的读数脚本。

要回答的事和判据在同一个文件夹的 报告.md 第 1 节。E-0004、E-0005 的读数脚本里现成的阶段原样拿来用
（导入 ../E-0005/E-0005.py，它又导入 ../E-0004/E-0004.py），这份脚本另外加下面这些阶段。
判据的编号写成"4-3"（第四步设计文件第九节判据第 3 条）、"5-6"（第五步设计文件判据第 6 条）。

  hosthold 开工时做一次，不碰路由器：主机的 br-lan 归 NetworkManager 管，它等不到 DHCP 就把 br-lan 删掉、过一阵再建，
           测试地址留不住。把 NetworkManager 里 br-lan 这个连接改成固定用测试地址、不要 IPv6，再连上；
           改以前的设置存在工作文件夹里。Wi-Fi 的连接不碰。
  tools    把读每队列统计要用的 python 和内核源码树里的 ynl 传到路由器的内存里，试读一次。
           每一次内存引导以后先做它：别的阶段读一个口的计数时都要用 ynl。
  stdstats 4-14、4-15：ping 1000 个定长的包，前后读 ethtool -S、标准的三组统计、每队列统计。
  setup    换上实验用的模块 variant-e6.ko（--insmod 带加载时的参数），或者 --file normal 换回正式的模块。
           带 exp_tx_thread=1（收"发完"进线程）时，另外把这四个线程各绑到一个核心上。
  info     只读命令的输出：5-1、5-2、5-3、5-4、4-2，4-3 里"有没有收发完的线程"，4-13 里 ethtool -S 的名字。
  pause    4-1、4-13：路由器往 wan、lan2 各发 64 条 TCP 流，主机用暂停帧压住 lan2 25 秒。
  run      做 E-0004 的一个阶段（--inner），前后读三本账、每条出口队列丢的帧；中间每 3 秒读一次健康报告的诊断，
           算每个发包环最多占到几格。--judge txrate 判 4-3、报 4-5；--judge drops 判 4-9。
  fwdmix   4-4：一边转发（--kind route-up 是 lan2 进 wan 出，route-down 是 wan 进 lan2 出），一边路由器自己往 wan 发。
  three    4-6：路由器同时往 wan、lan2、lan3 各发 64 条 TCP 流 30 秒，读四个核心花在软中断上的时间。
  jam      4-7：--part two 是 lan2、lan3 发着长帧一起被压住，wan 两个方向照常发；
           --part wan 是主机从 eno1 每 2 毫秒发一个暂停帧压 wan，压得住再三个口一起压；
           --part pair 是对照：只有 wan 两个方向各 64 条流、谁都不压，60 秒。
  modtimer 4-8：ping 1000 个量往返时间，再两个方向各发 30 秒，读收包环、发完环的中断各进了多少次。
  buf      4-10、5-7、5-8：四个开关（拿不到新页、第 0 组不补缓冲、页的号改坏一次、"发完"登记的号改坏一次）。
  unknown  4-11：最长帧放到 16383 的配法上，wan 两头 MTU 16334 压流量，数状态里没有认得的位的中断。
  sram     4-12、5-6：同一种配法上从 lan3 发 MTU 16357 的帧，造出"发包的内存满了"。做完要重新内存引导。
  diag     5-5：路由器往 wan 发着流量的时候读三个健康报告的诊断。
  reload   5-9：卸载、加载三遍。
  finish   收工的第一步，不碰路由器：把 NetworkManager 里 br-lan 的连接照 hosthold 存下来的改回去、断开，
           eno1 的 MTU 改回 1500；删掉主机 br-lan 的测试地址，回读，和开工以前记下的比。

例子（在这个文件夹里跑，前面带 PYTHONDONTWRITEBYTECODE=1）：
  python3 E-0006.py --stage info --label 正式的驱动-只读 --work /tmp/E-0006 --module /tmp/E-0006/模块.ko \\
      --output /tmp/E-0006/读数/正式的驱动-只读 --summary /tmp/E-0006/读数/结果-正式的驱动-只读.json

工作文件夹（--work）是上机期间放东西的地方，里面有 实验用的模块/ 和 工具/；每一遍的摘要也先写在它里面。
主机这一头做的事和 E-0004、E-0005 的读数脚本一样，做完都放回去。路由器这一头的改动都只在内存里。
地址（MAC）的数值不显示、不写进任何文件。

另有一种用法，只给这份脚本自己用：sudo python3 E-0006.py --pause-frames 网卡 秒数 间隔秒数，从这块网卡连着发暂停帧。

结果是标准输出最后一行的一个 JSON 对象。退出码 0 表示读完了，1 表示没读完，2 表示用法错误。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("e0005", os.path.join(HERE, "..", "E-0005", "E-0005.py"))
e5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e5)
e4, e3, lab, ask, read_registers = e5.e4, e5.e3, e5.lab, e5.ask, e5.read_registers

PARAM = e5.PARAM
# variant-e6.ko 的参数；正式的模块没有这些参数。
e5.PARAM_NAMES = ("exp_no_fc", "exp_cmpl_spread", "exp_tx_thread", "exp_rx_mod", "exp_tx_mod", "exp_maxframe",
                  "exp_fail_alloc", "exp_starve_ring", "exp_bad_page", "exp_bad_cmpl")
MISC_STAT, MISC_MASK = e4.EDMA + 0x5c, e4.EDMA + 0x60
TX_SRAM_FULL = 0x10
TX_RING_SLOTS = 1024
REPORTERS = ("edma_hw", "edma_ring", "edma_rxfill")
# 五个口在 PPE 里的口号。
PORT_NUMBER = {"lan4": 2, "lan3": 3, "lan2": 4, "wan": 5, "lan1": 6}
YNL = "python3 /tmp/ynl/pyynl/cli.py --no-schema --spec /tmp/ynl/netdev.yaml"
DIAG_FILE = "/tmp/e6-diag.txt"
LONG_MTU = 12262
SEVERE = "WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error"


def pause_frames(interface, seconds, interval):
    """从主机的一块网卡连着发暂停帧（802.3x），每 interval 秒一帧，每一帧要对面停最长的时间。做完打印发了几帧。"""
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((interface, 0))
    source = sock.getsockname()[4]
    frame = bytes([0x01, 0x80, 0xC2, 0x00, 0x00, 0x01]) + source + bytes([0x88, 0x08, 0x00, 0x01, 0xFF, 0xFF]) + bytes(42)
    end, count = time.monotonic() + seconds, 0
    while time.monotonic() < end:
        sock.send(frame)
        count += 1
        time.sleep(interval)
    print(count)


def pauser(interface, seconds, interval=0.01):
    """起一个发暂停帧的进程。"""
    return subprocess.Popen(["sudo", "-n", sys.executable, "-B", os.path.abspath(__file__), "--pause-frames",
                             interface, str(seconds), str(interval)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


# 标准的统计里的名字，对着 E-0004、E-0005 的读数脚本用的老名字（那时这些数都在 ethtool -S 里）。
MAC_OLD_NAMES = {"BroadcastFramesReceivedOK": "rx_broadcast", "MulticastFramesReceivedOK": "rx_multicast",
                 "FrameCheckSequenceErrors": "rx_fcs_error", "AlignmentErrors": "rx_align_error",
                 "FrameTooLongErrors": "rx_too_long", "OctetsReceivedOK": "rx_good_bytes",
                 "BroadcastFramesXmittedOK": "tx_broadcast", "MulticastFramesXmittedOK": "tx_multicast",
                 "FramesLostDueToIntMACXmitError": "tx_underrun", "OctetsTransmittedOK": "tx_bytes",
                 "FramesAbortedDueToXSColls": "tx_abort_collision", "MultipleCollisionFrames": "tx_multi_collision",
                 "SingleCollisionFrames": "tx_single_collision", "FramesWithExcessiveDeferral": "tx_excessive_defer",
                 "FramesWithDeferredXmissions": "tx_defer", "LateCollisions": "tx_late_collision"}
RMON_OLD_NAMES = {"etherStatsUndersizePkts": "rx_runt", "etherStatsFragments": "rx_fragment"}
BIN_OLD_NAMES = {"0-64": "64byte", "65-127": "65_127byte", "128-255": "128_255byte", "256-511": "256_511byte",
                 "512-1023": "512_1023byte", "1024-1518": "1024_1518byte", "1519-12288": "1519_maxbyte"}
MAC_PRIVATE = ("rx_unicast", "tx_unicast", "rx_bad_bytes", "tx_collisions")


def read_port(port, prompt, folder, name, netdev):
    """读路由器一个口的全部计数，分开放：ethtool -S 的私有项、MAC 一组、RMON 一组（四个数和收、发照帧长分的格子）、
    暂停帧一组、每队列统计里每条发送队列的包数和停、叫醒的次数。要先做过 tools 阶段。"""
    text = ask(port, prompt, folder, name,
               f"ethtool -S {netdev}; echo ==标准; ethtool --json -S {netdev} --groups eth-mac rmon; echo ==暂停帧; "
               f"ethtool --json -I -a {netdev}; echo ==队列; cat /sys/class/net/{netdev}/ifindex; "
               f"{YNL} --dump qstats-get --json '{{\"scope\": \"queue\"}}' --output-json", 90)
    private_text, rest = text.split("==标准", 1)
    standard_text, rest = rest.split("==暂停帧", 1)
    pause_text, queue_text = rest.split("==队列", 1)
    private = {}
    for line in private_text.split("\n"):
        key, _, value = line.strip().partition(": ")
        if value.strip().isdigit():
            private[key] = int(value)
    standard = json.loads(standard_text)[0]
    mac, rmon = standard["eth-mac"], standard["rmon"]
    bins = {side: {f"{row['low']}-{row['high']}": row["val"] for key, rows in rmon.items()
                   if isinstance(rows, list) and key.startswith(side) for row in rows} for side in ("rx", "tx")}
    pause = json.loads(pause_text)[0].get("statistics", {})
    queue_lines = [line for line in queue_text.split("\n") if line.strip()]
    index = int(queue_lines[0])
    queues = {row["queue-id"]: {"包数": row.get("tx-packets"), "停": row.get("tx-stop"), "叫醒": row.get("tx-wake")}
              for row in json.loads(queue_lines[-1]) if row.get("ifindex") == index and row.get("queue-type") == "tx"}
    return {"私有项": private, "MAC一组": mac, "RMON一组": {key: value for key, value in rmon.items() if not isinstance(value, list)},
            "收的格子": bins["rx"], "发的格子": bins["tx"], "暂停帧一组": pause, "每条发送队列": queues}


def router_stats(port, prompt, folder, name, netdev):
    """E-0004 的 router_stats 的替代：把一个口的全部计数读成 {名字: 数}。

    标准的统计、每队列统计里的数照老名字放一份（例如 MAC 一组的 OctetsTransmittedOK 也叫 tx_bytes，
    每队列统计里第 n 条发送队列的包数也叫 tx_ring<n>_packets），E-0004、E-0005 的阶段照老名字取。
    """
    reading = read_port(port, prompt, folder, name, netdev)
    stats = dict(reading["私有项"])
    stats.update({MAC_OLD_NAMES[key]: value for key, value in reading["MAC一组"].items() if key in MAC_OLD_NAMES})
    stats.update({RMON_OLD_NAMES[key]: value for key, value in reading["RMON一组"].items() if key in RMON_OLD_NAMES})
    stats["rx_jabbers"] = reading["RMON一组"].get("etherStatsJabbers", 0)
    for side, key in (("rx", "收的格子"), ("tx", "发的格子")):
        stats.update({f"{side}_{BIN_OLD_NAMES.get(span, span)}": value for span, value in reading[key].items()})
    stats["rx_pause"], stats["tx_pause"] = reading["暂停帧一组"]["rx_pause_frames"], reading["暂停帧一组"]["tx_pause_frames"]
    for queue, row in reading["每条发送队列"].items():
        stats[f"tx_ring{queue}_packets"], stats[f"tx_ring{queue}_stopped"], stats[f"tx_ring{queue}_woken"] = \
            row["包数"], row["停"], row["叫醒"]
    return stats


def snapshot_delta(before, after):
    """E-0004 的 snapshot_delta，另外加上 PPE 里这个口各条出口队列丢的合计、环上的账对不上的三个计数、读统计晚了的次数。"""
    delta = _snapshot_delta(before, after)
    for name, row in delta["路由器的口"].items():
        old, new = before["口"][name], after["口"][name]
        row["PPE里各条出口队列丢的合计"] = sum(new[key] - old[key] for key in new if key.startswith("ppe_queue"))
        row["环上的账对不上的计数涨的"] = {key: new[key] - old[key] for key in new
                                  if key.endswith(("_bad_page", "_fill_empty", "_no_packet")) and new[key] != old[key]}
        row["读统计晚了的次数涨的"] = new.get("stats_read_late", 0) - old.get("stats_read_late", 0)
    return delta


def sending_oddities(reading, out_port, port_speed, many_flows):
    """E-0004 的 sending_oddities 去掉"每条发送队列都被停过"那一样：环加长以后它不成立。"""
    sent = reading["路由器的口"][out_port]["每个发包环发了多少"]
    odd = e4.load_oddities(reading, port_speed, sent if many_flows else None)
    if not many_flows:
        odd = [item for item in odd if "协议栈" not in item]
    dropped = reading["路由器的口"][out_port]["发送队列丢的"]
    if dropped > reading["路由器的口"][out_port]["MAC发出去的帧"]:
        odd.append(f"{out_port} 的发送队列丢了 {dropped} 个，比发出去的帧还多")
    return odd


def books(before, after):
    """E-0005 的三本账，每个口另外加两样：ip -s link 的 tx_dropped 涨的、ethtool -S 里各条出口队列丢的合计。"""
    rows, delta = _books(before, after)
    for name, row in rows.items():
        if name != "wan":
            # 千兆 MAC 发出去的帧数里本来就不含它自己发的暂停帧（试跑里读到的：减了以后正好差出暂停帧的个数），不减。
            sent = delta["路由器的口"][name]["MAC发出去的帧"]
            row["这个口的MAC发出去的帧"], row["MAC自己发的暂停帧（已经减掉）"] = sent, 0
            row["发包环收到的减MAC发出去的"] = row["发包环收到的帧"] - sent
        row["口自己记的发送丢的涨"] = delta["路由器的口"][name]["口自己记的发送丢的"]
        row["ethtool里各条出口队列丢的合计"] = delta["路由器的口"][name]["PPE里各条出口队列丢的合计"]
    return rows, delta


def iperf(folder, name, target, source, seconds, flows, udp=False, reverse=False, netns=None, rate="0", bind_port=5201):
    """E-0004 的 iperf 的替代：放在网络命名空间里跑的时候前面带 timeout，到时候它自己收掉 iperf3。

    原来的写法超时只杀得掉最外面的 sudo，命名空间里的 iperf3 还活着，命名空间删不掉，网卡回不到主机。
    """
    argv = e4.iperf_argv(target, source, seconds, flows, udp, reverse, server_port=bind_port, rate=rate)
    if netns:
        argv = ["sudo", "-n", "ip", "netns", "exec", netns, "timeout", "-k", "5", str(seconds + 60)] + argv
    done = subprocess.run(argv, capture_output=True, text=True, timeout=seconds + 120)
    with open(os.path.join(folder, name + ".json"), "x") as file:
        file.write(done.stdout or done.stderr)
    return e4.iperf_report(done.stdout, done.stderr)


def set_host_mtu(folder, tag, interface, mtu):
    """E-0004 的 set_host_mtu 的替代：eno1 是 br-lan 的成员，两个一起改；br-lan 这时不在，就只改 eno1。"""
    names = [interface]
    if interface == "eno1":
        names = ["eno1", "br-lan"] if mtu > 1500 else ["br-lan", "eno1"]
        names = [name for name in names if os.path.exists(f"/sys/class/net/{name}")]
    for name in names:
        code, _ = e3.host(["sudo", "-n", "ip", "link", "set", "dev", name, "mtu", str(mtu)],
                          folder, f"{tag}-主机{name}的MTU改成{mtu}")
        if code:
            raise RuntimeError(f"{tag}：主机 {name} 的 MTU 没有改成")


_snapshot_delta, _books = e4.snapshot_delta, e5.books
e4.iperf, e4.set_host_mtu = iperf, set_host_mtu
e4.snapshot_delta, e4.sending_oddities, e5.books, e4.router_stats = snapshot_delta, sending_oddities, books, router_stats


def devlink(port, prompt, folder, name, command, timeout=30):
    """在路由器上执行一条带 -j 的 devlink 命令，返回读成的对象；命令没有做成时返回 {"没有做成": 输出, "退出码": 数}。"""
    outcome = lab.serial_command(port, f"devlink -j {command}", prompt, timeout)
    with open(os.path.join(folder, name + ".txt"), "x") as file:
        file.write(outcome.output)
    if not outcome.completed:
        raise RuntimeError(f"{name}：命令没有在 {timeout} 秒里做完")
    text = outcome.output.strip()
    if outcome.exit_code:
        return {"没有做成": text[-300:], "退出码": outcome.exit_code}
    return json.loads(text.split("\n")[-1]) if text else {}


def device_name(port, prompt, folder, tag):
    """devlink 设备的名字（只有一台）。"""
    devices = list(devlink(port, prompt, folder, f"{tag}-设备", "dev show").get("dev", {}))
    if len(devices) != 1:
        raise RuntimeError(f"devlink dev show 列出了 {len(devices)} 台设备，要的是 1 台")
    return devices[0]


def health(port, prompt, folder, tag):
    """三个健康报告现在的样子：{报告的名字: devlink health show 的那一行}。"""
    shown = devlink(port, prompt, folder, f"{tag}-健康报告", "health show").get("health", {})
    return {row["reporter"]: row for rows in shown.values() for row in rows}


def diagnose(port, prompt, folder, tag, device, reporter):
    """一个健康报告的诊断。"""
    return devlink(port, prompt, folder, f"{tag}-诊断-{reporter}", f"health diagnose {device} reporter {reporter}")


def dump(port, prompt, folder, tag, device, reporter):
    """一个健康报告存着的现场。"""
    return devlink(port, prompt, folder, f"{tag}-现场-{reporter}", f"health dump show {device} reporter {reporter}", 60)


def log_lines(port, prompt, folder, tag, pattern):
    """内核记录里带这几个字的行，返回 [(时刻秒, 这一行)]。"""
    text = ask(port, prompt, folder, tag, f"dmesg | grep -E '{pattern}'; true", 30)
    rows = []
    for line in text.split("\n"):
        found = re.match(r"\[\s*(\d+\.\d+)\]", line)
        if found:
            rows.append((float(found.group(1)), line))
    return rows


def log_count(port, prompt, folder, tag):
    """内核记录现在一共多少行。"""
    return int(ask(port, prompt, folder, tag, "dmesg | wc -l", 20))


def log_since(port, prompt, folder, tag, count):
    """内核记录里从第 count 行以后新增的行。"""
    text = ask(port, prompt, folder, tag, f"dmesg | tail -n +{count + 1}; true", 30)
    return [line for line in text.split("\n") if line]


def most_in_window(times, seconds):
    """这些时刻里，任何一段 seconds 秒里最多有几个。判据照这个数判。"""
    return max((sum(1 for other in times if start <= other < start + seconds) for start in times), default=0)


def most_in_limit_window(times, seconds):
    """照内核限速的分段数这些时刻，只报数：从一段的头一行起算 seconds 秒是一段，过了这一段的下一行另起一段。返回各段里最多的行数。"""
    most, count, start = 0, 0, None
    for when in sorted(times):
        if start is None or when >= start + seconds:
            start, count = when, 0
        count += 1
        most = max(most, count)
    return most


def tx_stops(port, prompt, folder, tag, name):
    """每队列统计里一个口各条发送队列"停""叫醒"的次数：{队列号: [停, 叫醒]}。"""
    index = int(ask(port, prompt, folder, f"{tag}-{name}的编号", f"cat /sys/class/net/{name}/ifindex", 15))
    text = ask(port, prompt, folder, f"{tag}-每队列统计",
               f"{YNL} --dump qstats-get --json '{{\"scope\": \"queue\"}}' --output-json", 60)
    rows = json.loads(text.split("\n")[-1])
    return {row["queue-id"]: [row.get("tx-stop"), row.get("tx-wake")] for row in rows
            if row.get("ifindex") == index and row.get("queue-type") == "tx"}


def ring_use(diagnosis):
    """诊断里每个发包环占着几格：{"口号-队列号": 格数}。"""
    return {f"{ring['port']}-{ring['queue']}": (ring["prod"] - ring["cons"]) % TX_RING_SLOTS
            for ring in diagnosis.get("tx_rings", [])}


def rates_mbit(report):
    """iperf3 每个间隔的速率，兆比特每秒。"""
    return [round(rate / 1000000) for rate in report["每10秒的速率"]]


NM_BRIDGE = "br-lan"
NM_ITEMS = ("ipv4.method", "ipv4.addresses", "ipv6.method")


def nm_settings(folder, name):
    """NetworkManager 里 br-lan 这个连接的三项设置：{名字: 值}。只读。"""
    _, text = e3.host(["nmcli", "-t", "-f", ",".join(NM_ITEMS), "connection", "show", NM_BRIDGE], folder, name)
    return dict(line.split(":", 1) for line in text.strip().split("\n") if ":" in line)


def host_hold(args):
    """把 NetworkManager 里 br-lan 的连接改成固定用测试地址，连上，等桥开始转发。改以前的设置只存第一次的。"""
    os.makedirs(args.output)
    saved = os.path.join(args.work, "主机br-lan的连接-改以前.json")
    before = nm_settings(args.output, "改以前的设置")
    if not os.path.exists(saved):
        with open(saved, "x") as file:
            json.dump(before, file, ensure_ascii=False)
    steps = {"改设置": e3.host(["sudo", "-n", "nmcli", "connection", "modify", NM_BRIDGE, "ipv4.method", "manual",
                             "ipv4.addresses", f"{e4.BRIDGE_ADDRESS}/24", "ipv6.method", "disabled"], args.output, "改设置")[0],
             "连上": e3.host(["sudo", "-n", "nmcli", "connection", "up", NM_BRIDGE], args.output, "连上", timeout=120)[0]}
    # 桥刚建出来的头三十秒不转发（生成树在听、在学），等过去。
    time.sleep(40)
    held = os.path.exists("/sys/class/net/br-lan") and e3.bridge_address_present()
    member = os.path.exists("/sys/class/net/br-lan/brif/eno1")
    return {"代号": "E-0006", "这一次": args.label, "阶段": "hosthold", "读完": True,
            "改以前的设置": before, "改以后的设置": nm_settings(args.output, "改以后的设置"), "每一步的退出码": steps,
            "判据": [{"编号": "开工", "看什么": "主机的 br-lan 在、带着测试地址、eno1 在桥里",
                    "通过": held and member and not any(steps.values()), "br-lan在而且有测试地址": held, "eno1在桥里": member}]}


def host_release(args):
    """把 NetworkManager 里 br-lan 的连接照存下来的改回去、断开；eno1 的 MTU 改回 1500。输出放在 --output 加"-主机"的文件夹里。"""
    folder = args.output + "-主机"
    os.makedirs(folder)
    saved = os.path.join(args.work, "主机br-lan的连接-改以前.json")
    if not os.path.exists(saved):
        return {"NetworkManager里br-lan的连接": "这一次没有改过"}
    with open(saved) as file:
        before = json.load(file)
    argv = ["sudo", "-n", "nmcli", "connection", "modify", NM_BRIDGE]
    for item in NM_ITEMS:
        argv += [item, before.get(item, "")]
    steps = {"改回去": e3.host(argv, folder, "改回去")[0],
             "断开": e3.host(["sudo", "-n", "nmcli", "connection", "down", NM_BRIDGE], folder, "断开", timeout=120)[0]}
    time.sleep(3)
    set_host_mtu(folder, "收工", "eno1", 1500)
    after = nm_settings(folder, "改回去以后的设置")
    with open("/sys/class/net/eno1/mtu") as file:
        mtu = int(file.read())
    return {"改以前的设置": before, "改回去以后的设置": after, "设置和改以前一样": after == before, "每一步的退出码": steps,
            "eno1的MTU": mtu}


PIN_TX_THREADS = ('n=0; for p in $(for c in /proc/[0-9]*/comm; do read -r x < $c 2>/dev/null || continue; '
                  'case "$x" in napi/edma_tx-*) q=${c#/proc/}; echo ${q%/comm};; esac; done | sort -n); do '
                  'taskset -p $(printf %x $((1 << (n % 4)))) $p >/dev/null; n=$((n + 1)); done')


def stage_setup(port, prompt, args, result):
    """E-0005 的 setup。加载时带了 exp_tx_thread=1 的，另外把收"发完"的线程照进程号从小到大各绑到 0、1、2、3 号核心上：
    开机脚本只绑收包的线程；进程号最小的是第 0 个发完环的线程，它的中断绑在 0 号核心上。"""
    e5.stage_setup(port, prompt, args, result)
    if "exp_tx_thread=1" in args.insmod:
        ask(port, prompt, args.output, "绑收发完的线程", PIN_TX_THREADS, 20)
        result["绑了以后的线程"] = ask(port, prompt, args.output, "绑了以后的线程", "th", 20).split("\n")
        result["要看的数"]["线程"] = result["绑了以后的线程"]


def stage_tools(port, prompt, args, result):
    """把 工作文件夹/工具/e6tools.tgz 传到路由器、解到内存里的根上，试读一次每队列统计。"""
    folder, tools = args.output, os.path.join(args.work, "工具")
    # test-network.py 加了测试地址以后，eno2、USB 网卡还归 NetworkManager 管着，带着它从路由器要来的地址和路由，
    # 主机发给路由器的包会走错网卡。先叫 NetworkManager 不管这两块网卡，清掉地址，只留测试地址。
    for netns, nic, address in ((e5.NETNS_A, "eno2", e4.ENO2_ADDRESS), (e5.NETNS_B, e4.USB, e4.USB_ADDRESS)):
        e5.out_of_netns(folder, netns, nic, address)
    paths = {name: e4.wait_ping(folder, f"等{name}通", source, target) for name, _interface, source, target in e4.PATHS[:1]}
    sent_before = {nic: e3.host_stat(nic, "tx_packets") for nic in ("eno2", e4.USB)}
    e4.host_ping(folder, "核走哪块网卡", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 50, None, 0.02)
    went = {nic: e3.host_stat(nic, "tx_packets") - count for nic, count in sent_before.items()}
    with open(os.path.join(tools, "e6tools.tgz"), "rb") as file:
        wanted = hashlib.md5(file.read()).hexdigest()
    ask(port, prompt, folder, "路由器的桥上的测试地址", f"ip addr replace {e4.ROUTER_USB_SIDE}/24 dev br-lan", 15)
    server = subprocess.Popen([sys.executable, "-m", "http.server", "--bind", e4.USB_ADDRESS, "--directory", tools, "8000"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        ask(port, prompt, folder, "取工具", f"uclient-fetch -q -O /tmp/e6tools.tgz http://{e4.USB_ADDRESS}:8000/e6tools.tgz", 180)
    finally:
        # 边界：文件服务不管取没取成都要停掉。
        lab.stop_process(server, 15, 5)
    if ask(port, prompt, folder, "取到的工具的校验值", "md5sum /tmp/e6tools.tgz | cut -c1-32", 30) != wanted:
        raise RuntimeError("取到路由器上的工具和主机上的不一样")
    ask(port, prompt, folder, "解开", "tar -xzf /tmp/e6tools.tgz -C / && rm /tmp/e6tools.tgz", 180)
    version = ask(port, prompt, folder, "python的版本", "python3 -c 'import sys, yaml; print(sys.version.split()[0])'", 60)
    stops = tx_stops(port, prompt, folder, "试读", "lan2")
    result["判据"] = []
    result["读数里不对劲的地方"] = [] if stops else ["每队列统计里没有读到 lan2 的发送队列"]
    if went["eno2"] < 50 or went[e4.USB] > 25:
        result["读数里不对劲的地方"].append(f"主机从 {e4.ENO2_ADDRESS} 发给路由器的 50 个 ping 没有都走 eno2：两块网卡各多发了 {went}")
    result["要看的数"] = {"lan2这一路通了": paths, "50个ping两块网卡各多发了几帧": went, "python的版本": version, "lan2各条发送队列的停和叫醒": stops,
                       "内存（kB）": ask(port, prompt, folder, "内存", "grep -E '^(MemFree|MemAvailable):' /proc/meminfo", 15).split("\n")}


def stage_stdstats(port, prompt, args, result):
    """4-14、4-15：主机经 eno2 ping 路由器 1000 个带 200 字节数据的包（整帧 242 字节，不算校验），前后读 lan2 的全部计数。"""
    folder = args.output
    e4.host_ping(folder, "先ping通", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 3, None, 0.2)
    before = read_port(port, prompt, folder, "以前-lan2", "lan2")
    pinged = list(e4.host_ping(folder, "ping1000个", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 1000, 200, 0.01))
    time.sleep(1)
    after = read_port(port, prompt, folder, "以后-lan2", "lan2")
    wan = read_port(port, prompt, folder, "wan", "wan")
    private = after["私有项"]
    mac_names = sorted(key for key in private if not re.match(r"(rx_ring|tx_ring|tx_cmpl|ppe_queue)\d|stats_read_late", key))
    rise = {key: {span: after[key][span] - before[key][span] for span in after[key]} for key in ("收的格子", "发的格子")}
    others = sum(value for spans in rise.values() for span, value in spans.items() if span != "128-255")
    octets = {key: after["MAC一组"][key] - before["MAC一组"][key] for key in ("OctetsReceivedOK", "OctetsTransmittedOK")}
    sent = (after["私有项"]["tx_unicast"] - before["私有项"]["tx_unicast"]
            + sum(after["MAC一组"][key] - before["MAC一组"][key] for key in ("MulticastFramesXmittedOK", "BroadcastFramesXmittedOK")))
    queued = {queue: after["每条发送队列"][queue]["包数"] - row["包数"] for queue, row in before["每条发送队列"].items()}
    result["判据"] = [
        {"编号": "4-14", "看什么": "MAC 的计数在标准的三组统计里，ethtool -S 里只留四项",
         "通过": mac_names == sorted(MAC_PRIVATE) and len(after["MAC一组"]) == 16 and len(after["RMON一组"]) == 4
         and len(after["收的格子"]) == 7 and len(after["发的格子"]) == 7 and len(wan["收的格子"]) == 6 and len(wan["发的格子"]) == 6
         and sorted(after["暂停帧一组"]) == ["rx_pause_frames", "tx_pause_frames"]
         and rise["收的格子"]["128-255"] >= 1000 and rise["发的格子"]["128-255"] >= 1000 and others < 100 and pinged == [1000, 1000],
         "ping发了回了": pinged, "ethtool -S lan2 里 MAC 的计数的名字": mac_names,
         "MAC一组的名字": sorted(after["MAC一组"]), "RMON一组的四个数": after["RMON一组"],
         "lan2收、发各有哪几格": [sorted(after["收的格子"]), sorted(after["发的格子"])],
         "wan收、发各有哪几格": [sorted(wan["收的格子"]), sorted(wan["发的格子"])],
         "暂停帧一组": after["暂停帧一组"], "各格涨的": rise, "128到255以外各格涨的合计": others,
         "只报数：收、发的字节数涨的": octets},
        {"编号": "4-15", "看什么": "每条发送队列的包数在每队列统计里，ethtool -S 里没有",
         "通过": not any(re.fullmatch(r"tx_ring\d+_packets", key) for key in private) and "rx_ring0_packets" in private
         and abs(sum(queued.values()) - sent) <= 20,
         "ethtool -S lan2 里带 ring 的名字": sorted(key for key in private if "ring" in key),
         "每队列统计里各条发送队列的包数涨的": queued, "MAC发出去的帧涨的": sent,
         "lan2自己发的暂停帧涨的": after["暂停帧一组"]["tx_pause_frames"] - before["暂停帧一组"]["tx_pause_frames"]}]
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {"判据": {check["编号"]: check["通过"] for check in result["判据"]},
                       "收、发的字节数涨的": octets, "各格涨的": rise}


def stage_info(port, prompt, args, result):
    """只读命令的输出就判得了的几条。"""
    folder, checks = args.output, []
    present = lab.serial_command(port, "which devlink", prompt, 15)
    shown = devlink(port, prompt, folder, "5-1-设备", "dev show").get("dev", {})
    info = devlink(port, prompt, folder, "5-1-设备信息", "dev info").get("info", {})
    drivers = [row.get("driver") for row in info.values()]
    checks.append({"编号": "5-1", "看什么": "镜像里带 devlink 命令，登记了一台 devlink 设备",
                   "通过": present.exit_code == 0 and len(shown) == 1 and all(name.startswith("platform/") for name in shown)
                   and drivers == ["ipq807x-eth"],
                   "devlink命令在": present.output.strip(), "设备": list(shown), "设备信息里驱动的名字": drivers})
    device = list(shown)[0]

    ports = devlink(port, prompt, folder, "5-2-端口", "port show").get("port", {})
    by_number = {row.get("port"): row for row in ports.values()}
    names = sorted(ask(port, prompt, folder, "5-2-网口的名字", "cd /sys/class/net && echo *; cd /", 15).split())
    sysfs = dict(line.split() for line in ask(
        port, prompt, folder, "5-2-物理口名",
        'for p in lan1 lan2 lan3 lan4 wan; do echo "$p $(cat /sys/class/net/$p/phys_port_name)"; done', 20).split("\n"))
    checks.append({"编号": "5-2", "看什么": "五个口各有一个 devlink 端口",
                   "通过": len(ports) == 5
                   and all(by_number.get(number, {}).get("netdev") == name and by_number[number].get("flavour") == "physical"
                           and "lanes" not in by_number[number] for name, number in PORT_NUMBER.items())
                   and sysfs == {name: f"p{number}" for name, number in PORT_NUMBER.items()}
                   and all(name in names for name in PORT_NUMBER),
                   "端口": {key: {item: row.get(item) for item in ("netdev", "flavour", "port", "lanes", "splittable", "type")}
                          for key, row in ports.items()},
                   "物理口名": sysfs, "网口的名字": names})

    switch = dict(line.split(" ", 1) for line in ask(
        port, prompt, folder, "5-3-交换机的编号",
        'for p in lan1 lan2 lan3 lan4 wan; do echo "$p $(cat /sys/class/net/$p/phys_switch_id 2>&1)"; done', 20).split("\n"))
    e4.usb_into_netns(folder)
    try:
        linked = e4.wait_ping(folder, "5-3-等线连上", e4.USB_NETNS_ADDRESS, e4.ENO2_ADDRESS, e4.NETNS)
        _, text = e3.host(["sudo", "-n", "ip", "netns", "exec", e4.NETNS, "ping", "-n", "-q", "-c", "100", "-i", "0.01", "-W", "1",
                           "-I", e4.USB_NETNS_ADDRESS, e4.ENO2_ADDRESS], folder, "5-3-经路由器的桥ping")
        bridged = e3.parse_ping(text)
    finally:
        # 边界：USB 网卡不管怎样都要放回来。
        e4.usb_out_of_netns(folder)
    counts = dict(line.split() for line in ask(
        port, prompt, folder, "5-3-两个口的收发",
        'for p in lan2 lan3; do echo "$p $(cat /sys/class/net/$p/statistics/rx_packets)"; done', 20).split("\n"))
    checks.append({"编号": "5-3", "看什么": "五个口读到同一个交换机的编号；桥照旧在软件里转发",
                   "通过": len(set(switch.values())) == 1 and re.fullmatch(r"[0-9a-f]+", list(switch.values())[0]) is not None
                   and bridged == (100, 100),
                   "五个口的phys_switch_id": switch, "放进命名空间以后连上了": linked,
                   "主机两块网卡经路由器的桥ping（发了、回了）": list(bridged), "lan2和lan3到这时收的包": counts})

    rows = health(port, prompt, folder, "5-4")
    hw = rows.get("edma_hw", {})
    checks.append({"编号": "5-4", "看什么": "三个健康报告",
                   "通过": sorted(rows) == sorted(REPORTERS)
                   and all(row.get("state") == "healthy" and row.get("error") == 0 for row in rows.values())
                   and hw.get("auto_recover") is True and hw.get("grace_period") == 5000
                   and not any(rows.get(name, {}).get("auto_recover") for name in ("edma_ring", "edma_rxfill")),
                   "三个报告": rows})

    flush = ask(port, prompt, folder, "4-2-清地址表超时的行数", "dmesg | grep -c 'flushing the address table timed out'; true", 20)
    checks.append({"编号": "4-2", "看什么": "加载时清地址表没有超时", "通过": flush == "0", "内核记录里的行数": flush})

    threads = ask(port, prompt, folder, "4-3-线程", "th", 20).split("\n")
    checks.append({"编号": "4-3（线程）", "看什么": "收发完的轮询不在线程里",
                   "通过": not any("edma_tx" in line for line in threads) and sum("edma_rx" in line for line in threads) == 4,
                   "收发的线程": threads})

    stats = read_port(port, prompt, folder, "4-13-lan2的名字", "lan2")["私有项"]
    checks.append({"编号": "4-13（名字）", "看什么": "ethtool -S 里没有发送队列被停的次数，有收包环拿不到缓冲的次数",
                   "通过": not any(key.endswith("_stopped") for key in stats) and "rx_ring0_no_buffer" in stats
                   and "ppe_queue0_drop" in stats and "stats_read_late" in stats and "tx_cmpl0_no_packet" in stats,
                   "ethtool -S lan2 的名字": sorted(stats)})
    result["设备"] = device
    result["判据"] = checks
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {check["编号"]: check["通过"] for check in checks}


def stage_pause(port, prompt, args, result):
    """4-1、4-13：E-0004 的"一个口发不动"（主机用暂停帧压住 lan2 25 秒），前后另外读内核记录、每队列统计。"""
    folder = args.output
    lines = log_count(port, prompt, folder, "以前-内核记录的行数")
    stops_before = tx_stops(port, prompt, folder, "以前", "lan2")
    args.inner, args.kind = "stall", "pause"
    e5.stage_run(port, prompt, args, result)
    stops_after = tx_stops(port, prompt, folder, "以后", "lan2")
    new_lines = log_since(port, prompt, folder, "以后-内核记录新增的行", lines)
    held = {moment: {key: value for key, value in rows.items() if key.startswith("lan2 ")}
            for moment, rows in result["压着没发完的字节（只列不是0的）"].items()}
    pause_rise = result["路由器的口"]["lan2"]["收到的暂停帧"]
    ping_after = result["放开以后三个口ping"]["lan2"]
    timeouts = result["发送队列超时的次数里出现过的值"]
    stop_rise = {queue: stops_after[queue][0] - stops_before[queue][0] for queue in stops_before
                 if stops_after.get(queue) and stops_before[queue][0] is not None}
    # 补做（上机开始以后加的）：压着 lan2 的时候路由器往 lan2 发 64 字节的 UDP。小包占的字节少，
    # 内核给发送队列的字节上限拦不住，发包环才满得了。
    e4.iperf_servers(port, prompt, folder, "小包", (5202,))
    small_before = tx_stops(port, prompt, folder, "小包以前", "lan2")
    small = subprocess.Popen(e4.iperf_argv(e4.ROUTER_LAN, e4.ENO2_ADDRESS, 25, 8, udp=True, reverse=True, server_port=5202,
                                           length=64, rate="0"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    blocker = None
    try:
        time.sleep(6)
        blocker = pauser("eno2", 12)
        time.sleep(6)
        small_rings = ring_use(diagnose(port, prompt, folder, "小包压着", device_name(port, prompt, folder, "小包"), "edma_hw"))
        small_frames = blocker.communicate(timeout=60)[0].strip()
        small_report = e4.iperf_report(e4.run_output(small, 90))
    finally:
        # 边界：流和发暂停帧的进程都要收掉。
        for process in (small, blocker):
            if process:
                lab.stop_process(process, 2, 5)
    ask(port, prompt, folder, "小包-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    small_after = tx_stops(port, prompt, folder, "小包以后", "lan2")
    small_rise = {queue: [after - before for before, after in zip(small_before[queue], small_after[queue])]
                  for queue in small_before if small_after.get(queue) and None not in small_before[queue] + small_after[queue]}
    # 再补一种（上机开始以后加的）：把 lan2 每条发送队列的字节上限的下限写得很大，内核就不拿字节上限拦这条队列，
    # 再压住 lan2，发包环才满得了。做完写回 0。
    limit_min = "/sys/class/net/lan2/queues/tx-*/byte_queue_limits/limit_min"
    e4.iperf_servers(port, prompt, folder, "放开上限", (5202,))
    open_before = tx_stops(port, prompt, folder, "放开上限以前", "lan2")
    ask(port, prompt, folder, "放开上限-写", f"for f in {limit_min}; do echo 100000000 > $f; done", 15)
    opened, blocker = None, None
    try:
        opened = subprocess.Popen(e4.iperf_argv(e4.ROUTER_LAN, e4.ENO2_ADDRESS, 30, e4.FLOWS, reverse=True, server_port=5202,
                                                interval=5), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(8)
        blocker = pauser("eno2", 12)
        time.sleep(6)
        open_rings = ring_use(diagnose(port, prompt, folder, "放开上限压着", device_name(port, prompt, folder, "放开上限"), "edma_hw"))
        open_frames = blocker.communicate(timeout=60)[0].strip()
        open_report = e4.iperf_report(e4.run_output(opened, 90))
    finally:
        # 边界：流和发暂停帧的进程要收掉，字节上限的下限不管怎样都要写回 0。
        for process in (opened, blocker):
            if process:
                lab.stop_process(process, 2, 5)
        lab.serial_command(port, f"for f in {limit_min}; do echo 0 > $f; done", prompt, 15)
    ask(port, prompt, folder, "放开上限-停iperf3", "killall iperf3 2>/dev/null; true", 20)
    limit_back = ask(port, prompt, folder, "放开上限-读回", f"cat {limit_min}", 15).split()
    open_after = tx_stops(port, prompt, folder, "放开上限以后", "lan2")
    open_rise = {queue: [after - before for before, after in zip(open_before[queue], open_after[queue])]
                 for queue in open_before if open_after.get(queue) and None not in open_before[queue] + open_after[queue]}
    open_ping = list(e4.host_ping(folder, "放开上限以后ping", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 100, None, 0.01))
    result["判据"] = [
        {"编号": "4-1", "看什么": "一个口被暂停帧压住 25 秒，内核不记超时",
         "通过": not new_lines and timeouts == ["0"] and pause_rise > 0
         and any(held[moment] for moment in ("堵着第一次", "堵着第二次")) and ping_after == [100, 100],
         "内核记录新增的行": new_lines, "tx_timeout里出现过的值": timeouts, "lan2收到的暂停帧涨": pause_rise,
         "压着时lan2各条发送队列压着的字节": held, "放开以后ping-lan2": ping_after},
        {"编号": "4-13", "看什么": "发送队列被停的次数在每队列统计里",
         "通过": bool(stop_rise) and sum(stop_rise.values()) > 0,
         "lan2各条发送队列的停和叫醒-以前": stops_before, "以后": stops_after, "停的次数涨": stop_rise},
        {"编号": "4-13（补做：压着的时候发小包）", "看什么": "发包环满了以后发送队列被停、放开以后被叫醒，次数在每队列统计里",
         "通过": any(rise[0] > 0 and rise[1] > 0 for rise in small_rise.values()),
         "lan2各条发送队列停和叫醒的次数涨的": small_rise, "压着时lan2的发包环占着几格（口号-队列号）":
         {ring: used for ring, used in small_rings.items() if ring.startswith(f"{PORT_NUMBER['lan2']}-")},
         "主机发了几帧暂停帧": small_frames, "小包那一组做完没有": small_report["做完"]},
        {"编号": "4-13（补做：把发送队列的字节上限放开）", "看什么": "发包环满了以后发送队列被停、放开以后被叫醒，次数在每队列统计里",
         "通过": any(rise[0] > 0 and rise[1] > 0 for rise in open_rise.values()) and open_ping == [100, 100],
         "lan2各条发送队列停和叫醒的次数涨的": open_rise, "压着时lan2的发包环占着几格（口号-队列号）":
         {ring: used for ring, used in open_rings.items() if ring.startswith(f"{PORT_NUMBER['lan2']}-")},
         "主机发了几帧暂停帧": open_frames, "每5秒的速率（兆比特每秒）": rates_mbit(open_report),
         "做完以后字节上限的下限读回": limit_back, "做完以后ping-lan2": open_ping}]
    result["要看的数"]["判据"] = {check["编号"]: check["通过"] for check in result["判据"]}


def diag_sampler_start(port, prompt, folder, device, count):
    """在路由器上起一个后台的小循环：每 3 秒把 edma_hw 的诊断记一行。"""
    ask(port, prompt, folder, "诊断-起后台的小循环",
        f"(for i in $(seq {count}); do devlink -j health diagnose {device} reporter edma_hw; echo; sleep 3; done "
        f"> {DIAG_FILE} 2>&1 &)", 15)


def diag_sampler_read(port, prompt, folder):
    """把后台记下来的诊断读回来：每个发包环最多占到几格，和硬件报错的几个计数最后是多少。"""
    text = ask(port, prompt, folder, "诊断-读回来", f"cat {DIAG_FILE} 2>/dev/null; true", 60)
    most, last = {}, {}
    for line in text.split("\n"):
        if not line.startswith("{"):
            continue
        last = json.loads(line)
        for ring, used in ring_use(last).items():
            most[ring] = max(most.get(ring, 0), used)
    return most, {key: last.get(key) for key in ("status", "unmasked", "errors", "no unmasked error")}


def stage_run(port, prompt, args, result):
    """做 E-0004 的一个阶段（E-0005 的 run），中间另外每 3 秒读一次 edma_hw 的诊断。"""
    folder = args.output
    device = device_name(port, prompt, folder, "以前")
    health_before = health(port, prompt, folder, "以前")
    diag_sampler_start(port, prompt, folder, device, args.samples)
    e5.stage_run(port, prompt, args, result)
    most, counts = diag_sampler_read(port, prompt, folder)
    health_after = health(port, prompt, folder, "以后")
    limits = result["另外读的"]["以后"]["发送队列的账"]
    result["每个发包环最多占到几格（口号-队列号）"] = most
    result["诊断里硬件报错的计数"] = counts
    result["健康报告"] = {"以前": health_before, "以后": health_after}
    look = result["要看的数"]
    look["每个发包环最多占到几格"] = most
    look["wan每条发送队列最多许压的字节"] = {key: value["最多许压"] for key, value in limits.items() if key.startswith("wan ")}
    look["三个健康报告的状态"] = {name: row.get("state") for name, row in health_after.items()}
    if any(used >= TX_RING_SLOTS - 16 for used in most.values()):
        result["读数里不对劲的地方"].append(f"有发包环占满过：{most}")
    if args.judge == "txrate":
        threads = ask(port, prompt, folder, "线程", "th", 20).split("\n")
        run = result["iperf3"]
        result["判据"] = [{"编号": "4-3", "看什么": "收发完不进线程，路由器往 wan 发 64 条 TCP 流",
                         "通过": not any("edma_tx" in line for line in threads) and run["做完"]
                         and run["重传"] is not None and run["重传"] < 1000,
                         "有没有收发完的线程": [line for line in threads if "edma_tx" in line],
                         "每10秒的速率（兆比特每秒）": rates_mbit(run), "重传": run["重传"]}]
        look["4-5只报数"] = {"每个发包环最多占到几格": most, "wan每条发送队列最多许压的字节": look["wan每条发送队列最多许压的字节"]}
    if args.judge == "drops":
        row = result["三本账"]["wan"]
        gap = row["发包环收到的减MAC发出去的"]
        counted = [row["口自己记的发送丢的涨"], row["ethtool里各条出口队列丢的合计"]]
        before = e4.router_stats(port, prompt, folder, "断开以前-wan", "wan")
        e4.toggle(port, prompt, folder, "断开再连上", "wan", False)
        e4.toggle(port, prompt, folder, "断开再连上", "wan", True)
        after = e4.router_stats(port, prompt, folder, "连上以后-wan", "wan")
        kept = all(after[key] >= before[key] for key in before if key.startswith("ppe_queue"))
        result["判据"] = [{"编号": "4-9", "看什么": "丢在 PPE 里的帧读得出来，三个数对得上",
                         "通过": gap > 0 and all(abs(gap - value) <= 20 for value in counted),
                         "发包环收到的减MAC发出去的": gap, "ip -s link 的 tx_dropped 涨的": counted[0],
                         "ethtool -S 里各条出口队列丢的合计": counted[1],
                         "口断开再连上以后每条出口队列丢的数没有变小": kept}]
    look["判据"] = {check["编号"]: check["通过"] for check in result["判据"]}


def stage_fwdmix(port, prompt, args, result):
    """4-4：转发的同时路由器自己往 wan 发 64 条 TCP 流。"""
    folder = args.output
    e3.router_addresses(port, prompt, folder, "旁边那一组", "wan")
    e3.ensure_bridge_address(folder, "旁边那一组")
    e4.firewall_open(port, prompt, folder, "旁边那一组", "wan")
    e4.iperf_servers(port, prompt, folder, "旁边那一组", (5202,))
    side = subprocess.Popen(e4.iperf_argv(e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 400, e4.FLOWS, reverse=True,
                                          server_port=5202, interval=5),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(5)
        args.inner = "forward"
        e5.stage_run(port, prompt, args, result)
    finally:
        # 边界：旁边那一组流要收掉，防火墙里临时放行的规则要删掉。
        lab.stop_process(side, 2, 5)
        lab.serial_command(port, "killall iperf3 2>/dev/null; true", prompt, 20)
    result["做完以后防火墙里带记号的规则还剩几条"] = e4.firewall_close(port, prompt, folder, "旁边那一组")
    # 旁边那一组往 wan 发的流量在读"以前"那一组数的时候一直在跑，路由器忙，读一个口要几十秒：
    # wan 的几样计数不是同一刻读的，这一遍 wan 的三本账不能比。转发的那一组走的 lan2 不受影响。
    result["读数里不对劲的地方"] = [item for item in result["读数里不对劲的地方"] if not item.startswith("wan：")]
    result["wan的三本账这一遍不能比"] = "旁边那一组往 wan 发的流量在读数的时候一直在跑，wan 的几样计数不是同一刻读的"
    ports = result["整个阶段前后涨了的"]["路由器的口"]
    sent_pause = {name: ports[name]["发出的暂停帧"] for name in ("wan", "lan2")}
    most = result["要看的数"].get("中间各口占着的缓冲最多到多少")
    result["判据"] = [{"编号": "4-4", "看什么": "转发的时候收包没有被挤：路由器的口自己不发暂停帧",
                     "通过": all(value == 0 for value in sent_pause.values()) and result["iperf3"]["做完"],
                     "走法": args.kind, "wan和lan2自己发的暂停帧涨": sent_pause,
                     "转发每10秒的速率（兆比特每秒）": rates_mbit(result["iperf3"]),
                     "路由器自己往wan发的包（旁边那一组）wan的MAC发出去的帧": ports["wan"]["MAC发出去的帧"],
                     "缓冲管理里1到6号口（收的一侧）最多占多少": most[8:14] if most else None}]
    result["要看的数"]["判据"] = {"4-4": result["判据"][0]["通过"]}


def stage_three(port, prompt, args, result):
    """4-6：路由器同时往 wan、lan2、lan3 各发 64 条 TCP 流 30 秒。只报数，两种配法各三遍以后照第 1 节的判法比。"""
    folder = args.output
    paths = (("wan", e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 5201), ("lan2", e4.ROUTER_LAN, e4.ENO2_ADDRESS, 5202),
             ("lan3", e4.ROUTER_USB_SIDE, e4.USB_ADDRESS, 5203))
    e3.router_addresses(port, prompt, folder, "三个口", "wan")
    e3.ensure_bridge_address(folder, "三个口")
    e4.firewall_open(port, prompt, folder, "三个口", "wan")
    e4.iperf_servers(port, prompt, folder, "三个口", tuple(path[3] for path in paths))
    cmpl_map = [f"{word:08x}" for word in read_registers(port, prompt, folder, "发包环对到哪个发完环", e4.REG_TXDESC2CMPL, 3)]
    before = e5.extras(port, prompt, folder, "以前")
    runs = []
    try:
        for _name, target, source, server_port in paths:
            runs.append(subprocess.Popen(e4.iperf_argv(target, source, 30, e4.FLOWS, reverse=True, server_port=server_port),
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        reports = {path[0]: e4.iperf_report(e4.run_output(run, 120)) for path, run in zip(paths, runs)}
    finally:
        # 边界：三组流都要收掉。
        for run in runs:
            lab.stop_process(run, 2, 5)
    after = e5.extras(port, prompt, folder, "以后")
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    left = e4.firewall_close(port, prompt, folder, "三个口")
    rows, delta = books(before, after)
    soft = [core["软中断"] for core in delta["四个核心各忙在哪里（百分之几）"]]
    rates = {name: rates_mbit(report) for name, report in reports.items()}
    result.update({"三组iperf3": reports, "三本账": rows, "整个阶段前后涨了的": delta, "发包环对到哪个发完环的三个字": cmpl_map,
                   "模块的参数": e5.module_params(port, prompt, folder, "这一遍"), "做完以后防火墙里带记号的规则还剩几条": left})
    result["判据"] = []
    result["读数里不对劲的地方"] = [f"{name} 那一组没有跑完" for name, report in reports.items() if not report["做完"]]
    result["要看的数"] = {"四个核心花在软中断上的百分比": soft, "最大和最小的差": round(max(soft) - min(soft), 1),
                       "三个口每10秒的速率（兆比特每秒）": rates,
                       "三个口速率的合计（三个间隔各自的和）": [sum(values) for values in zip(*rates.values())],
                       "三个口自己发的暂停帧": {name: delta["路由器的口"][name]["发出的暂停帧"] for name in rates},
                       "发完的中断各进了多少次": {name: count for name, count in delta["中断涨的"].items() if "txcmpl" in name},
                       "三本账里发包环收到的减MAC发出去的": {name: row["发包环收到的减MAC发出去的"] for name, row in rows.items()}}


def bridge_mtu(port, prompt, folder, tag, mtu):
    """路由器的桥和它的四个口、主机的 eno2 和 USB 网卡的 MTU 都设成 mtu，等两路都 ping 得通。"""
    members = ("lan1", "lan2", "lan3", "lan4") if mtu > 1500 else ()
    for nic in ("eno2", e4.USB):
        # 主机的网卡自己许设的最大 MTU 比要的小时（eno2 是 9216），设成它许设的最大数。
        e4.set_host_mtu(folder, f"{tag}-{mtu}", nic, min(mtu, e4.host_max_mtu(nic)))
    order = list(members) + ["br-lan"] if mtu > 1500 else ["br-lan", "lan1", "lan2", "lan3", "lan4"]
    ask(port, prompt, folder, f"{tag}-路由器的桥和四个口的MTU改成{mtu}",
        "; ".join(f"ip link set {name} mtu {mtu}" for name in order) + "; cat /sys/class/net/br-lan/mtu", 30)
    return {name: e4.wait_ping(folder, f"{tag}-{mtu}-等{name}通", source, target)
            for name, _interface, source, target in e4.PATHS[:2]}


def stage_jam(port, prompt, args, result):
    """4-7。"""
    folder = args.output
    if args.part == "two":
        linked = bridge_mtu(port, prompt, folder, "长帧", LONG_MTU)
        up_file = os.path.join(folder, "主机往wan发的那一组.json")
        up = subprocess.Popen(
            ["sh", "-c", f"until iperf3 -c {e4.ROUTER_BRIDGE_SIDE} -B {e4.BRIDGE_ADDRESS} -p 5204 -P {e4.FLOWS} -t 70 -i 5 -J "
                         f"> '{up_file}' 2>/dev/null; do sleep 1; done"])
        servers = e4.iperf_servers
        e4.iperf_servers = lambda port, prompt, folder, tag, ports=(5201,): servers(port, prompt, folder, tag, tuple(ports) + (5204,))
        try:
            args.kind = "pause-two"
            e4.stage_stall(port, prompt, args, result)
        finally:
            # 边界：换掉的函数要换回去，另外那一组流要收掉，MTU 不管怎样都要改回 1500。
            e4.iperf_servers = servers
            lab.stop_process(up, 2, 5)
            back = bridge_mtu(port, prompt, folder, "收尾", 1500)
        with open(up_file) as file:
            up_report = e4.iperf_report(file.read())
        start = int(result["时刻"]["开始堵（从两组流开始算的秒数）"] // 5) + 1
        down, up_rates = rates_mbit({"每10秒的速率": result["wan那一组每5秒的速率"]}), rates_mbit(up_report)
        free = result["PPE缓冲管理"]["放开以后"]["各口占着的缓冲"]
        overlap = {name: bool(rates[:2] and rates[start:start + 3]
                              and max(rates[start:start + 3]) >= min(rates[:2]) and min(rates[start:start + 3]) <= max(rates[:2]))
                   for name, rates in (("路由器往wan发", down), ("主机往wan发", up_rates))}
        result["判据"] = [{"编号": "4-7（压两个口）", "看什么": "lan2、lan3 发着长帧一起被压住，wan 两个方向不受影响",
                         "通过": all(overlap.values()) and result["堵着时ping-wan"] == [20, 20] and not any(free)
                         and result["lan3收到的暂停帧"] > 0
                         and result["路由器的口"]["lan2"]["收到的暂停帧"] > 0,
                         "MTU改成12262以后两路通了": linked, "改回1500以后两路通了": back,
                         "主机两块网卡实际设成的MTU（路由器这一头都是12262）": {nic: min(LONG_MTU, e4.host_max_mtu(nic)) for nic in ("eno2", e4.USB)},
                         "wan两个方向每5秒的速率（兆比特每秒）": {"路由器往wan发": down, "主机往wan发": up_rates},
                         "压着的那三个5秒是第几个起": start, "压着时和压以前的范围重叠": overlap,
                         "堵着时ping-wan": result["堵着时ping-wan"], "放开以后各口占着的缓冲": free,
                         "三个口收到的暂停帧": {"wan": result["wan收到的暂停帧"], "lan3": result["lan3收到的暂停帧"],
                                        "lan2": result["路由器的口"]["lan2"]["收到的暂停帧"]},
                         "lan2、lan3那两组每5秒的速率": [result["lan2那一组每5秒的速率"], result["lan3那一组每5秒的速率"]],
                         "压着时各口占着的缓冲": result["PPE缓冲管理"]["堵着第二次"]["各口占着的缓冲"]}]
        result["要看的数"]["判据"] = {"4-7（压两个口）": result["判据"][0]["通过"]}
        return

    if args.part == "pair":
        e3.router_addresses(port, prompt, folder, "对照", "wan")
        e3.ensure_bridge_address(folder, "对照")
        e4.firewall_open(port, prompt, folder, "对照", "wan")
        e4.iperf_servers(port, prompt, folder, "对照", (5201, 5204))
        before = e4.snapshot(port, prompt, folder, "对照-以前")
        runs = []
        try:
            runs.append(subprocess.Popen(e4.iperf_argv(e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 60, e4.FLOWS, reverse=True, interval=5),
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
            runs.append(subprocess.Popen(e4.iperf_argv(e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 60, e4.FLOWS, server_port=5204, interval=5),
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
            time.sleep(30)
            during = e4.bm_ports(port, prompt, folder, "对照-第30秒")
            reports = [e4.iperf_report(e4.run_output(run, 150)) for run in runs]
        finally:
            # 边界：两组流都要收掉。
            for run in runs:
                lab.stop_process(run, 2, 5)
        delta = e4.snapshot_delta(before, e4.snapshot(port, prompt, folder, "对照-以后"))
        ask(port, prompt, folder, "对照-停iperf3", "killall iperf3 2>/dev/null; true", 20)
        result.update(delta)
        result["做完以后防火墙里带记号的规则还剩几条"] = e4.firewall_close(port, prompt, folder, "对照")
        result["判据"] = []
        result["读数里不对劲的地方"] = [f"{name}那一组没有跑完" for name, report in zip(("路由器往wan发", "主机往wan发"), reports)
                                if not report["做完"]]
        result["要看的数"] = {"每5秒的速率（兆比特每秒）": {"路由器往wan发": rates_mbit(reports[0]), "主机往wan发": rates_mbit(reports[1])},
                           "wan自己发的暂停帧": delta["路由器的口"]["wan"]["发出的暂停帧"],
                           "wan收到的暂停帧": delta["路由器的口"]["wan"]["收到的暂停帧"],
                           "第30秒各口占着的缓冲": during["各口占着的缓冲"],
                           "四个核心各忙在哪里（百分之几）": delta["四个核心各忙在哪里（百分之几）"],
                           "线程各用了多少秒的核心时间": delta["线程各用了多少秒的核心时间"]}
        return

    paths = (("wan", e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 5201, "eno1", 0.002),
             ("lan2", e4.ROUTER_LAN, e4.ENO2_ADDRESS, 5202, "eno2", 0.01),
             ("lan3", e4.ROUTER_USB_SIDE, e4.USB_ADDRESS, 5203, e4.USB, 0.01))
    e3.router_addresses(port, prompt, folder, "压wan", "wan")
    e3.ensure_bridge_address(folder, "压wan")
    e4.firewall_open(port, prompt, folder, "压wan", "wan")
    e4.iperf_servers(port, prompt, folder, "压wan", (5201, 5202, 5203))
    before = e4.router_stats(port, prompt, folder, "压wan-以前", "wan")
    run = subprocess.Popen(e4.iperf_argv(paths[0][1], paths[0][2], 40, e4.FLOWS, reverse=True, interval=1),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    blocker = None
    try:
        time.sleep(15)
        blocker = pauser("eno1", 10, 0.002)
        frames = blocker.communicate(timeout=60)[0].strip()
        report = e4.iperf_report(e4.run_output(run, 120))
    finally:
        # 边界：流和发暂停帧的进程都要收掉。
        for process in (run, blocker):
            if process:
                lab.stop_process(process, 2, 5)
    after = e4.router_stats(port, prompt, folder, "压wan-以后", "wan")
    rates = rates_mbit(report)
    usual, pressed = rates[5:13], rates[17:23]
    stopped = bool(usual and pressed and max(pressed) <= 0.01 * min(usual))
    result.update({"每2毫秒一个暂停帧压wan": {"主机发了几帧": frames, "每秒的速率（兆比特每秒）": rates, "压以前的几秒": usual,
                                      "压着的几秒": pressed, "wan收到的暂停帧涨": after.get("rx_pause", 0) - before.get("rx_pause", 0),
                                      "压得住（压着的几秒都不到压以前最慢一秒的百分之一）": stopped}})
    three = None
    if stopped:
        e4.iperf_servers(port, prompt, folder, "三个口一起压", (5201, 5202, 5203))
        runs, blockers = [], []
        try:
            for _name, target, source, server_port, _nic, _interval in paths:
                runs.append(subprocess.Popen(e4.iperf_argv(target, source, 60, e4.FLOWS, reverse=True, server_port=server_port, interval=5),
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
            time.sleep(15)
            blockers = [pauser(nic, 25, interval) for _name, _target, _source, _server_port, nic, interval in paths]
            time.sleep(12)
            alive = lab.serial_command(port, "echo alive", prompt, 15)
            during = e4.bm_ports(port, prompt, folder, "三个口一起压-压着")
            for blocker in blockers:
                blocker.communicate(timeout=60)
            reports = {path[0]: e4.iperf_report(e4.run_output(one, 120)) for path, one in zip(paths, runs)}
        finally:
            # 边界：流和发暂停帧的进程都要收掉。
            for process in runs + blockers:
                lab.stop_process(process, 2, 5)
        time.sleep(10)
        e3.ensure_bridge_address(folder, "三个口一起压-放开以后")
        pings = {name: list(e4.host_ping(folder, f"三个口一起压-放开以后ping-{name}", source, target, 100, None, 0.01))
                 for name, _interface, source, target in e4.PATHS}
        free = e4.bm_ports(port, prompt, folder, "三个口一起压-放开以后")["各口占着的缓冲"]
        three = {"压着时串口还回话": alive.completed and "alive" in alive.output, "压着时各口占着的缓冲": during["各口占着的缓冲"],
                 "三组每5秒的速率（兆比特每秒）": {name: rates_mbit(report) for name, report in reports.items()},
                 "放开以后三个口ping": pings, "放开以后各口占着的缓冲": free,
                 "通过": alive.completed and all(pair == [100, 100] for pair in pings.values()) and not any(free)}
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    result["做完以后防火墙里带记号的规则还剩几条"] = e4.firewall_close(port, prompt, folder, "压wan")
    result["三个口一起压"] = three
    result["判据"] = [] if three is None else [{"编号": "4-7（三个口一起压）", "看什么": "三个口一起被压住再放开", **three}]
    result["读数里不对劲的地方"] = [] if report["做完"] else ["往 wan 发的那一组没有跑完"]
    result["要看的数"] = {"每2毫秒一个暂停帧压wan": result["每2毫秒一个暂停帧压wan"], "三个口一起压": three}


def stage_modtimer(port, prompt, args, result):
    """4-8：一种中断延时的配法上，ping 1000 个量往返时间，再两个方向各发 30 秒。只报数。"""
    folder = args.output
    timers = {"收包环": [f"{word:#x}" for word in read_registers(port, prompt, folder, "收包环12的中断延时", e4.EDMA + 0x49008 + 0x1000 * 12, 1)],
              "发完环": [f"{word:#x}" for word in read_registers(port, prompt, folder, "发完环4的中断延时", e4.EDMA + 0x21008 + 0x1000 * 4, 1)]}
    e4.host_ping(folder, "先ping通", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 3, None, 0.2)
    _, text = e3.host(["sudo", "-n", "ping", "-n", "-q", "-c", "1000", "-i", "0.01", "-W", "1", "-I", e4.ENO2_ADDRESS, e4.ROUTER_LAN],
                      folder, "ping1000个", timeout=120)
    found = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    e4.iperf_servers(port, prompt, folder, "两个方向")
    readings = {}
    for label, reverse in (("主机往lan2发", False), ("路由器往lan2发", True)):
        before = e4.snapshot(port, prompt, folder, f"{label}-以前")
        run = e4.iperf(folder, label, e4.ROUTER_LAN, e4.ENO2_ADDRESS, 30, e4.FLOWS, reverse=reverse)
        delta = e4.snapshot_delta(before, e4.snapshot(port, prompt, folder, f"{label}-以后"))
        readings[label] = {"每10秒的速率（兆比特每秒）": rates_mbit(run), "重传": run["重传"], "做完": run["做完"],
                           "收包环和发完环的中断各进了多少次": {name: count for name, count in delta["中断涨的"].items()
                                                  if "rxdesc" in name or "txcmpl" in name}}
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    result["判据"] = []
    result["读数里不对劲的地方"] = [f"{label} 没有跑完" for label, reading in readings.items() if not reading["做完"]]
    result["要看的数"] = {"模块的参数": e5.module_params(port, prompt, folder, "这一遍"), "寄存器里的中断延时": timers,
                       "ping发了回了": list(e3.parse_ping(text)),
                       "往返时间（最短、平均、最长，毫秒）": [float(found.group(index)) for index in (1, 2, 3)] if found else None,
                       **readings}


def stage_buf(port, prompt, args, result):
    """4-10、5-7、5-8。要先换上 variant-e6.ko（不带加载时的参数）。"""
    folder = args.output
    device = device_name(port, prompt, folder, "以前")
    lines = log_count(port, prompt, folder, "以前-内核记录的行数")
    stats_before = e4.router_stats(port, prompt, folder, "以前-lan2", "lan2")
    health_before = health(port, prompt, folder, "以前")
    during = []
    levels = e4.fill_levels

    def levels_and_health(port, prompt, folder, tag):
        """第 0 组不补缓冲的那一分钟里，每读一次空缓冲区环的缓冲数，就顺带读一次健康报告。"""
        if tag.startswith("21-第"):
            during.append({name: [row.get("state"), row.get("error")] for name, row in health(port, prompt, folder, tag).items()})
        return levels(port, prompt, folder, tag)

    e4.fill_levels = levels_and_health
    try:
        e4.stage_buffer(port, prompt, args, result)
    finally:
        # 边界：换掉的函数要换回去。
        e4.fill_levels = levels
    time.sleep(10)
    health_refilled = health(port, prompt, folder, "关掉10秒以后")
    stats_refilled = e4.router_stats(port, prompt, folder, "关掉以后-lan2", "lan2")
    fill_dump = dump(port, prompt, folder, "空缓冲区环", device, "edma_rxfill")
    fill_diag = diagnose(port, prompt, folder, "空缓冲区环", device, "edma_rxfill")
    empty_lines = [when for when, _line in log_lines(port, prompt, folder, "空缓冲区环空了的行", "fill ring .* is empty")]
    empty_rise = stats_refilled["rx_ring0_fill_empty"] - stats_before["rx_ring0_fill_empty"]

    e4.host_ping(folder, "改坏以前-先ping通", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 3, None, 0.2)
    ask(port, prompt, folder, "页的号改坏一次", f"echo 1 > {PARAM}/exp_bad_page", 15)
    e4.host_ping(folder, "页的号改坏一次-ping", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 20, None, 0.05)
    ask(port, prompt, folder, "发完登记的号改坏一次", f"echo 1 > {PARAM}/exp_bad_cmpl", 15)
    e4.host_ping(folder, "发完登记的号改坏一次-ping", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 20, None, 0.05)
    time.sleep(2)
    stats_after = e4.router_stats(port, prompt, folder, "改坏以后-lan2", "lan2")
    switches = e5.module_params(port, prompt, folder, "改坏以后")
    bad_page = sum(stats_after[key] - stats_refilled[key] for key in stats_after if key.endswith("_bad_page"))
    no_packet = sum(stats_after[key] - stats_refilled[key] for key in stats_after if key.endswith("_no_packet"))
    health_after = health(port, prompt, folder, "改坏以后")
    ring_dump = dump(port, prompt, folder, "账对不上", device, "edma_ring")
    ring_diag = diagnose(port, prompt, folder, "账对不上", device, "edma_ring")
    recover = devlink(port, prompt, folder, "对edma_ring执行恢复", f"health recover {device} reporter edma_ring")
    last_ping = list(e4.host_ping(folder, "最后ping", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 100, None, 0.01))
    new_lines = log_since(port, prompt, folder, "内核记录新增的行", lines)
    severe = [line for line in new_lines if re.search(SEVERE, line)]

    starved = [sample.get("edma_rxfill") for sample in during]
    ring_row, fill_row, hw_row = (health_after.get(name, {}) for name in ("edma_ring", "edma_rxfill", "edma_hw"))
    result["判据"] = [
        {"编号": "4-10", "看什么": "环上的账对不上的三样各有计数，内核记录限速",
         "通过": empty_rise > 0 and most_in_window(empty_lines, 5) <= 10 and bad_page == 1 and no_packet == 1
         and last_ping == [100, 100],
         "rx_ring0_fill_empty涨": empty_rise, "空缓冲区环空了的行数": len(empty_lines),
         "任何5秒里最多几行": most_in_window(empty_lines, 5),
         "只报数：照内核限速的分段数，一段里最多几行": most_in_limit_window(empty_lines, 5), "页对不上的计数涨": bad_page, "发完登记对不上的计数涨": no_packet,
         "两个一次性的开关读回": {key: switches.get(key) for key in ("exp_bad_page", "exp_bad_cmpl")},
         "最后ping": last_ping, "新增的警告和致命错误的行": severe},
        {"编号": "5-7", "看什么": "账对不上进 edma_ring，没有恢复",
         "通过": ring_row.get("state") == "error" and ring_row.get("error") in (1, 2) and hw_row.get("state") == "healthy"
         and "没有做成" in recover and "fault" in ring_dump and "ring" in ring_dump and "slot" in ring_dump,
         "edma_ring": ring_row, "edma_hw": hw_row, "现场": ring_dump, "诊断": ring_diag, "执行恢复的回话": recover},
        {"编号": "5-8", "看什么": "空缓冲区环空了进 edma_rxfill，补上以后回到健康",
         "通过": any(state and state[0] == "error" for state in starved)
         and health_refilled.get("edma_rxfill", {}).get("state") == "healthy"
         and health_refilled.get("edma_rxfill", {}).get("error", 0) > 0 and "empty_ring" in fill_dump
         and all(sample.get(name, ["healthy"])[0] == "healthy" for sample in during for name in ("edma_hw", "edma_ring")),
         "第0组不补缓冲的一分钟里每10秒三个报告的状态和出错次数": during, "关掉10秒以后": health_refilled,
         "以前": health_before, "现场": fill_dump, "诊断": fill_diag}]
    result["读数里不对劲的地方"] = result.get("读数里不对劲的地方", []) + ([f"内核记录里有新的警告：{severe}"] if severe else [])
    result["要看的数"] = dict(result.get("要看的数", {}), 判据={check["编号"]: check["通过"] for check in result["判据"]})


def stage_unknown(port, prompt, args, result):
    """4-11。要先换上 variant-e6.ko，加载时带 exp_maxframe=1。"""
    folder = args.output
    device = device_name(port, prompt, folder, "以前")
    irq_before = e3.interrupt_count(port, prompt, folder, "以前-edma_misc", "edma_misc")
    diag_before = diagnose(port, prompt, folder, "以前", device, "edma_hw")
    lines = log_count(port, prompt, folder, "以前-内核记录的行数")
    args.frame_port, args.jumbo_mtu, args.with_pause = "wan", 16334, False
    e4.stage_jumbo(port, prompt, args, result)
    irq_after = e3.interrupt_count(port, prompt, folder, "以后-edma_misc", "edma_misc")
    diag_after = diagnose(port, prompt, folder, "以后", device, "edma_hw")
    new_lines = log_since(port, prompt, folder, "内核记录新增的行", lines)
    logged = [when for when, _line in log_lines(port, prompt, folder, "没有认得的位的行", "holds no unmasked error")]
    disabled = [line for line in new_lines if "nobody cared" in line or "Disabling IRQ" in line]
    rise, counted = irq_after - irq_before, diag_after.get("no unmasked error", 0) - diag_before.get("no unmasked error", 0)
    known = {name: count - diag_before.get("errors", {}).get(name, 0) for name, count in diag_after.get("errors", {}).items()}
    result["判据"] = [{"编号": "4-11", "看什么": "状态里没有认得的位的中断只计数，内核不关这根中断",
                     "通过": rise == counted + sum(known.values()) and most_in_window(logged, 5) <= 10 and not disabled,
                     "edma_misc涨了": rise, "诊断里没有认得的位的次数涨了": counted, "诊断里九种错各涨了": known,
                     "内核记录里这种行（到这时一共）": len(logged), "任何5秒里最多几行": most_in_window(logged, 5),
                     "只报数：照内核限速的分段数，一段里最多几行": most_in_limit_window(logged, 5),
                     "内核关中断的行": disabled,
                     "三个健康报告": health(port, prompt, folder, "以后")}]
    result["要看的数"] = dict(result.get("要看的数", {}), 判据={"4-11": result["判据"][0]["通过"]},
                           edma_misc涨了=rise, 诊断里没有认得的位的次数涨了=counted)


def misc_registers(port, prompt, folder, tag):
    """报错中断的状态寄存器和屏蔽寄存器。"""
    status, mask = read_registers(port, prompt, folder, f"{tag}-状态和屏蔽", MISC_STAT, 2)
    return {"状态": f"{status:#x}", "屏蔽": f"{mask:#x}", "状态里有发包的内存满了这一位": bool(status & TX_SRAM_FULL),
            "屏蔽里这一位开着": bool(mask & TX_SRAM_FULL)}


def stage_sram(port, prompt, args, result):
    """4-12、5-6。要先换上 variant-e6.ko，加载时带 exp_maxframe=1。做完要重新内存引导。"""
    folder = args.output
    device = device_name(port, prompt, folder, "以前")
    name, interface, source, target = e4.PATHS[1]
    steps = {"以前": {"寄存器": misc_registers(port, prompt, folder, "以前"), "健康报告": health(port, prompt, folder, "以前"),
                    "edma_misc": e3.interrupt_count(port, prompt, folder, "以前-edma_misc", "edma_misc")}}
    lines = log_count(port, prompt, folder, "以前-内核记录的行数")
    e3.router_addresses(port, prompt, folder, "开头", "wan")
    e4.router_port_alone(port, prompt, folder, "开头", name, target)
    frame = e4.frame_try(port, prompt, folder, name, interface, source, target, 16357)

    def moment(tag):
        """这一刻的寄存器、中断次数、三个健康报告、内核记录里这种错的行、串口还回不回话。"""
        alive = lab.serial_command(port, "echo alive", prompt, 15)
        return {"串口还回话": alive.completed and "alive" in alive.output,
                "寄存器": misc_registers(port, prompt, folder, tag),
                "edma_misc": e3.interrupt_count(port, prompt, folder, f"{tag}-edma_misc", "edma_misc"),
                "健康报告": health(port, prompt, folder, tag),
                "内核记录里这种错的行的时刻": [when for when, _line in log_lines(port, prompt, folder, f"{tag}-这种错的行", "EDMA: TX SRAM full")]}

    steps["出错以后"] = moment("出错以后")
    steps["出错以后"]["现场"] = dump(port, prompt, folder, "出错以后", device, "edma_hw")
    steps["出错以后"]["诊断"] = diagnose(port, prompt, folder, "出错以后", device, "edma_hw")
    time.sleep(10)
    steps["等了10秒"] = moment("等了10秒")
    recover = devlink(port, prompt, folder, "手动恢复", f"health recover {device} reporter edma_hw")
    time.sleep(2)
    steps["手动恢复以后"] = dict(moment("手动恢复以后"), 恢复的回话=recover)
    ask(port, prompt, folder, "往状态寄存器写这一位", f"echo {TX_SRAM_FULL} > {PARAM}/exp_misc_stat", 15)
    steps["往状态寄存器写1以后"] = moment("往状态寄存器写1以后")
    recover = devlink(port, prompt, folder, "写1以后再手动恢复", f"health recover {device} reporter edma_hw")
    time.sleep(2)
    steps["写1以后再手动恢复"] = dict(moment("写1以后再手动恢复"), 恢复的回话=recover)
    new_lines = log_since(port, prompt, folder, "内核记录新增的行", lines)
    # 主机这一头改回去：USB 网卡的 MTU 回到 1500。路由器这一头做完要重新内存引导。
    e4.set_host_mtu(folder, "收尾", interface, 1500)

    first, waited = steps["出错以后"], steps["等了10秒"]
    rise = waited["edma_misc"] - steps["以前"]["edma_misc"]
    logged = len(waited["内核记录里这种错的行的时刻"])
    hw = waited["健康报告"].get("edma_hw", {})
    saved = first["现场"]
    looked = first["寄存器"]["状态里有发包的内存满了这一位"]
    result["每一步"] = steps
    result["发MTU16357的帧"] = frame
    result["内核记录新增的行"] = new_lines
    result["判据"] = [
        {"编号": "4-12", "看什么": "硬件报一种错，只关这一位，中断不再不停地进",
         "通过": frame["edma_misc的次数涨的"] > 0 and rise <= 2 and logged <= 2 and waited["寄存器"]["屏蔽"] == "0x1ef"
         and first["串口还回话"] and waited["串口还回话"],
         "edma_misc涨了": rise, "内核记录里这种错的行数": logged, "等了10秒以后的屏蔽寄存器": waited["寄存器"]["屏蔽"],
         "这一位关着的时候状态寄存器里读不读得到它": looked,
         "往状态寄存器写1以后的状态": steps["往状态寄存器写1以后"]["寄存器"]["状态"],
         "写1以后再手动恢复": {"屏蔽": steps["写1以后再手动恢复"]["寄存器"]["屏蔽"],
                         "edma_misc又涨了": steps["写1以后再手动恢复"]["edma_misc"] - steps["往状态寄存器写1以后"]["edma_misc"]}},
        {"编号": "5-6", "看什么": "edma_hw 报告这种错，存着现场，硬件还报着时恢复不成",
         "通过": hw.get("state") == "error"
         and ((looked and hw.get("error") == 1 and hw.get("recover") == 0 and logged == 1)
              or (not looked and hw.get("error") == 2 and hw.get("recover") == 1 and logged == 2))
         and saved.get("reported") == TX_SRAM_FULL and "tx_rings" in saved
         and all(waited["健康报告"].get(other, {}).get("state") == "healthy" for other in ("edma_ring", "edma_rxfill"))
         and steps["手动恢复以后"]["健康报告"].get("edma_hw", {}).get("state") == "error",
         "恢复的回调是哪一种写法的读数": "先看状态" if looked else "不看状态",
         "等了10秒以后的edma_hw": hw, "现场里报了的位": saved.get("reported"), "现场里上报时的状态": saved.get("status"),
         "两行之间隔多久（秒）": (round(waited["内核记录里这种错的行的时刻"][1] - waited["内核记录里这种错的行的时刻"][0], 3)
                          if logged == 2 else None),
         "手动恢复以后的edma_hw和屏蔽": [steps["手动恢复以后"]["健康报告"].get("edma_hw"), steps["手动恢复以后"]["寄存器"]["屏蔽"]],
         "手动恢复的回话": steps["手动恢复以后"]["恢复的回话"]}]
    result["读数里不对劲的地方"] = [] if frame["edma_misc的次数涨的"] else ["发 MTU 16357 的帧以后硬件报错的中断没有来：这一条没有量到"]
    result["要看的数"] = {"判据": {check["编号"]: check["通过"] for check in result["判据"]},
                       "每一步的寄存器": {tag: step["寄存器"] for tag, step in steps.items()},
                       "每一步的edma_misc": {tag: step["edma_misc"] for tag, step in steps.items()},
                       "每一步的edma_hw": {tag: step["健康报告"].get("edma_hw") for tag, step in steps.items()}}


def stage_diag(port, prompt, args, result):
    """5-5：路由器往 wan 发着流量的时候读诊断。"""
    folder = args.output
    device = device_name(port, prompt, folder, "以前")
    e3.router_addresses(port, prompt, folder, "诊断", "wan")
    e3.ensure_bridge_address(folder, "诊断")
    e4.firewall_open(port, prompt, folder, "诊断", "wan")
    e4.iperf_servers(port, prompt, folder, "诊断")
    run = subprocess.Popen(e4.iperf_argv(e4.ROUTER_BRIDGE_SIDE, e4.BRIDGE_ADDRESS, 20, e4.FLOWS, reverse=True),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        time.sleep(5)
        first = diagnose(port, prompt, folder, "第一次", device, "edma_hw")
        second = diagnose(port, prompt, folder, "第二次", device, "edma_hw")
        others = {name: diagnose(port, prompt, folder, "发着流量", device, name) for name in ("edma_ring", "edma_rxfill")}
        report = e4.iperf_report(e4.run_output(run, 90))
    finally:
        # 边界：流要收掉。
        lab.stop_process(run, 2, 5)
    ask(port, prompt, folder, "停iperf3", "killall iperf3 2>/dev/null; true", 20)
    left = e4.firewall_close(port, prompt, folder, "诊断")
    wan = [[(ring["prod"], ring["cons"]) for ring in reading.get("tx_rings", []) if ring["port"] == PORT_NUMBER["wan"]]
           for reading in (first, second)]
    result["判据"] = [{"编号": "5-5", "看什么": "诊断里有状态、还开着的位、各种错的次数、每个环的位置",
                     "通过": first.get("unmasked") == 0x1ff and len(first.get("errors", {})) == 9
                     and not any(first["errors"].values()) and "no unmasked error" in first and "status" in first
                     and len(first.get("tx_rings", [])) == 14 and len(first.get("tx_completion_rings", [])) == 4
                     and len(wan[0]) == 4 and wan[0] != wan[1]
                     and len(others["edma_rxfill"].get("rx_fill_rings", [])) == 4 and len(others["edma_ring"]) == 2,
                     "edma_hw的诊断（第一次）": first, "wan四个发包环的位置（两次）": wan,
                     "edma_ring的诊断": others["edma_ring"], "edma_rxfill的诊断": others["edma_rxfill"]}]
    result["读数里不对劲的地方"] = [] if report["做完"] else ["往 wan 发的那一组没有跑完"]
    result["要看的数"] = {"判据": {"5-5": result["判据"][0]["通过"]}, "每10秒的速率（兆比特每秒）": rates_mbit(report),
                       "做完以后防火墙里带记号的规则还剩几条": left}


def stage_reload(port, prompt, args, result):
    """5-9：卸载、加载三遍；卸载以后三个命令都是空的，再加载以后和 5-1、5-2、5-4 一样。"""
    folder, rounds = args.output, []
    lines = log_count(port, prompt, folder, "以前-内核记录的行数")
    for index in range(3):
        unload = lab.serial_command(port, "rmmod ipq807x-eth", prompt, 60)
        if not unload.completed or unload.exit_code:
            raise RuntimeError(f"第 {index} 遍卸载没有做成：{unload.output[-200:]}")
        empty = {what: devlink(port, prompt, folder, f"第{index}遍-卸载以后-{what}", f"{what} show") for what in ("dev", "port", "health")}
        load = lab.serial_command(port, "modprobe ipq807x-eth", prompt, 60)
        if not load.completed or load.exit_code:
            raise RuntimeError(f"第 {index} 遍加载没有做成：{load.output[-200:]}")
        e3.wait_router_link(port, prompt, folder, f"第{index}遍", ("lan2", "lan3", "wan"))
        rows = health(port, prompt, folder, f"第{index}遍-加载以后")
        ports = devlink(port, prompt, folder, f"第{index}遍-加载以后-端口", "port show").get("port", {})
        rounds.append({"卸载以后三个命令的输出": empty,
                       "卸载以后都是空的": all(not any(value.values()) for value in empty.values()),
                       "加载以后的端口数": len(ports),
                       "加载以后三个报告的状态和出错次数": {name: [row.get("state"), row.get("error")] for name, row in rows.items()}})
    for index, helper in enumerate(e3.COUNT_HELPERS):
        ask(port, prompt, folder, f"定义读收发包数的小函数-{index}", helper, 15)
    e3.router_addresses(port, prompt, folder, "加载以后", "wan")
    bound = e4.rebind(port, prompt, folder, "加载以后")
    new_lines = log_since(port, prompt, folder, "内核记录新增的行", lines)
    severe = [line for line in new_lines if re.search(SEVERE + "|stalled pool", line)]
    ping = list(e4.host_ping(folder, "加载以后ping", e4.ENO2_ADDRESS, e4.ROUTER_LAN, 100, None, 0.01))
    result["判据"] = [{"编号": "5-9", "看什么": "卸载、加载三遍",
                     "通过": not severe and ping == [100, 100]
                     and all(item["卸载以后都是空的"] and item["加载以后的端口数"] == 5
                             and item["加载以后三个报告的状态和出错次数"] == {name: ["healthy", 0] for name in REPORTERS}
                             for item in rounds),
                     "三遍": rounds, "内核记录里新增的警告": severe, "加载以后ping": ping, "重新绑核以后": bound}]
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {"判据": {"5-9": result["判据"][0]["通过"]}, "内核记录新增了几行": len(new_lines)}


STAGES = dict(e5.STAGES, setup=stage_setup, tools=stage_tools, stdstats=stage_stdstats, info=stage_info, pause=stage_pause, run=stage_run, fwdmix=stage_fwdmix,
              three=stage_three, jam=stage_jam, modtimer=stage_modtimer, buf=stage_buf, unknown=stage_unknown,
              sram=stage_sram, diag=stage_diag, reload=stage_reload)


def main():
    """读命令行，做一个阶段，把结果打印成一行 JSON。E-0004、E-0005 的阶段用到的参数名字和意思都照原来的。"""
    if len(sys.argv) == 5 and sys.argv[1] == "--pause-frames":
        pause_frames(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES) + ["hosthold", "finish"], help="做哪一个阶段")
    parser.add_argument("--inner", choices=sorted(e4.STAGES), help="run 阶段：做 E-0004 的哪一个阶段")
    parser.add_argument("--judge", choices=["txrate", "drops"], help="run 阶段：照哪一条判据判（不给就只报数）")
    parser.add_argument("--part", choices=["two", "wan", "pair"], help="jam 阶段：压两个口、压 wan，还是只有 wan 两个方向的对照")
    parser.add_argument("--samples", type=int, default=12, help="run 阶段：中间每 3 秒记一次，一共记几次（默认 12）")
    parser.add_argument("--ping-during", choices=["lan2", "wan"], help="run 阶段：阶段中间主机另外从这一路 ping 路由器 200 个")
    parser.add_argument("--label", required=True, help="这一次读数叫什么")
    parser.add_argument("--work", required=True, help="上机期间放东西的工作文件夹（里面有 实验用的模块/ 和 工具/）")
    parser.add_argument("--module", required=True, help="主机上编出来的正式的 ipq807x-eth.ko，和镜像里带的那一个比")
    parser.add_argument("--file", help="setup 阶段：实验用的模块的文件名，或者 normal")
    parser.add_argument("--insmod", default="", help="setup 阶段：加载实验用的模块时带的参数")
    parser.add_argument("--kind", choices=["route-up", "route-down", "bridge", "pause", "pause-two", "pause-wan", "linkdown", "close"],
                        help="fwdmix 阶段和 run 里的 forward、stall 阶段：哪一种")
    parser.add_argument("--flows", type=int, default=e4.FLOWS, help="几条流（默认 64）")
    parser.add_argument("--main", choices=["tcp", "udp"], default="tcp", help="txrate、forward 阶段：发的是 TCP 还是限速的 UDP")
    parser.add_argument("--rate", default="100M", help="--main udp 时每条流限到多少")
    parser.add_argument("--side", choices=["none", "tcp", "udp"], default="none", help="txrate 阶段：同时往 lan2 发什么")
    parser.add_argument("--side-flows", type=int, default=e4.FLOWS, help="--side 的那一组几条流")
    parser.add_argument("--side-rate", default="100M", help="--side udp 时每条流限到多少")
    parser.add_argument("--with-lan2", action="store_true", help="txrate 阶段：同时往 lan2 发")
    parser.add_argument("--diag", action="store_true", help="txrate 阶段：把两头的全部网络计数存下来，列出涨了一千以上的")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    args = parser.parse_args()
    if args.stage == "run" and not args.inner:
        parser.error("run 阶段要用 --inner 写明做 E-0004 的哪一个阶段")
    if args.stage == "setup" and not args.file:
        parser.error("setup 阶段要用 --file 写明换成哪一个")
    if args.stage == "fwdmix" and args.kind not in ("route-up", "route-down"):
        parser.error("fwdmix 阶段要用 --kind 写明 route-up 或者 route-down")
    if args.stage == "jam" and not args.part:
        parser.error("jam 阶段要用 --part 写明 two、wan 或者 pair")
    if args.stage == "run" and args.inner in ("forward", "stall") and not args.kind:
        parser.error("这个阶段要用 --kind 写明哪一种")
    # E-0004、E-0005 的阶段要、这份脚本的命令行不给的几个参数。
    args.cpu_bm = args.uni_ac = args.prehdr_src = args.frame_port = args.frame_low = args.frame_high = None
    args.jumbo_mtu, args.with_pause, args.layout = e4.MAX_MTU, False, "万兆口四个"
    e4.LAYOUT = args.layout
    e3.VARIANTS = os.path.join(args.work, "实验用的模块")
    e3.BRIDGE_SNAPSHOT = os.path.join(args.work, "主机br-lan-开工以前.txt")

    lab.stop_on_signals()
    if args.stage == "hosthold":
        result = host_hold(args)
        e3.e0002.write_summary(args.summary, result)
        return lab.print_result(result, result["读完"])
    if args.stage == "finish":
        released = host_release(args)
        result = e4.finish(args)
        result["代号"] = "E-0006"
        result["主机的br-lan和eno1"] = released
        e3.e0002.write_summary(args.summary, result)
        return lab.print_result(result, result["读完"])
    os.makedirs(args.output)
    prompt = re.compile(lab.LINUX_PROMPT.encode())
    port = lab.open_serial(args.device, args.baud, os.path.join(args.output, "串口.log"))
    level = None
    try:
        if lab.console_is_booting(port, 0.5):
            raise RuntimeError("控制台上有启动的输出，什么都没有发")
        result, level = e3.identity(port, prompt, args)
        result["代号"] = "E-0006"
        for index, helper in enumerate(e4.ROUTER_HELPERS):
            ask(port, prompt, args.output, f"01-定义小函数-{index}", helper, 15)
        STAGES[args.stage](port, prompt, args, result)
    except (RuntimeError, ValueError, KeyError, IndexError, OSError, TimeoutError, termios.error,
            subprocess.SubprocessError) as error:
        # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
        result = {"代号": "E-0006", "这一次": args.label, "阶段": args.stage, "读完": False,
                  "原因": f"{type(error).__name__}: {error}"}
    finally:
        if level is not None:
            lab.serial_command(port, f"dmesg -n {level}", prompt, 15)
        lab.close_serial(port)
    if result["读完"]:
        e3.e0002.write_summary(args.summary, result)
        result = {"代号": "E-0006", "这一次": args.label, "阶段": args.stage, "读完": True, "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result.get("判据", []) if not check["通过"]],
                  "读数里不对劲的地方": result.get("读数里不对劲的地方", []),
                  "要看的数": result.get("要看的数", {})}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
