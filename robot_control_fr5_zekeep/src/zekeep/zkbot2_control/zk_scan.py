#!/usr/bin/env python3
"""
zk_scan.py - ZKBOT-301ED / ZK3U PLC Modbus-RTU 통신 설정 스캐너

시리얼 파라미터(보드레이트/패리티/데이터비트)와 슬레이브 ID 조합을 전부 순회하며
어떤 조합에서 PLC가 응답하는지 찾는다.

★ 읽기 전용 ★
  이 스크립트는 홀딩 레지스터 읽기(FC03)만 사용한다.
  코일/레지스터 쓰기(FC05/06/15/16)는 일절 수행하지 않으므로
  로봇이 움직이거나 PLC 상태가 바뀔 일은 없다.

사용법:
    python3 zk_scan.py /dev/ttyUSB0
    python3 zk_scan.py COM3 --timeout 0.5
"""

import argparse
import sys
import time

# --- 의존성 ---------------------------------------------------------------
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("[에러] pyserial 이 없습니다.  ->  pip install pyserial")

# pymodbus 3.x / 2.x 양쪽 임포트 경로 대응
_PYMODBUS_MAJOR = 3
try:
    from pymodbus.client import ModbusSerialClient  # pymodbus >= 3.0
except ImportError:
    try:
        from pymodbus.client.sync import ModbusSerialClient  # pymodbus 2.x
        _PYMODBUS_MAJOR = 2
    except ImportError:
        sys.exit("[에러] pymodbus 가 없습니다.  ->  pip install pymodbus")

try:
    from pymodbus import __version__ as PYMODBUS_VERSION
except Exception:
    PYMODBUS_VERSION = "unknown"


# --- 스캔할 조합 ----------------------------------------------------------
# (baudrate, parity, bytesize)
SERIAL_COMBOS = [
    (9600,   "N", 8),   # 매뉴얼 공장 출하값
    (9600,   "E", 7),   # FX 프로그래밍 포트 기본값
    (19200,  "E", 7),
    (19200,  "N", 8),
    (38400,  "N", 8),
    (115200, "N", 8),
]

SLAVE_IDS = [1, 2, 3, 4, 5]

READ_START = 0      # 홀딩 레지스터 0번 = D0
READ_COUNT = 5      # D0 ~ D4


# --- pymodbus 버전별 인자 이름 흡수 ---------------------------------------
# 슬레이브 ID 키워드가 버전마다 다르다: unit(2.x) / slave(3.x) / device_id(4.x)
_ID_KWARG_CANDIDATES = ["slave", "device_id", "unit"]
_id_kwarg_cache = None


def read_holding(client, address, count, slave_id):
    """홀딩 레지스터 읽기. pymodbus 버전에 맞는 ID 키워드를 자동 탐색한다."""
    global _id_kwarg_cache

    candidates = [_id_kwarg_cache] if _id_kwarg_cache else _ID_KWARG_CANDIDATES
    last_exc = None

    for kw in candidates:
        try:
            result = client.read_holding_registers(address, count=count, **{kw: slave_id})
            _id_kwarg_cache = kw
            return result
        except TypeError as e:
            # 이 버전이 모르는 키워드 -> 다음 후보로
            last_exc = e
            continue

    if _id_kwarg_cache is None and last_exc is not None:
        raise RuntimeError(
            "pymodbus 슬레이브 ID 키워드를 찾지 못했습니다 "
            f"(시도: {_ID_KWARG_CANDIDATES}, pymodbus {PYMODBUS_VERSION})"
        ) from last_exc
    raise last_exc


def make_client(port, baud, parity, bytesize, stopbits, timeout):
    """버전에 맞는 ModbusSerialClient 생성."""
    kwargs = dict(
        port=port,
        baudrate=baud,
        parity=parity,
        bytesize=bytesize,
        stopbits=stopbits,
        timeout=timeout,
    )
    if _PYMODBUS_MAJOR == 2:
        kwargs["method"] = "rtu"
    else:
        # 재시도를 줄여야 무응답 조합에서 빨리 빠져나온다
        kwargs["retries"] = 1
    try:
        return ModbusSerialClient(**kwargs)
    except TypeError:
        # retries 등 미지원 버전 폴백
        kwargs.pop("retries", None)
        return ModbusSerialClient(**kwargs)


# --- 포트 사전 점검 -------------------------------------------------------
def list_ports_text():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return "  (감지된 시리얼 포트 없음)"
    return "\n".join(f"  {p.device:<20} {p.description}" for p in ports)


def preflight(port):
    """
    pymodbus 의 connect() 는 실패해도 False 만 돌려주고 이유를 안 알려준다.
    pyserial 로 먼저 한 번 열어서 OS 레벨 에러 원인을 정확히 뽑아낸다.
    """
    try:
        s = serial.Serial(port)
        s.close()
        return
    except serial.SerialException as e:
        msg = str(e)
        print(f"\n[에러] 시리얼 포트를 열 수 없습니다: {port}", file=sys.stderr)
        print(f"       원인: {msg}\n", file=sys.stderr)

        low = msg.lower()
        if "permission" in low or "13" in low:
            print("  → 권한 문제로 보입니다. 다음 중 하나를 시도하세요:", file=sys.stderr)
            print("       sudo usermod -aG dialout $USER   (재로그인 필요)", file=sys.stderr)
            print(f"       sudo chmod 666 {port}            (임시 조치)", file=sys.stderr)
        elif "no such" in low or "not exist" in low or "could not open" in low:
            print("  → 포트가 존재하지 않습니다. USB-RS485 어댑터 연결을 확인하세요.", file=sys.stderr)
            print("       dmesg | tail -20     (연결 시 어떤 장치로 잡히는지 확인)", file=sys.stderr)
        elif "busy" in low or "in use" in low or "access is denied" in low:
            print("  → 다른 프로그램이 포트를 점유 중입니다.", file=sys.stderr)
            print(f"       sudo fuser -v {port}   또는  sudo lsof {port}", file=sys.stderr)

        print("\n  현재 감지된 포트 목록:", file=sys.stderr)
        print(list_ports_text(), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[에러] 포트 점검 중 예외: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


# --- 메인 ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="ZK3U PLC Modbus-RTU 통신 설정 스캐너 (읽기 전용)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예)  python3 zk_scan.py /dev/ttyUSB0\n"
               "     python3 zk_scan.py COM3 --timeout 0.5 --stopbits 2",
    )
    ap.add_argument("port", help="시리얼 포트 (예: /dev/ttyUSB0, COM3)")
    ap.add_argument("--timeout", type=float, default=0.3, help="응답 타임아웃 초 (기본 0.3)")
    ap.add_argument("--stopbits", type=int, default=1, choices=[1, 2],
                    help="스톱비트 (기본 1). 전부 무응답이면 2로 재시도해 볼 것")
    ap.add_argument("--start", type=int, default=READ_START, help="읽기 시작 주소 (기본 0 = D0)")
    ap.add_argument("--count", type=int, default=READ_COUNT, help="읽을 레지스터 개수 (기본 5)")
    ap.add_argument("--ids", type=str, default=None,
                    help="슬레이브 ID 목록, 쉼표 구분 (기본 1,2,3,4,5)")
    args = ap.parse_args()

    slave_ids = SLAVE_IDS
    if args.ids:
        try:
            slave_ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            sys.exit("[에러] --ids 는 '1,2,3' 형식이어야 합니다.")

    total = len(SERIAL_COMBOS) * len(slave_ids)
    est = total * args.timeout * 2

    print("=" * 72)
    print("  ZK3U PLC Modbus-RTU 스캔  [읽기 전용 - 쓰기 명령 없음]")
    print("=" * 72)
    print(f"  포트        : {args.port}")
    print(f"  pymodbus    : {PYMODBUS_VERSION}")
    print(f"  읽기 대상   : 홀딩 레지스터 {args.start} ~ {args.start + args.count - 1}"
          f"  (D{args.start}~D{args.start + args.count - 1}, FC03)")
    print(f"  타임아웃    : {args.timeout}s   스톱비트: {args.stopbits}")
    print(f"  조합 수     : {total}개  (예상 최대 {est:.0f}초)")
    print("=" * 72)
    print()

    preflight(args.port)

    hits = []          # 정상 응답
    exception_hits = []  # Modbus 예외 응답 (= 통신은 살아있음)
    idx = 0

    for baud, parity, bytesize in SERIAL_COMBOS:
        label = f"{baud} {bytesize}{parity}{args.stopbits}"
        client = make_client(args.port, baud, parity, bytesize, args.stopbits, args.timeout)

        if not client.connect():
            for _ in slave_ids:
                idx += 1
            print(f"[{idx:3d}/{total}] {label:<14} 포트 연결 실패 - 이 조합 건너뜀")
            try:
                client.close()
            except Exception:
                pass
            continue

        for sid in slave_ids:
            idx += 1
            prefix = f"[{idx:3d}/{total}] {label:<14} id={sid}"

            try:
                rr = read_holding(client, args.start, args.count, sid)
            except Exception as e:
                print(f"{prefix} ... 예외: {type(e).__name__}: {e}")
                continue

            if rr is None:
                print(f"{prefix} ... 무응답")
                continue

            if rr.isError():
                # ExceptionResponse = PLC 가 프레임을 이해했으나 요청을 거절한 것.
                # 즉 보드레이트/패리티/ID 는 맞았다는 강한 신호다.
                name = type(rr).__name__
                if "Exception" in name:
                    print(f"{prefix} ... ⚠ 예외응답 ({rr}) ← 통신은 성립! 주소만 거절됨")
                    exception_hits.append((baud, parity, bytesize, sid, str(rr)))
                else:
                    print(f"{prefix} ... 무응답/CRC오류")
                continue

            regs = list(rr.registers)
            hexs = " ".join(f"0x{v:04X}" for v in regs)
            print(f"{prefix} ... ✅ 성공")
            print(f"{' ' * len(prefix)}     dec: {regs}")
            print(f"{' ' * len(prefix)}     hex: {hexs}")
            hits.append((baud, parity, bytesize, sid, regs))

            # 프레임 간 최소 간격(3.5 char time) 확보
            time.sleep(0.05)

        try:
            client.close()
        except Exception:
            pass

    # --- 요약 ---
    print()
    print("=" * 72)
    print("  스캔 결과")
    print("=" * 72)

    if hits:
        print(f"  ✅ 정상 응답 {len(hits)}건:")
        for baud, parity, bytesize, sid, regs in hits:
            print(f"     {baud} {bytesize}{parity}{args.stopbits}, 슬레이브 ID {sid}  →  {regs}")
    else:
        print("  ✅ 정상 응답: 없음")

    if exception_hits:
        print()
        print(f"  ⚠ 예외 응답 {len(exception_hits)}건 "
              f"(통신 파라미터는 맞음 / 읽기 주소만 거절):")
        for baud, parity, bytesize, sid, msg in exception_hits:
            print(f"     {baud} {bytesize}{parity}{args.stopbits}, 슬레이브 ID {sid}  →  {msg}")
        print("     → 이 설정으로 --start 를 바꿔 다시 시도해 보세요. (예: --start 50)")

    if not hits and not exception_hits:
        print()
        print("  전 조합 무응답. 확인할 것:")
        print("    1) RS485 A/B 결선 극성 (뒤바뀌면 완전 무응답)")
        print("    2) 종단저항 120Ω, GND 공통 연결")
        print("    3) PLC 가 Modbus-RTU 슬레이브 모드인지 (FX 전용 프로토콜이면 응답 안 함)")
        print("    4) --stopbits 2 로 재시도  (8N2 를 쓰는 장비가 있음)")
        print("    5) DB9 프로그래밍 포트라면 별도 파라미터 설정이 필요할 수 있음")
        print("    6) --start 1 로 재시도 (문서 주소가 1-base 표기일 가능성)")

    print("=" * 72)
    return 0 if (hits or exception_hits) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n중단됨.")
        sys.exit(130)
