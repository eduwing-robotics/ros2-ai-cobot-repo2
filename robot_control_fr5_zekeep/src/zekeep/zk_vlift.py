#!/usr/bin/env python3
"""
zk_vlift.py - 팔끝 수직 상승 실측·실행 도구 (2026-08-13)

오늘 문제: 흡착 후 지그(높이 10mm, ㄱ자 4점 가이드)에서 빼며 상승할 때
팔끝이 옆으로 밀려 부품이 가이드에 걸리고 흡착판 위에서 틀어짐(실패 목격).
zk_zline 의 "수직 직선" 실증은 하강 105mm 뿐 — 상승 방향은 검증된 적 없다.
★모든 판단은 오늘 자로 잰 숫자로만 한다. fk 모델 수치는 참고 표기일 뿐.

단계1  probe — 현재 방식(zk_carry ① 상승: A2/A3 교대, pick→lift 델타 비율)
       그대로 모델 기준 +20mm 상승. 시작/종료/스텝별 각도를 남기고,
       수평 이탈·실제 상승량은 사용자가 자로 실측한다. ★무부하 전용.

    python3 zk_vlift.py probe --robot zkbot1               # +20mm, 0.5°스텝, 5%
    python3 zk_vlift.py probe --rise 20 --step 0.5 --speed 5
    python3 zk_vlift.py probe --a3-deg 10                  # mm 대신 A3 +10° 지정

★무동작 함정 방지(8/12 zk_axis 사건 재발 금지): 스텝·최종마다 실제 각도
  변화량을 계획과 대조해 출력하고, 실동작이 계획의 80% 미만이면 🔴 표시와
  함께 종료코드 2 로 끝낸다 — "성공 로그처럼 보이는 무동작" 차단.
★물리 확인 절차 내장(8/12 오배치 사고 재발 금지): 동작 전 부품 유무·간섭
  체크리스트에 y 를 쳐야만 움직인다(--yes 로 생략 — 노드 위임용).
★시리얼 소유: zk_ros2_node 가 포트를 잡고 있으면 EPROTO 로 죽는 대신
  실행 전 lsof 로 검사해 명확한 메시지로 거부한다.

기록: 매 실행을 ~/zkbot_data/vlift_log.jsonl 에 1줄 JSON 으로 남긴다.
"""
import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort, HOME_FLAG          # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402

# 기구학 모델 — zk_zline.py 와 동일 상수(8/12 역산). 참고 표기 전용.
L1 = L2 = 200.0
Q20, Q30 = 7.741, -18.932
LOG = "/home/ar/zkbot_data/vlift_log.jsonl"


def fk(a2, a3):
    q2, q3 = math.radians(Q20 - a2), math.radians(Q30 - a3)
    return (L1 * math.sin(q2) + L2 * math.cos(q3),
            L1 * math.cos(q2) - L2 * math.sin(q3))


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def port_guard(prof):
    """다른 프로세스(zk_ros2_node 등)가 포트를 잡고 있으면 거부.
    EPROTO 쓰레기 로그 대신 원인과 해법을 바로 말한다."""
    dev = os.path.realpath(prof.port)
    try:
        out = subprocess.run(["lsof", "-t", dev], capture_output=True,
                             text=True, timeout=5).stdout.split()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return                          # lsof 없으면 기존 방식대로 진행
    if out:
        sys.exit(f"■ {dev} 를 PID {','.join(out)} 가 사용 중 "
                 f"(zk_ros2_node 추정) — 노드 내리고 재시도\n"
                 f"  확인: lsof {dev}")


def confirm_physical(loaded):
    """실물 동작 전 물리 상태 확인 (8/12 오배치 사고 재발 금지)."""
    if "--yes" in sys.argv:
        return
    print("\n■ 물리 확인 (직접 눈으로 보고 답할 것)")
    if loaded:
        print("  1. 부품이 흡착판에 붙어 있고 펌프 ON 인가?")
    else:
        print("  1. 흡착판·지그에 부품이 없는가? (무부하 검증 단계)")
    print("  2. 팔 이동 경로(위쪽 20mm+α)에 간섭물이 없는가?")
    ans = input("  둘 다 확인했으면 y 입력: ").strip().lower()
    if ans != "y":
        sys.exit("■ 물리 확인 미완 — 중단")


def append_log(rec):
    rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  기록 → {LOG}")


def read_angles(zk):
    a = P.angles(zk)
    if any(v is None for v in a.values()):
        raise Abort(f"각도 읽기 실패: {a}")
    return a


def check_homed(zk):
    ms = zk.m(32) or set()
    missing = [j for j, b in HOME_FLAG.items() if b not in ms]
    if missing:
        sys.exit(f"■ 원점 미확보: {missing} — 이 상태의 각도는 가짜다. "
                 f"zk_home_run.py 로 재동기 후 진행")


# ────────────────────────────────────────────────────────────────────
# 단계1  probe — 현재 방식(carry 비율) 상승의 수평 이탈 계측
# ────────────────────────────────────────────────────────────────────
def cmd_probe(prof):
    rise = float(arg("--rise", "20"))
    step = float(arg("--step", "0.5"))
    speed = float(arg("--speed", "5"))

    with open(prof.pose_file) as f:
        poses = json.load(f)
    for n in ("base_pick", "base_lift"):
        if n not in poses:
            sys.exit(f"■ 자세DB 에 '{n}' 없음: {prof.pose_file}")
    pick, lift = poses["base_pick"], poses["base_lift"]
    d2, d3 = lift["A2"] - pick["A2"], lift["A3"] - pick["A3"]
    ratio = d2 / d3                     # 현재 방식: A3 1° 당 A2 이동량 (부호 포함)

    confirm_physical(loaded=False)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name} {prof.port})")
    plan_t = 0.0
    steps = []
    a0 = aN = None
    try:
        check_homed(zk)
        a0 = read_angles(zk)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)
        off = max(abs(a0[j] - pick[j]) for j in ("A1", "A2", "A3"))
        print(f"  시작 자세  {P.fmt(a0)}")
        if off > 2.0:
            print(f"  ⚠ base_pick 과 {off:.1f}° 어긋난 지점에서 시작 — 그대로 진행"
                  f"(계측은 현재 위치 기준)")

        # 상승 계획: 모델 z 가 +rise 될 때까지 A3 를 0.01° 씩 walk (A2 는 비율 추종)
        z0 = fk(a0["A2"], a0["A3"])[1]
        r0 = fk(a0["A2"], a0["A3"])[0]
        sgn = 1.0 if d3 > 0 else -1.0   # lift 방향 = 상승 방향
        t = 0.0
        while fk(a0["A2"] + ratio * sgn * t, a0["A3"] + sgn * t)[1] - z0 < rise:
            t += 0.01
            if t > 45.0:
                raise Abort("상승 계획 실패: A3 45° 안에 모델 z 미도달")
        plan_t = t
        print(f"  현재 방식 비율: A3 1° 당 A2 {ratio:+.3f}°  (pick→lift 델타 비)")
        print(f"  계획: A3 {sgn*t:+.2f}° / A2 {sgn*t*ratio:+.2f}°  "
              f"[모델 z +{rise:g}mm 상당 — 참고용]  스텝 {step}° @{speed:g}%")

        lead = max(speed * 0.083, 0.2)
        phases = ((speed, lead), (speed, max(lead * 0.4, 0.15)))
        n = max(1, int(math.ceil(t / step)))
        prev = dict(a0)
        for i in range(1, n + 1):
            adv = min(i * step, t)
            t2 = a0["A2"] + sgn * adv * ratio
            t3 = a0["A3"] + sgn * adv
            P.goto_axis(zk, guard, "A2", t2, 0.15, phases)   # carry 와 동일: A2 선행
            P.goto_axis(zk, guard, "A3", t3, 0.15, phases)
            cur = read_angles(zk)
            rn, zn = fk(cur["A2"], cur["A3"])
            moved = max(abs(cur["A2"] - prev["A2"]), abs(cur["A3"] - prev["A3"]))
            flag = "" if moved > 0.1 else "  🔴 이 스텝 무동작 의심"
            print(f"  wp{i:2d}/{n}  A2 {cur['A2']:8.3f}  A3 {cur['A3']:8.3f}   "
                  f"[모델 z{zn - z0:+6.1f}  r{rn - r0:+5.1f}mm]{flag}", flush=True)
            steps.append({"i": i, "A2": round(cur["A2"], 3), "A3": round(cur["A3"], 3),
                          "model_dz": round(zn - z0, 2), "model_dr": round(rn - r0, 2)})
            prev = cur

        aN = read_angles(zk)
    except KeyboardInterrupt:
        print("\n■ 사용자 중단(Ctrl+C) — 조그 해제 후 종료")
    except Abort as e:
        print(f"\n■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        if aN is None and a0 is not None:       # 중단 시에도 종료 각도는 남긴다
            try:
                aN = read_angles(zk)
            except Exception:
                aN = None
        zk.close()

    if a0 is None or aN is None:
        sys.exit(2)

    dA2, dA3 = aN["A2"] - a0["A2"], aN["A3"] - a0["A3"]
    rn, zn = fk(aN["A2"], aN["A3"])
    r0, z0 = fk(a0["A2"], a0["A3"])
    plan3 = plan_t if plan_t else 1e-9
    frac = abs(dA3) / plan3
    print(f"\n  ── 결과 ──")
    print(f"  시작  {P.fmt(a0)}")
    print(f"  종료  {P.fmt(aN)}")
    print(f"  실동작 ΔA2 {dA2:+.3f}°  ΔA3 {dA3:+.3f}°   "
          f"(계획 ΔA3 {plan3:+.2f}° 의 {frac * 100:.0f}%)")
    ok_moved = frac >= 0.8
    if not ok_moved:
        print("  🔴 실동작이 계획의 80% 미만 — 무동작/부분동작. 이 회차 계측 무효")
    print(f"  모델 추정(참고): Δz {zn - z0:+.1f}mm  Δr {rn - r0:+.1f}mm "
          f"(+r = A1축에서 멀어지는 쪽)")
    print("\n  ▶ 자로 잴 것: ① 실제 상승량(mm)  ② 수평 이탈량(mm)과 방향")
    print("    방향 표기: 로봇 몸통에서 멀어짐 = 바깥(+) / 가까워짐 = 안(−)")
    append_log({"mode": "probe", "robot": prof.name, "rise_mm": rise,
                "step_deg": step, "speed_pct": speed,
                "ratio_a2_per_a3": round(ratio, 4),
                "start": {j: round(a0[j], 3) for j in ("A1", "A2", "A3")},
                "end": {j: round(aN[j], 3) for j in ("A1", "A2", "A3")},
                "plan_dA3": round(plan3, 3), "moved_frac": round(frac, 3),
                "model_dz": round(zn - z0, 2), "model_dr": round(rn - r0, 2),
                "steps": steps, "valid": ok_moved})
    sys.exit(0 if ok_moved else 2)


# ────────────────────────────────────────────────────────────────────
# up — IK 수직 직선 상승 (r 고정). 모델은 하강 105mm ±2mm 실증(8/12),
#      상승 방향은 오늘이 첫 검증 — 판단은 종이 트레이스 실측으로만.
# ────────────────────────────────────────────────────────────────────
def ik(r, z, a2g, a3g):
    """뉴턴 2변수 (zk_zline 과 동일). 초기치는 이웃 자세."""
    a2, a3 = a2g, a3g
    for _ in range(60):
        cr, cz = fk(a2, a3)
        er, ez = r - cr, z - cz
        if abs(er) < 0.02 and abs(ez) < 0.02:
            return a2, a3
        r2, z2 = fk(a2 + 1e-3, a3)
        r3, z3 = fk(a2, a3 + 1e-3)
        j11, j12 = (r2 - cr) / 1e-3, (r3 - cr) / 1e-3
        j21, j22 = (z2 - cz) / 1e-3, (z3 - cz) / 1e-3
        det = j11 * j22 - j12 * j21
        a2 += (j22 * er - j12 * ez) / det
        a3 += (-j21 * er + j11 * ez) / det
    raise Abort("IK 미수렴")


def cmd_up(prof):
    rise = float(arg("--rise", "30"))       # 30mm ≈ A3 약 10° 상당. 음수 = 직하강
    # ③2026-08-21: 3→1.5mm. 스텝당 이동이 절반이라 오버슛도 절반.
    z_step = float(arg("--z-step", "1.5"))
    speed = float(arg("--speed", "5"))
    loaded = "--loaded" in sys.argv         # 부품 문 상태 (체크리스트 전환)
    # 2026-08-21 갱신: 55.7 은 이설 전(8/13) ZK1 배치의 pick 접촉면이었다.
    # 두 번의 물리 이설로 무의미해졌고, 정상 하강까지 막았다.
    # 오늘 실측 — ZK1 pick 접촉면 z = -47.0 / ZK2 pick 93.8·place 57.5.
    # 가장 낮은 접촉면(-47.0)에서 5mm 여유를 둔다.
    Z_FLOOR = -52.0
    # 2단 프로파일 (8/13 사용자 지시: "6도만 들고 그다음은 빠르게"):
    #   가이드 구간(상승은 처음/하강은 마지막 slow_mm)만 정밀 저속,
    #   그 밖은 fast_speed + 큰 스텝. fast-speed 0 = 기존 단일 속도.
    #   ★fast 스텝은 커야 한다 — 20% lead 1.66° 보다 작은 이동은
    #     goto_axis 가 1단계를 skip 해 2% 로만 기어간다(속도 무의미).
    fast_speed = float(arg("--fast-speed", "0"))
    slow_mm = float(arg("--slow-mm", "21"))
    fast_step = float(arg("--fast-step", "10"))

    confirm_physical(loaded=loaded)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name} {prof.port})")
    steps = []
    a0 = aN = None
    plan = None
    try:
        check_homed(zk)
        a0 = read_angles(zk)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)
        r0, z0 = fk(a0["A2"], a0["A3"])
        z_tgt = z0 + rise
        if rise < 0 and z_tgt < Z_FLOOR and "--force" not in sys.argv:
            raise Abort(f"목표 z {z_tgt:.1f} < 하한 {Z_FLOOR} — --force 필요")
        # 계획을 먼저 전부 세운다 (실패할 계획이면 움직이기 전에 죽도록)
        #   wp = (z, A2, A3, fast여부). 가이드 구간(상승=처음/하강=마지막 slow_mm)은
        #   z_step 잘게, 나머지는 단일 이동("한번에", 8/13 사용자 지시).
        wps = []
        z = z0
        sgn = 1.0 if rise > 0 else -1.0
        a2p, a3p = a0["A2"], a0["A3"]

        def fine_to(z_end):
            nonlocal z, a2p, a3p
            while (z_end - z) * sgn > 0.01:
                z += sgn * min(z_step, abs(z_end - z))
                a2p, a3p = ik(r0, z, a2p, a3p)
                wps.append((z, a2p, a3p, False))

        def single_to(z_end):
            nonlocal z, a2p, a3p
            if (z_end - z) * sgn > 0.01:
                z = z_end
                a2p, a3p = ik(r0, z, a2p, a3p)
                wps.append((z, a2p, a3p, True))

        if fast_speed <= 0:
            fine_to(z_tgt)
        elif rise > 0:                       # 상승: 처음 slow_mm 정밀 → 나머지 한번에
            fine_to(min(z0 + slow_mm, z_tgt) if sgn > 0 else z_tgt)
            single_to(z_tgt)
        else:                                # 하강: 마지막 slow_mm 전까지 한번에 → 정밀
            single_to(max(z_tgt + slow_mm, min(z0, z_tgt + slow_mm)))
            fine_to(z_tgt)
        plan = (a2p - a0["A2"], a3p - a0["A3"])
        nf = sum(1 for w in wps if w[3])
        print(f"  시작 자세  {P.fmt(a0)}   [모델 r={r0:.1f} z={z0:.1f}]")
        print(f"  계획: z {rise:+g}mm 수직 직선({len(wps)}웨이포인트, 정밀 {len(wps)-nf}"
              f"@{speed:g}% + 한번에 {nf}@{fast_speed:g}%)  "
              f"ΔA2 {plan[0]:+.2f}° ΔA3 {plan[1]:+.2f}°", flush=True)

        lead = max(speed * 0.083, 0.2)
        fine_lead = float(arg("--fine-lead", "0.08"))
        # ②2026-08-21: 0.15°→0.08°. base_lift 자세에서 0.15° 는 r 로 약 0.45mm,
        #   0.08° 면 약 0.24mm — 이론 하한이 절반이 된다.
        phases = ((speed, lead), (2.0, fine_lead))
        f_lead = max(fast_speed * 0.083, 0.3)
        f_phases = ((fast_speed, f_lead), (2.0, 0.15))
        # ★2026-08-21 ③적용: 동시구동이 기본. 웨이포인트마다 A2·A3 를 같이 출발시켜
        #   같이 도착시킨다 → 덜컹임 절반 = 흡착 부품 회전 절반. 미세 수렴은 2% 단계가 마무리.
        #   --seq 로 옛 순차 방식 복귀 가능.
        simul = "--seq" not in sys.argv
        prev = dict(a0)
        for i, (zt, t2, t3, fast) in enumerate(wps, 1):
            ph = f_phases if fast else phases
            if simul:
                P.goto_two(zk, guard, {"A2": t2, "A3": t3},
                           lead=ph[0][1], pct=ph[0][0])
                P.goto_axis(zk, guard, "A2", t2, 0.2, (ph[-1],))
                P.goto_axis(zk, guard, "A3", t3, 0.2, (ph[-1],))
            else:
                P.goto_axis(zk, guard, "A2", t2, 0.2, ph)   # zline 검증 순서: A2 선행
                P.goto_axis(zk, guard, "A3", t3, 0.2, ph)
            cur = read_angles(zk)
            rn, zn = fk(cur["A2"], cur["A3"])
            moved = max(abs(cur["A2"] - prev["A2"]), abs(cur["A3"] - prev["A3"]))
            flag = "" if moved > 0.1 else "  🔴 이 스텝 무동작 의심"
            print(f"  wp{i:2d}/{len(wps)}  A2 {cur['A2']:8.3f}  A3 {cur['A3']:8.3f}   "
                  f"[모델 z{zn - z0:+6.1f}  r편차 {rn - r0:+5.2f}mm]{flag}", flush=True)
            steps.append({"i": i, "A2": round(cur["A2"], 3), "A3": round(cur["A3"], 3),
                          "model_dz": round(zn - z0, 2), "model_dr": round(rn - r0, 2)})
            prev = cur
        # ①2026-08-21 최종 r 보정 — 유일한 되먹임 구간.
        #   웨이포인트 목표는 전부 r0 선 위에 절대 계산되므로 누적은 없다.
        #   다만 **마지막 스텝의 착지 잔차**는 그대로 남는다 → 측정 각도에서
        #   다시 IK 를 풀어 (r0, z_tgt) 로 되돌린다.  --no-rfix 로 끌 수 있다.
        if "--no-rfix" not in sys.argv:
            R_TOL, Z_TOL, N_FIX = 0.10, 0.30, 3          # mm
            fx_ph = ((2.0, fine_lead),)
            for k in range(N_FIX):
                cur = read_angles(zk)
                rc, zc = fk(cur["A2"], cur["A3"])
                dr, dz = rc - r0, zc - z_tgt
                if abs(dr) <= R_TOL and abs(dz) <= Z_TOL:
                    print(f"  r보정 {k}회 — 허용 안 (Δr {dr:+.2f}mm, Δz {dz:+.2f}mm)", flush=True)
                    break
                t2, t3 = ik(r0, z_tgt, cur["A2"], cur["A3"])
                if abs(t2 - cur["A2"]) < 0.01 and abs(t3 - cur["A3"]) < 0.01:
                    print(f"  r보정 중단 — 필요 이동 0.01° 미만이라 조그로 못 줄임 "
                          f"(Δr {dr:+.2f}mm)", flush=True)
                    break
                P.goto_axis(zk, guard, "A3", t3, 0.05, fx_ph)   # A3 가 r 감도 큼
                P.goto_axis(zk, guard, "A2", t2, 0.05, fx_ph)   # A2 로 z 되돌림
                cur = read_angles(zk)
                rc2, zc2 = fk(cur["A2"], cur["A3"])
                print(f"  r보정 {k+1}회  Δr {dr:+.2f}→{rc2 - r0:+.2f}mm   "
                      f"Δz {dz:+.2f}→{zc2 - z_tgt:+.2f}mm", flush=True)
        aN = read_angles(zk)
    except KeyboardInterrupt:
        print("\n■ 사용자 중단(Ctrl+C) — 조그 해제 후 종료")
    except Abort as e:
        print(f"\n■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        if aN is None and a0 is not None:
            try:
                aN = read_angles(zk)
            except Exception:
                aN = None
        zk.close()

    if a0 is None or aN is None or plan is None:
        sys.exit(2)
    dA2, dA3 = aN["A2"] - a0["A2"], aN["A3"] - a0["A3"]
    r0, z0 = fk(a0["A2"], a0["A3"])
    rn, zn = fk(aN["A2"], aN["A3"])
    frac = abs(dA3) / max(abs(plan[1]), 1e-9)
    print(f"\n  ── 결과 ──")
    print(f"  시작  {P.fmt(a0)}")
    print(f"  종료  {P.fmt(aN)}")
    print(f"  실동작 ΔA2 {dA2:+.3f}°(계획 {plan[0]:+.2f})  "
          f"ΔA3 {dA3:+.3f}°(계획 {plan[1]:+.2f})  실행률 {frac * 100:.0f}%")
    ok_moved = frac >= 0.8
    if not ok_moved:
        print("  🔴 실동작이 계획의 80% 미만 — 무동작/부분동작. 이 회차 계측 무효")
    print(f"  모델 추정(참고): Δz {zn - z0:+.1f}mm  Δr {rn - r0:+.2f}mm "
          f"(+r = A1축에서 멀어지는 쪽)")
    print("\n  ▶ 종이 트레이스 판독: ① 선 세로 길이  ② 가로 치우침  ③ 기운 방향")
    append_log({"mode": "up", "robot": prof.name, "rise_mm": rise,
                "z_step_mm": z_step, "speed_pct": speed, "loaded": loaded,
                "start": {j: round(a0[j], 3) for j in ("A1", "A2", "A3")},
                "end": {j: round(aN[j], 3) for j in ("A1", "A2", "A3")},
                "plan_dA2": round(plan[0], 3), "plan_dA3": round(plan[1], 3),
                "moved_frac": round(frac, 3),
                "model_dz": round(zn - z0, 2), "model_dr": round(rn - r0, 2),
                "steps": steps, "valid": ok_moved})
    sys.exit(0 if ok_moved else 2)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ""
    prof = P.apply_profile(ZPROF.resolve_from_argv())
    port_guard(prof)
    if cmd == "probe":
        cmd_probe(prof)
    elif cmd == "up":
        cmd_up(prof)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
