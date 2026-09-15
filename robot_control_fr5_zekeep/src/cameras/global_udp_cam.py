#!/usr/bin/env python3
"""글로벌 카메라(비전 PC → HMV1/UDP) 수신 → MJPEG/JPEG 로 다시 내보내는 중계 (9/11).

    python3 global_udp_cam.py            # http://<로봇PC>:8779/  (/stream · /stream?w=640 · /snap · /health)

비전팀 사양(9/11): 송신 192.168.20.30 → 수신 192.168.20.10:21031, stream_id 3, 1280x960 JPEG q85 10fps,
헤더 32B + 페이로드 ≤1200B, ACK/재전송 없음.

★HMV1 헤더 32바이트 — 9/11 실측으로 풀어낸 배치(모두 big-endian):
    0..3   b"HMV1"          4  ver(1)        5  stream_id(3)     6..7  header_len(32)
    8..11  frame_id(u32)   12..15 (상수 416 — 용도 미상, 안 씀)   16..19 timestamp(u32, ms 로 보임)
    20..21 chunk_idx(u16)  22..23 chunk_count(u16)   24..27 frame_bytes(u32)   28..29 chunk_payload_len(u16)   30..31 0
  검증: chunk 0 페이로드가 ffd8ff(JPEG SOI), 150 청크×1200B, 합계 = frame_bytes, cv2 디코드 1280x960 OK.
  청크가 하나라도 빠지면 그 프레임은 버린다(재전송 없음) — /health 의 dropped 로 손실을 본다.
"""
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

# "udp"  = 비전 PC 가 보내주던 HMV1 수신 (9/11)
# "v4l2" = USB 직결 (9/11) — ★9/13 이후 쓰지 말 것. C270 은 비전팀 퍼블리셔가 소유한다.
# "ros"  = ★9/13 채택. 비전팀 factory_view 퍼블리셔의 ROS 토픽을 구독한다.
#   C270 한 대를 두 곳(Unity 21030 + 이 콘솔)이 같이 보려면 이 길밖에 없다.
#   V4L2 장치는 한 프로세스만 열 수 있으므로 직결(v4l2)과 동시 사용 불가.
SOURCE = os.environ.get("GCAM_SOURCE", "udp")
V4L2_DEV = os.environ.get("GCAM_DEV", "/dev/video8")   # C270 HD WEBCAM (1280x960 MJPG 10fps)
V4L2_RES = os.environ.get("GCAM_RES", "1280x960")
V4L2_FPS = int(os.environ.get("GCAM_FPS", "10"))
ROS_TOPIC = os.environ.get("GCAM_ROS_TOPIC", "/vision/factory_camera/image_view")
ROS_DOMAIN = os.environ.get("GCAM_ROS_DOMAIN", "90")   # 비전팀 factory_view 도메인 (셀은 73)
CROP_F = os.environ.get("GCAM_CROP_FILE", "/home/ar/bf2_console/state/gcam_crop.json")
# ★9/11 사용자 지정 화각: 왼쪽 바닥 여백을 걷어내고 조립대~FR5 가 꽉 차게(원본 1280x960 안에서 잘라낸다).
CROP_DEFAULT = {"x": 210, "y": 0, "w": 1045, "h": 950}


def load_crop():
    try:
        with open(CROP_F, encoding="utf-8") as f:
            d = json.load(f)
        return None if not d else {k: int(d[k]) for k in ("x", "y", "w", "h")}
    except FileNotFoundError:
        return dict(CROP_DEFAULT)
    except Exception:
        return dict(CROP_DEFAULT)


def save_crop(c):
    os.makedirs(os.path.dirname(CROP_F), exist_ok=True)
    with open(CROP_F, "w", encoding="utf-8") as f:
        json.dump(c or {}, f, ensure_ascii=False)


CROP = {"v": None}          # 기동 시 load_crop() 으로 채운다
UDP_PORT = int(os.environ.get("GCAM_UDP_PORT", "21031"))
HTTP_PORT = int(os.environ.get("GCAM_HTTP_PORT", "8779"))
STREAM_ID = 3
HDR = struct.Struct(">4sBBHIIIHHIHH")      # 32 bytes
BOOT = time.time()

latest = {"jpg": None, "fid": -1, "ts": 0, "t": 0.0}
stats = {"packets": 0, "complete": 0, "dropped": 0, "bad": 0, "src": None, "fps": 0.0}
lock = threading.Lock()
new_frame = threading.Condition(lock)


def rx_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    s.bind(("0.0.0.0", UDP_PORT))
    partial = {}                     # fid -> {idx: payload}
    meta = {}                        # fid -> (count, total)
    fps_t, fps_n = time.time(), 0
    while True:
        d, a = s.recvfrom(4096)
        stats["packets"] += 1
        if len(d) < 32 or d[:4] != b"HMV1":
            stats["bad"] += 1; continue
        magic, ver, sid, hlen, fid, _f12, ts, cidx, ccnt, total, plen, _r = HDR.unpack_from(d, 0)
        if sid != STREAM_ID or hlen < 32:
            stats["bad"] += 1; continue
        stats["src"] = a[0]
        payload = d[hlen:hlen + plen] if plen else d[hlen:]
        ch = partial.setdefault(fid, {})
        ch[cidx] = payload
        meta[fid] = (ccnt, total, ts)
        if len(ch) == ccnt:
            buf = b"".join(ch[i] for i in range(ccnt) if i in ch)
            if len(buf) == total and buf[:2] == b"\xff\xd8":
                with new_frame:
                    latest.update(jpg=buf, fid=fid, ts=ts, t=time.time())
                    new_frame.notify_all()
                stats["complete"] += 1
                fps_n += 1
            else:
                stats["bad"] += 1
            partial.pop(fid, None); meta.pop(fid, None)
            # 이 프레임보다 오래된 미완성 프레임은 버린다(청크 손실 = 재전송 없음)
            for old in [f for f in partial if f < fid]:
                partial.pop(old, None); meta.pop(old, None); stats["dropped"] += 1
        if len(partial) > 8:          # 안전장치: 고아 프레임 누적 방지
            for old in sorted(partial)[:-4]:
                partial.pop(old, None); meta.pop(old, None); stats["dropped"] += 1
        now = time.time()
        if now - fps_t >= 2.0:
            stats["fps"] = round(fps_n / (now - fps_t), 1); fps_t, fps_n = now, 0



# ══════════════════ 녹화 (9/13 사용자 요청: 콘솔 버튼으로 시작/저장/삭제)
REC_DIR = os.path.expanduser(os.environ.get("GCAM_REC_DIR", "~/Videos"))
REC_FPS = float(os.environ.get("GCAM_REC_FPS", "15"))
REC_SIZE = os.environ.get("GCAM_REC_SIZE", "1920x1440")   # 원본 1280x960 → 1.5배
REC = {"on": False, "path": None, "n": 0, "t0": 0.0, "proc": None, "err": None}
_rec_lock = threading.Lock()


def _rec_worker(size, fps):
    """latest 의 JPEG 를 디코딩 없이 ffmpeg(image2pipe) 로 넘긴다."""
    import subprocess
    w, h = (int(v) for v in size.lower().split("x"))
    os.makedirs(REC_DIR, exist_ok=True)
    path = os.path.join(REC_DIR, time.strftime("globalcam_%m%d_%H%M%S.mp4"))
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-",
             "-vf", f"scale={w}:{h}:flags=lanczos",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", path],
            stdin=subprocess.PIPE)
    except Exception as e:
        REC["err"] = f"ffmpeg 실행 실패: {e}"; REC["on"] = False; return
    REC.update(path=path, proc=proc, n=0, t0=time.time(), err=None)
    last, nxt = -1, 0.0
    while REC["on"]:
        with new_frame:
            if latest["fid"] == last:
                new_frame.wait(timeout=1.0)
            if latest["fid"] == last or latest["jpg"] is None:
                continue
            jpg, last = latest["jpg"], latest["fid"]
        now = time.time()
        if now < nxt:
            continue
        nxt = max(now, (nxt or now) + 1.0 / fps)
        try:
            proc.stdin.write(jpg); REC["n"] += 1
        except Exception as e:
            REC["err"] = f"쓰기 실패: {e}"; break
    try: proc.stdin.close()
    except Exception: pass
    proc.wait()
    REC["proc"] = None


def rec_start(size=None, fps=None):
    with _rec_lock:
        if REC["on"]:
            return False, "이미 녹화 중"
        if latest["jpg"] is None:
            return False, "영상이 아직 안 들어옴 — 비전팀 퍼블리셔 확인"
        REC["on"] = True
        threading.Thread(target=_rec_worker,
                         args=(size or REC_SIZE, float(fps or REC_FPS)), daemon=True).start()
    for _ in range(50):                     # 파일이 잡힐 때까지 잠깐 기다린다
        if REC["path"] or REC["err"]:
            break
        time.sleep(0.05)
    return (False, REC["err"]) if REC["err"] else (True, REC["path"])


def rec_stop(keep):
    """keep=True 저장 / False 삭제."""
    with _rec_lock:
        if not REC["on"]:
            return False, "녹화 중이 아님"
        REC["on"] = False
    for _ in range(100):                    # ffmpeg 가 파일을 닫을 때까지
        if REC["proc"] is None:
            break
        time.sleep(0.05)
    path, n = REC["path"], REC["n"]
    sec = round(n / REC_FPS, 1)
    REC["path"] = None
    if not keep:
        try:
            if path and os.path.exists(path): os.remove(path)
        except Exception as e:
            return False, f"삭제 실패: {e}"
        return True, f"삭제함 ({sec}초)"
    sz = os.path.getsize(path) / 1e6 if path and os.path.exists(path) else 0
    return True, f"{path} ({sz:.1f} MB · {sec}초)"


def ros_loop():
    """★9/13 비전팀 factory_view 퍼블리셔(sensor_msgs/Image, bgr8 1280x960)를 구독해 JPEG 로 바꿔 둔다.

    · C270 실물은 harmony_factory_camera_publisher_v1.py 가 소유(LOCKED_FINAL 화질/구도).
      우리는 그 결과만 받으므로 화질을 건드리지 않는다.
    · QoS 는 퍼블리셔와 같아야 데이터가 온다 — BEST_EFFORT / KEEP_LAST depth 1.
    · 도메인이 90 이라 셀(73)과 다르다. 이 프로세스에만 적용한다.
    """
    os.environ.setdefault("ROS_DOMAIN_ID", ROS_DOMAIN)
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
        from sensor_msgs.msg import Image
    except Exception as e:
        stats["src"] = f"ROS import 실패: {e}"
        print(f"global_udp_cam: rclpy 없음 — 'source /opt/ros/jazzy/setup.bash' 후 기동할 것 ({e})", flush=True)
        return

    fid = [0]; fps_t = [time.time()]; fps_n = [0]

    def cb(msg):
        if msg.encoding not in ("bgr8", "rgb8"):
            stats["bad"] += 1; return
        img = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
        if msg.encoding == "rgb8":
            img = img[:, :, ::-1]
        ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            stats["bad"] += 1; return
        fid[0] += 1
        with new_frame:                       # ★알림을 빼먹으면 /stream 이 2초 타임아웃마다 1장씩만 나간다(9/13)
            latest.update(jpg=out.tobytes(), fid=fid[0],
                          ts=int(time.time() * 1000), t=time.time())
            new_frame.notify_all()
        stats["complete"] += 1; stats["packets"] += 1
        fps_n[0] += 1; now = time.time()
        if now - fps_t[0] >= 2.0:
            stats["fps"] = round(fps_n[0] / (now - fps_t[0]), 1)
            fps_t[0] = now; fps_n[0] = 0

    rclpy.init(args=None)
    node = Node("bf2_gcam_factory_view_sub")
    node.create_subscription(
        Image, ROS_TOPIC,
        cb, QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1,
                       reliability=ReliabilityPolicy.BEST_EFFORT))
    stats["src"] = f"ROS {ROS_TOPIC} (domain {os.environ['ROS_DOMAIN_ID']})"
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node(); rclpy.shutdown()


def v4l2_loop():
    """USB 직결 소스: 카메라 MJPG 프레임을 JPEG 로 다시 싸서 latest 에 넣는다(UDP 경로와 같은 출력)."""
    w, h = (int(v) for v in V4L2_RES.lower().split("x"))
    fid = 0; fps_t, fps_n = time.time(), 0
    while True:
        cap = cv2.VideoCapture(V4L2_DEV, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h); cap.set(cv2.CAP_PROP_FPS, V4L2_FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            stats["bad"] += 1; time.sleep(2.0); continue
        stats["src"] = V4L2_DEV
        while True:
            ok, img = cap.read()
            if not ok or img is None:
                stats["dropped"] += 1
                if stats["dropped"] % 30 == 0:
                    break                      # 연속 실패 → 재오픈(케이블 재삽입 대비)
                time.sleep(0.05); continue
            ok2, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok2:
                stats["bad"] += 1; continue
            fid += 1
            with new_frame:
                latest.update(jpg=out.tobytes(), fid=fid, ts=int(time.time() * 1000), t=time.time())
                new_frame.notify_all()
            stats["complete"] += 1; stats["packets"] += 1; fps_n += 1
            now = time.time()
            if now - fps_t >= 2.0:
                stats["fps"] = round(fps_n / (now - fps_t), 1); fps_t, fps_n = now, 0
        cap.release(); time.sleep(1.0)


def encode_scaled(jpg, w, crop=None):
    """크롭(화각) → 폭 w 로 축소. 둘 다 없으면 원본 JPEG 를 그대로 돌려준다(재인코딩 안 함)."""
    crop = CROP["v"] if crop is None else crop
    if not w and not crop:
        return jpg
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpg
    if crop:
        H, W = img.shape[:2]
        x = max(0, min(W - 1, crop["x"])); y = max(0, min(H - 1, crop["y"]))
        cw = max(16, min(W - x, crop["w"])); ch = max(16, min(H - y, crop["h"]))
        img = img[y:y + ch, x:x + cw]
    if w and w < img.shape[1]:
        h = int(round(img.shape[0] * w / img.shape[1]))
        img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    elif not crop:
        return jpg
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return out.tobytes() if ok else jpg


PAGE = """<!doctype html><meta charset=utf-8><title>글로벌캠</title>
<style>body{margin:0;background:#111;color:#ddd;font:14px/1.4 system-ui}#h{padding:6px 10px;background:#222}
img{display:block;max-width:100vw;max-height:calc(100vh - 32px);margin:auto}</style>
<div id=h>글로벌캠 (비전 PC 192.168.20.30 → UDP 21031 → :%d) <span id=s></span></div>
<img id=v src="/stream">
<script>
const v=document.getElementById('v'),s=document.getElementById('s');
setInterval(async()=>{try{const j=await(await fetch('/health',{cache:'no-store'})).json();
 s.textContent=` — ${j.fps} fps · 프레임 ${j.fid} · 손실 ${j.dropped} · 나이 ${j.age==null?'-':j.age.toFixed(1)+'s'}`;
 if(j.age!=null&&j.age>5)v.src='/stream?'+Date.now();}catch(e){s.textContent=' — 서버 응답 없음'}},2000);
v.onerror=()=>setTimeout(()=>{v.src='/stream?'+Date.now()},1000);
</script>""" % HTTP_PORT


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        # 콘솔 camPanel 이 URL 뒤에 '?t=...' 를 덧붙여 '/stream?w=640?t=123' 이 온다(9/11 실측) → '?' 도 구분자로
        qs = {}
        for p in q.replace("?", "&").split("&"):
            if "=" in p:
                k, v = p.split("=", 1); qs[k] = "".join(ch for ch in v if ch.isdigit()) if k == "w" else v
        if path.startswith("/rec/"):
            act = path[5:]
            if act == "status":
                sec = round(REC["n"] / REC_FPS, 1) if REC["on"] else 0
                return self._send(200, json.dumps({
                    "on": REC["on"], "sec": sec, "frames": REC["n"],
                    "file": os.path.basename(REC["path"] or ""), "err": REC["err"],
                }).encode(), "application/json")
            if act == "start":
                ok, msg = rec_start(qs.get("size"), qs.get("fps"))
            elif act == "save":
                ok, msg = rec_stop(True)
            elif act == "discard":
                ok, msg = rec_stop(False)
            else:
                return self._send(404, b"not found", "text/plain")
            return self._send(200 if ok else 409,
                              json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False).encode(),
                              "application/json")
        if path == "/health":
            with lock:
                age = None if latest["t"] == 0 else round(time.time() - latest["t"], 2)
                body = json.dumps(dict(ok=latest["jpg"] is not None and age is not None and age < 3, boot=BOOT, fid=latest["fid"], source=SOURCE,
                                       age=age, crop=CROP["v"], **stats)).encode()
            return self._send(200, body, "application/json")
        if path == "/crop":
            # /crop            → 현재 화각    /crop?reset=1 → 전체 화면
            # /crop?x=&y=&w=&h= → 화각 지정(원본 1280x960 픽셀 기준, 파일에 저장돼 재기동에도 유지)
            if qs.get("reset"):
                CROP["v"] = None; save_crop(None)
            elif all(k in qs for k in ("x", "y", "w", "h")):
                try:
                    c = {k: int(qs[k]) for k in ("x", "y", "w", "h")}
                except ValueError:
                    return self._send(400, b'{"err":"x,y,w,h must be int"}', "application/json")
                CROP["v"] = c; save_crop(c)
            return self._send(200, json.dumps({"crop": CROP["v"], "full": [1280, 960]}).encode(), "application/json")
        if path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        w = int(qs.get("w", "0") or 0)
        if path == "/snap":
            with lock:
                jpg = latest["jpg"]
            if jpg is None:
                return self._send(503, b"no frame", "text/plain")
            return self._send(200, encode_scaled(jpg, w), "image/jpeg")
        if path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store"); self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            last = -1
            try:
                while True:
                    with new_frame:
                        if latest["fid"] == last:
                            new_frame.wait(timeout=2.0)
                        if latest["fid"] == last or latest["jpg"] is None:
                            continue
                        jpg, last = latest["jpg"], latest["fid"]
                    out = encode_scaled(jpg, w)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(out))
                    self.wfile.write(out); self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                return
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    CROP["v"] = load_crop()
    if SOURCE == "ros" and not os.path.exists(CROP_F):
        # 비전팀 영상은 이미 perspective·crop 이 적용된 최종본이라 여기서 또 자르지 않는다.
        CROP["v"] = None
    if SOURCE == "ros":
        threading.Thread(target=ros_loop, daemon=True).start()
        print(f"global_udp_cam: ROS {ROS_TOPIC} (domain {ROS_DOMAIN}) → http://0.0.0.0:{HTTP_PORT}/  /stream /stream?w=640 /snap /health", flush=True)
    elif SOURCE == "v4l2":
        threading.Thread(target=v4l2_loop, daemon=True).start()
        print(f"global_udp_cam: USB {V4L2_DEV} {V4L2_RES}@{V4L2_FPS} → http://0.0.0.0:{HTTP_PORT}/  /stream /stream?w=640 /snap /health", flush=True)
    else:
        threading.Thread(target=rx_loop, daemon=True).start()
        print(f"global_udp_cam: UDP :{UDP_PORT} (HMV1 stream {STREAM_ID}) → http://0.0.0.0:{HTTP_PORT}/  /stream /stream?w=640 /snap /health", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), H).serve_forever()
