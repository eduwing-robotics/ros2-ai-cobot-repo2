#!/usr/bin/env python3
"""
zk_status.py - ZK3U PLC 상태 실시간 조회 (읽기 전용)

FX 프로그래밍 프로토콜 CMD '0'(읽기) 만 사용. 쓰기·강제ON/OFF 없음.

    python3 zk_status.py                # 1회 조회
    python3 zk_status.py --watch        # 0.5초마다 갱신 (Ctrl+C 종료)
"""
import sys
import time

import serial

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

PORT = ZPROF.resolve_from_argv(announce=False).port
for a in sys.argv[1:]:          # 위치인자로 준 /dev/... 도 계속 받는다
    if a.startswith("/dev/"):
        PORT = a
WATCH = "--watch" in sys.argv

STX, ETX, ENQ, ACK = 0x02, 0x03, 0x05, 0x06

# FX 메모리맵 (바이트 주소)
A_X, A_Y, A_M, A_D, A_D8 = 0x0080, 0x00A0, 0x0100, 0x1000, 0x0E00


def cs(p):
    return f"{sum(p) & 0xFF:02X}".encode()


def frame(addr, n):
    b = b"0" + f"{addr:04X}".encode() + f"{n:02X}".encode() + bytes([ETX])
    return bytes([STX]) + b + cs(b)


def read(ser, addr, n, wait=0.12):
    ser.reset_input_buffer()
    ser.write(frame(addr, n))
    ser.flush()
    time.sleep(wait)
    r = ser.read(ser.in_waiting or 256)
    if not r or r[0] != STX:
        return None
    e = r.find(bytes([ETX]))
    if e < 0:
        return None
    try:
        return bytes.fromhex(r[1:e].decode("ascii"))
    except Exception:
        return None


def on_bits(data, octal=False):
    out = []
    for i, byte in enumerate(data or b""):
        for b in range(8):
            if byte >> b & 1:
                n = i * 8 + b
                out.append(f"{(n // 8) * 10 + n % 8}" if octal else str(n))
    return out


def words(b):
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b) - 1, 2)]


def dump(ser):
    x = read(ser, A_X, 4)
    y = read(ser, A_Y, 4)
    m = read(ser, A_M, 24)          # M0~M191
    d = read(ser, A_D + 50 * 2, 2)  # D50
    dt = read(ser, A_D + 100 * 2, 4)

    xs, ys, ms = on_bits(x, True), on_bits(y, True), on_bits(m)

    print(f"  X 입력  ON: {', '.join('X' + v for v in xs) or '(없음)'}")
    lim = [v for v in xs if v in ("0", "1", "2")]
    print(f"     리밋 X0~X2 : {'⚠ ' + ','.join('X'+v for v in lim) if lim else '안 걸림'}")
    print(f"     X5 비상정지선: {'ON' if '5' in xs else 'OFF'}")
    print(f"  Y 출력  ON: {', '.join('Y' + v for v in ys) or '(없음)'}")
    print(f"     Y14 펌프={'ON' if '14' in ys else 'OFF'}   "
          f"Y15 배기={'ON' if '15' in ys else 'OFF'}")
    jog = {1: "X+", 2: "X-", 3: "Y+", 4: "Y-", 5: "Z+", 6: "Z-"}
    act = [jog[int(v)] for v in ms if v.isdigit() and int(v) in jog]
    print(f"  M 조그  : {', '.join(act) if act else '전부 정지'}")
    print(f"  M21 기동: {'ON' if '21' in ms else 'OFF'}     "
          f"완료 M155/156/157: "
          f"{'/'.join('O' if str(v) in ms else '-' for v in (155, 156, 157))}")
    if d:
        print(f"  D50 속도: {words(d)[0]}%")
    if dt:
        w = words(dt)
        print(f"  D100/101 X목표펄스: {w[1] << 16 | w[0]}")


try:
    ser = serial.Serial(PORT, 9600, bytesize=7, parity="E", stopbits=1,
                        timeout=0.5, write_timeout=1.0)
except Exception as e:
    sys.exit(f"[에러] 포트 열기 실패: {e}")

ser.reset_input_buffer()
ser.write(bytes([ENQ]))
ser.flush()
time.sleep(0.2)
r = ser.read(8)
if not (r and r[0] == ACK):
    ser.close()
    sys.exit(f"[에러] PLC 링크 실패 (ENQ 응답: {r.hex(' ') if r else '무응답'})")

try:
    while True:
        if WATCH:
            print("\033[2J\033[H", end="")
        print("=" * 52)
        print(f"  ZK3U 상태  {time.strftime('%H:%M:%S')}   [읽기 전용]")
        print("=" * 52)
        dump(ser)
        if not WATCH:
            break
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\n종료")
finally:
    ser.close()
