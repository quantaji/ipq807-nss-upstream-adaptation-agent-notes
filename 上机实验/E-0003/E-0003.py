#!/usr/bin/env python3
"""E-0003（阶段 1 第三步：收发）的读数脚本。

判据在同一个文件夹的 报告.md 第 1.5 节，编号一样。一次只做一个阶段（--stage）：

  base     开始时的接线下，正式的模块：判据 1、2、3、5 到 12、15、20
  frames   四种帧各发一批，看 lan2 的收包数：判据 14（正式的模块、实验用的模块各跑一次）
  queues   "打开队列计数"的实验用模块下：判据 13
  misc     故意读错一个地址：判据 21
  reload   卸载再加载正式的模块：判据 16
  load     把一个实验用的模块传到路由器上换着加载（--file 文件名），或者换回正式的（--file normal）
  quiet    在路由器上把发地址的服务停掉并核对（第 1 次换线以前）
  swapped  第 1 次换线以后：判据 17、18
  finish   删掉主机 br-lan 的测试地址，回读核对和开工以前一样

例子（在这个文件夹里跑，前面带 PYTHONDONTWRITEBYTECODE=1）：
  python3 E-0003.py --stage base --label 第1版-base --module /tmp/E-0003/模块-第1版.ko \\
      --output /tmp/E-0003/第1版/base --summary 结果-第1版-base.json

主机这一头做的事：
  - eno2、USB 网卡：只 ping、发探测报文；判据 11、12、14 临时给 eno2 加一个 VLAN 子接口、
    一个地址、一条邻居项，做完删掉。它们平时的测试地址由 脚本/test-network.py 配。
  - br-lan（eno1 是它的成员）：加、删 192.168.13.10/24 这一个测试地址；从它 ping。
  - eno1：只读 carrier、speed 和 statistics/ 下面的数。
路由器这一头的改动都只在内存里。地址（MAC）的数值不显示、不写进任何文件。

结果是标准输出最后一行的一个 JSON 对象。
退出码 0 表示读完了（不等于每条判据都通过），1 表示没读完，2 表示用法错误。
"""

import argparse
import hashlib
import importlib.util
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

# E-0002 的读数脚本里现成的几样：在路由器上执行一条命令、读寄存器、写结果摘要。
_spec = importlib.util.spec_from_file_location("e0002", os.path.join(HERE, "..", "E-0002", "E-0002.py"))
e0002 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e0002)
ask = e0002.ask
read_registers = e0002.read_registers

ROUTER_PORTS = ("lan1", "lan2", "lan3", "lan4", "wan")
USB = "enxd4a2cde31c38"
# 主机三块网卡各自的测试地址，和它要 ping 的路由器的地址（报告第 1.4 节）。
ENO2_ADDRESS, ROUTER_LAN = "192.168.1.10", "192.168.1.1"
USB_ADDRESS, ROUTER_USB_SIDE = "192.168.11.10", "192.168.11.1"
BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE = "192.168.13.10", "192.168.13.1"
# 判据 11：VLAN 子接口上的地址。判据 12、14：只为了发"目的地址不是路由器的单播"用的地址。
VLAN_ID, VLAN_HOST, VLAN_ROUTER = 100, "192.168.100.10", "192.168.100.1"
FOREIGN_SOURCE, FOREIGN_TARGET = "192.168.99.1", "192.168.99.2"
MULTICAST = "239.255.77.77"
VARIANTS = "/tmp/E-0003/实验用的模块"
BRIDGE_SNAPSHOT = "/tmp/E-0003/主机br-lan-开工以前.txt"

# 路由器控制台上的两个小函数：n 口 rx|tx 读一个口的收包数或者发包数；c 把五个口的都列出来。
COUNT_HELPERS = ("n() { cat /sys/class/net/$1/statistics/$2_packets; }",
                 "c() { for i in lan1 lan2 lan3 lan4 wan; do echo $i $(n $i rx) $(n $i tx); done; }")
# EDMA 的寄存器，相对 PPE 的开头（edma_regs.h）。
EDMA = 0xb00000
REG_PORT_CTRL = EDMA + 0x4
REG_TXDESC_RING_SIZE = EDMA + 0x100c + 0x1000 * 23
REG_TXDESC_CTRL = EDMA + 0x1010 + 0x1000 * 23
REG_RXFILL_RING_SIZE = EDMA + 0x2900c + 0x1000 * 7
REG_RXFILL_RING_EN = EDMA + 0x2901c + 0x1000 * 7
REG_RXDESC_RING_SIZE = EDMA + 0x3900c + 0x1000 * 15
REG_RXDESC_CTRL = EDMA + 0x39018 + 0x1000 * 15
REG_QID2RID = EDMA + 0x5a000
# PPE 里每条队列的计数表：300 项，每项占 4 个字的位置，第一个字是包数。开关在 0x26000 的第 2 位。
REG_QUEUE_COUNTERS, QUEUES = 0x24000, 300
REG_QUEUE_COUNTER_ENABLE = 0x26000
REG_LAN2_BRIDGE_CTRL = 0x60300 + 4 * 4
# 判据 21 要读的"不是寄存器的地址"。
WRONG_ADDRESSES = (0xb00008, 0xb00010, 0xb00070, 0xb00ffc, 0xb28000, 0xb5b000)


def host(argv, folder, name, timeout=60):
    """在主机上执行一条命令，把输出存成 文件夹/名字.txt，返回（退出码，标准输出）。"""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    with open(os.path.join(folder, name + ".txt"), "x") as file:
        file.write(f"$ {' '.join(argv)}\n退出码 {done.returncode}\n{done.stdout}{done.stderr}")
    return done.returncode, done.stdout


def host_stat(interface, item):
    """读主机一块网卡 statistics/ 下面的一个数。只读。"""
    with open(f"/sys/class/net/{interface}/statistics/{item}") as file:
        return int(file.read())


def router_counts(port, prompt, folder, name):
    """读路由器五个口各自的收包数、发包数，返回 {口: (收, 发)}。"""
    lines = ask(port, prompt, folder, name, "c", 20).split("\n")
    counts = {}
    for line in lines:
        fields = line.split()
        counts[fields[0]] = (int(fields[1]), int(fields[2]))
    if set(counts) != set(ROUTER_PORTS):
        raise RuntimeError(f"{name}：读回来的口不是五个")
    return counts


def interrupt_count(port, prompt, folder, name, which):
    """读 /proc/interrupts 里一个中断在各个处理器上的次数之和。"""
    line = ask(port, prompt, folder, name, f"grep {which} /proc/interrupts", 15)
    numbers = []
    for field in line.split()[1:]:
        if not field.isdigit():
            break
        numbers.append(int(field))
    return sum(numbers)


def parse_ping(text):
    """从 ping 的输出里取"发了几个、回了几个"。"""
    found = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    if not found:
        raise RuntimeError("ping 的输出里没有找到发了几个、回了几个")
    return int(found.group(1)), int(found.group(2))


def host_ping(folder, name, source, target, count, size=None, interval=None, broadcast=False):
    """主机从一个测试地址 ping。返回（发了几个，回了几个）。"""
    argv = ["sudo", "-n", "ping", "-n", "-q", "-c", str(count), "-W", "1", "-I", source]
    if size:
        argv += ["-s", str(size), "-M", "do"]
    if interval:
        argv += ["-i", str(interval)]
    if broadcast:
        argv += ["-b"]
    _, output = host(argv + [target], folder, name, timeout=count + 60)
    # 网卡在 ping 的中间没有了的时候，ping 不打印那一行统计。这时算发了 0 个、回了 0 个。
    found = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def probes(folder, name, source, destination):
    """主机从 eno2 发 1000 个探测报文（UDP），返回这一段里 eno2 的发帧数涨了多少。"""
    before = host_stat("eno2", "tx_packets")
    code, _ = host([sys.executable, os.path.join(SCRIPTS, "packet-probe.py"), "send", "--kind", "udp",
                         "--source", source, "--destination", destination, "--port", "47000",
                         "--count", "1000", "--length", "600", "--rate", "500",
                         "--run-id", str(int(time.time()) & 0xFFFFFF)], folder, name)
    if code:
        raise RuntimeError(f"{name}：探测报文没有发成")
    time.sleep(1)
    return host_stat("eno2", "tx_packets") - before


def bridge_address_present():
    """主机的 br-lan 上有没有那个测试地址。只读。"""
    done = subprocess.run(["ip", "-br", "-4", "addr", "show", "dev", "br-lan"], capture_output=True, text=True)
    return f"{BRIDGE_ADDRESS}/24" in done.stdout


def bridge_snapshot():
    """主机 br-lan 的 IPv4 地址和走它的 IPv4 路由，拿来比前后一不一样。不含 MAC 地址。"""
    text = ""
    for argv in (["ip", "-br", "-4", "addr", "show", "dev", "br-lan"], ["ip", "-4", "route", "show", "dev", "br-lan"]):
        text += subprocess.run(argv, capture_output=True, text=True).stdout
    return text


def ensure_bridge_address(folder, tag):
    """ping 以前核主机 br-lan 的测试地址还在，没有就加上。返回加上了没有。

    主机的 br-lan 时有时无：NetworkManager 建出它、要不到地址就把它删掉，过一阵再建。
    所以先等它出现（最多 7 分钟），再加地址。第一次用以前记下开工时的样子。
    """
    if not os.path.exists(BRIDGE_SNAPSHOT):
        with open(BRIDGE_SNAPSHOT, "x") as file:
            file.write(bridge_snapshot())
    if bridge_address_present():
        return True
    for _ in range(420):
        if os.path.exists("/sys/class/net/br-lan"):
            break
        time.sleep(1)
    code, _ = host(["sudo", "-n", "ip", "addr", "add", f"{BRIDGE_ADDRESS}/24", "dev", "br-lan"],
                   folder, f"{tag}-给主机的br-lan加测试地址")
    return code == 0 and bridge_address_present()


def router_addresses(port, prompt, folder, tag, bridge_side_on):
    """路由器这一头的两个测试地址，加上了就不重复加。bridge_side_on 是 wan 或者 br-lan。"""
    other = "br-lan" if bridge_side_on == "wan" else "wan"
    ask(port, prompt, folder, f"{tag}-路由器的桥加{ROUTER_USB_SIDE}",
        f"ip addr replace {ROUTER_USB_SIDE}/24 dev br-lan", 15)
    ask(port, prompt, folder, f"{tag}-从{other}上去掉{ROUTER_BRIDGE_SIDE}",
        f"ip addr del {ROUTER_BRIDGE_SIDE}/24 dev {other} 2>/dev/null; true", 15)
    ask(port, prompt, folder, f"{tag}-{bridge_side_on}加{ROUTER_BRIDGE_SIDE}",
        f"ip addr replace {ROUTER_BRIDGE_SIDE}/24 dev {bridge_side_on}", 15)


def wait_router_link(port, prompt, folder, tag, names):
    """等这几个口都连上，而且该在桥里的已经回到桥里，最多 90 秒。"""
    for attempt in range(45):
        state = ask(port, prompt, folder, f"{tag}-等连线-第{attempt}次",
                    "for i in " + " ".join(names) + "; do cat /sys/class/net/$i/carrier 2>/dev/null || echo 0; done; "
                    "ls /sys/class/net/br-lan/brif 2>/dev/null | wc -l", 15).split("\n")
        if all(item == "1" for item in state[:-1]) and int(state[-1]) >= 4:
            return
        time.sleep(2)
    raise RuntimeError(f"{tag}：90 秒里口没有都连上，或者没有都回到桥里")


def ping_through(port, prompt, folder, tag, name, interface, source, target, count, size=None, interval=None):
    """主机从一块网卡 ping 路由器，前后读路由器五个口的收发包数和主机这块网卡的计数。

    interface 是读 statistics/ 的那块网卡：br-lan 那一路读的是 eno1。

    br-lan 那一路：主机的 br-lan 时有时无，所以 ping 得快（每 50 毫秒一个），ping 完核测试地址还在。
    还在，这一次才算数；不在了，说明 br-lan 中间被删掉过，这一次作废，等它再出现重来，最多 10 次。
    每一次都记在"作废的尝试"里。回答没收齐而地址一直在的，不重来。
    """
    through_bridge = source == BRIDGE_ADDRESS
    discarded = []
    for attempt in range(10 if through_bridge else 1):
        label = f"{tag}-第{attempt}次" if through_bridge else tag
        if through_bridge and not ensure_bridge_address(folder, label):
            discarded.append({"第几次": attempt, "原因": "测试地址没有加上"})
            time.sleep(5)
            continue
        before = router_counts(port, prompt, folder, f"{label}-以前-路由器的收发包数")
        host_before = (host_stat(interface, "rx_packets"), host_stat(interface, "rx_errors"))
        sent, received = host_ping(folder, f"{label}-ping", source, target, count, size,
                                   0.05 if through_bridge and not interval else interval)
        still_there = bridge_address_present() if through_bridge else True
        time.sleep(1)
        after = router_counts(port, prompt, folder, f"{label}-以后-路由器的收发包数")
        host_after = (host_stat(interface, "rx_packets"), host_stat(interface, "rx_errors"))
        if still_there:
            break
        discarded.append({"第几次": attempt, "原因": "ping 完以后测试地址不在了", "发了": sent, "回了": received})
    else:
        raise RuntimeError(f"{tag}：试了 10 次，主机 br-lan 的测试地址每一次都没有留到 ping 完")
    return {"口": name, "主机的网卡": interface, "发了": sent, "回了": received, "作废的尝试": discarded,
            "这个口收包数涨": after[name][0] - before[name][0], "这个口发包数涨": after[name][1] - before[name][1],
            "别的口收包数涨": {other: after[other][0] - before[other][0] for other in ROUTER_PORTS if other != name},
            "主机收帧数涨": host_after[0] - host_before[0], "主机收错数涨": host_after[1] - host_before[1]}


def landed(reading, count):
    """判据 8：这个口的收发包数各涨了至少 count，别的口的收包数涨得都不到 count。"""
    return (reading["这个口收包数涨"] >= count and reading["这个口发包数涨"] >= count
            and all(value < count for value in reading["别的口收包数涨"].values()))


def identity(port, prompt, args):
    """核跑的是哪一次引导、哪一个模块；定义读寄存器和读收发包数的小函数；读数期间控制台不显示内核记录。"""
    boot = ask(port, prompt, args.output, "00-启动号", "cat /proc/sys/kernel/random/boot_id", 15)
    on_router = ask(port, prompt, args.output, "00-路由器上镜像里带的模块",
                    "md5sum /lib/modules/$(uname -r)/ipq807x-eth.ko | cut -c1-32", 15)
    with open(args.module, "rb") as file:
        built = hashlib.md5(file.read()).hexdigest()
    ask(port, prompt, args.output, "01-定义读寄存器的小函数", e0002.HELPERS, 15)
    for index, helper in enumerate(COUNT_HELPERS):
        ask(port, prompt, args.output, f"01-定义读收发包数的小函数-{index}", helper, 15)
    level = ask(port, prompt, args.output, "02-控制台原来的级别", "cut -f1 /proc/sys/kernel/printk", 15)
    ask(port, prompt, args.output, "02-读数期间控制台不显示内核记录", "dmesg -n 1", 15)
    return {"代号": "E-0003", "这一次": args.label, "阶段": args.stage, "读完": True, "启动号": boot,
            "镜像里带的模块": on_router, "编出来的正式的模块": built, "模块一致": on_router == built}, int(level)


def vlan_check(port, prompt, folder):
    """判据 11：两头各建一个 VLAN 子接口，路由器 ping 主机，带 1472 字节。做完两头都删掉。"""
    host_device, router_device = f"eno2.{VLAN_ID}", f"lan2.{VLAN_ID}"
    try:
        for index, argv in enumerate((
                ["ip", "link", "add", "link", "eno2", "name", host_device, "type", "vlan", "id", str(VLAN_ID)],
                ["ip", "addr", "add", f"{VLAN_HOST}/24", "dev", host_device],
                ["ip", "link", "set", host_device, "up"])):
            code, _ = host(["sudo", "-n"] + argv, folder, f"11-主机建VLAN子接口-{index}")
            if code:
                raise RuntimeError("主机的 VLAN 子接口没有建成")
        ask(port, prompt, folder, "11-路由器建VLAN子接口",
            f"ip link add link lan2 name {router_device} type vlan id {VLAN_ID}", 15)
        ask(port, prompt, folder, "11-路由器的子接口加地址", f"ip addr add {VLAN_ROUTER}/24 dev {router_device}", 15)
        ask(port, prompt, folder, "11-路由器的子接口打开", f"ip link set {router_device} up", 15)
        time.sleep(2)
        output = ask(port, prompt, folder, "11-路由器从子接口ping主机",
                     f"ping -q -c 100 -s 1472 {VLAN_HOST}; true", 150)
        sent, received = parse_ping(output)
    finally:
        # 边界：不管量到哪一步，两头临时建的子接口都要删掉。
        subprocess.run(["sudo", "-n", "ip", "link", "del", host_device], capture_output=True)
        lab.serial_command(port, f"ip link del {router_device}", prompt, 15)
    return {"编号": "11", "看什么": "带 VLAN 标签的满长帧，路由器 ping 主机", "通过": (sent, received) == (100, 100),
            "发了": sent, "回了": received}


def foreign_unicast(folder, tag):
    """主机从 eno2 发 1000 个目的地址是 USB 网卡地址的单播。MAC 地址只在内存里过一下，不显示、不存。"""
    with open(f"/sys/class/net/{USB}/address") as file:
        mac = file.read().strip()
    try:
        subprocess.run(["sudo", "-n", "ip", "addr", "add", f"{FOREIGN_SOURCE}/24", "dev", "eno2"],
                       capture_output=True, check=True)
        subprocess.run(["sudo", "-n", "ip", "neigh", "replace", FOREIGN_TARGET, "lladdr", mac, "dev", "eno2"],
                       capture_output=True, check=True)
        return probes(folder, f"{tag}-目的地址不是路由器的单播", FOREIGN_SOURCE, FOREIGN_TARGET)
    finally:
        # 边界：临时加的邻居项和地址都要删掉。
        subprocess.run(["sudo", "-n", "ip", "neigh", "del", FOREIGN_TARGET, "dev", "eno2"], capture_output=True)
        subprocess.run(["sudo", "-n", "ip", "addr", "del", f"{FOREIGN_SOURCE}/24", "dev", "eno2"],
                       capture_output=True)


def no_forwarding(port, prompt, folder):
    """判据 12：lan2、lan3 从路由器的桥里拿出来，eno2 发的帧 USB 网卡收不收得到。做完放回桥里。"""
    try:
        ask(port, prompt, folder, "12-lan2从桥里拿出来", "ip link set lan2 nomaster", 15)
        ask(port, prompt, folder, "12-lan3从桥里拿出来", "ip link set lan3 nomaster", 15)
        time.sleep(2)
        before = router_counts(port, prompt, folder, "12-以前-路由器的收发包数")
        usb_before = host_stat(USB, "rx_packets")
        sent = probes(folder, "12-组播", ENO2_ADDRESS, MULTICAST)
        sent += foreign_unicast(folder, "12")
        time.sleep(1)
        after = router_counts(port, prompt, folder, "12-以后-路由器的收发包数")
        usb_after = host_stat(USB, "rx_packets")
    finally:
        # 边界：两个口都要放回桥里。
        lab.serial_command(port, "ip link set lan2 master br-lan", prompt, 15)
        lab.serial_command(port, "ip link set lan3 master br-lan", prompt, 15)
    lan2_rise, usb_rise = after["lan2"][0] - before["lan2"][0], usb_after - usb_before
    return {"编号": "12", "看什么": "两个口之间不直接转发", "通过": lan2_rise >= 2000 and usb_rise < 20,
            "主机发了": sent, "lan2收包数涨": lan2_rise, "主机USB网卡收帧数涨": usb_rise}


def stage_base(port, prompt, args, result):
    """开始时的接线，正式的模块：判据 1、2、3、5 到 12、15、20。"""
    folder, checks = args.output, []
    errors = ask(port, prompt, folder, "1-这个驱动报错的行数",
                 'dmesg | grep -E "ipq807x-eth|ethernet-switch" | grep -ciE "error|fail|timed out|cannot|warn"; true', 20)
    severe = ask(port, prompt, folder, "1-严重错误的行数",
                 'dmesg | grep -cE "WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error"; true', 20)
    names = sorted(ask(port, prompt, folder, "1-四个中断", "grep -o 'edma_[a-z]*' /proc/interrupts", 15).split("\n"))
    checks.append({"编号": "1", "看什么": "驱动加载",
                   "通过": errors == "0" and severe == "0"
                   and names == ["edma_misc", "edma_rxdesc", "edma_rxfill", "edma_txcmpl"],
                   "报错的行数": errors, "严重错误的行数": severe, "中断": names})

    single = {name: read_registers(port, prompt, folder, f"2-{name}", address, 1)[0]
              for name, address in (("EDMA的总开关", REG_PORT_CTRL), ("发包环的长度", REG_TXDESC_RING_SIZE),
                                    ("发包环的开关", REG_TXDESC_CTRL), ("空缓冲区环的长度", REG_RXFILL_RING_SIZE),
                                    ("空缓冲区环的开关", REG_RXFILL_RING_EN), ("收包环的长度", REG_RXDESC_RING_SIZE),
                                    ("收包环的开关", REG_RXDESC_CTRL))}
    table = read_registers(port, prompt, folder, "2-队列到环的表", REG_QID2RID, 64)
    checks.append({"编号": "2", "看什么": "环的寄存器",
                   "通过": single["EDMA的总开关"] & 0x2 == 0x2 and single["发包环的长度"] & 0xffff == 128
                   and single["发包环的开关"] & 1 == 1 and single["空缓冲区环的长度"] & 0xffff == 2048
                   and single["空缓冲区环的开关"] & 1 == 1 and single["收包环的长度"] & 0xffff == 2048
                   and single["收包环的开关"] & 1 == 1 and all(value == 0xffffffff for value in table),
                   "读到": {name: f"{value:08x}" for name, value in single.items()},
                   "队列到环的表里不是ffffffff的项数": sum(value != 0xffffffff for value in table)})

    threads = sorted(ask(port, prompt, folder, "3-收发线程", "ps | grep -o 'napi/edma_[rt]x-[0-9]*'; true", 15).split("\n"))
    checks.append({"编号": "3", "看什么": "收发线程",
                   "通过": len(threads) == 2 and threads[0].startswith("napi/edma_rx-")
                   and threads[1].startswith("napi/edma_tx-"), "线程": threads})

    router_addresses(port, prompt, folder, "4", "wan")
    wan_speed = ask(port, prompt, folder, "7-wan的速率", "cat /sys/class/net/wan/speed", 15)
    interrupts_before = [interrupt_count(port, prompt, folder, f"20-以前-{which}", which)
                         for which in ("edma_rxdesc", "edma_txcmpl")]
    paths = (("5", "lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN), ("6", "lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE),
             ("7", "wan", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE))
    readings = {}
    for number, name, interface, source, target in paths:
        readings[name] = ping_through(port, prompt, folder, number, name, interface, source, target, 100)
        if number == "5":
            interrupts_after = [interrupt_count(port, prompt, folder, f"20-以后-{which}", which)
                                for which in ("edma_rxdesc", "edma_txcmpl")]
        passed = (readings[name]["发了"], readings[name]["回了"]) == (100, 100)
        if name == "wan":
            passed = passed and wan_speed == "10000"
        checks.append({"编号": number, "看什么": f"{name}：主机 ping 路由器", "通过": passed, **readings[name]})
    checks.append({"编号": "8", "看什么": "报文落在对的网口上",
                   "通过": all(landed(reading, 100) for reading in readings.values()),
                   "每个口": {name: landed(reading, 100) for name, reading in readings.items()}})
    checks.append({"编号": "9", "看什么": "往外发的方向，从主机这一头看",
                   "通过": all(reading["主机收帧数涨"] >= 100 and reading["主机收错数涨"] == 0
                             for reading in readings.values()),
                   "每个口": {name: [reading["主机收帧数涨"], reading["主机收错数涨"]]
                           for name, reading in readings.items()}})
    checks.append({"编号": "20", "看什么": "中断在响",
                   "通过": all(after > before for before, after in zip(interrupts_before, interrupts_after)),
                   "edma_rxdesc": [interrupts_before[0], interrupts_after[0]],
                   "edma_txcmpl": [interrupts_before[1], interrupts_after[1]]})

    big = {name: ping_through(port, prompt, folder, f"10-{name}", name, interface, source, target, 100, size=1472)
           for _, name, interface, source, target in paths}
    checks.append({"编号": "10", "看什么": "满长的帧，两个方向",
                   "通过": all((reading["发了"], reading["回了"]) == (100, 100) for reading in big.values()),
                   "每个口": {name: [reading["发了"], reading["回了"]] for name, reading in big.items()}})

    checks.append(vlan_check(port, prompt, folder))
    checks.append(no_forwarding(port, prompt, folder))

    fast = ping_through(port, prompt, folder, "15", "lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN, 1000, interval=0.002)
    empty = ask(port, prompt, folder, "15-空缓冲区环空了的行数", 'dmesg | grep -c "RX fill ring is empty"; true', 20)
    checks.append({"编号": "15", "看什么": "连着收发不丢",
                   "通过": (fast["发了"], fast["回了"]) == (1000, 1000) and empty == "0",
                   "发了": fast["发了"], "回了": fast["回了"], "空缓冲区环空了的行数": empty})
    result["判据"] = checks


def stage_frames(port, prompt, args, result):
    """判据 14：四种帧各从 eno2 发一批，看 lan2 的收包数涨多少。"""
    folder, batches = args.output, []
    register = read_registers(port, prompt, folder, "14-lan2的控制寄存器", REG_LAN2_BRIDGE_CTRL, 1)[0]
    kinds = ("发给路由器自己地址的单播", "广播", "组播", "目的地址不是路由器的单播")
    for index, kind in enumerate(kinds):
        tag = f"14-{index}-发"
        before = router_counts(port, prompt, folder, f"14-{index}-以前")
        if kind == "发给路由器自己地址的单播":
            sent = host_ping(folder, tag, ENO2_ADDRESS, ROUTER_LAN, 100, interval=0.01)[0]
        elif kind == "广播":
            sent = host_ping(folder, tag, ENO2_ADDRESS, "192.168.1.255", 100, interval=0.01, broadcast=True)[0]
        elif kind == "组播":
            sent = probes(folder, tag, ENO2_ADDRESS, MULTICAST)
        else:
            sent = foreign_unicast(folder, tag)
        time.sleep(1)
        after = router_counts(port, prompt, folder, f"14-{index}-以后")
        batches.append({"编号": "14", "哪一种帧": kind, "通过": True, "主机发了": sent,
                        "lan2收包数涨": after["lan2"][0] - before["lan2"][0]})
    result["lan2的控制寄存器"] = f"{register:08x}"
    result["第17位"] = register >> 17 & 1
    result["判据"] = batches


def queue_counters(port, prompt, folder, name):
    """读 PPE 里 300 条队列各自的包数。"""
    words = read_registers(port, prompt, folder, name, REG_QUEUE_COUNTERS, QUEUES * 4)
    return [words[queue * 4] for queue in range(QUEUES)]


def stage_queues(port, prompt, args, result):
    """判据 13："打开队列计数"的实验用模块下，ping 的那一段里哪几条队列的计数涨了。"""
    folder = args.output
    enable = read_registers(port, prompt, folder, "13-队列计数的开关", REG_QUEUE_COUNTER_ENABLE, 1)[0]
    router_addresses(port, prompt, folder, "13", "wan")
    first = queue_counters(port, prompt, folder, "13-以前-第一回")
    before = queue_counters(port, prompt, folder, "13-以前-第二回")
    pings = {}
    for name, interface, source, target in (("lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN),
                                            ("lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE),
                                            ("wan", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE)):
        for size in (None, 1472):
            reading = ping_through(port, prompt, folder, f"13-{name}-{size or '小包'}", name, interface, source,
                                   target, 100, size=size)
            pings[f"{name}-{size or '小包'}"] = [reading["发了"], reading["回了"]]
    after = queue_counters(port, prompt, folder, "13-以后")
    # 计数要是读一回就清零，"以前-第二回"会比"以前-第一回"小，这时"以后"读到的就是涨的数。
    cleared = any(second < one for one, second in zip(first, before))
    rise = {queue: after[queue] - (0 if cleared else before[queue]) for queue in range(QUEUES)}
    risen = {str(queue): value for queue, value in rise.items() if value}
    result["队列计数的开关"] = f"{enable:08x}"
    result["计数读一回就清零"] = cleared
    result["ping"] = pings
    result["涨了的队列"] = risen
    result["判据"] = [{"编号": "13", "看什么": "送 CPU 的报文都进 0 号队列",
                     "通过": enable >> 2 & 1 == 1 and rise[0] > 0
                     and not any(rise[queue] for queue in range(1, 144)),
                     "0号队列涨": rise[0],
                     "1到143号里涨了的": {str(queue): rise[queue] for queue in range(1, 144) if rise[queue]}}]


def stage_misc(port, prompt, args, result):
    """判据 21：读几个不是寄存器的地址，看 edma_misc 的次数和内核记录。"""
    folder, tried = args.output, []
    lines_before = int(ask(port, prompt, folder, "21-以前-内核记录里EDMA报错的行数", 'dmesg | grep -c "EDMA: "; true', 20))
    count = interrupt_count(port, prompt, folder, "21-以前-edma_misc的次数", "edma_misc")
    verdict = {"编号": "21", "看什么": "硬件报错的中断来一次", "通过": False, "结论": "六个地址都没有让次数变"}
    for address in WRONG_ADDRESSES:
        outcome = lab.serial_command(port, f"r {address} 1", prompt, 20)
        with open(os.path.join(folder, f"21-读{address:#x}.txt"), "x") as file:
            file.write(outcome.output)
        time.sleep(1)
        now = interrupt_count(port, prompt, folder, f"21-读{address:#x}以后-edma_misc的次数", "edma_misc")
        tried.append({"地址": f"{address:#x}", "读到": outcome.output.strip(), "edma_misc的次数": now})
        if now == count:
            continue
        time.sleep(3)
        later = interrupt_count(port, prompt, folder, f"21-读{address:#x}再过3秒-edma_misc的次数", "edma_misc")
        lines = int(ask(port, prompt, folder, "21-以后-内核记录里EDMA报错的行数", 'dmesg | grep -c "EDMA: "; true', 20))
        text = ask(port, prompt, folder, "21-以后-内核记录里EDMA报错的行", 'dmesg | grep "EDMA: " | tail -n 5; true', 20)
        verdict.update({"通过": now - count == 1 and later == now and lines - lines_before >= 1,
                        "结论": "次数只加了一次" if later == now and now - count == 1 else "次数不止加了一次",
                        "次数": [count, now, later], "内核记录多了几行": lines - lines_before, "内核记录": text})
        break
    verdict["读过的地址"] = tried
    result["判据"] = [verdict]


def switch_module(port, prompt, folder, tag, commands):
    """卸载现在的模块，照 commands 加载另一个，等口重新连上，把路由器的测试地址加回去。

    返回这一次卸载加载以后读到的两样：内核记录里新增的警告和致命错误的行数、/proc/sys/kernel/tainted。
    """
    lines_before = int(ask(port, prompt, folder, f"{tag}-以前-内核记录的行数", "dmesg | wc -l", 20))
    unload = lab.serial_command(port, "rmmod ipq807x-eth", prompt, 60)
    load = None
    for command in commands:
        load = lab.serial_command(port, command, prompt, 60)
        if not load.completed or load.exit_code:
            break
    with open(os.path.join(folder, f"{tag}-卸载和加载.txt"), "x") as file:
        file.write(f"卸载：做完 {unload.completed} 退出码 {unload.exit_code}\n{unload.output}\n"
                   f"加载：做完 {load.completed} 退出码 {load.exit_code}\n{load.output}\n")
    if not unload.completed or unload.exit_code or not load.completed or load.exit_code:
        raise RuntimeError(f"{tag}：卸载或者加载没有做成")
    wait_router_link(port, prompt, folder, tag, ("lan2", "lan3", "wan"))
    for index, helper in enumerate(COUNT_HELPERS):
        ask(port, prompt, folder, f"{tag}-定义读收发包数的小函数-{index}", helper, 15)
    router_addresses(port, prompt, folder, tag, "wan")
    bad = ask(port, prompt, folder, f"{tag}-卸载加载以后新增的警告和致命错误的行数",
              f'dmesg | tail -n +{lines_before + 1} | grep -ciE "WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error"; true',
              20)
    tainted = ask(port, prompt, folder, f"{tag}-tainted", "cat /proc/sys/kernel/tainted", 15)
    return {"卸载以前内核记录的行数": lines_before, "新增的警告和致命错误的行数": bad, "tainted": tainted}


def stage_reload(port, prompt, args, result):
    """判据 16：卸载再加载正式的模块，看内核记录，再 ping 一遍 lan2、lan3。"""
    folder = args.output
    switched = switch_module(port, prompt, folder, "16", ["modprobe ipq807x-eth"])
    lines_before = switched["卸载以前内核记录的行数"]
    bad = ask(port, prompt, folder, "16-卸载以后内核记录里的警告",
              f'dmesg | tail -n +{lines_before + 1} | grep -ciE "page_pool|leak|WARNING:|BUG:|Oops|Call trace"; true', 20)
    result["卸载加载以后"] = switched
    pings = {name: ping_through(port, prompt, folder, f"16-{name}", name, interface, source, target, 100)
             for name, interface, source, target in (("lan2", "eno2", ENO2_ADDRESS, ROUTER_LAN),
                                                     ("lan3", USB, USB_ADDRESS, ROUTER_USB_SIDE))}
    result["判据"] = [{"编号": "16", "看什么": "卸载再加载驱动",
                     "通过": bad == "0" and switched["新增的警告和致命错误的行数"] == "0" and all((reading["发了"], reading["回了"]) == (100, 100)
                                              for reading in pings.values()),
                     "卸载以后内核记录里警告的行数": bad,
                     "每个口": {name: [reading["发了"], reading["回了"]] for name, reading in pings.items()}}]


def stage_load(port, prompt, args, result):
    """把一个实验用的模块传到路由器的内存盘里换上，或者换回正式的模块。"""
    folder = args.output
    if args.file == "normal":
        result["卸载加载以后"] = switch_module(port, prompt, folder, "换回正式的模块", ["modprobe ipq807x-eth"])
        result["换成了"] = "正式的模块"
    else:
        with open(os.path.join(VARIANTS, args.file), "rb") as file:
            wanted = hashlib.md5(file.read()).hexdigest()
        # 文件走 USB 网卡和 lan3 这一路取。不走 eno2 和 lan2：加载着"lan2 的第 17 位写 0"的模块时，
        # lan2 收不到发给路由器自己的单播，从这一路取不到文件。
        ask(port, prompt, folder, "路由器的桥上的测试地址", f"ip addr replace {ROUTER_USB_SIDE}/24 dev br-lan", 15)
        server = subprocess.Popen([sys.executable, "-m", "http.server", "--bind", USB_ADDRESS, "--directory",
                                   VARIANTS, "8000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            ask(port, prompt, folder, "取实验用的模块",
                f"uclient-fetch -q -O /tmp/variant.ko http://{USB_ADDRESS}:8000/{args.file}", 60)
        finally:
            # 边界：文件服务不管取没取成都要停掉。
            lab.stop_process(server, 15, 5)
        got = ask(port, prompt, folder, "取到的模块的校验值", "md5sum /tmp/variant.ko | cut -c1-32", 15)
        if got != wanted:
            raise RuntimeError("取到路由器上的模块和主机上的不一样")
        result["卸载加载以后"] = switch_module(port, prompt, folder, "换上实验用的模块", ["insmod /tmp/variant.ko"])
        result["换成了"] = args.file
        result["模块的校验值"] = wanted
    result["加载着的模块数"] = ask(port, prompt, folder, "加载着的模块", "lsmod | grep -c ipq807x_eth; true", 15)
    # 换模块以后路由器 wan 上的测试地址是新加的；主机 br-lan 那一头的也核一遍，后面量坏帧要从它 ping。
    result["主机br-lan的测试地址在"] = ensure_bridge_address(folder, "换模块以后")
    result["判据"] = [{"编号": "换模块", "看什么": "镜像里带的模块和编出来的正式的模块一致", "通过": result["模块一致"]}]


def stage_quiet(port, prompt, args, result):
    """第 1 次换线以前：把路由器上发地址的服务停掉（只在内存里），核确实停了。"""
    folder = args.output
    ask(port, prompt, folder, "停dnsmasq", "/etc/init.d/dnsmasq stop; true", 30)
    ask(port, prompt, folder, "停odhcpd", "/etc/init.d/odhcpd stop; true", 30)
    time.sleep(2)
    left = ask(port, prompt, folder, "还在跑的发地址的进程", "pgrep -l 'dnsmasq|odhcpd' | wc -l", 15)
    result["还在跑的发地址的进程数"] = left
    result["判据"] = [{"编号": "换线以前", "看什么": "路由器上发地址的服务停了", "通过": left == "0"}]


def stage_swapped(port, prompt, args, result):
    """第 1 次换线以后：判据 17（lan4，用 eno2）、18（lan1，用 eno1）。"""
    folder = args.output
    left = ask(port, prompt, folder, "还在跑的发地址的进程", "pgrep -l 'dnsmasq|odhcpd' | wc -l", 15)
    if left != "0":
        raise RuntimeError("路由器上发地址的服务还在跑，什么都没有做")
    router_addresses(port, prompt, folder, "17", "br-lan")
    speed = ask(port, prompt, folder, "18-lan1的速率", "cat /sys/class/net/lan1/speed", 15)
    lan4 = ping_through(port, prompt, folder, "17", "lan4", "eno2", ENO2_ADDRESS, ROUTER_LAN, 100)
    lan1 = ping_through(port, prompt, folder, "18", "lan1", "eno1", BRIDGE_ADDRESS, ROUTER_BRIDGE_SIDE, 100)
    result["判据"] = [
        {"编号": "17", "看什么": "lan4：主机 eno2 ping 路由器",
         "通过": (lan4["发了"], lan4["回了"]) == (100, 100) and lan4["这个口收包数涨"] >= 100
         and lan4["这个口发包数涨"] >= 100, **lan4},
        {"编号": "18", "看什么": "lan1：主机从 br-lan ping 路由器，连在 10000",
         "通过": (lan1["发了"], lan1["回了"]) == (100, 100) and speed == "10000"
         and lan1["这个口收包数涨"] >= 100 and lan1["这个口发包数涨"] >= 100, "lan1的速率": speed, **lan1}]


def finish(args):
    """删掉主机 br-lan 的测试地址，回读，和开工以前记下的比。不碰路由器。"""
    os.makedirs(args.output)
    if bridge_address_present():
        host(["sudo", "-n", "ip", "addr", "del", f"{BRIDGE_ADDRESS}/24", "dev", "br-lan"], args.output, "删掉测试地址")
    now = bridge_snapshot()
    with open(BRIDGE_SNAPSHOT) as file:
        before = file.read()
    with open(os.path.join(args.output, "回读.txt"), "x") as file:
        file.write(now)
    return {"代号": "E-0003", "这一次": args.label, "阶段": "finish", "读完": True,
            "测试地址还在": bridge_address_present(), "和开工以前一样": now == before,
            "判据": [{"编号": "收工", "看什么": "主机的 br-lan 和开工以前一样", "通过": now == before}]}


STAGES = {"base": stage_base, "frames": stage_frames, "queues": stage_queues, "misc": stage_misc,
          "reload": stage_reload, "load": stage_load, "quiet": stage_quiet, "swapped": stage_swapped}


def main():
    """读命令行，做一个阶段，把结果打印成一行 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES) + ["finish"], help="做哪一个阶段")
    parser.add_argument("--label", required=True, help="这一次读数叫什么")
    parser.add_argument("--module", help="主机上编出来的正式的 ipq807x-eth.ko，和镜像里带的那一个比（finish 不用）")
    parser.add_argument("--file", help="load 阶段：实验用的模块的文件名，或者 normal")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    args = parser.parse_args()
    if args.stage == "load" and not args.file:
        parser.error("load 阶段要用 --file 写明换成哪一个")
    if args.stage != "finish" and not args.module:
        parser.error("这个阶段要用 --module 给出编出来的正式的模块文件")

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
            result, level = identity(port, prompt, args)
            STAGES[args.stage](port, prompt, args, result)
        except (RuntimeError, ValueError, KeyError, IndexError, OSError, TimeoutError, termios.error,
                subprocess.SubprocessError) as error:
            # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
            result = {"代号": "E-0003", "这一次": args.label, "阶段": args.stage, "读完": False, "原因": str(error)}
        finally:
            if level is not None:
                lab.serial_command(port, f"dmesg -n {level}", prompt, 15)
            lab.close_serial(port)
    if result["读完"]:
        e0002.write_summary(args.summary, result)
        result = {"代号": "E-0003", "这一次": args.label, "阶段": args.stage, "读完": True, "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result["判据"] if not check["通过"]]}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
