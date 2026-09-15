#!/usr/bin/env python3
"""색점 검출 안정화 — 카메라 설정 자동 탐색·기억·복원 (9/5 신설).

왜 필요한가(사용자 지시 9/5):
  우리는 기둥 꼭대기를 **색점**으로 잡는다. 색은 조명에 취약하다 —
  반사가 생기면 유령점이 늘고, 빛이 바뀌면 색상(H)·채도(S)가 밀려 검출이 통째로 뒤집힌다.
  실제로 빈 베이스 게이트 10회 중 1회가 이 이유로 실패했다.
  → "빨강·노랑·파랑이 전부 잘 잡히던 설정"을 찾아 **기억**하고, 흐트러지면 **되돌린다**.

점수(중요): '4개 잡혔다'로는 부족하다. 임계선에 겨우 걸쳐 잡히는 설정은 조명이 조금만
  흔들려도 무너지기 때문이다. 그래서 **임계선에서의 여유(margin)** 를 점수로 쓴다.
    · 각 점의 H·S·V 가 자기 색 범위의 경계에서 얼마나 떨어져 있나(최소값 = 최악 여유)
    · 꼭짓점 밖 유령 덩어리 개수(반사) → 감점
    · 여러 프레임에서 같은 결과가 나오나(재현) → 감점 없으면 가점

사용:
  python3 color_lock.py check                 # 지금 설정 점수
  python3 color_lock.py calibrate             # 노출 훑어 최적 찾고 저장·적용
  python3 color_lock.py restore               # 저장된 설정 다시 적용
  python3 color_lock.py monitor [주기초]       # 감시 루프(점수 떨어지면 자동 복원)
"""
import sys, json, time, math, os
import urllib.request as UR
import numpy as np
import cv2

sys.path.insert(0, "/home/ar/bf2_console/tools")
import pillar_dots as PD
import base_depth_corner as B

CAM = "http://127.0.0.1:8766"
STORE = "/home/ar/bf2_console/color_lock.json"
# ★기준은 임의 상수가 아니라 '보정 때 실제로 나온 최고점'에 비례해 잡는다.
#   9/5: 0.35 로 박아 뒀더니 도달 불가능한 숫자였다(실측 최고 0.13~0.18).
SCORE_FLOOR = 0.05        # 이 아래는 무조건 이상
SCORE_FRAC = 0.60         # 저장된 최고점의 이 비율 아래로 떨어지면 '흐트러졌다'


def alarm_level():
    st = None
    if os.path.exists(STORE):
        try:
            st = json.load(open(STORE))
        except Exception:
            st = None
    best = (st or {}).get("score") or 0.0
    return max(SCORE_FLOOR, best * SCORE_FRAC)
GHOST_PENALTY = 0.0       # 9/5 저녁: 벽이 꽂히면 벽 점이 '유령'으로 세져 점수가 0 이 됨 → 감점 폐지(정보로만 표시)
ANCHOR_R = 30             # 정답 위치에서 이 반경(px) 안에 잡히면 '그 점을 찾았다'
ANCHORS = "/home/ar/bf2_console/color_anchors.json"   # 사용자 확인 기둥 색점 위치(관측자세)


def expo(**kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    try:
        return json.loads(UR.urlopen(f"{CAM}/expo" + ("?" + q if q else ""), timeout=30).read())
    except Exception as e:
        return {"err": str(e)}


# ★축마다 '위험한 경계'가 다르다(9/5 실측에서 드러난 오류):
#   H = 양쪽(다른 색으로 넘어감)
#   S = **아래쪽만** — 채도는 높을수록 좋다. 위험은 채도가 낮아져 흰 책상(S≈103)에
#       가까워지는 쪽뿐인데, 상한(255) 근접을 감점하는 바람에 파랑(S247)이 여유 0.08 로
#       찍혀 멀쩡한 설정이 전부 0점이 됐다.
#   V = 양쪽(어두우면 그늘에 묻히고, 밝으면 날아간다)
SIDES = {0: "both", 1: "low", 2: "both"}


def _ranges_of(c):
    """9/7: PD.RANGES 는 색에 따라 (lo,hi) 하나 또는 [(lo,hi), ...] 목록(빨강 랩어라운드)."""
    r = PD.RANGES[c]
    return r if isinstance(r, list) else [r]


def _margin(hsv_px, rng):
    """이 색이 자기 범위의 '위험한 경계'에서 얼마나 떨어져 있나(0~1)."""
    lo, hi = rng
    out = []
    for i in range(3):
        span = max(hi[i] - lo[i], 1)
        if SIDES[i] == "low":
            d = (hsv_px[i] - lo[i]) / span
        elif SIDES[i] == "high":
            d = (hi[i] - hsv_px[i]) / span
        else:
            d = min(hsv_px[i] - lo[i], hi[i] - hsv_px[i]) / span
        out.append(max(0.0, min(1.0, d)))
    return min(out)               # 최악 축이 그 점의 여유


def score_frame(img, rect):
    """한 프레임 점수. 반환 (score, detail)"""
    pts, why = PD.four_corners(img, rect)
    if not pts or len(pts) < 3:                       # ★3점 폴백 허용(벽이 기둥 하나를 가릴 수 있음)
        return 0.0, {"why": why or "기둥 3점 미만", "n": len(pts) if pts else 0}
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    cnt = {}
    for p in pts:
        cnt[p[3]] = cnt.get(p[3], 0) + 1
    if cnt.get("blue", 0) > 2 or cnt.get("yellow", 0) > 1 or cnt.get("red", 0) > 1:
        return 0.0, {"why": f"색 구성 불일치 {cnt}"}

    # ★여유는 '그 색 마스크에 실제로 든 화소'로만 재야 한다.
    #   9/5: 7×7 창 중앙값을 썼더니 창에 섞인 검은 기둥 화소 때문에 중앙값이 범위 밖으로 밀려
    #   4점을 멀쩡히 잡은 프레임도 여유 0 → 점수 0 이 됐다(노출 7단계 전부 0점).
    mars = []
    for x, y, a, c, *_ in pts:
        y0, y1 = max(0, int(y) - 5), int(y) + 6
        x0, x1 = max(0, int(x) - 5), int(x) + 6
        win = hsv[y0:y1, x0:x1].reshape(-1, 3)
        best = 0.0; got = False
        for lo, hi in _ranges_of(c):
            inr = win[(win[:, 0] >= lo[0]) & (win[:, 0] <= hi[0]) &
                      (win[:, 1] >= lo[1]) & (win[:, 1] <= hi[1]) &
                      (win[:, 2] >= lo[2]) & (win[:, 2] <= hi[2])]
            if len(inr) < 5:
                continue
            got = True; best = max(best, _margin(np.median(inr, axis=0), (lo, hi)))
        mars.append(best if got else 0.0)
    worst = min(mars)

    # 유령: 꼭짓점 근처가 아닌 색 덩어리
    d_all = PD.detect(img, None)
    ghosts = 0
    for k, v in d_all.items():
        for p in v:
            if min(math.dist(p[:2], (c[0], c[1])) for c in rect) > PD.CORNER_R_PX:
                ghosts += 1
    sc = max(0.0, worst - GHOST_PENALTY * ghosts)
    return sc, {"worst_margin": round(worst, 3), "margins": [round(m, 3) for m in mars],
                "ghosts": ghosts, "areas": [p[2] for p in pts],
                "colors": [p[3] for p in pts]}


def grab_and_score(n=3):
    """n 프레임 평균 점수 + 재현 확인."""
    scs, dets = [], []
    rect = None
    for _ in range(n):
        img, grid = B.grab_pair()
        if img is None:
            continue
        try:
            m, _w = B.detect_rect(img, grid)
        except Exception:
            m = None
        r = None
        if m:
            r = PD.plate_rect(m)
        if r is None:
            scs.append(0.0); dets.append({"why": "밑판 사각형 실패"}); continue
        rect = r
        s, d = score_frame(img, r)
        scs.append(s); dets.append(d)
    if not scs:
        return 0.0, {"why": "프레임 없음"}, rect
    ok = sum(1 for s in scs if s > 0)
    mean = sum(scs) / len(scs)
    # 재현: 전부 성공해야 만점, 하나라도 실패하면 그 비율만큼 깎는다
    return mean * (ok / len(scs)), {"frames": len(scs), "4점검출": ok,
                                    "scores": [round(s, 3) for s in scs],
                                    "detail": dets[-1]}, rect


def at_anchor_pose(tol=8.0):
    """★정답 위치는 관측자세의 화면좌표다. 로봇이 다른 곳에 있으면 안 잡히는 게 당연하므로
    그 상태로 채점하면 감시가 노출을 헛되이 흔든다. 자세가 맞을 때만 채점한다."""
    A = load_anchors()
    want = (A or {}).get("tcp")
    if not want:
        return True, "저장된 자세 없음(자세 확인 생략)"
    try:
        j = json.loads(UR.urlopen("http://127.0.0.1:8765/status", timeout=5).read())
        cur = j["robots"]["fr5"]["tcp"]
    except Exception as e:
        return False, f"로봇 상태 조회 실패 {e}"
    d = max(abs(cur[i] - want[i]) for i in range(3))
    return d <= tol, f"관측자세 대비 {d:.1f}mm"


def load_anchors():
    """사용자가 확인해 준 기둥 색점 위치(관측자세 화면좌표). 없으면 None."""
    if not os.path.exists(ANCHORS):
        return None
    try:
        return json.load(open(ANCHORS))
    except Exception:
        return None


def score_anchored(img, anchors):
    """★주의(9/5 사용자 지시): **베이스는 ZK 가 놓는 오차로 언제든 비틀리거나 움직인다.**
    ArUco 가 붙은 흰 판만 고정이고 밑판은 고정이 아니다. 그래서 이 '화면 절대좌표' 채점은
    베이스가 그대로일 때만 유효하다 — 베이스가 움직이면 색은 멀쩡한데 '못 찾음'으로 읽혀
    노출을 헛되이 흔든다. 상시 감시는 밑판 꼭짓점 기준(score_frame)을 쓰고,
    이 함수는 '베이스를 안 움직였을 때의 정밀 비교'용으로만 쓴다.
    각 정답 자리에 **기대한 색의 점이 반경 안에 잡혔는가** + 그 점의 색 여유.
    못 찾은 자리는 그 자체로 큰 감점 — '4개 다 잡히는 설정'을 우선한다."""
    d = PD.detect(img, None)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    found, mars, miss = 0, [], []
    used = []
    for a in anchors:
        ax, ay, ac = a["x"], a["y"], a["color"]
        cands = [p for p in d.get(ac, []) if math.dist(p[:2], (ax, ay)) <= ANCHOR_R]
        if not cands:
            miss.append(ac + f"({ax:.0f},{ay:.0f})"); mars.append(0.0); continue
        p = min(cands, key=lambda q: math.dist(q[:2], (ax, ay)))
        used.append((p[0], p[1]))
        found += 1
        y0, y1 = max(0, int(p[1]) - 5), int(p[1]) + 6
        x0, x1 = max(0, int(p[0]) - 5), int(p[0]) + 6
        win = hsv[y0:y1, x0:x1].reshape(-1, 3)
        lo, hi = _ranges_of(ac)[0]
        inr = win[(win[:, 0] >= lo[0]) & (win[:, 0] <= hi[0]) &
                  (win[:, 1] >= lo[1]) & (win[:, 1] <= hi[1]) &
                  (win[:, 2] >= lo[2]) & (win[:, 2] <= hi[2])]
        mars.append(_margin(np.median(inr, axis=0), (lo, hi)) if len(inr) >= 5 else 0.0)

    # 유령: 정답 자리 어디에도 안 붙은 색 덩어리
    ghosts = 0
    for k, v in d.items():
        for p in v:
            if all(math.dist(p[:2], (a["x"], a["y"])) > ANCHOR_R for a in anchors):
                ghosts += 1
    frac = found / max(1, len(anchors))
    sc = max(0.0, frac * (min(mars) if mars else 0.0) - GHOST_PENALTY * ghosts)
    return sc, {"found": f"{found}/{len(anchors)}", "worst_margin": round(min(mars), 3) if mars else 0,
                "margins": [round(m, 3) for m in mars], "ghosts": ghosts, "miss": miss}


def grab_and_score_anchored(anchors, n=3):
    scs, dets = [], []
    for _ in range(n):
        img, _g = B.grab_pair()
        if img is None:
            continue
        sc, d = score_anchored(img, anchors)
        scs.append(sc); dets.append(d)
    if not scs:
        return 0.0, {"why": "프레임 없음"}
    return sum(scs) / len(scs), {"frames": len(scs), "scores": [round(x, 3) for x in scs],
                                 "detail": dets[-1]}


def cmd_anchor():
    """지금 검출된 4점을 '정답 위치'로 저장(사용자가 화면에서 확인해 준 자리)."""
    img, grid = B.grab_pair()
    m, why = B.detect_rect(img, grid)
    rect = PD.plate_rect(m)
    pts, why2 = PD.four_corners(img, rect)
    if not pts or len(pts) < 4:
        print(f"❌ 4점을 못 잡아 저장 불가: {why2}"); return
    an = [{"x": round(p[0], 1), "y": round(p[1], 1), "color": p[3]} for p in pts]
    tcp = None
    try:
        tcp = json.loads(UR.urlopen("http://127.0.0.1:8765/status", timeout=5).read())["robots"]["fr5"]["tcp"]
    except Exception:
        pass
    json.dump({"made": time.strftime("%Y-%m-%d %H:%M"), "anchors": an, "tcp": tcp,
               "note": "관측자세 화면좌표. 로봇 자세가 바뀌면 다시 잡을 것"},
              open(ANCHORS, "w"), ensure_ascii=False, indent=1)
    print("✅ 정답 위치 저장:", ANCHORS)
    for a in an:
        print(f"   {a['color']:7} ({a['x']},{a['y']})")


def current_settings():
    e = expo()
    return {k: e.get(k) for k in ("exposure", "gain", "bright", "wb", "awb") if k in e}


def cmd_check():
    A = load_anchors()
    okp, pw = at_anchor_pose()
    print(f"로봇 자세: {pw} {'✅' if okp else '❌ (관측자세가 아님 — 밑판이 화면에 없을 수 있음)'}")
    s, d, _ = grab_and_score(3)                 # 밑판 꼭짓점 기준(베이스 이동 추종)
    if A and A.get("anchors"):
        _sa, da = grab_and_score_anchored(A["anchors"], 1)
        print(f"  (참고) 저장된 정답 위치에서의 검출: {da.get('detail', {}).get('found')} "
              f"— 다르면 베이스가 움직인 것이지 색이 무너진 게 아닐 수 있다")
    cur = current_settings()
    print(f"현재 설정: {cur}")
    print(f"점수 {s:.3f}  (기준 {alarm_level()} 이상이면 안전)  {json.dumps(d, ensure_ascii=False)}")
    if os.path.exists(STORE):
        st = json.load(open(STORE))
        print(f"저장된 설정: {st.get('settings')}  점수 {st.get('score')}  ({st.get('made')})")
    return s


def cmd_calibrate():
    base = expo()
    cur_exp = base.get("exposure")
    print(f"현재: {current_settings()}")
    if cur_exp is None:
        print("⚠ 이 소스는 노출 조회 불가 — 밝기 목표로 훑는다")
        cands = [("bright", v) for v in (70, 85, 100, 115, 130)]
    else:
        # ★현재값 기준 배수로 훑으면 실행할 때마다 범위가 흘러간다(직전 실행의 결과에 끌려감).
        #   절대값 사다리로 고정해 항상 같은 구간을 본다. 9/5 실측: 노출 낮으면 점을 잃고
        #   높일수록 여유가 커져 166 이 상단 끝이었다 → 위쪽을 더 열어 둔다.
        # ★★플리커 안전값만 쓴다(9/5 재발견). start_cam.sh 에 이미 근거가 박혀 있었다:
        #   "CAM_EXPOSURE=166 = 조명 120Hz 플리커 안전값(다른 노출이면 도트가 35px 씩 흔들림, 실증)".
        #   내가 200 으로 올렸다가 프레임마다 검출이 2/4↔4/4 로 튀었다 — 밝기가 아니라 **깜빡임**이
        #   원인이었다. RealSense 노출 단위는 0.1ms 라 120Hz 주기(8.333ms) = 83.3 단위의 배수만
        #   한 주기를 정확히 담아 프레임 간 밝기가 일정해진다.
        FLICKER_UNIT = 83.33            # 1/120 초 (0.1ms 단위)
        # 9/7 아침: 직사광에서 83 도 이미 포화(83% 흰색)라 사다리가 전부 0점 → 저노출까지 넓힌다.
        #   실측 최적 gain16/set42(점 10개·포화 20%), 저녁은 gain64/set333.
        LADDER = (8, 20, 42) + tuple(round(FLICKER_UNIT * k) for k in (1, 2, 3, 4, 5, 6))
        cands = [("set", v) for v in LADDER]
    # ★밑판 꼭짓점 기준으로 채점한다(베이스가 움직여도 따라간다).
    #   사용자가 확인해 준 정답 위치는 '베이스가 그대로일 때' 참고용으로만 같이 찍는다.
    A = load_anchors()
    ank = A["anchors"] if A else None
    print("밑판 꼭짓점 기준으로 채점(베이스 이동 추종)" + (f" · 참고 정답 위치 {len(ank)}곳" if ank else ""))
    best = None
    print(f"\n{'설정':>16} {'점수':>7}  검출   여유   유령  못찾은 점")
    for key, val in cands:
        expo(**{key: val})
        time.sleep(1.2)                       # 노출 반영 대기
        s, d, _ = grab_and_score(3)
        if ank:
            _sa, da = grab_and_score_anchored(ank, 1)
            d.setdefault("detail", {})["정답위치검출"] = da.get("detail", {}).get("found")
        dd = d.get('detail', {})
        print(f"{key}={val:<10} {s:7.3f}  {dd.get('found', '?'):>5}  {str(dd.get('worst_margin')):>6}  "
              f"{str(dd.get('ghosts')):>4}  {','.join(dd.get('miss', [])) or '-'}")
        if best is None or s > best[0]:
            best = (s, key, val, d)
    s, key, val, d = best
    # ★★9/5 사고: 마커를 옮기는 동안(손·물체가 화면에 있음) 전 후보가 0점이었는데도
    #   그중 첫 값(노출 100)을 '최적'이라며 저장해 **잘 맞춰 둔 167 을 덮어썼다**.
    #   쓸모없는 결과로 좋은 기억을 지우면 안 된다 → 바닥값 미만이면 저장하지 않는다.
    if s < SCORE_FLOOR:
        print(f"\n❌ 최적값도 {s:.3f} < {SCORE_FLOOR} — 장면이 흐트러졌거나 조명 문제.")
        print("   저장하지 않는다(기존 기억 보존). 화면을 정리한 뒤 다시 실행할 것.")
        if os.path.exists(STORE):
            old_st = json.load(open(STORE))
            expo(**old_st["apply"]); time.sleep(1.2)
            print(f"   → 기존 설정 {old_st['apply']} 로 되돌림")
        return s
    expo(**{key: val}); time.sleep(1.2)
    st = {"made": time.strftime("%Y-%m-%d %H:%M"), "score": round(s, 3),
          "apply": {key: val}, "settings": current_settings(), "detail": d}
    json.dump(st, open(STORE, "w"), ensure_ascii=False, indent=1)
    print(f"\n✅ 최적 {key}={val} 점수 {s:.3f} → 저장 {STORE}")
    return s


def cmd_restore():
    if not os.path.exists(STORE):
        print("저장된 설정 없음 — 먼저 calibrate"); return 0.0
    st = json.load(open(STORE))
    expo(**st["apply"]); time.sleep(1.2)
    A = load_anchors()
    if A and A.get("anchors"):
        s, d = grab_and_score_anchored(A["anchors"], 3)
    else:
        s, d, _ = grab_and_score(3)
    print(f"복원 {st['apply']} → 지금 점수 {s:.3f} (저장 당시 {st['score']})")
    return s


def cmd_monitor(period=20.0):
    print(f"색점 감시 시작 — {period:.0f}초마다 점수 확인, {alarm_level()} 미만이면 복원")
    bad = 0
    while True:
        ok_pose, pw = at_anchor_pose()
        if not ok_pose:
            time.sleep(period); continue          # 관측자세가 아니면 조용히 넘어간다
        # ★베이스는 움직인다(ZK 배치 오차) → 감시는 밑판 꼭짓점 기준으로만 채점한다.
        #   화면 절대좌표(anchors)로 재면 베이스가 조금만 틀어져도 '색이 무너졌다'고
        #   오판해 노출을 헛되이 바꾼다.
        s, d, _ = grab_and_score(2)
        if s < alarm_level():
            bad += 1
            print(f"{time.strftime('%H:%M:%S')} 점수 {s:.3f} 낮음({bad}회) {d.get('detail', {}).get('why', '')}", flush=True)
            if bad == 3:
                # ★자동 복구는 '기억해 둔 설정으로 되돌리기'까지만 한다.
                #   9/5 사고: 자동 재탐색이 흐트러진 장면에서 돌아 좋은 설정을 덮어썼다.
                #   재탐색은 사람이 화면을 정리한 뒤 직접 돌릴 일이다.
                print(f"{time.strftime('%H:%M:%S')} → 저장 설정으로 복원", flush=True)
                s2 = cmd_restore()
                if s2 < alarm_level():
                    print(f"{time.strftime('%H:%M:%S')} ⚠ 복원해도 낮음 — 자동 재탐색은 하지 않는다. "
                          f"화면 정리 후 `color_lock.py calibrate` 를 직접 실행할 것", flush=True)
            if bad >= 3 and bad % 20 == 0:
                print(f"{time.strftime('%H:%M:%S')} ⚠ 낮은 점수 {bad}회 지속 — 확인 필요", flush=True)
        else:
            bad = 0
        time.sleep(period)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        cmd_check()
    elif mode == "calibrate":
        cmd_calibrate()
    elif mode == "anchor":
        cmd_anchor()
    elif mode == "restore":
        cmd_restore()
    elif mode == "monitor":
        cmd_monitor(float(sys.argv[2]) if len(sys.argv) > 2 else 20.0)
    else:
        print(__doc__)
