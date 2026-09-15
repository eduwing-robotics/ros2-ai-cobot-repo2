#!/usr/bin/env python3
"""
zk_read.py - ZK3U 상태 정밀 조회 (체크섬 검증 + 2회 일치 확인)

FX 응답 프레임의 체크섬을 검증하고, 같은 값을 연속 2회 읽어
일치할 때만 신뢰한다. 깨진 프레임을 상태 변화로 오독하는 것을 막는다.

    python3 zk_read.py            # 1회
    python3 zk_read.py 10         # 10회 반복 (변화만 출력)
"""
import sys
import time

import serial

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15

XN = {0: "A1리밋(베이스)", 1: "A2리밋(큰팔)", 2: "A3리밋(작은팔)",
      3: "START", 4: "ORIGIN", 5: "급정지", 6: "INFRA"}
YN = {0: "A1펄스", 1: "A2펄스", 2: "A3펄스", 3: "ENG펄스", 4: "A1방향",
      5: "A2방향", 6: "A3방향", 8: "A1_ENA", 9: "A2_ENA", 10: "A3_ENA",
      12: "펌프", 13: "밸브"}

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

ser = serial.Serial(ZPROF.resolve_from_argv().port, 9600, bytesize=7, parity="E",
                    stopbits=1, timeout=0.05, write_timeout=1.0)

stats = {"ok": 0, "csum_bad": 0, "noresp": 0, "mismatch": 0}


def cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


def _once(a, n):
    body = b"0" + f"{a:04X}".encode() + f"{n:02X}".encode() + bytes([ETX])
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(bytes([STX]) + body + cs(body))
    ser.flush()
    buf, t = b"", time.time()
    need = 1 + n * 2 + 1 + 2
    while time.time() - t < 0.6:
        buf += ser.read(128)
        s = buf.find(bytes([STX]))
        if s >= 0 and len(buf) - s >= need:
            break
    s = buf.find(bytes([STX]))
    if s < 0:
        stats["noresp"] += 1
        return None
    e = buf.find(bytes([ETX]), s)
    if e < 0 or len(buf) < e + 3:
        stats["noresp"] += 1
        return None
    payload = buf[s + 1:e]
    got = buf[e + 1:e + 3]
    want = cs(payload + bytes([ETX]))
    if got.upper() != want.upper():        # ★ 체크섬 검증
        stats["csum_bad"] += 1
        return None
    try:
        v = bytes.fromhex(payload.decode("ascii"))
    except Exception:
        stats["csum_bad"] += 1
        return None
    if len(v) != n:
        stats["csum_bad"] += 1
        return None
    stats["ok"] += 1
    return v


def rd(a, n, tries=4):
    """연속 2회 같은 값이 나올 때만 채택."""
    prev = None
    for _ in range(tries):
        v = _once(a, n)
        if v is None:
            continue
        if prev is not None and v == prev:
            return v
        if prev is not None and v != prev:
            stats["mismatch"] += 1
        prev = v
    return prev


def bits(d, cnt, names):
    return [names.get(i, str(i)) for i in range(cnt) if d and d[i // 8] >> (i % 8) & 1]


def snapshot():
    x = rd(0x0080, 1)
    y = rd(0x00A0, 2)
    m = rd(0x0100, 32)
    d50 = rd(0x1000 + 50 * 2, 2)
    ms = [i * 8 + b for i, by in enumerate(m or b"") for b in range(8) if by >> b & 1]
    return (bits(x, 7, XN), bits(y, 14, YN), ms,
            int.from_bytes(d50, "little") if d50 else None)


link = False
for _ in range(5):
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    time.sleep(0.15)
    ser.write(bytes([ENQ]))
    ser.flush()
    time.sleep(0.3)
    if ACK in ser.read(16):
        link = True
        break
if not link:
    sys.exit("[에러] PLC 링크 실패")

n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
prev = None
try:
    for k in range(n):
        xs, ys, ms, d50 = snapshot()
        cur = (tuple(xs), tuple(ys), tuple(ms))
        if cur != prev or n == 1:
            print(f"[{k:>3}] X: {xs or '-'}")
            print(f"      Y: {ys or '-'}")
            print(f"      M: {ms or '-'}     D50={d50}%")
            prev = cur
        time.sleep(0.3)
finally:
    print(f"\n프레임 통계  정상 {stats['ok']}  체크섬오류 {stats['csum_bad']}  "
          f"무응답 {stats['noresp']}  재읽기불일치 {stats['mismatch']}")
    ser.close()
