#!/usr/bin/env python3
"""
zk_jog.py - ZK3U 점동(inching) 제어  [FX 프로그래밍 프로토콜]

    python3 zk_jog.py Z+ 0.25 10      # 축방향 지속시간(초) 속도(%)
    python3 zk_jog.py Z- 0.25 10
    python3 zk_jog.py stop            # 전축 즉시 정지만

★ 안전 설계
  - 조그 코일은 레벨 방식이라 ON 인 채 남으면 축이 계속 간다.
  - 그래서 M0~M7 을 '바이트 단위로' 쓴다. 한 번의 쓰기로 원하는 축 하나만 ON,
    나머지(반대방향 포함)는 전부 OFF 가 보장된다. 정지는 0x00 한 번.
  - finally + atexit + 시그널 핸들러에서 정지 쓰기를 3회 반복한다.
  - 지속시간 상한 2.0초로 하드 클램프.
"""
import atexit
import signal
import sys
import time

import serial

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

PORT = ZPROF.resolve_from_argv().port
STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15

A_M, A_X, A_D = 0x0100, 0x0080, 0x1000   # FX 메모리맵 (바이트 주소)
MAX_SEC = 2.0

# 조그 코일 M 번호
AXIS_M = {"X+": 1, "X-": 2, "Y+": 3, "Y-": 4, "Z+": 5, "Z-": 6}
ALL_JOG = [1, 2, 3, 4, 5, 6]


def m_addr(n):
    """FX 강제ON/OFF 주소: M n = 0x0800+n, 로우바이트 먼저 (실측 검증됨)."""
    v = 0x0800 + n
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}"

_ser = None


def cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


def _rd_frame(addr, n):
    b = b"0" + f"{addr:04X}".encode() + f"{n:02X}".encode() + bytes([ETX])
    return bytes([STX]) + b + cs(b)


def _wr_frame(addr, payload: bytes):
    b = (b"1" + f"{addr:04X}".encode() + f"{len(payload):02X}".encode()
         + payload.hex().upper().encode() + bytes([ETX]))
    return bytes([STX]) + b + cs(b)


def read(addr, n, wait=0.12):
    _ser.reset_input_buffer()
    _ser.write(_rd_frame(addr, n))
    _ser.flush()
    time.sleep(wait)
    r = _ser.read(_ser.in_waiting or 256)
    if not r or r[0] != STX:
        return None
    e = r.find(bytes([ETX]))
    if e < 0:
        return None
    try:
        return bytes.fromhex(r[1:e].decode("ascii"))
    except Exception:
        return None


def write(addr, payload: bytes, wait=0.12):
    _ser.reset_input_buffer()
    _ser.write(_wr_frame(addr, payload))
    _ser.flush()
    time.sleep(wait)
    r = _ser.read(_ser.in_waiting or 16)
    if not r:
        return False, "무응답"
    if r[0] == ACK:
        return True, "ACK"
    if r[0] == NAK:
        return False, "NAK(거절)"
    return False, r.hex(" ")


def force(cmd, n, wait=0.1):
    """cmd '7'=강제ON, '8'=강제OFF"""
    if _ser is None or not _ser.is_open:
        return False, "포트 닫힘"
    body = cmd.encode() + m_addr(n).encode() + bytes([ETX])
    _ser.reset_input_buffer()
    _ser.write(bytes([STX]) + body + cs(body))
    _ser.flush()
    time.sleep(wait)
    r = _ser.read(_ser.in_waiting or 16)
    if not r:
        return False, "무응답"
    return (r[0] == ACK), {ACK: "ACK", NAK: "NAK"}.get(r[0], r.hex(" "))


def all_stop(quiet=False):
    """조그 코일 M1~M6 전부 강제OFF. 2회 반복."""
    bad = []
    for _ in range(2):
        bad = []
        for n in ALL_JOG:
            ok, _msg = force("8", n, wait=0.06)
            if not ok:
                bad.append(n)
        if not bad:
            break
    if not quiet:
        print(f"  ■ 전축 정지(M1~M6 강제OFF): "
              f"{'OK' if not bad else f'실패 M{bad} — 즉시 비상정지 누르세요'}")
    return not bad


def on_bits(data, octal=False):
    out = []
    for i, byte in enumerate(data or b""):
        for b in range(8):
            if byte >> b & 1:
                n = i * 8 + b
                out.append(f"{(n // 8) * 10 + n % 8}" if octal else str(n))
    return out


def main():
    global _ser
    args = [a for a in sys.argv[1:]]
    if not args:
        sys.exit(__doc__)

    cmd = args[0].upper()
    dur = min(float(args[1]) if len(args) > 1 else 0.25, MAX_SEC)
    spd = int(args[2]) if len(args) > 2 else 10

    _ser = serial.Serial(PORT, 9600, bytesize=7, parity="E", stopbits=1,
                         timeout=0.5, write_timeout=1.0)
    atexit.register(lambda: all_stop(quiet=True))
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *a: (all_stop(), sys.exit(130)))

    # 링크
    _ser.reset_input_buffer()
    _ser.write(bytes([ENQ]))
    _ser.flush()
    time.sleep(0.2)
    r = _ser.read(8)
    if not (r and r[0] == ACK):
        sys.exit(f"[에러] PLC 링크 실패: {r.hex(' ') if r else '무응답'}")
    print("  링크 ENQ->ACK ✅")

    if cmd == "STOP":
        all_stop()
        return

    if cmd not in AXIS_M:
        sys.exit(f"[에러] 축은 {list(AXIS_M)} 중 하나")

    # --- 사전 점검 ---
    x = read(A_X, 4)
    xs = on_bits(x, True)
    print(f"  사전 X입력: {', '.join('X'+v for v in xs) or '없음'}"
          f"   (리밋 X0~X2 {'⚠걸림' if any(v in ('0','1','2') for v in xs) else '정상'})")
    m = read(A_M, 1)
    print(f"  사전 M0~M7: 0x{m[0]:02X}" if m else "  사전 M 읽기 실패")

    # --- 1단계: 속도 쓰기 (무해, 쓰기경로 검증) ---
    print(f"\n  [1] D50 = {spd}% 쓰기 ...", end=" ")
    ok, msg = write(A_D + 50 * 2, spd.to_bytes(2, "little"))
    print(msg)
    back = read(A_D + 50 * 2, 2)
    val = int.from_bytes(back, "little") if back else None
    print(f"      읽기 확인: D50 = {val}")
    if val != spd:
        all_stop()
        sys.exit("  ✗ 쓰기 검증 실패. 이동 중단.")
    print("      ✅ 쓰기 경로 정상")

    # --- 2단계: 점동 ---
    mn = AXIS_M[cmd]
    print(f"\n  [2] {cmd} 점동 {dur}s  (M{mn} 강제ON, 주소 {m_addr(mn)})")
    t0 = time.time()
    try:
        ok, msg = force("7", mn)
        print(f"      기동: {msg}")
        if not ok:
            all_stop()
            sys.exit("  ✗ 기동 실패")
        chk = read(A_M, 1, wait=0.05)
        if chk:
            state = "ON 반영됨" if chk[0] >> mn & 1 else "★비트 안 켜짐(래더가 덮어씀?)"
            print(f"      확인: M0~M7 = 0x{chk[0]:02X}  {state}")
        while time.time() - t0 < dur:
            time.sleep(0.02)
    finally:
        all_stop()
        print(f"      경과 {time.time()-t0:.2f}s")

    x2 = read(A_X, 4)
    print(f"\n  사후 X입력: {', '.join('X'+v for v in on_bits(x2, True)) or '없음'}")
    m2 = read(A_M, 1)
    print(f"  사후 M0~M7: 0x{m2[0]:02X}" if m2 else "  사후 M 읽기 실패")


if __name__ == "__main__":
    try:
        main()
    finally:
        if _ser:
            try:
                all_stop(quiet=True)
                _ser.close()
            except Exception:
                pass
