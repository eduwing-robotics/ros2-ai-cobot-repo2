#!/usr/bin/env python3
"""
zk_shutdown.py - 전원 끄기 전 안전 자세로 접기

왜 필요한가
  전원을 끄면 모터가 무여자가 되어 팔이 중력으로 처진다.
  펼쳐진 자세일수록 많이 처지고, 처진 만큼 카운터와 실제 위치가 어긋난다.
  접힌 자세로 두면 처질 여지가 작아 다음날 복구가 쉽다.

    python3 zk_shutdown.py
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from zkfx import ZK, PUMP, VALVE
from zk_safety import Guard, Abort, HOME_FLAG
from zkbot2_config import DEFAULT_PORT
import zk_pose as P

# 접힌 안전 자세 — 리밋(원점) 근처라 처짐이 적고 다음날 원점복귀도 짧다
SAFE = {"A1": -5.0, "A2": -5.0, "A3": -5.0}

zk = ZK(DEFAULT_PORT)
if not zk.link():
    sys.exit("링크 실패")
try:
    ms = zk.m(32) or set()
    miss = [j for j, b in HOME_FLAG.items() if b not in ms]
    if miss:
        print(f"⚠ 원점 미확보({miss}) — 각도를 믿을 수 없어 이동하지 않습니다.")
        print("  수동으로 팔을 접어두고 전원을 끄세요.")
    else:
        guard = Guard(zk, homing=False)
        guard.preflight(P.angles(zk))
        print("안전 자세로 접는 중...", flush=True)
        for j in ("A3", "A2", "A1"):        # 팔 끝부터 접는다
            P.goto_axis(zk, guard, j, SAFE[j], 0.8, ((20.0, 2.0), (8.0, 0.6)))
        print(f"\n✅ 접기 완료  {P.fmt(P.angles(zk))}")
except Abort as e:
    print(f"\n■ 중단: {e}")
finally:
    P.all_off(zk)
    zk.y_off(PUMP); zk.y_off(VALVE)
    print("\n펌프·밸브 OFF, 조그비트 해제 완료.")
    print("이제 전원을 꺼도 됩니다.")
    print(zk.report())
    zk.close()
