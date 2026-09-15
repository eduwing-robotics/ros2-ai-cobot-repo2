#!/usr/bin/env python3
"""
zk_move.py - ZK3U 자동이동 (D100/D200/D300 목표펄스 + M21 기동)

    python3 zk_move.py 0 0 3200          # X,Y,Z 목표펄스 (절대)
    python3 zk_move.py 0 0 3200 --speed 30

매뉴얼 기준: 목표 펄스는 32비트(D n = 하위워드, D n+1 = 상위워드),
기동 M21, 완료 플래그 M155(X)/M156(Y)/M157(Z). 1회전 = 6400펄스.

안전: STOP(X5) 감지 시 즉시 M21 OFF, 타임아웃 25초, finally 전체 OFF.
"""
import sys
import time

import serial

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15
A_X, A_Y, A_M, A_D = 0x0080, 0x00A0, 0x0100, 0x1000
TARGET_D = {"X": 100, "Y": 200, "Z": 300}
DONE_M = {"X": 155, "Y": 156, "Z": 157}
START_M = 21
TIMEOUT = 25.0

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) < 3:
    sys.exit(__doc__)
tx, ty, tz = (int(a) for a in args[:3])
speed = 30
if "--speed" in sys.argv:
    speed = int(sys.argv[sys.argv.index("--speed") + 1])

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
    return bytes.fromhex(r[1:i].decode()) if i > 0 else None


def wr(a, payload):
    r = snd(b"1" + f"{a:04X}".encode() + f"{len(payload):02X}".encode()
            + payload.hex().upper().encode() + bytes([ETX]))
    return bool(r) and r[0] == ACK


def addr_m(n):
    v = 0x0800 + n
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}"


def force(cmd, n):
    r = snd(cmd.encode() + addr_m(n).encode() + bytes([ETX]), 0.25)
    return bool(r) and r[0] == ACK


def d32(dn):
    b = rd(A_D + dn * 2, 4)
    return None if b is None else int.from_bytes(b, "little")


def mbits():
    d = rd(A_M, 32)
    return [] if d is None else [i * 8 + b for i, by in enumerate(d)
                                 for b in range(8) if by >> b & 1]


def xb():
    d = rd(A_X, 1)
    return [] if d is None else [i for i in range(7) if d[0] >> i & 1]


def yb():
    d = rd(A_Y, 2)
    return [] if d is None else [i for i in range(14) if d[i // 8] >> (i % 8) & 1]


YN = {0: "X펄스", 1: "Y펄스", 2: "Z펄스", 3: "ENG펄스", 4: "X방향", 5: "Y방향",
      6: "Z방향", 8: "X_ENA", 9: "Y_ENA", 10: "Z_ENA", 12: "펌프", 13: "밸브"}
XN = {0: "X리밋", 1: "Y리밋", 2: "Z리밋", 3: "START", 4: "ORIGIN", 5: "STOP", 6: "INFRA"}

try:
    ser.reset_input_buffer()
    ser.write(bytes([ENQ]))
    ser.flush()
    time.sleep(0.2)
    if ser.read(4)[:1] != bytes([ACK]):
        sys.exit("[에러] PLC 링크 실패")
    print("링크 ENQ->ACK ✅\n")

    if 5 in xb():
        sys.exit("★ STOP(급정지)이 걸려 있습니다. 풀고 다시 실행하세요.")

    print("=== 기동 전 ===")
    print(f"  M 플래그 : {mbits()}")
    print(f"  현재 목표: D100={d32(100)}  D200={d32(200)}  D300={d32(300)}")

    print("\n=== 값 쓰기 ===")
    wr(A_D + 50 * 2, speed.to_bytes(2, "little"))
    print(f"  D50 속도 = {int.from_bytes(rd(A_D+50*2,2),'little')}%")
    for ax, val in (("X", tx), ("Y", ty), ("Z", tz)):
        dn = TARGET_D[ax]
        ok = wr(A_D + dn * 2, val.to_bytes(4, "little", signed=True))
        back = d32(dn)
        mark = "✅" if back == val else "★불일치"
        print(f"  D{dn}/{dn+1} {ax}목표 = {val:>8}  ->  읽기 {back}  {mark}")
        if back != val:
            sys.exit("  쓰기 검증 실패 — 중단")

    print(f"\n=== M21 기동 (완료 플래그 M155/156/157 대기, 최대 {TIMEOUT:.0f}s) ===")
    force("7", START_M)
    t0 = time.time()
    prev = None
    done = False
    while time.time() - t0 < TIMEOUT:
        x, y, m = xb(), yb(), mbits()
        if 5 in x:
            print("  ★ STOP 감지 — 즉시 중단")
            break
        cur = (tuple(x), tuple(y), tuple(v for v in m if v in (21, 155, 156, 157)))
        if cur != prev:
            print(f"  [{time.time()-t0:5.1f}s] X:{[XN.get(i,i) for i in x] or '-'}"
                  f"  Y:{[YN.get(i,i) for i in y] or '-'}"
                  f"  M:{list(cur[2]) or '-'}")
            prev = cur
        if all(DONE_M[a] in m for a in ("X", "Y", "Z")):
            print(f"  ✅ 완료 플래그 3개 전부 ON — {time.time()-t0:.1f}s")
            done = True
            break
        time.sleep(0.2)
    if not done:
        print(f"  ⏱ {time.time()-t0:.1f}s 경과 (완료 플래그 미확인)")
    force("8", START_M)
    print("  M21 OFF")

    print("\n=== 기동 후 ===")
    print(f"  M 플래그 : {mbits()}")
    print(f"  목표값   : D100={d32(100)}  D200={d32(200)}  D300={d32(300)}")

finally:
    for _ in range(2):
        force("8", START_M)
        for n in (1, 2, 3, 4, 5, 6):
            force("8", n)
    print("\n■ M21/조그 전부 OFF")
    ser.close()
