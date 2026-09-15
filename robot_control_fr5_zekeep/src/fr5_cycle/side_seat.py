#!/usr/bin/env python3
"""측면캠(:8771) 안착 판정 — 9/8 신설.

★왜 따로 만드나
  기존 안착 판정은 손목캠/새카메라로 **기둥**을 본다. 두 카메라는 손목에 붙어 있어서
  "안착 TCP ↔ 베이스 관계가 맞나"를 간접 확인하는 방식이다. 그래서
    · 안착 높이에서 기둥이 안 보이면 원리적으로 못 한다(red_s, red_in)
    · 로봇 자세·조명에 흔들린다(9/8 실측: 파랑 12.008px vs 허용 12px 로 0.008 차이 탈락,
      노랑은 빨간 점 3.6px 인데 파란 점만 24.3px)
  측면캠은 **고정**이다. 로봇이 어디 있든 화면이 안 변하고, 5프레임 반복 산포가
  0.04~0.2px 로 손목캠(3px)보다 한 자릿수 안정적이다. 무엇보다 **벽 자체를 직접 본다**.

★원리
  이 벽을 뺀 나머지 전부(기둥 점 + 이미 꽂힌 다른 벽의 점)가 곧 '베이스'다.
  기준 저장 때 그 점들(anchors)과 이 벽의 점(wall)을 같이 찍어 두고,
  판정 때 anchors 로 베이스의 이동·회전을 구해 기준 벽 점을 옮긴 뒤 실제와 비교한다.
  → 베이스가 움직여도 상대 비교라 무관하고, 로봇 자세와도 무관하다.

★벽 점을 어떻게 가려내나
  꽂기 전 한 장(pre, 로봇이 랙에 있어 화면 밖) · 꽂고 물러난 뒤 한 장(post, 로봇 SAFE).
  post 에만 새로 생긴 점 = 이 벽의 점. 둘 다 있는 점 = anchors.
"""
import json, math, os, time
import numpy as np
import hover_align as HA

REF = "/home/ar/bf2_console/state/house/side_seat_ref.json"
SRC = "side"
N_FRAME = 7                 # 중앙값용 프레임 수
CLUSTER_PX = 6.0            # 프레임 간 같은 점으로 묶는 거리
MIN_SEEN = 5                # N_FRAME 중 이만큼 잡혀야 '안정된 점'(하한을 낮춘 만큼 엄격하게)
# ★9/8 실측: 하한 30 은 경계에 걸려 있었다. red_s 벽 점의 면적 중앙값이 29·35 라 프레임마다
#   들락날락해 7개 중 4~6개만 잡혔다(위치는 1.5px 로 정확했는데 '일부 미검출'로 보류).
#   빨간 점은 면적 100 으로 안정적이고, 문제는 파란 점들이었다. 하한을 낮춰 검출을 보강한다.
AMIN = 12                   # 측면캠 색점 최소 면적
NEW_PX = 12.0               # pre 에 이만큼 안에 같은 색 점이 없으면 '새로 생긴 점'
MATCH_PX = 25.0             # 판정 때 기준점 ↔ 현재점 짝짓기 반경
SEAT_TOL_PX = 10.0          # ★안착 합격선(벽 점 잔차). 측면캠 산포가 0.2px 이므로 넉넉하지 않다.
ANCHOR_MIN = 4              # 베이스 이동을 풀려면 최소 이만큼의 anchor 가 짝지어져야
COLORS = ("blue", "yellow", "red")
# ★9/8 실측: 측면캠 전체를 보면 안정된 파랑이 25개나 잡히는데 창틀·기계 홈·화면 모서리처럼
#   **베이스가 아니라 세상에 고정된** 점이 대부분이다. 그걸 기준점으로 쓰면 베이스가 움직였을 때
#   변환이 오염된다 → 밑판이 놓이는 영역만 본다. (베이스를 크게 옮기면 이 값 재확인)
ROI = (330, 60, 1030, 640)      # x0, y0, x1, y1


def _one(img):
    x0, y0, x1, y1 = ROI
    out = []
    for c in COLORS:
        for x, y, a in HA._blobs(img, c, AMIN, SRC):
            if x0 <= x <= x1 and y0 <= y <= y1:
                out.append((c, float(x), float(y), int(a)))
    return out


def snapshot(n=N_FRAME):
    """여러 프레임 중앙값으로 안정된 색점만 남긴다. 반환 [(색, x, y, 면적, 관측횟수)]."""
    acc = []
    for _ in range(n):
        acc.append(_one(HA.grab(SRC)))
        time.sleep(0.15)
    flat = [p for fr in acc for p in fr]
    used, out = [False] * len(flat), []
    for i, p in enumerate(flat):
        if used[i]:
            continue
        grp = [p]; used[i] = True
        for j in range(i + 1, len(flat)):
            q = flat[j]
            if not used[j] and q[0] == p[0] and math.hypot(q[1] - p[1], q[2] - p[2]) <= CLUSTER_PX:
                grp.append(q); used[j] = True
        if len(grp) >= MIN_SEEN:
            out.append((p[0], float(np.median([g[1] for g in grp])),
                        float(np.median([g[2] for g in grp])),
                        int(np.median([g[3] for g in grp])), len(grp)))
    return out


def _near(pt, pool, r):
    """같은 색 중 r 안에서 가장 가까운 점."""
    c, x, y = pt[0], pt[1], pt[2]
    cand = [(math.hypot(q[1] - x, q[2] - y), q) for q in pool if q[0] == c]
    cand = [t for t in cand if t[0] <= r]
    return min(cand, key=lambda t: t[0])[1] if cand else None


def _fit_rigid(src, dst):
    """src → dst 강체변환(회전+평행이동, 최소자승). 반환 (R, t, rms)."""
    S = np.array([[p[0], p[1]] for p in src], float)
    D = np.array([[p[0], p[1]] for p in dst], float)
    cs, cd = S.mean(0), D.mean(0)
    H = (S - cs).T @ (D - cd)
    U, _, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    if np.linalg.det(R) < 0:
        Vt[-1] *= -1; R = Vt.T @ U.T
    t = cd - R @ cs
    res = (S @ R.T + t) - D
    return R, t, float(np.sqrt((res ** 2).sum(1).mean()))


def save_ref(color, pre, post=None, confirm=3):
    """pre(꽂기 전 snapshot) 와 지금(post) 을 비교해 이 벽의 점과 anchors 를 저장.

    ★9/8 실측 교훈: post 한 번만 보면 '로봇이 비켜나며 드러난 점'까지 새 점으로 잡힌다
      (red_s 첫 시도에서 12개 중 5개가 그런 점이라 판정이 7/12 로 보류됐다).
      → confirm 회 더 관측해 **매번 보이는 점만** 기준에 남긴다. 게이트를 푸는 게 아니라
        기준을 제대로 만드는 쪽이다.
    """
    post = post if post is not None else snapshot()
    wall = [p for p in post if _near(p, pre, NEW_PX) is None]
    anchors = [p for p in post if _near(p, pre, NEW_PX) is not None]
    for _ in range(max(0, confirm)):
        now = snapshot()
        wall = [w for w in wall if _near(w, now, MATCH_PX) is not None]
        anchors = [a for a in anchors if _near(a, now, MATCH_PX) is not None]
    if not wall:
        raise RuntimeError(f"[side] {color} 새로 생긴 점이 없다 — 벽이 안 꽂혔거나 측면캠에 안 보인다")
    if len(anchors) < ANCHOR_MIN:
        raise RuntimeError(f"[side] 기준점(anchors) {len(anchors)}개 < {ANCHOR_MIN} — 베이스 이동을 못 푼다")
    d = json.load(open(REF)) if os.path.exists(REF) else {}
    d[color] = {"wall": [[p[0], p[1], p[2], p[3]] for p in wall],
                "anchors": [[p[0], p[1], p[2], p[3]] for p in anchors],
                "made": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(d, open(REF, "w"), ensure_ascii=False, indent=1)
    return {"wall": len(wall), "anchors": len(anchors),
            "wall_px": [(round(p[1]), round(p[2])) for p in wall]}


def verify(color):
    """반환 {'state': 'seated_side'|'not_seated'|'unknown', 'px':…, 'why':…}. 예외 대신 unknown."""
    try:
        d = json.load(open(REF)) if os.path.exists(REF) else {}
        ref = d.get(color)
        if not ref:
            return {"state": "unknown", "why": f"[side] {color} 안착 기준 없음"}
        now = snapshot()
        if not now:
            return {"state": "unknown", "why": "[side] 색점이 하나도 안 잡힘"}
        ra = [(p[0], p[1], p[2]) for p in ref["anchors"]]
        pairs = [(a, _near(a, now, MATCH_PX)) for a in ra]
        pairs = [(a, b) for a, b in pairs if b is not None]
        if len(pairs) < ANCHOR_MIN:
            return {"state": "unknown",
                    "why": f"[side] 기준점 짝 {len(pairs)}개 < {ANCHOR_MIN} — 베이스 이동을 못 품"}
        R, t, rms = _fit_rigid([(a[1], a[2]) for a, _ in pairs], [(b[1], b[2]) for _, b in pairs])
        det = []
        for w in ref["wall"]:
            p = R @ np.array([w[1], w[2]], float) + t          # 베이스 이동을 반영한 기대 자리
            q = _near((w[0], float(p[0]), float(p[1])), now, MATCH_PX)
            det.append((w[0], float(p[0]), float(p[1]),
                        None if q is None else math.hypot(q[1] - p[0], q[2] - p[1])))
        seen = [e for e in det if e[3] is not None]
        if not seen:
            return {"state": "not_seated", "px": None,
                    "why": f"[side] 벽 점 {len(det)}개가 기대 자리에 하나도 없음(anchors {len(pairs)}개 rms {rms:.1f}px)"}
        worst = max(e[3] for e in seen)
        why = (f"[side] 벽 점 {len(seen)}/{len(det)}개 최대 {worst:.1f}px "
               f"(허용 {SEAT_TOL_PX}, anchors {len(pairs)}개 rms {rms:.1f}px)")
        if len(seen) < len(det):
            return {"state": "unknown", "px": worst, "why": why + " — 일부 점 미검출"}
        return {"state": "seated_side" if worst <= SEAT_TOL_PX else "not_seated",
                "px": worst, "why": why}
    except Exception as ex:
        return {"state": "unknown", "why": f"[side] {type(ex).__name__}: {ex}"}
