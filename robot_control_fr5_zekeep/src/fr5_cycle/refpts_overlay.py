#!/usr/bin/env python3
"""★9/12 기준점 오버레이 (사용자 요청: "단계별로 인식하라는 점만 손목캠·새카메라 화면에 표시").
사이클(hover_align.check)이 **실제로 쓴** 기준점과 그 순간 매칭한 점을 state/refpts.json 에 내보내고,
콘솔에 떠 있는 두 스트림(손목 :8773 pillar_view, 새카메라 :8768 cam_server)이 그것만 그린다.
그리기 전용 — 로봇·노출·검출 로직은 건드리지 않는다. 뷰 서버가 이 모듈을 못 읽어도 스트림은 살아야 한다(호출부 try/except)."""
import json, os, time, math
import cv2

ROOT = os.environ.get("HOUSE_STATE_ROOT", "/home/ar/bf2_console/state")
PTS = os.path.join(ROOT, "refpts.json")
STG = os.path.join(ROOT, "stage.json")
# ★9/12 사용자 지시: 토글 없이 **항상** 그 단계의 기준점만 보인다(콘솔 버튼 없음).

COL = {"blue": (255, 120, 0), "yellow": (0, 210, 255), "red": (0, 0, 255), "green": (0, 255, 0), "white": (255, 255, 255)}
STALE_S = 25.0          # ★9/12: 이보다 오래된 점은 그리지 않는다 — 묵은 값이 실화면에 남아 오해를 산다(26분 전 테스트값이 표시된 사고)
_cache = {"pts": (0.0, None), "stg": (0.0, None)}


def _load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def _save(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, p)


def _cached(key, path):
    try:
        mt = os.path.getmtime(path)
    except Exception:
        _cache[key] = (0.0, None); return None
    if _cache[key][0] != mt:
        _cache[key] = (mt, _load(path))
    return _cache[key][1]


# ---------------------------------------------------------------- 쓰기(사이클 쪽)
def publish(color, src, ref, meas=None, delta=None, tcp=None, search=None, note=None):
    """ref: hover_ref 항목({"pillars":[(c,x,y,a)], "wall":[(x,y,a)]}). meas: measure() 결과(없으면 기준만).
    now 점은 기준 점마다 가장 가까운 검출(같은 색, search px 안)을 붙인다 — 표시용 근사(정렬 계산과 별개)."""
    d = _load(PTS) or {}
    if d.get("color") != color:
        d = {"color": color, "src": {}}
    rp = [[c, float(x), float(y)] for c, x, y, *_ in (ref.get("pillars") or [])]
    rw = [[float(w[0]), float(w[1])] for w in (ref.get("wall") or [])]
    e = {"ref_pillars": rp, "ref_wall": rw, "now_pillars": None, "now_wall": None,
         "delta": delta, "tcp": tcp, "search": search, "note": note, "ts": time.time()}
    if meas:
        R = (search or {}).get("r", 60.0)
        npl = []
        for c, x, y in rp:
            cx, cy = x, y
            if search and search.get("shift"):
                cx, cy = x + search["shift"][0], y + search["shift"][1]
            cand = [q for q in (meas.get("pillars") or []) if q[0] == c and math.hypot(q[1] - cx, q[2] - cy) <= R]
            npl.append([float(cand[0][1]), float(cand[0][2])] if (cand := sorted(cand, key=lambda q: math.hypot(q[1] - cx, q[2] - cy))) else None)
        nwl = []
        for x, y in rw:
            cand = sorted((q for q in (meas.get("wall") or []) if math.hypot(q[0] - x, q[1] - y) <= 90.0),
                          key=lambda q: math.hypot(q[0] - x, q[1] - y))
            nwl.append([float(cand[0][0]), float(cand[0][1])] if cand else None)
        e["now_pillars"], e["now_wall"] = npl, nwl
    d["src"][src] = e
    d["ts"] = time.time()
    _save(PTS, d)


def set_stage(stage, color=None):
    _save(STG, {"stage": stage, "color": color, "ts": time.time()})


def clear():
    try:
        os.remove(PTS)
    except Exception:
        pass


def refonly():
    return True          # 항상 기준점만


def publish_pts(color, src, stage, ref=None, now=None, note=None):
    """단계별 일반 점. ref/now 원소 = [라벨, 색이름, x, y]. (호버 정렬은 publish() 가 따로 쓴다)"""
    d = _load(PTS) or {}
    if d.get("color") != color:
        d = {"color": color, "src": {}}
    d["src"][src] = {"mode": "pts", "stage": stage, "note": note, "ts": time.time(),
                     "ref": [[str(a), str(b), float(x), float(y)] for a, b, x, y in (ref or [])],
                     "now": [[str(a), str(b), float(x), float(y)] for a, b, x, y in (now or [])]}
    d["ts"] = time.time()
    _save(PTS, d)


# ---------------------------------------------------------------- 그리기(뷰 서버 쪽)
def _ring(img, x, y, r, col, dashed=False, th=2):
    x, y = int(round(x)), int(round(y))
    if not dashed:
        cv2.circle(img, (x, y), r, col, th); return
    for a in range(0, 360, 30):
        cv2.ellipse(img, (x, y), (r, r), 0, a, a + 15, col, th)


def draw(img, src):
    """img 위에 src('wrist'|'newcam') 기준점/매칭점만 그린다. 반환: 그린 점 수."""
    pts = _cached("pts", PTS); stg = _cached("stg", STG) or {}
    stage = str(stg.get("stage") or "")
    color = (pts or {}).get("color") or stg.get("color") or ""
    n = 0
    e = ((pts or {}).get("src") or {}).get(src)
    if e is not None and (time.time() - float(e.get("ts") or 0)) > STALE_S:
        cv2.rectangle(img, (0, img.shape[0] - 30), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
        cv2.putText(img, f"{color} | {stage} | {src} | (지난 측정 {time.time()-float(e.get('ts') or 0):.0f}s - 표시 안 함)".encode('ascii','replace').decode(),
                    (10, img.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 2)
        return 0
    if e and e.get("mode") == "pts":                      # ★9/12 단계별 일반 점(랙·베이스·하강)
        for lab, c, x, y in e.get("ref") or []:
            col = COL.get(c, (0, 255, 0)); _ring(img, x, y, 22, col, dashed=True)
            cv2.putText(img, "REF " + lab, (int(x) - 30, int(y) - 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2); n += 1
        for lab, c, x, y in e.get("now") or []:
            col = COL.get(c, (0, 255, 0)); _ring(img, x, y, 11, col)
            cv2.putText(img, lab, (int(x) + 14, int(y) + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1); n += 1
        hdr = f"{color} | {e.get('stage') or stage} | {src} | " + (e.get("note") or f"{n} pts") + f" | {time.time()-float(e.get('ts') or 0):.0f}s"
        cv2.rectangle(img, (0, img.shape[0] - 30), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
        cv2.putText(img, hdr, (10, img.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        return n
    if e:
        age = time.time() - float(e.get("ts") or 0)
        sh = (e.get("search") or {}).get("shift")
        for c, x, y in e["ref_pillars"]:
            col = COL.get(c, (0, 255, 0))
            _ring(img, x, y, 22, col, dashed=True)
            cv2.putText(img, f"REF {c}", (int(x) - 30, int(y) - 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
            if sh:
                _ring(img, x + sh[0], y + sh[1], int((e.get("search") or {}).get("r", 15)), (160, 160, 160), dashed=True, th=1)
            n += 1
        for x, y in e["ref_wall"]:
            _ring(img, x, y, 22, (255, 255, 255), dashed=True)
            cv2.putText(img, "REF wall", (int(x) - 34, int(y) - 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
            n += 1
        for (c, x, y), q in zip(e["ref_pillars"], e.get("now_pillars") or []):
            if q:
                col = COL.get(c, (0, 255, 0))
                _ring(img, q[0], q[1], 11, col); cv2.line(img, (int(x), int(y)), (int(q[0]), int(q[1])), col, 1)
                cv2.putText(img, f"{q[0]-x:+.0f},{q[1]-y:+.0f}px", (int(q[0]) + 14, int(q[1]) + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        for (x, y), q in zip(e["ref_wall"], e.get("now_wall") or []):
            if q:
                _ring(img, q[0], q[1], 11, (255, 255, 255)); cv2.line(img, (int(x), int(y)), (int(q[0]), int(q[1])), (255, 255, 255), 1)
                cv2.putText(img, f"{q[0]-x:+.0f},{q[1]-y:+.0f}px", (int(q[0]) + 14, int(q[1]) + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        d = e.get("delta")
        hdr = f"{color} | {stage} | {src} | " + (f"d=({d[0]:+.2f},{d[1]:+.2f})mm" if d else "ref only") + f" | {age:.0f}s"
    else:
        hdr = f"{color} | {stage} | {src} | no ref pts yet"
    cv2.rectangle(img, (0, img.shape[0] - 30), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
    cv2.putText(img, hdr, (10, img.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return n
