#!/usr/bin/env python3
"""★포크 운반(리프트) — 조립 끝난 집을 포크 손잡이째 잡아 출하지로 옮긴다. 9/8 사용자 지정 절차.

  사용자가 준 순서(그대로 따른다):
    그리퍼 47 파지 → z550 상승 → rz 90 → x -400 → y -100 → x -700 → z285 → 그리퍼 100 개방
    → z575 상승 → 홈 자세(fork.json home_tcp, 관측자세 아님)

  속도(사용자 지정): 손잡이 잡으러 갈 때·홈으로 갈 때 20, **포크를 들고 있는 동안은 10**.

  기준값은 state/house/fork.json (사용자 티칭 + 파란 점 축척 실측).
  ⚠ 페이로드는 아직 벽 기준(0.7kg)이다 — 집 무게를 등록하지 않은 채로 도는 절차다.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import place_calc as PC  # noqa: E402

FORK = "/home/ar/bf2_console/state/house/fork.json"
SPD_FREE = 20      # 빈 손: 손잡이 잡으러 갈 때 · 홈으로 갈 때
SPD_LOAD = 10      # 집을 들고 있는 동안
TOL = 0.8          # 도달 허용(최소 실행 이동량 0.8mm 와 같은 눈금)
# ★9/8 사용자 정정: 포크 손잡이는 **47 을 유지해야 물고 있는 것**이고, 실측이 50 쪽으로 벌어지면
#   하중에 죠가 밀려 나는 중 = 떨어지는 신호다. (벽은 "실측 > 명령 = 물림" 이라 규칙이 정반대다 —
#   벽은 얇아 죠가 벽 두께에서 멈추지만, 포크는 이미 47 로 물린 뒤 벌어지는 것이라 의미가 다르다.)
#   ★9/8 실기 정정 2: 명령 47 을 줘도 죠는 **50 에서 멈춘다**(두 번 재현, 사용자 육안으로 물림 확인).
#   50 이 이 손잡이 두께에서의 도달값이다. 그래서 기준은 절대값이 아니라 **파지 직후 값(HOLD_REF)**,
#   거기서 더 벌어지면 빠지는 중으로 본다 — 사용자 규칙("늘어나면 떨어지는 것")을 실제 도달값 위에서 적용.
HOLD_MAX_OVER = 1  # 파지 직후 값 + 이만큼을 넘으면 벌어지는 중 → 즉시 정지
HOLD_REF = {"v": None}
# ★사용자 지시: "그리퍼 여는 구간 아닌 구간엔 절대 열면 안 된다" → 개방은 코드에 딱 두 군데뿐이고,
#   둘 다 자리 조건을 만족해야만 실행된다(아래 OPEN_AT_* 가드).
OPEN_MIN_Z_START = 400.0   # ①파지 전 개방: 손잡이 위 높이에서만
DROP_XY_TOL = 5.0          # ②내려놓기 개방: 출하지 자리와 이만큼 안에 있어야


def log(s):
    print(s, flush=True)


def _tcp():
    return [round(v, 2) for v in PC.st()["tcp"]]


def check_hold(cfg, where):
    """★들고 가는 내내 그리퍼 값을 본다. 명령 47 을 유지해야 정상 — 50 쪽으로 벌어지면 빠지는 중."""
    g = PC.grip_read()
    base = HOLD_REF["v"] if HOLD_REF["v"] is not None else cfg["grip_cmd"]
    lim = base + HOLD_MAX_OVER
    if g.isdigit() and int(g) > lim:
        raise RuntimeError(f"포크 빠지는 중({where}): 그리퍼 {g} > 파지 직후 {base}+{HOLD_MAX_OVER} — 죠가 벌어졌다")
    return g


def dot_px(n=5):
    """손잡이 파란 점(손목캠). z440 관측 자세에서만 보인다 — z280 에서는 화면 밖."""
    import cv2, numpy as np, urllib.request as UR
    xs, ys, ar = [], [], []
    for _ in range(n):
        b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=6).read()
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, np.array([95, 150, 140]), np.array([115, 255, 255]))
        k, lab, st, cen = cv2.connectedComponentsWithStats(m, 8)
        cand = [(cen[i][0], cen[i][1], st[i, cv2.CC_STAT_AREA]) for i in range(1, k)
                if st[i, cv2.CC_STAT_AREA] >= 60]
        if cand:
            c = max(cand, key=lambda q: q[2]); xs.append(c[0]); ys.append(c[1]); ar.append(c[2])
        time.sleep(0.2)
    if not xs:
        return None
    import statistics as _s
    return (_s.median(xs), _s.median(ys), int(_s.median(ar)), len(xs))


def locate(cfg, tries=2):
    """★파란 점으로 XY 보정(사용자 지시 '검출 기준으로 써줘'). 포크가 밀려도 따라간다.
    축척은 z440 에서 5mm 왕복으로 실측한 역행렬(0.261/0.254 mm/px, 교차항 ~0)."""
    import urllib.request as UR
    UR.urlopen(f"http://127.0.0.1:8766/expo?set={cfg['expo']}", timeout=6).read(); time.sleep(1.2)
    Ji = cfg["inv_mm_per_px"]; ref = cfg["ref_px"]
    for k in range(tries):
        d = dot_px()
        if not d:
            raise RuntimeError("손잡이 파란 점 미검출 — 파지 금지(포크 자리를 못 찾음)")
        du, dv = d[0] - ref[0], d[1] - ref[1]
        # ★9/9 실기 증명(부호 버그): Ji·Δpx 는 "카메라가 기준에서 어디로 갔나" 이지 "어디로 되돌려야 하나" 가 아니다.
        #   검출0 Δpx(-7.2,+6.5) → (-1.90,-1.67) 이동 → 검출1 Δpx(-14.6,+10.5) 로 잔차가 J 예측대로 2배.
        #   어제는 손잡이가 기준 자리에 있어(Δ≈0) 부호가 시험되지 않았다. 되돌리려면 반대로 움직인다.
        dx = -(Ji[0][0] * du + Ji[0][1] * dv)
        dy = -(Ji[1][0] * du + Ji[1][1] * dv)
        log(f"  검출 {k}: 점 ({d[0]:.1f},{d[1]:.1f}) 면적 {d[2]} {d[3]}/5 · 기준 대비 "
            f"Δpx ({du:+.1f},{dv:+.1f}) → XY ({dx:+.2f},{dy:+.2f})mm")
        if max(abs(dx), abs(dy)) < TOL:
            log(f"  → {TOL}mm 미만, 보정 없이 진행")
            return
        c = PC.st()["tcp"]
        PC.move([c[0] + dx, c[1] + dy, c[2], 180.0, 0.0, c[5]], tol=0.4, timeout=30, tag=f"보정{k}")
    log("  (보정 2회 후에도 잔차 남음 — 그대로 진행)")


def step(cfg, tag, x=None, y=None, z=None, rz=None, hold=True):
    """한 축씩만 바꾼다(사용자 순서 그대로). 도달 확인 + 들고 있으면 파지 확인."""
    c = PC.st()["tcp"]
    p = [c[0] if x is None else x, c[1] if y is None else y, c[2] if z is None else z,
         180.0, 0.0, c[5] if rz is None else rz]
    PC.move(p, tol=TOL, timeout=60, tag=tag)
    t = PC.st()["tcp"]
    bad = max(abs(t[i] - p[i]) for i in range(3))
    g = check_hold(cfg, tag) if hold else PC.grip_read()
    log(f"  ✓ {tag:12s} ({t[0]:8.2f},{t[1]:8.2f},{t[2]:7.2f}) rz{t[5]:+7.2f}  오차 {bad:.2f}mm  그리퍼 {g}")
    if bad > TOL + 0.5:
        raise RuntimeError(f"{tag}: 목표에서 {bad:.2f}mm 벗어남 — 정지")


def carry(grasp=True):
    cfg = json.load(open(FORK, encoding="utf-8"))
    log(f"포크 운반 시작 — 기준 {cfg['made']}  파지 {cfg['grip_cmd']}(실측 {cfg['grip_real']})")

    if grasp:
        # ① 파지: 손잡이 위에서 벌린 채 내려가 잡는다(빈 손 구간 = 속도 20)
        c = PC.st()["tcp"]
        if c[2] < OPEN_MIN_Z_START:
            raise RuntimeError(f"개방 금지: 지금 z{c[2]:.0f} < {OPEN_MIN_Z_START:.0f} — "
                               f"손잡이 위 높이가 아니다(들고 있는 중이면 여기서 열면 떨어진다)")
        PC.speed(SPD_FREE)
        log(f"  그리퍼 열기 → {cfg['grip_open']} (z{c[2]:.0f}, 손잡이 위 확인됨)")
        PC.gripper(cfg["grip_open"]); time.sleep(3.0)
        # ★관측 자세 복귀: 지금 자리가 어디든 먼저 손잡이 위로 간다(z 만 내리면 엉뚱한 XY 에서 내려간다)
        A = cfg["above_tcp"]
        c = PC.st()["tcp"]
        PC.move([A[0], A[1], max(c[2], A[2]), 180.0, 0.0, A[5]], tol=TOL, timeout=60, tag="손잡이 위 XY")
        step(cfg, "관측 z440", z=A[2], hold=False)
        locate(cfg)                                   # 파란 점으로 XY 보정
        step(cfg, "z 파지높이", z=cfg["grasp_z"], hold=False)
        log(f"  그리퍼 닫기 → {cfg['grip_cmd']}")
        PC.gripper(cfg["grip_cmd"]); time.sleep(4.0)
        g = PC.grip_read()
        if not g.isdigit() or int(g) >= cfg["grip_open"]:
            raise RuntimeError(f"파지 실패: 그리퍼 {g} — 닫히지 않았다")
        HOLD_REF["v"] = int(g)
        log(f"  파지 판정 OK: 그리퍼 {g} — 이 값을 유지 기준으로 삼는다(넘으면 빠지는 중)")

    # ② 여기서부터 집을 들고 간다 — 속도 10
    if HOLD_REF["v"] is None:                       # --nograsp: 이미 물고 있는 값을 기준으로 잡는다
        g0 = PC.grip_read()
        if not g0.isdigit() or int(g0) >= cfg["grip_open"]:
            raise RuntimeError(f"적재 구간 진입 거부: 그리퍼 {g0} — 물고 있지 않다")
        HOLD_REF["v"] = int(g0)
        log(f"  현재 파지값 {g0} 을 유지 기준으로 삼는다(넘으면 빠지는 중 → 정지)")
    PC.speed(SPD_LOAD)
    log(f"  ── 적재 구간(속도 {SPD_LOAD})")
    step(cfg, "z550 상승",  z=550.0)
    step(cfg, "rz 90",      rz=90.0)
    step(cfg, "x -400",     x=-400.0)
    step(cfg, "y -100",     y=-100.0)
    step(cfg, "x -700",     x=-700.0)
    step(cfg, "z285 하강",  z=285.0)

    # ③ 내려놓기 — ★출하지 자리에서만 연다(사용자 지시: 다른 구간에서 개방 절대 금지)
    c = PC.st()["tcp"]
    if abs(c[0] - (-700.0)) > DROP_XY_TOL or abs(c[1] - (-100.0)) > DROP_XY_TOL or abs(c[2] - 285.0) > DROP_XY_TOL:
        raise RuntimeError(f"개방 금지: 지금 ({c[0]:.1f},{c[1]:.1f},{c[2]:.1f}) 가 출하지 (-700,-100,285) 에서 "
                           f"{DROP_XY_TOL:.0f}mm 넘게 벗어남 — 여기서 열면 집을 엉뚱한 데 떨어뜨린다")
    log(f"  그리퍼 열기 → {cfg['grip_open']} (출하지 확인됨)")
    PC.gripper(cfg["grip_open"]); time.sleep(3.0)
    log(f"  개방 후 그리퍼 {PC.grip_read()}")
    step(cfg, "z575 상승",  z=575.0, hold=False)

    # ④ 빈 손 — 속도 20 으로 홈
    PC.speed(SPD_FREE)
    log(f"  ── 복귀 구간(속도 {SPD_FREE})")
    # ★9/9 사용자 정정: 홈은 관측자세가 아니다. 9/8 에 내가 "홈 자세" 를 OBS 로 멋대로 대체했었다.
    #   홈 = fork.json home_tcp (사용자가 조그로 맞춘 자세, 관절 90/-90/90/-90/-90/0). 없으면 가지 않고 멈춘다.
    H = cfg.get("home_tcp")
    if not H:
        raise RuntimeError("fork.json 에 home_tcp 가 없다 — 홈 자세를 먼저 저장(관측자세로 대체하지 않는다)")
    PC.move(list(H), tol=TOL, timeout=90, tag="홈")
    log(f"  ✓ 홈 {_tcp()}  그리퍼 {PC.grip_read()}")
    log("포크 운반 완료")


if __name__ == "__main__":
    try:
        carry(grasp=("--nograsp" not in sys.argv))
    except Exception as e:
        PC.post("stop", {"dry_run": False}); time.sleep(0.5)
        log(f"❌ 정지: {e}")
        log(f"   현재 TCP {_tcp()} 그리퍼 {PC.grip_read()} — 집은 사용자가 처리")
        raise
