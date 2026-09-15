#!/usr/bin/env python3
"""
zk_fx.py - 미쓰비시 FX 프로그래밍 프로토콜로 PLC 정보 읽기 (읽기 전용)

Modbus 가 아니라 FX 전용 프로토콜(포맷1)로 대화한다.
사용하는 명령은 CMD '0' = 디바이스 메모리 읽기 뿐.
쓰기(CMD '1')·강제ON(CMD '7')·강제OFF(CMD '8') 는 일절 사용하지 않는다.

프레임:  STX(02) '0' AAAA NN ETX(03) SS
         AAAA = 4자리 hex 바이트주소, NN = 읽을 바이트수(2자리 hex, 최대 40)
         SS   = STX 다음부터 ETX 까지 합의 하위 2자리 hex
응답:    STX(02) <데이터 hex ASCII> ETX(03) SS
"""
import sys
import time

import serial

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

PORT = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("/dev/")
        else ZPROF.resolve_from_argv(announce=False).port)
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 9600

STX, ETX, ENQ, ACK, NAK = 0x02, 0x03, 0x05, 0x06, 0x15


def checksum(payload: bytes) -> bytes:
    return f"{sum(payload) & 0xFF:02X}".encode()


def read_frame(addr: int, nbytes: int) -> bytes:
    body = b"0" + f"{addr:04X}".encode() + f"{nbytes:02X}".encode() + bytes([ETX])
    return bytes([STX]) + body + checksum(body)


def talk(ser, frame, wait=0.35):
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 128)


def parse(resp: bytes):
    """응답에서 데이터부(hex ASCII)를 꺼내 바이트로."""
    if not resp:
        return None, "무응답"
    if resp[0] == NAK:
        return None, "NAK (요청 거절)"
    if resp[0] != STX:
        return None, f"예상 밖 응답: {resp.hex(' ')}"
    e = resp.find(bytes([ETX]))
    if e < 0:
        return None, f"ETX 없음: {resp.hex(' ')}"
    payload = resp[1:e]
    try:
        return bytes.fromhex(payload.decode("ascii")), None
    except Exception:
        return None, f"파싱 실패: {payload!r}"


def words(b: bytes):
    """FX 는 리틀엔디언 워드."""
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b) - 1, 2)]


print("=" * 68)
print("  FX 프로그래밍 프로토콜 조회  [읽기 전용 - CMD '0' 읽기만]")
print(f"  포트 {PORT} @ {BAUD}")
print("=" * 68)

results = {}
for parity, bits in [("E", 7), ("N", 8)]:
    try:
        ser = serial.Serial(PORT, BAUD, bytesize=bits, parity=parity,
                            stopbits=1, timeout=0.5, write_timeout=1.0)
    except Exception as e:
        print(f"\n[{bits}{parity}1] 포트 열기 실패: {e}")
        continue

    print(f"\n[{bits}{parity}1] ------------------------------------------")

    # 1) 링크 체크
    r = talk(ser, bytes([ENQ]), 0.2)
    link = bool(r) and r[0] == ACK
    print(f"  ENQ -> {'ACK ✅' if link else (r.hex(' ') if r else '무응답')}")
    if not link:
        ser.close()
        continue

    # 2) D8000 영역 = PLC 기종/버전 정보
    #    FX 메모리맵: D8000 = 0x0E00, 워드당 2바이트
    for label, addr, n, names in [
        ("D8000~D8003 (기종/버전)", 0x0E00, 8,
         ["D8000 워치독", "D8001 PLC기종·버전", "D8002 메모리용량", "D8003 메모리종별"]),
        ("D0~D3 (일반)", 0x1000, 8, ["D0", "D1", "D2", "D3"]),
        ("D50 (속도)", 0x1000 + 50 * 2, 2, ["D50"]),
        ("D100~D101 (X목표)", 0x1000 + 100 * 2, 4, ["D100", "D101"]),
    ]:
        resp = talk(ser, read_frame(addr, n))
        data, err = parse(resp)
        if err:
            print(f"  {label:<26} -> {err}")
            continue
        ws = words(data)
        print(f"  {label:<26} -> {ws}")
        for nm, v in zip(names, ws):
            print(f"       {nm:<22} = {v:>6}  (0x{v:04X})")
        results[label] = ws

    ser.close()

# --- D8001 해석 ---
print("\n" + "=" * 68)
for k, ws in results.items():
    if k.startswith("D8000") and len(ws) > 1:
        d8001 = ws[1]
        series = d8001 // 100
        ver = d8001 % 100
        table = {240: "FX2N / FX2NC", 241: "FX1S", 242: "FX1N / FX1NC",
                 243: "FX3G / FX3GC", 244: "FX3U / FX3UC", 245: "FX3S",
                 246: "FX5U", 260: "FX3U 계열(확장)"}
        print(f"  D8001 = {d8001}")
        print(f"    기종코드 {series} -> {table.get(series, '미상')}")
        print(f"    버전 Ver.{ver/100 + 1:.2f}".replace("Ver.1", "Ver.1."))
print("=" * 68)
