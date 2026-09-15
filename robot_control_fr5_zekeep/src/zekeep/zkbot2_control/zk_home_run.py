#!/usr/bin/env python3
"""
zk_home_run.py - 원점복귀(M120) 실행 + 안전 가드

★ 2026-08-06 사고 반영판.
  이전 판은 소프트리밋이 없어서, A3가 리밋 스위치를 못 만난 채 계속 밀려가
  기구 끝을 때렸다("턱턱"). 이제 zk_safety.Guard 가 매 주기 감시하고,
  이동거리 상한을 넘으면 스스로 중단한다.

안전장치
  · 시작 전: X5(급정지), 링크품질 D8001 10회, 관절각 범위 점검
  · 감시 중: X5 / 통신두절 / 축별 이동거리 상한 (0.15초 주기)
  · 전체 타임아웃 자동 해제
  · try/finally 로 어떤 경우에도 M120 강제OFF
"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zkfx import ZK, A_D                                    # noqa: E402
from zk_safety import Guard, Abort, JOINT_D, HOME_FLAG, describe  # noqa: E402
from zkbot2_config import DEFAULT_PORT

# 원점속도 D2002=1000Hz=5.6°/s — 먼 자세(A1 -180° 등)에서는 40~50s 걸린다.
# 25s는 원점 근처 기준이라 8/8 mid 자세에서 A3 탐색 도중 잘렸음 → 90s로 확대.
TIMEOUT = 90.0
# 기존 원점 플래그는 이전 원점복귀 이력일 수 있다. M120을 충분히 유지하고
# 실제 각도 수렴과 리밋 해제를 모두 확인한 뒤에만 완료로 판단한다.
MIN_HOME_RUN_SECONDS = 4.0
HOME_ANGLE_TOLERANCE_DEG = 0.3


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def angles(zk):
    return {j: ang(zk, d) for j, d in JOINT_D.items()}


def fmt(a):
    return "  ".join(f"{j}={'  ----' if a[j] is None else f'{a[j]:8.2f}'}"
                     for j in ("A1", "A2", "A3"))


def main():
    print(describe(), flush=True)
    print(flush=True)

    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    zk = ZK(port)
    if not zk.link():
        sys.exit("링크 실패")

    # --force: 카운터가 검증범위를 크게 벗어났어도 강행한다.
    # 정당한 근거는 단 하나 — 해당 축의 리밋 스위치가 지금 ON이면
    # 카운터가 아무리 틀렸어도 팔의 물리 위치는 확정돼 있다.
    force = "--force" in sys.argv
    guard = Guard(zk, homing=True, force=force)
    aborted = None
    try:
        a0 = angles(zk)
        if force:
            xs0 = zk.x() or set()
            print(f"  [force] 물린 리밋: "
                  f"{[n for i,n in ((0,'X0/A1'),(1,'X1/A2'),(2,'X2/A3')) if i in xs0] or '없음'}",
                  flush=True)
        info = guard.preflight(a0)
        print("── 사전 점검 ──", flush=True)
        print(f"  시작 각도  {fmt(a0)}", flush=True)
        print(f"  원점 확보된 축: {info['homed'] or '없음'}", flush=True)
        print(f"  현재 물린 리밋: {info['limits_on'] or '없음'}", flush=True)
        if info["out_of_range"]:
            print(f"  ⚠ 소프트리밋 밖: {', '.join(info['out_of_range'])}", flush=True)
            print("    (원점 미확보 축은 카운터가 가짜일 수 있음 — "
                  "원점복귀 중엔 이동거리로 판정한다)", flush=True)

        print("\n── M120 원점복귀 시작 ──", flush=True)
        if not zk.m_on(120):
            sys.exit("  M120 강제ON 실패(NAK)")

        prev = None
        t0 = time.time()
        while time.time() - t0 < TIMEOUT:
            a = angles(zk)
            guard.check(a)                       # 위반 시 Abort 발생

            xs = zk.x() or set()
            ms = zk.m(32) or set()
            lim = ",".join(n for i, n in ((0, "X0"), (1, "X1"), (2, "X2"))
                           if i in xs) or "-"
            homed = ",".join(j for j, b in HOME_FLAG.items() if b in ms) or "-"
            line = f"{fmt(a)}  리밋 {lim:<10} 원점 {homed}"
            if line != prev:
                print(f"  {time.time()-t0:5.1f} {line}", flush=True)
                prev = line

            # ★8/7 픽스: 플래그만 보면 안 된다 — 원점이 이미 확보된 상태에서 재실행하면
            # 플래그가 처음부터 ON 이라 0.5초 만에 "완료" 오판, M120 을 끊어 복귀가 중단됨
            # (실증: A2 가 -20.3°에서 멈춤). 판정 = 플래그 AND 각도 수렴(전 축 |a|<1°).
            elapsed = time.time() - t0
            home_complete = (
                elapsed >= MIN_HOME_RUN_SECONDS
                and set(HOME_FLAG.values()) <= ms
                and all(v is not None and abs(v) <= HOME_ANGLE_TOLERANCE_DEG
                        for v in a.values())
                and not any(i in xs for i in (0, 1, 2))
            )
            if home_complete:
                print("\n  ★★ 세 축 모두 원점 확보(최소시간+플래그+각도 수렴+리밋 해제) — 카운터 재동기 완료",
                      flush=True)
                break
            time.sleep(0.15)
        else:
            aborted = f"타임아웃 {TIMEOUT:.0f}초"

    except Abort as e:
        aborted = str(e)
        print(f"\n  ■ 안전 중단: {e}", flush=True)
    finally:
        for _ in range(3):
            if zk.m_off(120):
                break
        print("  M120 해제", flush=True)

        a = angles(zk)
        ms = zk.m(32) or set()
        xs = zk.x() or set()
        print("\n── 최종 ──", flush=True)
        print(f"  각도  {fmt(a)}", flush=True)
        print(f"  원점  " + "  ".join(
            f"{j}={'ON' if b in ms else 'off'}" for j, b in HOME_FLAG.items()), flush=True)
        print(f"  리밋  X0={0 in xs} X1={1 in xs} X2={2 in xs}  "
              f"급정지X5={5 in xs}", flush=True)
        if aborted:
            print(f"  중단 사유: {aborted}", flush=True)
        print(f"\n{zk.report()}", flush=True)
        zk.close()


if __name__ == "__main__":
    main()
