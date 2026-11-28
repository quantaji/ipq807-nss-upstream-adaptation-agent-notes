#!/usr/bin/env python3
"""E-0007（芯片怎样排队、处理器时间怎样分：量常数、检验模型、量算出来的做法）的读数脚本。

要回答的事、每一遍量什么、判据在同一个文件夹的 报告.md 第 1 节。E-0003 到 E-0006 的读数脚本里现成的函数原样拿来用
（导入 ../E-0006/E-0006.py，它一层一层导入前面几份）。实验用的模块是 variant-e7.ko（差异在 variant-e7.diff），
它自己记每一轮轮询、每个核心的时间、每个线程等了多久，经 debugfs 的 ipq807x_diag 目录读出来。

阶段：
  hosthold 开工时做一次，不碰路由器：把 NetworkManager 里 br-lan 的连接改成固定用测试地址（E-0006 的 hosthold）。Wi-Fi 不碰。
  prep     每一次内存引导以后做一次：主机三块网卡的测试地址、路由器的测试地址和防火墙放行、把 工作文件夹/工具/ 里的
           文件传到路由器的 /tmp、起 iperf3 的服务端、读看门狗的状态。
  setup    换上实验用的模块（--file、--insmod），或者 --file normal 换回正式的模块；绑核；读回参数和诊断的配置。
  plan     照 --plan 点名的一张表做：表里每一行是一窗（先起负载，等稳，叫模块记若干秒，停负载，读回）。
           表在这份脚本的 PLANS 里。--only 只做表里名字带这个字的几窗。
  replay   E-0006 的 fwdmix（--kind route-up、route-down）或者 jam --part two 原样做一遍，同时叫模块记。
  stall    试看门狗：四个核心关着中断空转 --seconds 秒，看路由器有没有自己复位。做完多半要重新内存引导。
  fit      不碰路由器：把 工作文件夹/读数/ 下面各个摘要里的每一窗拢到一起，拟合常数，写成一个 JSON。
  finish   收工，不碰路由器：NetworkManager 里 br-lan 的连接改回去、断开，主机的测试地址删掉，回读（E-0006 的 finish）。

例子（在这个文件夹里跑，前面带 PYTHONDONTWRITEBYTECODE=1）：
  python3 E-0007.py --stage plan --plan rxdrop-wan --label 收了就丢-wan --work /tmp/E-0007 \\
      --module /tmp/E-0007/模块.ko --output /tmp/E-0007/读数/收了就丢-wan --summary /tmp/E-0007/读数/结果-收了就丢-wan.json

工作文件夹（--work）里有 实验用的模块/ 和 工具/；每一遍的原始输出和摘要也在它里面，不进仓库。
主机这一头做的事做完都放回去。路由器这一头的改动都只在内存里。地址（MAC）的数值不显示、不写进任何文件：
要用到对面网卡的地址的地方，都在一条 shell 命令里面取、里面用。

结果是标准输出最后一行的一个 JSON 对象。退出码 0 表示读完了，1 表示没读完，2 表示用法错误。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("e0006", os.path.join(HERE, "..", "E-0006", "E-0006.py"))
e6 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e6)
e5, e4, e3, lab, ask = e6.e5, e6.e4, e6.e3, e6.lab, e6.ask

DIAG = "/sys/kernel/debug/ipq807x_diag"
PARAM = e5.PARAM
LOAD_PARAMS = ("exp_no_fc", "exp_tx_thread", "exp_rx_softirq", "exp_combined", "exp_rx_hardirq", "exp_tx_hardirq",
               "exp_defer_irqs", "exp_gro_flush_us", "exp_cpu_q_ceiling", "exp_rx_weight", "exp_tx_weight",
               "exp_cmpl_shift", "exp_rx_mod", "exp_tx_mod", "exp_rx_ring", "exp_tx_ring", "exp_txcmpl_ring",
               "exp_bm_react", "exp_bm_ceiling", "exp_bm_weight", "exp_bm_group", "exp_bm_resume", "exp_raw_io",
               "exp_page_idx", "exp_sync_len", "diag_cap")
RUN_PARAMS = ("diag_xmit_time", "exp_tx_budget", "exp_rx_drop_port", "exp_rx_delay_us", "exp_fill_lag", "exp_cons_lag",
              "exp_fill_every", "exp_tx_scan_extra", "exp_hardirq_budget", "exp_rx_swq")
# 模块记的一轮轮询：和 ppe_diag.h 的 struct diag_poll 一样，64 字节。
POLL = struct.Struct("<QIIII17HBBI")
POLL_FIELDS = ("start", "dur", "since_irq", "gap", "xmit_ns", "n", "frames", "fwd", "loc", "ack", "scan", "q_in",
               "q_out", "xmit_loc", "xmit_fwd", "woken", "refill", "p_in", "c_in", "p_out", "c_out", "after_loc",
               "cpu", "flags", "pid")
NO_IRQ = 0xffffffff
MARK_PORT = 57351
DROP_PORT = 5300
SOFTIRQS = ("hi", "timer", "net_tx", "net_rx", "block", "irq_poll", "tasklet", "sched", "hrtimer", "rcu")
BM, EDMA = 0x600000, 0xb00000
RX_RING_IDS, FILL_RING_IDS, CMPL_RING_IDS = (12, 13, 14, 15), (4, 5, 6, 7), (4, 5, 6, 7)
TX_RING_IDS = {"lan3": (12, 13), "lan2": (14, 15), "wan": (16, 17, 18, 19)}
# 暂停帧的计数器（只读它的低 32 位）：wan 用万兆 MAC，lan2、lan3 用千兆 MAC。
PAUSE_REGS = {"wan发": 0x3894, "wan收": 0x3988, "lan2发": 0x16a4, "lan2收": 0x1644, "lan3发": 0x14a4, "lan3收": 0x1444}
# MAC 收到、发出的单播帧数（低 32 位）：测试的流量都是单播。链上"MAC 收到多少"这一格用它。
MAC_REGS = {"wan收帧": 0x3970, "wan发帧": 0x3864, "lan2收帧": 0x1694, "lan2发帧": 0x16f0, "lan3收帧": 0x1494, "lan3发帧": 0x14f0}
TXQ_WORDS = ("压着的字节", "字节上限", "排队规则里的包", "排队规则里的字节")


def standard_regs():
    """定时采样器每一次读的寄存器：[(名字, 地址)]。"""
    regs = [(f"占着{i}", BM + 0x1c0 + 4 * i) for i in range(15)] + [(f"状态字{i}", BM + 0x140 + 4 * i) for i in range(15)]
    for i, ring in enumerate(RX_RING_IDS):
        regs += [(f"收包环{i}生产", EDMA + 0x39004 + 0x1000 * ring), (f"收包环{i}消费", EDMA + 0x39008 + 0x1000 * ring)]
    for i, ring in enumerate(FILL_RING_IDS):
        regs += [(f"空缓冲区环{i}生产", EDMA + 0x29004 + 0x1000 * ring), (f"空缓冲区环{i}消费", EDMA + 0x29008 + 0x1000 * ring)]
    for i, ring in enumerate(CMPL_RING_IDS):
        regs += [(f"发完环{i}生产", EDMA + 0x19004 + 0x1000 * ring), (f"发完环{i}消费", EDMA + 0x19008 + 0x1000 * ring)]
    for name, rings in TX_RING_IDS.items():
        for q, ring in enumerate(rings):
            regs += [(f"{name}发包环{q}生产", EDMA + 0x1004 + 0x1000 * ring), (f"{name}发包环{q}消费", EDMA + 0x1008 + 0x1000 * ring)]
    regs += list(PAUSE_REGS.items())
    # 高通的库里的名字：PORT_REACTED_CNT（叫停以后还在收的包占着的）、SHARED_GROUP_CNT（共用的那一组用掉多少）、
    # AC_UNI_QUEUE_CNT_TBL（一条队列积着多少，主机的四条队列是 16 到 19 号）、AC_GRP_CNT_TBL、DROP_STAT（缓冲管理丢的）。
    regs += [("共用组用掉", BM + 0x280)] + [(f"叫停以后占着{i}", BM + 0x240 + 4 * i) for i in (4, 5, 10, 11, 12)]
    regs += [(f"主机队列{q}积着", 0x800000 + 0x4e000 + 0x10 * (16 + q)) for q in range(4)] + [("队列组用掉", 0x800000 + 0x54000)]
    regs += [(f"缓冲超载丢{i}", 0xe000 + 0x10 * i) for i in (11, 12)] + [(f"缓冲流控丢{i}", 0xe000 + 0x10 * (15 + i)) for i in (11, 12)]
    return regs + list(MAC_REGS.items())


def reg_address(name):
    """采样器、快读器的表里用的名字换成地址。"""
    return dict(standard_regs())[name]


def ctl(port, prompt, folder, name, text):
    """往模块的 ctl 文件里写一条命令。"""
    ask(port, prompt, folder, name, f"echo '{text}' > {DIAG}/ctl", 30)


def set_sampler_regs(port, prompt, folder):
    """把定时采样器要读的寄存器告诉模块。一条命令太长串口上容易出错，分成几条：头一条是 regs，后面的是 regs+（接着加）。"""
    addresses = [f"{address:x}" for _, address in standard_regs()]
    for index in range(0, len(addresses), 30):
        ctl(port, prompt, folder, f"采样器读哪些-{index}", ("regs " if index == 0 else "regs+ ") + " ".join(addresses[index:index + 30]))


def set_param(port, prompt, folder, name, key, value):
    ask(port, prompt, folder, name, f"echo {value} > {PARAM}/{key}", 15)


def ssh_prefix():
    """到路由器的 ssh 命令的前半截：先试 USB 网卡那一路，再试 eno2 那一路（哪块网卡这时在主机这边就走哪一路）。"""
    reasons = []
    for source, nic, target in ((e4.USB_ADDRESS, e4.USB, e4.ROUTER_USB_SIDE), (e4.ENO2_ADDRESS, "eno2", e4.ROUTER_LAN)):
        if not os.path.exists(f"/sys/class/net/{nic}"):
            reasons.append(f"{nic} 不在主机这边")
            continue
        argv, why = lab.ssh_argv(f"root@{target}", source, nic)
        if argv:
            return argv
        reasons.append(why)
    raise RuntimeError("到路由器的 ssh 两路都走不了：" + "；".join(reasons))


def pull(folder, name, remote, timeout=180):
    """经 ssh 在路由器上执行一条只读的命令，把它的输出原样存成 文件夹/名字，返回字节串。"""
    done = subprocess.run(ssh_prefix() + [remote], capture_output=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError(f"{name}：ssh 的退出码是 {done.returncode}：{done.stderr.decode(errors='replace')[-200:]}")
    with open(os.path.join(folder, name), "xb") as file:
        file.write(done.stdout)
    return done.stdout


def fields(words):
    """["a=1", "b=x"] 读成字典；是整数的读成整数。"""
    out = {}
    for word in words:
        key, _, value = word.partition("=")
        out[key] = int(value) if re.fullmatch(r"-?\d+", value) else value
    return out


def parse_summary(text):
    """把模块的 summary 文件读成字典。"""
    out = {"环": {}, "核心": {}, "线程": {}, "记号": {}, "口": {}, "发送队列": {}}
    for line in text.split("\n"):
        w = line.split()
        if not w:
            continue
        if w[0] == "now":
            out["now"] = int(w[1])
        elif w[0] in ("window", "config", "sampler", "watcher"):
            out[w[0]] = fields(w[1:])
        elif w[0] == "ring":
            out["环"][w[1]] = fields(w[2:])
        elif w[0] == "hist" and w[1].startswith("thread"):
            index = int(w[1][6:])
            name = ("rx" if index < 4 else "tx") + str(index % 4)
            out["线程"].setdefault(name, {})["直方-" + w[2]] = [int(v) for v in w[3:]]
        elif w[0] == "hist":
            out["环"][w[1]]["直方-" + w[2]] = [int(v) for v in w[3:]]
        elif w[0] == "cpu":
            core = out["核心"].setdefault(int(w[1]), {"xmit": {}})
            if w[2] == "xmit":
                core["xmit"][w[3]] = fields(w[4:])
            elif "=" in w[2]:
                core.update(fields(w[2:]))
            else:
                core[w[2]] = [int(v) for v in w[3:]]
        elif w[0] == "task_classes":
            out["线程的类别"] = w[1:]
        elif w[0] == "thread":
            out["线程"].setdefault(w[1], {}).update(fields(w[2:]))
        elif w[0] == "mark":
            out["记号"][int(w[1])] = int(w[2])
        elif w[0] == "port":
            out["口"][int(w[1])] = fields(w[2:])
        elif w[0] == "txq":
            out["发送队列"][w[1]] = fields(w[2:])
    return out


def trim(values):
    """直方图去掉末尾的 0。"""
    values = list(values)
    while values and not values[-1]:
        values.pop()
    return values


def shares(parsed):
    """每个核心这一窗里的时间各去了哪里（占窗长的百分之几）：硬中断三类、软中断十类、线程七类。

    窗长取模块自己记的开始和结束；模块到时间自己停，停以后不再记。
    """
    window = parsed["window"]
    length = window["stop"] - window["start"]
    out = {}
    for cpu, core in sorted(parsed["核心"].items()):
        row = {"硬中断-收包": core["irq_ns"][0], "硬中断-发完": core["irq_ns"][1], "硬中断-别的": core["irq_ns"][2]}
        row.update({"软中断-" + name: value for name, value in zip(SOFTIRQS, core["sirq_ns"]) if value})
        row.update({"线程-" + name: value for name, value in zip(parsed["线程的类别"], core["task_ns"])})
        row = {name: round(100 * value / length, 2) for name, value in row.items()}
        row["合计"] = round(sum(row.values()), 2)
        row["软中断在ksoftirqd里跑的"] = round(100 * sum(core["sirq_ksoftirqd_ns"]) / length, 2)
        row["切换次数"] = dict(zip(parsed["线程的类别"], core["task_sw"]))
        row["发包的进程等处理器"] = {"次数": core.get("gen_wait_cnt", 0), "一共纳秒": core.get("gen_wait_ns", 0),
                            "最长纳秒": core.get("gen_wait_max", 0), "直方": trim(core.get("hist_gen_wait", []))}
        row["中断次数"] = core["irq_cnt"]
        out[cpu] = row
    return out, length / 1e9


def reduce_summary(parsed):
    """一窗的 summary 压成摘要里留的样子。"""
    share, seconds = shares(parsed)
    rings = {}
    for name, ring in parsed["环"].items():
        rings[name] = {key: (trim(value) if isinstance(value, list) else value) for key, value in ring.items()}
    threads = {}
    for name, thread in parsed["线程"].items():
        threads[name] = {key: (trim(value) if isinstance(value, list) else value) for key, value in thread.items()}
    xmit = {}
    for cpu, core in parsed["核心"].items():
        for where, row in core["xmit"].items():
            total = xmit.setdefault(where, {"loc": 0, "fwd": 0, "loc_bytes": 0, "fwd_bytes": 0, "ns": 0, "各核心的包数": {}})
            for key in ("loc", "fwd", "loc_bytes", "fwd_bytes", "ns"):
                total[key] += row[key]
            if row["loc"] or row["fwd"]:
                total["各核心的包数"][cpu] = row["loc"] + row["fwd"]
    return {"窗长秒": round(seconds, 4), "每个核心的时间（百分之几）": share, "环": rings, "线程": threads,
            "从哪里发的包": {where: row for where, row in xmit.items() if row["loc"] or row["fwd"]},
            "发送被环满挡回的次数": sum(core.get("xmit_busy", 0) for core in parsed["核心"].values()),
            "发的包里不是一整块内存的": sum(core.get("xmit_nonlinear", 0) for core in parsed["核心"].values()),
            "口": parsed["口"], "config": parsed["config"],
            "发送队列（窗里涨的，和读的那一刻的）": {name: row for name, row in parsed["发送队列"].items()
                                     if any(row.values())},
            "窗的开始和结束（路由器的钟，纳秒）": [parsed["window"]["start"], parsed["window"]["stop"]]}


def quantiles(values, points=(50, 90, 99, 100)):
    if not len(values):
        return {}
    return {f"p{p}": float(numpy.percentile(values, p)) for p in points}


RX_COLUMNS = ("常数", "格数", "转发的帧", "交本机的帧", "应答的帧", "后续的缓冲", "轮里发的本机包", "轮里发的转发包", "逐格找的格数",
              "转发的批", "交本机的批", "补的页")
TX_COLUMNS = ("常数", "条数", "叫醒的队列")


def reduce_polls(blob, cap, window, fwd_flows=1, loc_flows=1, sizes=None):
    """八个环的每轮记录（收包环 0 到 3、发完环 0 到 3，各 cap 条）压成回归用的和、分档的平均、几个分位数。

    回归的式子：一轮的用时 = 各列乘各自的系数的和。这里只存 X'X、X'y、y'y 和条数，系数在 fit 阶段解。
    只用开始时刻落在这一窗里的记录。"转发的批"是这一轮转发的帧数和转发的流的条数里小的那个（内核把同一条流的包并成一批往上交），
    "交本机的批"同样算。
    """
    out = {}
    size = cap * POLL.size
    for index in range(len(blob) // size):
        kind, ring = ("rx", index) if index < 4 else ("tx", index - 4)
        rows = [row for row in POLL.iter_unpack(blob[index * size:(index + 1) * size])
                if row[0] and window["start"] <= row[0] <= window["stop"]]
        if not rows:
            continue
        data = numpy.array(rows, dtype=numpy.float64)
        col = {name: data[:, i] for i, name in enumerate(POLL_FIELDS)}
        dur = col["dur"]
        if kind == "rx":
            x = numpy.column_stack([numpy.ones(len(rows)), col["n"], col["fwd"], col["loc"], col["ack"],
                                    col["n"] - col["frames"], col["xmit_loc"], col["xmit_fwd"], col["scan"],
                                    numpy.minimum(col["fwd"], fwd_flows), numpy.minimum(col["loc"], loc_flows), col["refill"]])
        else:
            x = numpy.column_stack([numpy.ones(len(rows)), col["n"], col["woken"]])
        by_n = {}
        for low, high in ((0, 0), (1, 1), (2, 2), (3, 4), (5, 8), (9, 16), (17, 32), (33, 63), (64, 64), (65, 100000)):
            pick = (col["n"] >= low) & (col["n"] <= high)
            if pick.any():
                by_n[f"{low}-{high}"] = [int(pick.sum()), float(dur[pick].mean()), float(col["n"][pick].mean())]
        from_irq = col["since_irq"] != NO_IRQ
        flags = data[:, POLL_FIELDS.index("flags")].astype(int)
        # 式 1：这些轮里环上进来多少、出去多少。进来的 = 生产位置前后两次读数的差（照环长取余）加起来；出去的 = 各轮做的格数。
        # 记录是绕着存的，窗里的轮数比存得下的多时只是其中一段，这时进出只对这一段成立。
        order = numpy.argsort(col["start"])
        mod = (sizes or {}).get(f"{kind}{ring}", 65536)
        p_seq = numpy.column_stack([col["p_in"][order], col["p_out"][order]]).ravel()
        arrived = float((numpy.diff(p_seq) % mod).sum())
        out[f"{kind}{ring}"] = {
            "条数": len(rows), "XtX": (x.T @ x).tolist(), "Xty": (x.T @ dur).tolist(), "yty": float(dur @ dur),
            "这些轮里进来的格数（生产位置走了多少）": arrived, "这些轮里出去的格数": float(col["n"].sum()),
            "头一轮进来时积着": float(col["q_in"][order][0]), "最后一轮出去时积着": float(col["q_out"][order][-1]),
            "补的页合计": float(col["refill"].sum()), "收发完以后软中断里发的本机包合计": float(col["after_loc"].sum()),
            "在硬中断里做的轮": int((flags & 16).astype(bool).sum()),
            "列": RX_COLUMNS if kind == "rx" else TX_COLUMNS,
            "照格数分档（几轮、平均用时纳秒、平均格数）": by_n,
            "用时纳秒": quantiles(dur), "格数": quantiles(col["n"]), "进来时环上积着": quantiles(col["q_in"]),
            "出去时环上积着": quantiles(col["q_out"]),
            "中断到开始纳秒": quantiles(col["since_irq"][from_irq]),
            "没有中断的轮和上一轮隔多久纳秒": quantiles(col["gap"][~from_irq & (col["gap"] != NO_IRQ)]),
            "在哪些核心上跑过": sorted({int(v) for v in col["cpu"]}), "线程的进程号": sorted({int(v) for v in col["pid"]}),
            "在软中断里跑的轮": int((flags & 1).astype(bool).sum()), "在ksoftirqd里跑的轮": int((flags & 2).astype(bool).sum()),
            "用完份额的轮": int((flags & 4).astype(bool).sum()),
            "轮里驱动发包用的纳秒合计": float(col["xmit_ns"].sum())}
    return out


def reduce_samples(blob, sampler, names, rings, queues, window=None):
    """定时采样器的记录压成每个寄存器的几个数。rings：{环的名字: 多少格}；queues：发送队列的名字，照模块里的次序。

    window 给了（路由器的钟的开始、结束），只用落在这一段里的采样：采样器比开窗早起一点、晚停一点，
    这样"窗里涨了多少"的两头都是窗的两头，误差不超过一个采样间隔。
    """
    words, count = sampler["words"], sampler["n"]
    if not count:
        return {}
    data = numpy.frombuffer(blob[:count * words * 4], dtype="<u4").reshape(count, words).astype(numpy.int64)
    at = data[:, 0] | (data[:, 1] << 32)
    if window:
        data = data[(at >= window[0]) & (at <= window[1])]
        at = data[:, 0] | (data[:, 1] << 32)
    count = len(data)
    if count < 2:
        return {"次数": count}
    reg = {name: data[:, 3 + i] for i, name in enumerate(names)}
    out = {"次数": count, "隔多久纳秒": quantiles(numpy.diff(at)), "读一遍纳秒": quantiles(data[:, 2]),
           "头一次和最后一次的时刻": [int(at[0]), int(at[-1])]}
    used = numpy.column_stack([reg[f"占着{i}"] for i in range(15)])
    out["缓冲各口占着"] = {"最多": used.max(axis=0).tolist(), "平均": [round(float(v), 2) for v in used.mean(axis=0)],
                     "到100以上的次数": (used >= 100).sum(axis=0).tolist()}
    # 状态字（高通的库里叫 PORT_FC_STATUS）：第 0 到 10 位是这一刻放开的门限（PORT_XON_TH），第 16、17 位是流控的状态。
    word = numpy.column_stack([reg[f"状态字{i}"] for i in range(15)])
    out["放开的门限"] = {"最小": (word & 0x7ff).min(axis=0).tolist(), "最大": (word & 0x7ff).max(axis=0).tolist()}
    state = (word >> 16) & 3
    out["流控的状态"] = {"出现过的值": [sorted({int(v) for v in state[:, i]}) for i in range(15)],
                    "不是0的采样占百分之几": [round(100 * float((state[:, i] != 0).mean()), 2) for i in range(15)]}
    out["共用组用掉"] = quantiles(reg["共用组用掉"] & 0x7ff, (0, 50, 99, 100))
    out["叫停以后占着最多"] = {i: int((reg[f"叫停以后占着{i}"] & 0x1ff).max()) for i in (4, 5, 10, 11, 12)}
    out["主机四条队列积着"] = [quantiles(reg[f"主机队列{q}积着"] & 0xfff, (50, 99, 100)) for q in range(4)]
    out["缓冲管理丢的涨了"] = {name: int((reg[name][-1] - reg[name][0]) & 0xffffffff)
                       for name in ("缓冲超载丢11", "缓冲超载丢12", "缓冲流控丢11", "缓冲流控丢12")}
    # 式 1、式 2：每个环这一段里进来多少（生产位置走了多少）、出去多少（消费位置走了多少）、积着多少。
    groups = [(f"收包环{i}", rings.get(f"rx{i}", 0)) for i in range(4)] + [(f"空缓冲区环{i}", rings.get(f"rx{i}", 0)) for i in range(4)]
    groups += [(f"发完环{i}", rings.get(f"tx{i}", 0)) for i in range(4)]
    groups += [(f"{name}发包环{q}", rings.get("txdesc", 1024)) for name, ids in TX_RING_IDS.items() for q in range(len(ids))]
    moves = {}
    for name, size in groups:
        if not size:
            continue
        held = (reg[f"{name}生产"] - reg[f"{name}消费"]) % size
        moves[name] = {"进来": int((numpy.diff(reg[f"{name}生产"]) % size).sum()), "出去": int((numpy.diff(reg[f"{name}消费"]) % size).sum()),
                       "开头积着": int(held[0]), "末尾积着": int(held[-1]), "积着": quantiles(held, (50, 99, 100))}
    out["各环这一段里进出的格数"] = moves
    pause = {}
    for name in PAUSE_REGS:
        steps = numpy.diff(reg[name]) & 0xffffffff
        pause[name] = {"涨了": int(steps.sum()), "涨过的采样间隔占百分之几": round(100 * float((steps > 0).mean()), 2)}
    out["暂停帧"] = pause
    out["MAC收发的单播帧涨了"] = {name: int((reg[name][-1] - reg[name][0]) & 0xffffffff) for name in MAC_REGS}
    soft = data[:, 3 + len(names):]
    out["发送队列"] = {}
    for index, queue in enumerate(queues):
        if 4 * index + 4 > soft.shape[1]:
            break
        part = soft[:, 4 * index:4 * index + 4]
        if part.any():
            out["发送队列"][queue] = {word: quantiles(part[:, k], (0, 50, 99, 100)) for k, word in enumerate(TXQ_WORDS)}
    return out


def send_marks(base, count=6):
    """主机从 USB 网卡那一路往路由器发几个带号的 UDP 包，记下每一个发出去的时刻（主机的单调时钟，纳秒）。

    模块收到时记下路由器的单调时钟；两头的差的最小值就是两个钟差多少，最大减最小是这样对钟的误差。
    USB 网卡这时不在主机这边（放进命名空间了）就改走 eno2。
    """
    for source, nic, target in ((e4.USB_ADDRESS, e4.USB, e4.ROUTER_USB_SIDE), (e4.ENO2_ADDRESS, "eno2", e4.ROUTER_LAN)):
        if os.path.exists(f"/sys/class/net/{nic}"):
            break
    else:
        return {}
    sent = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((source, 0))
        for index in range(count):
            sent[base + index] = time.monotonic_ns()
            sock.sendto(struct.pack(">I", base + index) + bytes(60), (target, MARK_PORT))
            time.sleep(0.02)
    return sent


def clock_offset(sent, seen):
    """路由器的单调时钟减主机的单调时钟：{差（纳秒）, 误差（纳秒）, 对上的记号个数}。"""
    gaps = [seen[seq] - at for seq, at in sent.items() if seq in seen]
    if not gaps:
        return {"对上的记号个数": 0}
    return {"路由器的钟减主机的钟纳秒": min(gaps), "这样对钟的误差纳秒": max(gaps) - min(gaps), "对上的记号个数": len(gaps)}


NETNS_LOOP = ("import time\n"
              "s = '/sys/class/net/{nic}/statistics/'\n"
              "while True:\n"
              "    print(time.monotonic_ns(), open(s + 'tx_packets').read().strip(), open(s + 'rx_packets').read().strip(), flush=True)\n"
              "    time.sleep(0.01)\n")


class HostSeries(threading.Thread):
    """每 10 毫秒读一次主机三块网卡发、收的帧数，带主机的单调时钟。放进网络命名空间的那块网卡由命名空间里的一个小进程读。

    读到的序列存盘（文件夹/主机网卡的序列-网卡名.npy），以后要重算用它。
    """

    NICS = ("eno1", "eno2", e4.USB)

    def __init__(self, folder, netns_nic=None):
        super().__init__(daemon=True)
        self.folder, self.netns_nic, self.rows, self.stop_flag, self.helper = folder, netns_nic, {}, threading.Event(), None
        self.here = [nic for nic in self.NICS if nic != netns_nic and os.path.exists(f"/sys/class/net/{nic}")]
        for nic in self.here:
            self.rows[nic] = []
        if netns_nic:
            self.helper_file = open(os.path.join(folder, f"命名空间里读的-{netns_nic}.txt"), "x")
            self.helper = subprocess.Popen(in_ns([sys.executable, "-c", NETNS_LOOP.format(nic=netns_nic)], e4.NETNS),
                                           stdout=self.helper_file, stderr=subprocess.DEVNULL)

    def run(self):
        while not self.stop_flag.is_set():
            now = time.monotonic_ns()
            for nic in self.here:
                self.rows[nic].append((now, e3.host_stat(nic, "tx_packets"), e3.host_stat(nic, "rx_packets")))
            time.sleep(0.01)

    def finish(self):
        """停下来，把序列存盘，返回 {网卡: 数组（时刻、发的帧数、收的帧数）}。"""
        self.stop_flag.set()
        self.join()
        series = {nic: numpy.array(rows, dtype=numpy.float64) for nic, rows in self.rows.items() if rows}
        if self.helper:
            lab.stop_process(self.helper, 15, 5)
            self.helper_file.close()
            with open(self.helper_file.name) as file:
                rows = [line.split() for line in file if len(line.split()) == 3]
            if rows:
                series[self.netns_nic] = numpy.array(rows, dtype=numpy.float64)
        for nic, rows in series.items():
            numpy.save(os.path.join(self.folder, f"主机网卡的序列-{nic}.npy"), rows)
        return series


def host_counts(series, start_ns, stop_ns):
    """主机的钟的一段时间里每块网卡发、收了多少帧：把每 10 毫秒读的累计数照时刻插到这一段的两头再相减。"""
    out = {}
    for nic, rows in series.items():
        if len(rows) < 3 or rows[0, 0] > start_ns or rows[-1, 0] < stop_ns:
            continue
        # 有的网卡的计数不是随发随涨，是隔一段才更新一次（eno1 量到一秒上下才变一次）：把它多久变一次量出来，
        # 这一段两头各可能差一次更新的量，折成帧数一起报。
        changed = rows[1:][numpy.diff(rows[:, 1]) != 0]
        step = float(numpy.median(numpy.diff(changed[:, 0]))) if len(changed) > 2 else float(stop_ns - start_ns)
        sent = float(numpy.interp(stop_ns, rows[:, 0], rows[:, 1]) - numpy.interp(start_ns, rows[:, 0], rows[:, 1]))
        out[nic] = {"发": round(sent),
                    "收": round(float(numpy.interp(stop_ns, rows[:, 0], rows[:, 2]) - numpy.interp(start_ns, rows[:, 0], rows[:, 2]))),
                    "发的计数多久变一次（毫秒）": round(step / 1e6, 1),
                    "发的帧数因为计数更新得慢可能差多少": round(sent * step / max(1.0, float(stop_ns - start_ns)))}
    return out


def host_pause(netns_nic):
    """主机三块网卡的统计里名字带 pause、xon、xoff、flow_control 的几行：{网卡: {名字: 数}}。只读；这个命令的输出里没有地址。"""
    out = {}
    for nic in HostSeries.NICS:
        done = subprocess.run(in_ns(["ethtool", "-S", nic], e4.NETNS if nic == netns_nic else None), capture_output=True, text=True)
        rows = {}
        for line in done.stdout.split("\n"):
            name, _, value = line.strip().partition(":")
            if re.search(r"pause|xon|xoff|flow_control", name) and value.strip().isdigit():
                rows[name] = int(value)
        out[nic] = rows
    return out


def in_ns(argv, netns):
    return ["sudo", "-n", "ip", "netns", "exec", netns] + argv if netns else argv


# 主机上用内核自带的 pktgen 发定速的 UDP 帧。对面网卡的地址在这条 shell 命令里面取、里面用，不显示。
PKTGEN_HOST = r'''set -e
modprobe pktgen
PG=/proc/net/pktgen
echo rem_device_all > $PG/kpktgend_{cpu}
echo "add_device {nic}" > $PG/kpktgend_{cpu}
ping -n -q -c 1 -W 1 -I {source} {gateway} >/dev/null 2>&1 || true
mac=$(ip neigh show {gateway} dev {l3dev} | sed -n 's/.*lladdr \([0-9a-f:]*\).*/\1/p' | head -n 1)
[ -n "$mac" ]
for line in "count {count}" "clone_skb 0" "pkt_size {size}" "delay 0" "ratep {pps}" "dst {dst}" "src_min {source}" \
    "src_max {source}" "dst_mac $mac" "udp_dst_min {dport}" "udp_dst_max {dport}" "udp_src_min 9000" \
    "udp_src_max {sport_max}" "flag UDPSRC_RND"; do echo "$line" > $PG/{nic}; done
(sleep {limit}; echo stop > $PG/pgctrl) &
guard=$!
echo start > $PG/pgctrl
kill $guard 2>/dev/null || true
sed -n '/Result:/,$p' $PG/{nic} | sed 's/[0-9a-f][0-9a-f]:[0-9a-f:]\{{14\}}/（地址）/g'
'''
# 路由器的 shell 一行只收五百来个字，长的命令先定义成小函数（每一条定义都短），用的时候只发函数名和参数。
# pc：三个口的收发数。bl：wan、lan2 每条发送队列的字节上限。
# pgm：对面的地址（只在别的函数里面用，不显示）。pgw：往 pktgen 的一个设备文件里写几行。
# pg 口 线程号 对面的地址 三层的设备 自己的地址 个数 帧长 每秒几帧 发送队列：起 pktgen。pgr 口 线程号：等它做完、读结果。
E7_HELPERS = (
    'pc() { for p in wan lan2 lan3; do s=/sys/class/net/$p/statistics; echo "$p $(cat $s/rx_packets) $(cat $s/tx_packets) '
    '$(cat $s/rx_bytes) $(cat $s/tx_bytes) $(cat $s/rx_dropped) $(cat $s/tx_dropped)"; done; }',
    'bl() { for f in /sys/class/net/wan/queues/tx-*/byte_queue_limits/limit /sys/class/net/lan2/queues/tx-*/byte_queue_limits/limit; '
    'do echo "bql $f $(cat $f)"; done; }',
    'pgm() { ping -c 1 -W 1 $1 >/dev/null 2>&1; ip neigh show $1 dev $2 | sed -n "s/.*lladdr \\([0-9a-f:]*\\).*/\\1/p" | head -n 1; }',
    'pgw() { d=$1; shift; for l in "$@"; do echo "$l" > /proc/net/pktgen/$d; done; }',
    'pg() { P=/proc/net/pktgen; echo rem_device_all > $P/kpktgend_$2; echo "add_device $1" > $P/kpktgend_$2; m=$(pgm $3 $4); '
    '[ -n "$m" ] || return 1; pgw $1 "dst_mac $m" "dst $3" "src_min $5" "src_max $5" "count $6" "pkt_size $7" "ratep $8" '
    '"queue_map_min $9" "queue_map_max $9" "clone_skb 0"; (echo start > $P/pgctrl &) ; }',
    "pgr() { while grep -q Running /proc/net/pktgen/kpktgend_$2 2>/dev/null && ! grep -q 'Result: OK' /proc/net/pktgen/$1; "
    "do sleep 1; done; sed -n '/Result:/,$p' /proc/net/pktgen/$1 | sed 's/[0-9a-f][0-9a-f]:[0-9a-f:]\\{14\\}/x/g'; }",
)

# 一路：主机这一头的网卡、发包时要绑的那一层的设备、主机的地址、路由器的地址、路由器的口。
PATH = {"wan": ("eno1", "br-lan", e4.BRIDGE_ADDRESS, e4.ROUTER_BRIDGE_SIDE, "wan"),
        "lan2": ("eno2", "eno2", e4.ENO2_ADDRESS, e4.ROUTER_LAN, "br-lan"),
        "lan3": (e4.USB, e4.USB, e4.USB_ADDRESS, e4.ROUTER_USB_SIDE, "br-lan")}


def flow_start(port, prompt, folder, tag, flow, seconds, netns):
    """起一条负载，不等它做完。返回一个句柄，交给 flow_stop。flow 的写法在 PLANS 上面的说明里。"""
    kind = flow["kind"]
    if kind == "pktgen":
        nic, l3dev, source, gateway, _ = PATH[flow["to"]]
        script = PKTGEN_HOST.format(cpu=flow.get("cpu", 2), nic=nic, l3dev=l3dev, source=source, gateway=gateway,
                                    dst=flow.get("dst", gateway), count=int(flow["pps"] * seconds), size=flow["size"],
                                    pps=flow["pps"], dport=flow.get("dport", DROP_PORT), sport_max=9000 + flow.get("flows", 256) - 1,
                                    limit=int(seconds + 20))
        return subprocess.Popen(["sudo", "-n", "sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if kind == "iperf":
        # 写法：path 是 wan、lan2、lan3（主机和路由器之间）或者 forward（穿过路由器，走法由这张表的 topo 定）。
        # dir：to-router 是主机发、路由器收；from-router 是路由器发；forward 的方向由 topo 定。
        if flow["path"] == "forward":
            _, _, _, server_inside, server_address, client_address, server_sends, _, _, _ = e4.FORWARD_KINDS[flow["topo"]]
            # 主机上另有一个常驻的 iperf3 服务端占着 5201，穿过路由器的这一组用 5211。
            flow = dict(flow, port=flow.get("port", 5211))
            server = subprocess.Popen(in_ns(["timeout", str(int(seconds + 60)), "iperf3", "-s", "-1", "-B", server_address, "-p", str(flow["port"])],
                                            e4.NETNS if server_inside else None),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            target, source, reverse = server_address, client_address, server_sends
            client_netns = None if server_inside else e4.NETNS
        else:
            _, _, source, target, _ = PATH[flow["path"]]
            server, reverse, client_netns = None, flow["dir"] == "from-router", netns.get(flow["path"])
        argv = ["timeout", str(int(seconds + 45)), "iperf3", "-c", target, "-B", source, "-p", str(flow.get("port", 5201)), "-P", str(flow.get("flows", 1)),
                "-t", str(int(seconds)), "-i", "1", "-J"]
        if flow.get("proto", "tcp") == "udp":
            argv += ["-u"]
        if flow.get("rate", "0") != "0" or flow.get("proto") == "udp":
            argv += ["-b", str(flow.get("rate", "0"))]
        if flow.get("len"):
            argv += ["-l", str(flow["len"])]
        if reverse:
            argv += ["-R"]
        client = subprocess.Popen(in_ns(argv, client_netns), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return client, server
    if kind == "rpktgen":
        _, _, peer, source, l3dev = PATH[flow["to"]]
        command = (f"pg {flow['to']} {flow.get('cpu', 0)} {peer} {l3dev} {source} {int(min(flow['pps'], 300000) * seconds)} {flow['size']} "
                   f"{flow['pps']} {flow.get('queue', flow.get('cpu', 0))}")
        ask(port, prompt, folder, f"{tag}-路由器上起pktgen-{flow['to']}-{flow.get('cpu', 0)}", command, 30)
        return flow["to"]
    if kind == "pause":
        return e6.pauser(flow["nic"], flow.get("seconds", seconds), flow.get("interval", 0.01))
    raise ValueError(f"不认得的负载：{kind}")


def flow_stop(port, prompt, folder, tag, flow, handle, seconds):
    """等一条负载做完（它自己到时间停），返回它自己报的数。"""
    kind = flow["kind"]
    if kind == "pktgen":
        out, error = handle.communicate(timeout=seconds + 60)
        found = re.search(r"(\d+)pps (\d+)Mb/sec", out)
        sent = re.search(r"OK: \d+\(c\d+\+d\d+\) usec, (\d+) \(", out)
        return {"做完": handle.returncode == 0, "实际每秒帧": int(found.group(1)) if found else None,
                "一共发了": int(sent.group(1)) if sent else None, "错误": error[-200:] if handle.returncode else ""}
    if kind == "iperf":
        client, server = handle
        try:
            out, error = client.communicate(timeout=seconds + 90)
        finally:
            # 边界：服务端不管客户端做没做完都要收掉。
            if server:
                lab.stop_process(server, 2, 5)
        report = e4.iperf_report(out, error)
        try:
            end = json.loads(out).get("end", {})
            sums = end.get("sum", {})
            report["每秒的速率"] = [round(item["sum"]["bits_per_second"]) for item in json.loads(out).get("intervals", [])]
            report["UDP一共发的包"] = sums.get("packets")
        except ValueError:
            pass
        del report["每10秒的速率"]
        return report
    if kind == "rpktgen":
        text = ask(port, prompt, folder, f"{tag}-路由器上pktgen的结果-{handle}", f"pgr {handle} {flow.get('cpu', 0)}", seconds + 60)
        found = re.search(r"(\d+)pps (\d+)Mb/sec", text)
        return {"做完": "OK" in text, "实际每秒帧": int(found.group(1)) if found else None}
    if kind == "pause":
        out, _ = handle.communicate(timeout=seconds + 60)
        return {"发了几帧暂停帧": out.strip()}
    raise ValueError(kind)


ROUTER_COUNTS = "pc; ns; bl"
COUNT_NAMES = ("收到的帧", "发出去的帧", "收到的字节", "发出去的字节", "收的丢了", "发的丢了")


def router_counts(port, prompt, folder, name):
    """路由器三个口的收发数、路由器自己的 TCP 计数、wan 和 lan2 每条发送队列的字节上限。路由器闲着的时候读。"""
    text = ask(port, prompt, folder, name, ROUTER_COUNTS, 60)
    ports, limits, tcp_lines = {}, {}, []
    for line in text.split("\n"):
        w = line.split()
        if w and w[0] in ("wan", "lan2", "lan3"):
            ports[w[0]] = dict(zip(COUNT_NAMES, (int(v) for v in w[1:])))
        elif w and w[0] == "bql":
            limits["/".join(w[1].split("/")[4:7:2])] = int(w[2])
        elif line.strip():
            tcp_lines.append(line)
    freq = {}
    for line in ask(port, prompt, folder, name + "-频率", "cat /sys/devices/system/cpu/cpufreq/policy0/stats/time_in_state", 20).split("\n"):
        w = line.split()
        if len(w) == 2 and w[0].isdigit() and w[1].isdigit():
            freq[w[0]] = int(w[1])
    return {"口": ports, "发送队列的字节上限": limits, "tcp": e4.tcp_table("\n".join(tcp_lines)), "频率": freq}


def counts_rise(before, after):
    return {"处理器各个频率（千赫）各待了多少个百分之一秒": {name: after["频率"].get(name, 0) - value for name, value in before["频率"].items()},
            "口": {name: {key: after["口"][name][key] - value for key, value in row.items()} for name, row in before["口"].items()},
            "路由器自己的TCP计数涨的": e4.tcp_rise(before["tcp"], after["tcp"]),
            "发送队列的字节上限（以前、以后）": {name: [value, after["发送队列的字节上限"].get(name)]
                                   for name, value in before["发送队列的字节上限"].items()}}


def marks_round(port, prompt, folder, name, base):
    """对钟：路由器闲着的时候叫模块记 0.4 秒（它记着的时候才认带号的包），主机发 6 个带号的包。返回主机这一头每个号的时刻。"""
    ctl(port, prompt, folder, name, "run 400")
    sent = send_marks(base)
    time.sleep(0.5)
    return sent


def record(port, prompt, folder, tag, seconds, spec):
    """记一段：起定时采样（比要的多记 1 秒，开窗前先起），开窗（spec 里 diag 是 False 就不开窗），等这一段过去。"""
    sample = spec.get("sample", {"us": 1000, "cpu": 3})
    watch = spec.get("watch")
    if sample:
        ctl(port, prompt, folder, f"{tag}开采样", f"sample {sample['us']} {sample['cpu']} {int((seconds + 1) * 1e6 / sample['us'])}")
    if watch:
        ctl(port, prompt, folder, f"{tag}快读器读哪些", "wregs " + " ".join(f"{reg_address(name):x}" for name in watch["regs"]))
    if spec.get("diag", True):
        ctl(port, prompt, folder, f"{tag}开窗", f"run {int(seconds * 1000)}")
    started = time.monotonic()
    if watch:
        time.sleep(watch.get("after", 1))
        ctl(port, prompt, folder, f"{tag}开快读", f"watch {watch['cpu']} {watch['ms']} {watch.get('count', 200000)}")
    opened = time.monotonic_ns()
    time.sleep(max(0, seconds + (0.3 if sample else 0) - (time.monotonic() - started)))
    return [opened, opened + int(seconds * 1e9)]


def collect(folder, tag, spec, names, fwd_flows, loc_flows):
    """把模块这一段记的读回来、压成摘要里留的样子。原始的 summary、每轮的记录、采样、快读都存在文件夹里。"""
    parsed = parse_summary(pull(folder, f"{tag}summary.txt", f"cat {DIAG}/summary").decode())
    diag = spec.get("diag", True)
    out = reduce_summary(parsed) if diag else {"没有开窗": True}
    rings = {name: ring["count"] for name, ring in parsed["环"].items()}
    if diag and spec.get("raw", True):
        blob = pull(folder, f"{tag}polls.bin", "cat " + " ".join(f"{DIAG}/{kind}{i}" for kind in ("rx", "tx") for i in range(4)))
        out["每轮的记录"] = reduce_polls(blob, parsed["config"]["diag_cap"], parsed["window"], fwd_flows, loc_flows, rings)
    if spec.get("sample", {"us": 1000}) and parsed["sampler"]["n"]:
        blob = pull(folder, f"{tag}samples.bin", f"cat {DIAG}/samples")
        bounds = [parsed["window"]["start"], parsed["window"]["stop"]] if diag else None
        out["定时采样"] = reduce_samples(blob, parsed["sampler"], names, rings, list(parsed["发送队列"]), bounds)
        if not diag and out["定时采样"].get("次数", 0) > 1:
            out["窗的开始和结束（路由器的钟，纳秒）"] = out["定时采样"]["头一次和最后一次的时刻"]
    if spec.get("watch"):
        blob = pull(folder, f"{tag}watch.bin", f"cat {DIAG}/watch")
        out["快读"] = reduce_watch(blob, parsed["watcher"], spec["watch"]["regs"])
    return out


def window(port, prompt, args, tag, spec, netns_nic):
    """做一窗。spec 的写法在 PLANS 上面的说明里。返回这一窗的读数。

    一窗可以分几段（spec 里的 phases）：负载从头到尾不停，每一段各叫模块记一次，各段从负载开始算的起点是定死的。
    """
    folder = os.path.join(args.output, tag)
    os.makedirs(folder)
    warm, gap = spec.get("warm", 3), spec.get("gap", 20)
    phases = spec.get("phases") or [{"seconds": spec.get("seconds", 8)}]
    step = max(phase["seconds"] for phase in phases) + gap
    total = warm + step * (len(phases) - 1) + phases[-1]["seconds"] + 5
    drop_first = all(flow["kind"] == "pktgen" for flow in spec["flows"])
    for key, value in spec.get("params", {}).items():
        if key != "exp_rx_drop_port" or drop_first:
            set_param(port, prompt, folder, f"写{key}", key, value)
    for index, command in enumerate(spec.get("ctl", [])):
        ctl(port, prompt, folder, f"ctl-{index}", command)
    names = [name for name, _ in standard_regs()]
    set_sampler_regs(port, prompt, folder)
    fwd_flows = sum(flow.get("flows", 1) for flow in spec["flows"] if flow.get("path") == "forward") or 1
    loc_flows = sum(flow.get("flows", 1) for flow in spec["flows"] if flow.get("dir") == "to-router") or 1
    before, pause_before = router_counts(port, prompt, folder, "以前"), host_pause(netns_nic)
    base = int(time.time()) % 100000 * 100
    marks = marks_round(port, prompt, folder, "对钟-以前", base)
    series = HostSeries(folder, netns_nic)
    series.start()
    began = time.monotonic()
    handles = [flow_start(port, prompt, folder, tag, flow, total, {}) for flow in spec["flows"]]
    out = {"这一窗": tag, "负载": spec["flows"], "写的参数": spec.get("params", {}), "ctl": spec.get("ctl", []), "每一段": []}
    readings, late = [], []
    try:
        for index, phase in enumerate(phases):
            wait = began + warm + step * index - time.monotonic()
            if wait < 0 and index:
                late.append(f"第 {index} 段比定的起点晚了 {-wait:.1f} 秒")
            time.sleep(max(0, wait))
            if index == 0 and not drop_first and "exp_rx_drop_port" in spec.get("params", {}):
                set_param(port, prompt, folder, "写exp_rx_drop_port", "exp_rx_drop_port", spec["params"]["exp_rx_drop_port"])
            name = phase.get("name", "")
            extra = [flow_start(port, prompt, folder, f"{tag}-{name}", flow, phase["seconds"] + 1, {}) for flow in phase.get("extra", [])]
            host_span = record(port, prompt, folder, name, phase["seconds"], spec)
            extra_out = [flow_stop(port, prompt, folder, f"{tag}-{name}", flow, handle, phase["seconds"] + 1)
                         for flow, handle in zip(phase.get("extra", []), extra)]
            if index < len(phases) - 1:
                reading = collect(folder, name, spec, names, fwd_flows, loc_flows)
                reading.update({"这一段": name, "这一段另加的负载": phase.get("extra", []), "另加的负载自己报的": extra_out,
                                "发开窗命令以后主机的钟的这一段（纳秒）": host_span})
                readings.append(reading)
            else:
                last = {"这一段": name, "这一段另加的负载": phase.get("extra", []), "另加的负载自己报的": extra_out,
                        "发开窗命令以后主机的钟的这一段（纳秒）": host_span}
    finally:
        # 边界：负载不管怎样都要等它停、收掉；写过的参数要改回 0。
        loads = []
        for flow, handle in zip(spec["flows"], handles):
            try:
                loads.append(flow_stop(port, prompt, folder, tag, flow, handle, total))
            except (subprocess.SubprocessError, RuntimeError) as error:
                loads.append({"做完": False, "错误": f"{type(error).__name__}: {error}"[:200]})
        rows = series.finish()
        for key in spec.get("params", {}):
            set_param(port, prompt, folder, f"改回{key}", key, 0)
        for index, command in enumerate(spec.get("undo", [])):
            ctl(port, prompt, folder, f"ctl-改回-{index}", command)
    time.sleep(1)
    # 最后一段的在负载停了以后读（模块记的留到下一次开窗）；读完再对一次钟。
    reading = collect(folder, last["这一段"], spec, names, fwd_flows, loc_flows)
    reading.update(last)
    readings.append(reading)
    marks.update(marks_round(port, prompt, folder, "对钟-以后", base + 50))
    seen = parse_summary(pull(folder, "对钟.txt", f"grep '^mark' {DIAG}/summary; true").decode())["记号"]
    clock = clock_offset(marks, seen)
    after, pause_after = router_counts(port, prompt, folder, "以后"), host_pause(netns_nic)
    for reading in readings:
        bounds = reading.get("窗的开始和结束（路由器的钟，纳秒）")
        if bounds and "路由器的钟减主机的钟纳秒" in clock:
            offset = clock["路由器的钟减主机的钟纳秒"]
            reading["同一段时间里主机网卡的帧数"] = host_counts(rows, bounds[0] - offset, bounds[1] - offset)
            reading["同一段时间的误差秒"] = round(clock["这样对钟的误差纳秒"] / 1e9 + 0.01, 4)
        elif not bounds:
            # 没有开窗、也没有采样的窗（量观测的开销用）：只有主机这一头的数，照发开窗命令的时刻取这一段。
            span = reading["发开窗命令以后主机的钟的这一段（纳秒）"]
            reading["主机的钟的这一段里主机网卡的帧数"] = host_counts(rows, span[0], span[1])
    out["每一段"] = readings
    out.update({key: value for key, value in readings[0].items() if key not in ("这一段", "这一段另加的负载", "另加的负载自己报的")}
               if len(readings) == 1 else {})
    out["负载自己报的（整段负载的，不是窗里的）"] = loads
    out["整段负载前后（不是窗里的）"] = counts_rise(before, after)
    out["整段负载前后主机网卡的暂停帧计数涨的"] = {
        nic: {name: pause_after[nic].get(name, 0) - value for name, value in row.items() if pause_after[nic].get(name, 0) != value}
        for nic, row in pause_before.items()}
    out["对钟"] = clock
    out["读数里不对劲的地方"] = late + [f"第 {i} 条负载没有做完：{load.get('错误', '')}" for i, load in enumerate(loads) if load.get("做完") is False]
    if clock.get("对上的记号个数", 0) < 6:
        out["读数里不对劲的地方"].append(f"对钟只对上 {clock.get('对上的记号个数', 0)} 个记号")
    for reading in readings:
        share = reading.get("每个核心的时间（百分之几）")
        if share and abs(share[0]["合计"] - 100) > 3:
            out["读数里不对劲的地方"].append(f"{reading['这一段']} 0 号核心各样时间加起来是 {share[0]['合计']}%，不是 100%")
    return out


def reduce_watch(blob, watcher, regs):
    """快读器记下的每一次变化：[(时刻, 第几个寄存器, 值)]。压成：每个寄存器变了几次、每一次变化的时刻和值（最多留 400 次），
    读一圈要多久。"""
    count = watcher["n"]
    data = numpy.frombuffer(blob[:count * 16], dtype="<u8").reshape(count, 2)
    out = {"记了几次变化": count, "读了几圈": watcher["loops"],
           "读一圈纳秒": round(watcher["ms"] * 1e6 / max(1, watcher["loops"]), 1), "寄存器": regs, "变化": {}}
    for index, name in enumerate(regs):
        rows = data[(data[:, 1] >> 32) == index]
        out["变化"][name] = {"次数": int(len(rows)), "时刻和值": [[int(at), int(value & 0xffffffff)] for at, value in rows[:400]]}
    return out


# ---- 每一张表 ----
# 一张表：{"topo": 走法, "mtu": 长帧时两头的 MTU, "windows": [(名字, spec), ...]}
# topo：None 是主机三块网卡都在主机这边；route-up、route-down、bridge 是 E-0004 的三种穿过路由器的走法（一块网卡放进命名空间）。
# spec：{"flows": [负载...], "seconds": 记几秒（默认 8）, "params": {运行时可写的参数: 值}, "ctl": [写进 ctl 的命令],
#        "undo": [做完以后写进 ctl 的命令], "sample": {"us": 隔多少微秒, "cpu": 在哪个核心上读} 或者 None,
#        "watch": {"regs": [名字...], "cpu":, "ms":}, "raw": 读不读每轮的记录}
# 负载：{"kind": "pktgen", "to": 路, "size": 帧长, "pps": 每秒几帧, "flows": 几条流, "dport": 目的端口}  主机发定速 UDP
#       {"kind": "iperf", "path": 路 或 "forward", "dir": "to-router"/"from-router", "proto": "tcp"/"udp", "flows":, "rate": 每条流限到多少, "len":}
#       {"kind": "rpktgen", "to": 路, "size":, "pps":, "cpu": 哪个核心的 pktgen 线程, "queue": 哪条发送队列}  路由器自己造包
#       {"kind": "pause", "nic": 主机的网卡, "interval": 每多少秒一帧}  主机发暂停帧

def drop_windows(to, sizes, rates, flows):
    rows = []
    for size in sizes:
        for pps in rates[size]:
            for count in flows:
                rows.append((f"{to}-{size}字节-{pps}帧每秒-{count}条流",
                             {"flows": [{"kind": "pktgen", "to": to, "size": size, "pps": pps, "flows": count}],
                              "params": {"exp_rx_drop_port": DROP_PORT}}))
    return rows


K = 1000
PLANS = {
    # 实验 1：只收、收了就丢。两种帧长加长帧，速度一档一档加；256 条流看落到四个环各多少，1 条流压一个环。
    "rxdrop-wan": {"topo": None, "mtu": {"wan": 9000}, "windows": drop_windows(
        "wan", (64, 1518, 9014),
        {64: (50 * K, 200 * K, 500 * K, 1000 * K, 2000 * K, 4000 * K), 1518: (50 * K, 200 * K, 400 * K, 800 * K), 9014: (20 * K, 60 * K, 130 * K)},
        (256, 1))},
    "rxdrop-lan2": {"topo": None, "mtu": {"lan2": 9000}, "windows": drop_windows(
        "lan2", (64, 1518, 9014),
        {64: (50 * K, 200 * K, 500 * K, 1000 * K, 1480 * K), 1518: (20 * K, 50 * K, 81 * K), 9014: (5 * K, 13 * K)}, (256, 1))},
    # 观测本身的开销：同一档负载，采样器开着、关着，每轮记录读、不读，发包计时开着、关着。
    "overhead": {"topo": None, "windows": [
        (name, {"flows": [{"kind": "pktgen", "to": "wan", "size": 64, "pps": 4000 * K, "flows": 256}],
                "params": dict({"exp_rx_drop_port": DROP_PORT}, **extra), "sample": sample})
        for name, extra, sample in (("采样器每毫秒", {}, {"us": 1000, "cpu": 3}), ("采样器关着", {}, None),
                                    ("采样器每100微秒", {}, {"us": 100, "cpu": 3}), ("发包计时开着", {"diag_xmit_time": 1}, None))] + [
        # 整个记录关着（模块不记每一轮、不记每个核心的时间）：一窗留着采样器好读 MAC 收的帧数，一窗连采样器也关着。
        (name, {"flows": [{"kind": "pktgen", "to": "wan", "size": 64, "pps": 4000 * K, "flows": 256}],
                "params": {"exp_rx_drop_port": DROP_PORT}, "sample": sample, "diag": False})
        for name, sample in (("记录关着-采样器每毫秒", {"us": 1000, "cpu": 3}), ("记录和采样器都关着", None))] + [
        (name, {"flows": [{"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}],
                "sample": sample, "diag": diag})
        for name, sample, diag in (("往wan发-记录开着", {"us": 1000, "cpu": 3}, True), ("往wan发-记录关着", {"us": 1000, "cpu": 3}, False))]},
    # 实验 1 的后半、实验 2：让驱动慢下来（每轮先等若干微秒），把缓冲压到门限，快读器盯着"占着多少"、状态字、暂停帧的计数。
    # 再分别少报新缓冲、少报消费位置，看"占着多少"跟不跟着涨（一份缓冲什么时候还回去）。
    "thresh-wan": {"topo": None, "windows": [
        (name, {"flows": [{"kind": "pktgen", "to": "wan", "size": 1518, "pps": pps, "flows": 1}], "seconds": 6, "raw": False,
                "params": dict({"exp_rx_drop_port": DROP_PORT}, **extra),
                "watch": {"regs": ["占着12", "状态字12", "wan发", "收包环0生产", "收包环1生产", "收包环2生产", "收包环3生产", "占着5"],
                          "cpu": 3, "ms": 1500}})
        for name, pps, extra in (("慢到每轮等500微秒-400K", 400 * K, {"exp_rx_delay_us": 500}),
                                 ("慢到每轮等2000微秒-200K", 200 * K, {"exp_rx_delay_us": 2000}),
                                 ("少报300个新缓冲", 100 * K, {"exp_fill_lag": 300}),
                                 ("少报300格消费位置", 100 * K, {"exp_cons_lag": 300}),
                                 ("都不少报", 100 * K, {}))]},
    "thresh-lan2": {"topo": None, "windows": [
        (name, {"flows": [{"kind": "pktgen", "to": "lan2", "size": 1518, "pps": 81 * K, "flows": 1}], "seconds": 6, "raw": False,
                "params": dict({"exp_rx_drop_port": DROP_PORT}, **extra),
                "watch": {"regs": ["占着11", "状态字11", "lan2发", "收包环0生产", "收包环1生产", "收包环2生产", "收包环3生产", "占着4"],
                          "cpu": 3, "ms": 1500}})
        for name, extra in (("慢到每轮等2000微秒", {"exp_rx_delay_us": 2000}), ("少报300个新缓冲", {"exp_fill_lag": 300}),
                            ("都不少报", {}))]},
    # 实验 3：稳的小流量，中断延时寄存器换几个数：每一轮做几格、两次中断隔多久跟着怎样变。
    "modtimer-rx": {"topo": None, "windows": [
        (f"收包延时{value}", {"flows": [{"kind": "pktgen", "to": "wan", "size": 64, "pps": 20 * K, "flows": 1}],
                          "params": {"exp_rx_drop_port": DROP_PORT}, "ctl": [f"mod rx {value}"], "undo": ["mod rx 1000"], "seconds": 5})
        for value in (0, 50, 250, 500, 1000, 2000, 4000)] + [
        # 很稀的流量（每秒 200 帧）：快读器盯着四个收包环的生产位置，和每轮记录里中断的时刻对，得"帧进环到中断进来隔多久"。
        (f"稀-收包延时{value}", {"flows": [{"kind": "pktgen", "to": "wan", "size": 64, "pps": 200, "flows": 1}],
                            "params": {"exp_rx_drop_port": DROP_PORT}, "ctl": [f"mod rx {value}"], "undo": ["mod rx 1000"], "seconds": 5,
                            "sample": None, "watch": {"regs": ["收包环0生产", "收包环1生产", "收包环2生产", "收包环3生产"],
                                                      "cpu": 3, "ms": 2000}})
        for value in (0, 250, 1000, 4000)]},
    "modtimer-tx": {"topo": None, "windows": [
        (f"发完延时{value}", {"flows": [{"kind": "rpktgen", "to": "wan", "size": 64, "pps": 20 * K, "cpu": 0, "queue": 0}],
                          "ctl": [f"mod tx {value}"], "undo": ["mod tx 150"], "seconds": 5,
                          "watch": {"regs": ["wan发包环0消费", "发完环0生产", "发完环0消费"], "cpu": 3, "ms": 300}})
        for value in (0, 50, 150, 500, 1000, 2000)] + [
        (f"稀-发完延时{value}", {"flows": [{"kind": "rpktgen", "to": "wan", "size": 64, "pps": 200, "cpu": 0, "queue": 0}],
                            "ctl": [f"mod tx {value}"], "undo": ["mod tx 150"], "seconds": 5, "sample": None,
                            "watch": {"regs": ["wan发包环0消费", "发完环0生产", "发完环0消费"], "cpu": 3, "ms": 2000}})
        for value in (0, 150, 1000, 4000)]},
    # 实验 4：收了交给本机，定速 UDP（路由器上 iperf3 的服务端收）。
    "local-wan": {"topo": None, "windows": [
        (f"{length}字节-{rate}", {"flows": [{"kind": "iperf", "path": "wan", "dir": "to-router", "proto": "udp", "flows": 4,
                                           "rate": rate, "len": length}]})
        for length, rates in ((18, ("5M", "20M", "50M")), (1472, ("100M", "400M", "1000M")))
        for rate in rates]},
    # 实验 5、7：只发。路由器自己定速造包（pktgen，不经过协议栈）；路由器的 iperf3 发 UDP、发 TCP（主机只回应答）。
    "txonly-pktgen": {"topo": None, "windows": [
        (f"{to}-{size}字节-{pps}帧每秒", {"flows": [{"kind": "rpktgen", "to": to, "size": size, "pps": pps, "cpu": 0, "queue": 0}],
                                      "params": {"diag_xmit_time": 1}})
        for to, size, rates in (("wan", 64, (50 * K, 200 * K, 500 * K, 1000 * K, 2000 * K)), ("wan", 1518, (50 * K, 200 * K, 800 * K)),
                                ("lan2", 64, (200 * K, 1480 * K, 2000 * K)), ("lan2", 1518, (40 * K, 81 * K, 120 * K)))
        for pps in rates]},
    "txonly-iperf": {"topo": None, "windows": [
        (f"{proto}-{flows}条流-每条{rate}", {"flows": [{"kind": "iperf", "path": "wan", "dir": "from-router", "proto": proto,
                                                    "flows": flows, "rate": rate}], "params": {"diag_xmit_time": 1}})
        for proto, flows, rates in (("udp", 4, ("50M", "250M", "1000M")), ("tcp", 64, ("4M", "16M", "32M", "64M", "0")),
                                    ("tcp", 1, ("0",)))
        for rate in rates]},
    # 实验 6：只转发，定速 UDP，三种走法，两种帧长。
    **{f"fwd-{topo}": {"topo": topo, "windows": [
        (f"{length}字节-{flows}条流-每条{rate}", {"flows": [{"kind": "iperf", "path": "forward", "topo": topo, "proto": "udp",
                                                         "flows": flows, "rate": rate, "len": length}], "params": {"diag_xmit_time": 1}})
        for length, flows, rates in ((18, 32, ("0.25M", "1M", "2.5M", "5M")), (1472, 32, ("3M", "12M", "30M")))
        for rate in rates]}
       for topo in ("route-up", "route-down", "bridge")},
    # 实验 8：转发的输入固定（lan2 进 wan 出的 UDP 小包、wan 进 lan2 出的 UDP 小包），路由器自己往 wan 发的 TCP 一档一档加。
    **{f"mix-{topo}": {"topo": topo, "windows": [
        (f"转发每条{fwd}-本机发每条{rate}", {"flows": [{"kind": "iperf", "path": "forward", "topo": topo, "proto": "udp", "flows": 32,
                                                "rate": fwd, "len": 18},
                                             {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64,
                                              "rate": rate, "port": 5202}], "params": {"diag_xmit_time": 1}})
        for fwd in ("0.5M", "1.25M", "2.5M", "5M") for rate in ("1M", "8M", "24M", "48M", "0")]}
       for topo in ("route-up", "route-down")},
    # 实验 10 的一半：E-0006 的 4-4 的负载（转发是 64 条 TCP，路由器同时往 wan 发 64 条 TCP），一窗一窗量。
    **{f"fwdmix-{topo}": {"topo": topo, "windows": [
        (f"第{index}遍", {"flows": [{"kind": "iperf", "path": "forward", "topo": topo, "proto": "tcp", "flows": 64},
                                 {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}],
                        "seconds": 20, "warm": 6})
        for index in (1, 2, 3)] + [
        ("只有转发", {"flows": [{"kind": "iperf", "path": "forward", "topo": topo, "proto": "tcp", "flows": 64}], "seconds": 20, "warm": 6}),
        ("只有路由器往wan发", {"flows": [{"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}],
                        "seconds": 20, "warm": 6})]}
       for topo in ("route-up", "route-down")},
    # 实验 9（也是 E-0006 判据 4-7 头一半的负载）：路由器往 lan2、lan3 各发 64 条 TCP 长帧，wan 两个方向各 64 条 TCP。
    # 一窗一条时间线、三段：叫停前、叫停中、叫停后，各记 15 秒，三段的起点从负载开始算是定死的。
    # "叫停"的窗在中间那一段主机从 eno2、USB 网卡每 10 毫秒发一个暂停帧；"不叫停"的窗时间线一样、不发。两种交替各两窗。
    "jam": {"topo": None, "mtu": {"lan2": e6.LONG_MTU, "lan3": e6.LONG_MTU}, "windows": [
        (name, {"flows": [{"kind": "iperf", "path": "lan2", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5203},
                          {"kind": "iperf", "path": "lan3", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5204},
                          {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5201},
                          {"kind": "iperf", "path": "wan", "dir": "to-router", "proto": "tcp", "flows": 64, "port": 5202}],
                "warm": 8, "gap": 20, "raw": False, "sample": {"us": 5000, "cpu": 3},
                "phases": [{"name": "叫停前", "seconds": 15},
                           {"name": "叫停中", "seconds": 15, "extra": pause},
                           {"name": "叫停后", "seconds": 15}]})
        for name, pause in (("叫停-1", [{"kind": "pause", "nic": "eno2"}, {"kind": "pause", "nic": e4.USB}]), ("对照-1", []),
                            ("叫停-2", [{"kind": "pause", "nic": "eno2"}, {"kind": "pause", "nic": e4.USB}]), ("对照-2", []))]},
    # 每一种可以调的量换了以后量同样的四窗（走法是 wan 进 lan2 出）：只转发小包、只有路由器往 wan 发、两样一起、
    # 转发换成 64 条 TCP 再和路由器往 wan 发一起。换哪一样由 setup 阶段加载时的参数、或者这一遍的 --pre 定。
    "knob": {"topo": "route-down", "windows": [
        (name, {"flows": flows, "params": {"diag_xmit_time": 1}})
        for name, flows in (
            ("只转发小包", [{"kind": "iperf", "path": "forward", "topo": "route-down", "proto": "udp", "flows": 32, "rate": "2.5M", "len": 18}]),
            ("只有路由器往wan发", [{"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}]),
            ("转发小包加路由器往wan发", [{"kind": "iperf", "path": "forward", "topo": "route-down", "proto": "udp", "flows": 32, "rate": "2.5M", "len": 18},
                              {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}]),
            ("转发TCP加路由器往wan发", [{"kind": "iperf", "path": "forward", "topo": "route-down", "proto": "tcp", "flows": 64},
                               {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "port": 5202}]))]},
    # 实验 11：留着不参加拟合的一种配法：wan 进 lan2 出的转发（UDP，512 字节一个包），同时路由器往 wan 发限速的 TCP、
    # 主机往路由器的 wan 发限速的 TCP。
    "holdout": {"topo": "route-down", "windows": [
        (f"第{index}遍", {"flows": [{"kind": "iperf", "path": "forward", "topo": "route-down", "proto": "udp", "flows": 32, "rate": "7.5M", "len": 470},
                                 {"kind": "iperf", "path": "wan", "dir": "from-router", "proto": "tcp", "flows": 64, "rate": "24M", "port": 5202},
                                 {"kind": "iperf", "path": "wan", "dir": "to-router", "proto": "tcp", "flows": 16, "rate": "50M", "port": 5203}],
                        "seconds": 15, "warm": 6, "params": {"diag_xmit_time": 1}})
        for index in (1, 2, 3)]},
}


def set_mtu(port, prompt, folder, tag, mtus, value):
    """长帧的表：把表里点名的口两头的 MTU 改成 value（主机的网卡最多改到它自己许的最大数）。

    wan：主机的 eno1（连着 br-lan）和路由器的 wan。lan2、lan3：路由器的桥和四个口、主机的 eno2 和 USB 网卡一起改（E-0006 的做法）。
    """
    if "wan" in mtus:
        e6.set_host_mtu(folder, f"{tag}-wan", "eno1", min(value, e4.host_max_mtu("eno1")))
        ask(port, prompt, folder, f"{tag}-路由器wan的MTU改成{value}", f"ip link set wan mtu {value}", 30)
    if "lan2" in mtus or "lan3" in mtus:
        e6.bridge_mtu(port, prompt, folder, tag, value)
    time.sleep(3)


def stage_plan(port, prompt, args, result):
    """照一张表一窗一窗做。"""
    plan = PLANS[args.plan]
    topo, folder = plan.get("topo"), args.output
    rows = [(name, spec) for name, spec in plan["windows"] if not args.only or args.only in name]
    result.update({"表": args.plan, "走法": topo, "每一窗": [], "判据": [], "读数里不对劲的地方": []})
    result["这一遍先在路由器上执行的命令"] = args.pre
    if args.pre:
        ask(port, prompt, folder, "开头-这一遍先执行的命令", args.pre, 60)
    result["模块的参数"] = module_params(port, prompt, folder, "开头")
    e4.iperf_servers(port, prompt, folder, "开头", (5201, 5202, 5203, 5204))
    if topo:
        nic, address, route = e4.FORWARD_KINDS[topo][:3]
        e4.nic_into_netns(folder, nic, address, route)
    try:
        if topo:
            _, _, _, server_inside, server_address, client_address = e4.FORWARD_KINDS[topo][:6]
            if not e4.wait_ping(folder, "等线连上", client_address, server_address, None if server_inside else e4.NETNS):
                raise RuntimeError("网卡放进网络命名空间以后 40 秒里 ping 不通对面")
        if plan.get("mtu"):
            set_mtu(port, prompt, folder, "长帧", plan["mtu"], max(plan["mtu"].values()))
        for index, (name, spec) in enumerate(rows):
            try:
                reading = window(port, prompt, args, f"{index:02d}-{name}", spec, nic if topo else None)
            except (RuntimeError, subprocess.SubprocessError, OSError, KeyError, ValueError) as error:
                # 外部边界：一窗里路由器上的命令没有做完、负载起不来，只作废这一窗，已经做完的窗留着。
                result["没有做成的窗"] = result.get("没有做成的窗", []) + [f"{name}：{type(error).__name__}: {error}"[:300]]
                os.write(port.fd, b"\x03\n")
                time.sleep(3)
                lab.read_serial(port, 1.0)
                lab.serial_command(port, "killall iperf3 2>/dev/null; echo stop > /proc/net/pktgen/pgctrl 2>/dev/null; true", prompt, 30)
                e4.iperf_servers(port, prompt, folder, f"第{index}窗出错以后", (5201, 5202, 5203, 5204))
                continue
            result["每一窗"].append(reading)
            result["读数里不对劲的地方"] += [f"{name}：{item}" for item in reading["读数里不对劲的地方"]]
    finally:
        # 边界：MTU 不管怎样都要改回 1500，网卡不管怎样都要放回来，路由器上的 iperf3 和 pktgen 要停掉。
        if plan.get("mtu"):
            set_mtu(port, prompt, folder, "收尾", plan["mtu"], 1500)
        if topo:
            e4.nic_out_of_netns(folder, nic, address if nic == "eno2" else e4.USB_ADDRESS)
        lab.serial_command(port, "killall iperf3 2>/dev/null; echo stop > /proc/net/pktgen/pgctrl 2>/dev/null; true", prompt, 30)
        if args.post:
            lab.serial_command(port, args.post, prompt, 60)
    result["要看的数"] = {"做了几窗": len(result["每一窗"]),
                       "每一窗每一段的窗长和0号核心空闲": [
                           [row["这一窗"], part.get("这一段"), part.get("窗长秒"),
                            part.get("每个核心的时间（百分之几）", {}).get(0, {}).get("线程-idle")]
                           for row in result["每一窗"] for part in row["每一段"]],
                       "对钟": [row["对钟"] for row in result["每一窗"]], "没有做成的窗": result.get("没有做成的窗", [])}


def module_params(port, prompt, folder, tag):
    text = ask(port, prompt, folder, f"{tag}-模块的参数", f"for f in {PARAM}/*; do echo \"${{f##*/}}=$(cat $f 2>/dev/null)\"; done", 20)
    return fields(text.split())


PIN_TX = e6.PIN_TX_THREADS
PIN_IRQS = ("for n in 0 1 2 3; do for k in rxdesc rxfill txcmpl; do "
            "i=$(grep \"edma_$k$n\\$\" /proc/interrupts | cut -d: -f1 | tr -d ' '); "
            "[ -n \"$i\" ] && echo $((1 << n)) > /proc/irq/$i/smp_affinity; done; done; true")


def stage_setup(port, prompt, args, result):
    """换模块（做法和 E-0005 的 setup 一样：经主机上临时的文件服务取到路由器的 /tmp），绑核，读回参数和诊断的配置。"""
    folder = args.output
    if args.file == "normal":
        result["卸载加载以后"] = e3.switch_module(port, prompt, folder, "换回正式的模块", ["modprobe ipq807x-eth"])
        result["换成了"] = "正式的模块"
    else:
        with open(os.path.join(e3.VARIANTS, args.file), "rb") as file:
            wanted = hashlib.md5(file.read()).hexdigest()
        fetch(port, prompt, folder, e3.VARIANTS, args.file, "/tmp/variant.ko", wanted)
        result["卸载加载以后"] = e3.switch_module(port, prompt, folder, "换上实验用的模块",
                                             [f"insmod /tmp/variant.ko {args.insmod}".strip()])
        result.update({"换成了": args.file, "模块的校验值": wanted, "加载时的参数": args.insmod})
    result["重新绑核以后"] = e4.rebind(port, prompt, folder, "换模块以后")
    if "exp_rx_softirq=1" in args.insmod:
        # 收包的轮询不进线程时没有收包的线程，开机脚本等不到线程就不绑中断：这里把第 n 个环的三种中断绑到 n 号核心上。
        ask(port, prompt, folder, "绑中断", PIN_IRQS, 20)
        result["重新绑核以后"]["中断"] = ask(port, prompt, folder, "绑了以后的中断", "iq", 20).split("\n")
    if "exp_tx_thread=1" in args.insmod:
        ask(port, prompt, folder, "绑收发完的线程", PIN_TX, 20)
        result["重新绑核以后"]["线程"] = ask(port, prompt, folder, "绑了以后的线程", "th", 20).split("\n")
    ask(port, prompt, folder, "路由器的桥上的测试地址", f"ip addr replace {e4.ROUTER_USB_SIDE}/24 dev br-lan", 15)
    e4.firewall_open(port, prompt, folder, "换模块以后", "wan")
    result["主机br-lan的测试地址在"] = e3.ensure_bridge_address(folder, "换模块以后")
    result["模块的参数"] = module_params(port, prompt, folder, "读回")
    result["判据"], result["读数里不对劲的地方"] = [], []
    # 网口有没有声明"包可以不是一整块内存"、分段、合并这几样特性：模型里"分片数"那一项要用。
    result["wan的几样特性"] = ask(port, prompt, folder, "wan的特性",
                             "ethtool -k wan | grep -E 'scatter-gather:|segmentation-offload:|generic-receive|checksumming:|fraglist'", 20).split("\n")
    if args.file.startswith("variant-e7"):
        ask(port, prompt, folder, "挂debugfs", "mount | grep -q debugfs || mount -t debugfs none /sys/kernel/debug; true", 15)
        parsed = parse_summary(ask(port, prompt, folder, "诊断的配置", f"cat {DIAG}/summary", 30))
        result["诊断的配置"] = parsed["config"]
        result["线程"] = {name: {key: thread.get(key) for key in ("pid", "cpu", "policy", "nice")} for name, thread in parsed["线程"].items()}
        result["口"] = parsed["口"]
        if parsed["config"].get("poll_size") != POLL.size or not parsed["config"].get("probes"):
            result["读数里不对劲的地方"].append(f"模块的配置和读数脚本对不上：{parsed['config']}")
    result["要看的数"] = {key: result.get(key) for key in ("换成了", "模块的参数", "诊断的配置", "线程", "重新绑核以后", "wan的几样特性")}


def fetch(port, prompt, folder, directory, name, target, wanted):
    """把主机上一个文件经临时的文件服务（绑在 USB 网卡的测试地址上）取到路由器上，核校验值。"""
    ask(port, prompt, folder, f"取{name}-路由器的桥上的测试地址", f"ip addr replace {e4.ROUTER_USB_SIDE}/24 dev br-lan", 15)
    # 主机上别的程序可能占着 8000：从 8000 起找头一个在 USB 网卡的测试地址上绑得上的端口。
    for number in range(8000, 8020):
        probe = socket.socket()
        try:
            probe.bind((e4.USB_ADDRESS, number))
        except OSError:
            continue
        finally:
            probe.close()
        break
    else:
        raise RuntimeError("主机上 8000 到 8019 的端口都被占着，起不了临时的文件服务")
    server = subprocess.Popen([sys.executable, "-m", "http.server", "--bind", e4.USB_ADDRESS, "--directory", directory, str(number)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        ask(port, prompt, folder, f"取{name}", f"uclient-fetch -q -O {target} http://{e4.USB_ADDRESS}:{number}/{name}", 180)
    finally:
        # 边界：文件服务不管取没取成都要停掉。
        lab.stop_process(server, 15, 5)
    if ask(port, prompt, folder, f"取到的{name}的校验值", f"md5sum {target} | cut -c1-32", 30) != wanted:
        raise RuntimeError(f"取到路由器上的 {name} 和主机上的不一样")


def stage_prep(port, prompt, args, result):
    """每一次内存引导以后做一次。"""
    folder, tools = args.output, os.path.join(args.work, "工具")
    for netns, nic, address in ((e5.NETNS_A, "eno2", e4.ENO2_ADDRESS), (e5.NETNS_B, e4.USB, e4.USB_ADDRESS)):
        e5.out_of_netns(folder, netns, nic, address)
    e3.router_addresses(port, prompt, folder, "开头", "wan")
    ask(port, prompt, folder, "路由器的桥上的测试地址", f"ip addr replace {e4.ROUTER_USB_SIDE}/24 dev br-lan", 15)
    e4.firewall_open(port, prompt, folder, "开头", "wan")
    result["主机br-lan的测试地址在"] = e3.ensure_bridge_address(folder, "开头")
    paths = {name: e4.wait_ping(folder, f"等{name}通", source, target) for name, _nic, source, target in e4.PATHS}
    got = {}
    for name in sorted(os.listdir(tools)):
        with open(os.path.join(tools, name), "rb") as file:
            wanted = hashlib.md5(file.read()).hexdigest()
        fetch(port, prompt, folder, tools, name, f"/tmp/{name}", wanted)
        got[name] = wanted
    if "pktgen.ko" in got:
        ask(port, prompt, folder, "加载pktgen", "lsmod | grep -q '^pktgen' || insmod /tmp/pktgen.ko; ls /proc/net/pktgen | head -n 3", 30)
    if "e6tools.tgz" in got:
        ask(port, prompt, folder, "解开工具", "tar -xzf /tmp/e6tools.tgz -C / && rm /tmp/e6tools.tgz", 180)
    ask(port, prompt, folder, "挂debugfs", "mount | grep -q debugfs || mount -t debugfs none /sys/kernel/debug; true", 15)
    watchdog = ask(port, prompt, folder, "看门狗", "for f in state status timeout timeleft nowayout identity; do "
                   'echo "$f=$(cat /sys/class/watchdog/watchdog0/$f 2>/dev/null)"; done; ubus call system watchdog', 30)
    result.update({"三路通了": paths, "传到路由器上的文件": got, "看门狗": watchdog.split("\n"),
                   "内存（kB）": ask(port, prompt, folder, "内存", "grep -E '^(MemFree|MemAvailable):' /proc/meminfo", 15).split("\n"),
                   "ssh走得通": bool(pull(folder, "ssh试读.txt", "echo ok").strip() == b"ok"),
                   "判据": [], "读数里不对劲的地方": [f"{name} 这一路 ping 不通" for name, ok in paths.items() if not ok]})
    if "e6tools.tgz" in got:
        try:
            result["python的版本"] = ask(port, prompt, folder, "python的版本", "python3 -c 'import sys, yaml, uuid; print(sys.version.split()[0])'", 60)
            result["每队列统计试读"] = e6.tx_stops(port, prompt, folder, "试读", "lan2")
        except RuntimeError as error:
            # 外部边界：读每队列统计的工具只有重放 E-0006 的阶段要用，它读不成不拦着别的。
            result["读数里不对劲的地方"].append(f"读每队列统计的工具用不了：{error}")
    result["要看的数"] = {key: result.get(key) for key in ("三路通了", "传到路由器上的文件", "看门狗", "内存（kB）", "ssh走得通", "每队列统计试读")}


def stage_stall(port, prompt, args, result):
    """试看门狗这条路：读看门狗的状态，叫模块让四个核心关着中断空转 --seconds 秒，看串口上有没有出现引导程序的输出。

    空转到时间自己结束：看门狗没有咬，路由器也自己回来。
    """
    folder = args.output
    before = ask(port, prompt, folder, "看门狗-以前", "for f in state status timeout timeleft; do "
                 'echo "$f=$(cat /sys/class/watchdog/watchdog0/$f 2>/dev/null)"; done; ubus call system watchdog', 30)
    os.write(port.fd, f"echo 'stall {args.seconds}' > {DIAG}/ctl\n".encode())
    started, seen, reset_at = time.monotonic(), b"", None
    while time.monotonic() - started < args.seconds + 60:
        seen += lab.read_serial(port, 1.0)
        if reset_at is None and (b"U-Boot" in seen or b"Format: Log Type" in seen or b"Hit any key" in seen):
            reset_at = round(time.monotonic() - started, 1)
            break
    with open(os.path.join(folder, "空转期间串口上的字.txt"), "x") as file:
        file.write(re.sub(r"(?i)([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", "（地址）", seen.decode(errors="replace")))
    result.update({"空转了几秒": args.seconds, "看门狗-以前": before.split("\n"), "路由器自己复位了": reset_at is not None,
                   "从开始空转到串口上出现引导程序的输出隔了几秒": reset_at,
                   "判据": [{"编号": "恢复-看门狗", "看什么": "内核整个卡住时看门狗让路由器自己复位", "通过": reset_at is not None,
                           "隔了几秒": reset_at}],
                   "读数里不对劲的地方": []})
    result["要看的数"] = {"路由器自己复位了": reset_at is not None, "隔了几秒": reset_at, "看门狗-以前": before.split("\n")}


def patient_ask(port, prompt, folder, name, command, timeout=60):
    """重放期间给 E-0003 到 E-0006 的阶段用的：每条命令至少等 90 秒。

    收包的线程调了优先级以后，旁边那一组流量跑着的时候路由器上读计数的命令要 20 到 25 秒，那几份脚本里写的 20 秒不够。
    """
    return ask(port, prompt, folder, name, command, max(timeout, 90))


def stage_replay(port, prompt, args, result):
    """E-0006 的一个阶段原样做一遍。--pre、--post 给了，前后各在路由器上执行一条命令（换一种可以调的量、改回去）。

    fwdmix、jam 同时叫模块记（见 replay_recorded）。unknown（判据 4-11）、pause（4-1）、txrate（4-3，等于 E-0006 的
    run --inner txrate --judge txrate）只照 E-0006 的阶段做，不叫模块记：unknown 用的是 E-0006 的实验用的模块，里面没有诊断目录。
    E-0006 的 jam 收尾时会删掉放行 wan 的临时规则，所以做完一律补回去。
    """
    folder = args.output
    if args.pre:
        ask(port, prompt, folder, "先执行的命令", args.pre, 30)
    e3.ask = e4.ask = e5.ask = e6.ask = patient_ask
    try:
        if args.inner in ("fwdmix", "jam"):
            replay_recorded(port, prompt, args, result)
        elif args.inner == "txrate":
            args.judge = "txrate"
            e6.stage_run(port, prompt, args, result)
        else:
            e6.STAGES[args.inner](port, prompt, args, result)
    finally:
        # 边界：换的东西不管这个阶段做没做成都要改回去，放行 wan 的规则要补回去。
        e3.ask = e4.ask = e5.ask = e6.ask = ask
        if args.post:
            ask(port, prompt, folder, "做完执行的命令", args.post, 30)
        e4.firewall_open(port, prompt, folder, "重放以后", "wan")


def replay_recorded(port, prompt, args, result):
    """fwdmix 在转发的那 30 秒开始时开一窗 28 秒；jam（two）整个阶段开一窗。"""
    folder = args.output
    set_sampler_regs(port, prompt, folder)
    names = [name for name, _ in standard_regs()]
    if args.inner == "fwdmix":
        original = e4.iperf

        def recording_iperf(*positional, **named):
            ctl(port, prompt, folder, "开采样", "sample 1000 3 28000")
            ctl(port, prompt, folder, "开窗", "run 28000")
            return original(*positional, **named)

        e4.iperf = recording_iperf
        try:
            e6.stage_fwdmix(port, prompt, args, result)
        finally:
            # 边界：换掉的函数要换回去。
            e4.iperf = original
    else:
        ctl(port, prompt, folder, "开采样", "sample 5000 3 30000")
        ctl(port, prompt, folder, "开窗", "run 150000")
        opened = time.monotonic()
        args.part = "two"
        e6.stage_jam(port, prompt, args, result)
        # 模块到 150 秒自己停；阶段做完得早，等它停了再读，不然读到的窗没有结束的时刻。
        time.sleep(max(0.0, 152.0 - (time.monotonic() - opened)))
    time.sleep(2)
    result["模块记的"] = collect(folder, "", {"raw": True}, names, 64, 64)


def solve(parts):
    """把几窗的 X'X、X'y 加起来解出系数。返回 {列: [系数, 标准误差]} 和剩下的方差占原来的几成。"""
    xtx = sum(numpy.array(part["XtX"]) for part in parts)
    xty = sum(numpy.array(part["Xty"]) for part in parts)
    yty, count = sum(part["yty"] for part in parts), sum(part["条数"] for part in parts)
    columns = parts[0]["列"]
    keep = [i for i in range(len(columns)) if xtx[i, i] > 0 and (i == 0 or xtx[i, i] * count > xtx[0, i] ** 2 * 1.0001)]
    a, b = xtx[numpy.ix_(keep, keep)], xty[keep]
    beta, _, rank, _ = numpy.linalg.lstsq(a, b, rcond=None)
    residual = max(0.0, yty - 2 * beta @ b + beta @ a @ beta)
    sigma2 = residual / max(1, count - len(keep))
    cov = sigma2 * numpy.linalg.pinv(a)
    mean = xty[0] / count
    spread = yty - count * mean * mean
    return {"系数（纳秒）和标准误差": {columns[i]: [round(float(beta[k]), 1), round(float(cov[k, k] ** 0.5), 2)] for k, i in enumerate(keep)},
            "没有变过、解不出来的列": [columns[i] for i in range(len(columns)) if i not in keep], "矩阵的秩": int(rank),
            "轮数": count, "残差的方差占原来的几成": round(residual / spread, 4) if spread > 0 else None}


def stage_fit(args):
    """不碰路由器。把 工作文件夹/读数/结果-*.json 里的每一窗拢到一起，照表分组解每轮用时的回归。"""
    folder = os.path.join(args.work, "读数")
    windows = []
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("结果-") and name.endswith(".json")):
            continue
        with open(os.path.join(folder, name)) as file:
            data = json.load(file)
        for row in data.get("每一窗", []):
            windows.append(dict(row, 表=data.get("表"), 这一次=data.get("这一次"), 模块的参数=data.get("模块的参数")))
    groups = {}
    for row in windows:
        for ring, part in row.get("每轮的记录", {}).items():
            groups.setdefault((row["这一次"], ring[:2]), []).append(part)
            groups.setdefault(("全部", ring[:2]), []).append(part)
    fits = {f"{label}-{kind}": solve(parts) for (label, kind), parts in sorted(groups.items())}
    result = {"代号": "E-0007", "这一次": args.label, "阶段": "fit", "读完": True, "一共几窗": len(windows), "回归": fits,
              "判据": [], "读数里不对劲的地方": []}
    result["要看的数"] = {"一共几窗": len(windows), "全部-rx": fits.get("全部-rx"), "全部-tx": fits.get("全部-tx")}
    return result


STAGES = {"prep": stage_prep, "setup": stage_setup, "plan": stage_plan, "replay": stage_replay, "stall": stage_stall}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="/dev/ttyUSB0", help="主机上的串口（默认 /dev/ttyUSB0）")
    parser.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES) + ["hosthold", "finish", "fit"], help="做哪一个阶段")
    parser.add_argument("--plan", choices=sorted(PLANS), help="plan 阶段：做哪一张表")
    parser.add_argument("--only", help="plan 阶段：只做名字里带这个字的几窗")
    parser.add_argument("--pre", default="", help="plan、replay 阶段：做以前先在路由器上执行的一条命令（换一种可以调的量）")
    parser.add_argument("--post", default="", help="plan 阶段：做完以后在路由器上执行的一条命令（把 --pre 换的改回去）")
    parser.add_argument("--inner", choices=["fwdmix", "jam", "unknown", "pause", "txrate"], help="replay 阶段：做 E-0006 的哪一个阶段")
    parser.add_argument("--kind", choices=["route-up", "route-down"], help="replay 阶段做 fwdmix 时：哪一种走法")
    parser.add_argument("--seconds", type=int, default=90, help="stall 阶段：空转几秒（默认 90）")
    parser.add_argument("--label", required=True, help="这一次读数叫什么")
    parser.add_argument("--work", required=True, help="上机期间放东西的工作文件夹（里面有 实验用的模块/ 和 工具/）")
    parser.add_argument("--module", help="主机上编出来的正式的 ipq807x-eth.ko，和镜像里带的那一个比")
    parser.add_argument("--file", help="setup 阶段：实验用的模块的文件名，或者 normal")
    parser.add_argument("--insmod", default="", help="setup 阶段：加载实验用的模块时带的参数")
    parser.add_argument("--output", required=True, help="放每一步原始输出的新文件夹")
    parser.add_argument("--summary", required=True, help="结果摘要写到哪个新文件")
    args = parser.parse_args()
    if args.stage == "plan" and not args.plan:
        parser.error("plan 阶段要用 --plan 写明做哪一张表")
    if args.stage == "setup" and not args.file:
        parser.error("setup 阶段要用 --file 写明换成哪一个")
    if args.stage == "replay" and (not args.inner or (args.inner == "fwdmix" and not args.kind)):
        parser.error("replay 阶段要用 --inner 写明 fwdmix 或者 jam；fwdmix 还要 --kind")
    if args.stage not in ("hosthold", "finish", "fit") and not args.module:
        parser.error("这个阶段要用 --module 给出主机上编出来的正式的模块")
    # E-0004 到 E-0006 的阶段要、这份脚本的命令行不给的几个参数。
    args.cpu_bm = args.uni_ac = args.prehdr_src = args.frame_port = args.frame_low = args.frame_high = None
    args.jumbo_mtu, args.with_pause, args.layout = e4.MAX_MTU, False, "万兆口四个"
    args.flows, args.main, args.rate, args.side, args.part, args.samples, args.ping_during = e4.FLOWS, "tcp", "100M", "none", None, 12, None
    args.side_flows, args.side_rate, args.with_lan2, args.diag, args.judge = e4.FLOWS, "100M", False, False, None
    e4.LAYOUT = args.layout
    e3.VARIANTS = os.path.join(args.work, "实验用的模块")
    e3.BRIDGE_SNAPSHOT = os.path.join(args.work, "主机br-lan-开工以前.txt")
    # E-0005、E-0006 的阶段读模块的参数时把名字列在一条命令里：只给它几个名字，全列上去路由器的 shell 一行收不下。
    e5.PARAM_NAMES = ("exp_no_fc", "exp_tx_thread", "exp_rx_softirq", "exp_combined", "exp_rx_hardirq", "exp_tx_hardirq")

    lab.stop_on_signals()
    if args.stage in ("hosthold", "finish", "fit"):
        if args.stage == "hosthold":
            result = e6.host_hold(args)
        elif args.stage == "fit":
            result = stage_fit(args)
        else:
            released = e6.host_release(args)
            result = e4.finish(args)
            result["主机的br-lan和eno1"] = released
        result["代号"] = "E-0007"
        e3.e0002.write_summary(args.summary, result)
        return lab.print_result({key: result.get(key) for key in ("代号", "这一次", "阶段", "读完", "判据", "要看的数")}, result["读完"])
    os.makedirs(args.output)
    prompt = re.compile(lab.LINUX_PROMPT.encode())
    port = lab.open_serial(args.device, args.baud, os.path.join(args.output, "串口.log"))
    level = None
    try:
        if lab.console_is_booting(port, 0.5):
            raise RuntimeError("控制台上有启动的输出，什么都没有发")
        result, level = e3.identity(port, prompt, args)
        result["代号"] = "E-0007"
        for index, helper in enumerate(e4.ROUTER_HELPERS + E7_HELPERS):
            if len(helper) > 400:
                raise RuntimeError(f"第 {index} 个小函数的定义有 {len(helper)} 个字，路由器的 shell 一行收不下")
            ask(port, prompt, args.output, f"01-定义小函数-{index}", helper, 15)
        STAGES[args.stage](port, prompt, args, result)
    except (RuntimeError, ValueError, KeyError, IndexError, OSError, TimeoutError, termios.error,
            subprocess.SubprocessError) as error:
        # 外部边界：串口、设备、主机网卡的错误，和读回来的东西认不出，都照实报出来，不往上抛。
        result = {"代号": "E-0007", "这一次": args.label, "阶段": args.stage, "读完": False,
                  "原因": f"{type(error).__name__}: {error}"}
    finally:
        if level is not None and args.stage != "stall":
            lab.serial_command(port, f"dmesg -n {level}", prompt, 15)
        lab.close_serial(port)
    if result["读完"]:
        e3.e0002.write_summary(args.summary, result)
        result = {"代号": "E-0007", "这一次": args.label, "阶段": args.stage, "读完": True, "摘要写在": args.summary,
                  "没有通过的判据": [check["编号"] for check in result.get("判据", []) if not check["通过"]],
                  "读数里不对劲的地方": result.get("读数里不对劲的地方", []),
                  "要看的数": result.get("要看的数", {})}
    return lab.print_result(result, result["读完"])


if __name__ == "__main__":
    sys.exit(main())
