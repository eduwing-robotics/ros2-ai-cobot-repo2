#!/usr/bin/env python3
"""
zk_lift.py - ZKBOT 수직 상승/하강 (A2·A3 교대 이동)

    zk_lift.py --robot zkbot1 --up 5            # 총 5° 상승
    zk_lift.py --robot zkbot1 --down 5          # 총 5° 하강
    zk_lift.py --robot zkbot1 --up 5 --ratio 1.0 --step 1.0 --speed 6

★ 2단계 스텝 (2026-08-10 실측으로 확정 — 부품 밀림 최소화)
    zk_lift.py --robot zkbot1 --up 10 --slow-deg 5 --slow-step 0.5 --step 2.0

  · 큰 스텝(2.0°)  → 각도 정밀도는 좋지만 관성이 커서 **부품이 밀린다**
  · 작은 스텝(0.5°) → 부품은 안정적이나 가감속 횟수가 많아 각도 오차가 크다
  → **부품이 막 뜨는 구간(상승 초기 / 하강 말기)만 작은 스텝**, 나머지는 큰 스텝.
    상승은 앞쪽 slow-deg 를, 하강은 뒤쪽 slow-deg 를 느리게 간다.

  실측 결과 (10° 왕복, 속도 5%, 가감속 300ms):
    0.5° 단일 스텝      오차 0.25°  (팔끝 1.7mm)   부품 조금 밀림
    2.0° 단일 스텝      오차 0.14°  (0.96mm)      부품 많이 밀림
    2단계 스텝          오차 0.079° (0.55mm)      부품 밀림 최소  ★확정

★ 왜 이렇게 하는가 (2026-08-10 사용자 실측)
  A3 만 움직이면 팔 끝이 원호를 그려 **앞뒤로 밀린다** → 흡착한 부품이 끌린다.
  A2 와 A3 를 **번갈아 조금씩** 주면 두 호가 상쇄되어 수직에 가깝게 움직인다.
  ("a3+와 a2+를 하나씩 번갈아 눌러야 밀리지 않고 들어올려진다" — 실물 관찰)

★ URDF 순기구학을 쓰지 않는 이유
  zkbot.urdf 로 계산한 야코비안이 실측과 방향이 반대로 나왔다(A3+ 를 계산은
  하강, 실물은 상승). URDF 모델의 관절 방향/치수가 실물과 다른 것으로 보여,
  검증되지 않은 계산 대신 **실물에서 확인된 교대 비율**을 쓴다.
  URDF 를 실측으로 교정하면 그때 zk_kin.py 기반으로 바꿀 수 있다.

★ 부호: A2·A3 모두 **+ 방향이 상승**이다(실측).

안전
  · goto_axis 가 소프트리밋·X5·통신두절을 매 스텝 감시한다
  · 총 이동량 상한 30° (실수로 큰 값을 줘도 막는다)
  · 스텝을 너무 작게 하면 감속 오버슛으로 진동한다(2026-08-06 A2 60회 왕복 전과)
    → 최소 0.3° 로 제한
"""
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort                     # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402

MAX_TOTAL_DEG = 30.0
MIN_STEP_DEG = 0.3


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    up = float(arg("--up")) if "--up" in sys.argv else None
    down = float(arg("--down")) if "--down" in sys.argv else None
    if (up is None) == (down is None):
        sys.exit("■ --up 또는 --down 중 하나를 지정하세요 (예: --up 5)")

    total = up if up is not None else -down
    sign = 1.0 if total > 0 else -1.0
    total_abs = abs(total)
    if total_abs > MAX_TOTAL_DEG:
        sys.exit(f"■ 총 이동량 상한 {MAX_TOTAL_DEG}° 초과 — 받은 값 {total_abs}°")

    ratio = float(arg("--ratio", "1.0"))      # A2 이동량 / A3 이동량
    step = float(arg("--step", "1.0"))        # A3 기준 1회 이동량
    speed = float(arg("--speed", "6"))
    tol = float(arg("--tol", "0.15"))
    # 2단계 스텝 — 부품이 취약한 구간만 잘게 나눈다
    slow_deg = float(arg("--slow-deg", "0"))
    slow_step = float(arg("--slow-step", "0.5"))
    if step < MIN_STEP_DEG or (slow_deg > 0 and slow_step < MIN_STEP_DEG):
        sys.exit(f"■ 스텝은 {MIN_STEP_DEG}° 이상이어야 합니다 (진동 방지)")
    if slow_deg > total_abs:
        sys.exit(f"■ --slow-deg({slow_deg}°) 가 총 이동량({total_abs}°) 보다 큽니다")

    prof = P.apply_profile(ZPROF.resolve_from_argv())
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")

    lead = max(speed * 0.083, 0.2)
    phases = ((speed, lead), (speed, max(lead * 0.4, 0.15)))

    try:
        a0 = P.angles(zk)
        print(f"  시작 자세  {P.fmt(a0)}", flush=True)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)

        # ── 스텝 구간 계획 ──
        #   상승: 앞쪽 slow_deg 를 느리게 (부품이 막 뜨는 구간)
        #   하강: 뒤쪽 slow_deg 를 느리게 (착지 직전)
        if slow_deg <= 0:
            plan = [(total_abs, step)]
        elif sign > 0:
            plan = [(slow_deg, slow_step), (total_abs - slow_deg, step)]
        else:
            plan = [(total_abs - slow_deg, step), (slow_deg, slow_step)]
        plan = [(d, s) for d, s in plan if d > 1e-6]

        desc = " + ".join(f"{d}°@{s}°" for d, s in plan)
        print(f"\n  ── {'상승' if sign > 0 else '하강'} 총 {total_abs}° "
              f"[{desc}], A2:A3 = {ratio}:1, 속도 {speed}% ──", flush=True)

        # ★ 누적 목표 방식 (2026-08-10 픽스)
        #   이전에는 매 스텝 "현재각 + step" 으로 목표를 잡았다. 그러면 오버슛으로
        #   이미 허용오차 안에 들어온 스텝을 goto_axis 가 건너뛰면서 그만큼 덜 가고,
        #   그 오차가 누적돼 A2:A3 비율이 무너진다.
        #   실측: 10° 상승에서 A2 9.77 : A3 11.13 으로 어긋났고, 같은 양을 내려도
        #   원위치로 돌아오지 않아 부품이 밀렸다.
        #   → 시작각 기준 누적 목표를 쓰면 스텝을 건너뛰어도 다음 스텝이 따라잡는다.
        base2, base3 = a0["A2"], a0["A3"]
        if base2 is None or base3 is None:
            raise Abort("시작 각도 읽기 실패")

        done = 0.0                      # 지금까지 진행한 각도 (구간 경계에서도 누적 유지)
        seg_no = 0
        for seg_deg, seg_step in plan:
            seg_no += 1
            n = max(1, int(round(seg_deg / seg_step)))
            print(f"    · 구간{seg_no}: {seg_deg}° 를 {seg_step}° × {n}스텝", flush=True)
            for i in range(n):
                adv = done + seg_step * (i + 1)
                if i == n - 1:
                    adv = done + seg_deg          # 구간 끝은 정확히 맞춘다
                t2 = base2 + sign * adv * ratio
                t3 = base3 + sign * adv
                # A2 먼저, 그다음 A3 — 손으로 번갈아 누르던 것과 같은 순서
                P.goto_axis(zk, guard, "A2", t2, tol, phases)
                P.goto_axis(zk, guard, "A3", t3, tol, phases)
                a = P.angles(zk)
                print(f"      [{i+1}/{n}]  A2={a['A2']:8.3f}°  A3={a['A3']:8.3f}°", flush=True)
            done += seg_deg

        time.sleep(0.3)
        a1 = P.angles(zk)
        print(f"\n  최종 자세  {P.fmt(a1)}", flush=True)
        print("  ── 변화량 ──", flush=True)
        for j in ("A1", "A2", "A3"):
            if a0[j] is not None and a1[j] is not None:
                print(f"    {j}  {a1[j] - a0[j]:+7.3f}°", flush=True)
    except Abort as e:
        print(f"\n  ■ 안전 중단: {e}", flush=True)
        sys.exit(1)
    finally:
        P.all_off(zk)
        print(f"\n{zk.report()}", flush=True)
        zk.close()


if __name__ == "__main__":
    main()
