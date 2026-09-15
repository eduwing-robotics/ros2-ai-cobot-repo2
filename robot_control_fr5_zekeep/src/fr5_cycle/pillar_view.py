#!/usr/bin/env python3
"""기둥 색점 전용 뷰 — 9/5 신설 (:8773).

왜 따로 두나(사용자 결정 9/5):
  콘솔이 그리는 점은 `cam_server` 의 **벽 스티커용 임계값**(CAM_BLUE_S=245·CAM_YELLOW_S=140)
  으로 잡은 것이라 기둥 색점(파랑 S246~250·노랑 S120)과 자가 다르다 — 노랑을 놓치고
  밑판 높이(506mm)의 유령 파랑을 그린다. 그렇다고 그 값을 내리면 9/2 에 유령 8곳을 걸러내려
  실측으로 올려 둔 **벽 정렬 임계값**이 흔들린다.
  → cam_server 는 손대지 않고, 기둥 검출 결과만 별도 화면으로 띄운다.

화면에 표시하는 것:
  · 기둥 4점(색 원 + B/Y/R 이름표)과 밑판 사각형
  · 사용자가 확인해 준 정답 위치(작은 십자) — 검출이 그 자리에 붙는지 눈으로 바로 확인
  · 검출 n/4 · 최악 색 여유 · 유령 개수 · 현재 노출

  python3 pillar_view.py     # http://<PC>:8773/
"""
import json, sys, time, threading, math, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import cv2, numpy as np

sys.path.insert(0, "/home/ar/bf2_console/tools")
import pillar_dots as PD
import color_lock as CL
try:
    import refpts_overlay as RO     # ★9/12
except Exception:
    RO = None

CAM = "http://127.0.0.1:8766"
PORT = 8773
COL = {"blue": (255, 60, 0), "yellow": (0, 210, 255), "red": (0, 0, 255)}
state = {"jpg": None, "found": 0, "margin": 0.0, "ghosts": 0, "t": 0}


def plate_rect(img):
    """밑판 사각형 — 뎁스 없이 색/형상만으로. base_depth_corner 는 뎁스 격자를 받아야 해서
    뷰어에서는 매 프레임 부르지 않고, 실패하면 직전 값을 재사용한다."""
    import base_depth_corner as B
    try:
        img2, grid = B.grab_pair()
        m, _w = B.detect_rect(img2, grid)
        return PD.plate_rect(m)          # ★뎁스 box 우선(벽에 안 흔들림)
    except Exception:
        return None


def worker():
    rect = None
    last_rect = 0
    anchors = (CL.load_anchors() or {}).get("anchors") or []
    last_anchor_load = time.time()
    expo_txt = ""
    last_expo = 0
    while True:
        try:
            # 밑판 사각형은 2초에 한 번만(뎁스 잡기가 무겁다)
            if time.time() - last_rect > 2.0:
                r = plate_rect(None)
                if r is not None:
                    rect = r
                last_rect = time.time()
            if time.time() - last_anchor_load > 10.0:
                anchors = (CL.load_anchors() or {}).get("anchors") or []
                last_anchor_load = time.time()
            if time.time() - last_expo > 5.0:
                try:
                    e = CL.current_settings()
                    expo_txt = f"expo {e.get('exposure')}  bright {float(e.get('bright') or 0):.0f}"
                except Exception:
                    expo_txt = ""
                last_expo = time.time()

            buf = urllib.request.urlopen(CAM + "/raw", timeout=3).read()
            img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                time.sleep(0.15); continue
            out = img.copy()
            if RO is not None:                              # ★9/12 항상 그 단계의 기준점만
                try: RO.draw(out, "wrist")
                except Exception: pass
                ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok: state.update(jpg=jpg.tobytes(), t=time.time())
                time.sleep(0.12); continue

            if rect is not None:
                cv2.polylines(out, [np.array(rect, np.int32)], True, (90, 90, 90), 1)

            # 정답 위치(작은 십자)
            for a in anchors:
                cv2.drawMarker(out, (int(a["x"]), int(a["y"])), (160, 160, 160),
                               cv2.MARKER_CROSS, 14, 1)

            pts, why = (PD.four_corners(img, rect) if rect is not None else (None, "밑판 사각형 대기"))
            n = len(pts) if pts else 0
            ghosts = 0
            worst = 0.0
            if pts:
                hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                mars = []
                for x, y, a, c, *_ in pts:
                    cv2.circle(out, (int(x), int(y)), 13, COL[c], 2)
                    cv2.putText(out, c[0].upper(), (int(x) + 16, int(y) + 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, COL[c], 2)
                    # ★9/9: 빨강은 9/6 부터 범위가 리스트(랩어라운드 2구간) — 구간별 여유 중 최대를 쓴다.
                    #   (그 뒤로 뷰가 재기동된 적이 없어 오늘 재기동하자 매 프레임 ValueError 로 상태가 안 올라갔다)
                    rs = PD.RANGES[c]; rs = rs if isinstance(rs, list) else [rs]
                    w = hsv[max(0, int(y) - 5):int(y) + 6, max(0, int(x) - 5):int(x) + 6].reshape(-1, 3)
                    best = 0.0
                    for lo, hi in rs:
                        inr = w[(w[:, 0] >= lo[0]) & (w[:, 0] <= hi[0]) & (w[:, 1] >= lo[1]) &
                                (w[:, 1] <= hi[1]) & (w[:, 2] >= lo[2]) & (w[:, 2] <= hi[2])]
                        if len(inr) >= 5:
                            best = max(best, CL._margin(np.median(inr, axis=0), (lo, hi)))
                    mars.append(best)
                worst = min(mars) if mars else 0.0
                # 유령: 꼭짓점 근처가 아닌 색 덩어리 — 회색 원으로 표시(무엇이 걸러졌는지 보이게)
                for k, v in PD.detect(img, None).items():
                    for p in v:
                        if rect is not None and min(math.dist(p[:2], (c[0], c[1])) for c in rect) > PD.CORNER_R_PX:
                            ghosts += 1
                            cv2.circle(out, (int(p[0]), int(p[1])), 9, (120, 120, 120), 1)

            # ★OpenCV putText 는 한글을 못 그린다(물음표로 나옴) → 화면 글자는 영문으로
            bar = f"PILLARS {n}/4   MARGIN {worst:.2f}   GHOST {ghosts}   {expo_txt}"
            if why and n < 4:
                bar += "   CHECK"
            cv2.rectangle(out, (0, 0), (out.shape[1], 34), (0, 0, 0), -1)
            cv2.putText(out, bar, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                        (0, 255, 120) if n == 4 else (0, 165, 255), 2)

            if RO is not None:
                try: RO.draw(out, "wrist")                 # 일반 모드에서도 기준점은 위에 겹쳐 그린다
                except Exception: pass
            ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if ok:
                state.update(jpg=jpg.tobytes(), found=n, margin=round(worst, 3),
                             ghosts=ghosts, t=time.time())
        except Exception:
            time.sleep(0.3)
        time.sleep(0.12)


PAGE = ("""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>기둥 색점 뷰</title>
<style>body{margin:0;background:#111;color:#ddd;font-family:system-ui}
img{width:100%;max-width:1100px;display:block;margin:0 auto}
p{max-width:1100px;margin:8px auto;font-size:13px;color:#9aa}</style></head>
<body><img src="/stream">
<p>색 원 = 검출된 기둥 색점 · 작은 십자 = 사용자가 확인해 준 정답 위치 · 회색 원 = 걸러낸 유령.
콘솔 손목캠 화면은 <b>벽 스티커용 임계값</b>이라 기둥 점 기준이 다릅니다.</p>
</body></html>""").encode()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    if state["jpg"]:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + state["jpg"] + b"\r\n")
                    time.sleep(0.12)
            except Exception:
                return
        if self.path.startswith("/status"):
            b = json.dumps({k: state[k] for k in ("found", "margin", "ghosts", "t")}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/raw"):
            j = state["jpg"] or b""
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(j))); self.end_headers(); self.wfile.write(j); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    time.sleep(1.5)
    print(f"pillar_view → http://0.0.0.0:{PORT}/", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
