#!/usr/bin/env python3
"""上机实验 E-0001 的读数脚本：阶段 1 第一步（驱动加载、开机配置、注册网口）。

经串口在路由器上读回新驱动 ipq807x-eth 写进硬件的东西，照 报告.md 第 1 节的判据比对，
把简短的结果摘要写成一个 JSON 文件。两次上机（改以前、改以后）跑的是同一个脚本。

这个脚本只读寄存器，另外把驱动卸载再加载一次。它不写路由器的存储，不碰主机的网卡。
地址（MAC）的数值不进任何输出：比地址只比指纹，指纹是 md5 的前 12 位。

三种用法：
  读数：  python3 E-0001.py --label 改以前 --module 编出来的模块文件 \\
              --output /tmp/E-0001/改以前 --summary 结果-改以前.json
  比两次：python3 E-0001.py --compare 结果-改以前.json 结果-改以后.json
  补读：  python3 E-0001.py --extra --output /tmp/E-0001/补读
          （哈希偏移全表每一组有几项不是 0；br-lan 这个网口是谁建的）
  补做：  python3 E-0001.py --unclocked-read --output /tmp/E-0001/读没开时钟的口
          （读 1 号口的 MAC 寄存器，它的时钟没有开；可能让路由器重启，排在最后做）

结果是标准输出最后一行的一个 JSON 对象。
退出码 0 表示读完了（不等于每条判据都通过），1 表示没读完，2 表示用法错误。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import termios
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "脚本"))
import lab

# 内核给每份寄存器映射备的调试文件。每个寄存器占一行，一行固定 17 个字节："地址: 数值"。
REGISTERS = "/sys/kernel/debug/regmap/3a000000.ethernet-switch/registers"

# 在路由器的控制台上定义两个小函数，后面的读数命令都用它们：
#   r 地址 个数            从这个地址起连着读几个寄存器
#   t 起始 步长 项数 字数   读一张表：每一项从它的第 0 个字起，连着读它的几个字
# 多个字的表项必须从第 0 个字起整项连着读；单独读中间一个字，读到的是上一次读过的那一项。
HELPERS = (
    f"F={REGISTERS}; "
    "r() { dd if=$F bs=17 skip=$(($1/4)) count=$2 2>/dev/null; }; "
    "t() { i=0; while [ $i -lt $3 ]; do r $(($1+i*$2)) $4; i=$((i+1)); done; }"
)

PORT_NAMES = ("lan1", "lan2", "lan3", "lan4", "wan")
PASS_NAMES = ("第一次加载", "卸载再加载以后")

# 整表读回的表：名字、起始地址、每一项的步长、项数、每一项读几个字。
FULL_TABLES = (
    ("口的控制寄存器", 0x60300, 0x4, 8, 1),
    ("广播域表", 0x61800, 0x10, 32, 2),
    ("口登记的广播域", 0x201000, 0x10, 8, 3),
    ("EDMA队列到环", 0xB5A000, 0x4, 64, 1),
    ("哈希偏移第0组", 0x830000, 0x10, 256, 1),
    ("CPU代码段", 0x814000, 0x10, 256, 1),
    ("PPE环对应队列", 0x42A000, 0x40, 16, 10),
    ("环回口两个寄存器", 0x1C00, 0xC, 2, 1),
    ("生成树报文的地址", 0x610F8, 0x8, 1, 2),
    ("生成树报文的规则", 0x61400, 0x10, 1, 3),
    ("2到6号口MAC的帧长和间隔", 0x1218, 0x200, 5, 2),
    ("2到6号口MAC的巨帧和计数", 0x1230, 0x200, 5, 2),
    ("最大帧长表", 0x63000, 0x8, 8, 2),
    ("组播最大帧长", 0x60A00, 0x4, 8, 1),
    ("口的转发状态", 0x60100, 0x4, 8, 1),
    ("出口VLAN", 0x20420, 0x4, 8, 1),
    ("出口总开关", 0x26000, 0x4, 1, 1),
    ("优先级设置写到的地方", 0xE000, 0x10, 8, 2),
    ("厂商的优先级寄存器", 0x60900, 0x10, 8, 1),
    ("EDMA开关", 0xB00004, 0x4, 1, 1),
    ("EDMA环对环", 0xB0000C, 0x4, 5, 1),
    ("EDMA的DMA参数", 0xB00048, 0x4, 1, 1),
    ("EDMA写通道", 0xB00050, 0x4, 1, 1),
    ("EDMA杂项中断掩码", 0xB00060, 0x4, 1, 1),
)

# 只取摘要值的表（内容多，在路由器上算 md5，只把 md5 传回来）。
DIGEST_TABLES = (
    ("出口广播域标签", 0x20000, 0x4, 32, 1),
    ("端口调度时间表", 0x47A000, 0x10, 50, 1),
    ("端口调度时间表长度", 0x400000, 0x4, 1, 1),
    ("缓冲区时间表", 0xC000, 0x10, 96, 1),
    ("缓冲区时间表开关", 0xB000, 0x4, 1, 1),
    ("缓冲区流控方式", 0x600100, 0x4, 15, 1),
    ("缓冲区分组", 0x600180, 0x4, 15, 1),
    ("缓冲区共用池", 0x600290, 0x4, 1, 1),
    ("缓冲区每口门限", 0x601000, 0x10, 15, 2),
    ("目的到队列", 0x810000, 0x10, 3072, 1),
    ("优先级偏移", 0x842000, 0x10, 256, 1),
    ("哈希偏移全表", 0x830000, 0x10, 4096, 1),
    ("单播队列门限", 0x848000, 0x10, 256, 4),
    ("组播队列门限", 0x84A000, 0x10, 44, 3),
    ("队列共用池", 0x84C000, 0x10, 1, 3),
    ("第二级调度器的对应", 0x440000, 0x10, 64, 1),
    ("第二级调度器保证速率组", 0x442000, 0x10, 64, 1),
    ("第二级调度器超额速率组", 0x444000, 0x10, 64, 1),
    ("第二级调度器的口", 0x446000, 0x10, 64, 1),
    ("第一级调度器的对应", 0x402000, 0x10, 300, 1),
    ("第一级调度器保证速率组", 0x404000, 0x10, 512, 1),
    ("第一级调度器超额速率组", 0x406000, 0x10, 512, 1),
    ("第一级调度器的口", 0x408000, 0x10, 300, 1),
)

# 摘要里只记"不是 0 的项"的表：照判据这两张表应该全是 0。
ZERO_TABLES = ("哈希偏移第0组", "CPU代码段")

# 环对应队列的表里，厂商在这块芯片上写的四行（qca-ssdk 的 src/init/ssdk_hppe.c 第 1235 到 1245 行），
# 和你定的主机收包环那一行。每行 10 个字，没有列出的字都是 0。
RING_ROWS = {0: {0: 0xF}, 1: {0: 0xF00}, 2: {4: 0xFFFF}, 3: {0: 0xF0}, 15: {0: 0x1}}

DRIVER_LOG = re.compile(r"ipq807x|ethernet-switch")
BAD_WORDS = re.compile(r"error|fail|timed out|cannot|warn", re.IGNORECASE)
EMPTY_FINGERPRINT = "d41d8cd98f00"


def ask(port, prompt, folder, name, command, timeout):
    """在路由器上执行一条命令，把输出存成 文件夹/名字.txt，返回输出。没做完或者退出码不是 0 就停。"""
    outcome = lab.serial_command(port, command, prompt, timeout)
    with open(os.path.join(folder, name + ".txt"), "x") as file:
        file.write(outcome.output)
    if not outcome.completed:
        raise RuntimeError(f"{name}：命令没有在 {timeout} 秒里做完，这一次读数作废")
    if outcome.exit_code:
        raise RuntimeError(f"{name}：命令的退出码是 {outcome.exit_code}")
    return outcome.output.strip("\n")


def read_table(port, prompt, folder, tag, table, digest):
    """读一张表。整表读回时返回每一项的各个字；只取摘要值时返回路由器上算出的 md5。"""
    name, base, stride, count, words = table
    command = f"t {base} {stride} {count} {words}"
    seconds = 30 + count * words * 0.1
    if digest:
        value = ask(port, prompt, folder, f"{tag}-{name}", command + " | md5sum | cut -c1-32", seconds)
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise RuntimeError(f"{name}：读回来的不是一个摘要值，这一次读数作废")
        return value
    lines = ask(port, prompt, folder, f"{tag}-{name}", command, seconds).split("\n")
    if len(lines) != count * words:
        raise RuntimeError(f"{name}：读回来 {len(lines)} 行，要的是 {count * words} 行")
    rows = []
    for item in range(count):
        row = []
        for word in range(words):
            address, value = lines[item * words + word].split(": ")
            if int(address, 16) != base + item * stride + word * 4:
                raise RuntimeError(f"{name}：读回来的地址 {address} 不是要读的地址")
            row.append(int(value, 16))
        rows.append(row)
    return rows


def read_everything(port, prompt, folder, tag):
    """把一遍要读的东西全读回来：网口、地址的指纹、内核记录、时钟、每一张表。"""
    reading = {"表": {}, "摘要值": {}}
    reading["网口"] = ask(port, prompt, folder, f"{tag}-网口", "(cd /sys/class/net; echo *)", 15).split()
    reading["驱动认到设备"] = ask(
        port, prompt, folder, f"{tag}-驱动认到设备",
        "[ -e /sys/bus/platform/drivers/ipq807x-eth/3a000000.ethernet-switch ] && echo 1 || echo 0", 15) == "1"

    # 指纹：五个口各自的地址，和出厂数据分区里的 ethaddr。板级文件写着五个口都读这个 ethaddr。
    fingerprints = ask(
        port, prompt, folder, f"{tag}-地址的指纹",
        "for p in " + " ".join(PORT_NAMES) + "; do echo $p $(md5sum < /sys/class/net/$p/address | cut -c1-12); done; "
        "M=$(grep appsblenv /proc/mtd | cut -d: -f1); "
        'echo factory $(tr "\\000" "\\n" < /dev/${M}ro 2>/dev/null | grep -m1 "^ethaddr=" | cut -d= -f2 '
        "| tr A-F a-f | md5sum | cut -c1-12)", 30)
    reading["指纹"] = dict(line.split() for line in fingerprints.replace("factory", "出厂").split("\n"))

    log = ask(port, prompt, folder, f"{tag}-内核记录里这个驱动的行",
              "dmesg | grep -E 'ipq807x|ethernet-switch'; true", 30)
    reading["驱动的记录"] = [line for line in log.split("\n") if DRIVER_LOG.search(line)]
    reading["内核记录里的严重错误行数"] = int(ask(
        port, prompt, folder, f"{tag}-内核记录里的严重错误行数",
        "dmesg | grep -c -E 'WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error'; true", 30))

    clocks = ask(
        port, prompt, folder, f"{tag}-口的时钟",
        "(cd /sys/kernel/debug/clk; for c in gcc_port?_mac_clk gcc_nss_port?_?x_clk; "
        "do echo $c $(cat $c/clk_enable_count); done)", 30)
    reading["时钟"] = {name: int(count) for name, count in (line.split() for line in clocks.split("\n"))}

    for table in FULL_TABLES:
        reading["表"][table[0]] = read_table(port, prompt, folder, tag, table, False)

    # 试跑以后加的一步：最大帧长表只有 0 号口写进去了。这里先整项读 0 号口那一项，
    # 再单独读 1 号口那一项的第二个字，看单独读到的是 1 号口的，还是刚读过的 0 号口的。
    alone = ask(port, prompt, folder, f"{tag}-单独读最大帧长表1号口的第二个字", "r 405504 2; r 405516 1", 15)
    reading["单独读最大帧长表1号口的第二个字"] = [line.split(": ")[1] for line in alone.split("\n")]
    for table in DIGEST_TABLES:
        reading["摘要值"][table[0]] = read_table(port, prompt, folder, tag, table, True)
    return reading


def judge(first, second, reload):
    """照 报告.md 第 1 节的判据比对。每条给出：通过没有、读数。没有通过标准的条目，通过一栏是 None。"""
    tables = first["表"]
    checks = []
    bridge = [row[0] for row in tables["口的控制寄存器"]]

    bad_lines = [line for line in first["驱动的记录"] if BAD_WORDS.search(line)]
    checks.append(("1", "驱动加载不报错",
                   first["驱动认到设备"] and not bad_lines and first["内核记录里的严重错误行数"] == 0,
                   f"驱动认到设备：{first['驱动认到设备']}；这个驱动带报错字样的记录 {len(bad_lines)} 行；"
                   f"内核记录里的严重错误 {first['内核记录里的严重错误行数']} 行"))

    checks.append(("2", "五个口都在，没有 eth0",
                   set(PORT_NAMES) <= set(first["网口"]) and "eth0" not in first["网口"],
                   " ".join(first["网口"])))

    factory = first["指纹"]["出厂"]
    checks.append(("3", "五个口的地址都等于出厂数据分区里的 ethaddr",
                   factory != EMPTY_FINGERPRINT and all(first["指纹"][name] == factory for name in PORT_NAMES),
                   " ".join(f"{name}={value}" for name, value in first["指纹"].items())))

    wrong = []
    for vsi, row in enumerate(tables["广播域表"]):
        member = 0x01 | (1 << vsi if 1 <= vsi <= 7 else 0)
        if row != [0x01010100 | member, 0]:
            wrong.append(f"{vsi} 号：{row[0]:08x} {row[1]:08x}")
    checks.append(("4.1", "广播域表：口 N 的广播域只有口 N 和 CPU，不认识的报文只送 CPU，不学习",
                   not wrong, "32 项都对" if not wrong else "不对的项：" + "；".join(wrong)))

    numbers = [(row[1] >> 10) & 0x1F if row[1] & 0x200 else None for row in tables["口登记的广播域"]]
    checks.append(("4.2", "1 到 7 号口各自登记进自己那一号广播域",
                   numbers[1:] == [1, 2, 3, 4, 5, 6, 7],
                   "0 到 7 号口登记的广播域号：" + " ".join(str(number) for number in numbers)))

    checks.append(("4.3", "每个口：地址学习关、地址搬家关、隔离位图全开",
                   all(value & 0xFF09 == 0xFF00 for value in bridge),
                   " ".join(f"{value:08x}" for value in bridge)))

    checks.append(("4.4", "发送开关只有 CPU 口（0 号口）开着",
                   [value >> 16 & 1 for value in bridge] == [1, 0, 0, 0, 0, 0, 0, 0],
                   "0 到 7 号口的发送开关：" + " ".join(str(value >> 16 & 1) for value in bridge)))

    entry = tables["EDMA队列到环"][0][0]
    checks.append(("5.1", "EDMA 队列到环的表第 0 项：0 号队列进 15 号环", entry == 0x0000000F, f"{entry:08x}"))

    for number, name, meaning in (("5.2", "哈希偏移第0组", "哈希偏移第 0 组 256 项全是 0"),
                                  ("5.3", "CPU代码段", "CPU 代码段 256 项全是 0")):
        nonzero = {index: row[0] for index, row in enumerate(tables[name]) if row[0]}
        checks.append((number, meaning, not nonzero,
                       "全是 0" if not nonzero else f"{len(nonzero)} 项不是 0，见摘要里的读数"))

    rings = tables["PPE环对应队列"]
    for number, meaning, ring_numbers in (
            ("5.4", "环对应队列的表：0、1、3 号环和主机收包环（15 号）四行", (0, 1, 3, 15)),
            ("5.5", "环对应队列的表：2 号环那一行和厂商一致", (2,)),
            ("5.6", "环对应队列的表：没有人写的 4 到 14 号环全是 0", tuple(range(4, 15)))):
        wrong = []
        for ring in ring_numbers:
            expected = [RING_ROWS.get(ring, {}).get(word, 0) for word in range(10)]
            if rings[ring] != expected:
                wrong.append(f"{ring} 号环读到 " + " ".join(f"{value:x}" for value in rings[ring]))
        checks.append((number, meaning, not wrong, "对" if not wrong else "；".join(wrong)))

    loopback = [row[0] for row in tables["环回口两个寄存器"]]
    checks.append(("6.1", "环回口的两个寄存器（只记录）", None,
                   f"地址 0x1c00 读到 {loopback[0]:08x}，地址 0x1c0c 读到 {loopback[1]:08x}"))

    checks.append(("6.2", "7 号口的发送开关关着", bridge[7] >> 16 & 1 == 0, f"{bridge[7]:08x}"))

    stp = tables["生成树报文的地址"][0] + tables["生成树报文的规则"][0]
    checks.append(("6.3", "生成树协议报文的规则读回来和写进去的一样",
                   stp == [0xC2000000, 0x00010180, 0x3, 0x2, 0x93FC],
                   " ".join(f"{value:08x}" for value in stp)))

    port1 = [first["时钟"][name] for name in ("gcc_port1_mac_clk", "gcc_nss_port1_rx_clk", "gcc_nss_port1_tx_clk")]
    others = [count for name, count in first["时钟"].items() if "port1" not in name]
    checks.append(("7.1", "1 号口的三个时钟没有开，2 到 6 号口的开着",
                   port1 == [0, 0, 0] and len(others) == 15 and all(count > 0 for count in others),
                   " ".join(f"{name}={count}" for name, count in sorted(first["时钟"].items()))))

    wrong = []
    for index in range(5):
        ctrl2, debug = tables["2到6号口MAC的帧长和间隔"][index]
        jumbo, mib = tables["2到6号口MAC的巨帧和计数"][index]
        fine = (ctrl2 >> 8 & 0x3FFF == 0x3000 and ctrl2 >> 24 & 0xF == 1 and ctrl2 >> 6 & 1 == 0
                and debug >> 8 & 0xFF == 0xC and jumbo == 0x3000 and mib & 1 == 1)
        if not fine:
            wrong.append(f"{index + 2} 号口：{ctrl2:08x} {debug:08x} {jumbo:08x} {mib:08x}")
    checks.append(("7.2", "2 到 6 号口 MAC 的最大帧长、帧间隔、计数器开关",
                   not wrong, "五个口都对" if not wrong else "；".join(wrong)))

    different = sorted(name for name in tables if name != "EDMA队列到环" and tables[name] != second["表"][name])
    different += sorted(name for name in first["摘要值"] if first["摘要值"][name] != second["摘要值"][name])
    reload_bad = [line for line in second["驱动的记录"] if BAD_WORDS.search(line)]
    checks.append(("8", "卸载再加载一次：都不报错，口先没有后回来，地址没变，各张表和第一次加载以后一样",
                   reload["卸载的退出码"] == 0 and reload["加载的退出码"] == 0
                   and not set(PORT_NAMES) & set(reload["卸载以后的网口"])
                   and set(PORT_NAMES) <= set(second["网口"]) and second["指纹"] == first["指纹"]
                   and second["驱动认到设备"] and not reload_bad
                   and second["内核记录里的严重错误行数"] == 0 and not different,
                   f"卸载的退出码 {reload['卸载的退出码']}，加载的退出码 {reload['加载的退出码']}；"
                   f"卸载以后的网口：{' '.join(reload['卸载以后的网口'])}；"
                   f"加载以后的网口：{' '.join(second['网口'])}；指纹没变：{second['指纹'] == first['指纹']}；"
                   f"带报错字样的记录 {len(reload_bad)} 行；"
                   f"和第一次加载以后不一样的表：{'、'.join(different) if different else '没有'}"))

    checks.append(("9", "每个口的控制寄存器第 17 位是 1",
                   all(value >> 17 & 1 for value in bridge),
                   "0 到 7 号口的第 17 位：" + " ".join(str(value >> 17 & 1) for value in bridge)))

    wrong = [f"{index} 号口：{row[0]:08x} {row[1]:08x}" for index, row in enumerate(tables["最大帧长表"])
             if row[0] != 0x05F205F2 or row[1] & 3 != 3]
    checks.append(("附带", "最大帧长表：收发都是 1522，两个计数器开着",
                   not wrong, "八个口都对" if not wrong else "；".join(wrong)))

    return [{"编号": number, "看什么": meaning, "通过": passed, "读数": text}
            for number, meaning, passed, text in checks]


def shorten(reading):
    """把一遍的读数缩成摘要里放得下的样子：每一项的各个字写成一串十六进制。"""
    short = {}
    for name, rows in reading["表"].items():
        if name in ZERO_TABLES:
            short[name + "里不是0的项"] = {str(index): f"{row[0]:08x}" for index, row in enumerate(rows) if row[0]}
        else:
            short[name] = [" ".join(f"{value:08x}" for value in row) for row in rows]
    short["单独读最大帧长表1号口的第二个字"] = reading["单独读最大帧长表1号口的第二个字"]
    return {"读数": short, "摘要值": reading["摘要值"], "网口": reading["网口"], "指纹": reading["指纹"],
            "时钟": reading["时钟"], "驱动的记录": reading["驱动的记录"],
            "内核记录里的严重错误行数": reading["内核记录里的严重错误行数"]}


def measure(port, prompt, args):
    """一次上机的全部读数：核镜像、读一遍、卸载再加载、再读一遍、比对判据。"""
    identity = ask(port, prompt, args.output, "00-身份",
                   "cat /proc/sys/kernel/random/boot_id; uname -r; "
                   "md5sum /lib/modules/$(uname -r)/ipq807x-eth.ko | cut -c1-32", 15).split("\n")
    with open(args.module, "rb") as file:
        built = hashlib.md5(file.read()).hexdigest()
    ask(port, prompt, args.output, "01-定义小函数", HELPERS, 15)

    # 读数期间不让内核往控制台上打记录：打出来的字会混进读数里。读完以后恢复原来的级别。
    # 这只管控制台上显示什么，内核记录本身照常记，后面照常读得到。
    level = ask(port, prompt, args.output, "02-控制台原来的级别", "cut -f1 /proc/sys/kernel/printk", 15)
    ask(port, prompt, args.output, "02-读数期间控制台不显示内核记录", "dmesg -n 1", 15)

    first = read_everything(port, prompt, args.output, "1")

    reload = {}
    reload["卸载的退出码"] = int(ask(port, prompt, args.output, "2-卸载", "rmmod ipq807x-eth; echo $?", 60).split("\n")[-1])
    reload["卸载以后的网口"] = ask(port, prompt, args.output, "2-卸载以后的网口", "(cd /sys/class/net; echo *)", 15).split()
    reload["加载的退出码"] = int(ask(port, prompt, args.output, "2-加载", "modprobe ipq807x-eth; echo $?", 60).split("\n")[-1])

    second = read_everything(port, prompt, args.output, "3")
    ask(port, prompt, args.output, "4-恢复控制台的级别", f"dmesg -n {int(level)}", 15)

    return {"代号": "E-0001", "这一次": args.label, "读完": True,
            "启动号": identity[0], "内核": identity[1],
            "路由器上的模块": identity[2], "编出来的模块": built, "模块一致": identity[2] == built,
            "判据": judge(first, second, reload), "卸载再加载": reload,
            PASS_NAMES[0]: shorten(first), PASS_NAMES[1]: shorten(second)}


def extra_readings(port, prompt, args):
    """补读两样：哈希偏移全表 16 组（每组 256 项）各有几项不是 0；br-lan 这个网口在系统配置里是什么。"""
    boot = ask(port, prompt, args.output, "00-启动号", "cat /proc/sys/kernel/random/boot_id", 15)
    ask(port, prompt, args.output, "01-定义小函数", HELPERS, 15)
    counts = ask(port, prompt, args.output, "02-哈希偏移全表每组不是0的项数",
                 "t 8585216 16 4096 1 | awk '{g=int((NR-1)/256); if ($2!=\"00000000\") c[g]++} "
                 "END{for(i=0;i<16;i++) printf \"%d \", c[i]+0; print NR}'", 500).split()
    bridge = ask(port, prompt, args.output, "03-br-lan在系统配置里是什么",
                 "uci -q get network.@device[0].name; uci -q get network.@device[0].type; "
                 "uci -q get network.@device[0].ports", 15).split("\n")
    return {"代号": "E-0001", "这一次": "补读", "读完": True, "启动号": boot,
            "哈希偏移全表读到的项数": int(counts[16]),
            "哈希偏移全表每组不是0的项数": {str(group): int(counts[group]) for group in range(16)},
            "系统配置里第一个设备的名字、种类、成员": bridge}


def unclocked_read(port, prompt, args):
    """补做：读 1 号口的 MAC 寄存器。这个口的时钟没有开，读了可能让路由器重启，所以单独排在最后。"""
    result = {"代号": "E-0001", "这一次": "读没开时钟的 1 号口", "读完": True}
    before = ask(port, prompt, args.output, "00-读以前",
                 "cat /proc/sys/kernel/random/boot_id; cat /sys/class/watchdog/watchdog0/state; "
                 "cat /proc/sys/kernel/panic; cat /sys/kernel/debug/clk/gcc_port1_mac_clk/clk_enable_count", 15).split("\n")
    result.update({"启动号": before[0], "看门狗": before[1], "内核出错几秒后重启": before[2],
                   "1号口MAC时钟的计数": before[3]})
    if before[1] != "active" or before[3] != "0":
        result.update({"读完": False, "原因": "看门狗没有在跑，或者 1 号口的时钟其实开着，前提不成立，没有读"})
        return result

    ask(port, prompt, args.output, "01-定义小函数", HELPERS, 15)
    outcome = lab.serial_command(port, "r 4096 1; r 4120 1", prompt, 20)
    with open(os.path.join(args.output, "02-读1号口的MAC寄存器.txt"), "x") as file:
        file.write(outcome.output)
    result["命令做完了"] = outcome.completed
    if outcome.completed:
        result["读到"] = outcome.output.strip("\n").split("\n")
        after = ask(port, prompt, args.output, "03-读以后",
                    "cat /proc/sys/kernel/random/boot_id; "
                    "dmesg | grep -c -E 'WARNING:|BUG:|Oops|Call trace|Unable to handle|Internal error'; true", 30).split("\n")
        result.update({"读以后的启动号": after[0], "读以后内核记录里的严重错误行数": int(after[1])})
        return result

    # 命令没有回来：听控制台 150 秒，看路由器是不是重启了（看门狗 30 秒，内核出错以后 3 秒）。
    heard = bytearray()
    until = time.monotonic() + 150
    while time.monotonic() < until and not lab.BOOT_ACTIVITY.search(heard):
        heard += lab.read_serial(port, until - time.monotonic())
    with open(os.path.join(args.output, "03-命令没回来以后的控制台.txt"), "x") as file:
        file.write(heard.decode(errors="replace"))
    result["路由器重启了"] = bool(lab.BOOT_ACTIVITY.search(heard))
    return result


def compare(before_path, after_path):
    """比两次上机的摘要：第一次加载以后的读数和摘要值，哪些不一样。"""
    with open(before_path) as file:
        before = json.load(file)
    with open(after_path) as file:
        after = json.load(file)
    differences = {}
    for kind in ("读数", "摘要值"):
        old, new = before[PASS_NAMES[0]][kind], after[PASS_NAMES[0]][kind]
        for name in old:
            if old[name] == new[name]:
                continue
            if isinstance(old[name], list):
                differences[name] = {f"第 {index} 项": {"改以前": old[name][index], "改以后": new[name][index]}
                                     for index in range(len(old[name])) if old[name][index] != new[name][index]}
            else:
                differences[name] = {"改以前": old[name], "改以后": new[name]}
    for name in ("网口", "指纹", "时钟"):
        if before[PASS_NAMES[0]][name] != after[PASS_NAMES[0]][name]:
            differences[name] = {"改以前": before[PASS_NAMES[0]][name], "改以后": after[PASS_NAMES[0]][name]}
    return {"代号": "E-0001", "比的是": [before["这一次"], after["这一次"]],
            "两次的启动号": [before["启动号"], after["启动号"]], "不一样的地方": differences}


def one_line(value):
    """把一个值写成一行 JSON，汉字照原样写。"""
    return json.dumps(value, ensure_ascii=False)


def write_summary(path, result):
    """把结果摘要写成一个新的 JSON 文件。每条判据、每张表各占一行，文件短，人也能读。"""
    lines = []
    for key, value in result.items():
        if key == "判据":
            body = ",\n".join("  " + one_line(check) for check in value)
            lines.append(f"{one_line(key)}: [\n{body}\n ]")
        elif key in PASS_NAMES:
            parts = []
            for name, content in value.items():
                if name in ("读数", "摘要值"):
                    body = ",\n".join(f"   {one_line(table)}: {one_line(rows)}" for table, rows in content.items())
                    parts.append(f"  {one_line(name)}: {{\n{body}\n  }}")
                else:
                    parts.append(f"  {one_line(name)}: {one_line(content)}")
            lines.append(f"{one_line(key)}: {{\n" + ",\n".join(parts) + "\n }")
        else:
            lines.append(f"{one_line(key)}: {one_line(value)}")
    with open(path, "x") as file:
        file.write("{\n " + ",\n ".join(lines) + "\n}\n")


def main():
    """读命令行，做其中一种用法，把结果打印成一行 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--label", help="这一次上机叫什么，例如 改以前、改以后")
    parser.add_argument("--module", help="主机上编出来的 ipq807x-eth.ko，用来核路由器上跑的是不是它")
    parser.add_argument("--output", help="放每一步原始输出的新文件夹，例如 /tmp/E-0001/改以前")
    parser.add_argument("--summary", help="结果摘要写到哪个新文件")
    parser.add_argument("--unclocked-read", action="store_true", help="补做：读没开时钟的 1 号口的 MAC 寄存器")
    parser.add_argument("--extra", action="store_true", help="补读：哈希偏移全表每组不是 0 的项数、br-lan 是谁建的")
    parser.add_argument("--compare", nargs=2, metavar=("改以前", "改以后"), help="比两份结果摘要")
    args = parser.parse_args()

    if args.compare:
        return lab.print_result(compare(*args.compare), True)
    if not args.output or not (args.unclocked_read or args.extra or (args.label and args.module and args.summary)):
        parser.error("读数要给 --label --module --output --summary；补读、补做要给 --extra 或 --unclocked-read，加 --output")

    lab.stop_on_signals()
    os.makedirs(args.output)
    prompt = re.compile(lab.LINUX_PROMPT.encode())
    port = lab.open_serial(args.device, args.baud, os.path.join(args.output, "串口.log"))
    try:
        if lab.console_is_booting(port, 0.5):
            raise RuntimeError("控制台上有启动的输出，什么都没有发")
        if args.unclocked_read:
            result = unclocked_read(port, prompt, args)
        elif args.extra:
            result = extra_readings(port, prompt, args)
        else:
            result = measure(port, prompt, args)
    except (RuntimeError, ValueError, OSError, TimeoutError, termios.error) as error:
        # 外部边界：串口和设备的错误、读回来的东西认不出，都照实报出来，不往上抛。
        result = {"代号": "E-0001", "这一次": args.label, "读完": False, "原因": str(error)}
    finally:
        lab.close_serial(port)

    if args.summary and result["读完"]:
        write_summary(args.summary, result)
        result = {"代号": "E-0001", "这一次": args.label, "读完": True, "启动号": result["启动号"],
                  "模块一致": result["模块一致"], "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result["判据"] if check["通过"] is False]}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
