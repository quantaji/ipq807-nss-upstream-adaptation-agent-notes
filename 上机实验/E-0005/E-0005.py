#!/usr/bin/env python3
"""E-0005（主机发包的反压：每个口的发包环一个流控组）的读数脚本。

要回答的事和判据在同一个文件夹的 报告.md 第 1 节。量法大多是 E-0004 的读数脚本里现成的阶段，
这份脚本把它们原样拿来用（导入 ../E-0004/E-0004.py），另外加四个阶段：

  setup   换上一个实验用的模块，带上加载时的参数；再写运行时的参数；把硬件里真的成了什么样读回来。
          --file 模块的文件名（在 工作文件夹/实验用的模块/ 里），或者 normal（换回镜像里带的正式的模块）
          --insmod "exp_fc_group=1 exp_tx_ring_xg=1024 …"   加载时的参数
          --cpu-bm "开不开流控 上限 保留的 权重"              对主机发来的包的缓冲管理（运行时写）
          --uni-ac "上限 权重 被流控的包也照上限丢不丢"       每条单播队列的丢包上限（运行时写）
          --prehdr-src 0 或 1                                  前置头里"从哪里来"填不填出口的口号（运行时写）
  run     做 E-0004 的一个阶段（--inner 阶段名，别的参数照 E-0004 的写），前后另外读几样、中间在路由器上每 3 秒记一次：
          每个口三本账（发包环收到的帧、这个口的 MAC 发出去的帧、主机网卡收到的帧）；PPE 里三个口的出口队列丢了多少；
          每条发送队列"最多许压多少字节"和"压着多少字节"；发送队列的排队规则记的数；缓冲管理 15 个口；发包环的位置；内存；
          内核记录里硬件报错的行、"发送队列超时"的行数；路由器上每个 iperf3 进程在哪个核心上。
          --ping-during lan2 或 wan：阶段开始 8 秒以后，主机另外从这一路 ping 路由器 200 个（每 0.1 秒一个），报回了几个。
  fwd2    转发带得动多少：eno2、USB 网卡各放进一个网络命名空间，两个千兆口一起进、万兆口出，30 秒。
  cells   主机发的帧前面那 32 字节的前置头算不算进缓冲的份数：压住 lan3，路由器往 lan3 发 40 个定长的帧，读占了多少份。
  base5   E-0004 的判据第 1、2、3、4、12、13 条，在定下来的驱动上重做。第 2 条里跟着这次改动变了的三样照新的判：
          万兆口的发包环（16 到 23 号）长度是 1024；每个发包环的控制字是"使能"加上口号左移一位；
          缓冲管理里算主机发来的包的 8 个口流控开关都是 1。
  finish  收工的第一步：删掉主机 br-lan 的测试地址，回读，和开工以前记下的比。不碰路由器。

例子（在这个文件夹里跑，前面带 PYTHONDONTWRITEBYTECODE=1）：
  python3 E-0005.py --stage setup --label 候选一 --work /tmp/E-0005 --module /tmp/E-0005/模块.ko \\
      --file variant-e5.ko --insmod "exp_fc_group=1" --cpu-bm "1 250 100 4" \\
      --output /tmp/E-0005/读数/候选一-配置 --summary /tmp/E-0005/读数/结果-候选一-配置.json
  python3 E-0005.py --stage run --inner txrate --label 候选一-只往wan-1 --work /tmp/E-0005 --module /tmp/E-0005/模块.ko \\
      --output /tmp/E-0005/读数/候选一-只往wan-1 --summary /tmp/E-0005/读数/结果-候选一-只往wan-1.json

工作文件夹（--work）是上机期间放东西的地方；每一遍的摘要也先写在它里面，做完由监督者合成一个结果摘要放进仓库。
主机这一头做的事和 E-0004 的读数脚本一样；fwd2 另外把 eno2 也放进一个网络命名空间，做完放回来、把测试地址加回去。
路由器这一头的改动都只在内存里。地址（MAC）的数值不显示、不写进任何文件。

结果是标准输出最后一行的一个 JSON 对象。退出码 0 表示读完了，1 表示没读完，2 表示用法错误。
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
_spec = importlib.util.spec_from_file_location("e0004", os.path.join(HERE, "..", "E-0004", "E-0004.py"))
e4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e4)
e3, lab, ask, read_registers = e4.e3, e4.lab, e4.ask, e4.read_registers

PARAM = "/sys/module/ipq807x_eth/parameters"
PARAM_NAMES = ("exp_fc_group", "exp_tx_ring_xg", "exp_txcmpl_ring", "exp_rx_thread", "exp_tx_thread", "exp_prehdr_src",
               "exp_cpu_bm", "exp_uni_ac")
# 每个口的第一条单播队列：主机发给这个口的包进的就是它（144 加上口号减 1 的 16 倍）。
PORT_QUEUE = {"lan3": 176, "lan2": 192, "wan": 208}
# PPE 里每条单播队列丢了多少：每条队列 6 项，每项 4 个字，第一个字是丢的帧数（厂商的库 hppe_qm_reg.h 的 UNI_DROP_CNT_TBL）。
UNI_DROP = 0x800000 + 0x1e0000
HOST_NICS = ("eno1", "eno2", e4.USB)
DURING = "/tmp/e5-during.txt"
PAUSE_SCRIPT = os.path.join(HERE, "..", "E-0004", "发暂停帧.py")
NETNS_A, NETNS_B = "e0005a", "e0005b"


def module_params(port, prompt, folder, tag):
    """实验用的模块的参数现在各是多少；正式的模块没有这些参数。"""
    names = " ".join(PARAM_NAMES)
    lines = ask(port, prompt, folder, f"{tag}-模块的参数",
                f'for n in {names}; do echo "$n=$(cat {PARAM}/$n 2>/dev/null || echo 没有这个参数)"; done', 20).split("\n")
    return dict(line.split("=", 1) for line in lines if "=" in line)


def ring_lines(port, prompt, folder, tag):
    """每个发包环和发完环：长度、硬件和驱动各到了哪里、控制字、还占着几格（实验用的模块才读得到）。"""
    text = ask(port, prompt, folder, f"{tag}-发包环", f"cat {PARAM}/exp_rings 2>/dev/null; true", 20)
    return [line for line in text.split("\n") if line]


def queue_drops(port, prompt, folder, tag):
    """PPE 里三个口的第一条单播队列到现在各丢了多少帧（6 项各自的数）。"""
    drops = {}
    for name, queue in PORT_QUEUE.items():
        words = read_registers(port, prompt, folder, f"{tag}-PPE队列丢的-{name}", UNI_DROP + queue * 6 * 16, 24)
        drops[name] = [words[index * 4] for index in range(6)]
    return drops


def queue_limits(port, prompt, folder, tag):
    """三个口每条发送队列"最多许压多少字节"和"压着多少字节"。"""
    paths = " ".join(f"/sys/class/net/{name}/queues/tx-*/byte_queue_limits" for name in PORT_QUEUE)
    limits = {}
    for line in ask(port, prompt, folder, f"{tag}-发送队列的账",
                    f'for f in {paths}; do echo "$f $(cat $f/limit) $(cat $f/inflight)"; done', 20).split("\n"):
        path, limit, inflight = line.split()
        fields = path.split("/")
        limits[f"{fields[4]} {fields[6]}"] = {"最多许压": int(limit), "压着": int(inflight)}
    return limits


def host_side(folder, tag):
    """主机三块网卡收发了多少帧、丢了多少，和每块网卡的排队规则丢了多少。只读。"""
    reading = {}
    for nic in HOST_NICS:
        if not os.path.exists(f"/sys/class/net/{nic}"):
            reading[nic] = None
            continue
        counts = {item: e3.host_stat(nic, item) for item in ("rx_packets", "tx_packets", "rx_dropped", "tx_dropped")}
        text = e3.host(["tc", "-s", "qdisc", "show", "dev", nic], folder, f"{tag}-主机{nic}的排队规则")[1]
        found = re.search(r"dropped (\d+)", text)
        counts["排队规则丢的"] = int(found.group(1)) if found else None
        reading[nic] = counts
    return reading


def extras(port, prompt, folder, tag):
    """E-0004 的阶段不读、这次另外要读的一组数。"""
    memory = {}
    for line in ask(port, prompt, folder, f"{tag}-内存",
                    "grep -E '^(MemFree|MemAvailable|Cached|Slab|SReclaimable|SUnreclaim):' /proc/meminfo", 15).split("\n"):
        name, value = line.split(":")
        memory[name] = int(value.split()[0])
    qdisc = ask(port, prompt, folder, f"{tag}-排队规则",
                "for d in wan lan2 lan3; do echo \"== $d\"; tc -s qdisc show dev $d | grep -E 'qdisc|Sent|backlog'; done", 20)
    logged = ask(port, prompt, folder, f"{tag}-内核记录",
                 'echo "超时 $(dmesg | grep -c \'NETDEV WATCHDOG\') $(dmesg | grep -c \'TX queue\')"; '
                 "dmesg | grep 'EDMA: ' | tail -n 40; true", 20).split("\n")
    return {"一组数": e4.snapshot(port, prompt, folder, f"{tag}-一组数"), "PPE队列丢的": queue_drops(port, prompt, folder, tag),
            "内核记录里发送队列超时的行数（内核的、驱动的）": logged[0].split()[1:],
            "内核记录里硬件报错的行": [line for line in logged[1:] if line],
            "发送队列的账": queue_limits(port, prompt, folder, tag), "排队规则": qdisc.split("\n"),
            "缓冲管理": e4.bm_ports(port, prompt, folder, tag), "发包环": ring_lines(port, prompt, folder, tag),
            "内存（kB）": memory, "主机": host_side(folder, tag)}


def books(before, after):
    """每个口三本账：发包环收到的帧、这个口的 MAC 发出去的帧、主机网卡收到的帧；外加 PPE 里这个口的队列丢的。"""
    delta = e4.snapshot_delta(before["一组数"], after["一组数"])
    host_nic = {"wan": "eno1", "lan2": "eno2", "lan3": e4.USB}
    rows = {}
    for name, reading in delta["路由器的口"].items():
        nic = host_nic[name]
        old, new = before["主机"][nic], after["主机"][nic]
        # MAC 发出去的帧里有它自己发的暂停帧（记在组播里），这些不是从发包环来的，先减掉。
        sent = reading["MAC发出去的帧"] - reading["发出的暂停帧"]
        rows[name] = {"发包环收到的帧": sum(reading["每个发包环发了多少"]), "这个口的MAC发出去的帧": sent,
                      "MAC自己发的暂停帧（已经减掉）": reading["发出的暂停帧"],
                      "发包环收到的减MAC发出去的": sum(reading["每个发包环发了多少"]) - sent,
                      "主机网卡收到的帧": new["rx_packets"] - old["rx_packets"] if old and new else None,
                      "PPE里这个口的队列丢的（6项）": [b - a for a, b in zip(before["PPE队列丢的"][name], after["PPE队列丢的"][name])],
                      "PPE里这个口的队列丢的-以后读到的原数": after["PPE队列丢的"][name]}
    return rows, delta


def sampler_start(port, prompt, folder, count):
    """在路由器上起一个后台的小循环：每 3 秒记一次缓冲管理 15 个口、每条发送队列压着多少字节、发包环的位置。"""
    used, status = e4.PPE_BM + 0x1c0, e4.PPE_BM + 0x140
    loop = (f'(for i in $(seq {count}); do echo "== $i"; r {used} 15; echo "--"; r {status} 15; echo "--"; bq; echo "--"; '
            f'cat {PARAM}/exp_rings 2>/dev/null; echo "--"; '
            'for p in $(pidof iperf3); do set -- $(cut -d")" -f2 /proc/$p/stat); echo "iperf3 进程$p 核心${37} 用了${12}+${13}"; done; '
            f"sleep 3; done > {DURING} 2>&1 &)")
    ask(port, prompt, folder, "中间-起后台的小循环", loop, 15)


def sampler_read(port, prompt, folder):
    """把后台的小循环记下来的读回来：每一次的各口占着的缓冲、流控的状态字、压着的字节（只留不是 0 的）、每个环占着几格。"""
    text = ask(port, prompt, folder, "中间-读回来", f"cat {DURING} 2>/dev/null; true", 60)
    samples = []
    for block in text.split("== ")[1:]:
        parts = block.split("--\n")
        if len(parts) < 4:
            continue
        values = [[int(line.split(": ")[1], 16) for line in part.split("\n") if ": " in line] for part in parts[:2]]
        held = {}
        for line in parts[2].split("\n"):
            path, _, value = line.partition(" ")
            if value.strip() not in ("", "0"):
                fields = path.split("/")
                held[f"{fields[4]} {fields[6]}"] = int(value)
        rings = {}
        for line in parts[3].split("\n"):
            found = re.match(r"tx (\d+) .* held (\d+)", line)
            if found and found.group(2) != "0":
                rings[found.group(1)] = int(found.group(2))
        processes = [line for line in parts[4].split("\n") if line.startswith("iperf3")] if len(parts) > 4 else []
        samples.append({"各口占着的缓冲": values[0], "流控的状态字": values[1], "压着的字节": held, "发包环占着几格": rings,
                        "iperf3进程在哪个核心、到这时用了多少嘀嗒": processes})
    return samples


def stage_setup(port, prompt, args, result):
    """换模块、带上参数，把硬件里真的成了什么样读回来。"""
    folder = args.output
    if args.file == "normal":
        result["卸载加载以后"] = e3.switch_module(port, prompt, folder, "换回正式的模块", ["modprobe ipq807x-eth"])
        result["换成了"] = "正式的模块"
    else:
        with open(os.path.join(e3.VARIANTS, args.file), "rb") as file:
            wanted = hashlib.md5(file.read()).hexdigest()
        ask(port, prompt, folder, "路由器的桥上的测试地址", f"ip addr replace {e4.ROUTER_USB_SIDE}/24 dev br-lan", 15)
        server = subprocess.Popen([sys.executable, "-m", "http.server", "--bind", e4.USB_ADDRESS, "--directory",
                                   e3.VARIANTS, "8000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            ask(port, prompt, folder, "取实验用的模块",
                f"uclient-fetch -q -O /tmp/variant.ko http://{e4.USB_ADDRESS}:8000/{args.file}", 60)
        finally:
            # 边界：文件服务不管取没取成都要停掉。
            lab.stop_process(server, 15, 5)
        if ask(port, prompt, folder, "取到的模块的校验值", "md5sum /tmp/variant.ko | cut -c1-32", 15) != wanted:
            raise RuntimeError("取到路由器上的模块和主机上的不一样")
        result["卸载加载以后"] = e3.switch_module(port, prompt, folder, "换上实验用的模块",
                                             [f"insmod /tmp/variant.ko {args.insmod}".strip()])
        result["换成了"] = args.file
        result["模块的校验值"] = wanted
        result["加载时的参数"] = args.insmod
    result["重新绑核以后"] = e4.rebind(port, prompt, folder, "换模块以后")
    result["主机br-lan的测试地址在"] = e3.ensure_bridge_address(folder, "换模块以后")
    wrote = {}
    for name, value in (("exp_cpu_bm", args.cpu_bm), ("exp_uni_ac", args.uni_ac), ("exp_prehdr_src", args.prehdr_src)):
        if value is not None and args.file != "normal":
            ask(port, prompt, folder, f"写-{name}", f'echo "{value}" > {PARAM}/{name}', 15)
            wrote[name] = value
    result["运行时写的参数"] = wrote
    result["参数读回"] = module_params(port, prompt, folder, "读回")
    result["发包环"] = ring_lines(port, prompt, folder, "读回")
    switch = [word & 1 for word in read_registers(port, prompt, folder, "读回-缓冲管理的流控开关", e4.PPE_BM + 0x100, 15)]
    result["缓冲管理15个口的流控开关"] = switch
    result["缓冲管理0号口和5号口的两个字"] = {
        index: [f"{word:08x}" for word in read_registers(port, prompt, folder, f"读回-缓冲管理{index}号口", e4.PPE_BM + 0x1000 + index * 16, 2)]
        for index in (0, 5)}
    result["队列的上限的第一个字"] = {
        name: f"{read_registers(port, prompt, folder, f'读回-{name}的队列的上限', 0x800000 + 0x48000 + queue * 16, 1)[0]:08x}"
        for name, queue in PORT_QUEUE.items()}
    controls = {}
    for line in result["发包环"]:
        found = re.match(r"tx (\d+) port (\d+) .* size (\d+) .* ctrl (0x[0-9a-f]+)", line)
        if found:
            controls[found.group(1)] = {"口号": int(found.group(2)), "长度": int(found.group(3)), "控制字": found.group(4)}
    result["每个发包环的口号、长度、控制字"] = controls
    result["判据"] = []
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {"参数读回": result["参数读回"], "缓冲管理15个口的流控开关": switch,
                       "每个发包环的口号、长度、控制字": controls, "队列的上限的第一个字": result["队列的上限的第一个字"],
                       "线程": result["重新绑核以后"]["线程"]}


def stage_run(port, prompt, args, result):
    """做 E-0004 的一个阶段，前后另外读一组数，中间在路由器上每 3 秒记一次。"""
    folder = args.output
    before = extras(port, prompt, folder, "另外读-以前")
    sampler_start(port, prompt, folder, args.samples)
    pinger = None
    if args.ping_during:
        source, target = {"lan2": (e4.ENO2_ADDRESS, e4.ROUTER_LAN), "wan": (e4.BRIDGE_ADDRESS, e4.ROUTER_BRIDGE_SIDE)}[args.ping_during]
        pinger = subprocess.Popen(["sh", "-c", f"sleep 8; ping -n -q -c 200 -i 0.1 -I {source} {target}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        e4.STAGES[args.inner](port, prompt, args, result)
        if pinger:
            result[f"阶段中间主机ping-{args.ping_during}（发了、回了）"] = list(e3.parse_ping(pinger.communicate(timeout=60)[0]))
    finally:
        if pinger:
            # 边界：ping 的进程要收掉。
            lab.stop_process(pinger, 2, 5)
    time.sleep(2)
    after = extras(port, prompt, folder, "另外读-以后")
    rows, delta = books(before, after)
    result["三本账"] = rows
    result["整个阶段前后涨了的"] = delta
    result["中间每3秒记的"] = sampler_read(port, prompt, folder)
    result["另外读的"] = {"以前": {key: value for key, value in before.items() if key != "一组数"},
                       "以后": {key: value for key, value in after.items() if key != "一组数"}}
    result["模块的参数"] = module_params(port, prompt, folder, "这一遍")
    uneven = [f"{name}：发包环收到的比 MAC 发出去的多 {row['发包环收到的减MAC发出去的']} 帧" for name, row in rows.items()
              if abs(row["发包环收到的减MAC发出去的"]) > 20]
    result["读数里不对劲的地方"] = result.get("读数里不对劲的地方", []) + uneven
    look = dict(result.get("要看的数", {}))
    look["三本账"] = {name: {key: row[key] for key in ("发包环收到的帧", "这个口的MAC发出去的帧", "主机网卡收到的帧")}
                    for name, row in rows.items()}
    look["中间各口占着的缓冲最多到多少"] = [max(column) for column in zip(*[sample["各口占着的缓冲"] for sample in result["中间每3秒记的"]])] \
        if result["中间每3秒记的"] else None
    result["要看的数"] = look


def into_netns(folder, netns, nic, address, route):
    """把主机的一块网卡放进一个网络命名空间，在里面配上地址和一条路由。"""
    steps = [("建命名空间", ["ip", "netns", "add", netns]),
             ("放进去", ["ip", "link", "set", "dev", nic, "netns", netns]),
             ("加地址", ["ip", "-n", netns, "addr", "add", f"{address}/24", "dev", nic]),
             ("打开", ["ip", "-n", netns, "link", "set", "dev", nic, "up"]),
             ("加路由", ["ip", "-n", netns, "route", "add", route[0], "via", route[1]])]
    for name, argv in steps:
        if e3.host(["sudo", "-n"] + argv, folder, f"{nic}进命名空间-{name}")[0]:
            raise RuntimeError(f"{nic} 放进网络命名空间：{name}没有做成")


def out_of_netns(folder, netns, nic, address):
    """把网卡放回来，把它平时的测试地址加回去（做法和 E-0004 的读数脚本一样）。"""
    e3.host(["sudo", "-n", "ip", "netns", "del", netns], folder, f"{nic}出命名空间-删命名空间")
    for _ in range(20):
        if os.path.exists(f"/sys/class/net/{nic}"):
            break
        time.sleep(0.5)
    e3.host(["sudo", "-n", "nmcli", "device", "set", nic, "managed", "no"], folder, f"{nic}出命名空间-NetworkManager不管它")
    time.sleep(2)
    e3.host(["sudo", "-n", "ip", "-4", "addr", "flush", "dev", nic], folder, f"{nic}出命名空间-清掉网卡上的地址")
    e3.host(["sudo", "-n", "ip", "addr", "replace", f"{address}/24", "dev", nic], folder, f"{nic}出命名空间-测试地址加回去")
    e3.host(["sudo", "-n", "ip", "link", "set", "dev", nic, "up"], folder, f"{nic}出命名空间-打开")


def stage_fwd2(port, prompt, args, result):
    """转发带得动多少：两个千兆口（lan2、lan3）一起进，经路由从万兆口 wan 出，30 秒，每一路 64 条 TCP 流。"""
    folder = args.output
    route = ("192.168.13.0/24", e4.ROUTER_LAN)
    senders = ((NETNS_A, "eno2", e4.ENO2_ADDRESS, e4.ENO2_ADDRESS, 5201),
               (NETNS_B, e4.USB, e4.USB_NETNS_ADDRESS, e4.USB_ADDRESS, 5202))
    e3.router_addresses(port, prompt, folder, "转发", "wan")
    e3.ensure_bridge_address(folder, "转发")
    servers, clients, inside = [], [], []
    try:
        for netns, nic, address, _usual, _port in senders:
            into_netns(folder, netns, nic, address, route)
            inside.append((netns, nic))
        for netns, nic, address, _usual, _port in senders:
            if not e4.wait_ping(folder, f"等{nic}连上", address, e4.BRIDGE_ADDRESS, netns):
                raise RuntimeError(f"{nic} 放进网络命名空间以后 40 秒里 ping 不通主机的 br-lan")
        for _netns, _nic, _address, _usual, server_port in senders:
            servers.append(subprocess.Popen(["iperf3", "-s", "-1", "-B", e4.BRIDGE_ADDRESS, "-p", str(server_port)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(2)
        before = extras(port, prompt, folder, "另外读-以前")
        for netns, _nic, address, _usual, server_port in senders:
            argv = ["sudo", "-n", "ip", "netns", "exec", netns] + \
                e4.iperf_argv(e4.BRIDGE_ADDRESS, address, 30, args.flows, server_port=server_port)
            clients.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        reports = [e4.iperf_report(client.communicate(timeout=120)[0]) for client in clients]
        after = extras(port, prompt, folder, "另外读-以后")
    finally:
        # 边界：进程要收掉，网卡要放回来，不管中间出没出事。
        for process in clients + servers:
            lab.stop_process(process, 2, 5)
        for (netns, nic), sender in zip(inside, senders):
            out_of_netns(folder, netns, nic, sender[3])
    rows, delta = books(before, after)
    result["两路各自的iperf3"] = {senders[index][1]: report for index, report in enumerate(reports)}
    result["三本账"] = rows
    result["整个阶段前后涨了的"] = delta
    result["另外读的"] = {"以前": {key: value for key, value in before.items() if key != "一组数"},
                       "以后": {key: value for key, value in after.items() if key != "一组数"}}
    result["模块的参数"] = module_params(port, prompt, folder, "这一遍")
    ports = delta["路由器的口"]
    result["判据"] = []
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {"两路每10秒的速率": {senders[index][1]: report.get("每10秒的速率") for index, report in enumerate(reports)},
                       "两路的重传": {senders[index][1]: report.get("重传") for index, report in enumerate(reports)},
                       "lan2和lan3的MAC收到的帧": [ports["lan2"]["MAC收到的帧"], ports["lan3"]["MAC收到的帧"]],
                       "wan的MAC发出去的帧": ports["wan"]["MAC发出去的帧"],
                       "四个核心空闲百分之几": [core["空闲"] for core in delta["四个核心各忙在哪里（百分之几）"]]}


def stage_cells(port, prompt, args, result):
    """主机发的帧占几份缓冲：用暂停帧压住 lan3，路由器往主机的 USB 网卡发 40 个定长的 ping，读缓冲管理多占了多少份。

    ping 带 N 字节数据时，以太帧（不含校验码）是 N 加 42 字节。一份缓冲 256 字节。
    前置头的 32 字节不算进去的话，带 182、214、215、470、471 字节的帧各占 1、1、2、2、3 份；算进去的话是 1、2、2、3、3 份。
    """
    folder = args.output
    target = e4.USB_ADDRESS
    ask(port, prompt, folder, "先ping通", f"ping -q -c 3 -W 1 {target}; true", 20)
    rows = []
    for size in (182, 214, 215, 470, 471):
        idle = e4.bm_ports(port, prompt, folder, f"{size}-压以前")
        blocker = subprocess.Popen(["sudo", "-n", "python3", "-B", PAUSE_SCRIPT, e4.USB, "14"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            time.sleep(2)
            ask(port, prompt, folder, f"{size}-发40个",
                f"for i in $(seq 40); do ping -q -c 1 -W 1 -s {size} {target} >/dev/null 2>&1 & done; wait; true", 30)
            paused = e4.bm_ports(port, prompt, folder, f"{size}-压着")
            frames_sent = blocker.communicate(timeout=40)[0].strip()
        finally:
            # 边界：发暂停帧的进程要收掉。
            lab.stop_process(blocker, 2, 5)
        time.sleep(3)
        freed = e4.bm_ports(port, prompt, folder, f"{size}-放开以后")
        rise = [b - a for a, b in zip(idle["各口占着的缓冲"], paused["各口占着的缓冲"])]
        rows.append({"ping带的字节": size, "以太帧的字节（不含校验码）": size + 42, "发了几个": 40,
                     "压着时各口多占的缓冲": rise, "压着时各口占着的缓冲": paused["各口占着的缓冲"],
                     "放开以后各口占着的缓冲": freed["各口占着的缓冲"], "主机发了几个暂停帧": frames_sent})
    result["每一种长度"] = rows
    result["模块的参数"] = module_params(port, prompt, folder, "这一遍")
    result["判据"] = []
    result["读数里不对劲的地方"] = []
    result["要看的数"] = {row["以太帧的字节（不含校验码）"]: max(row["压着时各口多占的缓冲"][:8]) for row in rows}


# 定下来的驱动里每个发包环的口号和长度（E-0005 定的）：万兆口 1024 格，千兆口 128 格。
TX_RING_PORT = {10: 2, 11: 2, 12: 3, 13: 3, 14: 4, 15: 4, 16: 5, 17: 5, 18: 5, 19: 5, 20: 6, 21: 6, 22: 6, 23: 6}
XG_TX_RING, G_TX_RING = 1024, 128
TRUE_TX_RINGS = {}


def read_registers_for_base(port, prompt, folder, name, address, count):
    """给 E-0004 的 base 阶段用的读寄存器：发包环那两个字（长度、控制字）照原样记在 TRUE_TX_RINGS 里。

    E-0004 的判法写死了"发包环长度是 128"。万兆口的环读到 1024 时，交给它判的那一份写成 128，
    好让它把别的寄存器照常判完；真的长度由 stage_base5 自己判，摘要里记的也是真的长度。
    """
    words = read_registers(port, prompt, folder, name, address, count)
    found = re.fullmatch(r"2-发包环(\d+)", name)
    if found:
        ring = int(found.group(1))
        TRUE_TX_RINGS[ring] = list(words)
        if ring >= 16 and words[0] & 0xffff == XG_TX_RING:
            words = [words[0] & ~0xffff | G_TX_RING] + list(words[1:])
    return words


def stage_base5(port, prompt, args, result):
    """E-0004 的判据第 1、2、3、4、12、13 条；第 2 条里发包环的长度、控制字、流控开关照定下来的驱动判。"""
    e4.read_registers = read_registers_for_base
    try:
        e4.stage_base(port, prompt, args, result)
    finally:
        # 边界：E-0004 的模块里换掉的函数要换回去。
        e4.read_registers = read_registers
    check = next(item for item in result["判据"] if item["编号"] == "2")
    sizes = {ring: TRUE_TX_RINGS[ring][0] & 0xffff for ring in TX_RING_PORT}
    controls = {ring: TRUE_TX_RINGS[ring][1] & 0x3f for ring in TX_RING_PORT}
    sizes_ok = all(sizes[ring] == (XG_TX_RING if ring >= 16 else G_TX_RING) for ring in TX_RING_PORT)
    controls_ok = all(controls[ring] == 1 | port_id << 1 for ring, port_id in TX_RING_PORT.items())
    switch_ok = check["缓冲管理15个口的流控开关"][:8] == [1] * 8
    for ring in TX_RING_PORT:
        check["环的长度和开关"][f"发包环{ring}"] = [sizes[ring], controls[ring] & 1]
    check["发包环的控制字"] = {ring: f"{value:#x}" for ring, value in controls.items()}
    check["发包环的长度照定下来的"] = sizes_ok
    check["发包环的控制字照定下来的"] = controls_ok
    check["算主机发来的包的8个口流控都开着"] = switch_ok
    check["通过"] = bool(check["通过"] and sizes_ok and controls_ok and switch_ok)


def edma_lines(port, prompt, folder, tag):
    """量帧长时每个数以后读的"硬件报错的状态字"：驱动记的是以 EDMA: 开头的行（E-0004 的读数脚本找的字和驱动写的对不上）。"""
    return ask(port, prompt, folder, f"{tag}-硬件报错的行", "dmesg | grep 'EDMA: ' | tail -n 6; true", 20).split("\n")


e4.misc_status_lines = edma_lines
STAGES = dict(e4.STAGES, setup=stage_setup, run=stage_run, fwd2=stage_fwd2, cells=stage_cells, base5=stage_base5)


def main():
    """读命令行，做一个阶段，把结果打印成一行 JSON。E-0004 的阶段用到的参数名字和意思都照 E-0004 的。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES) + ["finish"], help="做哪一个阶段")
    parser.add_argument("--inner", choices=sorted(e4.STAGES), help="run 阶段：做 E-0004 的哪一个阶段")
    parser.add_argument("--samples", type=int, default=12, help="run 阶段：中间每 3 秒记一次，一共记几次（默认 12）")
    parser.add_argument("--ping-during", choices=["lan2", "wan"], help="run 阶段：阶段中间主机另外从这一路 ping 路由器 200 个")
    parser.add_argument("--label", required=True, help="这一次读数叫什么")
    parser.add_argument("--work", required=True, help="上机期间放东西的工作文件夹（里面有 实验用的模块/）")
    parser.add_argument("--module", required=True, help="主机上编出来的正式的 ipq807x-eth.ko，和镜像里带的那一个比")
    parser.add_argument("--file", help="setup、load 阶段：实验用的模块的文件名，或者 normal")
    parser.add_argument("--insmod", default="", help="setup 阶段：加载实验用的模块时带的参数")
    parser.add_argument("--prehdr-src", choices=["0", "1"], help='setup 阶段：前置头里"从哪里来"填不填出口的口号')
    parser.add_argument("--with-lan2", action="store_true", help="txrate 阶段：同时往 lan2 发")
    parser.add_argument("--ports", help="swapped 阶段：口=主机网卡，用逗号隔开")
    parser.add_argument("--mtu-ports", help="swapped 阶段：要量到上限的口，用逗号隔开")
    parser.add_argument("--light-ports", help="swapped 阶段：要做关口再开和统计的口")
    parser.add_argument("--number", default="22", help="swapped 阶段：判据的编号")
    parser.add_argument("--kind", choices=["in", "out", "through", "wan-in", "wan-out", "bridge", "route-up", "route-down",
                                           "pause", "pause-two", "pause-wan", "linkdown", "close"],
                        help="long、forward、stall 阶段：哪一种")
    parser.add_argument("--layout", choices=["万兆口四个", "每个口两个"], default="万兆口四个", help="发包环是哪一种排法")
    parser.add_argument("--flows", type=int, default=e4.FLOWS, help="txrate、forward、fwd2 阶段：几条流（默认 64）")
    parser.add_argument("--main", choices=["tcp", "udp"], default="tcp", help="txrate、forward 阶段：发的是 TCP 还是限速的 UDP")
    parser.add_argument("--rate", default="100M", help="--main udp 时每条流限到多少")
    parser.add_argument("--side", choices=["none", "tcp", "udp"], default="none", help="txrate 阶段：同时往 lan2 发什么")
    parser.add_argument("--side-flows", type=int, default=e4.FLOWS, help="--side 的那一组几条流")
    parser.add_argument("--side-rate", default="100M", help="--side udp 时每条流限到多少")
    parser.add_argument("--diag", action="store_true", help="txrate 阶段：把两头的全部网络计数存下来，列出涨了一千以上的")
    parser.add_argument("--seconds", type=int, default=1800, help="long 阶段：跑多少秒")
    parser.add_argument("--cpu-bm", help='setup 阶段：对主机发来的包的"开不开流控 上限 保留的 权重"；limits 阶段也用')
    parser.add_argument("--uni-ac", help='setup 阶段：每条单播队列的"上限 权重 被流控的包也照上限丢不丢"')
    parser.add_argument("--frame-port", choices=[path[0] for path in e4.FRAME_PATHS], help="maxframe 阶段：只量这一个口")
    parser.add_argument("--jumbo-mtu", type=int, default=e4.MAX_MTU, help="jumbo 阶段：两头的 MTU 设成多少")
    parser.add_argument("--with-pause", action="store_true", help="jumbo 阶段：另外量长帧发着的时候这个口被暂停帧压住")
    parser.add_argument("--frame-low", type=int, help="maxframe 阶段：从这个数接着找")
    parser.add_argument("--frame-high", type=int, help="maxframe 阶段：最小的不通的数")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    args = parser.parse_args()
    inner = args.inner if args.stage == "run" else args.stage
    if args.stage == "run" and not args.inner:
        parser.error("run 阶段要用 --inner 写明做 E-0004 的哪一个阶段")
    if args.stage in ("setup", "load") and not args.file:
        parser.error("这个阶段要用 --file 写明换成哪一个")
    if inner in ("long", "forward", "stall") and not args.kind:
        parser.error("这个阶段要用 --kind 写明哪一种")
    if inner == "swapped" and not args.ports:
        parser.error("swapped 阶段要用 --ports 写明哪个口接着哪块网卡")
    e4.LAYOUT = args.layout
    e3.VARIANTS = os.path.join(args.work, "实验用的模块")
    e3.BRIDGE_SNAPSHOT = os.path.join(args.work, "主机br-lan-开工以前.txt")

    lab.stop_on_signals()
    if args.stage == "finish":
        result = e4.finish(args)
        result["代号"] = "E-0005"
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
        result["代号"] = "E-0005"
        for index, helper in enumerate(e4.ROUTER_HELPERS):
            ask(port, prompt, args.output, f"01-定义小函数-{index}", helper, 15)
        STAGES[args.stage](port, prompt, args, result)
    except (RuntimeError, ValueError, KeyError, IndexError, OSError, TimeoutError, termios.error,
            subprocess.SubprocessError) as error:
        # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
        result = {"代号": "E-0005", "这一次": args.label, "阶段": args.stage, "读完": False, "原因": str(error)}
    finally:
        if level is not None:
            lab.serial_command(port, f"dmesg -n {level}", prompt, 15)
        lab.close_serial(port)
    if result["读完"]:
        e3.e0002.write_summary(args.summary, result)
        result = {"代号": "E-0005", "这一次": args.label, "阶段": args.stage, "读完": True, "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result["判据"] if not check["通过"]],
                  "读数里不对劲的地方": result.get("读数里不对劲的地方", []),
                  "要看的数": result.get("要看的数", {})}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
