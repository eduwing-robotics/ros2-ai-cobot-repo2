#!/usr/bin/env python3
"""
zk_loopback.py - USB-RS232 어댑터/케이블 자체 점검 (루프백)

[준비]
  1. 케이블을 PLC 에서 뽑는다.
  2. DB9 커넥터의 2번 핀과 3번 핀을 서로 단락시킨다.
     (핀셋, 클립, 점퍼선, 심지어 구부린 종이클립도 됨)
  3. 이 스크립트를 실행한다.

[판정]
  통과 -> 어댑터/케이블/드라이버 전부 정상. 문제는 PLC 쪽 또는 배선 방식.
  실패 -> 어댑터나 케이블 자체가 불량. 교체 대상.

PLC 에 연결하지 않은 상태에서 하는 시험이라 로봇에 아무 영향이 없다.
"""
import sys
import time

import serial

sys.path.insert(0, "/home/ar")
import zk_profiles as ZPROF  # noqa: E402

PORT = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("/dev/")
        else ZPROF.resolve_from_argv(announce=False).port)

TESTS = [
    (9600,   b"\x55\xAA\x00\xFF\x12\x34"),
    (19200,  b"HELLO-ZK3U"),
    (115200, bytes(range(32, 64))),
]

print("=" * 66)
print("  USB-RS232 어댑터 루프백 시험")
print(f"  포트: {PORT}")
print("=" * 66)
print()
print("  ※ DB9 의 2번 핀과 3번 핀이 단락되어 있어야 합니다.")
print("  ※ 케이블은 PLC 에서 분리한 상태여야 합니다.")
print()

passed = 0
for baud, payload in TESTS:
    try:
        s = serial.Serial(PORT, baudrate=baud, bytesize=8, parity="N",
                          stopbits=1, timeout=0.5, write_timeout=1.0)
    except Exception as e:
        print(f"  [{baud:>6}] 포트 열기 실패: {e}")
        continue

    try:
        s.reset_input_buffer()
        s.reset_output_buffer()
        s.write(payload)
        s.flush()
        time.sleep(0.15)
        echo = s.read(len(payload))
    except Exception as e:
        print(f"  [{baud:>6}] 오류: {type(e).__name__}: {e}")
        s.close()
        continue
    finally:
        try:
            s.close()
        except Exception:
            pass

    if echo == payload:
        print(f"  [{baud:>6}] ✅ 통과   보냄={payload.hex(' ')[:32]}...")
        passed += 1
    elif echo:
        print(f"  [{baud:>6}] ⚠ 일부/깨짐")
        print(f"            보냄 = {payload.hex(' ')}")
        print(f"            받음 = {echo.hex(' ')}")
    else:
        print(f"  [{baud:>6}] ❌ 수신 0바이트")

print()
print("=" * 66)
if passed == len(TESTS):
    print("  ✅ 어댑터·케이블·드라이버 정상.")
    print("     → 문제는 PLC 쪽입니다. 확인 순서:")
    print("        1) PLC 전원")
    print("        2) DB9 가 RS232 인지 RS422 인지 (RS422면 이 케이블로 불가)")
    print("        3) 널모뎀(크로스) 젠더 필요 여부 - 2번/3번 핀 교차")
    print("        4) RS485 채널 + USB-RS485 어댑터로 전환")
elif passed > 0:
    print("  ⚠ 일부만 통과. 케이블 접촉 불량 또는 고속에서 불안정.")
    print("     핀 단락 상태를 다시 확인하고 재시도해 보세요.")
else:
    print("  ❌ 전부 실패.")
    print("     핀 2-3 단락이 확실하다면 어댑터/케이블 불량입니다. 교체하세요.")
    print("     (PL2303 은 가짜 칩이 많아 드라이버는 잡혀도 실동작이 안 되는 사례가 있습니다)")
print("=" * 66)
