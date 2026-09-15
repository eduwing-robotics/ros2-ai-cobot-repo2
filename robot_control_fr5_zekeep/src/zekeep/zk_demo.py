#!/usr/bin/env python3
"""
zk_demo.py - ZK3U 통합 데모: 정상화 -> 원점복귀 -> Z축 이동 -> 흡착

FX 프로그래밍 프로토콜. 안전장치:
  - X5(STOP) 가 ON 되면 즉시 전체 중단
  - 각 단계마다 타임아웃
  - finally 에서 모든 코일/출력 강제 OFF (2회 반복)
"""
import sys
import time

import serial

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15
A_X, A_Y, A_M, A_D = 0x0080, 0x00A0, 0x0100, 0x1000

JOG = {"X+": 1, "X-": 2, "Y+": 3, "Y-": 4, "Z+": 5, "Z-": 6}
HOME_M = 120
DONE_M = [121, 122, 123]      # 실측: 원점복귀 완료 시 복귀하는 플래그
PUMP_Y, VALVE_Y = 0o14, 0o15  # Y14 펌프 / Y15 밸브

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

ser = serial.Serial(ZPROF.resolve_from_argv().port, 9600, bytesize=7, parity="E",
                    stopbits=1, timeout=0.05, write_timeout=1.0)


def cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


def snd(body, budget=0.4):
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
    if i < 1:
        return None
    try:
        return bytes.fromhex(r[1:i].decode())
    except Exception:
        return None


def wr(a, payload):
    r = snd(b"1" + f"{a:04X}".encode() + f"{len(payload):02X}".encode()
            + payload.hex().upper().encode() + bytes([ETX]))
    return bool(r) and r[0] == ACK


def addr_m(n):
    v = 0x0800 + n
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}"


def addr_y(o):
    v = 0x0500 + o          # o 는 이미 10진 변환된 값
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}"


def force(cmd, hex4):
    r = snd(cmd.encode() + hex4.encode() + bytes([ETX]), 0.25)
    return bool(r) and r[0] == ACK


def xbits():
    d = rd(A_X, 1)
    return [] if d is None else [i for i in range(7) if d[0] >> i & 1]


def ybits():
    d = rd(A_Y, 2)
    return [] if d is None else [i for i in range(14) if d[i // 8] >> (i % 8) & 1]


def mbits(n=32):
    d = rd(A_M, n)
    return [] if d is None else [i * 8 + b for i, by in enumerate(d)
                                 for b in range(8) if by >> b & 1]


XN = {0: "X리밋", 1: "Y리밋", 2: "Z리밋", 3: "START", 4: "ORIGIN", 5: "STOP", 6: "INFRA"}
YN = {0: "X펄스", 1: "Y펄스", 2: "Z펄스", 3: "ENG펄스", 4: "X방향", 5: "Y방향",
      6: "Z방향", 8: "X_ENA", 9: "Y_ENA", 10: "Z_ENA", 12: "펌프", 13: "밸브"}


def show(tag):
    x, y = xbits(), ybits()
    print(f"    {tag:<10} X:{[XN.get(i, i) for i in x] or '-'}   "
          f"Y:{[YN.get(i, i) for i in y] or '-'}")
    return x


def stop_all(quiet=False):
    for _ in range(2):
        for n in JOG.values():
            force("8", addr_m(n))
        force("8", addr_m(HOME_M))
        force("8", addr_y(PUMP_Y))
        force("8", addr_y(VALVE_Y))
    if not quiet:
        print("    ■ 전체 정지/출력 OFF 완료")


def estop_active():
    return 5 in xbits()


def step(title):
    print(f"\n{'='*54}\n  {title}\n{'='*54}")


try:
    ser.reset_input_buffer()
    ser.write(bytes([ENQ]))
    ser.flush()
    time.sleep(0.2)
    if ser.read(4)[:1] != bytes([ACK]):
        sys.exit("[에러] PLC 링크 실패")
    print("링크 ENQ->ACK ✅")

    # ---------- 0. 정상화 ----------
    step("0) 정상 상태로 되돌리기")
    stop_all()
    if estop_active():
        sys.exit("    ★ STOP(급정지)이 걸려 있습니다. 풀고 다시 실행하세요.")
    show("현재")
    print(f"    M 플래그: {mbits()}")
    ok, _ = wr(A_D + 50 * 2, (30).to_bytes(2, "little")), None
    d = rd(A_D + 50 * 2, 2)
    print(f"    D50 속도 = {int.from_bytes(d,'little') if d else '?'}%")

    # ---------- 1. 원점복귀 ----------
    step("1) 원점복귀 (M120)")
    before = set(mbits())
    force("7", addr_m(HOME_M))
    print("    M120 강제ON")
    t0 = time.time()
    seen = False
    while time.time() - t0 < 25:
        if estop_active():
            print("    ★ STOP 감지 — 중단")
            break
        m = set(mbits())
        if HOME_M in m:
            seen = True
        show(f"{time.time()-t0:4.1f}s")
        if seen and all(f in m for f in DONE_M) and HOME_M not in m:
            print("    ✅ 완료 플래그 M121/122/123 복귀 — 원점복귀 종료")
            break
        time.sleep(0.3)
    force("8", addr_m(HOME_M))
    print(f"    소요 {time.time()-t0:.1f}s   최종 M: {mbits()}")

    # ---------- 2. Z축 이동 ----------
    step("2) Z축 이동 (M5 Z+ 1.5초 → M6 Z- 1.5초)")
    for axis, sec in [("Z+", 1.5), ("Z-", 1.5)]:
        if estop_active():
            print("    ★ STOP 감지 — 중단")
            break
        print(f"    ▶ {axis}")
        force("7", addr_m(JOG[axis]))
        t0 = time.time()
        while time.time() - t0 < sec:
            show(f"{time.time()-t0:4.1f}s")
            if estop_active():
                break
            time.sleep(0.25)
        force("8", addr_m(JOG[axis]))
        print(f"    ■ {axis} 정지")
        time.sleep(0.4)

    # ---------- 3. 흡착 ----------
    step("3) 흡착 (Y14 펌프 → Y15 배기밸브)")
    if not estop_active():
        print("    ▶ 펌프 ON 2.5초")
        force("7", addr_y(PUMP_Y))
        t0 = time.time()
        while time.time() - t0 < 2.5:
            show(f"{time.time()-t0:4.1f}s")
            time.sleep(0.35)
        force("8", addr_y(PUMP_Y))
        print("    ■ 펌프 OFF")
        time.sleep(0.5)
        print("    ▶ 배기밸브 ON 1.2초")
        force("7", addr_y(VALVE_Y))
        t0 = time.time()
        while time.time() - t0 < 1.2:
            show(f"{time.time()-t0:4.1f}s")
            time.sleep(0.35)
        force("8", addr_y(VALVE_Y))
        print("    ■ 밸브 OFF")

finally:
    print()
    stop_all()
    show("최종")
    ser.close()
