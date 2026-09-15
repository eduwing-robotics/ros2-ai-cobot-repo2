#!/usr/bin/env python3
"""손목캠 기준점 뷰 (:8777)
사이클의 각 단계에서 '내가 기준으로 쓰는 점'을 손목캠 화면에 겹쳐 보여준다.
  · 관측(z650)  : 밑판 기둥 4점 + ArUco 마커
  · 랙(z556)    : 벽 색점 전부 · 양 끝 · 파지 기준(가운데 점 또는 끝점 중점) · 기준 Pc0
  · 파지/운반   : 든 벽 점 + 파지 서명 자리
  · 정렬(z440)  : z440 기준의 기둥 특징·벽 점(점선) ↔ 지금 검출(실선)
  · 안착(z355)  : 안착 기준 점 ↔ 지금 검출
그리기 전용. 로봇·노출을 건드리지 않는다(카메라는 /raw 만 읽음).
"""
import json, math, os, time, threading
import urllib.request as UR
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np, cv2
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ST = "/home/ar/bf2_console/state/house/"
CAM = "http://127.0.0.1:8766/raw"
CYCLE = "http://127.0.0.1:8776/state"
OBS = (200.0, -330.0, 650.0)

C_REF, C_NOW, C_TXT = (0, 200, 255), (0, 255, 80), (255, 255, 255)


def jload(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d


def robot():
    try:
        import place_calc as PC
        return PC.st()["tcp"]
    except Exception:
        return None


def cycle_state():
    try:
        return json.loads(UR.urlopen(CYCLE, timeout=1.5).read())
    except Exception:
        return {}


def phase_of(tcp):
    if tcp is None:
        return "?"
    x, y, z = tcp[0], tcp[1], tcp[2]
    if abs(x - OBS[0]) < 30 and abs(y - OBS[1]) < 30 and z > 600:
        return "OBSERVE"
    if x < -80:
        return "RACK" if z > 500 else "PICK"
    if 380 < z < 500:
        return "ALIGN"
    if z <= 380:
        return "SEAT"
    return "CARRY"


def ring(img, x, y, r, col, lab=None, dashed=False, th=2):
    x, y = int(round(x)), int(round(y))
    if dashed:
        for a in range(0, 360, 30):
            p1 = (int(x + r * math.cos(math.radians(a))), int(y + r * math.sin(math.radians(a))))
            p2 = (int(x + r * math.cos(math.radians(a + 15))), int(y + r * math.sin(math.radians(a + 15))))
            cv2.line(img, p1, p2, col, th)
    else:
        cv2.circle(img, (x, y), r, col, th)
    cv2.drawMarker(img, (x, y), col, cv2.MARKER_CROSS, 8, 1)
    if lab:
        cv2.putText(img, lab, (x + r + 3, y - r), cv2.FONT_HERSHEY_SIMPLEX, 0.42, col, 1, cv2.LINE_AA)


def draw(img, tcp, color, phase):
    notes = []
    import place_calc as PC
    if phase == "OBSERVE":
        try:
            import pillar_dots as PD, base_depth_corner as B
            im2, grid = B.grab_pair()
            m, _w = B.detect_rect(im2, grid)
            rect = PD.plate_rect(m)
            pts, why = PD.four_corners(im2, rect) if rect is not None else (None, "밑판 사각형 없음")
            if pts:
                for p in pts:
                    ring(img, p[0], p[1], 16, C_NOW, f"pillar {p[3]}")
                notes.append(f"pillars {len(pts)}/4 - base pose ref")
            else:
                notes.append(f"pillar detect fail: {why}")
        except Exception as e:
            notes.append(f"pillar draw fail: {e}")
        try:
            import base_twist as BT
            mk, _ = BT.markers()
            ref = (jload(ST + "aruco_ref.json") or {}).get("markers") or {}
            for i, cs in (mk or {}).items():
                c = np.mean(np.array(cs, float), axis=0)
                ring(img, c[0], c[1], 12, (255, 160, 0), f"id{i}")
                r0 = ref.get(str(i))
                if r0:
                    c0 = np.mean(np.array(r0, float), axis=0)
                    ring(img, c0[0], c0[1], 12, C_REF, None, dashed=True)
                    cv2.line(img, (int(c0[0]), int(c0[1])), (int(c[0]), int(c[1])), C_REF, 1)
            if mk:
                notes.append(f"ArUco {len(mk)} (dashed = saved ref)")
        except Exception as e:
            notes.append(f"ArUco fail: {e}")

    elif phase in ("RACK", "PICK"):
        rr = (jload(ST + "rack_ref.json") or {}).get(color) or {}
        try:
            lo, hi = PC.WALL_DOT_HSV[color]
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            n, lab, stt, cen = cv2.connectedComponentsWithStats(m)
            pts = [(float(cen[i][0]), float(cen[i][1]), int(stt[i, 4])) for i in range(1, n)
                   if 40 < stt[i, 4] < 3000 and 20 < cen[i][0] < 1260 and 20 < cen[i][1] < 700]
            xh = (rr.get("Pc0") or [640])[0]
            pts = [q for q in pts if abs(q[0] - xh) <= getattr(PC, "X_WIN_PX", 45.0)]
            pts.sort(key=lambda q: q[1])
            for i, q in enumerate(pts):
                ring(img, q[0], q[1], 11, C_NOW, f"{i+1}")
            if len(pts) >= 2:
                p1, p2 = pts[0], pts[-1]
                cv2.line(img, (int(p1[0]), int(p1[1])), (int(p2[0]), int(p2[1])), C_NOW, 1)
                want = getattr(PC, "WALL_DOT_N", {}).get(color)
                if want and len(pts) == want:
                    g = PC._grip_point([(q[0], q[1]) for q in pts], (p1[0], p1[1]), (p2[0], p2[1]))
                    mode = "middle dot"
                else:
                    g = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
                    mode = "ends mid"
                ring(img, g[0], g[1], 22, (0, 0, 255), f"GRIP REF ({mode})", th=3)
                notes.append(f"{color} dots {len(pts)} (expect {want}) | grip ref = {mode}")
            if rr.get("Pc0"):
                ring(img, rr["Pc0"][0], rr["Pc0"][1], 22, C_REF, "ref Pc0", dashed=True)
        except Exception as e:
            notes.append(f"rack draw fail: {e}")
        _held(img, color, notes)

    elif phase in ("ALIGN", "CARRY"):
        h = ((jload(ST + "hover_ref.json") or {}).get(color) or {}).get("wrist")
        if h:
            for c0, x0, y0, a0 in h.get("pillars", []):
                ring(img, x0, y0, 18, C_REF, f"ref {c0}", dashed=True)
            for w in h.get("wall", []):
                ring(img, w[0], w[1], 20, (255, 0, 255), "ref wall dot", dashed=True)
            notes.append(f"z440 ref {h.get('made')} feats {len(h.get('pillars', []))} expo {h.get('expo')}")
            try:
                import hover_align as HA
                meas, why = HA.measure(img.copy(), color, h, None, "wrist")
                if meas:
                    for c0, x0, y0, a0 in meas["pillars"]:
                        ring(img, x0, y0, 12, C_NOW, c0)
                    for w in meas["wall"]:
                        ring(img, w[0], w[1], 14, (0, 255, 255), "wall dot")
                    notes.append(f"now feats {len(meas['pillars'])} wall {len(meas['wall'])}")
                else:
                    notes.append(f"measure fail: {why}")
            except Exception as e:
                notes.append(f"align measure fail: {e}")
        else:
            notes.append(f"{color} has no z440 ref")
        _held(img, color, notes)

    elif phase == "SEAT":
        pr = jload(ST + f"pose_refs/{color}.json") or {}
        dots = (((pr.get("seat") or {}).get("cams") or {}).get("wrist") or {}).get("dots") or {}
        k = 0
        for c0, lst in dots.items():
            for q in lst:
                ring(img, q[0], q[1], 18, C_REF, f"seat ref {c0}", dashed=True); k += 1
        notes.append(f"seat ref dots {k} ({(pr.get('seat') or {}).get('made')})")
        _held(img, color, notes)
    return notes


def _held(img, color, notes):
    try:
        import place_calc as PC
        ref = PC.load_grasp_ref(color)
        if ref:
            for q in ref.get("cam1_wall_pd", []):
                ring(img, q[0], q[1], 24, (255, 0, 255), "sig anchor", dashed=True)
            notes.append(f"grasp sig {ref.get('made')} expo {ref.get('expo')}")
        w = PC.held_wall_dots(color)
        for q in w:
            ring(img, q[0], q[1], 16, (0, 255, 255), f"held dot {q[2]}")
        if ref and w:
            r0 = ref["cam1_wall_mid"]
            q = min(w, key=lambda t: math.hypot(t[0] - r0[0], t[1] - r0[1]))
            cv2.line(img, (int(r0[0]), int(r0[1])), (int(q[0]), int(q[1])), (255, 0, 255), 2)
            d = math.hypot(q[0] - r0[0], q[1] - r0[1])
            notes.append(f"vs sig {d:.0f}px ({d*0.09:.2f}mm)")
    except Exception as e:
        notes.append(f"held draw fail: {e}")


def frame():
    b = UR.urlopen(CAM, timeout=4).read()
    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
    tcp = robot()
    s = cycle_state()
    color = s.get("color") or "blue"
    ph = phase_of(tcp)
    notes = []
    try:
        notes = draw(img, tcp, color, ph)
    except Exception as e:
        notes = [f"draw error: {e}"]
    hdr = f"[{ph}] {color}  TCP " + (", ".join(f"{v:.1f}" for v in tcp[:3]) if tcp else "?") + f"   stage={s.get('stage')}"
    cv2.rectangle(img, (0, 0), (img.shape[1], 22 + 18 * len(notes)), (0, 0, 0), -1)
    cv2.putText(img, hdr, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_TXT, 1, cv2.LINE_AA)
    for i, t in enumerate(notes):
        cv2.putText(img, t, (6, 34 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (180, 220, 255), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


PAGE = ("""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>기준점 뷰(손목캠)</title>
<style>body{margin:0;background:#111;color:#ddd;font-family:system-ui}
img{width:100%;max-width:1100px;display:block;margin:0 auto}
p{max-width:1100px;margin:8px auto;font-size:13px;color:#9aa}</style></head>
<body><img src="/stream">
<p>점선 = 저장된 기준 자리 · 실선 = 지금 검출 · 굵은 빨강 = 파지 기준점.
자세를 보고 단계를 스스로 고릅니다(관측 / 랙 / 정렬 / 안착). 그리기 전용이라 로봇·노출을 건드리지 않습니다.</p>
</body></html>""").encode("utf-8")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.end_headers()
            try:
                while True:
                    try:
                        j = frame()
                    except Exception:
                        time.sleep(0.4); continue
                    self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(j))
                    self.wfile.write(j); self.wfile.write(b"\r\n")
                    time.sleep(0.5)
            except Exception:
                return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers(); self.wfile.write(PAGE)


if __name__ == "__main__":
    print("기준점 뷰 :8777  (손목캠 + 단계별 기준 오버레이)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8777), H).serve_forever()
