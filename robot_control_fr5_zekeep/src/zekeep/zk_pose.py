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

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D                                       # noqa: E402
from zk_safety import Guard, Abort, JOINT_D, HOME_FLAG, JOINT_LIMITS  # noqa: E402
import zk_profiles as ZPROF                                    # noqa: E402

# ★ 2026-08-10 C1: 포트·조그비트·자세DB는 zk_profiles 가 단일 출처다.
#   개체마다 조그비트가 다르므로(2호기는 M31/M41/M51 계열) 하드코딩하면
#   엉뚱한 로봇·엉뚱한 축이 움직인다. 아래 값은 기본 프로파일(zkbot1)의
#   초기값일 뿐이고, apply_profile() 로 교체해서 쓴다.
_PROFILE = ZPROF.get(ZPROF.DEFAULT_ROBOT)
POSE_FILE = _PROFILE.pose_file
BITS = _PROFILE.bits
ALL_JOG = ZPROF.ALL_JOG
ORDER = ZPROF.ORDER
MAX_PULSES = 60


def apply_profile(prof):
    """이 모듈이 쓸 로봇을 바꾼다 (조그비트 + 자세DB).

    프로세스 하나가 로봇 한 대를 맡는 전제다. 한 프로세스에서 두 대를
    번갈아 다루려면 goto_axis(..., bits=...) 로 명시해 넘길 것.
    """
    global _PROFILE, POSE_FILE, BITS
    _PROFILE = prof
    POSE_FILE = prof.pose_file
    BITS = prof.bits
    return prof


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


# 실측 피팅 모델 (zk_zline/zk_vlift 와 동일 상수, 2026-08-12 역산).
# ★zk_kin.py(URDF)는 검증 실패 — 이 상수 쌍만 신뢰한다 (r=226.3/246.2 재현 확인).
_L = 200.0
_Q20, _Q30 = 7.741, -18.932

def rz_of(a2, a3):
    """관절각 → (수평거리 r, 높이 z) [mm]. A1 회전면 내."""
    import math
    q2, q3 = math.radians(_Q20 - a2), math.radians(_Q30 - a3)
    return (_L * math.sin(q2) + _L * math.cos(q3),
            _L * math.cos(q2) - _L * math.sin(q3))


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
    """D50 은 32bit float(%). 정수를 쓰면 float 해석으로 사실상 0이 된다.

    ★2026-08-12 사이클타임: **이미 같은 값이면 쓰지 않고 바로 돌아온다.**
      carry 는 같은 속도로 수십~수백 스텝을 도는데 매번 쓰기 2회 + 0.3초 대기가
      순수 낭비였다(호출당 0.6초). 값 비교는 읽기 2회(수십 ms)면 끝난다.
      쓰기를 건너뛰어도 PLC 의 D50 값 자체는 그대로이므로 물리 동작은 동일하다.
    """
    want = float(pct)
    cur = struct.unpack("<f", struct.pack("<HH", zk.d(50) or 0, zk.d(51) or 0))[0]
    if abs(cur - want) < 0.01:
        return cur                       # 이미 그 속도 — 쓰기·대기 생략
    lo, hi = struct.unpack("<HH", struct.pack("<f", want))
    zk.set_d(50, lo)
    zk.set_d(51, hi)
    time.sleep(0.3)
    got = struct.unpack("<f", struct.pack("<HH", zk.d(50), zk.d(51)))[0]
    return got


def settle(zk, d, timeout=0.8, quiet=0.01):
    """조그를 끈 뒤 **각도가 실제로 멈출 때까지만** 기다린다 (고정 0.5초 대체).

    ★고정 대기를 그냥 지우면 안 된다 — 감속 중에 각도를 읽어 그 값으로 다음
      스텝을 계산하면 보정이 어긋난다. 연속 두 읽기 차가 quiet 미만이면 정지로
      본다. 대부분 0.2~0.3초에 끝나 스텝당 0.2~0.3초를 회수한다.
    """
    t0 = time.time()
    prev = ang(zk, d)
    while time.time() - t0 < timeout:
        v = ang(zk, d)
        if v is not None and prev is not None and abs(v - prev) < quiet:
            return v
        prev = v
    return prev


def goto_two(zk, guard, tgts, lead=0.6, pct=8.0, bits=None):
    """★2026-08-21 ③적용: A2·A3 를 **동시에** 목표로 보낸다 (부품 회전 대책).

    왜: goto_axis 를 축마다 순차로 부르면 웨이포인트마다 가속-정지가 두 번씩
    일어나 "팍팍" 거리고, 그 덜컹임이 흡착판 위 부품을 회전시킨다(점접촉이라
    yaw 저항이 마찰뿐). 두 축을 같이 켜고 각자 lead 에 닿는 순간 각자 끄면
    덜컹임 횟수가 절반이 되고 궤적도 대각선(직선에 가깝게)이 된다.

    goto_axis 와 같은 레벨-제어 원칙(끊어치기 금지). 미세 수렴은 호출측에서
    기존 goto_axis 2% 단계로 마무리한다."""
    B = bits or BITS
    plan = {}
    for j, target in tgts.items():
        d = JOINT_D[j]
        lo, hi = JOINT_LIMITS[j]
        if not (lo <= target <= hi):
            raise Abort(f"{j} 목표 {target:.2f}도가 소프트리밋({lo}~{hi}) 밖")
        cur = ang(zk, d)
        if cur is None:
            raise Abort(f"{j} 각도 읽기 실패")
        err = target - cur
        if abs(err) > lead:
            fwd, rev = B[j]
            plan[j] = (d, target, err, fwd if err > 0 else rev, lo, hi)
    if not plan:
        return
    set_speed(zk, pct)
    dps = max(1.0, (zk.d(2002) or 1000) / 177.78)
    for j, (_, tg, err, b, _, _) in plan.items():
        print(f"    [{j}동시] -> {tg:8.2f} ({err:+.2f}도)", flush=True)
        for bb in b:
            zk.m_on(bb)
    t0 = time.time()
    limit_s = max(abs(v[2]) for v in plan.values()) / dps + 4.0
    live = set(plan)
    try:
        while live and time.time() - t0 < limit_s:
            xs = zk.x() or set()
            if 5 in xs:
                raise Abort("X5(급정지) 감지")
            for j in list(live):
                d, tg, err, b, lo, hi = plan[j]
                v = ang(zk, d)
                if v is None:
                    continue
                if not (lo - 2 <= v <= hi + 2):
                    raise Abort(f"{j} 소프트리밋 이탈 {v:.2f}도")
                if abs(tg - v) <= lead or (tg - v > 0) != (err > 0):
                    for bb in b:
                        zk.m_off(bb)
                    live.discard(j)
    finally:
        for j, (_, _, _, b, _, _) in plan.items():
            for bb in b:
                zk.m_off(bb)
        time.sleep(0.1)


def goto_axis(zk, guard, joint, target, tol, phases=None, bits=None):
    """한 축을 목표각까지 — 연속 제어.

    bits=None 이면 현재 프로파일(apply_profile)의 조그비트를 쓴다.
    한 프로세스가 두 로봇을 다룰 때만 명시적으로 넘길 것.

    ★ 끊어 치는 펄스 제어는 실패한다. 비트를 끈 뒤에도 감속하며 더 가기 때문에
      '최소 이동 단위'가 생기고, 그게 허용오차보다 크면 영원히 왕복한다
      (2026-08-06 실증: A2가 -21°~-26° 사이를 60회 진동).
      → 조그를 켜둔 채 각도를 계속 읽다가, 남은 거리가 '감속 여유(lead)'에
        닿으면 끈다. 빠르게 접근한 뒤 느린 속도로 한 번 더 다듬는다.
    """
    d = JOINT_D[joint]
    fwd, rev = (bits or BITS)[joint]
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

        set_speed(zk, pct)              # 같은 속도면 즉시 반환(쓰기·대기 없음)
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
        v = settle(zk, d)               # 고정 0.5초 → 실제 정지 확인까지만
        print(f"      정지 {v:8.2f}도  오차 {target-v:+.2f}도", flush=True)
        guard.check(angles(zk))

    cur = ang(zk, d)
    print(f"    -> {joint} 최종 {cur:8.2f}도  (목표 {target:.2f}, "
          f"오차 {target-cur:+.2f}도)", flush=True)
    return True


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    prof = apply_profile(ZPROF.resolve(argv=sys.argv))
    db = load()

    if cmd == "list":
        if not db:
            print("저장된 자세 없음")
            return
        for k, v in db.items():
            print(f"  {k:<16} " + "  ".join(f"{j}={v[j]:9.3f}°" for j in ORDER)
                  + f"   ({v.get('saved','')})")
        return

    # ★ 어느 로봇을 잡았는지 항상 밝힌다 — 두 대가 붙어 있을 때
    #   눈으로 확인하지 않으면 엉뚱한 로봇을 움직이게 된다.
    print(f"[{prof.name}] {prof.port}", flush=True)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name} {prof.port})")
    try:
        a = angles(zk)
        if cmd == "show":
            print(f"현재 자세  {fmt(a)}")
            # ★ 각도만 보면 안 된다 — 원점이 풀리면 카운터가 가짜값을 낸다.
            #   2026-08-10 실제로 A1 이 -301.9°(소프트리밋 -190 밖)로 읽혔고,
            #   원점 플래그가 전부 off 인 상태였다. 그 값을 저장할 뻔했다.
            ms = zk.m(32) or set()
            homed = {j: (b in ms) for j, b in HOME_FLAG.items()}
            mark = "  ".join(f"{j}={'ON' if v else 'off'}" for j, v in homed.items())
            if all(homed.values()):
                print(f"원점 확보  {mark}")
            else:
                print(f"🔴 원점 미확보  {mark}")
                print("   → 위 각도는 신뢰할 수 없습니다. "
                      "zk_home_run.py --robot <이름> [--force] 로 재동기하세요.")
            return

        if cmd == "save":
            name = sys.argv[2]
            ms = zk.m(32) or set()
            missing = [j for j, b in HOME_FLAG.items() if b not in ms]
            if missing:
                sys.exit(f"■ 저장 거부 — 원점 미확보: {missing}\n"
                         f"  원점 없이 저장한 각도는 나중에 재현할 수 없습니다.")
            db[name] = {j: round(a[j], 3) for j in ORDER}
            # ★2026-08-21: (r,z) 를 함께 기록한다 — 배치 변경 시 "z 만 5mm 낮추기" 같은
            #   일괄 보정과, 반경별 수직 여유 판단의 근거가 된다. 재현에는 안 쓰인다(참고값).
            _r, _z = rz_of(a["A2"], a["A3"])
            db[name]["r_mm"], db[name]["z_mm"] = round(_r, 1), round(_z, 1)
            db[name]["saved"] = time.strftime("%Y-%m-%d %H:%M")
            save_db(db)
            print(f"저장됨: {name}")
            print(f"  {fmt(a)}")
            print(f"  → {POSE_FILE}")
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
            # ★ 2026-08-10 픽스: --speed 가 무시되던 버그.
            #   goto_axis 의 기본 phases((30,2.5),(8,0.6)) 가 set_speed 로 속도를
            #   덮어써서, --speed 5 를 줘도 실제로는 30% 로 움직였다(실측 로그로 확인).
            #   → 지정 속도를 1단계로, 그 30%(최소 3%)를 다듬기 단계로 만든다.
            #   감속 여유(lead)도 속도에 비례시킨다(30%에서 2.5° 실측 기준).
            slow = max(spd * 0.3, 3.0)
            phases = ((spd, max(spd * 0.083, 0.25)),
                      (slow, max(slow * 0.083, 0.2)))

            # ★ 축 이동 순서 지정 (2026-08-10 추가)
            #   기본은 A1→A2→A3. 부품을 들고 이동할 때는 **먼저 들고 나서 돌아야**
            #   부품이 바닥이나 주변을 쓸지 않는다 → --order A3,A2,A1
            #   반대로 목적지에서 내려놓을 때는 돌고 나서 내리는 기본 순서가 맞다.
            order = ORDER
            if "--order" in sys.argv:
                order = tuple(a.strip().upper()
                              for a in sys.argv[sys.argv.index("--order") + 1].split(","))
                if sorted(order) != sorted(ORDER):
                    sys.exit(f"■ --order 는 A1,A2,A3 를 모두 포함해야 합니다 — 받은 값 {order}")
            print(f"축 이동 순서: {' → '.join(order)}\n", flush=True)

            for j in order:
                print(f"  ── {j} ──", flush=True)
                goto_axis(zk, guard, j, tgt[j], tol, phases)
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
