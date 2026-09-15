#!/usr/bin/env python3
"""
zk_carry.py - 정밀 운반: base_pick → (정밀 상승) base_lift → (A1 회전) → (정밀 하강) base_place

    zk_carry.py --robot zkbot1                        # 티칭 자세 기본값
    zk_carry.py --robot zkbot1 --speed 5 --rot-speed 10 --slow-deg 5 --slow-step 0.5 --step 2.0

셀 파이프라인 MOVE#2 (부품 파지 구간) 전용. 펌프는 건드리지 않는다
(SUCTION/RELEASE 는 오케스트레이터가 별도 phase 로 수행).

★ zk_lift.py 의 확정 기법을 자세 간 이동으로 일반화 (2026-08-10 실측 근거):
  · A2·A3 교대 이동 — 두 호가 상쇄되어 팔끝이 수직에 가깝게 움직인다
  · 2단계 스텝 — 부품이 취약한 구간(상승 초기·하강 말기)만 slow_step 으로
  · 누적 목표 방식 — 오버슛으로 스텝을 건너뛰어도 비율이 안 무너진다
  · 지배축(이동량 큰 축) 기준 스텝, 나머지 축은 비율 추종

안전: Guard 가 소프트리밋·X5·통신두절을 매 스텝 감시. 시작 자세가 pick 과
2° 이상 어긋나면 경고 후 진행(현재 각도 기준 누적이므로 동작 자체는 안전).
"""
import json
import sys

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort                     # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402

MIN_STEP_DEG = 0.3
MAX_SEG_DEG = 95.0          # 자세 간 단일 구간 상한 (하강 ~71° 를 허용)


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def alt_move(zk, guard, cur, tgt, slow_zone, step, slow_step, slow_deg, tol, phases,
             fast_phases=None):
    """A2·A3 교대로 cur→tgt 이동. slow_zone='start'|'end' 구간만 slow_step.

    fast_phases 를 주면 **취약구간 밖(큰 스텝 구간)만** 그 속도로 달린다.
    2026-08-12: 취약구간은 흡착 직후 5°·착지 마지막 10° 뿐이고 나머지는 공중
    이동이라 느릴 이유가 없다(사이클타임 = 시간당 생산량).
    ★취약구간에 빠른 phases 를 쓰면 안 된다 — 관성(통신지연 150ms×속도)이
    slow_step 0.5° 보다 커져 스텝이 의미를 잃는다."""
    d2, d3 = tgt["A2"] - cur["A2"], tgt["A3"] - cur["A3"]
    dom = "A3" if abs(d3) >= abs(d2) else "A2"          # 지배축
    sub = "A2" if dom == "A3" else "A3"
    d_dom = {"A2": d2, "A3": d3}[dom]
    d_sub = {"A2": d2, "A3": d3}[sub]
    total = abs(d_dom)
    if total < 1e-3:
        return
    if total > MAX_SEG_DEG:
        raise Abort(f"구간 이동량 {total:.1f}° 상한({MAX_SEG_DEG}) 초과")
    sign = 1.0 if d_dom > 0 else -1.0
    ratio = d_sub / d_dom                                # 부호 포함 비율
    sd = min(slow_deg, total)
    if sd <= 0:
        plan = [(total, step)]
    elif slow_zone == "start":
        plan = [(sd, slow_step), (total - sd, step)]
    else:
        plan = [(total - sd, step), (sd, slow_step)]
    plan = [(d, s) for d, s in plan if d > 1e-6]
    desc = " + ".join(f"{d:.1f}°@{s}°" for d, s in plan)
    print(f"    교대이동 {dom}기준 {sign*total:+.1f}° [{desc}] "
          f"{sub}비율 {ratio:+.3f}", flush=True)

    base_dom, base_sub = cur[dom], cur[sub]              # ★누적 목표 기준점
    done = 0.0
    for seg_deg, seg_step in plan:
        # 큰 스텝 구간 = 공중 이동 → fast_phases (있으면). slow_step 구간은 항상 저속.
        seg_phases = fast_phases if (fast_phases and seg_step > slow_step) else phases
        n = max(1, int(round(seg_deg / seg_step)))
        for i in range(n):
            adv = done + (seg_deg if i == n - 1 else seg_step * (i + 1))
            t_dom = base_dom + sign * adv
            t_sub = base_sub + sign * adv * ratio
            P.goto_axis(zk, guard, sub, t_sub, tol, seg_phases)  # A2 먼저 순서 유지:
            P.goto_axis(zk, guard, dom, t_dom, tol, seg_phases)  # dom=A3 이면 sub=A2 선행
        done += seg_deg
    # 구간 끝 정밀 마무리
    P.goto_axis(zk, guard, "A2", tgt["A2"], tol, phases)
    P.goto_axis(zk, guard, "A3", tgt["A3"], tol, phases)


def main():
    pick_n = arg("--pick", "base_pick")
    lift_n = arg("--lift", "base_lift")
    place_n = arg("--place", "base_place")
    speed = float(arg("--speed", "5"))
    # ★2026-08-12 3회차 실패로 기본값 원복: 공중 15%·회전 20%·스텝 3.0 으로 올렸더니
    #   로봇 각도는 tol 안(A1 -0.011°)이었는데도 삽입 실패 = **부품이 흡착판 위에서
    #   미끄러졌다**(진공 파지는 마찰로만 회전을 버틴다). 속도는 옵트인으로만 쓴다.
    fast_speed = float(arg("--fast-speed", "5"))    # 공중(취약구간 밖) 이동 속도
    rot_speed = float(arg("--rot-speed", "10"))     # A1 회전 (10 초과 금지 — 미끄러짐)
    step = float(arg("--step", "2.0"))              # 공중 스텝
    # ★상승만 다르게 (사용자 지시 8/12): 흡착 직후 5° 는 정밀, **그 다음은 잘게
    #   왔다갔다 하지 말고 한 번에** 올린다. 상승은 부품이 흡착판에 눌리는 방향이라
    #   회전·하강보다 미끄러짐 위험이 작다. 회전·하강은 원래(저속·스텝 분할) 유지.
    up_speed = float(arg("--up-speed", "15"))       # 5° 이후 상승 속도
    up_step = float(arg("--up-step", "999"))        # 999 = 분할 없이 단일 이동
    slow_step = float(arg("--slow-step", "0.5"))
    slow_deg = float(arg("--slow-deg", "5"))
    tol = float(arg("--tol", "0.15"))
    if step < MIN_STEP_DEG or slow_step < MIN_STEP_DEG:
        sys.exit(f"■ 스텝은 {MIN_STEP_DEG}° 이상 (진동 방지)")

    prof = P.apply_profile(ZPROF.resolve_from_argv())
    try:
        with open(prof.pose_file) as f:
            poses = json.load(f)
    except FileNotFoundError:
        sys.exit(f"■ 자세DB 없음: {prof.pose_file}")
    for n in (pick_n, lift_n, place_n):
        if n not in poses:
            sys.exit(f"■ 티칭 자세 '{n}' 없음 — zk_pose.py save 필요")
    pick, lift, place = poses[pick_n], poses[lift_n], poses[place_n]

    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")

    lead = max(speed * 0.083, 0.2)
    phases = ((speed, lead), (speed, max(lead * 0.4, 0.15)))
    # 공중 이동용: lead(감속여유) = 속도×0.083 실측식. 마무리 phase 는 저속으로
    # 떨어뜨려 큰 스텝에서도 목표를 지나치지 않게 한다.
    fast_lead = max(fast_speed * 0.083, 0.3)
    fast_phases = ((fast_speed, fast_lead), (speed, max(lead * 0.4, 0.15)))
    up_lead = max(up_speed * 0.083, 0.3)
    up_phases = ((up_speed, up_lead), (speed, max(lead * 0.4, 0.15)))
    rot_lead = max(rot_speed * 0.083, 0.3)
    rot_phases = ((rot_speed, rot_lead), (8, 0.6))

    try:
        a0 = P.angles(zk)
        print(f"  시작 자세  {P.fmt(a0)}", flush=True)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)
        off = max(abs(a0[j] - pick[j]) for j in ("A1", "A2", "A3"))
        if off > 2.0:
            print(f"  ⚠ 시작 자세가 '{pick_n}' 과 {off:.1f}° 어긋남 — 현재 위치 기준 진행",
                  flush=True)

        print(f"\n  ── ① 상승 {pick_n}→{lift_n} (초기 {slow_deg}° 잘게 → 이후 "
              f"{up_speed:g}% 단일 이동) ──", flush=True)
        alt_move(zk, guard, {j: a0[j] for j in ("A2", "A3")}, lift,
                 "start", up_step, slow_step, slow_deg, tol, phases, up_phases)

        print(f"\n  ── ② A1 회전 → {place_n} ({rot_speed}%) ──", flush=True)
        P.goto_axis(zk, guard, "A1", place["A1"], tol, rot_phases)

        # ③ 하강 — 2026-08-11 개선(3연속 뒤틀림 실패 대응):
        #   공중 구간(마지막 10° 전까지)만 큰 스텝, 마지막 10° 는 전부 0.5°.
        #   하강 전·마지막 10° 진입 직전에 A1 재정렬(실측: 하강 중 A1 +0.3° 밀림).
        SLOW_DOWN_DEG = 10.0
        # ★공중 하강도 상승과 대칭으로 단일 이동 옵션(--down-step 999, 8/12 사용자 제안):
        #   "바닥까지 10° 남은 지점"까지는 한 번에 내리고, 거기서부터 원래대로 0.5° 스텝.
        #   마지막 10° 의 정밀도는 손대지 않으므로 삽입 공차에는 영향이 없다.
        down_step = float(arg("--down-step", f"{step:g}"))
        down_speed = float(arg("--down-speed", f"{fast_speed:g}"))
        down_lead = max(down_speed * 0.083, 0.3)
        down_phases = ((down_speed, down_lead), (speed, max(lead * 0.4, 0.15)))
        print(f"\n  ── ③ 정밀 하강 {lift_n}→{place_n} (공중 {down_speed:g}%/스텝 {down_step:g}° "
              f"→ 마지막 {SLOW_DOWN_DEG}° 전부 {slow_step}°) ──", flush=True)
        P.goto_axis(zk, guard, "A1", place["A1"], 0.12, rot_phases)   # 하강 전 재정렬
        a = P.angles(zk)
        d2, d3 = place["A2"] - a["A2"], place["A3"] - a["A3"]
        total = max(abs(d2), abs(d3))
        if total > SLOW_DOWN_DEG:                  # 공중 구간: 큰 스텝으로 접근점까지
            frac = (total - SLOW_DOWN_DEG) / total
            mid = {"A2": a["A2"] + d2 * frac, "A3": a["A3"] + d3 * frac}
            alt_move(zk, guard, {j: a[j] for j in ("A2", "A3")}, mid,
                     "end", down_step, slow_step, 0, tol, phases, down_phases)
            P.goto_axis(zk, guard, "A1", place["A1"], 0.12, rot_phases)  # 진입 직전 재정렬
            a = P.angles(zk)
        alt_move(zk, guard, {j: a[j] for j in ("A2", "A3")}, place,
                 "end", slow_step, slow_step, 0, tol, phases)          # 전 구간 0.5°

        a = P.angles(zk)
        errs = {j: a[j] - place[j] for j in ("A1", "A2", "A3")}
        print(f"\n  도착 자세  {P.fmt(a)}")
        print("  목표 오차  " + "  ".join(f"{j} {e:+.3f}°" for j, e in errs.items()),
              flush=True)
    except Abort as e:
        P.all_off(zk)
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
