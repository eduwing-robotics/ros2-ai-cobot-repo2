#!/usr/bin/env python3
"""ZKBOT 2호기 흡착 I/O 시험.

PLC 출력은 1호기 제어함/기존 배선 자료를 바탕으로 한다.
  Y14: 진공 펌프
  Y15: 밸브(배기/해제 용도는 실제 동작으로 추가 확인 필요)

사용법:
  python3 zk_io.py pump-pulse --seconds 1

시험은 펌프만 제한 시간 동안 ON하고, 성공·실패·Ctrl+C 모두에서
펌프와 밸브를 OFF로 되돌린다. 팔 관절 조그는 사용하지 않는다.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zkbot2_config import DEFAULT_PORT
from zkfx import PUMP, VALVE, ZK


def main() -> int:
    parser = argparse.ArgumentParser(description="ZKBOT 2호기 흡착 I/O 시험")
    parser.add_argument("command", choices=("pump-pulse", "valve-pulse", "pump-valve-sequence"))
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--port", default=DEFAULT_PORT)
    args = parser.parse_args()

    if not 0.1 <= args.seconds <= 5.0:
        parser.error("--seconds는 안전상 0.1~5.0초만 허용합니다.")

    zk = ZK(args.port)
    if not zk.link():
        print("■ PLC 링크 실패", file=sys.stderr)
        return 1

    try:
        xs = zk.x() or set()
        if 5 in xs:
            print("■ X5(비상정지)가 눌려 있어 I/O 시험을 실행하지 않습니다.", file=sys.stderr)
            return 1

        # 각 시험은 한 출력만 바꾸거나, 명시적인 순서로만 조합한다.
        zk.y_off(PUMP)
        zk.y_off(VALVE)

        if args.command == "pump-pulse":
            print(f"펌프 Y14 ON: {args.seconds:.1f}초 (밸브 Y15 OFF 유지)", flush=True)
            if not zk.y_on(PUMP):
                print("■ 펌프 Y14 ON 명령이 PLC에서 거부됐습니다.", file=sys.stderr)
                return 1
            time.sleep(args.seconds)
            print("펌프 펄스 완료", flush=True)
            return 0

        if args.command == "valve-pulse":
            print(f"밸브 Y15 ON: {args.seconds:.1f}초 (펌프 Y14 OFF 유지)", flush=True)
            if not zk.y_on(VALVE):
                print("■ 밸브 Y15 ON 명령이 PLC에서 거부됐습니다.", file=sys.stderr)
                return 1
            time.sleep(args.seconds)
            print("밸브 펄스 완료", flush=True)
            return 0

        # Y15의 기능을 확인하는 조합 시험: 먼저 흡착을 만든 뒤 Y15만 짧게 켠다.
        # 시험 물체는 사람이 반드시 지지해야 한다.
        print("펌프 Y14 ON: 2.0초 (Y15 OFF) → 밸브 Y15 ON: "
              f"{args.seconds:.1f}초 (Y14 유지)", flush=True)
        if not zk.y_on(PUMP):
            print("■ 펌프 Y14 ON 명령이 PLC에서 거부됐습니다.", file=sys.stderr)
            return 1
        time.sleep(2.0)
        if not zk.y_on(VALVE):
            print("■ 밸브 Y15 ON 명령이 PLC에서 거부됐습니다.", file=sys.stderr)
            return 1
        time.sleep(args.seconds)
        print("펌프·밸브 조합 시험 완료", flush=True)
        return 0
    finally:
        # 시험 결과와 무관하게 출력은 반드시 해제한다.
        for _ in range(2):
            zk.y_off(PUMP)
            zk.y_off(VALVE)
        ys = zk.y() or set()
        if PUMP in ys or VALVE in ys:
            print("🔴 출력 OFF 확인 실패 — 비상정지와 PLC 출력을 확인하세요.", file=sys.stderr)
        else:
            print("출력 확인: Y14/Y15 OFF", flush=True)
        print(zk.report(), flush=True)
        zk.close()


if __name__ == "__main__":
    raise SystemExit(main())
