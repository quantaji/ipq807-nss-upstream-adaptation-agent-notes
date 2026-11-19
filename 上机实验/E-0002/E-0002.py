#!/usr/bin/env python3
"""上机实验 E-0002 的读数脚本：阶段 1 第二步（连线）。

经串口读路由器上每个口连没连上、什么速率，读回新驱动 ipq807x-eth 在连线时写进硬件的东西，
再看主机发的帧有没有真的到路由器的 MAC。照 报告.md 第 1 节的判据比对，把结果摘要写成 JSON。

接线由命令行说明，脚本自己不知道线插在哪里。换线以后用新的接线再跑一遍。

这个脚本不写路由器的存储。主机这一头它只做两件事：读网卡的连线状态和收发计数；
从给了测试地址的网卡发探测报文（地址由 test-network.py 事先配好，脚本自己不配地址）。
eno1 只读，不给它发探测报文的地址。地址（MAC）的数值不进任何输出。

用法：
  python3 E-0002.py --label 现在的接线 --module 编出来的模块文件 \\
      --output /tmp/E-0002/现在的接线 --summary 结果-现在的接线.json \\
      --link lan2=eno2 --link lan3=enxd4a2cde31c38 --link wan=eno1 \\
      --probe eno2=192.168.1.2 --probe enxd4a2cde31c38=192.168.11.10 \\
      --speed-steps wan --slow-steps lan2 --updown lan2

  --link 路由器的口=主机的网卡     这根线现在接着。没有写到的口算没接线
  --probe 主机的网卡=它的测试地址   从这块网卡发探测报文。没有写到的网卡只读发送计数
  --speed-steps 万兆口              把这个口依次限制成 5G、2.5G、千兆，最后放开
  --slow-steps 千兆口               把这个口依次限制成 100 兆、10 兆，最后放开
  --updown 口                       把这个口关掉再打开

补做（报告.md 第 1.9 节，万兆口换速率以后头几秒的坏帧）：
  python3 E-0002.py --label 坏帧-lan1 --module 编出来的模块文件 \\
      --output /tmp/E-0002/坏帧-lan1 --summary 结果-坏帧-lan1.json \\
      --link lan1=eno1 --bad-frames lan1
  把这个口"5G、2.5G、千兆、放开回万兆"连着走 5 遍（--rounds 可以改遍数），
  每换一次速率记这几秒里收了多少帧、其中校验错的多少。带了 --bad-frames 就只做这一件事。
  判读是每一种换法单独算（万兆到 5G、5G 到 2.5G、2.5G 到千兆、千兆到万兆）。

结果是标准输出最后一行的一个 JSON 对象。
退出码 0 表示读完了（不等于每条判据都通过），1 表示没读完，2 表示用法错误。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import termios
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "脚本")
sys.path.insert(0, SCRIPTS)
import lab

REGISTERS = "/sys/kernel/debug/regmap/3a000000.ethernet-switch/registers"

# 在路由器的控制台上定义一个小函数：r 地址 个数，从这个地址起连着读几个寄存器。
# 调试文件里每个寄存器占一行，一行固定 17 个字节："地址: 数值"。
HELPERS = f"F={REGISTERS}; r() {{ dd if=$F bs=17 skip=$(($1/4)) count=$2 2>/dev/null; }}"

# 这块板子上口的名字和口号。5、6 号口是万兆口，用万兆 MAC；2、3、4 号口是千兆口。
PORTS = {"lan4": 2, "lan3": 3, "lan2": 4, "wan": 5, "lan1": 6}
FIRST_XGMAC_PORT = 5

# 口的收发时钟该是多少赫兹（2-连线.md 第三节那张表里这块板子用得到的几行）。
CLOCK_10G = {10000: 312500000, 5000: 156250000, 2500: 78125000, 1000: 125000000}
CLOCK_1G = {1000: 125000000, 100: 25000000, 10: 2500000}
# 万兆 MAC 发送配置寄存器里速率那一栏（第 29、30 位）和"走 USXGMII"那一位（第 31 位）。
XGMAC_SPEED = {10000: (0, 1), 5000: (1, 1), 2500: (2, 1), 1000: (3, 0)}
GMAC_SPEED = {1000: 2, 100: 1, 10: 0}
# ethtool 里每一档速率的位。
ADVERTISE = {10: 0x2, 100: 0x8, 1000: 0x20, 10000: 0x1000, 2500: 1 << 47, 5000: 1 << 48}
# ethtool 里"暂停帧"的两位。限制速率时一起报上，不然口就不再和对面商量流控了。
ADVERTISE_PAUSE = 0x2000 | 0x4000

# 补做用：路由器和主机两头都显示连上以后，再等这么多秒才读"换以后"那一回。
SETTLE_SECONDS = 10
SPEED_NAMES = {10000: "万兆", 5000: "5G", 2500: "2.5G", 1000: "千兆"}

PROBE_COUNT = 1000
# 探测报文的三种长度：UDP 里装的字节数、整帧落在 MAC 的哪一档收帧计数里。
# 先用第一种；空等的时候那一档自己就在涨（主机别的帧落进了这一档），就换下一种再测。
PROBE_LENGTHS = ((600, "512到1023字节"), (1100, "1024字节以上"), (300, "256到511字节"))


def ask(port, prompt, folder, name, command, timeout):
    """在路由器上执行一条命令，把输出存成 文件夹/名字.txt，返回输出。没做完或者退出码不是 0 就停。"""
    outcome = lab.serial_command(port, command, prompt, timeout)
    path = os.path.join(folder, name + ".txt")
    with open(path, "x") as file:
        file.write(outcome.output)
    if not outcome.completed:
        raise RuntimeError(f"{name}：命令没有在 {timeout} 秒里做完，这一次读数作废")
    if outcome.exit_code:
        raise RuntimeError(f"{name}：命令的退出码是 {outcome.exit_code}")
    return outcome.output.strip("\n")


def read_registers(port, prompt, folder, name, address, count):
    """从一个地址起连着读几个寄存器，核对读回来的地址，返回数值。"""
    lines = ask(port, prompt, folder, name, f"r {address} {count}", 20).split("\n")
    if len(lines) != count:
        raise RuntimeError(f"{name}：读回来 {len(lines)} 行，要的是 {count} 行")
    values = []
    for index, line in enumerate(lines):
        got, value = line.split(": ")
        if int(got, 16) != address + index * 4:
            raise RuntimeError(f"{name}：读回来的地址 {got} 不是要读的地址")
        values.append(int(value, 16))
    return values


def host_number(interface, item):
    """读主机一块网卡的一个数：carrier、speed，或者 statistics/tx_packets。只读。"""
    with open(f"/sys/class/net/{interface}/{item}") as file:
        return int(file.read())


def host_speed(interface):
    """读主机一块网卡的速率。它报"有连线"可能比路由器晚，最多等 30 秒；一直没有连线就返回 -1。"""
    for _ in range(30):
        if host_number(interface, "carrier"):
            return host_number(interface, "speed")
        time.sleep(1)
    return -1


def read_port(port, prompt, folder, tag, name):
    """读路由器一个口的连线状态、MAC 的寄存器、发送开关、收发时钟。"""
    number = PORTS[name]
    state = ask(port, prompt, folder, f"{tag}-{name}-连线状态",
                f"cd /sys/class/net/{name}; cat carrier; cat speed 2>/dev/null || echo -1; "
                "cat duplex 2>/dev/null || echo unknown; cd /", 15).split("\n")
    reading = {"有连线": state[0] == "1", "速率": int(state[1]), "双工": state[2]}
    gmac = 0x1000 + (number - 1) * 0x200
    enable, speed = read_registers(port, prompt, folder, f"{tag}-{name}-千兆MAC", gmac, 2)
    reading["千兆MAC"] = {"收": enable & 1, "发": enable >> 1 & 1, "双工": enable >> 4 & 1, "速率栏": speed & 3}
    if number >= FIRST_XGMAC_PORT:
        xgmac = 0x3000 + (number - FIRST_XGMAC_PORT) * 0x4000
        tx, rx, packet_filter, watchdog = read_registers(port, prompt, folder, f"{tag}-{name}-万兆MAC", xgmac, 4)
        flow = read_registers(port, prompt, folder, f"{tag}-{name}-万兆MAC的暂停时间", xgmac + 0x70, 1)[0]
        reading["万兆MAC"] = {"发": tx & 1, "收": rx & 1, "速率栏": tx >> 29 & 3, "走USXGMII": tx >> 31,
                            "不限超长帧": tx >> 16 & 1, "收所有帧": packet_filter & 1,
                            "暂停帧的处理": packet_filter >> 6 & 3, "速率适配": packet_filter >> 31,
                            "看门狗打开": watchdog >> 8 & 1, "看门狗门限": watchdog & 0xF, "暂停时间": flow >> 16}
    bridge = read_registers(port, prompt, folder, f"{tag}-{name}-口的控制寄存器", 0x60300 + number * 4, 1)[0]
    reading["发送开关"] = bridge >> 16 & 1
    clocks = ask(port, prompt, folder, f"{tag}-{name}-收发时钟",
                 f"cat /sys/kernel/debug/clk/gcc_nss_port{number}_rx_clk/clk_rate "
                 f"/sys/kernel/debug/clk/gcc_nss_port{number}_tx_clk/clk_rate", 15).split("\n")
    reading["收发时钟"] = [int(value) for value in clocks]
    return reading


def read_counters(port, prompt, folder, tag, name):
    """读路由器一个口 MAC 的收帧计数。万兆口读万兆 MAC 的，千兆口读千兆 MAC 的。"""
    number = PORTS[name]
    if number >= FIRST_XGMAC_PORT:
        base = 0x3000 + (number - FIRST_XGMAC_PORT) * 0x4000
        words = {}
        for item, offset in (("全部收帧", 0x900), ("广播", 0x918), ("组播", 0x920), ("校验错", 0x928),
                             ("256到511字节", 0x958), ("512到1023字节", 0x960), ("1024字节以上", 0x968),
                             ("单播", 0x970), ("暂停帧", 0x988)):
            low, high = read_registers(port, prompt, folder, f"{tag}-{name}-收{item}", base + offset, 2)
            words[item] = high << 32 | low
        return words
    base = 0x1000 + (number - 1) * 0x200
    broadcast, pause, multicast, fcs = read_registers(port, prompt, folder, f"{tag}-{name}-收帧数前四项",
                                                      base + 0x40, 4)
    medium, large, huge = read_registers(port, prompt, folder, f"{tag}-{name}-收帧数按长度", base + 0x70, 3)
    unicast = read_registers(port, prompt, folder, f"{tag}-{name}-收单播", base + 0x94, 1)[0]
    return {"广播": broadcast, "组播": multicast, "校验错": fcs, "256到511字节": medium, "512到1023字节": large,
            "1024字节以上": huge, "单播": unicast, "暂停帧": pause}


def host_settled_packets(interface):
    """读主机一块网卡的已发帧数，等它连着两次（隔 1 秒）读到一样的数再用。只读。"""
    value = host_number(interface, "statistics/tx_packets")
    for _ in range(30):
        time.sleep(1)
        again = host_number(interface, "statistics/tx_packets")
        if again == value:
            return value
        value = again
    raise RuntimeError(f"{interface}：已发帧数 30 秒里一直在变")


def frames_by_probe(port, prompt, folder, tag, name, source, interface):
    """判据 9：主机从给了测试地址的网卡发 1000 个一样长的组播探测报文，看路由器这个口的收帧数涨多少。

    一次测量要干净才拿来下结论，干净指两样：空等的时候那一档收帧数没有涨；
    发的那一段里主机这块网卡一共只发了这 1000 个帧（读主机的已发帧数核）。
    - 空等的时候就在涨的，照判据换一个帧长再测。
    - 发的那一段里主机自己还发了别的帧的，这一次测量不干净，同一个帧长重测，最多 4 次。
    每一次测量都记下来，早先不干净的不丢。
    """
    attempts = []
    for length, bucket in PROBE_LENGTHS:
        for _ in range(4):
            index = len(attempts)
            before_idle = read_counters(port, prompt, folder, f"{tag}-第{index}次-空等以前", name)
            time.sleep(PROBE_COUNT / 500 + 1)
            host_before = host_settled_packets(interface)
            before = read_counters(port, prompt, folder, f"{tag}-第{index}次-发以前", name)
            sent = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS, "packet-probe.py"), "send", "--kind", "udp",
                 "--source", source, "--destination", "239.255.77.77", "--port", "47000",
                 "--count", str(PROBE_COUNT), "--length", str(length), "--rate", "500",
                 "--run-id", str(int(time.time()) & 0xFFFFFF)],
                capture_output=True, text=True, timeout=60)
            with open(os.path.join(folder, f"{tag}-{name}-第{index}次-发探测报文.txt"), "x") as file:
                file.write(sent.stdout + sent.stderr)
            time.sleep(1)
            after = read_counters(port, prompt, folder, f"{tag}-第{index}次-发以后", name)
            host_grown = host_settled_packets(interface) - host_before
            attempt = {"帧长": length + 46, "看哪一档": bucket, "发送命令的退出码": sent.returncode,
                       "空等时涨": before[bucket] - before_idle[bucket], "发以后涨": after[bucket] - before[bucket],
                       "校验错涨": after["校验错"] - before_idle["校验错"], "主机这一段一共发了": host_grown,
                       "路由器各项涨": {item: after[item] - before[item] for item in before}}
            attempt["干净"] = attempt["空等时涨"] == 0 and host_grown == PROBE_COUNT
            attempts.append(attempt)
            if attempt["干净"] or attempt["空等时涨"] != 0:
                break
        if attempts[-1]["干净"]:
            break
    last = attempts[-1]
    return {"办法": "发探测报文", "每次": attempts,
            "通过": last["干净"] and last["发送命令的退出码"] == 0 and last["发以后涨"] == PROBE_COUNT
            and last["校验错涨"] == 0}


def host_sent(interface):
    """读主机一块网卡的发送计数：已发帧数，和只作记录的发送出错数、发送丢掉数。只读。"""
    return {item: host_number(interface, f"statistics/{item}") for item in ("tx_packets", "tx_errors", "tx_dropped")}


def bracket(port, prompt, folder, tag, name, interface):
    """夹着读一回，要两头都安静才算数。

    次序：主机的发送计数、路由器的收帧数、等 3 秒、再读路由器、再读主机。
    前后两次主机的数相同、前后两次路由器的数也相同，说明这几秒里没有帧在路上，这时两头的数才拿来比。
    只看主机的数不够：主机网卡的计数可能隔一会儿才更新一次，帧已经到了路由器，主机的数还没有动。
    """
    for attempt in range(40):
        host_before = host_sent(interface)
        router_before = read_counters(port, prompt, folder, f"{tag}-第{attempt}回-前", name)
        time.sleep(3)
        router_after = read_counters(port, prompt, folder, f"{tag}-第{attempt}回-后", name)
        if host_sent(interface) == host_before and router_after == router_before:
            return host_before, router_before
        time.sleep(1)
    raise RuntimeError(f"{name}：夹着读了 40 回，两头一直没有同时安静下来")


def frames_by_counters(port, prompt, folder, tag, name, interface):
    """判据 10：隔 60 秒以上夹着读两回，比主机发的帧数和路由器 MAC 收的帧数各涨了多少。

    这 60 秒里主机一帧都没有发的，分不出通还是不通，再等 60 秒读一回，最多等 5 回。
    """
    host_first, router_first = bracket(port, prompt, folder, f"{tag}-头一回", name, interface)
    for round_number in range(5):
        time.sleep(60)
        host_second, router_second = bracket(port, prompt, folder, f"{tag}-后一回{round_number}", name, interface)
        if host_second["tx_packets"] != host_first["tx_packets"]:
            break
    host_grown = host_second["tx_packets"] - host_first["tx_packets"]
    router_grown = sum(router_second[item] - router_first[item] for item in ("单播", "组播", "广播"))
    errors = router_second["校验错"] - router_first["校验错"]
    return {"办法": "夹着读两头的计数", "主机发的涨": host_grown, "路由器收的涨": router_grown, "校验错涨": errors,
            "只作记录": {"路由器各项涨": {item: router_second[item] - router_first[item] for item in router_first},
                     "主机各项涨": {item: host_second[item] - host_first[item] for item in host_first}},
            "通过": host_grown == router_grown and host_grown > 0 and errors == 0}


def frames(port, prompt, folder, tag, name, interface, probes):
    """按这块网卡有没有测试地址，选判据 9 或者判据 10 的办法。"""
    if interface in probes:
        return frames_by_probe(port, prompt, folder, tag, name, probes[interface], interface)
    return frames_by_counters(port, prompt, folder, tag, name, interface)


def wait_link(port, prompt, folder, tag, name, up, speed):
    """等这个口变成要的状态（连上并且是这个速率，或者断开），最多等 60 秒。返回等到没有。"""
    for attempt in range(30):
        state = ask(port, prompt, folder, f"{tag}-{name}-等连线-{attempt}",
                    f"cat /sys/class/net/{name}/carrier; cat /sys/class/net/{name}/speed 2>/dev/null || echo -1",
                    15).split("\n")
        if (state[0] == "1") == up and (not up or int(state[1]) == speed):
            return True
        time.sleep(2)
    return False


def check_port(reading, interface, speed):
    """照判据 1、5、6、7 核一个口的读数。interface 是 None 表示这个口没有接线。返回不对的地方。"""
    wrong = []
    xgmac = "万兆MAC" in reading
    mac = reading["万兆MAC"] if xgmac else reading["千兆MAC"]
    if interface is None:
        if reading["有连线"] or mac["收"] or mac["发"] or reading["发送开关"]:
            wrong.append("没有接线的口有连线，或者 MAC 的收发开关、口的发送开关开着")
        return wrong
    if not reading["有连线"] or reading["速率"] != speed or reading["双工"] != "full":
        wrong.append(f"连线状态不对：有连线 {reading['有连线']}，速率 {reading['速率']}，双工 {reading['双工']}，要的速率 {speed}")
    if not (mac["收"] and mac["发"] and reading["发送开关"]):
        wrong.append("MAC 的收发开关或者口的发送开关没有开")
    if xgmac:
        if (mac["速率栏"], mac["走USXGMII"]) != XGMAC_SPEED.get(speed):
            wrong.append(f"万兆 MAC 的速率栏是 {mac['速率栏']}、走 USXGMII 那一位是 {mac['走USXGMII']}")
        clock = CLOCK_10G.get(speed)
    else:
        if mac["速率栏"] != GMAC_SPEED.get(speed) or not mac["双工"]:
            wrong.append(f"千兆 MAC 的速率栏是 {mac['速率栏']}、双工那一位是 {mac['双工']}")
        clock = CLOCK_1G.get(speed)
    if reading["收发时钟"] != [clock, clock]:
        wrong.append(f"收发时钟是 {reading['收发时钟']}，要的是 {clock}")
    return wrong


def speed_steps(port, prompt, args, links, probes, name, speeds, summary_key):
    """把一个口依次限制成几档速率，每档读一遍、看一遍报文过不过得来，最后放开限制。"""
    interface = links[name]
    results = {}
    everything = sum(ADVERTISE[speed] for speed in (CLOCK_10G if PORTS[name] >= FIRST_XGMAC_PORT else CLOCK_1G))
    original = host_speed(interface)
    for speed in speeds + [None]:
        mask = (everything if speed is None else ADVERTISE[speed]) | ADVERTISE_PAUSE
        want = original if speed is None else speed
        tag = f"{summary_key}-{name}-{'放开' if speed is None else speed}"
        ask(port, prompt, args.output, f"{tag}-限制速率", f"ethtool -s {name} advertise {mask:#x}", 20)
        reached = wait_link(port, prompt, args.output, tag, name, True, want)
        reading = read_port(port, prompt, args.output, tag, name)
        host_side = host_speed(interface)
        wrong = [] if reached else [f"60 秒里没有等到这个口以 {want} 连上"]
        wrong += check_port(reading, interface, want)
        if host_side != want:
            wrong.append(f"主机那一头的速率是 {host_side}")
        evidence = frames(port, prompt, args.output, tag, name, interface, probes) if reached else None
        results["放开以后" if speed is None else str(speed)] = {
            "读数": reading, "主机的速率": host_side, "不对的地方": wrong, "报文": evidence,
            "通过": not wrong and bool(evidence and evidence["通过"])}
    return results


def measure(port, prompt, args):
    """一种接线下的全部读数。"""
    links = dict(item.split("=") for item in args.link)
    probes = dict(item.split("=") for item in args.probe)
    identity = ask(port, prompt, args.output, "00-身份",
                   "cat /proc/sys/kernel/random/boot_id; md5sum /lib/modules/$(uname -r)/ipq807x-eth.ko | cut -c1-32",
                   15).split("\n")
    with open(args.module, "rb") as file:
        built = hashlib.md5(file.read()).hexdigest()
    ask(port, prompt, args.output, "01-定义小函数", HELPERS, 15)
    level = ask(port, prompt, args.output, "02-控制台原来的级别", "cut -f1 /proc/sys/kernel/printk", 15)
    ask(port, prompt, args.output, "02-读数期间控制台不显示内核记录", "dmesg -n 1", 15)

    result = {"代号": "E-0002", "这一次": args.label, "读完": True, "启动号": identity[0],
              "路由器上的模块": identity[1], "编出来的模块": built, "模块一致": identity[1] == built,
              "接线": links, "判据": [], "口": {}, "报文": {}}
    checks = result["判据"]

    # 判据 1、2、5、6、7：每个口的连线状态和寄存器
    wrong_linked, wrong_unlinked = [], []
    for name in PORTS:
        reading = read_port(port, prompt, args.output, "1", name)
        interface = links.get(name)
        host_side = host_speed(interface) if interface else None
        reading["主机的速率"] = host_side
        result["口"][name] = reading
        wrong = check_port(reading, interface, host_side)
        (wrong_linked if interface else wrong_unlinked).extend(f"{name}：{item}" for item in wrong)
    checks.append({"编号": "1、5、6", "看什么": "接着线的口连上，速率和对面一样，MAC、发送开关、时钟都对",
                   "通过": not wrong_linked, "读数": "；".join(wrong_linked) or "都对"})
    checks.append({"编号": "2、7", "看什么": "没有接线的口没有连线，MAC 和发送开关关着",
                   "通过": not wrong_unlinked, "读数": "；".join(wrong_unlinked) or "都对"})

    # 判据 4：选择寄存器
    mux = read_registers(port, prompt, args.output, "1-选择寄存器", 0x10, 1)[0]
    checks.append({"编号": "4", "看什么": "选择寄存器低六位是 15", "通过": mux & 0x3F == 0x15, "读数": f"{mux:08x}"})

    # 判据 8：万兆 MAC 的固定设置。只看这种连接方式下已经配置过的万兆口（网口打开过就配置过）
    wrong = []
    for name, reading in result["口"].items():
        mac = reading.get("万兆MAC")
        if mac and (mac["不限超长帧"], mac["收所有帧"], mac["暂停帧的处理"], mac["速率适配"], mac["看门狗打开"],
                    mac["看门狗门限"], mac["暂停时间"]) != (1, 1, 2, 1, 1, 0xB, 0xFFFF):
            wrong.append(name)
    checks.append({"编号": "8", "看什么": "万兆 MAC 的固定设置在口复位以后还在",
                   "通过": not wrong, "读数": "不对的口：" + "、".join(wrong) if wrong else "两个万兆口都对"})

    # 判据 9、10：报文真的过得来
    for name, interface in links.items():
        if result["口"][name]["有连线"]:
            result["报文"][name] = frames(port, prompt, args.output, "2", name, interface, probes)
    failed = [name for name, item in result["报文"].items() if not item["通过"]]
    checks.append({"编号": "9、10", "看什么": "主机发的帧到了路由器的 MAC",
                   "通过": not failed and len(result["报文"]) == len(links),
                   "读数": "没有通过的口：" + "、".join(failed) if failed else f"{len(result['报文'])} 个口都通过"})

    # 判据 11、12、13：各档速率
    for name in args.speed_steps:
        result[f"{name}的各档速率"] = speed_steps(port, prompt, args, links, probes, name, [5000, 2500, 1000], "3")
    for name in args.slow_steps:
        result[f"{name}的各档速率"] = speed_steps(port, prompt, args, links, probes, name, [100, 10], "4")
    for name in args.speed_steps + args.slow_steps:
        steps = result[f"{name}的各档速率"]
        checks.append({"编号": "11、12、13", "看什么": f"{name} 的各档速率",
                       "通过": all(item["通过"] for item in steps.values()),
                       "读数": "；".join(f"{speed}：{'通过' if item['通过'] else '不通过'}" for speed, item in steps.items())})

    # 判据 17：关掉再打开
    for name in args.updown:
        interface = links[name]
        speed = host_speed(interface)
        ask(port, prompt, args.output, f"5-{name}-关掉", f"ip link set {name} down", 20)
        down = wait_link(port, prompt, args.output, "5-关掉以后", name, False, None)
        closed = read_port(port, prompt, args.output, "5-关掉以后", name)
        ask(port, prompt, args.output, f"5-{name}-打开", f"ip link set {name} up", 20)
        up = wait_link(port, prompt, args.output, "5-打开以后", name, True, speed)
        opened = read_port(port, prompt, args.output, "5-打开以后", name)
        evidence = frames(port, prompt, args.output, "5-打开以后", name, interface, probes) if up else None
        wrong = check_port(closed, None, None) + check_port(opened, interface, speed)
        result[f"{name}关掉再打开"] = {"关掉以后": closed, "打开以后": opened, "报文": evidence, "不对的地方": wrong}
        checks.append({"编号": "17", "看什么": f"{name} 关掉再打开",
                       "通过": down and up and not wrong and bool(evidence and evidence["通过"]),
                       "读数": "；".join(wrong) or "关掉以后断开，打开以后重新连上，报文过得来"})

    # 判据 3、14、15、16 用的内核记录：每个口"连上""断开"的行，和这个驱动报错的行
    log = ask(port, prompt, args.output, "6-内核记录里连上和断开的行",
              "dmesg | grep -E 'Link is (Up|Down)' | cut -c1-120; true", 30)
    result["内核记录里连上和断开的行"] = [line for line in log.split("\n") if line]
    bad = ask(port, prompt, args.output, "6-内核记录里这个驱动报错的行",
              "dmesg | grep -E 'ipq807x-eth|ethernet-switch' | grep -i -E 'error|fail|timed out|cannot|warn' "
              "| cut -c1-160; true", 30)
    severe = int(ask(port, prompt, args.output, "6-内核记录里的严重错误行数",
                     "dmesg | grep -c -E 'WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error'; true", 30))
    result["这个驱动报错的行"] = [line for line in bad.split("\n") if line]
    checks.append({"编号": "3", "看什么": "内核记录里这个驱动没有报错，没有严重错误",
                   "通过": not result["这个驱动报错的行"] and severe == 0,
                   "读数": f"报错的行 {len(result['这个驱动报错的行'])} 行，严重错误 {severe} 行"})

    # 判据 19、20：上一轮改的两处
    wrong = []
    for number in range(8):
        first, second = read_registers(port, prompt, args.output, f"7-最大帧长表-{number}", 0x63000 + number * 8, 2)
        if first != 0x05F205F2 or second & 3 != 3:
            wrong.append(f"{number} 号口：{first:08x} {second:08x}")
    checks.append({"编号": "19", "看什么": "最大帧长表八项都是 05f205f2，两个计数器开着",
                   "通过": not wrong, "读数": "；".join(wrong) or "八个口都对"})
    precedence = [read_registers(port, prompt, args.output, f"7-优先级-{number}", 0x60900 + number * 0x10, 1)[0]
                  for number in range(8)]
    checks.append({"编号": "20", "看什么": "优先级先信哪一处：八个口都是 00014608",
                   "通过": all(value == 0x14608 for value in precedence),
                   "读数": " ".join(f"{value:08x}" for value in precedence)})

    ask(port, prompt, args.output, "8-恢复控制台的级别", f"dmesg -n {int(level)}", 15)
    return result


def bad_frames(port, prompt, args):
    """补做（报告.md 第 1.9 节）：万兆口换速率以后头几秒，收进来的帧里有多少是校验错的。

    把"5G、2.5G、千兆、放开回万兆"连着走几遍。每换一次速率，换以前读一回万兆 MAC 的计数，
    两头都连上、又过了 SETTLE_SECONDS 秒以后再读一回，记这中间收了多少帧、其中校验错的多少。
    这几秒里至少收到一个帧的那一次才算数。判读是每一种换法单独算。

    驱动在万兆口换速率连上时把 MAC 复位的话，MAC 的计数会从零重新数，"换以后"那一回读到的
    就比"换以前"小。这一回里只要有一次是这样，就认定每一次换速率计数都清零了（驱动每次换速率
    做的事是一样的），这几秒收的帧就直接用"换以后"读到的数；一次都没有，就用两回读数的差。
    两回读到的原数都记在"每一次"里。
    """
    links = dict(item.split("=") for item in args.link)
    name = args.bad_frames
    interface = links[name]
    identity = ask(port, prompt, args.output, "00-身份",
                   "cat /proc/sys/kernel/random/boot_id; md5sum /lib/modules/$(uname -r)/ipq807x-eth.ko | cut -c1-32",
                   15).split("\n")
    with open(args.module, "rb") as file:
        built = hashlib.md5(file.read()).hexdigest()
    ask(port, prompt, args.output, "01-定义小函数", HELPERS, 15)
    level = ask(port, prompt, args.output, "02-控制台原来的级别", "cut -f1 /proc/sys/kernel/printk", 15)
    ask(port, prompt, args.output, "02-读数期间控制台不显示内核记录", "dmesg -n 1", 15)

    everything = sum(ADVERTISE[speed] for speed in CLOCK_10G)
    current = host_speed(interface)
    records = []
    for round_number in range(1, args.rounds + 1):
        for speed in (5000, 2500, 1000, 10000):
            mask = (everything if speed == 10000 else ADVERTISE[speed]) | ADVERTISE_PAUSE
            tag = f"第{round_number}遍-换到{speed}"
            before = read_counters(port, prompt, args.output, f"{tag}-换以前", name)
            ask(port, prompt, args.output, f"{tag}-限制速率", f"ethtool -s {name} advertise {mask:#x}", 20)
            reached = wait_link(port, prompt, args.output, tag, name, True, speed)
            host_side = host_speed(interface)
            started = time.monotonic()
            pinged = None
            # E-0003 第 19 条加的：两头都连上以后，主机马上 ping 路由器 20 个，每 0.2 秒一个，
            # 好让这几秒里一定有帧进来。链路没有连上的不 ping。不带 --ping-after-link 时不做这一步。
            if args.ping_after_link and reached and host_side == speed:
                source, target = args.ping_after_link.split("=")
                done = subprocess.run(["ping", "-n", "-q", "-c", "20", "-i", "0.2", "-W", "1", "-I", source, target],
                                      capture_output=True, text=True, timeout=60)
                with open(os.path.join(args.output, f"{tag}-ping.txt"), "x") as file:
                    file.write(done.stdout + done.stderr)
                found = re.search(r"(\d+) packets transmitted, (\d+) received", done.stdout)
                pinged = [int(found.group(1)), int(found.group(2))] if found else [0, 0]
            time.sleep(max(0, SETTLE_SECONDS - (time.monotonic() - started)))
            after = read_counters(port, prompt, args.output, f"{tag}-换以后", name)
            records.append({"第几遍": round_number, "换法": f"{SPEED_NAMES[current]}到{SPEED_NAMES[speed]}",
                            "路由器连上了": reached, "主机那一头的速率": host_side,
                            **({"ping发了和回了": pinged} if args.ping_after_link else {}),
                            "换以前全部收帧": before["全部收帧"], "换以后全部收帧": after["全部收帧"],
                            "换以前校验错": before["校验错"], "换以后校验错": after["校验错"]})
            current = speed
    ask(port, prompt, args.output, "08-恢复控制台的级别", f"dmesg -n {int(level)}", 15)

    cleared = any(record["换以后全部收帧"] < record["换以前全部收帧"]
                  or record["换以后校验错"] < record["换以前校验错"] for record in records)
    for record in records:
        record["这几秒收的帧"] = record["换以后全部收帧"] - (0 if cleared else record["换以前全部收帧"])
        record["其中校验错"] = record["换以后校验错"] - (0 if cleared else record["换以前校验错"])
        record["算数"] = record["这几秒收的帧"] > 0

    kinds = []
    for kind in dict.fromkeys(record["换法"] for record in records):
        counted = [record for record in records if record["换法"] == kind and record["算数"]]
        with_bad = [record for record in counted if record["其中校验错"] > 0]
        if not counted:
            verdict = "没有一次算数"
        elif len(with_bad) * 2 >= len(counted):
            verdict = "量得出来"
        elif not with_bad:
            verdict = "没有"
        else:
            verdict = "在两者之间"
        kinds.append({"换法": kind, "换了几次": sum(record["换法"] == kind for record in records),
                      "算数的次数": len(counted), "算数里有坏帧的次数": len(with_bad), "照判读": verdict})
    return {"代号": "E-0002", "这一次": args.label, "读完": True, "启动号": identity[0],
            "路由器上的模块": identity[1], "编出来的模块": built, "模块一致": identity[1] == built,
            "口": name, "换了几次速率": len(records), "换速率时计数清零了": cleared,
            "按换法": kinds, "每一次": records}


def write_summary(path, result):
    """把结果摘要写成一个新的 JSON 文件，最上面一层每一项占一行，判据每条占一行。"""
    lines = []
    for key, value in result.items():
        if key in ("判据", "每一次", "按换法"):
            body = ",\n".join("  " + json.dumps(check, ensure_ascii=False) for check in value)
            lines.append(f"{json.dumps(key, ensure_ascii=False)}: [\n{body}\n ]")
        else:
            lines.append(f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}")
    with open(path, "x") as file:
        file.write("{\n " + ",\n ".join(lines) + "\n}\n")


def main():
    """读命令行，做一种接线下的读数，把结果打印成一行 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--label", required=True, help="这一次读数叫什么，例如 现在的接线、第1次换线以后")
    parser.add_argument("--module", required=True, help="主机上编出来的 ipq807x-eth.ko")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    parser.add_argument("--link", action="append", default=[], metavar="口=网卡", help="一根接着的线")
    parser.add_argument("--probe", action="append", default=[], metavar="网卡=地址", help="从这块网卡发探测报文")
    parser.add_argument("--speed-steps", action="append", default=[], metavar="万兆口", help="走 5G、2.5G、千兆")
    parser.add_argument("--slow-steps", action="append", default=[], metavar="千兆口", help="走 100 兆、10 兆")
    parser.add_argument("--updown", action="append", default=[], metavar="口", help="关掉再打开")
    parser.add_argument("--bad-frames", metavar="万兆口", help="补做：这个口换速率以后头几秒的坏帧。只做这一件事")
    parser.add_argument("--rounds", type=int, default=5, help="补做时把四档速率连着走几遍（默认 5）")
    parser.add_argument("--ping-after-link", metavar="主机的地址=路由器的地址",
                        help="补做时每换一次速率、两头都连上以后，主机从这个地址 ping 路由器 20 个")
    args = parser.parse_args()
    named = [item.split("=")[0] for item in args.link] + args.speed_steps + args.slow_steps + args.updown
    if args.bad_frames:
        named.append(args.bad_frames)
        if args.bad_frames not in dict(item.split("=") for item in args.link):
            parser.error("--bad-frames 的口要用 --link 写明接的是主机哪块网卡")
    if any(name not in PORTS for name in named) or "eno1" in [item.split("=")[0] for item in args.probe]:
        parser.error("口的名字只能是 lan1 到 lan4 和 wan；eno1 不许给探测报文的地址")

    lab.stop_on_signals()
    os.makedirs(args.output)
    prompt = re.compile(lab.LINUX_PROMPT.encode())
    port = lab.open_serial(args.device, args.baud, os.path.join(args.output, "串口.log"))
    try:
        if lab.console_is_booting(port, 0.5):
            raise RuntimeError("控制台上有启动的输出，什么都没有发")
        result = bad_frames(port, prompt, args) if args.bad_frames else measure(port, prompt, args)
    except (RuntimeError, ValueError, KeyError, OSError, TimeoutError, termios.error,
            subprocess.TimeoutExpired) as error:
        # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
        result = {"代号": "E-0002", "这一次": args.label, "读完": False, "原因": str(error)}
    finally:
        lab.close_serial(port)

    if result["读完"]:
        write_summary(args.summary, result)
        short = {"代号": "E-0002", "这一次": args.label, "读完": True, "启动号": result["启动号"],
                 "模块一致": result["模块一致"], "摘要写在": args.summary}
        if args.bad_frames:
            short.update({item: result[item] for item in ("换了几次速率", "换速率时计数清零了", "按换法")})
        else:
            short["没有通过的判据"] = [check["编号"] for check in result["判据"] if not check["通过"]]
        result = short
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
