#!/usr/bin/env python3
"""기둥 꼭대기 색점 검출 — 9/5 신설.

왜 새로 만드나:
  `base_depth_corner.base_dots` 는 **9/3 배치**(기둥 꼭대기 = 흰 점)를 전제로 짜여 있다.
  9/5 현재 실물 배치는 기둥 꼭대기가 **색점**이고, 빨강도 H≈147(분홍빛)이라
  base_dots 의 빨강 범위(H 0~12 / 160~180)로는 원리적으로 안 잡힌다(실측 0개).

색이 다르다는 점이 오히려 이득이다:
  네 모서리가 색으로 구분되므로 **직사각형의 180° 뒤집힘이 원천적으로 사라진다**
  (흰 점 4개였을 때는 모양이 같아 yaw 힌트로만 풀 수 있었다).

실측 색(9/5 관측자세, D435 손목캠):
    파랑  H 104  S 216~231  V 164~180   (좌상·좌하 두 곳)
    노랑  H  30  S 107      V 175       (우상)
    빨강  H 147  S 139      V 110       (우하)  ← 분홍빛이라 통상 빨강 범위 밖

  python3 pillar_dots.py            # 현재 프레임에서 검출 결과 출력
  python3 pillar_dots.py --save out.jpg
"""
import sys, math
import numpy as np
import cv2

sys.path.insert(0, "/home/ar/bf2_console/tools")

# 실측 기반 범위 — 넉넉하게 잡되 서로 겹치지 않게.
# ★배경(책상)이 H 109~115·S 63~103·V 250 로 **파랑 색상 범위 안**이다(9/5 실측).
#   진짜 파랑 점은 S 224~227·V 173~178 → 채도 하한과 명도 상한으로 가른다.
RANGES = {
    # ★V 하한 140: 검은 기둥 막대 자체가 H106·S200대의 '어두운 파랑'(V 51~121)으로 잡혀
    #   진짜 점(V 173~195)과 한 덩어리가 되고, 열림 연산에서 조각나 검출을 놓쳤다(9/5 실측).
    # ★V 상한 235 → 255 (9/5): 점 위 반사광이 235 를 넘어 그 화소가 마스크에서 빠지면서
    #   덩어리 폭이 15↔8px 로 잘렸다 붙었다 했고, 그 탓에 무게중심이 프레임마다 2.2px 흔들려
    #   위치 반복정밀도가 0.036→0.311mm 로 무너졌다(벽 1개 조건 실측).
    #   상한은 흰 책상을 빼려고 넣었지만 책상은 S≤103 이라 **채도만으로 이미 배제**된다 — 불필요했다.
    "blue":   ((95, 150, 140), (115, 255, 255)),  # S150↑ 로 흰 책상(S≤103) 제외
    # ★9/5: 좌하 기둥 파랑점이 국소 그늘로 V 29~61(다른 파랑 V140+) → 정상 밴드로 못 잡음.
    #   '어둡지만 완전포화(S235+)' 2차 밴드로 그늘 점만 살린다. 어두운 기둥 몸통은 S200~230 이라 안 걸림(실측 오검출 0).
    "blue_dark": ((96, 235, 25), (116, 255, 150)),
    "yellow": ((15, 90, 110), (38, 255, 255)),
    # ★9/6 19:1x 실측: 빨강 점 픽셀 H 170~175 로 상한 175 에 걸쳐 점의 절반이 잘리고 2~5조각으로 쪼개짐
    #   → 중심이 σy 13px 로 튀고 10프레임 중 3회 미검출. 빨강은 H 가 180→0 으로 감기므로 두 구간 합집합으로.
    #   실측(z470 같은 프레임): 현재 200+115 조각 → 합집합 2324 하나 (파랑 1473·1318, 노랑 1936 과 같은 수준)
    "red":    [((135, 100, 55), (179, 255, 255)), ((0, 100, 55), (6, 255, 255))],
}
AREA_MIN, AREA_MAX = 40, 3200   # 9/6 19:1x: 빨강 랩어라운드 수정으로 점이 온전해지자 근거리(z470)에서 2324px² → 옛 상한 1800 에 걸려 사라짐. 같은 프레임 노랑 1936·파랑 1473
CORNER_R_PX = 90          # 밑판 꼭짓점에서 이 반경 안의 색점만 기둥 후보로 인정
FIT_REJECT_MM = 4.0       # 모델(200×130) 맞춤 오차가 이보다 크면 기둥 조합이 아니라고 본다
ASPECT = (0.5, 2.0)


def _blobs(hsv, gray, lo, hi):
    m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    out = []
    for i in range(1, n):
        a = int(st[i, 4]); w, h = int(st[i, 2]), int(st[i, 3])
        if not (AREA_MIN < a < AREA_MAX):
            continue
        if not (ASPECT[0] < w / max(1, h) < ASPECT[1]):
            continue
        ys, xs = np.nonzero(lab == i)
        # 밝기 가중 무게중심(서브픽셀)
        wts = gray[ys, xs].astype(float) + 1.0
        out.append((float((xs * wts).sum() / wts.sum()),
                    float((ys * wts).sum() / wts.sum()), a))
    return out


def detect(img, rect=None):
    """반환 {"blue":[(x,y,a)], "yellow":[...], "red":[...]}.
    rect(밑판 사각형)가 주어지면 **밑판 바깥**의 점만 남긴다 — 기둥은 밑판 모서리 바깥에 있고,
    밑판 위의 색 자국·반사를 배제한다."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    def _multi(r):                                   # 색당 (lo,hi) 하나 또는 [(lo,hi), ...] 합집합
        rs = r if isinstance(r, list) else [r]
        acc = []
        for lo, hi in rs:
            acc += _blobs(hsv, gray, lo, hi)
        if len(rs) == 1:
            return acc
        acc.sort(key=lambda p: -p[2])                # 랩어라운드 두 구간이 같은 점을 각각 잡으면 근접 중복 제거
        keep = []
        for p in acc:
            if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 12 for q in keep):
                keep.append(p)
        return keep
    out = {k: _multi(r) for k, r in RANGES.items() if k != "blue_dark"}
    # ★그늘 파랑점 병합(중복은 근접 제거)
    for p in _blobs(hsv, gray, *RANGES["blue_dark"]):
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 12 for q in out["blue"]):
            out["blue"].append(p)
    # ★기둥은 밑판 '바깥'이 아니다 — STL 상 기둥 중심은 밑판 가장자리에서 5mm **안쪽**이다
    #   (COLUMN_CENTERS (5,5)… vs 밑판 210×140). 9/5: 바깥만 남기던 필터가 빨강 점을 통째로
    #   지워버려 검출 0개였다. 따라서 '밑판 꼭짓점 근처'로 거른다.
    if rect is not None:
        poly = np.array(rect, np.float32)
        for k in out:
            out[k] = [d for d in out[k]
                      if min(math.dist(d[:2], (c[0], c[1])) for c in poly) <= CORNER_R_PX]
    for k in out:
        out[k].sort(key=lambda d: -d[2])
    return out


# ★원뿔 폭 38° → 75° (9/5 실측으로 상향). 두 가지가 드러났다:
#   ① 아래쪽 기둥 점은 꼭짓점에서 21~40px 로 아주 가까워, 작은 어긋남에도 각도가 크게 튄다
#      (우하 27.9~62.2°, 좌하 3.7~56.1°). 38° 로는 멀쩡한 점을 간헐적으로 잘라냈다.
#   ② 벽 점도 28.1° 로 원뿔 안에 들어온다 — 즉 **각도로는 어차피 벽을 못 가른다**.
#   → 원뿔은 '대략 그 모서리 근처' 만 거르는 거친 필터로 쓰고, 실제 판별은
#     **꼭짓점별 색**(EDGE_WALL_COLORS 와 절대 충돌하지 않음이 증명됨)과 모양 검증이 한다.
CONE_DEG = 75.0
CORNER_MIN_PX, CORNER_MAX_PX = 10.0, 130.0   # 꼭짓점에서 기둥 점까지 허용 거리
# ★★색 배치표 — 사용자 확인 9/5. 이 표가 판별의 근거이므로 실물이 바뀌면 반드시 같이 고칠 것.
#
#   기둥(꼭짓점)      좌상 파랑 · 우상 노랑 · 좌하 파랑 · 우하 빨강
#   벽(모서리)        오른쪽=파랑(흰 바탕) · 아래=노랑(검은 바탕) · 왼쪽=빨강 · 위=빨강
#
#   ★이 배치는 **꼭짓점마다 기둥 색이 그 모서리에 닿는 두 벽의 색과 겹치지 않는다**:
#       좌상 파랑 ↔ 위·왼(빨강)      우상 노랑 ↔ 위(빨강)·오른(파랑)
#       좌하 파랑 ↔ 왼(빨강)·아래(노랑)  우하 빨강 ↔ 아래(노랑)·오른(파랑)
#   → 네 벽이 다 꽂혀도 '바깥 대각 위치 + 꼭짓점별 색' 만으로 원리적으로 안 헷갈린다.
#     (실측 9/5: 우상 꼭짓점에 노랑 기둥점과 파랑 벽점이 나란히 있어도 정확히 갈렸다)
#   ⚠단, **벽이 정해진 자리에 꽂힐 때만** 성립한다. 예: 파란 벽을 왼쪽에 꽂으면 좌상·좌하와 충돌.
#     그 경우를 아래 _collision_warn() 이 잡아 경고한다.
CORNER_COLORS = ("yellow", "red", "blue", "blue")     # (5,5) (205,5) (205,135) (5,135)
CORNER_MODEL = ((5.0, 5.0), (205.0, 5.0), (205.0, 135.0), (5.0, 135.0))
# 모델 꼭짓점 ↔ 화면상 위치 대응(CORNER_COLORS 의 색 순서로 결정됨):
#   모델0(5,5)=화면 우상(노랑) · 모델1(205,5)=우하(빨강) · 모델2(205,135)=좌하(파랑) · 모델3(5,135)=좌상(파랑)
# 모서리(모델 꼭짓점 i → i+1)에 꽂히는 벽의 점 색
EDGE_WALL_COLORS = {(0, 1): "blue",    # 우상-우하 = 오른쪽 벽(흰 바탕·파랑 점)
                    (1, 2): "yellow",  # 우하-좌하 = 아래쪽 벽(검은 바탕·노랑 점)
                    (2, 3): "red",     # 좌하-좌상 = 왼쪽 벽(검은 바탕·빨강 점)
                    (3, 0): "red"}     # 좌상-우상 = 위쪽 벽(검은 바탕·빨강 점)


def _collision_warn():
    """색 배치가 '꼭짓점마다 충돌 없음'을 만족하는지 자체 점검. 어기면 경고 문자열."""
    bad = []
    for i, col in enumerate(CORNER_COLORS):
        adj = [EDGE_WALL_COLORS.get((i, (i + 1) % 4)),
               EDGE_WALL_COLORS.get(((i - 1) % 4, i))]
        if col in adj:
            bad.append(f"꼭짓점{i}({col}) ↔ 인접 벽 {adj}")
    return "; ".join(bad)


def plate_rect(m):
    """★기둥 배정용 밑판 사각형 — 반드시 뎁스 높이대역 box 를 우선한다(9/5 실측).
    detect_rect 의 'corners'(색 경계 서브픽셀 정제)는 검은 벽이 꽂히면 벽 바깥 가장자리를
    밑판 변으로 잡아 아래 변이 22px 밀리고, 꼭짓점이 파란 기둥 점 16px 옆에 와 배정이 무너진다.
    'box' 는 뎁스 높이대역(밑판 6mm, 벽 80mm 는 제외) 기반이라 벽에 흔들리지 않는다."""
    if not m:
        return None
    b = m.get("box")
    if b is not None and len(b) == 4:
        return [(float(c[0]), float(c[1])) for c in b]
    cs = [c for c in (m.get("corners") or []) if c]
    return [(float(c[0]), float(c[1])) for c in cs] if len(cs) == 4 else None


def _order_ccw(pts):
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def four_corners(img, rect, yaw_tol_deg=45.0):
    """기둥 4점을 색 이름표와 함께 돌려준다.

    ★판별 원리(9/5 사용자 지적으로 확정):
      **색만으로는 못 가른다** — 파랑은 물론 노랑·빨강도 벽에 붙어 있는 색이다(실측: 벽 파랑과
      기둥 파랑은 H·S·V 가 완전히 겹친다). 그러니 **위치가 기준**이어야 한다.

        ① 기둥 점은 밑판 꼭짓점에서 **바깥 대각 방향**(±38°, 15~120px)에 있다.
           벽 점은 벽을 따라, 즉 **변 위**에 놓인다 → 이 원뿔 밖이라 후보에서 빠진다.
        ② 꼭짓점마다 색이 정해져 있고(노랑·빨강·파랑·파랑), 베이스는 ZK 배치 오차로 틀어져도
           **90° 넘게 돌지 않는다** → 꼭짓점↔색 대응이 뒤바뀌지 않는다. 이걸 확인에 쓴다.
        ③ 마지막으로 4점이 200×130 모델에 맞는지 검사한다.

      즉 위치로 후보를 좁히고(①), 색으로 짝을 확정하고(②), 모양으로 검증한다(③).

    반환 (pts, why) — pts = [(x, y, area, color)] 모델 꼭짓점 순서((5,5),(205,5),(205,135),(5,135))."""
    import house_geometry as HG

    if rect is None or len(rect) != 4:
        return None, "밑판 사각형 없음 — 위치 기준 판별 불가"
    d = detect(img, None)                     # 원뿔 조건으로 거를 것이므로 여기선 안 자른다
    ring = _order_ccw([(float(c[0]), float(c[1])) for c in rect])
    ctr = (sum(c[0] for c in ring) / 4.0, sum(c[1] for c in ring) / 4.0)

    # ① 꼭짓점별로 '바깥 대각 원뿔' 안의 색점 후보 수집
    per_corner = []
    for c in ring:
        ux, uy = c[0] - ctr[0], c[1] - ctr[1]
        L = math.hypot(ux, uy) or 1.0
        ux, uy = ux / L, uy / L
        cand = []
        for col, lst in d.items():
            for p in lst:
                vx, vy = p[0] - c[0], p[1] - c[1]
                r = math.hypot(vx, vy)
                if not (CORNER_MIN_PX <= r <= CORNER_MAX_PX):
                    continue
                if (vx * ux + vy * uy) / r < math.cos(math.radians(CONE_DEG)):
                    continue                  # 변 방향(벽 점)은 여기서 탈락
                cand.append((col, p, r))
        per_corner.append(cand)

    # ①-b ★벽 점 배제(9/5 3벽 실측: 벽 끝 점이 꼭짓점 원뿔 안에 들어와 기둥으로 오인).
    #   벽 점은 변을 따라 같은 색이 40~150px 간격으로 여러 개 늘어서고, 기둥 점은 홀로 있다(같은 색 기둥은 200mm=490px 밖).
    allpts = [(col, p) for col, lst in d.items() for p in lst]
    def is_wall_dot(col, p):
        for col2, q in allpts:
            if col2 != col or q is p:
                continue
            dist = math.dist(p[:2], q[:2])
            if 40.0 <= dist <= 150.0:
                return True
        return False
    per_corner = [[t for t in cc if not is_wall_dot(t[0], t[1])] for cc in per_corner]

    # ② 꼭짓점↔색 배치를 순환 이동 4가지로 맞춰 본다. 4점이 다 있으면 4점, 한 꼭짓점이 비면 3점 폴백.
    best = None
    for shift in range(4):
        chosen, missing = [], []
        for i in range(4):
            want = CORNER_COLORS[(i + shift) % 4]
            cand = [t for t in per_corner[i] if t[0] == want]
            if not cand:
                chosen.append(None); missing.append(i); continue
            cand.sort(key=lambda t: (t[2], -t[1][2]))
            chosen.append((want, cand[0][1]))
        if len(missing) > 1:
            continue
        idx = [i for i in range(4) if chosen[i] is not None]
        meas = [chosen[i][1][:2] for i in idx]
        model = tuple(CORNER_MODEL[(i + shift) % 4] for i in idx)
        me = [math.dist(model[a], model[(a + 1) % len(model)]) for a in range(len(model))]
        pe = [math.dist(meas[a], meas[(a + 1) % len(meas)]) for a in range(len(meas))]
        den = sum(v * v for v in pe)
        if den <= 0:
            continue
        sc = sum(a * b for a, b in zip(me, pe)) / den
        _pose, rms = HG.fit_base_pose([(q[0] * sc, q[1] * sc) for q in meas], model)
        # 4점 완전 일치를 3점보다 우선, 같은 급이면 rms
        key = (len(missing), rms)
        if best is None or key < best[0]:
            best = (key, chosen, shift, model, rms, missing)

    if best is None:
        got = [sorted(set(t[0] for t in cc)) for cc in per_corner]
        return None, ("꼭짓점별 색 배치가 안 맞음 — 꼭짓점 후보색(벽 점 제외) " + str(got) +
                      f" (기대 {list(CORNER_COLORS)} 의 순환)")
    _key, chosen, shift, model, rms, missing = best
    if rms > FIT_REJECT_MM:
        return None, f"모양 불일치 rms {rms:.2f}mm > {FIT_REJECT_MM}mm — 기둥 점이 아닐 수 있다"

    # 모델 꼭짓점 순서((5,5) 부터)로 정렬해 돌려준다
    out = [None] * 4
    for i, ch in enumerate(chosen):
        if ch is None:
            continue
        col, p = ch
        out[(i + shift) % 4] = (p[0], p[1], p[2], col, (i + shift) % 4)
    out = [o for o in out if o is not None]           # 3점 폴백이면 3개(모델 순서 유지)
    return out, (f"기둥 3점(꼭짓점 {missing} 가려짐)" if missing else None)


def main():
    import base_depth_corner as B
    img, grid = B.grab_pair()
    if img is None:
        print("프레임 없음"); return
    try:
        m, why = B.detect_rect(img, grid)
    except Exception as e:
        m, why = None, str(e)
    rect = None
    if m:
        cs = [c for c in (m.get("corners") or []) if c]
        rect = cs if len(cs) == 4 else (m.get("box") or None)
    print(f"밑판 사각형: {'OK' if rect is not None else '없음 (' + str(why) + ')'}")
    d = detect(img, rect)
    for k, v in d.items():
        print(f"[{k}] {len(v)}개 " + " ".join(f"({p[0]:.1f},{p[1]:.1f})a{p[2]}" for p in v[:4]))
    pts, why2 = four_corners(img, rect)
    if pts:
        print(f"\n기둥 4점: {len(pts)}개" + (f"  ⚠ {why2}" if why2 else "  ✅"))
        for p in pts:
            print(f"   {p[3]:7} ({p[0]:7.1f},{p[1]:7.1f}) 면적 {p[2]}")
        if len(pts) == 4:
            print("  변 길이(px):")
            import itertools
            for a, b in itertools.combinations(range(4), 2):
                print(f"    {pts[a][3]}–{pts[b][3]}: {math.dist(pts[a][:2], pts[b][:2]):7.1f}")
    else:
        print("\n❌", why2)
    if "--save" in sys.argv:
        out = img.copy()
        col = {"blue": (255, 0, 0), "yellow": (0, 255, 255), "red": (0, 0, 255)}
        for k, v in d.items():
            for p in v:
                cv2.circle(out, (int(p[0]), int(p[1])), 12, col[k], 2)
                cv2.putText(out, k[0].upper(), (int(p[0]) + 14, int(p[1])),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, col[k], 2)
        cv2.imwrite(sys.argv[sys.argv.index("--save") + 1], out)
        print("저장:", sys.argv[sys.argv.index("--save") + 1])


if __name__ == "__main__":
    main()
