#!/usr/bin/env python3
"""D435 손목캠 라이브 스트림 + ArUco 검출 오버레이 (8/25).

    python3 cam_server.py            # http://<PC IP>:8766 브라우저로 접속 (USB D435 직접)
⚠ RealSense 파이프는 프로세스 하나만 열 수 있다 — aruco_check.py 등과 동시 실행 금지.

8/27 영상 소스 선택 (D435 케이블을 비전 담당이 가져갔을 때 — 네트워크로 받기):
    --source rs                      # 기본. pyrealsense2 로 USB 직접 (컬러+정렬 뎁스)
    --source ros [--color-topic auto|T] [--depth-topic off|auto|T] [--domain 73]
                                     # ROS2 이미지 구독. 기본 auto = 토픽 목록에서 color/rgb 이미지 자동 선택
                                     #   (compressed 우선 → WiFi 대역 1/10), 뎁스는 기본 off(캘리브 땐 --depth-topic auto).
                                     #   15s 프레임 없으면 자동 재탐색. 비전 PC 가 realsense-ros 든 cam_pub.py 든 무관.
                                     #   ★ROS env(cyclonedds_team.xml) source 한 셸에서 — 또는 ./start_cam.sh 사용.
    --source udp [--udp-port 5005]   # ★순수 UDP JPEG 조각 수신 — 카메라 PC 에서 cam_udp_send.py --to <이 PC IP>.
                                     #   ROS/DDS/도메인 설정 전혀 불필요. 뎁스는 송신 --depth 일 때만(png16).
    --source mjpeg --url http://...  # MJPEG/RTSP 등 cv2.VideoCapture 가 여는 URL (컬러만, 뎁스 없음 → depth_mm=None)
    --port 8766
검출·/dots·/stream·/focus·/click 은 소스와 무관하게 동일. 프레임은 항상 1280x720 으로 맞춘다(픽셀 기준 좌표 보존).
"""
import argparse
import json
import os
import threading
import time

import cv2
import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DICT_NAMES = ["DICT_4X4_50", "DICT_4X4_100", "DICT_4X4_250", "DICT_5X5_50",
              "DICT_5X5_100", "DICT_5X5_250", "DICT_6X6_50", "DICT_6X6_250",
              "DICT_ARUCO_ORIGINAL", "DICT_APRILTAG_36h11"]

latest = {"jpg": None, "info": "starting"}
# 8/30: 물고 있는 벽의 기울기를 뎁스 평면 피팅으로 직접 재기 위한 격자 샘플 요청함.
#   rs 프레임 객체를 핸들러로 넘기면 프레임 풀 수명 문제가 생기므로, 요청만 걸어두고
#   다음 프레임에서 루프가 채운다(요청 1개, 응답 1개).
depth_req = {"box": None, "res": None}
# 8/30: 뎁스 화면 보기(/depthview) — 카메라 위치를 새로 잡을 때 뎁스가 유효한지 눈으로 확인용.
depth_img = {"want": False, "jpg": None, "t": 0.0, "info": ""}
lock = threading.Lock()
# 사용자가 라이브 화면에서 클릭한 타깃(8/25 요청: "내가 점 찍을 순 없어?").
# 설정되면 검출은 이 주변(반경 SEED_R)에서 '가장 가까운' 블롭만 채택 + 추적 갱신.
seed = {"xy": None}
SEED_R = 200
SRCOBJ = {"o": None}          # 9/1: /expo 가 소스(RsSource)에 접근하기 위한 참조


def make_detector(name):
    # cv2 4.6 = 구 API(Dictionary_get/DetectorParameters_create), 4.7+ = ArucoDetector.
    # ⚠혼용 금지: 4.6에서 DetectorParameters() 직접 생성 후 사용 → segfault(8/25 실증)
    if hasattr(cv2.aruco, "ArucoDetector"):
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
        prm = cv2.aruco.DetectorParameters()
        prm.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX   # ★9/3: 정수 코너 → 마커 각도 0.55° 양자화(z478 yaw 실측) → 서브픽셀 정제
        det = cv2.aruco.ArucoDetector(d, prm)
        return lambda img: det.detectMarkers(img)
    d = cv2.aruco.Dictionary_get(getattr(cv2.aruco, name))
    p = cv2.aruco.DetectorParameters_create()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return lambda img: cv2.aruco.detectMarkers(img, d, parameters=p)


MAX_PER_KIND = 6   # 색깔당 최대 표시 개수
# 9/1 저녁: 물고 있는 벽의 아래 점이 화면 하단(y≈660)에 걸쳐 잘리면서 면적이 1/10(1300→140)로
#   줄어 검출에서 탈락했다. 그 결과 벽 점이 1개만 잡혀 yaw 를 못 재고 삽입이 실패했다.
#   → 하단 여백을 0 으로 열어 잘린 점도 잡는다. ⚠잘린 점은 무게중심이 위로 밀리므로
#     거리/각도 기준에 쓸 때는 온전한 점을 우선할 것.
EDGE = int(os.environ.get("CAM_EDGE", "20"))
EDGE_B = int(os.environ.get("CAM_EDGE_BOTTOM", "0"))
BLUE_MIN = float(os.environ.get("CAM_BLUE_MIN", "30"))
# 9/1 저녁 실측: 물고 있는 벽의 아래 파란 점이 그늘져 V=75 (위쪽 점은 94).
#   V 하한 80 에 걸려 검출에서 탈락 → 벽 점이 1개만 잡히고 yaw 를 못 쟀다. 60 으로 내린다.
#   (H105·S255 로 색상·채도는 완벽 — 밝기만 부족했다)
BLUE_V = int(os.environ.get("CAM_BLUE_V", "60"))
# ★9/2 색 임계 실측 재조정(관측자세, 사용자가 점 재마킹 후 20프레임 실측):
#   파랑 진짜 점 = S255 포화(8/26 기록 최저 168) vs 유령 8곳 = S103~164 → S 로 완벽 분리.
#   빨강 = V73~93 인데 하한 70 이라 밝기 조금만 내려가도 탈락(20% 소실의 진범. H 랩어라운드 아님).
#   노랑 = S157~193, V15 어둠노이즈 유령 1곳.
#   기본값은 종전과 동일 — cam1 만 start_cam.sh 에서 조인 값을 넣는다(새캠은 이미 안정).
BLUE_S = int(os.environ.get("CAM_BLUE_S", "110"))
RED_S = int(os.environ.get("CAM_RED_S", "100"))
RED_V = int(os.environ.get("CAM_RED_V", "70"))
YELLOW_S = int(os.environ.get("CAM_YELLOW_S", "90"))
# 9/1 새카메라: 그리퍼 몸체(기판 초록·부품 빨강)가 화면 우상단에 상주 → 오검출.
#   그리퍼는 카메라와 한 몸이라 프레임 내 위치가 불변 — 고정 제외 구역이 정답.
#   CAM_EXCLUDE="x0,y0,x1,y1[;x0,y0,x1,y1...]"
EXCLUDE = [tuple(float(v) for v in b.split(","))
           for b in os.environ.get("CAM_EXCLUDE", "").split(";") if b.strip()]
# 초록 임계 인스턴스별 오버라이드 — CAM_HSV_GREEN="loH,loS,loV,hiH,hiS,hiV"
_g = os.environ.get("CAM_HSV_GREEN", "48,80,52,85,255,255").split(",")
GREEN_LO, GREEN_HI = tuple(int(v) for v in _g[:3]), tuple(int(v) for v in _g[3:])
# 8/26 작업별 색 포커스: 서버 JobStep 이 정한 부품(색)만 검출 — /focus?kinds=blue,red  /focus?clear=1
focus = {"kinds": None}
BOOT = int(time.time())   # 서버 기동 시각 — 콘솔 워치독이 재시작을 감지해 스트림 재접속


def detect_dots(img):
    """파지점 도트 검출 — 검은 부품 위 '흰 점' + 흰 부품 위 '파란 점'.

    반환: [(kind, (cx,cy), area), ...] 면적 상위 각 1개.
    원리: ①흰 점 = 어두운 주변(검은 부품) 속 밝은 소형 블롭
          ②파란 점 = HSV 파랑 범위 소형 블롭
    """
    out = []
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # ── 파란 점 (흰 부품 위) ──
    # 8/26: 색깔당 1개(면적 최대)만 반환하던 결함 → 전부 반환(면적순, 색당 최대 MAX_PER_KIND)
    #       "빨간 점 하나가 인식 안 됨" = 두 번째 빨간 점이 면적 경쟁에서 밀려 버려진 것.
    def _all(mask, kind, lo, hi):
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        got = []
        for c in cnts:
            a = cv2.contourArea(c)
            if not (lo < a < hi):
                continue
            # 8/26 모양 필터(파랑 오탐 대응): 스티커는 원형 — 실측 원형도 0.55~0.78·채움 0.5~0.6.
            #   그림자/모서리 반사는 길쭉(원형도 0.2~0.4). 문턱 원형도 0.45·채움 0.4.
            x, y, w, h = cv2.boundingRect(c)
            circ = 4 * np.pi * a / (cv2.arcLength(c, True) ** 2 + 1e-6)
            if circ < 0.45 or a / float(w * h) < 0.4 or max(w, h) > 2.2 * min(w, h):
                continue
            m = cv2.moments(c)
            # ★9/1: 정수 반올림이 정확도 상한이었다(기선 297px 에서 1px = 0.19° yaw 오차).
            #   윤곽 모멘트는 원래 서브픽셀이므로 그대로 쓴다(소수 2자리).
            got.append((kind, (round(m["m10"]/m["m00"], 2), round(m["m01"]/m["m00"], 2)), a))
        got.sort(key=lambda t: -t[2])
        return got[:MAX_PER_KIND]

    # 파랑: 8/26 채도 하한 80→110 (오탐 = 어두운 남색 그림자 S88·V78 / 실제 스티커 S168~224)
    out += _all(cv2.inRange(hsv, (90, BLUE_S, BLUE_V), (130, 255, 255)), "blue", BLUE_MIN, 5000)   # 9/1 H 하한 95→90(실측 H98, 여유 확보). 초록 상한 85 와 미겹침

    # ── 빨간 점 (8/25 사용자가 빨강으로 변경 — 채도 높아 흑백 부품 모두에서 유리) ──
    mask_r = cv2.inRange(hsv, (0, RED_S, RED_V), (10, 255, 255)) | \
             cv2.inRange(hsv, (168, RED_S, RED_V), (180, 255, 255))
    out += _all(mask_r, "red", 15, 5000)

    # ── 노란 점 (8/26 추가): HSV 18~35, 실측 노란 점 면적 ≈500px @1280x720 ──
    out += _all(cv2.inRange(hsv, (18, YELLOW_S, 90), (35, 255, 255)), "yellow", 15, 5000)

    # ── 초록 점 (8/26 추가): 실측 펜 끝 HSV(83,193,74) — 청록 쪽·어두워서 V 하한 45, 파랑(95~)과 미겹침 ──
    # 8/26 저녁: 어두운 청록 그림자(색상 80~92·명도<70) 오탐 → 상한 85·채도 130·명도 75 로 조임
    # 8/27 오후: 조명이 바뀌어 가운데·아래 점이 흐려짐(실측 아래 H82 S108 V63, 면적 12) → V 62→52 · 면적 20→12.
    #   12프레임 검증: 현재값은 대부분 1점만, V52+area12 는 대부분 3점 + 벽 영역 밖 오탐 0.
    #   ★색상 상한 85 는 유지(어두운 청록 오탐 차단 핵심). V35 까지 낮추면 오탐 다발(8/27 실증) — 내리지 말 것.
    #   ★면적 하한 12 는 오검출(면적 14 블롭) 유발 — 20 유지. V52 만으로 아래 점 면적 159 확보(8/27 실측).
    out += _all(cv2.inRange(hsv, GREEN_LO, GREEN_HI), "green", 20, 5000)   # 기본 (48,80,52)~(85,255,255), CAM_HSV_GREEN 으로 오버라이드

    # ★반사상 컷 (9/2 규명): 벽들이 광택면이라 각 스티커가 '옆 벽'에 비쳐 같은 색 유령이
    #   오른쪽 25~90px(|dy|≤35)에 더 작게 맺힌다(실측: 노랑 3점 전부 +60px, 파랑 +37~47px,
    #   골든 기준에 박힌 면적60 유령 2개도 +47px). 같은 색 진짜 점은 세로 배열이라 이 기하와
    #   겹치지 않는다 → 왼쪽에 더 큰 동색 블롭이 있으면 반사상으로 버린다.
    cut = []
    for k, (x, y), a in out:
        ghost = False
        for k2, (x2, y2), a2 in out:
            # 9/2: 창은 반사 실측 범위(+37~60px)만 덮는 25~90 으로 한다. 300 까지 넓혔다가
            #   초록→빨강 교체로 '두 벽에 같은 색'(간격 ~183px)이 생기자 진짜 점을 잘라먹었다.
            #   같은 색 벽이 추가될 수 있으니 창을 벽 간격보다 좁게 유지하는 것이 원칙.
            if k2 == k and 25 <= (x - x2) <= 90 and abs(y - y2) <= 35 and a < a2 * 0.8:
                ghost = True
                break
        if not ghost:
            cut.append((k, (x, y), a))
    out = cut

    # 흰 점 검출은 8/26 제거(사용자 요청: 랙에서 흰 부품 경계 오탐). 백업 backup/cam_server.py.0826_before_shape
    return out


# ─────────────────────────────────────────────────────────────────────
# 8/27 영상 소스 추상화: read() → (color_bgr 1280x720, depth) / depth 는 get_distance(x,y)[m] 을 가진 객체 또는 None
# ─────────────────────────────────────────────────────────────────────
W, HGT = 1280, 720
SRC = {"name": "rs", "last_ts": 0.0, "frames": 0}   # /health 용
try:                                                     # ★9/12 기준점 오버레이(표시 전용)
    import sys as _sys, os as _os; _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tools"))
    import refpts_overlay as _RO
except Exception:
    _RO = None
_RO_SRC = {"rs": "wrist", "v4l2": "newcam"}


def _fit(img):
    """어떤 소스든 1280x720 으로 — 도트 픽셀 좌표(캘리브 Minv·ROI)가 이 해상도 기준."""
    if img is None:
        return None
    if img.shape[1] != W or img.shape[0] != HGT:
        img = cv2.resize(img, (W, HGT), interpolation=cv2.INTER_AREA)
    return img


class NpDepth:
    """numpy 뎁스(정렬된 1280x720, mm 또는 m) 를 rs.depth_frame.get_distance 처럼 노출."""
    def __init__(self, arr, scale_to_m):
        self.a = arr
        self.k = scale_to_m
    def get_distance(self, x, y):
        v = float(self.a[int(y), int(x)])
        return v * self.k if v > 0 else 0.0


class RsSource:
    """USB D435 직접 (원래 방식). CAM_EXPOSURE 지정 시 자동노출 끄고 그 값으로 고정.
    ★9/1: 조명 120Hz 플리커 — 노출은 166(=16.6ms) 이어야 흔들림 2px(다른 값은 35px)."""
    def __init__(self):
        import pyrealsense2 as rs      # 지연 import — ros/mjpeg 모드는 pyrealsense2 없어도 됨
        self.rs = rs
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, 1280, 720, rs.format.bgr8, 30)
        # 8/25: 뎁스 동시 스트림 + 컬러 정렬 — 도트까지 거리(mm)로 3축 보정("뎁스니까 z도 되잖아")
        cfg.enable_stream(rs.stream.depth, 848, 480, rs.format.z16, 30)
        self.align = rs.align(rs.stream.color)
        self.sen = None
        self.pipe = pipe
        prof = pipe.start(cfg)
        self.prof = prof
        # ★9/1: 자동노출/자동WB 가 프레임마다 출렁여 HSV 마스크 크기가 변하고,
        #   그 결과 같은 색 점 3개가 통째로 19x15px 씩 밀렸다(노랑은 49px·면적 80~213).
        #   "점이 깜빡이며 다른 자리로 옮겨 다닌다"의 진짜 원인. → 자동을 잠시 켜 수렴시킨 뒤 그 값으로 고정.
        if os.environ.get("CAM_LOCK_AE", "0") == "1":
            try:
                sen = None
                for s_ in prof.get_device().query_sensors():
                    if s_.get_info(rs.camera_info.name).lower().startswith("rgb"):
                        sen = s_; break
                self.sen = sen
                if sen is not None:
                    # ★자동이 켜진 채로 get_option(exposure) 를 읽으면 '자동이 적용 중인 값'이 아니라
                    #   수동 설정값(기본 166)이 나온다. 그걸 박으면 화면이 어두워져 도트가 붕괴한다(9/1 실증).
                    #   → 프레임 메타데이터의 actual_exposure/gain_level 을 쓰고, 안 되면 밝기로 맞춘다.
                    for _ in range(60):
                        fr = pipe.wait_for_frames()
                    tgt = float(np.mean(np.asanyarray(fr.get_color_frame().get_data())))
                    exp = gain = None
                    try:
                        cf = fr.get_color_frame()
                        MD = rs.frame_metadata_value
                        if cf.supports_frame_metadata(MD.actual_exposure):
                            exp = float(cf.get_frame_metadata(MD.actual_exposure))
                        if cf.supports_frame_metadata(MD.gain_level):
                            gain = float(cf.get_frame_metadata(MD.gain_level))
                    except Exception:
                        pass
                    sen.set_option(rs.option.enable_auto_exposure, 0)
                    sen.set_option(rs.option.enable_auto_white_balance, 0)
                    if exp:
                        sen.set_option(rs.option.exposure, exp)
                    if gain:
                        sen.set_option(rs.option.gain, gain)
                    # 밝기 검증 — 자동일 때 평균과 10% 넘게 벌어지면 노출을 비례 보정(최대 6회)
                    for _ in range(6):
                        for _ in range(8):
                            fr = pipe.wait_for_frames()
                        cur = float(np.mean(np.asanyarray(fr.get_color_frame().get_data())))
                        if abs(cur - tgt) <= tgt * 0.10 or cur < 1:
                            break
                        e = sen.get_option(rs.option.exposure)
                        rng = sen.get_option_range(rs.option.exposure)
                        e2 = max(rng.min, min(rng.max, e * (tgt / max(cur, 1.0))))
                        sen.set_option(rs.option.exposure, e2)
                    print(f"[rs] 노출 고정 exposure={sen.get_option(rs.option.exposure):.0f} "
                          f"gain={sen.get_option(rs.option.gain):.0f} "
                          f"밝기 {tgt:.1f}→{cur:.1f}  (해제: CAM_LOCK_AE=0)", flush=True)
            except Exception as e:
                print(f"[rs] 노출 고정 실패(자동 유지): {e}", flush=True)
        else:
            try:
                for s_ in prof.get_device().query_sensors():
                    if s_.get_info(rs.camera_info.name).lower().startswith("rgb"):
                        self.sen = s_
                exp = os.environ.get("CAM_EXPOSURE")
                gain = os.environ.get("CAM_GAIN")
                if self.sen is not None and exp:
                    self.sen.set_option(rs.option.enable_auto_exposure, 0)
                    self.sen.set_option(rs.option.exposure, float(exp))
                    if gain:
                        self.sen.set_option(rs.option.gain, float(gain))
                    # ★9/2: 여기서 '자동 화이트밸런스'를 안 껐던 것이 유령점의 진짜 원인.
                    #   노출만 고정하고 WB 를 자동으로 두면 프레임마다 색조가 흘러 HSV 마스크
                    #   크기가 출렁인다 → 파랑 검출 2~4개 왕복, 면적 편차 319(새캠은 9).
                    #   Intel 권고도 동일: AWB/AE 는 일관성이 없으니 고정값을 쓸 것.
                    #   Power Line Frequency 는 지역값 명시(한국 60Hz=2). Auto(3)는 헌팅한다.
                    try:
                        self.sen.set_option(rs.option.enable_auto_white_balance, 0)
                        self.sen.set_option(rs.option.white_balance,
                                            float(os.environ.get("CAM_WB", "4600")))
                    except Exception as e_:
                        print(f"[rs] WB 고정 실패: {e_}", flush=True)
                    try:
                        self.sen.set_option(rs.option.power_line_frequency,
                                            float(os.environ.get("CAM_PLF", "2")))
                    except Exception:
                        pass
                    wb = self.sen.get_option(rs.option.white_balance)
                    awb = self.sen.get_option(rs.option.enable_auto_white_balance)
                    print(f"[rs] 노출 고정 exposure={exp} gain={gain or '기본'} "
                          f"WB={wb:.0f}(자동 {awb:.0f}) PLF={self.sen.get_option(rs.option.power_line_frequency):.0f}"
                          f"  (★166=플리커 안전값 9/1, ★자동WB OFF 9/2)", flush=True)
                elif self.sen is not None:
                    self.sen.set_option(rs.option.enable_auto_exposure, 1)
                    self.sen.set_option(rs.option.enable_auto_white_balance, 1)
                    print("[rs] 자동노출/자동WB", flush=True)
            except Exception as e:
                print(f"[rs] 노출 설정 실패: {e}", flush=True)

    def set_emitter(self, on):
        """★IR 프로젝터(깊이용 점 패턴) 점멸이 RGB 프레임에 섞여 파랑 블롭이 주기적으로
        쪼그라들고 유령점이 생기는지 검증/차단하기 위한 스위치 (9/2).
        새캠(일반 USB)에는 없는 요소라 'D435 만 유독 심하다'는 관찰과 맞는다."""
        dev = self.prof.get_device() if hasattr(self, "prof") else None
        if dev is None:
            return None
        for s_ in dev.query_sensors():
            try:
                if s_.supports(self.rs.option.emitter_enabled):
                    s_.set_option(self.rs.option.emitter_enabled, 1.0 if on else 0.0)
                    return float(s_.get_option(self.rs.option.emitter_enabled))
            except Exception:
                continue
        return None

    def set_exposure(self, v):
        self.sen.set_option(self.rs.option.exposure, float(v))

    def get_exposure(self):
        return float(self.sen.get_option(self.rs.option.exposure))

    def read(self):
        frames = self.align.process(self.pipe.wait_for_frames())
        return np.asanyarray(frames.get_color_frame().get_data()), frames.get_depth_frame()


class RosSource:
    """ROS2 토픽 구독 (비전 PC 가 카메라를 들고 있을 때). rclpy 스핀은 백그라운드 스레드."""
    STALE_REDISCOVER_S = 15.0     # 이만큼 프레임이 없으면 토픽 재탐색(비전 PC 가 재기동/이름 변경)

    def __init__(self, color_topic, depth_topic):
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image, CompressedImage
        rclpy.init()
        self.node = Node("bf2_cam_server_sub")
        self.qos = qos_profile_sensor_data
        self.Image, self.CompressedImage = Image, CompressedImage
        self.cv = threading.Condition()
        self.color = None
        self.depth = None
        self.seq = 0
        self.subs = []
        self.want_color, self.want_depth = color_topic, depth_topic   # "auto" 가능
        self.color_topic = self.depth_topic = None
        self.compressed = False
        threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True).start()
        self._subscribe()

    def _discover(self):
        """토픽 목록에서 컬러/뎁스 이미지 토픽을 고른다.
        컬러: 이름에 color|rgb 포함 + Image/CompressedImage. ★compressed 우선(WiFi 대역 1/10) → raw.
        뎁스: aligned_depth 포함 Image 만(정렬 안 된 raw depth 는 픽셀이 안 맞아 오히려 해로움)."""
        # ★퍼블리셔가 있는 토픽만 — 우리 옛 구독이 그래프에 남긴 이름을 다시 고르는 함정(8/27 재탐색 실패 진범)
        tt = {t: ty for t, ty in self.node.get_topic_names_and_types()
              if self.node.count_publishers(t) > 0}
        imgs = [t for t, ty in tt.items() if "sensor_msgs/msg/Image" in ty]
        comps = [t for t, ty in tt.items() if "sensor_msgs/msg/CompressedImage" in ty]
        def _is_color(t):
            l = t.lower()
            return ("color" in l or "rgb" in l) and "depth" not in l and "infra" not in l
        c_comp = sorted(t for t in comps if _is_color(t) and "compressedDepth" not in t)
        c_raw = sorted(t for t in imgs if _is_color(t))
        color = (c_comp or c_raw or [None])[0]
        depth = sorted(t for t in imgs if "aligned_depth" in t.lower())
        return color, (depth[0] if depth else None), (imgs + comps)

    def _subscribe(self):
        for sub in self.subs:
            self.node.destroy_subscription(sub)
        self.subs = []
        color, depth = self.want_color, self.want_depth
        if color == "auto" or depth == "auto":
            dc, dd, all_t = self._discover()
            if color == "auto":
                color = dc
            if depth == "auto":
                depth = dd
            if color is None:
                print(f"[ros] 컬러 이미지 토픽 없음 — 보이는 이미지 토픽: {all_t or '(없음)'}  (재탐색 대기)")
                return False
        self.color_topic, self.depth_topic = color, depth
        self.compressed = color.endswith("/compressed")
        ctype = self.CompressedImage if self.compressed else self.Image
        self.subs.append(self.node.create_subscription(ctype, color, self._on_color, self.qos))
        if depth:
            self.subs.append(self.node.create_subscription(self.Image, depth, self._on_depth, self.qos))
        print(f"[ros] color={color}{' (compressed)' if self.compressed else ''}  depth={depth or '-'}"
              f"  domain={os.environ.get('ROS_DOMAIN_ID','0')}")
        SRC["topic"] = color
        return True

    def _on_color(self, msg):
        try:
            if self.compressed:
                img = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
            else:
                img = self._img(msg)
        except Exception as e:      # 인코딩 미지원 등 — 죽지 말고 건너뜀
            self.node.get_logger().warn(f"color decode fail: {e}", throttle_duration_sec=5)
            return
        with self.cv:
            self.color = img
            self.seq += 1
            self.cv.notify_all()

    def _on_depth(self, msg):
        try:
            a = np.frombuffer(msg.data, np.uint16 if msg.encoding == "16UC1" else np.float32)
            a = a.reshape(msg.height, msg.width)
            k = 0.001 if msg.encoding == "16UC1" else 1.0
            if (msg.width, msg.height) != (W, HGT):
                a = cv2.resize(a, (W, HGT), interpolation=cv2.INTER_NEAREST)
            with self.cv:
                self.depth = NpDepth(a, k)
        except Exception as e:
            self.node.get_logger().warn(f"depth decode fail: {e}", throttle_duration_sec=5)

    @staticmethod
    def _img(msg):
        enc = msg.encoding
        if enc in ("bgr8", "rgb8"):
            a = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 3)
            return cv2.cvtColor(a, cv2.COLOR_RGB2BGR) if enc == "rgb8" else a.copy()
        if enc in ("bgra8", "rgba8"):
            a = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width, 4)
            return cv2.cvtColor(a, cv2.COLOR_RGBA2BGR if enc == "rgba8" else cv2.COLOR_BGRA2BGR)
        if enc == "mono8":
            return cv2.cvtColor(np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.width),
                                cv2.COLOR_GRAY2BGR)
        raise ValueError(f"encoding {enc}")

    def read(self):
        """새 컬러 프레임이 올 때까지 대기(최대 2s). 안 오면 None → 호출부가 '신호 없음' 표시.
        STALE_REDISCOVER_S 동안 프레임이 없으면(비전 PC 재기동·토픽명 변경) 토픽 재탐색."""
        with self.cv:
            s0 = self.seq
            self.cv.wait_for(lambda: self.seq != s0, timeout=2.0)
            if self.seq != s0:
                self._stale_since = None
                return self.color, self.depth
        now = time.time()
        if getattr(self, "_stale_since", None) is None:
            self._stale_since = now
        elif now - self._stale_since > self.STALE_REDISCOVER_S:
            self._stale_since = now
            print("[ros] 프레임 없음 → 토픽 재탐색")
            self._subscribe()
        return None, None


class UdpSource:
    """순수 UDP JPEG 조각 수신 (송신 = cam_udp_send.py, ROS/DDS 불필요). 헤더 '!4sIBHHI' 17B, 자세한 건 송신기 docstring."""
    HDR = None

    def __init__(self, port):
        import socket
        import struct
        self.HDR = struct.Struct("!4sIBHHI")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(("0.0.0.0", port))
        self.port = port
        self.cv = threading.Condition()
        self.color = None
        self.depth = None
        self.seq = 0
        self.peer = None
        self.stat = {"frames": 0, "dropped": 0, "pkts": 0, "bad_magic": 0}
        SRC["udp"] = self.stat
        threading.Thread(target=self._rx, daemon=True).start()
        print(f"[udp] listen 0.0.0.0:{port}")
        SRC["topic"] = f"udp:{port}"

    def _rx(self):
        bufs = {}          # (fid, kind) → {"n":cnt, "total":len, "parts":{idx:bytes}}
        last_fid = {0: None, 1: None}     # None = 아직 수신 전 → 어떤 id 든 수락
        last_rx = 0.0
        H = self.HDR
        while True:
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except OSError:
                continue
            if len(pkt) < H.size:
                continue
            magic, fid, kind, idx, cnt, total = H.unpack_from(pkt)
            self.stat["pkts"] = self.stat.get("pkts", 0) + 1
            if magic != b"BF2C" or kind not in (0, 1):
                self.stat["bad_magic"] = self.stat.get("bad_magic", 0) + 1
                if self.stat["bad_magic"] <= 3:
                    print(f"[udp] 매직 불일치 from {addr[0]}: {pkt[:8]!r} len={len(pkt)}")
                continue
            now = time.time()
            if now - last_rx > 1.0:
                # 1s 이상 무신호 뒤 첫 패킷 = 송신기 재기동으로 간주 → frame_id 기준 리셋
                #   (8/27: 재기동한 송신기가 id 1 부터 다시 시작해 "과거 프레임" 필터에 전부 걸린 사고)
                last_fid = {0: None, 1: None}
                bufs.clear()
            last_rx = now
            if self.peer != addr[0]:
                self.peer = addr[0]
                SRC["topic"] = f"udp:{self.port} ← {addr[0]}"
                print(f"[udp] 송신자 {addr[0]}")
            # 과거 프레임 조각은 버림(uint32 랩어라운드 고려해 반차이로 비교)
            lf = last_fid[kind]
            if lf is not None and fid != lf and ((lf - fid) & 0xFFFFFFFF) < 0x7FFFFFFF:
                continue
            key = (fid, kind)
            b = bufs.get(key)
            if b is None:
                b = bufs[key] = {"n": cnt, "total": total, "parts": {}}
            b["parts"][idx] = pkt[H.size:]
            if len(b["parts"]) < b["n"]:
                continue
            data = b"".join(b["parts"][i] for i in range(b["n"]))
            del bufs[key]
            # 같은 kind 의 더 오래된 미완성 프레임 정리(손실분)
            for k in [k for k in bufs if k[1] == kind and k[0] != fid]:
                del bufs[k]; self.stat["dropped"] += 1
            last_fid[kind] = fid
            if len(data) != total or not data:
                continue
            arr = np.frombuffer(data, np.uint8)
            if arr.size == 0:      # 9/1: 빈 페이로드가 imdecode 어서션을 터뜨려 _rx 스레드가 통째로 죽었다
                continue           #      (그 뒤로는 소켓만 열린 채 아무도 안 읽는 '조용한 귀머거리' 상태)
            if kind == 0:
                try:
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                except Exception:
                    continue
                if img is None:
                    continue
                with self.cv:
                    self.color = img
                    self.seq += 1
                    self.stat["frames"] += 1
                    self.cv.notify_all()
            else:
                try:
                    dep = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                except Exception:
                    continue
                if dep is None or dep.dtype != np.uint16:
                    continue
                if (dep.shape[1], dep.shape[0]) != (W, HGT):
                    dep = cv2.resize(dep, (W, HGT), interpolation=cv2.INTER_NEAREST)
                with self.cv:
                    self.depth = NpDepth(dep, 0.001)

    def read(self):
        with self.cv:
            s0 = self.seq
            self.cv.wait_for(lambda: self.seq != s0, timeout=2.0)
            if self.seq == s0:
                return None, None
            return self.color, self.depth


class MjpegSource:
    """cv2.VideoCapture 가 여는 URL(MJPEG http / RTSP). 컬러만."""
    def __init__(self, url):
        self.url = url
        self.cap = cv2.VideoCapture(url)
        if not self.cap.isOpened():
            raise RuntimeError(f"open fail: {url}")

    def read(self):
        ok, img = self.cap.read()
        if not ok:
            self.cap.release(); time.sleep(1.0)
            self.cap = cv2.VideoCapture(self.url)      # 끊기면 재접속
            return None, None
        return img, None


class V4l2Source:
    """USB UVC 카메라(뎁스 없음) — 9/1 손목 두 번째 카메라(Realtek 0bda:5844).
    ★USB 재삽입·D435 연결로 /dev/videoN 번호가 밀린다(실측: video0→video1).
      고정 경로 대신 udev 시리얼로 매번 다시 찾는다."""

    def __init__(self, dev="", serial="", rot=0, fps=0, capw=1280, caph=720):
        self.capw, self.caph = capw, caph
        self.want_dev, self.serial, self.rot = dev, serial, rot % 360
        self.cap = None
        self.dev = None
        # 9/1: 이 카메라는 30fps 로 JPEG 를 만들 이유가 없다(검출 8Hz, 눈으로는 15fps 면 충분).
        #      상한을 걸면 CPU 62% → 30%대. 0 = 무제한.
        self.min_dt = (1.0 / fps) if fps and fps > 0 else 0.0
        self._t_last = 0.0

    def _find(self):
        if self.want_dev and os.path.exists(self.want_dev):
            return self.want_dev
        import glob
        import subprocess
        for d in sorted(glob.glob("/dev/video*")):
            try:
                pr = subprocess.run(["udevadm", "info", "-q", "property", "-n", d],
                                    capture_output=True, text=True, timeout=3).stdout
            except Exception:
                continue
            if "ID_V4L_CAPABILITIES=:capture:" not in pr:
                continue
            if self.serial and f"ID_SERIAL_SHORT={self.serial}" not in pr:
                continue
            if not self.serial and "RealSense" in pr:
                continue          # 시리얼 미지정이면 D435 는 건너뛴다(손목캠 몫)
            return d
        return self.want_dev or None

    def _open(self):
        dev = self._find()
        if not dev:
            return False
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.capw)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.caph)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            # 9/1: 노출 고정 실험 철회 — 자동노출/자동WB 를 명시적으로 복원.
            #   (한 번 수동(1)으로 바꾼 UVC 설정은 장치에 남는다 → 켤 때마다 자동(3)으로 되돌려야 함)
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)          # V4L2: 3=auto, 1=manual
            cap.set(cv2.CAP_PROP_AUTO_WB, 1)
            print("[v4l2] 자동노출/자동WB 복원", flush=True)
        except Exception:
            pass
        self.cap, self.dev = cap, dev
        print(f"[v4l2] {dev} 열림 (rot={self.rot})", flush=True)
        return True

    def read(self):
        if self.cap is None and not self._open():
            time.sleep(2)
            return None, None
        if self.min_dt:
            # ★9/1 2차: read() 앞에서 sleep 하는 방식은 효과가 없었다(루프가 이미 느리면 sleep=0,
            #   게다가 프레임은 전부 디코딩됨 → 실측 100%). grab() 은 디코딩 없이 큐만 비우므로
            #   목표 시각 전까지는 grab 으로 버리고, 시각이 되면 그때만 retrieve(디코딩)한다.
            while True:
                now = time.time()
                if now - self._t_last >= self.min_dt:
                    break
                if not self.cap.grab():
                    break
                time.sleep(0.002)
            self._t_last = time.time()
            ok, f = self.cap.retrieve()
        else:
            ok, f = self.cap.read()
        if not ok:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
            return None, None
        if self.rot == 90:
            f = cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE)
        elif self.rot == 180:
            f = cv2.rotate(f, cv2.ROTATE_180)
        elif self.rot == 270:
            f = cv2.rotate(f, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return f, None          # 뎁스 없음



# ─────────────────────────────────────────────────────────────────────
# 9/1 도트 시간적 안정화 — "점이 깜빡이며 다른 위치로 옮겨 다닌다"(사용자)
#   원인: 매 검출마다 전부 새로 찾아 그대로 내보냄 → ①잡티 블롭이 1~2프레임만 떴다 사라짐
#         ②같은 점도 무게중심이 ±1~2px 떨림.
#   대책: 프레임 간 연결(association) → 연속 CONFIRM_N 회 잡힌 것만 공개, MISS_N 회 놓쳐야 삭제,
#         위치는 최근 SMOOTH_N 개의 중앙값. 정지 상태에서 표시가 고정되고 잡티는 아예 안 보인다.
#   ★평균이 아니라 중앙값 — 8/30 에 '프레임 평균이 존재하지 않는 좌표를 만든' 사고가 있었다.
# ─────────────────────────────────────────────────────────────────────
STAB_ON = os.environ.get("CAM_STABLE", "1") != "0"
ASSOC_R = float(os.environ.get("CAM_ASSOC_R", "15"))   # 정지 시 진짜 점은 2px 도 안 움직인다. 반사 점프가 (+20,+15)=25px 라 25면 그걸 따라간다(9/1 실증) — 15 로 분리   # 같은 점으로 볼 최대 이동(px/검출주기)
CONFIRM_N = int(os.environ.get("CAM_CONFIRM_N", "3"))  # 이만큼 연속 보이면 공개 — 유령(반사상, ≤5/30회)은 3연속이 거의 안 나온다(9/1 인구조사)
MISS_N = int(os.environ.get("CAM_MISS_N", "3"))        # 이만큼 연속 놓치면 삭제
SMOOTH_N = int(os.environ.get("CAM_SMOOTH_N", "5"))


class DotTracker:
    def __init__(self):
        self.tracks = []      # {kind, xs[], ys[], areas[], hits, miss, conf}

    def update(self, dots):
        used = [False] * len(dots)
        for tr in self.tracks:
            best, bd = -1, ASSOC_R ** 2
            for i, (kind, (x, y), area) in enumerate(dots):
                if used[i] or kind != tr["kind"]:
                    continue
                d = (x - tr["xs"][-1]) ** 2 + (y - tr["ys"][-1]) ** 2
                if d < bd:
                    best, bd = i, d
            if best >= 0:
                used[best] = True
                k, (x, y), area = dots[best]
                tr["xs"].append(x); tr["ys"].append(y); tr["areas"].append(area)
                del tr["xs"][:-SMOOTH_N]; del tr["ys"][:-SMOOTH_N]; del tr["areas"][:-SMOOTH_N]
                tr["hits"] += 1; tr["miss"] = 0
                if tr["hits"] >= CONFIRM_N:
                    tr["conf"] = True
            else:
                tr["miss"] += 1
                tr["hits"] = 0
        self.tracks = [t for t in self.tracks if t["miss"] < MISS_N]
        # ★같은 색에서 면적이 훨씬 큰 트랙이 근처(150px)에 있으면 작은 쪽은 유령으로 보고 버린다.
        #   (9/1: 클릭 순간 잡힌 면적 80 유령이 실제 점 면적 307 을 밀어내고 계속 출력됐다)
        big = [t for t in self.tracks if t["conf"] and t["areas"]
               and sorted(t["areas"])[len(t["areas"])//2] >= 150]
        keep = []
        for t in self.tracks:
            am = sorted(t["areas"])[len(t["areas"])//2] if t["areas"] else 0
            ghost = any(b is not t and b["kind"] == t["kind"] and am * 2.5 < (sorted(b["areas"])[len(b["areas"])//2])
                        and (b["xs"][-1]-t["xs"][-1])**2 + (b["ys"][-1]-t["ys"][-1])**2 < 150.0**2
                        for b in big)
            if not ghost:
                keep.append(t)
        self.tracks = keep
        for i, (kind, (x, y), area) in enumerate(dots):
            if used[i]:
                continue
            # ★반사상 억제(9/1 인구조사): 유령은 항상 진짜 점의 오른쪽 25~50px 에 뜬다.
            #   같은 색 진짜 점 사이 간격은 190px — 확정 트랙 80px 안의 '연결 안 된' 같은 색 검출은
            #   반사상으로 보고 버린다(트랙도 안 만든다).
            near = [t for t in self.tracks if t["kind"] == kind and t["conf"]
                    and (x - t["xs"][-1]) ** 2 + (y - t["ys"][-1]) ** 2 < 80.0 ** 2]
            if near:
                tr = min(near, key=lambda t: (x - t["xs"][-1]) ** 2 + (y - t["ys"][-1]) ** 2)
                if tr["miss"] >= 2:
                    # ★트랙이 2회 연속 실검출을 못 받았다 = 그 자리에 점이 진짜로 없다(팔이 움직였다).
                    #   근처의 이 검출이 새 진짜 위치 → 트랙을 통째로 갈아탄다.
                    #   (갈아타기 없이 '존재 증거'로만 쓰면 낡은 트랙이 옛 자리에 영원히 살아남아
                    #    50px 어긋난 표시가 계속됐다 — 9/1 블루 호버에서 실증)
                    tr["xs"], tr["ys"], tr["areas"] = [x], [y], [area]
                    tr["miss"] = 0; tr["hits"] += 1
                else:
                    # 반사상 교대(스티커↔반사): 위치는 버리되 존재 증거로 깜빡임만 방지
                    tr["miss"] = 0
                continue
            self.tracks.append({"kind": kind, "xs": [x], "ys": [y], "areas": [area],
                                "hits": 1, "miss": 0, "conf": CONFIRM_N <= 1})
        out = []
        for tr in self.tracks:
            # ★한 번 확정된 점은 MISS_N 회 연속으로 놓칠 때까지 계속 보여준다.
            #   (miss>0 이면 감추던 초기 코드 = 한 프레임만 놓쳐도 깜빡임 → 개수 6~13 요동)
            if not tr["conf"]:
                continue
            xs, ys = sorted(tr["xs"]), sorted(tr["ys"])
            out.append((tr["kind"],
                        (round(xs[len(xs) // 2], 2), round(ys[len(ys) // 2], 2)),
                        float(sorted(tr["areas"])[len(tr["areas"]) // 2])))
        out.sort(key=lambda t: (t[0], t[1][1], t[1][0]))
        return out


tracker = DotTracker()


DET_HZ = float(os.environ.get("CAM_DET_HZ", "8"))   # 9/1: 스트림은 30fps, 검출만 8Hz
_det = {"t": 0.0, "dots": [], "info": "no marker"}


def cam_loop(src):
    dets = {n: make_detector(n) for n in DICT_NAMES}
    locked = None            # 한 번 잡힌 딕셔너리로 고정(프레임률 확보)
    n_frame = 0
    blank = np.zeros((HGT, W, 3), np.uint8)
    while True:
        img, depth_f = src.read()
        if img is None:
            # 소스 끊김 — 검은 화면에 표시만 갱신(콘솔 워치독이 스트림은 살아있다고 보게)
            b = blank.copy()
            cv2.putText(b, f"NO SIGNAL ({SRC['name']}) {time.time()-SRC['last_ts']:.0f}s",
                        (300, 360), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            ok, jpg = cv2.imencode(".jpg", b, [cv2.IMWRITE_JPEG_QUALITY, 60])
            with lock:
                latest["jpg"] = jpg.tobytes(); latest["info"] = "no signal"; latest["dots"] = []
            continue
        img = _fit(img)
        # ★★9/5 진범: 지금까지 오버레이를 **소스가 돌려준 버퍼에 직접** 그려 왔다.
        #   RealSense 는 프레임 버퍼를 재활용하므로, 그 버퍼가 다시 돌아올 때 지난 프레임의
        #   원·십자·글씨가 남아 있다. 그 결과 ①/raw 가 30회 중 7회 주석 섞인 프레임을 내주고
        #   ②검출 입력 raw 에도 지난 그림이 섞여 **검출기가 자기가 그린 점을 다시 검출**한다
        #   (오래 시달린 '유령점'의 유력한 원인. 8/28 에 raw=img.copy() 로 고친 줄 알았지만
        #    복사 시점이 아니라 '어디에 그리느냐'가 문제였다).
        #   → 화면용으로 한 장 복사해 두고, 그리기는 전부 그 복사본에만 한다.
        img = img.copy()
        SRC["last_ts"] = time.time(); SRC["frames"] += 1
        n_frame += 1
        # 9/1: 검출(ArUco+도트+뎁스중앙값)이 매 프레임 돌아 CPU 182% → 8Hz 로 분리.
        #      스트림/오버레이는 그대로 전 프레임. 검출 사이엔 직전 결과를 그려준다.
        do_det = (time.time() - _det["t"]) >= (1.0 / DET_HZ if DET_HZ > 0 else 0.0)
        raw = img.copy() if do_det else None   # ⚠검출은 원본에서(8/25 오버레이 오검출 사고)
        info = "no marker"
        try_names = ([locked] if locked else DICT_NAMES) if do_det else []
        for name in try_names:
            corners, ids, _ = dets[name](img)
            if ids is not None and len(ids):
                locked = name
                # 9/1: ID 로 신원이 확정되고 코너가 서브픽셀 → yaw 기준선으로 최적(도트 쌍은 매 측정
                #   '가장 먼 두 점'이 바뀌어 각도가 7° 튀었다). /aruco 로 노출.
                latest["aruco"] = [{"id": int(i), "corners": [[round(float(x), 2), round(float(y), 2)]
                                                              for x, y in c[0]]}
                                   for i, c in zip(ids.flatten(), corners)]
                latest["aruco_t"] = time.time()
                cv2.aruco.drawDetectedMarkers(img, corners, ids)
                pxs = [f"id{int(i)}:{np.linalg.norm(c[0][0]-c[0][1]):.0f}px"
                       for i, c in zip(ids.flatten(), corners)]
                info = f"{name}  " + " ".join(pxs)
                break
        else:
            if locked and try_names:
                locked = None    # 놓치면 다시 전수 스캔
        # ── 파지점 도트 검출 (8/25 사용자 방식: 검은 부품=흰 점, 흰 부품=파란 점) ──
        if not do_det:
            dots, info = _det["dots"], _det["info"]
        if do_det:
            dots = [d for d in detect_dots(raw)
                    if 20 < d[1][0] < W - 20 and 20 < d[1][1] < HGT - 35    # 가장자리 오검출 제거(하단 35: 테이블 모서리 유령줄, 9/1)
                    and not any(x0 <= d[1][0] <= x1 and y0 <= d[1][1] <= y1
                                for x0, y0, x1, y1 in EXCLUDE)]
            latest["dots_raw"] = [{"kind": k, "px": x, "py": y, "area": a}
                                  for k, (x, y), a in dots]                # /dots?raw=1 로 확인용
            if STAB_ON:
                dots = tracker.update(dots)
        _ro = bool(_RO is not None and SRC["name"] in _RO_SRC)   # ★9/12 항상 기준점만
        fk = focus["kinds"]
        if fk and do_det:
            dots = [d for d in dots if d[0] in fk]
        sxy = seed["xy"]
        if sxy and do_det:
            near = [d for d in dots
                    if (d[1][0]-sxy[0])**2 + (d[1][1]-sxy[1])**2 < SEED_R**2]
            near.sort(key=lambda d: (d[1][0]-sxy[0])**2 + (d[1][1]-sxy[1])**2)
            dots = near[:1]
            if dots:
                seed["xy"] = list(dots[0][1])   # 추적: 찾은 위치로 시드 갱신
            cv2.circle(img, tuple(map(int, sxy)), SEED_R, (255, 0, 255), 1)
            cv2.drawMarker(img, tuple(map(int, sxy)), (255, 0, 255),
                           cv2.MARKER_TILTED_CROSS, 24, 2)
        for kind, (dxf, dyf), area in ([] if _ro else dots):
            dx, dy = int(round(dxf)), int(round(dyf))
            color = {"white": (255, 255, 255), "blue": (255, 128, 0),
                     "red": (0, 0, 255), "yellow": (0, 200, 255),
                     "green": (0, 255, 0)}.get(kind, (0, 255, 0))
            cv2.drawMarker(img, (dx, dy), color, cv2.MARKER_CROSS, 30, 2)
            cv2.circle(img, (dx, dy), 12, color, 2)
            cv2.putText(img, kind, (dx + 14, dy - 10), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, color, 2)
        if dots:
            info += "  |  " + " ".join(f"{k}({x:.0f},{y:.0f})" for k, (x, y), _ in dots)
        for x0, y0, x1, y1 in EXCLUDE:
            cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (90, 90, 90), 1)
        cv2.drawMarker(img, (W // 2, HGT // 2), (0, 0, 255), cv2.MARKER_CROSS, 20, 1)  # 화면 중앙
        if fk:
            cv2.putText(img, "FOCUS " + "+".join(sorted(fk)), (1000, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 0, 255), 2)
        if not _ro:
            cv2.putText(img, info, (12, 705), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 255, 255), 2)
        if _RO is not None and SRC["name"] in _RO_SRC:
            try: _RO.draw(img, _RO_SRC[SRC["name"]])         # 기준점/매칭점(기준점만 모드가 아니어도 위에 겹침)
            except Exception: pass
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with lock:
                latest["jpg"] = jpg.tobytes()
                if do_det:
                    latest["raw"] = raw        # 8/28 /raw·/hsv 용 (9/1: 검출 프레임에서만 = 8Hz)
                latest["info"] = info
                def _dmm(x, y, r=2):
                    """(x,y) 주변 (2r+1) 창 뎁스 중앙값(mm) — 0(무효)은 제외."""
                    if depth_f is None:
                        return None
                    try:
                        vs = [depth_f.get_distance(min(W - 1, max(0, x+dx)),
                                                   min(HGT - 1, max(0, y+dy))) * 1000.0
                              for dx in (-r, 0, r) for dy in (-r, 0, r)]
                        vs = sorted(v for v in vs if v > 1)
                        return round(vs[len(vs)//2], 1) if vs else None
                    except Exception:
                        return None
                if depth_img.get("want"):                # 8/30 /depthview
                    try:
                        if depth_f is None:
                            depth_img["info"] = "뎁스 없음(소스에 뎁스 스트림이 없음)"
                            depth_img["jpg"] = None
                        else:
                            raw16 = np.asanyarray(depth_f.get_data())
                            v = raw16.astype(np.float32)
                            valid = v > 0
                            if valid.sum() < 100:
                                depth_img["info"] = "유효 뎁스 픽셀 거의 없음 — 너무 가깝거나(<175mm) 반사"
                            else:
                                lo = float(np.percentile(v[valid], 5)); hi = float(np.percentile(v[valid], 95))
                                depth_img["info"] = (f"유효 {100*valid.mean():.0f}% · 5~95% 구간 "
                                                     f"{lo*0.001*1000:.0f}~{hi*0.001*1000:.0f}(raw)")
                            hi2 = max(hi if valid.sum() >= 100 else 1.0, 1.0)
                            n = np.clip((v / hi2) * 255.0, 0, 255).astype(np.uint8)
                            col = cv2.applyColorMap(n, cv2.COLORMAP_JET)
                            col[~valid] = (0, 0, 0)          # 무효는 검정
                            if col.shape[:2] != img.shape[:2]:
                                col = cv2.resize(col, (img.shape[1], img.shape[0]))
                            ok2, j2 = cv2.imencode(".jpg", col, [cv2.IMWRITE_JPEG_QUALITY, 75])
                            depth_img["jpg"] = j2.tobytes() if ok2 else None
                        depth_img["t"] = time.time()
                    except Exception as e:
                        depth_img["info"] = f"뎁스 변환 실패: {e}"; depth_img["jpg"] = None
                    depth_img["want"] = False
                if depth_req.get("box") is not None:      # 8/30 /depthgrid
                    _x0, _y0, _x1, _y1, _nx, _ny, _r = depth_req["box"]
                    _g = []
                    for _j in range(_ny):
                        for _i in range(_nx):
                            _x = int(round(_x0 + (_x1-_x0) * (_i/(_nx-1) if _nx > 1 else 0)))
                            _y = int(round(_y0 + (_y1-_y0) * (_j/(_ny-1) if _ny > 1 else 0)))
                            _g.append([_x, _y, _dmm(_x, _y, _r)])
                    depth_req["res"] = _g
                    depth_req["box"] = None
                if do_det:
                    latest["dots"] = [{"kind": k, "px": x, "py": y, "area": a,
                                       "depth_mm": _dmm(int(round(x)), int(round(y)))}
                                      for k, (x, y), a in dots]
                    _det["dots"] = dots; _det["info"] = info; _det["t"] = time.time()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 요청 로그 소음 제거
        pass

    def do_GET(self):
        if self.path.startswith("/raw"):
            # 8/28: 오버레이 없는 원본 프레임(JPEG q95) — 색 임계 실측용
            with lock:
                rw = latest.get("raw")
            if rw is None:
                self.send_response(503); self.end_headers(); return
            ok, j = cv2.imencode(".jpg", rw, [cv2.IMWRITE_JPEG_QUALITY, 95])
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.end_headers()
            self.wfile.write(j.tobytes()); return
        if self.path.startswith("/hsv"):
            # 8/28: /hsv?x=&y=&r=5 → 원본 프레임 (x,y) 주변 (2r+1)^2 창의 HSV 중앙값 + BGR
            q = parse_qs(urlparse(self.path).query)
            with lock:
                rw = latest.get("raw")
            try:
                x = int(float(q["x"][0])); y = int(float(q["y"][0])); r = int(q.get("r", ["5"])[0])
            except (KeyError, ValueError):
                self.send_response(400); self.end_headers(); return
            if rw is None:
                self.send_response(503); self.end_headers(); return
            win = rw[max(0,y-r):y+r+1, max(0,x-r):x+r+1]
            hv = cv2.cvtColor(win, cv2.COLOR_BGR2HSV).reshape(-1, 3)
            bg = win.reshape(-1, 3)
            out = {"x": x, "y": y, "hsv": [int(v) for v in np.median(hv, axis=0)],
                   "bgr": [int(v) for v in np.median(bg, axis=0)]}
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(out).encode()); return
        if self.path.startswith("/depthview"):
            html = (b"<body style='margin:0;background:#111;color:#ddd;font-family:sans-serif'>"
                    b"<div style='padding:6px'>\xec\x86\x90\xeb\xaa\xa9\xec\xba\xa0 \xeb\x8e\x81\xec\x8a\xa4 "
                    b"(\xea\xb2\x80\xec\xa0\x95=\xeb\xac\xb4\xed\x9a\xa8 \xc2\xb7 \xed\x8c\x8c\xeb\x9e\x91=\xea\xb0\x80\xea\xb9\x8c\xec\x9b\x80 \xc2\xb7 "
                    b"\xeb\xb9\xa8\xea\xb0\x95=\xeb\xa9\x80\xec\x9d\x8c)</div>"
                    b"<img id=d style='width:49vw'><img id=c style='width:49vw'>"
                    b"<div id=t style='padding:6px;font-size:13px'></div><script>"
                    b"function f(){document.getElementById('d').src='/depthimg?'+Date.now();"
                    b"document.getElementById('c').src='/raw?'+Date.now();"
                    b"fetch('/depthinfo').then(r=>r.text()).then(t=>document.getElementById('t').textContent=t);"
                    b"setTimeout(f,400);}f();</script></body>")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html))); self.end_headers()
            self.wfile.write(html); return
        if self.path.startswith("/depthinfo"):
            with lock: depth_img["want"] = True
            t0 = time.time()
            while time.time() - t0 < 3.0 and depth_img["t"] < t0: time.sleep(0.02)
            b = depth_img.get("info", "").encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b); return
        if self.path.startswith("/depthimg"):
            with lock: depth_img["want"] = True
            t0 = time.time()
            while time.time() - t0 < 3.0 and depth_img["t"] < t0: time.sleep(0.02)
            j = depth_img.get("jpg")
            if not j:
                self.send_response(503); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(j); return
        if self.path.startswith("/depthgrid"):
            # 8/30: /depthgrid?x0=&y0=&x1=&y1=&nx=&ny=&r= → 격자 점의 뎁스(mm) 목록.
            #   물고 있는 벽면에 격자를 깔고 평면 피팅 → 파지 기울기를 직접 계측.
            q = parse_qs(urlparse(self.path).query)
            try:
                box = (int(float(q["x0"][0])), int(float(q["y0"][0])),
                       int(float(q["x1"][0])), int(float(q["y1"][0])),
                       int(q.get("nx", ["8"])[0]), int(q.get("ny", ["8"])[0]),
                       int(q.get("r", ["2"])[0]))
            except (KeyError, ValueError):
                self.send_response(400); self.end_headers(); return
            with lock:
                depth_req["res"] = None; depth_req["box"] = box
            t0 = time.time(); res = None
            while time.time() - t0 < 4.0:
                with lock:
                    res = depth_req.get("res")
                if res is not None: break
                time.sleep(0.02)
            if res is None:
                self.send_response(503); self.end_headers(); return
            out = {"pts": [{"x": a, "y": b, "d": c} for a, b, c in res]}
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(out).encode()); return
        if self.path.startswith("/click"):
            # 라이브 화면 클릭 → 타깃 시드 설정 (x,y = 1280x720 좌표) / clear=1 해제
            q = parse_qs(urlparse(self.path).query)
            if q.get("clear"):
                seed["xy"] = None
            else:
                try:
                    seed["xy"] = [float(q["x"][0]), float(q["y"][0])]
                except (KeyError, ValueError):
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(str(seed["xy"]).encode())
            return
        if self.path.startswith("/focus"):
            q = parse_qs(urlparse(self.path).query)
            if "clear" in q or not q.get("kinds", [""])[0]:
                focus["kinds"] = None
            else:
                focus["kinds"] = set(k.strip() for k in q["kinds"][0].split(",") if k.strip())
            self.send_response(200); self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(str(sorted(focus["kinds"]) if focus["kinds"] else None).encode())
            return
        if self.path.startswith("/expo"):
            # 9/1: 재기동 없이 노출 조절. /expo (조회) · /expo?set=8000 · /expo?bright=95 (평균밝기 목표)
            import json as _j
            q = parse_qs(urlparse(self.path).query)
            src = SRCOBJ.get("o")
            msg = {}
            with lock:
                rw = latest.get("raw")
            cur_b = float(np.mean(rw)) if rw is not None else None
            if src is None or not hasattr(src, "set_exposure") or getattr(src, "sen", None) is None:
                msg = {"err": "이 소스는 노출 조절 불가", "bright": cur_b}
            else:
                if "set" in q:
                    src.set_exposure(float(q["set"][0]))
                if "awb" in q and getattr(src, "sen", None) is not None:
                    src.sen.set_option(src.rs.option.enable_auto_white_balance,
                                       0.0 if q["awb"][0] in ("0","off","false") else 1.0)
                if "wb" in q and getattr(src, "sen", None) is not None:
                    src.sen.set_option(src.rs.option.enable_auto_white_balance, 0.0)
                    src.sen.set_option(src.rs.option.white_balance, float(q["wb"][0]))
                if getattr(src, "sen", None) is not None:
                    msg["wb"] = src.sen.get_option(src.rs.option.white_balance)
                    msg["awb"] = src.sen.get_option(src.rs.option.enable_auto_white_balance)
                if "emitter" in q and hasattr(src, "set_emitter"):
                    msg["emitter"] = src.set_emitter(q["emitter"][0] not in ("0", "off", "false"))
                if "gain" in q and getattr(src, "sen", None) is not None:
                    src.sen.set_option(src.rs.option.gain, float(q["gain"][0]))
                elif "bright" in q:
                    tgt = float(q["bright"][0])
                    for _ in range(8):
                        time.sleep(0.35)
                        with lock:
                            rw = latest.get("raw")
                        cur_b = float(np.mean(rw)) if rw is not None else 0.0
                        if cur_b < 1 or abs(cur_b - tgt) <= tgt * 0.04:
                            break
                        e = src.get_exposure()
                        # ★D400 데이터시트 노출 유효범위 41~10000 (9/2 검색 확인). 41 미만은 규격 밖.
                        src.set_exposure(max(41.0, min(10000.0, e * (tgt / max(cur_b, 1.0)))))
                    msg["iterated"] = True
                time.sleep(0.35)
                with lock:
                    rw = latest.get("raw")
                msg.update({"exposure": src.get_exposure(),
                            "bright": float(np.mean(rw)) if rw is not None else None})
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
            self.wfile.write(_j.dumps(msg).encode()); return
        if self.path.startswith("/aruco"):
            import json as _j
            with lock:
                body = _j.dumps({"boot": BOOT,
                                 "age": round(time.time() - latest.get("aruco_t", 0), 2)
                                        if latest.get("aruco_t") else None,
                                 "markers": latest.get("aruco", [])}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
            self.wfile.write(body); return
        if self.path.startswith("/reset"):
            # 9/1: 낡은 유령 트랙이 MISS_N 동안 버티며 진짜 점을 밀어내는 사고 → 트래커 초기화
            tracker.tracks = []
            self._j(200, b'{"ok":1,"msg":"tracker reset"}'); return
        if self.path.startswith("/dots"):
            # 도트 검출 결과 JSON (캘리브·dot_align 용, 8/25)
            import json as _j
            with lock:
                body = _j.dumps({"boot": BOOT, "source": SRC["name"],
                                 "focus": sorted(focus["kinds"]) if focus["kinds"] else None,
                                 "dots": (latest.get("dots_raw", []) if "raw=1" in self.path
                                          else latest.get("dots", [])),
                                 "stable": (STAB_ON and "raw=1" not in self.path),
                                 "info": latest.get("info", "")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/health"):
            import json as _j
            age = time.time() - SRC["last_ts"] if SRC["last_ts"] else None
            body = _j.dumps({"boot": BOOT, "source": SRC["name"], "frames": SRC["frames"],
                             "topic": SRC.get("topic"), "udp": SRC.get("udp"),
                             "age": round(age, 2) if age is not None else None,
                             "ok": bool(age is not None and age < 3.0)}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with lock:
                        jpg = latest["jpg"]
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.05)   # ~20fps
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("""<title>FR5 손목캠</title>
<body style="margin:0;background:#111;color:#eee;font-family:sans-serif">
<div style="padding:8px 12px">FR5 손목 D435 — <b>점을 클릭하면 그 점만 추적</b>(보라 원 = 추적 범위) · 더블클릭 = 해제</div>
<img id="v" src="/stream" style="width:100%;max-width:__W__px;display:block;cursor:crosshair">
<script>
const v=document.getElementById('v');
v.onclick=e=>{const r=v.getBoundingClientRect();
  const x=(e.clientX-r.left)*__W__/r.width, y=(e.clientY-r.top)*__H__/r.height;
  fetch(`/click?x=${x.toFixed(0)}&y=${y.toFixed(0)}`)};
v.ondblclick=()=>fetch('/click?clear=1');
</script>
</body>""".replace("__W__", str(W)).replace("__H__", str(HGT)).encode())


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)     # 로그 파일로 보낼 때 [ros] 줄이 바로 보이게
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["rs", "ros", "udp", "mjpeg", "v4l2"], default=os.environ.get("CAM_SOURCE", "rs"))
    ap.add_argument("--dev", default=os.environ.get("CAM_DEV", ""), help="v4l2: /dev/videoN (생략하면 시리얼로 자동탐색)")
    ap.add_argument("--serial", default=os.environ.get("CAM_SERIAL", ""), help="v4l2: udev ID_SERIAL_SHORT (재삽입 대비)")
    ap.add_argument("--rot", type=int, default=int(os.environ.get("CAM_ROT", "0")), help="v4l2: 0|90|180|270 (★캘리브 후엔 바꾸지 말 것 — 픽셀 기준이 전부 무효)")
    ap.add_argument("--fps", type=int, default=int(os.environ.get("CAM_FPS", "0")), help="v4l2: 프레임 상한(0=무제한). 15 면 CPU 절반")
    ap.add_argument("--capres", default=os.environ.get("CAM_CAPRES", "1280x720"),
                    help="v4l2: 캡처 해상도. ★이 센서는 4:3 이 네이티브 — 800x600 이 720p 크롭보다 세로 화각 25% 넓다(9/1 실측)")
    ap.add_argument("--udp-port", type=int, default=int(os.environ.get("CAM_UDP_PORT", "5005")),
                    help="udp: cam_udp_send.py 가 보내는 포트")
    ap.add_argument("--color-topic", default=os.environ.get("CAM_COLOR_TOPIC", "auto"),
                    help="ros: 토픽명 또는 auto(토픽 목록에서 color/rgb 이미지 자동 선택, compressed 우선)")
    ap.add_argument("--depth-topic", default=os.environ.get("CAM_DEPTH_TOPIC", "off"),
                    help="ros: 토픽명 | auto(aligned_depth 자동) | off(기본 — WiFi 대역 보호, 캘리브 땐 auto)")
    ap.add_argument("--domain", default=os.environ.get("ROS_DOMAIN_ID"),
                    help="ros: ROS_DOMAIN_ID (비전 PC 와 같아야 함, 계약=73)")
    ap.add_argument("--url", default=os.environ.get("CAM_URL", ""))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CAM_PORT", "8766")))
    a = ap.parse_args()
    SRC["name"] = a.source
    if a.source == "rs":
        src = RsSource()
    elif a.source == "ros":
        if a.domain:
            os.environ["ROS_DOMAIN_ID"] = str(a.domain)
        dt = None if a.depth_topic in ("off", "", "none") else a.depth_topic
        src = RosSource(a.color_topic, dt)
    elif a.source == "udp":
        src = UdpSource(a.udp_port)
    elif a.source == "v4l2":
        capw, caph = (int(v) for v in a.capres.lower().split("x"))
        # ★9/1: 회전 프레임을 원 규격에 억지로 맞추면 비균일 확대(원형 스티커→타원) →
        #   검출기 모양 필터에 전부 걸린다. 이 프로세스의 표준 프레임을 회전 후 크기로 바꾼다.
        W, HGT = (caph, capw) if a.rot in (90, 270) else (capw, caph)
        src = V4l2Source(a.dev, a.serial, a.rot, a.fps, capw, caph)
    else:
        if not a.url:
            ap.error("--source mjpeg 는 --url 필요")
        src = MjpegSource(a.url)
    # ⚠RealSense 파이프는 메인 스레드에서 돌린다 — 서브스레드에서 segfault(8/25 실증)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"cam_server[{a.source}] → http://0.0.0.0:{a.port}  (스트림: /stream, 상태: /health)")
    SRCOBJ["o"] = src
    cam_loop(src)
