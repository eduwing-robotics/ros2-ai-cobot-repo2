#!/usr/bin/env python3
"""9/3 뎁스 유도 밑판 꼭짓점: 뎁스 격자(돌출 블록 = 바닥보다 가까움)로 밑판 마스크 → 경계 변 → 컬러 서브픽셀 직선 → 교점.
  python3 base_depth_corner.py [--save out.jpg]   (손목캠 라이브)   /  --pair color.jpg grid.json (저장본)"""
import sys, math, json, urllib.request
import cv2, numpy as np
from edge_anchor import _subpix_line

RAISE_MM = 15.0      # 바닥보다 이만큼 가까우면 밑판(돌출)
PLATE_BAND = 12.0    # 밑판 윗면 뎁스 ± 이 대역만 밑판으로(기둥 제외)
# ★9/7 21:1x 사용자 지시("내벽하고 외벽 조건 따로 써 이러다 외벽 망가지면 힘들어"):
#   내벽 대응(밑판 윗면이 내벽에 끊겨 두 칸이 되는 경우)은 기본 OFF. 외벽만 있을 때는 아래 두 곳이
#   전혀 실행되지 않아 코드 경로가 예전과 완전히 같다. 기둥 검출이 실패했을 때만 켜서 한 번 더 본다.
INNER_WALL_MODE = False
NX, NY = 128, 72


def grab_pair():
    buf = urllib.request.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    grid = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8766/depthgrid?x0=0&y0=0&x1=1279&y1=719&nx={NX}&ny={NY}&r=2", timeout=8).read())
    return img, grid


def depth_mask(grid, shape):
    """바닥 평면(최소자승, 2회 반복으로 돌출 제외) 대비 높이 → 밑판 윗면 대역만 마스크. 반환 (마스크, 바닥평면 중앙값)."""
    pts = grid["pts"]
    xs = sorted({p["x"] for p in pts}); ys = sorted({p["y"] for p in pts})
    D = np.full((len(ys), len(xs)), np.nan)
    xi = {x: i for i, x in enumerate(xs)}; yi = {y: i for i, y in enumerate(ys)}
    for p in pts:
        if p["d"] and 100 < p["d"] < 1000:
            D[yi[p["y"]], xi[p["x"]]] = p["d"]
    valid = ~np.isnan(D)
    YY, XX = np.mgrid[0:len(ys), 0:len(xs)]
    med = np.nanmedian(D)
    cand = valid & (np.abs(D - med) < 80)                   # 책상 밖(바닥 884+ 등) 제외
    P = np.c_[XX[cand], YY[cand], np.ones(cand.sum())]; d = D[cand]
    rng = np.random.default_rng(0); best, best_n = None, -1
    for _ in range(300):                                    # ★RANSAC 바닥 평면(임계 4mm): 밑판(+55mm)·기둥·가까운 바닥 편향 배제
        idx = rng.choice(len(d), 3, replace=False)
        try:
            coef = np.linalg.solve(P[idx], d[idx])
        except np.linalg.LinAlgError:
            continue
        n_in = int((np.abs(P @ coef - d) < 4.0).sum())
        if n_in > best_n:
            best, best_n = coef, n_in
    inl = np.abs(P @ best - d) < 4.0
    coef, *_ = np.linalg.lstsq(P[inl], d[inl], rcond=None)   # 인라이어로 최종 최소자승
    plane = coef[0] * XX + coef[1] * YY + coef[2]
    height = plane - D
    depth_mask.plane_inliers = int(inl.sum())
    raised = valid & (height > RAISE_MM)
    vals = height[raised]
    if vals.size < 20:
        return None, float(np.nanmedian(plane))
    hist, edges = np.histogram(vals, bins=int(max(5, (vals.max() - vals.min()) / 2.0)))
    top = 0.5 * (edges[np.argmax(hist)] + edges[np.argmax(hist) + 1])   # 밑판 윗면 높이(최빈)
    m = (valid & (np.abs(height - top) < PLATE_BAND)).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    if n < 2:
        return None, float(np.nanmedian(plane))
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    small = (lab == i).astype(np.uint8) * 255
    # ★9/7 21:0x 내벽 추가로 처음 나온 문제: 내벽(4mm)이 밑판 윗면 대역을 위/아래 두 조각으로 끊는다.
    #   여기서 '가장 큰 조각 하나'만 남기던 탓에 아래 칸만 밑판이 되어 사각형이 반쪽(y290~599)이 되고,
    #   위쪽 두 꼭짓점에 기둥 점이 없어 색 배치 검증이 무너져 기둥 0/4 가 났다.
    #   → 큰 조각의 테두리를 3칸 넓힌 범위 안에 있고 면적이 25% 이상인 조각만 함께 밑판으로 본다.
    #   온전한 밑판(외벽만 꽂힌 경우)은 조각이 하나라 이 루프가 아무것도 더하지 않는다 = 기존 경로 그대로.
    def _box(k):
        return (st[k, cv2.CC_STAT_LEFT], st[k, cv2.CC_STAT_TOP],
                st[k, cv2.CC_STAT_LEFT] + st[k, cv2.CC_STAT_WIDTH],
                st[k, cv2.CC_STAT_TOP] + st[k, cv2.CC_STAT_HEIGHT])

    def _adjacent(a, b, gap=4):
        """벽 하나(≤ 몇 칸)를 사이에 두고 한 축으로 이어지는가 — 한 축은 절반 넘게 겹치고 다른 축의 틈이 gap 이하."""
        ax0, ay0, ax1, ay1 = a; bx0, by0, bx1, by1 = b
        ox = min(ax1, bx1) - max(ax0, bx0); oy = min(ay1, by1) - max(ay0, by0)
        gx = max(0, max(ax0, bx0) - min(ax1, bx1)); gy = max(0, max(ay0, by0) - min(ay1, by1))
        if ox > 0.5 * min(ax1 - ax0, bx1 - bx0) and gy <= gap:
            return True
        return oy > 0.5 * min(ay1 - ay0, by1 - by0) and gx <= gap

    _a0 = st[i, cv2.CC_STAT_AREA]; _b0 = _box(i)
    for _k in range(1, n) if INNER_WALL_MODE else ():
        if _k == i or st[_k, cv2.CC_STAT_AREA] < 0.25 * _a0:
            continue
        if _adjacent(_b0, _box(_k)):
            small[lab == _k] = 255
    full = cv2.resize(small, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    depth_mask.last = {"plate_h": float(top), "plane_med": float(np.nanmedian(plane))}
    return full, float(np.nanmedian(plane))


def detect(img, grid):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask, floor = depth_mask(grid, gray.shape)
    if mask is None:
        return None, "뎁스 돌출 영역 없음"
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cs, key=cv2.contourArea)
    ap = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True).reshape(-1, 2)
    H, W = gray.shape
    lines = []
    for i in range(len(ap)):
        p, q = ap[i], ap[(i + 1) % len(ap)]
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        if L < 100:
            continue
        # 프레임 테두리에 붙은 변(잘린 변)은 제외
        if all(v <= 12 or v >= W - 12 for v in (p[0], q[0])) or all(v <= 12 or v >= H - 12 for v in (p[1], q[1])):
            continue
        r = _subpix_line(gray, (int(p[0]), int(p[1]), int(q[0]), int(q[1])), band=14, step=2)   # 뎁스 경계는 ±10px 거칠어 밴드 넓게
        if r and r[1] < 1.0:
            lines.append({"len": int(L), "ang": round(r[0], 3), "sigma": round(r[1], 2), "n": r[2], "v": r[3], "p": tuple(int(v) for v in p), "q": tuple(int(v) for v in q)})
    corners = []
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = lines[i], lines[j]
            d = abs(((a["ang"] - b["ang"]) + 180) % 180)
            if not (60 < d < 120):
                continue
            vx1, vy1, x1, y1 = a["v"]; vx2, vy2, x2, y2 = b["v"]
            A = np.array([[vx1, -vx2], [vy1, -vy2]], float); rhs = np.array([x2 - x1, y2 - y1], float)
            if abs(np.linalg.det(A)) < 1e-6:
                continue
            t = np.linalg.solve(A, rhs); cx, cy = x1 + vx1 * t[0], y1 + vy1 * t[0]
            near = min(math.hypot(cx - e[0], cy - e[1]) for e in (a["p"], a["q"], b["p"], b["q"]))
            if near < 80 and 0 <= cx < W and 0 <= cy < H:
                corners.append({"xy": (round(float(cx), 2), round(float(cy), 2)), "edges": (i, j), "sig": max(a["sigma"], b["sigma"])})
    return {"floor_mm": round(float(floor), 1), "poly": ap.tolist(), "n_edges": len(lines),
            "edges": [{k: v for k, v in l.items() if k in ("len", "ang", "sigma", "n", "p", "q")} for l in lines],
            "corners": corners}, None


if __name__ == "__main__":
    if "--pair" in sys.argv:
        i = sys.argv.index("--pair"); img = cv2.imread(sys.argv[i + 1]); grid = json.load(open(sys.argv[i + 2]))
    else:
        img, grid = grab_pair()
        S = "/tmp/robot_cell"
        cv2.imwrite(f"{S}/live_color.jpg", img); json.dump(grid, open(f"{S}/live_grid.json", "w"))
    r, why = detect(img, grid)
    print(json.dumps(r, ensure_ascii=False) if r else f"실패: {why}")
    if "--save" in sys.argv and r:
        out = img.copy()
        cv2.polylines(out, [np.array(r["poly"], np.int32).reshape(-1, 1, 2)], True, (0, 255, 0), 1)
        for e in r["edges"]:
            cv2.line(out, e["p"], e["q"], (255, 0, 0), 2)
        for c in r["corners"]:
            cv2.circle(out, (int(c["xy"][0]), int(c["xy"][1])), 9, (0, 0, 255), 2)
        cv2.imwrite(sys.argv[sys.argv.index("--save") + 1], out)


def detect_rect(img, grid):
    """★밑판 = 마스크 최소외접사각형 → 네 변을 컬러 서브픽셀 직선으로 정제 → 네 꼭짓점·yaw·중심."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask, floor = depth_mask(grid, gray.shape)
    if mask is None:
        return None, "뎁스 돌출 영역 없음"
    # ★9/7 21:0x: 내벽이 밑판 윗면을 가로지르며 남긴 좁은 이음매(≈10px)를 OPEN(61) 이 끊어 버려
    #   아래 칸만 밑판이 됐다. 먼저 그 폭만 메우고(21px) 그다음에 가는 돌출부를 제거한다.
    #   온전한 밑판 마스크는 메울 틈이 없어 CLOSE 가 아무것도 바꾸지 않는다 = 외벽만 있을 때 경로 그대로.
    if INNER_WALL_MODE:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((61, 61), np.uint8))   # ★꽂힌 벽 등 가는 돌출부 제거(밑판 본체만)
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cs, key=cv2.contourArea)
    # ★9/7 20:5x 내벽 추가로 처음 나온 문제: 내벽(4mm)이 밑판 테두리를 가로질러 마스크를 위/아래 두 조각으로
    #   끊는다 → 가장 큰 조각(아래 칸)만 밑판으로 잡혀 사각형이 (510,300)~(779,599) 로 반쪽이 되고,
    #   위쪽 두 꼭짓점에 기둥 점이 없어 색 배치 검증이 무너져 기둥 0/4 가 된다.
    #   조각났을 때만(가장 큰 조각 < 전체의 70%) 그 틈을 메워 다시 잡는다 — 온전한 밑판은 조각이 하나라
    #   비율 1.0 이므로 외벽만 있을 때의 경로는 그대로다.
    box = cv2.boxPoints(cv2.minAreaRect(c))                 # 4점(대략, 격자 해상도 ±10px)
    # ★순서 고정 [TL, TR, BR, BL] — measure() 가 corners[2]=BL, corners[3]=TL, lines[2]=아래변, lines[3]=왼쪽변 으로 가정
    bx = box[np.argsort(box[:, 0])]; left = bx[:2][np.argsort(bx[:2][:, 1])]; right = bx[2:][np.argsort(bx[2:][:, 1])]
    box = np.array([left[0], right[0], right[1], left[1]], np.float32)   # TL(왼쪽 위), TR, BR, BL
    H, W = gray.shape
    lines = []
    for i in range(4):
        p, q = box[i], box[(i + 1) % 4]
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        if L < 80:
            lines.append(None); continue
        # 변의 양 끝 12% 는 기둥·모서리 라운딩 영역이라 제외하고 가운데만 샘플
        p2 = p + (q - p) * 0.12; q2 = q - (q - p) * 0.12
        if not (0 <= p2[0] < W and 0 <= q2[0] < W and 0 <= p2[1] < H and 0 <= q2[1] < H):
            lines.append(None); continue
        r = _subpix_line(gray, (float(p2[0]), float(p2[1]), float(q2[0]), float(q2[1])), band=16, step=2)
        lines.append(None if (r is None or r[1] > 1.2) else {"len": int(L), "ang": r[0], "sigma": r[1], "n": r[2], "v": r[3]})
    # ★평행 제약: 불량 변(σ>0.3 또는 맞은편과 각도차>2°)은 맞은편 변 방향으로 고정하고 오프셋만 중앙값으로 재추정
    def _par_refit(i):
        j = (i + 2) % 4
        ref = lines[j]
        if ref is None:
            return None
        vx, vy = ref["v"][0], ref["v"][1]
        p, q = box[i], box[(i + 1) % 4]
        p2 = p + (q - p) * 0.12; q2 = q - (q - p) * 0.12
        # 변 중앙을 지나며 ref 방향인 세그먼트 주변에서 법선 방향 교차점 샘플
        cx, cy = (p2 + q2) / 2; L = math.hypot(*(q2 - p2))
        seg = (cx - vx * L / 2, cy - vy * L / 2, cx + vx * L / 2, cy + vy * L / 2)
        r = _subpix_line(gray, seg, band=20, step=2)
        if r is None:
            return None
        # 방향은 ref 로 고정, 위치는 샘플 점들의 법선 오프셋 중앙값
        nx, ny = -vy, vx
        pts = np.array([[r[3][2], r[3][3]]])   # fitLine 의 대표점만 있으므로 오프셋은 그 점 기준
        off = float((pts[0][0] - cx) * nx + (pts[0][1] - cy) * ny)
        x0, y0 = cx + nx * off, cy + ny * off
        return {"len": int(L), "ang": math.degrees(math.atan2(vy, vx)), "sigma": r[1], "n": r[2], "v": (vx, vy, x0, y0), "par": True}
    for i in range(4):
        l = lines[i]; o = lines[(i + 2) % 4]
        bad = l is None or l["sigma"] > 0.3 or (o is not None and abs(((l["ang"] - o["ang"]) + 90) % 180 - 90) > 2.0)
        if bad and o is not None and o["sigma"] <= 0.3:
            rf = _par_refit(i)
            if rf:
                lines[i] = rf
    corners = []
    for i in range(4):
        a, b = lines[i], lines[(i + 1) % 4]
        if a is None or b is None:
            corners.append(None); continue
        vx1, vy1, x1, y1 = a["v"]; vx2, vy2, x2, y2 = b["v"]
        A = np.array([[vx1, -vx2], [vy1, -vy2]], float); rhs = np.array([x2 - x1, y2 - y1], float)
        if abs(np.linalg.det(A)) < 1e-6:
            corners.append(None); continue
        t = np.linalg.solve(A, rhs); corners.append((float(x1 + vx1 * t[0]), float(y1 + vy1 * t[0])))
    ok = [c for c in corners if c]
    # yaw: 긴 변 쌍(0-2 또는 1-3 중 더 긴 쪽)의 각도를 −45~45 로 정규화해 평균
    pair = (0, 2) if (lines[0] or lines[2]) and max((lines[i]["len"] for i in (0, 2) if lines[i]), default=0) >= max((lines[i]["len"] for i in (1, 3) if lines[i]), default=0) else (1, 3)
    angs = [lines[i]["ang"] for i in pair if lines[i]]
    yaw = None
    if angs:
        yaw = float(np.mean([((a + 45) % 90) - 45 for a in angs]))
    return {"plate_h": getattr(depth_mask, "last", {}).get("plate_h"), "box": box.tolist(),
            "edges": [None if l is None else {k: (round(v, 3) if isinstance(v, float) else v) for k, v in l.items() if k != "v"} for l in lines],
            "corners": [None if c is None else (round(c[0], 2), round(c[1], 2)) for c in corners],
            "yaw": None if yaw is None else round(yaw, 3),
            "center": None if len(ok) < 3 else (round(float(np.mean([c[0] for c in ok])), 2), round(float(np.mean([c[1] for c in ok])), 2))}, None


def base_frame(img, grid):
    """밑판 좌표계: 신뢰도(σ 낮음·점 많음) 최고인 인접 변 두 개의 교점=원점, 긴 변 방향=yaw. 반환 dict 또는 (None, why)."""
    r, why = detect_rect(img, grid)
    if not r:
        return None, why
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # detect_rect 내부 lines 를 다시 얻기 위해 재계산(간단히: edges 에 σ·n 있음, 꼭짓점은 corners)
    ed = r["edges"]; cs = r["corners"]
    good = [i for i in range(4) if ed[i] and not ed[i].get("par") and ed[i]["sigma"] <= 0.3 and ed[i]["n"] >= 40]
    if len(good) < 2:
        return None, f"신뢰 변 부족 {good}"
    # 인접 쌍(i, i+1) 중 둘 다 good 인 것 → 교점은 corners[i]
    pairs = [(i, (i + 1) % 4) for i in range(4) if i in good and (i + 1) % 4 in good and cs[i]]
    if not pairs:
        return None, f"인접 신뢰 변 쌍 없음 {good}"
    i, j = min(pairs, key=lambda p: ed[p[0]]["sigma"] + ed[p[1]]["sigma"])
    origin = cs[i]
    long_i = i if ed[i]["len"] >= ed[j]["len"] else j
    a = ed[long_i]["ang"]; yaw = ((a + 45) % 90) - 45
    return {"origin": origin, "origin_corner": i, "yaw": round(float(yaw), 3), "edges_used": (i, j),
            "sigma": (ed[i]["sigma"], ed[j]["sigma"]), "plate_h": r["plate_h"], "rect": r}, None


def pillar_tops(img, grid, plate_rect=None, top_h=(55.0, 110.0)):
    """기둥 꼭대기 4개 검출: 뎁스 높이(바닥 평면 대비 55~110mm) 성분 → 컬러로 검정 사각형 minAreaRect 정제.
    반환 [{"center":(x,y),"box":[4점],"ang":deg,"area":px}] (최대 4, 면적 큰 순)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pts = grid["pts"]
    xs = sorted({p["x"] for p in pts}); ys = sorted({p["y"] for p in pts})
    D = np.full((len(ys), len(xs)), np.nan); xi = {x: i for i, x in enumerate(xs)}; yi = {y: i for i, y in enumerate(ys)}
    for p in pts:
        if p["d"] and 100 < p["d"] < 1000:
            D[yi[p["y"]], xi[p["x"]]] = p["d"]
    valid = ~np.isnan(D); YY, XX = np.mgrid[0:len(ys), 0:len(xs)]
    med = np.nanmedian(D); cand = valid & (np.abs(D - med) < 80)
    P = np.c_[XX[cand], YY[cand], np.ones(cand.sum())]; d = D[cand]
    rng = np.random.default_rng(1); best, bn = None, -1
    for _ in range(300):
        idx = rng.choice(len(d), 3, replace=False)
        try:
            coef = np.linalg.solve(P[idx], d[idx])
        except np.linalg.LinAlgError:
            continue
        n_in = int((np.abs(P @ coef - d) < 4.0).sum())
        if n_in > bn:
            best, bn = coef, n_in
    inl = np.abs(P @ best - d) < 4.0
    coef, *_ = np.linalg.lstsq(P[inl], d[inl], rcond=None)
    plane = coef[0] * XX + coef[1] * YY + coef[2]; height = plane - D
    # 밑판 윗면 높이(최빈) 위로 top_h 대역 = 기둥 꼭대기
    raised = valid & (height > 15.0)
    vals = height[raised]
    if vals.size < 20:
        return []
    hist, edges = np.histogram(vals, bins=int(max(5, (vals.max() - vals.min()) / 2.0)))
    plate = 0.5 * (edges[np.argmax(hist)] + edges[np.argmax(hist) + 1])
    m = (valid & (height - plate > top_h[0]) & (height - plate < top_h[1])).astype(np.uint8) * 255
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    comps = sorted([(int(st[i, 4]), i) for i in range(1, n) if st[i, 4] >= 2], reverse=True)[:6]
    out = []
    step = xs[1] - xs[0] if len(xs) > 1 else 10
    for a, i in comps:
        cx, cy = cen[i][0] * step + step / 2, cen[i][1] * step + step / 2
        # 컬러 정제: 중심 주변 60px 창에서 검정(Otsu) 최대 성분의 minAreaRect
        x0, y0 = int(max(0, cx - 60)), int(max(0, cy - 60)); win = gray[y0:y0 + 120, x0:x0 + 120]
        if win.size == 0:
            continue
        _, th = cv2.threshold(win, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        cs, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cs = [c for c in cs if 150 < cv2.contourArea(c) < 4000]
        if not cs:
            continue
        # 중심에 가장 가까운 성분
        c = min(cs, key=lambda c: abs(cv2.moments(c)["m10"] / max(1e-6, cv2.moments(c)["m00"]) + x0 - cx) + abs(cv2.moments(c)["m01"] / max(1e-6, cv2.moments(c)["m00"]) + y0 - cy))
        (rx, ry), (w, h), ang = cv2.minAreaRect(c)
        box = cv2.boxPoints(((rx + x0, ry + y0), (w, h), ang))
        out.append({"center": (round(float(rx + x0), 2), round(float(ry + y0), 2)), "box": box.tolist(), "ang": round(float(ang), 2), "wh": (round(float(w), 1), round(float(h), 1)), "area": int(cv2.contourArea(c))})
    # 4개로 정리: 서로 40px 이상 떨어진 것만
    keep = []
    for o in out:
        if all(math.hypot(o["center"][0] - k["center"][0], o["center"][1] - k["center"][1]) > 40 for k in keep):
            keep.append(o)
    return keep[:4]


def pillar_tops_color(img, rect, max_len=200, cap=34):
    """색 없이 기둥 꼭대기: 밑판 꼭짓점에서 바깥 대각(±30°)으로 뻗는 검정 막대의 끝 구간(cap px) minAreaRect. 반환 [{corner,center,box,wh}]"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rect = np.array(rect, np.float32); ctr = rect.mean(0)
    _, dark = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    ys_, xs_ = np.nonzero(dark); P = np.c_[xs_, ys_].astype(np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV); sat_mask = ((hsv[..., 1] > 90) & (hsv[..., 2] > 60)).astype(np.uint8)
    sat_mask = cv2.morphologyEx(sat_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    out = []
    for k, c in enumerate(rect):
        u = (c - ctr); u /= np.linalg.norm(u)
        v = P - c; dist = np.linalg.norm(v, axis=1)
        near = dist < max_len
        Q = P[near]; dq = dist[near]; cosang = ((Q - c) @ u) / np.maximum(dq, 1e-6)
        sel = (dq > 8) & (cosang > math.cos(math.radians(30)))
        Q = Q[sel]; dq = dq[sel]
        if len(Q) < 50:
            out.append(None); continue
        hist, _ = np.histogram(dq, bins=np.arange(0, max_len + 5, 5)); end = None
        for b in range(2, len(hist)):
            if hist[b] == 0 and hist[b - 1] > 0:
                end = b * 5; break
        L = end if end else float(dq.max())
        far = Q[(dq > L - cap) & (dq <= L)]
        if len(far) < 20:
            out.append(None); continue
        (cx, cy), (w, h), ang = cv2.minAreaRect(far)
        src = "dark-end"
        # ★꼭대기에 색점 스티커가 있으면 검정 막대가 스티커 앞에서 끝난다 → 막대 끝 너머(L−5..L+45) 부채꼴의 채도 높은 원 중심을 꼭대기로
        if False:   # 9/3 18:30 색점 보정 비활성(파란 기 흰 배경이 채도 마스크에 잡혀 오검출)
            ys2, xs2 = np.nonzero(sat_mask)
            if len(xs2):
                P2 = np.c_[xs2, ys2].astype(np.float32); v2 = P2 - c; d2 = np.linalg.norm(v2, axis=1)
                cos2 = (v2 @ u) / np.maximum(d2, 1e-6)
                s2 = (d2 > L - 5) & (d2 < L + 45) & (cos2 > math.cos(math.radians(35)))
                if s2.sum() >= 30:
                    cx, cy = float(P2[s2][:, 0].mean()), float(P2[s2][:, 1].mean()); w = h = 30.0; ang = 0.0; src = "tip-dot"
        out.append({"corner": k, "center": (float(cx), float(cy)), "box": cv2.boxPoints(((cx, cy), (w, h), ang)).tolist(), "wh": (float(w), float(h)), "bar_len": float(L), "src": src})
    return out


def base_dots(img, rect=None):
    """9/3 18:50 사용자 배치: 흰 점=기둥 꼭대기(4), 빨간 점=밑판 귀퉁이(4)+중앙(1). 반환 {"white":[(x,y,area)], "red":[(x,y,area)]} (서브픽셀 중심)."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out = {"white": [], "red": []}
    # 흰 점(기둥 꼭대기): 탑햇(25px) 으로 큰 밝은 면(책상)은 지우고 작은 밝은 점만 남김 → 책상에 붙은 팁 점도 분리됨
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)))
    wm = ((th > 70) & (hsv[..., 1] < 130)).astype(np.uint8) * 255
    wm = cv2.morphologyEx(wm, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(wm)
    for i in range(1, n):
        a = st[i, 4]; w, h = st[i, 2], st[i, 3]
        if not (50 < a < 900) or not (0.6 < w / max(1, h) < 1.7):
            continue
        ys_, xs_ = np.nonzero(lab == i); wts = gray[ys_, xs_].astype(float)
        out["white"].append((float((xs_ * wts).sum() / wts.sum()), float((ys_ * wts).sum() / wts.sum()), int(a)))
    rm = cv2.inRange(hsv, (0, 90, 60), (12, 255, 255)) | cv2.inRange(hsv, (160, 90, 60), (180, 255, 255))
    rm = cv2.morphologyEx(rm, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(rm)
    for i in range(1, n):
        a = st[i, 4]; w, h = st[i, 2], st[i, 3]
        if 40 < a < 3000 and 0.5 < w / max(1, h) < 2.0:
            ys_, xs_ = np.nonzero(lab == i)
            out["red"].append((float(xs_.mean()), float(ys_.mean()), int(a)))
    if rect is not None:   # 밑판 안(귀퉁이·중앙)만 빨강, 밑판 밖은 흰 점만
        poly = np.array(rect, np.float32)
        out["red"] = [d for d in out["red"] if cv2.pointPolygonTest(poly, (d[0], d[1]), False) >= 0]
        out["white"] = [d for d in out["white"] if cv2.pointPolygonTest(poly, (d[0], d[1]), False) < 0]
        # 흰 점: ArUco 흰 칸 배제 — 밑판 꼭짓점에서 바깥 대각 방향 40~130px 구간(기둥 막대 위)만
        ctr = poly.mean(0); keep = []
        for d in out["white"]:
            ok = False
            for c in poly:
                u = c - ctr; u = u / np.linalg.norm(u); v = np.array(d[:2]) - c; L = np.linalg.norm(v)
                if 30 < L < 150 and (v @ u) / max(L, 1e-6) > math.cos(math.radians(38)):
                    ok = True; break
            if ok:
                keep.append(d)
        out["white"] = keep
        # 빨강: 밑판 안의 것 전부(귀퉁이마다 2개·중앙 1개 — 어느 것이 기준인지는 사용자 확인)
    return out


def pillar_outer_corners(img, white_pts, plate_rect):
    """흰 점(기둥 꼭대기 중심)에서 바깥 대각 방향으로 검정이 끝나는 지점 = 꼭대기 바깥 꼭짓점(서브픽셀). 반환 [(x,y) or None]"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(float)
    ctr = np.array(plate_rect, np.float32).mean(0)
    out = []
    for (wx, wy, *_r) in white_pts:
        u = np.array([wx, wy]) - ctr; u /= np.linalg.norm(u)
        prof = []; ts = np.arange(6, 60, 0.5)             # 흰 점 반경(~7px) 바깥부터 60px 까지
        for t in ts:
            x, y = wx + u[0] * t, wy + u[1] * t; x0, y0 = int(x), int(y)
            if not (0 <= x0 < gray.shape[1] - 1 and 0 <= y0 < gray.shape[0] - 1):
                break
            fx, fy = x - x0, y - y0
            prof.append(gray[y0, x0] * (1 - fx) * (1 - fy) + gray[y0, x0 + 1] * fx * (1 - fy) + gray[y0 + 1, x0] * (1 - fx) * fy + gray[y0 + 1, x0 + 1] * fx * fy)
        prof = np.array(prof)
        if len(prof) < 10:
            out.append(None); continue
        # 실측(9/3): 흰 점이 꼭대기 바깥 모서리에 붙어 있어 바깥으로 ~2px 검정 테두리만 있고 바로 흰 책상.
        # → 첫 25px 안의 최저점(검정 테두리)을 찾고, 그 뒤 밝기가 (최저+책상)/2 를 넘는 지점 = 바깥 꼭짓점
        n25 = int(25 / 0.5)
        seg = prof[:n25]
        imin = int(np.argmin(seg))
        if seg[imin] > 150:                          # 검정 테두리가 안 보이면 점 중심에서 8px 바깥으로 근사
            t = 8.0
        else:
            hi = float(np.median(prof[imin + 4:imin + 30])) if len(prof) > imin + 30 else 180.0
            thr = (seg[imin] + hi) / 2
            idx = np.where((prof[imin:-1] < thr) & (prof[imin + 1:] >= thr))[0]
            if len(idx) == 0:
                t = ts[imin]
            else:
                i = imin + idx[0]; f = (thr - prof[i]) / max(1e-6, (prof[i + 1] - prof[i])); t = ts[i] + 0.5 * f
        out.append((float(wx + u[0] * t), float(wy + u[1] * t)))
    return out


def slot_lines_from_outer(outer):
    """기둥 바깥 사각형 [좌하,우하,우상,좌상](px) ↔ 판 mm (210×140) 호모그래피로 STL 홈 중심을 투영 → 슬롯 선.
    원근으로 두 긴변 px 길이가 달라도(614 vs 601) 호모그래피는 꼭짓점별로 정확히 맞는다."""
    if any(c is None for c in outer):
        return None
    P = np.array(outer, np.float32)
    # 어느 변이 긴변인지: 0→1 vs 1→2
    long_is_01 = np.linalg.norm(P[1] - P[0]) > np.linalg.norm(P[2] - P[1])
    # 판 mm 좌표계(긴변 = x 210, 짧은변 = y 140), 꼭짓점 순서에 맞춤
    M = np.array([[0, 0], [210, 0], [210, 140], [0, 140]], np.float32) if long_is_01 else np.array([[0, 0], [0, 140], [210, 140], [210, 0]], np.float32)
    Hm = cv2.getPerspectiveTransform(M, P)
    def tp(x, y):
        v = Hm @ np.array([x, y, 1.0]); return (float(v[0] / v[2]), float(v[1] / v[2]))
    # STL(z85): 기둥 10×10 네 모서리. 긴변(x) 벽 홈 중심 = 기둥 안쪽 x면(모서리에서 10) 위, 짧은변 방향 5.5 안쪽.
    #           짧은변(y) 벽 홈 중심 = 안쪽 y면(10) 위, 긴변 방향 4.5 안쪽.
    corners_mm = {0: (0, 0), 1: (210, 0), 2: (210, 140), 3: (0, 140)}
    g = {}
    for k, (cx, cy) in corners_mm.items():
        sx = 1 if cx == 0 else -1; sy = 1 if cy == 0 else -1
        g[k] = {"long": tp(cx + sx * 10.0, cy + sy * 5.5), "short": tp(cx + sx * 4.5, cy + sy * 10.0)}
    slots = {"long_01": (g[0]["long"], g[1]["long"]), "long_32": (g[3]["long"], g[2]["long"]),
             "short_03": (g[0]["short"], g[3]["short"]), "short_12": (g[1]["short"], g[2]["short"])}
    # 검산: 슬롯 길이 mm (호모그래피 역변환)
    Hi = np.linalg.inv(Hm)
    def back(p):
        v = Hi @ np.array([p[0], p[1], 1.0]); return np.array([v[0] / v[2], v[1] / v[2]])
    lens = {k: float(np.linalg.norm(back(b) - back(a))) for k, (a, b) in slots.items()}
    return {"H": Hm.tolist(), "grooves": g, "slots": slots, "slot_len_mm": lens}


def slot_from_two_pillars(img, long_mm=210.0, groove_in_mm=10.0, groove_side_mm=5.5, short=False):
    """슬롯 호버(z478)에서 한 캠에 보이는 기둥 꼭대기 2개(흰 점) → 바깥 꼭짓점 → 홈 중심 2개 → 슬롯 선.
    두 바깥 꼭짓점 간 거리 = 판 변 길이(긴변 210 / 짧은변 140). 홈 중심 = 꼭짓점에서 변 방향 안쪽 10 + 판 쪽(수직) 5.5(긴변)/4.5(짧은변).
    반환 {"tips":[(x,y)], "outer":[(x,y)], "grooves":[(x,y)], "scale":px/mm, "ang":deg} 또는 (None, why)"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY); hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (35, 35)))
    wm = ((th > 70) & (hsv[..., 1] < 130)).astype(np.uint8) * 255; wm = cv2.morphologyEx(wm, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(wm)
    tips = []
    for i in range(1, n):
        a = st[i, 4]; w, h = st[i, 2], st[i, 3]
        if 120 < a < 2500 and 0.6 < w / max(1, h) < 1.7:
            x0, y0 = int(cen[i][0]), int(cen[i][1]); ring = gray[max(0, y0 - 25):y0 + 26, max(0, x0 - 25):x0 + 26]
            if ring.size and np.percentile(ring, 20) < 80:          # 주변에 검정(기둥)이 있어야
                ys_, xs_ = np.nonzero(lab == i); wts = gray[ys_, xs_].astype(float)
                tips.append((float((xs_ * wts).sum() / wts.sum()), float((ys_ * wts).sum() / wts.sum()), int(a)))
    if len(tips) < 2:
        return None, f"기둥 꼭대기 흰 점 {len(tips)}개"
    tips = sorted(tips, key=lambda t: -t[2])[:2]; tips = sorted(tips, key=lambda t: t[0])
    p0, p1 = np.array(tips[0][:2]), np.array(tips[1][:2])
    axis = (p1 - p0) / np.linalg.norm(p1 - p0)
    # 판 쪽(수직) 방향: 두 점 중간에서 양쪽 수직으로 40px 가서 더 어두운 쪽이 판
    mid = (p0 + p1) / 2; nrm = np.array([-axis[1], axis[0]])
    def dark_at(q):
        x, y = int(q[0]), int(q[1]); return float(gray[max(0, y - 6):y + 7, max(0, x - 6):x + 7].mean()) if 0 <= x < gray.shape[1] and 0 <= y < gray.shape[0] else 255.0
    if dark_at(mid + nrm * 60) > dark_at(mid - nrm * 60):
        nrm = -nrm
    # 바깥 꼭짓점: 흰 점에서 바깥(변 방향 바깥 + 판 반대쪽) 대각으로 검정 테두리 끝
    outer = []
    for p, sgn in ((p0, -1), (p1, 1)):
        u = (axis * sgn - nrm); u /= np.linalg.norm(u)
        prof = []; ts = np.arange(4, 40, 0.5)
        for t in ts:
            x, y = p + u * t; x0, y0 = int(x), int(y)
            if not (0 <= x0 < gray.shape[1] - 1 and 0 <= y0 < gray.shape[0] - 1):
                break
            prof.append(float(gray[y0, x0]))
        prof = np.array(prof)
        if len(prof) < 10:
            outer.append(tuple(map(float, p + u * 8))); continue
        imin = int(np.argmin(prof[:40])) if len(prof) >= 40 else int(np.argmin(prof))
        if prof[imin] > 150:
            t = 8.0
        else:
            hi = float(np.median(prof[imin + 4:imin + 24])) if len(prof) > imin + 24 else 180.0; thr = (prof[imin] + hi) / 2
            idx = np.where((prof[imin:-1] < thr) & (prof[imin + 1:] >= thr))[0]
            t = ts[imin + idx[0]] + 0.5 * (thr - prof[imin + idx[0]]) / max(1e-6, prof[imin + idx[0] + 1] - prof[imin + idx[0]]) if len(idx) else ts[imin]
        outer.append(tuple(map(float, p + u * t)))
    o0, o1 = np.array(outer[0]), np.array(outer[1])
    L = np.linalg.norm(o1 - o0); side = 140.0 if short else long_mm
    k = L / side                                            # px/mm
    ax = (o1 - o0) / L
    gs = 4.5 if short else groove_side_mm
    g0 = o0 + ax * (groove_in_mm * k) + nrm * (gs * k); g1 = o1 - ax * (groove_in_mm * k) + nrm * (gs * k)
    ang = math.degrees(math.atan2(ax[1], ax[0]))
    return {"tips": [tuple(map(float, p0)), tuple(map(float, p1))], "outer": outer, "grooves": [tuple(map(float, g0)), tuple(map(float, g1))],
            "scale": float(k), "ang": float(ang), "slot_len_px": float(np.linalg.norm(g1 - g0))}, None


def plate_edge_near_slot(img, slot):
    """판 가장자리(슬롯 쪽 윗변): 홈 중심선 아래(판 쪽)로 열마다 밝음→어둠 첫 전이를 찾되, 빨간 점·색 점 위 전이는 버리고
    Huber 직선 2패스(3σ 컷) 로 맞춘다. 반환 {p,q,ang,sigma,n,offset_px} 또는 None"""
    bgr = img; gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(float); hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    g0, g1 = np.array(slot["grooves"][0]), np.array(slot["grooves"][1]); ax = (g1 - g0) / np.linalg.norm(g1 - g0)
    o = np.array(slot["outer"][0]); nrm = np.array([-ax[1], ax[0]])
    if np.dot(g0 - o, nrm) < 0:
        nrm = -nrm
    L = float(np.linalg.norm(g1 - g0)); pts = []
    for t in np.arange(L * 0.10, L * 0.90, 2.0):
        base = g0 + ax * t
        prof = []; coords = []; ss = np.arange(-4, 70, 0.5)
        for s_ in ss:
            x, y = base + nrm * s_; x0, y0 = int(x), int(y)
            if not (0 <= x0 < gray.shape[1] - 1 and 0 <= y0 < gray.shape[0] - 1):
                prof = []; break
            fx, fy = x - x0, y - y0
            prof.append(gray[y0, x0] * (1 - fx) * (1 - fy) + gray[y0, x0 + 1] * fx * (1 - fy) + gray[y0 + 1, x0] * (1 - fx) * fy + gray[y0 + 1, x0 + 1] * fx * fy); coords.append((x0, y0))
        if not prof:
            continue
        prof = np.array(prof)
        # 판 = 깊은 검정(<70)이 20px 이상 이어지는 첫 구간의 시작 (색 점·그림자 같은 짧은 어둠은 무시)
        dark = prof < 70
        run = 0; i_start = None
        for i, dk in enumerate(dark):
            run = run + 1 if dk else 0
            if run >= 40:            # 0.5px 간격 → 20px
                i_start = i - run + 1; break
        if i_start is None or i_start < 2:
            continue
        # 전이점: 시작 직전 밝은 값과 어두운 값의 50% 교차(서브픽셀)
        hi = float(np.max(prof[max(0, i_start - 16):i_start])); lo = float(prof[i_start + 2]); thr = (hi + lo) / 2
        j = i_start
        while j > 0 and prof[j - 1] < thr:
            j -= 1
        if j == 0:
            continue
        den = prof[j] - prof[j - 1]
        f = (thr - prof[j - 1]) / den if abs(den) > 1e-3 else 0.5
        f = min(1.0, max(0.0, float(f))); s_ = ss[j - 1] + 0.5 * f
        if not np.isfinite(s_):
            continue
        # 전이 픽셀이 붉으면(색 점) 버림
        cx, cy = coords[j]; hh, sv = hsv[cy, cx, 0], hsv[cy, cx, 1]
        if sv > 110 and (hh < 12 or hh > 160):
            continue
        pts.append(base + nrm * s_)
    if len(pts) < 20:
        return None
    P = np.array([q_ for q_ in pts if np.all(np.isfinite(q_))], np.float32)
    if len(P) < 20:
        return None
    vx, vy, x0, y0 = cv2.fitLine(P, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    d = np.abs((P[:, 0] - x0) * vy - (P[:, 1] - y0) * vx); keep = d < max(1.0, 2.5 * np.median(d) + 1.0)
    if keep.sum() >= 20:
        vx, vy, x0, y0 = cv2.fitLine(P[keep], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel(); d = np.abs((P[keep][:, 0] - x0) * vy - (P[keep][:, 1] - y0) * vx)
    p = (float(x0 - vx * L / 2), float(y0 - vy * L / 2)); q = (float(x0 + vx * L / 2), float(y0 + vy * L / 2))
    return {"p": p, "q": q, "ang": math.degrees(math.atan2(vy, vx)), "sigma": float(d.std()), "n": int(keep.sum()),
            "offset_px": float(np.dot(np.array([x0, y0]) - g0, nrm))}


def refine_user_lines(img, pts, band=10):
    """사용자가 찍은 점 쌍(P1-P2, P3-P4 …)을 씨앗으로 서브픽셀 직선 맞춤. 반환 [{p,q,ang,sigma,n}]"""
    from edge_anchor import _subpix_line
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out = []
    for i in range(0, len(pts) - 1, 2):
        a, b = pts[i], pts[i + 1]
        r = _subpix_line(gray, (float(a[0]), float(a[1]), float(b[0]), float(b[1])), band=band, step=2)
        if r is None:
            out.append(None); continue
        ang, sig, n, (vx, vy, x0, y0) = r; L = math.hypot(b[0] - a[0], b[1] - a[1])
        out.append({"p": (float(x0 - vx * L / 2), float(y0 - vy * L / 2)), "q": (float(x0 + vx * L / 2), float(y0 + vy * L / 2)), "ang": ang, "sigma": sig, "n": n})
    return out


def blue_slot_line(img, user_edge_pts=None):
    """파랑(긴변) 슬롯 홈 중심선 @ 슬롯 호버: 보이는 두 기둥 팁(짧은 변 140mm 쌍) 으로 축척 → 긴 변 판 가장자리 방향(사용자 선 정제 또는 자동)
    → 좌측 기둥 바깥 꼭짓점에서 변 방향 10mm + 판 쪽 5.5mm = 홈 중심 → 190mm 선. 반환 dict 또는 (None, why)"""
    r, why = slot_from_two_pillars(img, long_mm=140.0)      # 두 팁 = 짧은 변 바깥 꼭짓점 140mm
    if not r:
        return None, why
    k = r["scale"]                                          # px/mm (기둥 꼭대기 높이)
    o0, o1 = np.array(r["outer"][0]), np.array(r["outer"][1])   # 좌·우 기둥 바깥 꼭짓점(위쪽 짧은 변)
    # 긴 변 방향: 사용자 선(정제) 있으면 그것, 없으면 짧은 변에 수직
    if user_edge_pts and len(user_edge_pts) >= 2:
        ls = refine_user_lines(img, user_edge_pts[:2])
        if ls and ls[0]:
            p, q = np.array(ls[0]["p"]), np.array(ls[0]["q"]); ax = (q - p) / np.linalg.norm(q - p)
        else:
            ax = None
    else:
        ax = None
    sh = (o1 - o0) / np.linalg.norm(o1 - o0)                # 짧은 변 방향(좌→우)
    if ax is None:
        ax = np.array([-sh[1], sh[0]])
    if ax[1] < 0:                                           # 아래로(판 쪽) 향하게
        ax = -ax
    inward = sh                                             # 왼쪽 기둥에서 판 안쪽 = 오른쪽
    g0 = o0 + ax * (10.0 * k) + inward * (5.5 * k)
    g1 = g0 + ax * (190.0 * k)
    return {"scale": k, "outer_left": tuple(map(float, o0)), "g0": tuple(map(float, g0)), "g1": tuple(map(float, g1)), "ang": math.degrees(math.atan2(ax[1], ax[0])), "short_slot": r}, None
