#!/usr/bin/env python3
"""
zk_io.py - ZK3U 출력 직접 제어 (펌프/밸브)

    python3 zk_io.py pump on          # 펌프 켜고 유지 (끄기 전까지)
    python3 zk_io.py pump off
    python3 zk_io.py pump 10          # 펌프 10초 후 자동 OFF
    python3 zk_io.py valve 3          # 배기밸브 3초
    python3 zk_io.py suck 10 2        # 흡착 10초 -> 배기 2초 (한 사이클)
    python3 zk_io.py status
    python3 zk_io.py off              # 전부 OFF

출력을 직접 강제하므로 래더 프로그램과 무관하게 동작한다.
"""
import sys
import time

import serial

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15
PUMP, VALVE = 0o14, 0o15      # Y14 펌프, Y15 배기밸브

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

ser = serial.Serial(ZPROF.resolve_from_argv().port, 9600, bytesize=7, parity="E",
                    stopbits=1, timeout=0.05, write_timeout=1.0)


def cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


def snd(body, budget=0.35):
    ser.reset_input_buffer()
    ser.write(bytes([STX]) + body + cs(body))
    ser.flush()
    buf, t = b"", time.time()
    while time.time() - t < budget:
        buf += ser.read(64)
        i = buf.find(bytes([ETX]))
        if i > 0 and len(buf) >= i + 3:
            break
        if buf[:1] in (bytes([ACK]), bytes([NAK])):
            break
    return buf


def rd(a, n):
    r = snd(b"0" + f"{a:04X}".encode() + f"{n:02X}".encode() + bytes([ETX]))
    if not r or r[0] != STX:
        return None
    i = r.find(bytes([ETX]))
    return bytes.fromhex(r[1:i].decode()) if i > 0 else None


def ay(o):
    v = 0x0500 + o
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}"


def force(cmd, o):
    r = snd(cmd.encode() + ay(o).encode() + bytes([ETX]), 0.25)
    return bool(r) and r[0] == ACK


def ybits():
    d = rd(0x00A0, 2)
    return [] if d is None else [i for i in range(14) if d[i // 8] >> (i % 8) & 1]


def status():
    y = ybits()
    print(f"  펌프 Y14 : {'ON' if 12 in y else 'OFF'}")
    print(f"  밸브 Y15 : {'ON' if 13 in y else 'OFF'}")
    x = rd(0x0080, 1)
    if x is not None and x[0] >> 5 & 1:
        print("  ★ 급정지(X5) 걸림")


def hold(o, sec, label):
    force("7", o)
    print(f"  ▶ {label} ON — {sec:.0f}초")
    t0 = time.time()
    try:
        while time.time() - t0 < sec:
            left = sec - (time.time() - t0)
            on = 12 if o == PUMP else 13
            mark = "●" if on in ybits() else "○"
            print(f"\r     {mark} 남은 {left:4.1f}s   ", end="", flush=True)
            time.sleep(0.5)
    finally:
        force("8", o)
        force("8", o)
        print(f"\r  ■ {label} OFF          ")


try:
    a = sys.argv[1:] or ["status"]
    cmd = a[0].lower()

    if cmd == "status":
        status()
    elif cmd == "off":
        for _ in range(2):
            force("8", PUMP)
            force("8", VALVE)
        print("  ■ 전부 OFF")
        status()
    elif cmd in ("pump", "valve"):
        o = PUMP if cmd == "pump" else VALVE
        name = "펌프" if cmd == "pump" else "배기밸브"
        arg = a[1] if len(a) > 1 else "on"
        if arg == "on":
            force("7", o)
            print(f"  ▶ {name} ON (유지). 끄려면: zk_io.py {cmd} off")
            status()
        elif arg == "off":
            force("8", o)
            force("8", o)
            print(f"  ■ {name} OFF")
        else:
            hold(o, float(arg), name)
    elif cmd == "suck":
        s1 = float(a[1]) if len(a) > 1 else 10
        s2 = float(a[2]) if len(a) > 2 else 2
        hold(PUMP, s1, "흡착 펌프")
        time.sleep(0.4)
        hold(VALVE, s2, "배기밸브")
    else:
        print(__doc__)
finally:
    ser.close()
