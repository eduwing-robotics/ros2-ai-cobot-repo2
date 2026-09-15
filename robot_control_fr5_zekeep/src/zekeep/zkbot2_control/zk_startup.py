#!/usr/bin/env python3
"""
zk_startup.py - 아침 기동 루틴 (전원 재투입 후 이것 하나만 실행)

전원을 끄면 매번 같은 일이 벌어진다:
  · 모터가 무여자가 되어 팔이 중력으로 처진다 → 카운터와 실제 위치가 어긋남 → 원점 소실
  · FX3U는 D0~D199 가 비유지 영역이라 **D76(크리프속도)이 0으로 초기화된다**
    → 원점복귀가 리밋을 만난 뒤 감속하지 못해 "털털" 거린다
    (2026-08-06 아침에 정확히 이 상태였다)

이 스크립트가 그 둘을 자동으로 처리한다.

    python3 zk_startup.py           # 점검 + 복구 + 원점복귀
    python3 zk_startup.py --check   # 완전 읽기 전용 점검
"""
import struct
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from zkfx import ZK, A_D, PUMP, VALVE                   # noqa: E402
from zk_safety import JOINT_D, HOME_FLAG, JOINT_LIMITS  # noqa: E402
from zkbot2_config import DEFAULT_PORT

CREEP_D, CREEP_V = 76, 200          # 크리프속도 — 비유지 영역이라 매번 다시 넣는다
PY = sys.executable
CHECK = "--check" in sys.argv
PORT = (sys.argv[sys.argv.index("--port") + 1]
        if "--port" in sys.argv else DEFAULT_PORT)


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def run(script, *args):
    print(f"\n  $ {script} {' '.join(args)}", flush=True)
    r = subprocess.run([PY, str(BASE_DIR / script)] + list(args) + ["--port", PORT],
                       capture_output=True, text=True)
    tail = [l for l in r.stdout.splitlines() if l.strip()][-6:]
    for l in tail:
        print(f"    {l}", flush=True)
    return r.returncode == 0


def main():
    print("=" * 60)
    print("ZKBOT 기동 점검")
    print("=" * 60)

    try:
        zk = ZK(PORT)
    except Exception as e:
        sys.exit(f"■ 포트를 못 엽니다: {e}\n"
                 f"  → sudo chmod 666 {PORT}\n"
                 f"  → 어댑터 확인: lsusb | grep -i 1a86")
    if not zk.link():
        sys.exit("■ PLC 링크 실패 — 전원과 DB9 커넥터를 확인하세요")

    # 1) 통신 품질
    good = sum(1 for _ in range(10)
               if (b := zk.read(0x0E00 + 2, 2))
               and int.from_bytes(b, "little") == 24320)
    print(f"\n[1] 통신 품질   D8001 {good}/10  "
          f"{'✅' if good >= 9 else '🔴 커넥터 확인 필요'}")
    if good < 9:
        sys.exit("■ 링크 불안정 — 진행 중단")

    # 2) 급정지 / 리밋
    xs = zk.x() or set()
    print(f"[2] 급정지 X5   {'🔴 눌려 있음 — 푸세요' if 5 in xs else '✅ 해제'}")
    lims = [n for i, n in ((0, "A1"), (1, "A2"), (2, "A3")) if i in xs]
    print(f"    리밋 물림   {lims if lims else '✅ 없음'}")

    # 3) 크리프속도 — ★ 전원 OFF 로 날아가는 값
    cur = zk.d(CREEP_D)
    print(f"[3] 크리프속도  D{CREEP_D} = {cur}", end="")
    if cur == CREEP_V:
        print("  ✅ 이미 설정됨")
    else:
        if CHECK:
            print(f"  🔴 {CREEP_V} 이어야 함 (전원 OFF 로 초기화됨)")
        else:
            zk.set_d(CREEP_D, CREEP_V)
            time.sleep(0.3)
            ok = zk.d(CREEP_D) == CREEP_V
            print(f"  → {CREEP_V} 로 복구 {'✅' if ok else '🔴 실패'}")

    # 4) 모드 / 원점 / 각도
    print(f"[4] 모드        D90 = {zk.d(90)} "
          f"({ {0:'정지',1:'수동',2:'자동'}.get(zk.d(90),'?') })")
    ms = zk.m(32) or set()
    homed = [j for j, b in HOME_FLAG.items() if b in ms]
    print(f"[5] 원점 확보   {homed if homed else '🔴 없음'}")
    print("[6] 관절 각도")
    oor = []
    for j, d in JOINT_D.items():
        v = ang(zk, d)
        lo, hi = JOINT_LIMITS[j]
        bad = v is None or not (lo <= v <= hi)
        if bad:
            oor.append(j)
        print(f"      {j}  {v:9.2f}°   {'🔴 범위 밖' if bad else '✅'}")

    # 7) 출력 정리
    ys = zk.y() or set()
    if PUMP in ys or VALVE in ys:
        if CHECK:
            print("[7] 펌프/밸브가 켜져 있음 (점검 모드에서는 출력 변경 안 함)")
        else:
            print("[7] 펌프/밸브가 켜져 있음 → OFF")
            zk.y_off(PUMP)
            zk.y_off(VALVE)
    else:
        print("[7] 펌프/밸브   ✅ OFF")

    # M121~M123은 과거 원점복귀 이력일 수 있으므로 현재 각도도 함께 본다.
    actual_home = all(
        v is not None and abs(v) <= 0.3
        for v in (ang(zk, d) for d in JOINT_D.values())
    )
    need_home = len(homed) < 3 or not actual_home
    zk.close()

    print("\n" + "=" * 60)
    if CHECK:
        print("점검만 수행했습니다 (--check). 실제 복구는 인자 없이 실행하세요.")
        return
    if 5 in xs:
        print("■ 급정지가 눌려 있어 진행할 수 없습니다.")
        return
    if not need_home and not oor:
        print("✅ 이미 준비된 상태입니다. 바로 작업 가능합니다.")
        return

    print("원점복귀를 진행합니다")
    print("=" * 60)
    for j, i in (("A1", 0), ("A2", 1), ("A3", 2)):
        if i in xs:
            print(f"\n· {j} 가 리밋에 얹혀 있음 → 먼저 빼냅니다")
            run("zk_escape_limit.py", j)

    print("\n· 원점복귀")
    run("zk_home_run.py")

    zk = ZK(PORT)
    zk.link()
    ms = zk.m(32) or set()
    xs = zk.x() or set()
    homed = [j for j, b in HOME_FLAG.items() if b in ms]
    final_angles = {j: ang(zk, d) for j, d in JOINT_D.items()}
    final_ready = (
        len(homed) == 3
        and all(v is not None and abs(v) <= 0.3 for v in final_angles.values())
        and not any(i in xs for i in (0, 1, 2))
    )
    print("\n" + "=" * 60)
    if final_ready:
        print("✅ 준비 완료 — 세 축 실제 각도 0 수렴 및 리밋 해제 확인")
        print("   다음: python3 zk_pose.py show")
    else:
        print("🔴 원점 최종 확인 실패")
        print(f"   플래그: {homed}, 각도: {final_angles}, 리밋: {sorted(xs)}")
        print("   → 리밋이 ON이면 먼저 zk_escape_limit.py <축>을 실행하세요.")
        print("   → 리밋 탐색은 실제 위치를 눈으로 확인한 뒤에만 수행하세요.")
    print("=" * 60)
    zk.close()


if __name__ == "__main__":
    main()
