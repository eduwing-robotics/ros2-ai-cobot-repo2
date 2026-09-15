#!/usr/bin/env python3
"""
zk_pose.py - 자세 저장 / 목록 / 재현

자동재생(티칭 플레이백)이 아직 막혀 있으므로, 당장 쓸 수 있는 픽앤플레이스 수단.
관절각 피드백(D1010/D1030/D1050)을 읽으면서 조그 비트로 목표각까지 다가가는
폐루프 방식이다. 원점복귀가 끝나 있어야 각도가 의미를 갖는다.

    python3 zk_pose.py save  <이름>          # 현재 자세 저장
    python3 zk_pose.py list                  # 저장된 자세 목록
    python3 zk_pose.py show                  # 현재 각도만 표시
    python3 zk_pose.py goto  <이름> [--tol 0.5]

조그 비트 (2026-08-06 HMI 실측 캡처)
    A1 정 M1+M101+M181 / 역 M2+M103
    A2 정 M3+M105+M183 / 역 M4+M107
    A3 정 M5+M109+M185 / 역 M6+M111        ← A3 정방향만 패턴 추정
  '정방향'이 카운터를 + 로 움직인다(실측). 다만 축마다 확신이 다르므로
  goto 는 첫 펄스의 부호를 보고 방향이 틀렸으면 스스로 뒤집는다.

안전
  · 조그는 레벨 방식 → 짧은 펄스 후 매번 전 비트 OFF, finally 에서도 OFF
  · 소프트리밋(zk_safety) 위반 / X5 / 통신두절 시 즉시 중단
  · 축당 펄스 횟수 상한
"""
import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zkfx import ZK, A_D                                       # noqa: E402
from zk_safety import Guard, Abort, JOINT_D, HOME_FLAG, JOINT_LIMITS  # noqa: E402
from zkbot2_config import DATA_DIR, DEFAULT_PORT

POSE_FILE = str(DATA_DIR / "poses.json")
# 축: (정방향 비트, 역방향 비트) — 2026-08-06 여섯 조합 전부 실증 완료
#   정방향 = 카운터가 + 로 가는 쪽. HMI 캡처 + 실제 대각도 이동으로 확인.
BITS = {
    # 2호기 HMI 실측(2026-08-08): 양수 M1+M31+M101, 음수 M2+M103.
    "A1": ((1, 31, 101), (2, 103)),
    # 2호기 HMI 실측(2026-08-08): 양수 M3+M41+M105, 음수 M4+M107.
    "A2": ((3, 41, 105), (4, 107)),
    # 2호기 HMI 실측(2026-08-08): 양수 M5+M51+M109, 음수 M6+M111.
    "A3": ((5, 51, 109), (6, 111)),
}
ALL_JOG = tuple(range(1, 13)) + (31, 33, 41, 43, 51, 53) \
    + tuple(range(101, 113)) + tuple(range(181, 187))
ORDER = ("A1", "A2", "A3")
MAX_PULSES = 60


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def angles(zk):
    return {j: ang(zk, d) for j, d in JOINT_D.items()}


def all_off(zk):
    for _ in range(2):
        for b in ALL_JOG:
            zk.m_off(b)


def load():
    if os.path.exists(POSE_FILE):
        return json.load(open(POSE_FILE))
    return {}


def save_db(db):
    os.makedirs(os.path.dirname(POSE_FILE), exist_ok=True)
    json.dump(db, open(POSE_FILE, "w"), indent=2, ensure_ascii=False)


def fmt(a):
    return "  ".join(f"{j}={'  ----' if a[j] is None else f'{a[j]:9.3f}°'}"
                     for j in ORDER)


def pulse(zk, bits, sec):
    """★ 켠 비트만 끈다. 매번 전체(26개×2)를 끄면 펄스당 1초를 낭비한다.
       (실제로 그 때문에 goto 가 타임아웃났다. 전체 해제는 finally 에서만.)"""
    for b in bits:
        zk.m_on(b)
    t = time.time()
    while time.time() - t < sec:
        time.sleep(0.03)
    for _ in range(2):                 # 확실히 끄되 켠 것만
        for b in bits:
            zk.m_off(b)
    time.sleep(0.08)


def set_speed(zk, pct):
    """D50 은 32bit float(%). 정수를 쓰면 float 해석으로 사실상 0이 된다."""
    lo, hi = struct.unpack("<HH", struct.pack("<f", float(pct)))
    zk.set_d(50, lo)
    zk.set_d(51, hi)
    time.sleep(0.3)
    got = struct.unpack("<f", struct.pack("<HH", zk.d(50), zk.d(51)))[0]
    return got


def goto_axis(zk, guard, joint, target, tol, phases=None):
    """한 축을 목표각까지 — 연속 제어.

    ★ 끊어 치는 펄스 제어는 실패한다. 비트를 끈 뒤에도 감속하며 더 가기 때문에
      '최소 이동 단위'가 생기고, 그게 허용오차보다 크면 영원히 왕복한다
      (2026-08-06 실증: A2가 -21°~-26° 사이를 60회 진동).
      → 조그를 켜둔 채 각도를 계속 읽다가, 남은 거리가 '감속 여유(lead)'에
        닿으면 끈다. 빠르게 접근한 뒤 느린 속도로 한 번 더 다듬는다.
    """
    d = JOINT_D[joint]
    fwd, rev = BITS[joint]
    lo, hi = JOINT_LIMITS[joint]
    if not (lo <= target <= hi):
        raise Abort(f"{joint} 목표 {target:.2f}도가 소프트리밋({lo}~{hi}) 밖")

    # (속도%, 감속 여유) — 여유는 실측 오버슛 기준: 30%에서 약 2.5도, 8%에서 0.7도
    for pct, lead in (phases or ((30.0, 2.5), (8.0, 0.6))):
        cur = ang(zk, d)
        if cur is None:
            raise Abort(f"{joint} 각도 읽기 실패")
        err = target - cur
        if abs(err) <= tol:
            break
        if abs(err) <= lead:          # 이 속도로는 더 다가갈 수 없다
            continue

        set_speed(zk, pct)
        time.sleep(0.3)
        dps = max(1.0, (zk.d(2002) or 1000) / 177.78)
        bits = fwd if err > 0 else rev
        print(f"    [{joint}] {cur:8.2f} -> {target:8.2f}  ({err:+.2f}도)  "
              f"{pct:.0f}% = {dps:.1f}도/s  여유 {lead}도", flush=True)

        for b in bits:
            zk.m_on(b)
        t0 = time.time()
        limit_s = abs(err) / dps + 4.0
        try:
            while time.time() - t0 < limit_s:
                v = ang(zk, d)
                if v is None:
                    continue
                xs = zk.x() or set()
                if 5 in xs:
                    raise Abort("X5(급정지) 감지")
                if not (lo - 2 <= v <= hi + 2):
                    raise Abort(f"{joint} 소프트리밋 이탈 {v:.2f}도")
                if abs(target - v) <= lead or (target - v > 0) != (err > 0):
                    break
        finally:
            for _ in range(2):
                for b in bits:
                    zk.m_off(b)
        time.sleep(0.5)
        v = ang(zk, d)
        print(f"      정지 {v:8.2f}도  오차 {target-v:+.2f}도", flush=True)
        guard.check(angles(zk))

    cur = ang(zk, d)
    print(f"    -> {joint} 최종 {cur:8.2f}도  (목표 {target:.2f}, "
          f"오차 {target-cur:+.2f}도)", flush=True)
    return True


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    db = load()

    if cmd == "list":
        if not db:
            print("저장된 자세 없음")
            return
        for k, v in db.items():
            print(f"  {k:<16} " + "  ".join(f"{j}={v[j]:9.3f}°" for j in ORDER)
                  + f"   ({v.get('saved','')})")
        return

    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    zk = ZK(port)
    if not zk.link():
        sys.exit("링크 실패")
    try:
        a = angles(zk)
        if cmd == "show":
            print(f"현재 자세  {fmt(a)}")
            return

        if cmd == "save":
            name = sys.argv[2]
            ms = zk.m(32) or set()
            missing = [j for j, b in HOME_FLAG.items() if b not in ms]
            if missing:
                sys.exit(f"■ 저장 거부 — 원점 미확보: {missing}\n"
                         f"  원점 없이 저장한 각도는 나중에 재현할 수 없습니다.")
            db[name] = {j: round(a[j], 3) for j in ORDER}
            db[name]["saved"] = time.strftime("%Y-%m-%d %H:%M")
            save_db(db)
            print(f"저장됨: {name}")
            print(f"  {fmt(a)}")
            print(f"  → {POSE_FILE}")
            return

        if cmd == "goto-axis":
            if len(sys.argv) < 4:
                sys.exit("사용법: zk_pose.py goto-axis <A1|A2|A3> <목표각도> [--tol 0.5]")
            joint = sys.argv[2].upper()
            if joint not in ORDER:
                sys.exit(f"축은 {ORDER} 중 하나")
            target = float(sys.argv[3])
            tol = 0.5
            if "--tol" in sys.argv:
                tol = float(sys.argv[sys.argv.index("--tol") + 1])

            ms = zk.m(32) or set()
            missing = [j for j, b in HOME_FLAG.items() if b not in ms]
            if missing:
                sys.exit(f"■ 이동 거부 — 원점 미확보: {missing}")

            guard = Guard(zk, homing=False)
            guard.preflight(a)
            print(f"현재  {fmt(a)}")
            print(f"단일축 목표  {joint}={target:.3f}°  허용오차 {tol}°")
            all_off(zk)
            # 저속 접근 뒤 미세 속도로 한 번 더 다듬어 역방향 오버슈트를 줄인다.
            goto_axis(zk, guard, joint, target, tol, phases=((8.0, 0.6), (2.0, 0.18)))
            print(f"\n★ 단일축 도달 완료  {fmt(angles(zk))}")
            return

        if cmd == "goto":
            name = sys.argv[2]
            tol = 0.5
            if "--tol" in sys.argv:
                tol = float(sys.argv[sys.argv.index("--tol") + 1])
            if name not in db:
                sys.exit(f"'{name}' 없음. 목록: {list(db)}")
            tgt = db[name]
            ms = zk.m(32) or set()
            missing = [j for j, b in HOME_FLAG.items() if b not in ms]
            if missing:
                sys.exit(f"■ 이동 거부 — 원점 미확보: {missing}")

            spd = 30.0
            if "--speed" in sys.argv:
                spd = float(sys.argv[sys.argv.index("--speed") + 1])

            guard = Guard(zk, homing=False)
            guard.preflight(a)
            got = set_speed(zk, spd)
            time.sleep(0.4)
            hz = zk.d(2002) or 1000
            dps = max(1.0, hz / 177.78)
            print(f"현재  {fmt(a)}")
            print(f"목표  " + "  ".join(f"{j}={tgt[j]:9.3f}°" for j in ORDER))
            print(f"속도  D50={got}%  →  D2002={hz}Hz = {dps:.1f}°/s"
                  f"   허용오차 {tol}°\n")
            all_off(zk)
            for j in ORDER:          # A1 → A2 → A3 순서로 하나씩
                print(f"  ── {j} ──", flush=True)
                goto_axis(zk, guard, j, tgt[j], tol)
            print(f"\n★ 도달 완료  {fmt(angles(zk))}")
            return

        print(__doc__)
    except Abort as e:
        print(f"\n■ 안전 중단: {e}")
    finally:
        all_off(zk)
        ms = zk.m(32) or set()
        stuck = [b for b in ALL_JOG if b in ms]
        if stuck:
            print(f"🔴 조그 비트 잔류: {stuck}")
        zk.close()


if __name__ == "__main__":
    main()
