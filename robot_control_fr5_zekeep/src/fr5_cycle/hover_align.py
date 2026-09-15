#!/usr/bin/env python3
"""호버 정렬(설계 4단계) — z_seat+85 에서 **든 벽 점 ↔ 베이스 특징(기둥 점·안착된 벽 점)** 을 같은 프레임에서 기준 관계에
맞춘다. 카메라 2대: 손목캠(:8766, 앞끝) + 새카메라(:8768, 뒤끝). 9/5 밤 신설(사용자 설계).

왜: 목표 TCP 계산(z650 관측 1회)은 파지 치우침·베이스 미세 이동을 못 흡수했고 그 빈자리를 사용자 육안 nudge 가 메웠다.
    이 모듈이 그 nudge 를 대신한다. 그리퍼가 벽 어디를 물었든 이 정렬 뒤엔 무관해진다.

원리(카메라별 동일):
  · 기준(ref): 사용자가 "맞다" 한 정렬 상태에서 [베이스 특징 P_ref, 든 벽 점 W_ref(1~2개)] 저장.
  · 지금: P_ref→P_now 2D 유사변환 S(베이스 기준계) → W_exp=S(W_ref) → Δ=W_now−W_exp (px).
  · 로봇 mm 환산은 카메라 종류로 다르다:
      손목캠(moving="base"): 로봇이 δ 움직이면 **베이스 px 가** J·δ 움직이고 벽 px 는 그대로 → δ = +Jinv·Δ.
        매핑 = 관측자세 매핑을 높이(d_h/377)와 rz(R(rz−180)) 로 환산. ★rz 회전 없으면 노랑(rz 90)에서 90° 틀림(오프라인 실증).
      새카메라(moving="wall"): 고정 카메라라 **벽 px 가** J·δ 움직이고 베이스는 그대로 → δ = −Jinv·Δ.
        매핑 = `probe newcam` 으로 z440 에서 ±10mm 조그해 실측(cam2robot_newcam.json).
  · 회전: 로봇 +rz → 화면 각 변화 = ROT_SIGN[src]·(+rz). 손목캠 −1(거울 매핑), 새카메라는 probe 가 재서 저장. 미검증 → 발산 가드.
  · 두 카메라가 다 있으면 XY 는 평균, 서로 COMBINE_TOL 넘게 다르면 정지. rz 는 벽 점 2개인 카메라 우선.
  · 벽 점 1개(노랑·red_s)인 카메라는 위치만, 회전은 −θ(베이스가 돈 만큼) 가정.

게이트(하나라도 걸리면 하강 금지·정지·보고, 자동 재시도 없음):
  특징 매칭 ≥2 · 벽 점 ≥1 · 축척 |s−1|≤3% · rms≤6px · 카메라 간 불일치 ≤1.5mm/0.6° · MAX_ITER 수렴(0.3mm/0.15°) · 발산 즉시 정지

  hover_align.py ref   <색> [--src wrist|newcam] [--wall x,y[,x,y]]   # 지금 프레임을 기준으로(새카메라는 벽 점 좌표 지정)
  hover_align.py check <색>                                           # 모든 가용 카메라 Δ(이동 없음)
  hover_align.py align <색> [--dry]                                   # 보정 루프(1% 속도, ≤3mm/0.5° 스텝)
  hover_align.py probe <newcam|side> <색> --wall x,y[,x,y]           # 고정카메라 매핑(벽 든 채 z440, ±10mm 조그)
  hover_align.py detect <색> [--src side|newcam|wrist]                # 지금 프레임 검출만 출력(기준 없이)
  hover_align.py test  <색> ref.jpg now.jpg [z] [--rz r] [--save out]   # 손목캠 저장 프레임 오프라인 검증
"""
import sys, os, json, math, time
import urllib.request as UR
import numpy as np
import cv2

sys.path.insert(0, "/home/ar/bf2_console/tools")
import pillar_dots as PD
import house_geometry as HG
try:
    import refpts_overlay as _RO      # ★9/12 기준점 오버레이(표시 전용, 없어도 정렬은 돈다)
except Exception:
    _RO = None

BR = "http://127.0.0.1:8765"
MAP = "/home/ar/bf2_console/cam2robot_observe.json"
NEWCAM_MAP = "/home/ar/bf2_console/cam2robot_newcam.json"
REF = "/home/ar/bf2_console/hover_ref_0905.json"
D_OBS, Z_OBS, RZ_MAP = 377.0, 650.0, 180.0
# 카메라 3대. moving: 로봇이 움직일 때 화면에서 움직이는 쪽(손목캠=베이스, 고정캠=든 벽). 매핑 파일은 고정캠은 probe 로 생성.
SRC = {"wrist":  {"url": "http://127.0.0.1:8766/raw", "moving": "base", "map": None},
       "newcam": {"url": "http://127.0.0.1:8768/raw", "moving": "base", "map": "/home/ar/bf2_console/cam2robot_newcam.json"},   # 9/6 13:05 실증: 손목 뎁스캠 180° 반대편에 부착 → 로봇과 함께 움직임(베이스 px 가 이동, 든 벽은 고정). 매핑은 probe_arm
       "side":   {"url": "http://127.0.0.1:8771/raw", "moving": "wall", "map": "/home/ar/bf2_console/cam2robot_side.json"}}
ROT_SIGN = {"wrist": +1.0}                      # 고정캠은 probe 가 rot_sign 을 파일에 저장
# 소스별 검출 파라미터(9/6 새벽 라이브 프레임 실측): 새카메라 파랑 V≈100(손목캠 범위 V140 미달), 측면캠 640×480 점 면적 29~118
# 9/6 19:1x: 빨강 랩어라운드 수정 후 근거리(z440~470)에서 기둥 점이 2300px² 까지 커져 옛 상한 1400 에 걸려 사라짐 → 3200 으로.
#   (newcam 상한은 fixed_cam_seeds 가 '든 벽 점 = 상한 초과' 로 쓰므로 그대로 둔다)
DET = {"wrist":  {"amin_wall": 150, "feat_area": (60, 3200), "near": 80.0, "search": 140.0, "ranges": None},
       "newcam": {"amin_wall": 40,  "feat_area": (20, 1400), "near": 60.0, "search": 120.0,
                  "ranges": {"blue": ((95, 150, 70), (118, 255, 255)), "yellow": ((15, 80, 110), (40, 255, 255)),
                             "red": [((0, 100, 80), (10, 255, 255)), ((160, 100, 80), (180, 255, 255))]},
                  "exclude": [(240, 370, 420, 470), (360, 650, 560, 720)]},   # 카메라에 붙어 같이 움직이는 고정물: 왼쪽 빨간 케이블(13:12 프로브 8px, 14:56 (272,427)), 아래 파란 점(4px)
       # 9/7: 측면캠을 1280×720 으로 올림(같은 거리에서 점 면적 16~31 → 51~115, σ 0.03~0.2px) → 면적·반경 기준 2배로
       "side":   {"amin_wall": 30,  "feat_area": (25, 1000), "near": 60.0, "search": 120.0,
                  # 측면 빨간 기둥점 H 0~5(라이브 실측, 랩어라운드) → 두 구간 합집합
                  "ranges": {"blue": ((95, 120, 80), (125, 255, 255)), "yellow": ((15, 60, 100), (40, 255, 255)),
                             "red": [((0, 60, 60), (12, 255, 255)), ((160, 60, 60), (180, 255, 255))]},
                  "exclude": [(740, 410, 820, 500), (1150, 60, 1280, 320), (820, 560, 980, 720), (250, 495, 405, 600)]}}   # 9/7 1280×720 좌표(옛 640×480 값 2배) + 창 반사·바닥 반사(면적 1600·2192) 제외          # 검은 상자 위 파란 점(고정물, 베이스 아님) 제외
HELD_BOX = (820, 0, 1280, 720)                  # 손목캠에서 든 벽이 보이는 영역(ref 생성 시)
# ★9/10 A타입 내벽(노랑): 든 벽 점이 x≈764 · 면적 223 이라 외벽 기준(x≥820 · 면적 300)에 둘 다 걸린다.
#   외벽 자세에 맞춰 잡힌 창이라 내벽 자세에서는 안 맞는 것 — 게이트 완화가 아니라 **자세별 창 보정**이다.
#   사용자가 사진에 직접 표시한 두 점 (763,458)·(764,521) 을 담도록 색별로 넓힌다.
HELD_BOX_BY_COLOR = {"yellow_in": (700, 0, 1280, 720)}
HELD_AMIN_BY_COLOR = {"yellow_in": 150}
SCALE_TOL, RMS_TOL_PX = 0.03, 6.0
MAX_STEP_MM, MAX_STEP_DEG = 3.0, 0.5
TOL_MM, TOL_DEG = 0.3, 0.15
EXPECT_TCP = None   # ★9/11: 이번 사이클의 계산 목표 [x,y] — 예측 이동 매칭의 기준점(house_cycle 이 정렬 전에 세팅)
LEARN_MOVE = {"blue_in", "yellow_in", "red_in"}   # 내벽 3색 — 자를 믿지 말고 실제 이동으로 배운다
LEARN_STEP_MM = 1.5      # 시험 이동 크기(최소 실행 이동 0.8mm 보다 커야 한다)
LEARN_K_MIN, LEARN_K_MAX = 0.15, 4.0  # 배운 K 의 특이값 허용 범위
LEARN_K_COND = 4.0                    # 두 축 배율이 이 이상 차이나면 측정이 깨진 것으로 보고 거부
SANE_MAX_MM = 25.0   # ★9/11: 한 번의 정렬 계산이 이 크기를 넘으면 오매칭으로 보고 거부(실기 53mm 명령 사고)
COMBINE_TOL_MM, COMBINE_TOL_DEG = 1.5, 0.6
FAR_STEP_MM = 4.0   # ★9/11: 두 캠이 같은 방향으로 이만큼 넘게 멀면 불일치 판정을 보류하고 다가간다
MIN_EXEC_MM = 0.8                               # 로봇 최소 실행 이동량(실측). 이보다 작은 보정은 명령해도 왜곡돼 실행된다
MAX_ITER = 10                                   # 스텝 ≤3mm 라 20mm 급 초기 오차(회전 중심 버그 전) 도 수렴하게
# ★z440 노출: 관측자세(z650)용 417 이면 가까워진 기둥 점이 하얗게 날아간다(9/6 00:5x 라이브: 노랑 S4·V255, 파랑 H90·V251 → 검출 0).
#   이것이 "벽 물고 가까워지면 기둥점·벽점 인식 안 됨"의 원인. 라이브 재현(00:5x, 관측 노출 417): 333/250/167 은 파랑만, 83 에서 파랑+노랑.
#   호버 정렬은 플리커 안전값 사다리(250·167·83)를 전부 시도해 기준 특징이 가장 많이 매칭되는 노출을 고르고, 끝나면 관측 노출로 복원.
# ★9/7 실측(red_s z440): 벽 점이 208 부터 보이기 시작해 500 에서 면적 2040 으로 안정된다(250 은 330~0 으로 경계).
#   사다리가 250·167·83 로 내려가기만 해서 red_s 는 늘 경계 노출에 걸렸고, 노출이 바뀌면 점 중심이 23px(4.2mm) 움직여
#   가짜 정렬 오차가 났다 → 위쪽까지 넓히고, 같은 매칭 수면 벽 점이 큰 노출을 고른다.
HOVER_EXPO_LADDER = (250, 167, 83, 333, 417, 500)
FLICKER_SAFE = (8, 20, 42, 83, 167, 250, 333, 417, 500)   # ★D435 는 이 값만 쓴다(9/1: 아니면 점이 35px 흔들림)
EXPO_SETTLE_S = 1.4   # ★9/10 실측: 노출 변경 후 이만큼 기다려야 프레임이 안정된다(0.6s 면 전환 중 프레임 → 측정 실패/튐)
NEWCAM_URL = "http://127.0.0.1:8768/expo"
NEWCAM_TOL = 0.15          # 새카메라 밝기가 이만큼(±15%) 벗어나야 사다리를 옮긴다


def newcam_bright():
    """방이 얼마나 밝은지의 지표 — 새카메라는 자동노출이라 조명 변화를 그대로 따라간다.
    ★절대 밝기를 손목캠에 맞추면 안 된다(9/10 실측: 새카메라 151 에 맞추면 손목캠 42 로 가는데
      그 칸에서는 든 벽 점이 2개→1개로 줄어 회전을 못 본다). 변화량만 본다."""
    try:
        return float(json.loads(UR.urlopen(NEWCAM_URL, timeout=3).read()).get("bright") or 0) or None
    except Exception:
        return None


def expo_shifted(base_expo, steps):
    """플리커 안전 사다리에서 base_expo 로부터 steps 칸 이동한 값(범위 밖이면 끝 값)."""
    try:
        lad = sorted(FLICKER_SAFE)
        i = min(range(len(lad)), key=lambda k: abs(lad[k] - float(base_expo)))
        return float(lad[max(0, min(len(lad) - 1, i + steps))])
    except Exception:
        return float(base_expo)
NEWCAM_MAP = SRC["newcam"]["map"]
WALL_DOT_HSV = {
    "blue":   ((95, 150, 140), (115, 255, 255)),
    "yellow": ((15, 80, 110), (38, 255, 255)),
    # 9/6 20:2x 실측(빨강 긴 벽 든 손목캠): 빨강 점 H 가 175 를 넘어가 조각남(306+240px) → 상한 179 로 하나(2424px).
    #   0~8 구간까지 더해도 차이 없어 단일 범위 유지(place_calc 6곳이 lo,hi 튜플을 그대로 씀).
    # ★9/7 18:4x: place_calc 만 고치고 여기를 빠뜨려 정렬 쪽은 계속 한쪽 구간만 봤다(같은 표가 두 군데 있는 문제).
    #   빨강 벽 점 실측 색상은 H 3~17 로 0쪽이다. 색상환 양쪽을 다 봐야 한다(노랑 H15~ 와 겹치지 않게 상한 12).
    "red":    [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    "red_s":  [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    "red_in": [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    # ★9/10 A타입 내벽 2장 — 같은 표가 place_calc 에도 있다(9/7 함정: 한쪽만 고치면 절반만 낫는다).
    "blue_in": ((100, 120, 90), (130, 255, 255)),
    # ★9/10 저녁: 든 벽 점 실측 H 21~22 · S 196~210 · V 97~98 → V 하한 120 에만 걸려 사라졌다.
    #   S 가 200 대라 흰 면·반사와 확실히 구분되므로 **V 하한만** 조명에 맞춘다(H·S 는 그대로 = 구분력 유지).
    "yellow_in": ((15, 90, 80), (40, 255, 255)),
}


_FIXED_CAM_COLOR = {"red_s": "red", "red_in": "red", "blue_in": "blue", "yellow_in": "yellow"}   # 고정캠(newcam/side) 색 범위 대체

# ★9/10 사용자: red_s 벽 **옆면**에 노랑 점을 하나 더 붙였다. 손목캠은 위에서 보므로 옆면 점을 원리적으로 못 보고,
#   새카메라는 옆에서 보므로 이 점이 보인다(화면 전체에서 유일한 노랑 = 오검출 여지 0, 재현성 σ 0.04mm).
#   빨강 1점만으로는 **회전을 볼 수 없어** 15:36 막힘이 났다(벽이 죠 안에서 0.41° 돌았는데 1점이라 못 봄).
#   → (색, 카메라)별로 '든 벽 점'의 색을 따로 지정한다. 기둥 특징 쪽은 종전대로 전 색을 본다.
#   ★자리마다 새카메라에 유일한 색이 다르다(새카메라는 팔에 붙어 자리마다 시야가 바뀐다).
#     red_s 자리: red 4개 / yellow 1개  → 벽 점 = 노랑
#     노랑 자리 : red 1개 / yellow 1개  → 벽 점 = 빨강   (사용자가 노랑 벽 옆면에 빨간 점 추가, 9/10)
#   "그 화면에서 유일한 색" 을 고르는 것이 규칙 — 같은 색이 여럿이면 엉뚱한 점을 물 수 있다.
WALL_COLOR_BY_SRC = {("red_s", "newcam"): "yellow",
                     ("yellow", "newcam"): "red"}

# ★9/10 사용자 설계: "바닥의 두 노란점을 기준으로 선을 그어 그 선에 맞춘다".
#   yellow_in 은 벽이 짧아 **든 벽 점이 어느 자세에서도 안 보인다**(실측 확인).
#   → 든 벽을 기둥에 맞추는 대신 **로봇을 밑판에 맞춘다**: 밑판 특징이 기준 사진의 자리로 돌아오도록 로봇을 움직인다.
#   벽은 제대로 물렸다고 믿는다(파지 확인 수단이 없음 — 사용자 지시).
#   이득: 관측자세(z650)에서 잰 베이스(rms 1.2~2.2mm) 대신 **꽂을 자리 바로 위에서 그 자리를 직접** 본다(점 산포 0.02mm).
BASE_ONLY_ALIGN = {"yellow_in", "blue_in"}
#   ★9/11 20:02 실증: blue_in 은 밑판 노란 3점만 볼 때 삽입 성공(242.09,-376.29).
#   든 벽 파란 점(9/10 저장)까지 보면 오늘 파지와 4mm 어긋나 반대로 끌려간다(19:40 오삽입, 19:52 게이트).
#   사용자 9/10 원지시 그대로: "blue_in = 밑판 노랑 3점 / yellow_in = 밑판 노랑 2점".
#   🔴벽 점을 다시 쓰려면 **오늘 성공한 자리에서 기준을 새로 찍은 뒤**에만(낡은 벽 점은 독).
#   (9/10 원지시 "블루_in : 밑판 노랑점 3개 / 옐로_in : 밑판 노랑점 2개").
#   blue_in 은 든 벽 파란 점 2개까지 기준에 들어가 있었는데, 그 중 하나가 안 보이면 회전을 못 봐서
#   4~5mm 를 엉뚱하게 당겼다(9/11 19:33·19:40·19:52 연속 실패). 밑판 기준 모드로 통일.


def wall_color(color, src):
    """든 벽 점을 찾을 때 쓸 색. 지정이 없으면 색 이름 그대로(= 종전 동작)."""
    return WALL_COLOR_BY_SRC.get((color, src), color)

# ------------------------------------------------------------------ 검출
def grab(src="wrist"):
    b = UR.urlopen(SRC[src]["url"], timeout=5).read()
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)


def _blobs(img, color, amin, src="wrist"):
    rng = DET[src]["ranges"]
    # 고정캠 테이블은 기본 3색만 둔다. 짧은 빨강(red_s)·내벽(red_in)은 같은 빨간 점이라 red 범위를 쓴다.
    r = (rng[_FIXED_CAM_COLOR.get(color, color)] if rng else WALL_DOT_HSV[color])
    ranges = r if isinstance(r, list) else [r]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = None
    for lo, hi in ranges:
        mm = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        m = mm if m is None else cv2.bitwise_or(m, mm)
    if src == "wrist":
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    out = [(float(cen[i][0]), float(cen[i][1]), int(st[i, 4])) for i in range(1, n) if st[i, 4] >= amin]
    ex = DET[src].get("exclude") or []
    return [q for q in out if not any(x0 <= q[0] <= x1 and y0 <= q[1] <= y1 for x0, y0, x1, y1 in ex)]


def _merge_fragments(pts, r):
    """★9/6 라이브: 프레임 아래쪽 벽 점이 그늘로 94/61/61px 조각 3개로 갈라져 검출 실패 → r px 안 조각을 하나로 합친다(면적 가중 중심)."""
    out = []
    for x, y, a in sorted(pts, key=lambda q: -q[2]):
        for m in out:
            if math.hypot(m[0] - x, m[1] - y) <= r:
                A = m[2] + a; m[0] = (m[0] * m[2] + x * a) / A; m[1] = (m[1] * m[2] + y * a) / A; m[2] = A; break
        else:
            out.append([x, y, a])
    return [(m[0], m[1], int(m[2])) for m in out]


def wall_dots(img, color, ref=None, seeds=None, src="wrist"):
    """든 벽 점(1~2). ref 있으면 기준 자리 ±near 의 같은 색 점(물린 벽은 화면에서 거의 안 움직임).
    seeds(ref 생성용 좌표) 있으면 그 근처. 둘 다 없으면(손목캠) HELD_BOX 안 큰 점."""
    wcol = wall_color(color, src)
    pts = _merge_fragments(_blobs(img, wcol, 30 if src == "wrist" else 8, src), 30.0 if src == "wrist" else 10.0)
    pts = [q for q in pts if q[2] >= DET[src]["amin_wall"]]
    near = DET[src]["near"]
    anchors = [(p[0], p[1]) for p in ref["wall"]] if ref else (seeds or None)
    if anchors:
        out = []
        for rx, ry in anchors:
            c = [q for q in pts if math.hypot(q[0] - rx, q[1] - ry) <= near]
            if c:
                out.append(min(c, key=lambda q: math.hypot(q[0] - rx, q[1] - ry)))
        return out
    if src != "wrist":
        return []
    x0, y0, x1, y1 = HELD_BOX_BY_COLOR.get(color, HELD_BOX)
    _amin = HELD_AMIN_BY_COLOR.get(color, 300)
    pts = [q for q in pts if x0 <= q[0] <= x1 and y0 <= q[1] <= y1 and q[2] >= _amin]
    pts.sort(key=lambda q: -q[2])
    return pts[:2]


# ★9/9 사용자 지적: 이 함수가 모으는 "베이스 고정 특징" 에는 **이미 꽂힌 다른 벽들의 점**이 섞인다.
#   한 사이클 안에서는 고정이지만, 벽이 한 장 더 꽂히거나 빠지거나 조금 비뚤어지면 기준이 통째로 흔들리고,
#   조명이 바뀌면 어떤 벽 점은 잡히고 어떤 건 안 잡혀 **엉뚱한 점끼리 짝지어진다**(9/9 16:56 축척 1.094 거부).
#   기둥 점과 안착 벽 점은 둘 다 같은 색점이라 검출기로는 구분할 수 없다.
#   → **ArUco 마커를 기준점으로 추가**한다. 마커는 벽이 바뀌어도 안 움직이고 노출에도 거의 안 흔들려,
#     짝짓기의 닻 역할을 한다. 색점만으로 맞추던 것보다 축척이 튀기 어렵다.
#   ⚠ 색별로 켠다 — 잘 되는 색의 정렬 경로는 건드리지 않는다.
# ★9/10 yellow 추가: A타입 옐로 자리(= B red_s 자리)에서는 손목캠에 기둥이 1개만 들어온다.
#   어제 기준(특징 4개)은 파랑·레드가 이미 꽂힌 상태에서 그 벽 점들을 특징으로 쓴 것이라,
#   벽이 한 장뿐인 지금은 참조점이 사라져 정렬이 아예 못 돈다(9/7 "다른 벽 점에 기대던 기준" 과 같은 함정).
#   → red_s 와 같은 처방: 책상 고정 ArUco 중심을 특징으로 함께 쓴다(기둥 1 + 마커 2 = 3개).
# ★9/10 사용자 설계: "기둥 1개 + 든 벽 점 1개"만으로 정렬한다.
#   왜: 지금까지 부족한 특징을 **꽂힌 벽의 점**으로 메워 왔는데(파랑 기준의 노랑 4개가 전부 옐로 벽 위였다),
#       벽 배치가 바뀌는 사이클에서는 그 기준이 통째로 흔들린다. A 파랑은 **첫 벽**이라 베이스가 비어 있어 특히 치명적.
#   근거(9/10 실측): 기둥 점 검출 산포 σ0.05px(=0.01mm) — 4점 닮음변환의 rms 1.0~1.7px(0.18~0.31mm)보다 오히려 정확하다.
#       4점이 좋아 보였던 건 평균화가 아니라 **서로 다른 물체(벽·기둥)에 붙은 점**이라 편향이 섞여 있었기 때문.
#   위험은 하나 — 바로 옆 벽 점 오인(실측 34.9px = 6.4mm 떨어져 색·면적까지 비슷). 그래서 아래 두 게이트를 건다.
PILLAR_PICK_F = "/home/ar/bf2_console/state/house/pillar_pick.json"  # 색/캠별 '진짜 기둥' 위치(사용자 지정)
SINGLE_SEARCH_PX = 15.0     # 기준 특징 1개일 때 매칭 반경 — 이웃 벽 점(34.9px)을 확실히 배제
# ★9/13 실측(B 파랑 손목, 15:30 대낮): 기준은 9/10 밤 21:10 에 찍혔고 노랑 점은 밝을수록 HSV 범위를 벗어난다
#   → 같은 점의 면적이 694 → 최대 378(54%). 노출 42~167 여섯 칸, 게인 64~192 다섯 칸, **열 조합 전부**
#     중심이 (827,262~263) 로 1px 안에서 일치 = 같은 점임이 증명됨. 그런데 면적 하한 60% 라 정지했다.
#   게이트를 숫자로 푸는 건 9/7 에 실패한 길이다(3/5 로 계산해 10mm 엉뚱한 보정). 대신 **증거를 더 요구한다**:
#   여러 노출 칸에서 중심이 같은 자리로 모이면 그건 같은 점이므로 면적만 작아도 통과, 하나라도 흔들리면 정지.
# ★9/13 실측(B 파랑 새카메라): 지정 기둥 red 가 예측 자리에서 18.4px 떨어져 SINGLE_SEARCH_PX(15px≈3.7mm)를
#   넘겨 매칭 0개 → 정지. 반경만 넓히면 옆 벽 점을 물 위험이 생기므로 **넓힌 반경에서는 증거를 더 요구한다**:
#   ① 그 색 후보가 그 반경 안에 딱 하나 ② 면적이 기준의 60~160%. 둘 중 하나라도 어긋나면 정지(종전과 동일).
SINGLE_SEARCH_WIDE_PX = 70.0
WIDE_AREA_LO, WIDE_AREA_HI = 0.60, 1.60
AREA_ID_SPREAD_PX = 3.0     # 여러 노출에서 중심이 이만큼 안에 모여야 '같은 점'
AREA_ID_MIN_N     = 3       # 최소 이만큼의 노출 칸에서 잡혀야 함
AREA_ID_FLOOR     = 0.30    # 그래도 이 아래로 작아지면 조각이므로 정지
AREA_ID_CEIL      = 3.00
AREA_OK = {}                # (src, 지정기둥x, 지정기둥y) → {"ratio","spread","n"} — align() 마다 비운다
AREA_LADDER_LOW = (63.0, 55.0, 42.0)   # 낮에는 사다리 최저 83 으로도 부족(실측 최적 63)

SINGLE_AREA_BY_SRC = {"wrist": (0.6, 1.6), "newcam": (0.4, 2.5)}   # 기준 면적 대비 허용(다른 점 오인 방어)
#   ★9/11 사용자 설계 원칙: "기준 점이 그 자리에, 같은 거리·yaw" — 신원은 예측 위치 SINGLE_SEARCH_PX 안 + 면적으로 본다.
#   손목캠(수직)은 면적이 안정 → 0.6~1.6 엄격 유지(red_s·red_in 은 손목캠 단독이라 교차검증이 없어 여기서 막아야 한다).
#   새카메라(팔에 비스듬)는 6mm 이동에 면적 58%(340/584) 로 변함(실측, 위치는 예측 8px 안) → 0.4~2.5. 항상 손목캠과 짝이라 1.5mm 불일치 게이트가 받친다.
SINGLE_AREA_LO, SINGLE_AREA_HI = 0.6, 1.6   # 기본(등록 안 된 소스)
PICK_TOL_PX = 45.0          # 저장 시 화이트리스트 좌표에서 이만큼 안의 점만 기둥으로 인정


def pillar_pick(color, src):
    """사용자가 지정한 '진짜 기둥' 화면 위치들. state/house 아래라 집 타입별로 갈린다(B 에는 파일이 없어 종전 동작).

    ★9/10 사용자: "동그라미 친 것만 신경써줘, 그거 외에는 다 끼워진 벽이니까"
      → 한 점([x, y])뿐 아니라 **여러 점**([[x1,y1],[x2,y2], ...])도 받는다. 반환은 항상 [(x,y), ...] 또는 None.
      새카메라의 red_s 처럼 기둥이 두 개 보이는 자리에서 나머지(끼워진 벽 점)를 전부 배제하기 위한 것."""
    try:
        v = (json.load(open(PILLAR_PICK_F)).get(color) or {}).get(src)
    except Exception:
        return None
    if not v:
        return None
    if isinstance(v[0], (int, float)):          # [x, y] — 한 점(종전 형식)
        return [(float(v[0]), float(v[1]))]
    return [(float(q[0]), float(q[1])) for q in v]


ALIGN_ARUCO = set()                        # ★9/10 네 색 모두 기둥1(또는 2)+벽점 방식으로 전환 — 마커는 책상 고정이라 밑판 이동을 못 따라감                    # ★9/10 yellow 도 제외: 기둥1+벽점1 방식으로 전환          # ★9/10 red 제외: 기둥1+벽점1 방식으로 전환(마커는 책상 고정이라 밑판 이동을 못 따라감)   # 정렬 특징에 ArUco 마커를 함께 쓰는 색(9/10 red 추가: A 에선 손목캠 기둥 2개뿐)
ALIGN_ARUCO_AREA = 400          # 마커를 특징 목록에 넣을 때 쓰는 가짜 면적(면적 게이트 통과용)


def aruco_feats():
    """ArUco 마커 중심을 정렬 특징으로. [( 'ar<id>', x, y, area )] — 없으면 빈 목록."""
    try:
        import base_twist as BT
        m, why = BT.markers()
        if not m:
            return []
        out = []
        for mid, corners in sorted(m.items()):
            xs = [c[0] for c in corners]; ys = [c[1] for c in corners]
            out.append((f"ar{mid}", sum(xs) / 4.0, sum(ys) / 4.0, ALIGN_ARUCO_AREA))
        return out
    except Exception as e:
        print(f"  (ArUco 특징 실패: {e})", flush=True)
        return []


def base_feats(img, exclude, src="wrist", color=None):
    """베이스 고정 특징 = 색점 중 든 벽 점이 아닌 것 전부(기둥 꼭대기 + 안착 벽 점). [(color,x,y,area)]
    color 가 ALIGN_ARUCO 에 있으면 ArUco 마커 중심을 함께 넣는다(9/9)."""
    lo_a, hi_a = DET[src]["feat_area"]
    if src == "wrist":
        d = PD.detect(img, None)
    else:
        d = {c: _blobs(img, c, lo_a, src) for c in ("blue", "yellow", "red")}
    out = []
    for c, lst in d.items():
        for p in lst:
            if not (lo_a <= p[2] <= hi_a):
                continue
            if any(math.dist(p[:2], e[:2]) < (25 if src == "wrist" else 12) for e in exclude):
                continue
            out.append((c, p[0], p[1], p[2]))
    if src == "wrist" and color in ALIGN_ARUCO:
        af = aruco_feats()
        if af:
            out.extend(af)
    return out


def measure(img, color, ref=None, seeds=None, src="wrist"):
    w = wall_dots(img, color, ref, seeds, src)
    if color in BASE_ONLY_ALIGN:
        # ★밑판 기준 모드: 든 벽 점을 아예 쓰지 않는다(있어도 무시). 밑판 특징만 본다.
        return {"wall": [], "pillars": base_feats(img, [], src, color)}, None
    if len(w) < 1:
        return None, f"[{src}] 든 벽 점 0개(벽을 안 들었거나 기준 자리에 없음)"
    w = sorted(w, key=lambda q: (q[1], q[0]))
    # ★9/10: 지정 기둥이 '든 벽 점' 창(x≥820)에 들어오는 자리가 있다(red_s 실측: 기둥 (834,480) 이 벽으로 잡힘).
    #   기둥을 벽으로 세면 정렬이 통째로 틀어지므로, 지정 좌표 근처의 점은 벽 목록에서 뺀다.
    pk = pillar_pick(color, src)
    if pk:
        w2 = [q for q in w if all(math.hypot(q[0] - px, q[1] - py) > PICK_TOL_PX for px, py in pk)]
        if len(w2) != len(w):
            print(f"  (벽 목록에서 지정 기둥 근처 {len(w) - len(w2)}개 제외)", flush=True)
        if not w2:
            return None, f"[{src}] 든 벽 점 0개(지정 기둥 제외 후) — 벽을 안 들었거나 기준 자리에 없음"
        w = w2
    return {"wall": [(q[0], q[1], q[2]) for q in w], "pillars": base_feats(img, w, src, color)}, None


MULTI_N = 5             # 정렬 측정에 쓸 프레임 수
MULTI_MERGE_PX = 15.0   # 이 안이면 같은 특징으로 본다
MULTI_MIN_SEEN = 2      # 이 횟수 이상 보인 특징만 채택(유령 배제)


def measure_multi(color, ref=None, seeds=None, src="wrist", n=MULTI_N):
    """★9/7 사용자 지시("게이트는 엄격하게, 검출을 보강하라"):
    호버 정렬은 지금까지 **한 장**만 보고 판단해 점 하나가 깜빡이면 바로 실패했다.
    랙(rack_ends)·베이스(pillars_px)는 이미 여러 프레임을 모은다 → 정렬도 같게 한다.
    n 장을 찍어 같은 색·MULTI_MERGE_PX 안의 검출을 하나로 묶고, MULTI_MIN_SEEN 회 이상 보인 것만
    중앙값 좌표로 채택한다. 게이트(기준 개수 전부·축척·rms)는 그대로 둔다."""
    import statistics as _s
    accP, accW, ok = [], [], 0
    for _ in range(n):
        m, why = measure(grab(src), color, ref, seeds, src)
        if not m:
            time.sleep(0.08); continue
        ok += 1
        accP.append(m["pillars"]); accW.append(m["wall"])
        time.sleep(0.08)
    if not ok:
        return None, f"[{src}] {n}프레임 모두 측정 실패"

    def merge(lists, colored):
        groups = []
        for fi, lst in enumerate(lists):
            for q in lst:
                col = q[0] if colored else None
                x, y = (q[1], q[2]) if colored else (q[0], q[1])
                a = q[3] if colored else q[2]
                for g in groups:
                    if g["col"] == col and math.hypot(x - g["x"][-1], y - g["y"][-1]) <= MULTI_MERGE_PX:
                        g["x"].append(x); g["y"].append(y); g["a"].append(a); g["f"].add(fi); break
                else:
                    groups.append({"col": col, "x": [x], "y": [y], "a": [a], "f": {fi}})
        out = []
        for g in groups:
            if len(g["f"]) < min(MULTI_MIN_SEEN, ok):
                continue
            mx, my, ma = _s.median(g["x"]), _s.median(g["y"]), int(_s.median(g["a"]))
            out.append((g["col"], mx, my, ma) if colored else (mx, my, ma))
        return out

    P, W = merge(accP, True), merge(accW, False)
    W.sort(key=lambda q: (q[1], q[0]))
    if not W and color not in BASE_ONLY_ALIGN:
        return None, f"[{src}] 든 벽 점 0개({ok}/{n}프레임)"
    n_min = min(len(p) for p in accP) if accP else 0
    if len(P) > n_min:
        print(f"  ({src} 다중프레임: 기둥 특징 {n_min}→{len(P)}개로 보강, {ok}/{n}프레임)", flush=True)
    return {"wall": W, "pillars": P}, None


def current_expo():
    """지금 손목캠 노출값(없으면 None)."""
    try:
        import color_lock as CL
        return (CL.current_settings() or {}).get("exposure")
    except Exception:
        return None


def set_expo(val):
    """손목캠 노출(플리커 안전값만). color_lock 저장은 건드리지 않는다(관측자세 값은 그대로, 정렬 뒤 restore)."""
    try:
        import color_lock as CL
        CL.expo(set=int(val)); time.sleep(0.9); return True
    except Exception as e:
        print("  ⚠ 노출 설정 실패:", e); return False


def expo_for_area(color, target_area, src="wrist", tries=6, tol=0.30, lo=42.0, hi=2000.0):
    """★9/7 사용자 지적("노출을 숫자로 고정하지 말고 밝기에 맞춰 자동으로"):
    노출 숫자를 고정하면 조명이 바뀔 때마다 어긋난다(오늘 red_s 기준 250 ↔ 실제 필요 500).
    카메라 자동노출은 9/2 에 실패한 길이다(AWB/AE 를 켜면 색조가 흘러 파랑 검출 2~4개 왕복, 면적 편차 319).
    → 대신 **점이 기준과 같은 크기로 보일 때까지** 노출을 자동으로 맞춘다. 기준마다 면적이 이미 저장돼 있어
      다시 등록할 필요가 없고, 같은 크기로 보이면 중심도 같은 자리를 가리킨다(면적 617→2038 일 때 중심 23px 이동).
    반환 (맞춘 노출, 그때 면적) / 실패 (None, None)."""
    if not target_area or target_area <= 0:
        return None, None
    e = float(current_expo() or 250.0)
    best = None
    for _ in range(tries):
        e = max(lo, min(hi, e))
        set_expo(e)
        time.sleep(EXPO_SETTLE_S)   # ★9/10: 노출 바꾼 직후 프레임은 전환 중이라 못 쓴다
        q = wall_dots(grab(src), color, None, None, src)
        a = max([p[2] for p in q], default=0)
        if a > 0:
            r = a / float(target_area)
            if best is None or abs(math.log(max(r, 1e-6))) < abs(math.log(max(best[2] / float(target_area), 1e-6))):
                best = (e, a, a)
            if abs(r - 1.0) <= tol:
                print(f"  (노출 자동: 점 면적 {a} ≈ 기준 {int(target_area)} → 노출 {e:.0f} 채택)", flush=True)
                return e, a
            e = e / (r ** 0.7)          # 면적은 노출에 대략 비례 — 0.7 지수로 부드럽게 수렴
        else:
            e = e * 1.6                  # 점이 안 보이면 밝게
    if best:
        set_expo(best[0])
        print(f"  (노출 자동: 최선 면적 {best[1]} vs 기준 {int(target_area)} → 노출 {best[0]:.0f})", flush=True)
        return best[0], best[1]
    return None, None


def restore_expo():
    try:
        import color_lock as CL
        st_ = json.load(open(CL.STORE)) if os.path.exists(CL.STORE) else {}
        ap = st_.get("apply") or {}
        if ap.get("set"): CL.expo(set=int(ap["set"]), **({"gain": int(ap["gain"])} if ap.get("gain") else {})); time.sleep(0.6)
    except Exception as e:
        print("  ⚠ 노출 복원 실패:", e)


def pick_hover_expo(color, ref):
    """★z440 노출 자동 선택(9/6 00:5x 라이브 실측: 417→파랑·노랑 둘 다 날아감 / 333·250·167→파랑만 / **83→파랑+노랑 둘 다**).
    사다리 전부 시도해 **기준 특징과 매칭되는 수가 최대**인 노출을 고른다(첫 성공에서 멈추면 333 에서 파랑만 잡고 끝남)."""
    best = None
    for e in HOVER_EXPO_LADDER:
        set_expo(e)
        time.sleep(EXPO_SETTLE_S)   # ★9/10: 노출 바꾼 직후 프레임은 전환 중이라 못 쓴다
        meas, why = measure(grab("wrist"), color, ref, None, "wrist")
        nw = len(meas["wall"]) if meas else 0
        nm = len(match_feats(ref["pillars"], meas["pillars"], DET["wrist"]["search"])[0]) if meas else 0
        wa = int(max([q[2] for q in meas["wall"]], default=0)) if meas else 0
        print(f"  호버 노출 {e}: 벽 점 {nw}(면적 {wa}) 매칭 특징 {nm}")
        key = (nm, nw, wa)          # 매칭 수 → 벽 점 수 → 벽 점 면적(경계 노출 회피)
        # ★9/10: 요구를 2 로 못박아 둬서 **기둥 1개짜리 지정 기준에서는 사다리가 절대 성공하지 못했다**
        #   (오늘 밤 노랑이 어두워져 검출 0 이 됐는데 사다리를 다 돌고도 포기한 원인).
        #   기준이 요구하는 개수를 그대로 쓴다 — 완화가 아니라 기준에 맞추는 것.
        need = max(1, len(ref["pillars"]))
        if nw >= 1 and nm >= need and (best is None or key > best[0]):
            best = (key, e)
    if best is None:
        return None
    set_expo(best[1]); print(f"  → 호버 노출 {best[1]} 채택(매칭 {best[0][0]})"); return best[1]


def pick_expo_by_area(color, ref, src="wrist"):
    """★9/11 17:00 B 파랑: 지정 기둥이 예측 자리에서 잡히는데 면적 53%(392/694) — 방이 어젯밤보다 밝아 기준 노출 333 에선
    노란 점이 날아간다(실측 250 에서 86%). 사용자 지시 "못 잡으면 카메라를 조정해 특정지어 잡아라":
    사다리를 돌며 **지정 기둥 면적이 기준에 가장 가까운** 노출을 고른다(벽 점 ≥1 이어야 채택). 게이트는 그대로."""
    rp = ref["pillars"][0] if ref.get("pillars") else None
    if rp is None:
        return None
    best = None
    cents = []
    # ★9/13: 낮에는 최저 83 으로도 노랑이 계속 작다(실측 최적 63) → 아래 칸을 더 둔다.
    ladder = ([float(ref["expo"])] if ref.get("expo") else []) + [e for e in HOVER_EXPO_LADDER] + list(AREA_LADDER_LOW)
    for e in ladder:
        set_expo(e); time.sleep(EXPO_SETTLE_S)
        meas, why = measure(grab(src), color, ref, None, src)
        if not meas or not meas["wall"]:
            print(f"  면적 노출 {e:.0f}: 벽 점 없음", flush=True); continue
        s_, d_, _ = match_feats(ref["pillars"], meas["pillars"], DET[src]["search"])
        if not s_:
            print(f"  면적 노출 {e:.0f}: 기둥 매칭 없음", flush=True); continue
        got = min(meas["pillars"], key=lambda q: math.hypot(q[1] - d_[0][0], q[2] - d_[0][1]))
        ratio = got[3] / rp[3] if rp[3] else 1.0
        print(f"  면적 노출 {e:.0f}: 기둥 면적 {int(got[3])}/{int(rp[3])} ({ratio:.0%})", flush=True)
        cents.append((e, float(got[1]), float(got[2]), float(got[3])))
        key = abs(math.log(max(ratio, 1e-3)))
        if best is None or key < best[0]:
            best = (key, e, ratio)
    # ★같은 점임을 '여러 노출에서 중심이 한 자리로 모이는가'로 증명한다(면적 완화가 아니라 증거 추가).
    key_id = (src, round(rp[1]), round(rp[2]))
    AREA_OK.pop(key_id, None)
    if len(cents) >= AREA_ID_MIN_N:
        mx = sorted(c[1] for c in cents)[len(cents) // 2]
        my = sorted(c[2] for c in cents)[len(cents) // 2]
        spread = max(math.hypot(c[1] - mx, c[2] - my) for c in cents)
        if spread <= AREA_ID_SPREAD_PX:
            AREA_OK[key_id] = {"ratio": best[2] if best else 0.0, "spread": spread, "n": len(cents)}
            print(f"  (같은 점 확인: 노출 {len(cents)}칸에서 중심 ({mx:.0f},{my:.0f}) 산포 {spread:.1f}px "
                  f"≤ {AREA_ID_SPREAD_PX:.0f}px → 면적이 작아도 같은 점으로 인정)", flush=True)
        else:
            print(f"  (같은 점 아님: 노출 {len(cents)}칸 중심 산포 {spread:.1f}px > {AREA_ID_SPREAD_PX:.0f}px "
                  f"— 노출마다 다른 점을 물고 있다)", flush=True)
    if best is None:
        return None
    set_expo(best[1]); time.sleep(EXPO_SETTLE_S)
    print(f"  → 면적 기준 노출 {best[1]:.0f} 채택 ({best[2]:.0%})", flush=True)
    return best[1]


# ------------------------------------------------------------------ 기하
def _rot(th):
    c, s = math.cos(math.radians(th)), math.sin(math.radians(th))
    return np.array([[c, -s], [s, c]])


def similarity(src, dst):
    A = np.array(src, float); B = np.array(dst, float)
    ca, cb = A.mean(0), B.mean(0); A0, B0 = A - ca, B - cb
    za = A0[:, 0] + 1j * A0[:, 1]; zb = B0[:, 0] + 1j * B0[:, 1]
    den = float((np.abs(za) ** 2).sum())
    if den < 1e-9 or len(src) < 2:
        s, th = 1.0, 0.0
    else:
        z = complex((np.conj(za) * zb).sum() / den); s, th = abs(z), math.degrees(math.atan2(z.imag, z.real))
    t = cb - s * (_rot(th) @ ca)
    fit = (s * (_rot(th) @ A.T)).T + t
    return s, th, float(t[0]), float(t[1]), float(np.sqrt(((fit - B) ** 2).sum(1).mean()))


def apply_sim(sim, pts):
    s, th, tx, ty, _ = sim
    P = np.array([p[:2] for p in pts], float)
    return (s * (_rot(th) @ P.T)).T + np.array([tx, ty])


def wall_mid_ang(w):
    (x1, y1), (x2, y2) = w[0][:2], w[1][:2]
    return ((x1 + x2) / 2, (y1 + y2) / 2), math.degrees(math.atan2(y2 - y1, x2 - x1))


def match_feats(ref_p, now_p, search=None):
    search = search or DET["wrist"]["search"]
    src, dst, lab, used = [], [], [], set()
    for c, x, y, *_ in ref_p:
        best = None
        for j, (c2, x2, y2, a2) in enumerate(now_p):
            if c2 != c or j in used:
                continue
            d = math.hypot(x2 - x, y2 - y)
            if d <= search and (best is None or d < best[0]):
                best = (d, j)
        if best is not None:
            used.add(best[1]); j = best[1]
            src.append((x, y)); dst.append((now_p[j][1], now_p[j][2]))
            lab.append(f"{c}({x:.0f},{y:.0f})→({now_p[j][1]:.0f},{now_p[j][2]:.0f}) {best[0]:.0f}px")
    return src, dst, lab


def plane_scale(ref):
    """★9/11: 기준 특징이 놓인 평면의 자 보정 배율. 기둥 '윗면'에서 실측한 Jinv 를 그 평면에 맞게 늘린다.
    내벽(blue_in·yellow_in)의 기준점은 밑판 '바닥' 이라 카메라에서 더 멀고, 같은 mm 이동에 픽셀이 덜 움직인다.
    실측(9/11 z440, 빈손): 로봇 ±4mm 프로브 → 밑판 노란 점 14.5px/4mm = 3.66px/mm (0.2730 mm/px).
      교차검증: 사용자가 밑판에 붙인 빨강·파랑 스티커(점 사이 30mm) = 112.0px → 0.2679 mm/px (1.9% 차).
      그때 쓰던 자 0.1832 mm/px → **1.49배 작았다**(정렬이 필요량의 2/3만 움직여 수렴이 질질 끌림).
    ref["plane_scale"] 에 저장한다(색·카메라별). 없으면 1.0 = 종전 그대로(외벽 8색·B red_in 영향 0)."""
    try:
        v = float(ref.get("plane_scale", 1.0))
    except Exception:
        return 1.0
    return v if 0.2 <= v <= 5.0 else 1.0


def jinv_for(src, z_tcp, rz_tcp):
    """카메라별 (Jinv[mm/px, 로봇 프레임], rot_sign). 손목캠: 높이·rz 환산. 고정캠(newcam/side): probe 파일 그대로."""
    if src == "wrist":
        m = json.load(open(MAP)); d_h = D_OBS - (Z_OBS - z_tcp)
        if d_h < 60:
            raise RuntimeError(f"z{z_tcp:.0f}: 기둥 뎁스 {d_h:.0f}mm — 매핑 환산 불가")
        return _rot(HG.wrap_deg(rz_tcp - RZ_MAP)) @ (np.array(m["Jinv_mm_per_px"], float) * (d_h / D_OBS)), ROT_SIGN["wrist"]
    f = SRC[src]["map"]
    if not os.path.exists(f):
        raise RuntimeError(f"{src} 매핑 없음 — `hover_align.py probe {src} <색> --wall x,y`")
    m = json.load(open(f))
    if abs(z_tcp - m["z"]) > 5:
        raise RuntimeError(f"{src} 매핑은 z{m['z']:.0f} 용, 지금 z{z_tcp:.0f}")
    J = np.array(m["Jinv_mm_per_px"], float)
    if SRC[src]["moving"] == "base":
        # ★손목에 붙은 카메라(새카메라): rz 가 돌면 화면도 같이 돈다 → 매핑 촬영 시 rz 대비 차이만큼 회전.
        #   9/6 19:0x: 매핑은 rz −180 에서 측정. 노랑 슬롯은 rz 90 → 이 보정 없으면 90° 틀린 방향으로 감(9/2 '축매핑 발산 3회' 와 같은 함정).
        rz_map = float((m.get("tcp") or [0, 0, 0, 0, 0, RZ_MAP])[5])
        J = _rot(HG.wrap_deg(rz_tcp - rz_map)) @ J
    return J, m.get("rot_sign", -1.0)


WALL_COLUMN_PX = 80.0      # 든 벽 점과 같은 x 열(±이 값) 의 같은 색 특징은 벽의 것 → 기준에서 뺀다
SAME_COLOR_NEAR_PX = 60.0  # 든 벽 점 이 거리 안의 같은 색 '기둥 특징'은 반사/벽 자신으로 보고 기준에서 뺀다
COLLINEAR_PERP_PX = 25.0   # 매칭 특징의 직선 대비 수직 퍼짐이 이보다 좁으면 회전을 풀지 않는다
WALL_PAIR_MAX_PX = 90.0    # ★9/9: 기준 벽 점 ↔ 측정 벽 점 1:1 짝 허용 거리. red_s 벽 점 간격 192px 의 절반(96px)보다
#   작아야 엉뚱한 점끼리 짝지어지지 않는다. 이보다 멀면 짝짓지 말고 거부(완화가 아니라 오매칭 차단)


def _pred_shift_px(ref, z_tcp, rz_tcp, src, tcp_now):
    """★9/10: 기준을 찍은 자리와 지금 자리가 다르면 **기둥은 화면에서 그만큼 밀려 보인다**(기둥은 세상에 고정,
    카메라가 로봇과 함께 움직이므로). 그런데 매칭은 기준 픽셀 주변만 뒤져서, 3mm 만 떨어져도
    16px 밀린 기둥을 15px 반경 밖이라고 놓쳤다(16:14·16:18 red_s 두 번 연속 정지).

    반경을 늘리는 건 감지기를 무디게 하는 것이라 하지 않는다. 대신 **어디로 밀렸을지 예측해서 거기를 뒤진다.**
      예측 = J · Δ로봇 (J = Jinv 의 역, mm→px)
    실측 검증(16:18 자리): Δ로봇 (+2.70,−2.04)mm → 예측 (+11.2,−14.6)px, 실측 (+7,−14)px → 잔차 4.2px.
    벽 점은 로봇과 함께 움직이므로 **밀지 않는다** — 기둥(베이스 특징)에만 적용."""
    # ★9/11 16:16 A 파랑 정지: 베이스가 (−2.4,+0.8) 움직여 로봇이 따라간 자리에선 기둥이 **기준 픽셀 그대로** 보이는데
    #   (기준 자리 대비 Δ로봇)로 예측하면 13px 엉뚱한 곳을 뒤져 4px 옆의 진짜 기둥을 놓친다.
    #   예측의 기준점 = "이번 사이클의 계산 목표"(EXPECT_TCP, house_cycle 이 넘김). 없으면 기준 자리.
    rt = EXPECT_TCP if EXPECT_TCP else ref.get("tcp")
    if not rt or not tcp_now:
        return (0.0, 0.0)
    d_mm = np.array([tcp_now[0] - rt[0], tcp_now[1] - rt[1]], float)
    if float(np.hypot(*d_mm)) < 0.3:                       # 같은 자리 — 굳이 밀지 않는다
        return (0.0, 0.0)
    try:
        J = np.linalg.inv(jinv_ref(ref, src, z_tcp, rz_tcp)[0])
    except Exception:
        return (0.0, 0.0)
    v = J @ d_mm
    return (float(v[0]), float(v[1]))


def jinv_ref(ref, src, z_tcp, rz_tcp):
    """★9/11: 기준에 실측 매핑(Jinv_override)이 있으면 그것을 쓴다. 없으면 종전 jinv_for × plane_scale.
    red_s 새카메라 실측(z440, rz+91.2, ±2mm 양방향): J = [[-0.083,4.842],[4.889,0.070]] px/mm 로 거의 등방인데,
    저장된 매핑은 [[0,4.954],[5.641,0.057]] 로 한 축이 14% 커서 방향에 따라 손목캠과 1.8mm 어긋났다.
    ⚠rz 가 다르면 손목캠 매핑은 회전하므로 override 는 **그 색이 늘 쓰는 rz** 에서 잰 것만 저장한다."""
    ovr = ref.get("Jinv_override")
    if ovr:
        try:
            return np.array(ovr, float), ROT_SIGN.get(src, -1.0)
        except Exception:
            pass
    J, rs = jinv_for(src, z_tcp, rz_tcp)
    return J * plane_scale(ref), rs


def color_base_only(ref):
    """기준에 든 벽 점이 없으면 밑판 기준 모드(BASE_ONLY_ALIGN 색에서만 그렇게 저장된다)."""
    return not ref.get("wall")


def delta(ref, meas, z_tcp, rz_tcp, src="wrist", tcp_now=None):
    single = len(ref["pillars"]) == 1
    sx, sy = _pred_shift_px(ref, z_tcp, rz_tcp, src, tcp_now)
    ref_p = ref["pillars"]
    if sx or sy:
        ref_p = [(c, x + sx, y + sy, a) for c, x, y, a in ref["pillars"]]
        print(f"  (기준 자리와 {math.hypot(tcp_now[0]-ref['tcp'][0], tcp_now[1]-ref['tcp'][1]):.2f}mm 차 → "
              f"[{src}] 기둥 예상 이동 ({sx:+.0f},{sy:+.0f})px 만큼 옮겨서 찾는다)", flush=True)
    s_, d_, lab = match_feats(ref_p, meas["pillars"],
                              SINGLE_SEARCH_PX if single else DET[src]["search"])
    if not s_:
        # 두 가설: ① 베이스가 움직여 로봇이 따라감 → 기둥은 '계산 목표' 기준 예측 자리(EXPECT_TCP, 첫 회엔 기준 픽셀 그대로)
        #          ② 베이스 측정이 틀림 → 기둥은 '기준 자리' 기준 예측 자리(9/11 오전 B 파랑: 6mm 어긋나 30px 밀려 보임)
        #   ①에서 못 찾으면 ②를 뒤진다. 반경은 그대로(15px) — 넓히는 게 아니라 '있을 법한 두 자리'를 본다.
        global EXPECT_TCP
        _keep = EXPECT_TCP
        try:
            EXPECT_TCP = None
            sx2, sy2 = _pred_shift_px(ref, z_tcp, rz_tcp, src, tcp_now) if tcp_now else (0.0, 0.0)
        finally:
            EXPECT_TCP = _keep
        if (abs(sx2 - sx) > 2 or abs(sy2 - sy) > 2):
            ref_p2 = [(c, x + sx2, y + sy2, a) for c, x, y, a in ref["pillars"]]
            s_, d_, lab = match_feats(ref_p2, meas["pillars"], SINGLE_SEARCH_PX if single else DET[src]["search"])
            if s_:
                print(f"  ([{src}] 가설① 자리엔 없고 가설②(기준 자리 대비 {sx2:+.0f},{sy2:+.0f}px)에서 찾음 — 베이스 측정 오차 의심)", flush=True)
                sx, sy = sx2, sy2
    if single and not s_:
        # ★넓힌 반경 폴백: 같은 색 후보가 '딱 하나' 이고 면적이 기준과 맞을 때만 채택한다.
        rc0, rx0, ry0, ra0 = ref["pillars"][0]
        ex, ey = rx0 + sx, ry0 + sy
        cand = [q for q in meas["pillars"]
                if q[0] == rc0 and math.hypot(q[1] - ex, q[2] - ey) <= SINGLE_SEARCH_WIDE_PX]
        if len(cand) == 1 and ra0 and WIDE_AREA_LO * ra0 <= cand[0][3] <= WIDE_AREA_HI * ra0:
            q = cand[0]
            dd = math.hypot(q[1] - ex, q[2] - ey)
            print(f"  ⚠ [{src}] 지정 기둥이 예측 자리에서 {dd:.1f}px(>{SINGLE_SEARCH_PX:.0f}px) 벗어남 — "
                  f"{SINGLE_SEARCH_WIDE_PX:.0f}px 안에 {rc0} 후보가 하나뿐이고 면적 {int(q[3])}/{int(ra0)} "
                  f"({q[3]/ra0:.0%}) 로 기준과 맞아 **같은 점으로 채택**", flush=True)
            s_, d_ = [(ex, ey)], [(q[1], q[2])]
            lab = lab + [f"(넓힌 반경 채택: {dd:.1f}px)"]
        elif len(cand) > 1:
            print(f"  ([{src}] 넓힌 반경 {SINGLE_SEARCH_WIDE_PX:.0f}px 안에 {rc0} 후보가 "
                  f"{len(cand)}개 — 어느 점인지 특정 못 해 정지)", flush=True)
    if sx or sy:
        s_ = [(x - sx, y - sy) for x, y in s_]      # ★sim 은 반드시 '원래 기준 픽셀 → 지금' 이어야 한다
    if single and s_:
        # ★이웃 벽 점 오인 방어: 면적이 기준과 크게 다르면 다른 점을 문 것으로 본다.
        rc, rx, ry, ra = ref["pillars"][0]
        got = min(meas["pillars"], key=lambda q: math.hypot(q[1] - d_[0][0], q[2] - d_[0][1]))
        lo_, hi_ = SINGLE_AREA_BY_SRC.get(src, (SINGLE_AREA_LO, SINGLE_AREA_HI))
        if ra and not (lo_ * ra <= got[3] <= hi_ * ra):
            _ok = AREA_OK.get((src, round(rx), round(ry)))
            _r = got[3] / ra
            if _ok and AREA_ID_FLOOR <= _r <= AREA_ID_CEIL:
                print(f"  ⚠ [{src}] 기둥 면적 {int(got[3])}/{int(ra)} ({_r:.0%}) — 게이트({lo_:.0%}~{hi_:.0%}) 밖이지만 "
                      f"노출 {_ok['n']}칸에서 중심이 {_ok['spread']:.1f}px 안에 모여 **같은 점임이 확인됨** → 진행 "
                      f"(기준은 다른 조명에서 찍힌 것)", flush=True)
            else:
                return None, (f"[{src}] 기둥 면적 {int(got[3])} 이 기준 {int(ra)} 의 "
                              f"{lo_:.0%}~{hi_:.0%} 밖 — 다른 점을 문 것으로 보고 정지")
        if ra and not (0.6 * ra <= got[3] <= 1.6 * ra):
            print(f"  ⚠ [{src}] 기둥 면적 {int(got[3])}/{int(ra)} ({got[3]/ra:.0%}) — 위치는 예측 안, 시야각 차로 보고 진행", flush=True)
        lab = lab + [f"(기둥1: {rc} 면적 {int(got[3])}/{int(ra)}, 반경 {SINGLE_SEARCH_PX:.0f}px)"]
    if len(s_) < 1:
        return None, f"[{src}] 베이스 특징 매칭 {len(s_)}개(1 이상 필요) — 후보 {[(c, round(x), round(y)) for c, x, y, a in meas['pillars']]}"
    if len(s_) == 1:
        # 15:43 실기: 베이스 −2° 회전으로 새카메라에 기둥 1개만 → 평행이동만(s=1, θ=0). 회전은 rz 담당 카메라(손목 2점)가 맡는다.
        #   베이스 회전분의 벽 기대위치 오차 ≈ 기둥↔벽 거리(≈120px)×sin(θ) — 2° 에 4px(1mm) 수준.
        sim = (1.0, 0.0, float(d_[0][0] - s_[0][0]), float(d_[0][1] - s_[0][1]), 0.0)
        lab = lab + ["(1특징: 평행이동만)"]
    else:
        sim = similarity(s_, d_)
    # ★9/7 실기(red_s z440): 기둥 4점 중 파란 점 하나가 가려져 면적 222→149, 25px 밀리면서
    #   두 점 간격이 176→147px 로 보여 축척 0.966 → 정렬 자체가 실패했다(나머지 3점은 1.010 으로 정상).
    #   → 4점 이상이면 최악의 한 점을 빼고 다시 맞춰 본다(3점 이상 남을 때만, 게이트를 통과할 때만 채택).
    if len(s_) >= 4 and (abs(sim[0] - 1.0) > SCALE_TOL or sim[4] > RMS_TOL_PX):
        # 전체 맞춤의 잔차가 가장 큰 한 점(=가려진 점)을 빼고 한 번만 다시 맞춘다.
        res = np.linalg.norm(apply_sim(sim, [(x, y, 0) for x, y in s_]) - np.array(d_, float), axis=1)
        k = int(np.argmax(res))
        s2 = [q for i, q in enumerate(s_) if i != k]; d2 = [q for i, q in enumerate(d_) if i != k]
        try:
            sim2 = similarity(s2, d2)
        except Exception:
            sim2 = None
        if sim2 and abs(sim2[0] - 1.0) <= SCALE_TOL and sim2[4] <= RMS_TOL_PX:
            lab = [l for i, l in enumerate(lab) if i != k] + [f"(이상점 1개 제외 잔차 {res[k]:.0f}px: {lab[k]})"]
            sim, s_, d_ = sim2, s2, d2
    # ★9/7 실기(red_s z440, 발산 1.33→3.60mm): 특징이 3개로 줄면 남은 점들이 거의 한 직선 위에 놓인다
    #   (blue 861,481 · blue 1070,484 · yellow 810,468 → 수직 퍼짐 10px). 직선에서는 회전이 결정되지 않아
    #   실제 0.2° 를 θ=+5.32° 로 읽고 벽 기대위치를 망친다 → 수직 퍼짐이 좁으면 평행이동만 쓴다.
    if len(s_) >= 2:
        P = np.array(s_, float); P = P - P.mean(axis=0)
        try:
            perp = float(np.linalg.svd(P, compute_uv=False)[-1]) / max(1.0, np.sqrt(len(P)))
        except Exception:
            perp = 999.0
        if perp < COLLINEAR_PERP_PX:
            t = np.array(d_, float).mean(axis=0) - np.array(s_, float).mean(axis=0)
            sim = (1.0, 0.0, float(t[0]), float(t[1]), float(sim[4]))
            lab = lab + [f"(특징이 거의 일직선 수직퍼짐 {perp:.0f}px < {COLLINEAR_PERP_PX} → 회전 포기, 평행이동만)"]
    s, th, tx, ty, rms = sim
    if abs(s - 1.0) > SCALE_TOL:
        return None, f"[{src}] 특징 축척 {s:.3f} — 기준과 높이/거리가 다름"
    if rms > RMS_TOL_PX:
        return None, f"[{src}] 특징 맞춤 rms {rms:.1f}px > {RMS_TOL_PX} — 오매칭 의심(안착 벽 이동/점 뒤바뀜)"
    if color_base_only(ref):
        # ★밑판 기준: 밑판 특징이 기준 자리에서 (tx,ty) 만큼 옮겨 보이면, 로봇이 밑판에 대해 그만큼 움직인 것이다.
        #   되돌리려면 반대로 간다 → dpx = (-tx, -ty). (9/10 실측 검증: 밑판 점 이동 = +J·Δ로봇)
        dpx = (-float(tx), -float(ty))
        Jinv, rsign = jinv_ref(ref, src, z_tcp, rz_tcp)
        dmm = Jinv @ np.array(dpx)
        return {"src": src, "dpx": dpx, "dang_img": -th, "dmm": (float(dmm[0]), float(dmm[1])),
                "drz": 0.0, "sim": {"s": s, "theta": th, "rms": rms},
                "matched": lab + [f"(밑판 기준 모드: 특징 {len(s_)}개, 든 벽 점 미사용)"],
                "one_dot": True, "scale_mm_px": float(np.hypot(*Jinv[:, 0])), "rz": rz_tcp}, None
    w_exp = apply_sim(sim, ref["wall"]); w_now = np.array([p[:2] for p in meas["wall"]], float)
    one_dot = len(meas["wall"]) < 2 or len(ref["wall"]) < 2
    # ★9/9 17:35 실기 크래시: 기준 벽 점 2개인데 측정이 3개(조각/반사) → w_now - w_exp 가
    #   (3,2)-(2,2) 로 broadcast 에러. 지금까지 red_s 기준이 1점이라 one_dot 로 빠져 이 길을 안 탔다.
    #   기준 각 점에 가장 가까운 측정 점을 1:1 로 붙이고 남는 것은 버린다. 짝이 멀면 오매칭이므로 거부.
    if not one_dot and len(w_now) != len(w_exp):
        used, pick, far = set(), [], 0.0
        for e in w_exp:
            best = None
            for i, q in enumerate(w_now):
                if i in used:
                    continue
                dd = float(np.hypot(q[0] - e[0], q[1] - e[1]))
                if best is None or dd < best[0]:
                    best = (dd, i)
            if best is None:
                break
            used.add(best[1]); pick.append(best[1]); far = max(far, best[0])
        if len(pick) != len(w_exp):
            return None, f"[{src}] 든 벽 점 측정 {len(w_now)}개 ↔ 기준 {len(w_exp)}개 — 짝지을 수 없음"
        if far > WALL_PAIR_MAX_PX:
            return None, f"[{src}] 든 벽 점 짝 거리 {far:.0f}px > {WALL_PAIR_MAX_PX} — 오매칭 의심(재파지/기준 재촬영)"
        print(f"  (든 벽 점 측정 {len(meas['wall'])}개 중 기준과 짝지은 {len(pick)}개 사용, 최대 짝 거리 {far:.0f}px)", flush=True)
        w_now = w_now[pick]
    if one_dot and len(w_exp) >= 2 and len(w_now) >= 1:
        # ★9/11 19:33 실기: 기준 2점인데 1점만 검출되자 **순서만 보고 기준 첫 점과 비교** → Δ(-195,+64)px
        #   = 두 기준 점 간격(194px) 그대로. 자 1.49배가 곱해져 53mm 명령 → 12mm 끌려간 뒤 발산 감지로 정지.
        #   1점만 보일 때는 '그 점이 어느 기준 점인지' 를 최근접으로 고르고, 그마저 멀면 거부한다.
        j = int(np.argmin([float(np.hypot(w_now[0][0] - e[0], w_now[0][1] - e[1])) for e in w_exp]))
        dd = float(np.hypot(w_now[0][0] - w_exp[j][0], w_now[0][1] - w_exp[j][1]))
        if dd > WALL_PAIR_MAX_PX:
            return None, (f"[{src}] 든 벽 점 {len(w_now)}/{len(w_exp)}개만 검출 — 가장 가까운 기준 점과도 {dd:.0f}px "
                          f"> {WALL_PAIR_MAX_PX} — 벽이 프레임 밖으로 나갔거나 오매칭. 재파지 권함")
        print(f"  (든 벽 점 1개만 검출 → 기준 {j+1}번 점과 짝지음, 거리 {dd:.0f}px)", flush=True)
        w_exp = w_exp[j:j + 1]
    if one_dot:
        w_exp, w_now = w_exp[:1], w_now[:1]
        mid_exp, mid_now = tuple(w_exp[0]), tuple(w_now[0]); dang = -th
    else:
        if np.linalg.norm(w_now - w_exp) > np.linalg.norm(w_now[::-1] - w_exp):
            w_now = w_now[::-1]
        mid_exp, ang_exp = wall_mid_ang(w_exp); mid_now, ang_now = wall_mid_ang(w_now)
        dang = HG.wrap_deg(ang_now - ang_exp)
        if dang > 90: dang -= 180
        if dang < -90: dang += 180
    dpx = (mid_now[0] - mid_exp[0], mid_now[1] - mid_exp[1])
    Jinv, rsign = jinv_ref(ref, src, z_tcp, rz_tcp)
    dmm = Jinv @ np.array(dpx)
    if float(np.hypot(*dmm)) > SANE_MAX_MM:      # ★9/11: 정상 사이클에서 나올 수 없는 크기 — 계산 결과를 믿지 말고 즉시 거부
        return None, (f"[{src}] 정렬 계산 {float(np.hypot(*dmm)):.1f}mm (Δ{dpx[0]:+.0f},{dpx[1]:+.0f}px) > {SANE_MAX_MM}mm "
                      f"— 점 오매칭/기준 불일치 의심, 움직이지 않고 정지")
    if SRC[src]["moving"] == "wall":
        dmm = -dmm                     # 벽이 로봇과 함께 움직이는 카메라: Δ 를 없애려면 반대로
    return {"src": src, "dpx": dpx, "dang_img": dang, "dmm": (float(dmm[0]), float(dmm[1])), "drz": rsign * dang,
            "sim": {"s": s, "theta": th, "rms": rms}, "matched": lab, "one_dot": one_dot,
            "scale_mm_px": float(np.hypot(*Jinv[:, 0])), "rz": rz_tcp}, None


# ------------------------------------------------------------------ 로봇
def st():
    return json.loads(UR.urlopen(BR + "/status", timeout=6).read())["robots"]["fr5"]


def post(a, b):
    r = UR.Request(BR + "/" + a, json.dumps(b).encode(), {"Content-Type": "application/json"})
    return UR.urlopen(r, timeout=20).read().decode()


def move_rel(dx, dy, drz, tol=0.3, timeout=40):
    """1% 속도 상대 이동. 도달은 TCP 폴링(9/5: busy 조기 False 오판 사고)."""
    c = st()["tcp"]
    tgt = [c[0] + dx, c[1] + dy, c[2], c[3], c[4], HG.wrap_deg(c[5] + drz)]
    # 9/6 12:56 실기: 브리지 fr5/move 는 관절 명령(joints 6개 요구) → 422. 카테시안은 move_tcp + dry_run 선검사(place_calc.move 와 동일 계약).
    post("fr5/speed", {"value": 1, "dry_run": False})
    r = json.loads(post("fr5/move_tcp", {"tcp": tgt, "dry_run": True}))
    if r.get("result") != "dry_run":
        raise RuntimeError(f"상대이동 dry_run 거부 {r}")
    r = json.loads(post("fr5/move_tcp", {"tcp": tgt, "dry_run": False}))
    if r.get("result") not in ("started", "ok"):
        raise RuntimeError(f"상대이동 거부 {r}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.3); n = st()["tcp"]
        if max(abs(n[i] - tgt[i]) for i in range(3)) <= tol and abs(HG.wrap_deg(n[5] - tgt[5])) <= 0.1:
            return n
    raise RuntimeError(f"이동 미도달 (목표 {[round(v, 2) for v in tgt]} 현재 {[round(v, 2) for v in st()['tcp']]})")


# ------------------------------------------------------------------ 기준·측정·정렬
def load_ref(color, src=None):
    """색별 기준 {"wrist": {...}, "newcam": {...}}. src 주면 그 카메라 것만."""
    if not os.path.exists(REF):
        return None
    d = json.load(open(REF)).get(color)
    if not d:
        return None
    return d.get(src) if src else d


def save_ref(color, src="wrist", seeds=None, img=None):
    img = img if img is not None else grab(src)
    meas, why = measure(img, color, None, seeds, src)
    if not meas:
        raise RuntimeError(why)
    pick = pillar_pick(color, src)
    if not pick and color not in BASE_ONLY_ALIGN:
        # ★9/10 사용자 지적: "사진도 줬는데 왜 자꾸 맘대로 점을 넓게 해서 저장하나."
        #   지정이 없으면 검출된 밑판 특징이 **전부** 기준에 들어가고, 거기엔 이미 꽂힌 벽의 점이 섞인다.
        #   그 벽이 빠지거나 밀리면 기준이 통째로 틀어진다 → 지정 없이는 저장을 거부한다.
        raise RuntimeError(
            f"[{color}/{src}] 기둥 지정이 없다 — pillar_pick.json 에 이 카메라의 기준 점을 먼저 등록할 것. "
            f"(지금 검출된 후보: {[(c, round(x), round(y), int(a)) for c, x, y, a in meas['pillars']]})")
    if pick:
        # ★지정된 기둥들만 남긴다 — 꽂힌 벽의 점이 기준에 섞이는 것을 원천 차단.
        keep, used = [], []
        for px, py in pick:
            cand = [q for q in meas["pillars"] if math.hypot(q[1] - px, q[2] - py) <= PICK_TOL_PX]
            if not cand:
                raise RuntimeError(f"[{src}] 지정 기둥({px:.0f},{py:.0f}) 근처 {PICK_TOL_PX:.0f}px 안에 점이 없다 — 자세/노출 확인")
            q = min(cand, key=lambda q: math.hypot(q[1] - px, q[2] - py))
            if q not in keep:
                keep.append(q); used.append((px, py))
        dropped = [(c, round(x), round(y), int(a)) for c, x, y, a in meas["pillars"] if (c, x, y, a) not in [tuple(k) for k in keep]]
        meas["pillars"] = keep
        print(f"  (지정 기둥 {len(keep)}개만 사용: " +
              ", ".join(f"{q[0]}({q[1]:.0f},{q[2]:.0f})a{int(q[3])}" for q in keep) +
              f" · 제외 {len(dropped)}개 {dropped[:4]})")
        if not meas["wall"] and color not in BASE_ONLY_ALIGN:
            raise RuntimeError(f"[{src}] 든 벽 점 0개 — 기둥 1개 방식은 벽 점이 반드시 있어야 한다")
    elif len(meas["pillars"]) < 2:
        raise RuntimeError(f"[{src}] 베이스 특징 {len(meas['pillars'])}개 — 이 자세에선 기준을 못 만든다(둘 다 보이는 자세 필요)")
    if len(meas["wall"]) < 2:
        print(f"  ⚠ [{src}] 든 벽 점 {len(meas['wall'])}개 — 위치만 정렬, 회전은 다른 카메라/랙 각으로")
    # ★9/7 실기(red_s): 든 벽 점 바로 옆(26px)에 같은 색 반사가 잡혀 '기둥 특징'으로 기준에 박혔다.
    #   벽 점과 같은 색이라 다음 정렬에서 벽 점을 이 특징에 짝지을 수 있다 → 벽 점 근처의 같은 색 특징은 기준에서 뺀다.
    wcol = wall_color(color, src)
    if (color, src) not in WALL_COLOR_BY_SRC:
        wcol = "red" if color in ("red", "red_s") else color
    if meas["wall"]:
        keep = []
        for q in meas["pillars"]:
            if q[0] == wcol and any(math.hypot(q[1] - w[0], q[2] - w[1]) <= SAME_COLOR_NEAR_PX for w in meas["wall"]):
                print(f"  (기준에서 제외: 든 벽 점 {SAME_COLOR_NEAR_PX:.0f}px 안의 같은 색 특징 {q[0]}({q[1]:.0f},{q[2]:.0f}) area {q[3]})")
                continue
            # ★9/7 16:4x 실기(파랑): 든 벽의 **반대쪽 끝 점**(1027,702)과 그 조각 2개가 '기둥 특징'으로 저장됐다.
            #   기준 벽 점(1027,279)에서 423px 떨어져 위 규칙을 빠져나갔다. 든 벽의 점들은 같은 색이고
            #   화면에서 거의 같은 x 열(±20px)에 세로로 늘어선다 → 그 열의 같은 색은 벽의 것으로 보고 뺀다.
            if q[0] == wcol and any(abs(q[1] - w[0]) <= WALL_COLUMN_PX for w in meas["wall"]):
                print(f"  (기준에서 제외: 든 벽과 같은 x열의 같은 색 특징 {q[0]}({q[1]:.0f},{q[2]:.0f}) area {q[3]})")
                continue
            keep.append(q)
        if not pick:                      # 지정 기둥 방식은 이미 하나로 골라 뒀다 — 건드리지 않는다
            meas["pillars"] = keep
            if len(meas["pillars"]) < 2:
                raise RuntimeError(f"[{src}] 같은 색 특징 제외 후 베이스 특징 {len(meas['pillars'])}개 — 기준 저장 불가")
    tcp = st()["tcp"]
    d = json.load(open(REF)) if os.path.exists(REF) else {}
    expo = None
    if src == "wrist":
        try:
            expo = json.loads(UR.urlopen("http://127.0.0.1:8766/expo", timeout=3).read()).get("exposure")
        except Exception:
            pass
    d.setdefault(color, {})[src] = {"wall": meas["wall"], "pillars": meas["pillars"], "tcp": tcp, "z": tcp[2], "expo": expo, "newcam_bright": newcam_bright(),
                                    "made": time.strftime("%Y-%m-%d %H:%M"), "note": "사용자 정렬 확인 상태(하강 직전)"}
    json.dump(d, open(REF, "w"), ensure_ascii=False, indent=1)
    print(f"✅ [{color}/{src}] 호버 기준 저장: 벽 점 {[(round(x), round(y)) for x, y, a in meas['wall']]} "
          f"베이스 특징 {len(meas['pillars'])}개 z{tcp[2]:.0f} rz{tcp[5]:.1f}")


def promote_ref(color, note="삽입 성공 사이클에서 자동 승격"):
    """★삽입 성공 직후 호출: 성공한 사이클의 정렬 상태(마지막 check 프레임)를 기준으로 승격. 손으로 맞춘 기준보다 정확."""
    last = promote_ref.last.get(color) if hasattr(promote_ref, "last") else None
    if not last:
        return False
    d = json.load(open(REF)) if os.path.exists(REF) else {}
    # ★9/12: 승격이 측정값을 통째로 덮어써서 **지정(pillar_pick)·자(plane_scale)·실측 매핑(Jinv_override)·노출을 전부
    #   날리고 있었다.** (9/11 B red 새카메라가 1점→4점으로 되돌아가고, yellow_in 의 자 1.49 가 사라진 원인.)
    #   → ①지정된 기둥만 남기고 ②기준에만 있는 설정 키들은 옛 항목에서 이어받는다. 못 지키면 그 카메라는 승격하지 않는다.
    KEEP = ("plane_scale", "plane_scale_note", "Jinv_override", "Jinv_override_note",
            "expo", "newcam_bright", "note", "wall_removed_0911")
    saved = []
    for src, m in last.items():
        old_e = (d.get(color) or {}).get(src) or {}
        m = dict(m)
        pick = pillar_pick(color, src)
        if pick:
            keep = []
            for px, py in pick:
                cand = [q for q in m.get("pillars", []) if math.hypot(q[1] - px, q[2] - py) <= PICK_TOL_PX]
                if not cand:
                    keep = None; break
                q = min(cand, key=lambda q: math.hypot(q[1] - px, q[2] - py))
                if q not in keep:
                    keep.append(q)
            if keep is None:
                print(f"  (승격 건너뜀 [{color}/{src}]: 지정 기둥이 측정에 없다 — 옛 기준 유지)"); continue
            if len(keep) != len(m.get("pillars", [])):
                print(f"  (승격: 지정 기둥 {len(keep)}개만 유지, {len(m['pillars']) - len(keep)}개 제외)")
            m["pillars"] = keep
        elif color not in BASE_ONLY_ALIGN:
            print(f"  (승격 건너뜀 [{color}/{src}]: 기둥 지정 없음 — 지정 없는 기준을 새로 만들지 않는다)"); continue
        if color in BASE_ONLY_ALIGN:
            m["wall"] = []                                   # 밑판 기준 모드는 든 벽 점을 쓰지 않는다
        elif not m.get("wall"):
            print(f"  (승격 건너뜀 [{color}/{src}]: 든 벽 점 0개)"); continue
        for k in KEEP:                                       # 기준에만 있는 설정은 이어받는다
            if k in old_e and k not in m:
                m[k] = old_e[k]
        d.setdefault(color, {})[src] = dict(m, made=time.strftime("%Y-%m-%d %H:%M"), note=note)
        saved.append(src)
    if not saved:
        print(f"  (호버 기준 승격 없음 [{color}] — 옛 기준 그대로)"); return False
    json.dump(d, open(REF, "w"), ensure_ascii=False, indent=1)
    print(f"  ✅ [{color}] 호버 기준 승격({', '.join(saved)})"); return True
promote_ref.last = {}


def available_sources(color):
    refs = load_ref(color) or {}
    out = []
    for src in ("wrist", "newcam", "side"):
        if src not in refs:
            continue
        if SRC[src]["map"] and not os.path.exists(SRC[src]["map"]):
            print(f"  ⚠ {src} 기준은 있으나 매핑 없음(probe 필요) → 제외"); continue
        try:
            UR.urlopen(SRC[src]["url"].replace("/raw", "/health"), timeout=2).read()
        except Exception:
            print(f"  ⚠ {src} 카메라 응답 없음 → 제외"); continue
        out.append(src)
    return out


def _pub(color, src, ref, meas, z, rz, tcp, D=None, why=None):
    """★9/12 표시용 내보내기. 정렬 계산과 완전히 분리 — 어떤 예외도 삼킨다."""
    if _RO is None:
        return
    try:
        single = len(ref.get("pillars") or []) == 1
        try:
            sx, sy = _pred_shift_px(ref, z, rz, src, tcp) if tcp else (0.0, 0.0)
        except Exception:
            sx, sy = 0.0, 0.0
        search = {"shift": [float(sx), float(sy)], "r": float(SINGLE_SEARCH_PX if single else DET[src]["search"])}
        _RO.publish(color, src, ref, meas,
                    delta=[float(D["dmm"][0]), float(D["dmm"][1])] if D else None,
                    tcp=[round(float(v), 2) for v in tcp[:3]] if tcp else None, search=search, note=why)
    except Exception:
        pass


def check(color, srcs=None, roles=None):
    """가용 카메라 전부 Δ. 반환 (combined, per_src, why). combined=None 이면 하강 금지.
    roles: {src: "xy"|"rz"|"both"|"measure"} — 결합에 쓰는 역할(measure=측정·승격만, 결합 제외). 없으면 전부 both."""
    if not hasattr(check, "expo_done"):
        check.expo_done = False
    roles = roles or {}
    srcs = srcs or (list(roles) if roles else None) or available_sources(color)
    if not srcs:
        return None, {}, f"{color} 호버 기준 없음 (hover_align.py ref {color} [--src newcam --wall x,y])"
    t = st()["tcp"]; z, rz = t[2], t[5]
    per, last = {}, {}
    for src in srcs:
        ref = load_ref(color, src)
        _pub(color, src, ref, None, z, rz, t)                 # 기준점 먼저(측정 실패해도 기준은 보이게)
        if abs(z - ref["z"]) > 3.0:
            return None, per, f"[{src}] 높이 z{z:.0f} ≠ 기준 z{ref['z']:.0f}"
        if src == "wrist" and ref.get("expo") and not check.expo_done:
            # ★9/7 red_s 사고: 기준은 노출 83 에서 찍혔는데 정렬 때 42 라 **노란 기둥 점이 사라져** 파랑 3개로만 맞춤 →
            #   좌표계가 틀어져 로봇을 y +3mm 엉뚱하게 옮김. 기준을 찍은 노출로 먼저 맞추고 잰다.
            try:
                want = float(ref["expo"])
                # ★9/10 사용자 제안 "새카메라 밝기를 따라가고, 안 되면 올리거나 내리자".
                #   기준을 찍을 때의 새카메라 밝기와 지금을 비교해 **사다리 시작 칸만** 옮긴다.
                #   (검출이 모자라면 그 아래 기존 사다리 폴백이 계속 훑는다)
                nb0, nb1 = ref.get("newcam_bright"), newcam_bright()
                if nb0 and nb1 and nb0 > 1:
                    r = nb1 / nb0
                    if r < 1 - NEWCAM_TOL:            # 방이 어두워졌다 → 노출 한 칸 위
                        want = expo_shifted(want, +1 if r > 0.6 else +2)
                        print(f"  (새카메라 밝기 {nb0:.0f}→{nb1:.0f}, 어두워짐 → 기준 노출 {ref['expo']:.0f}→{want:.0f})", flush=True)
                    elif r > 1 + NEWCAM_TOL:          # 밝아졌다 → 한 칸 아래
                        want = expo_shifted(want, -1 if r < 1.7 else -2)
                        print(f"  (새카메라 밝기 {nb0:.0f}→{nb1:.0f}, 밝아짐 → 기준 노출 {ref['expo']:.0f}→{want:.0f})", flush=True)
                if abs(float(current_expo() or 0) - want) > 1.0:
                    set_expo(want); time.sleep(EXPO_SETTLE_S)   # ★9/10: 0.6s 로는 전환 중 프레임을 읽는다(실측 1.2~1.4s 필요)
                    print(f"  (기준 촬영 노출 {want:.0f} 로 맞춤)", flush=True)
            except Exception:
                pass
        img = grab(src)
        meas, why = measure_multi(color, ref, None, src)      # ★단발 → 다중프레임 병합
        if src == "wrist" and (not meas or len(meas["pillars"]) < max(2, len(ref["pillars"]))) and not check.expo_done:
            check.expo_done = True
            if pick_hover_expo(color, ref) is not None:
                meas, why = measure(grab(src), color, ref, None, src)
        # ★9/10: 조종하지 않는 카메라(role="measure")의 실패는 정렬 전체를 멈추면 안 된다.
        #   참고용으로 재는 것이라 못 재면 "못 쟀다"고 남기고 넘어가는 게 맞다(B타입 파랑 wrist=measure 가
        #   rms 7.1px 로 실패했다고 사이클이 멈춘 사례). 조종 카메라(xy/rz/both)의 실패는 그대로 정지.
        def _bail(why_):
            if roles.get(src, "both") == "measure":
                print(f"  (참고 카메라 [{src}] 측정 실패 — 정렬은 계속: {why_})", flush=True)
                return None          # 이 src 는 건너뛴다
            return ("STOP", why_)
        if not meas:
            _b = _bail(why)
            if _b is None: continue
            return None, per, _b[1]
        # ★기준 특징이 다 안 잡히면 좌표계가 틀어진다(red_s 3mm 오차의 진범).
        #   멈추기 전에 노출 사다리로 되찾아 본다 — 못 찾을 때만 정지.
        if src == "wrist" and len(meas["pillars"]) < len(ref["pillars"]):
            keep = current_expo()
            for e in ([float(ref["expo"])] if ref.get("expo") else []) + list(HOVER_EXPO_LADDER):
                set_expo(e)
                time.sleep(EXPO_SETTLE_S)   # ★9/10: 노출 바꾼 직후 프레임은 전환 중이라 못 쓴다
                m2, _w2 = measure_multi(color, ref, None, src)
                if m2 and len(m2["pillars"]) >= len(ref["pillars"]):
                    print(f"  (기둥 특징 {len(meas['pillars'])}→{len(m2['pillars'])}개: 노출 {e:.0f} 로 되찾음)", flush=True)
                    meas = m2; break
            else:
                if keep: set_expo(keep)
        # ★9/7 16:2x 사용자 지적으로 원복: 이 '기준 개수 전부' 요구는 까다로움이 아니라
        #   **기준이 지금 화면과 안 맞는다는 것을 알려 주는 감지기**다. 60%로 풀었더니 3/5 로 계산해
        #   10mm 짜리 엉뚱한 보정을 내놓았다(파랑 15:42 기준이 15:56 픽 변경보다 앞서 낡았던 것).
        #   개수가 모자라면 푸는 게 아니라 기준을 다시 찍는 것이 맞다.
        if len(meas["pillars"]) < len(ref["pillars"]):
            _w = (f"[{src}] 기둥 특징 {len(meas['pillars'])}개 < 기준 {len(ref['pillars'])}개 — "
                  f"노출 사다리로도 못 되찾음(기준 노출 {ref.get('expo')}). 좌표계가 틀어져 정지")
            _b = _bail(_w)
            if _b is None: continue
            return None, per, _b[1]
        D, why = delta(ref, meas, z, rz, src, tcp_now=t)
        if D is None and src == "wrist" and "면적" in (why or "") and not getattr(check, "area_expo_done", False):
            check.area_expo_done = True
            print(f"  ⚠ [{src}] {why} → 노출을 조정해 같은 점을 기준 면적으로 다시 잡는다", flush=True)
            if pick_expo_by_area(color, ref, src) is not None:
                check.expo_done = True          # 고른 노출을 정렬 끝까지 유지(안 그러면 다음 반복에서 기준 노출로 되돌아가 또 실패)
                meas, why2 = measure_multi(color, ref, None, src)
                if meas:
                    D, why = delta(ref, meas, z, rz, src, tcp_now=t)
        # ★9/13 15:18 A 파랑 정지의 진범: 손목캠 검출은 2개(파랑 1001,556 / 120,267)인데
        #   **지정 기둥(노랑 835,266)이 그 안에 없었다**. 위쪽 노출 사다리는 '검출 개수 < 기준 개수'(2<1=거짓)
        #   만 보기 때문에 한 칸도 안 돌고 3초 만에 정지했다(실측 로그 15:18:18→21).
        #   개수가 아니라 **지정한 그 점이 잡혔는가**가 조건이어야 한다 —
        #   사용자 지시 "손목캠이 자동으로 노출 맞춰라, 기준 점 나올 때까지".
        #   (실측 9/13 같은 자리: 노출 333/417 노랑 0개, 83/167/250 에서 (833,26x) 검출)
        if D is None and src == "wrist" and "매칭" in (why or "") and not getattr(check, "match_expo_done", False):
            check.match_expo_done = True
            print(f"  ⚠ [{src}] {why}", flush=True)
            print(f"  → 지정 기준 점이 안 잡혔다. 노출 사다리로 되찾는다(기준 노출 {ref.get('expo')})", flush=True)
            if pick_hover_expo(color, ref) is not None:
                check.expo_done = True      # 고른 노출을 정렬 끝까지 유지
                meas3, _w3 = measure_multi(color, ref, None, src)
                if meas3:
                    meas = meas3
                    D, why = delta(ref, meas, z, rz, src, tcp_now=t)
            else:
                why = (why or "") + " — 노출 사다리 전 칸(250/167/83/333/417/500)에서도 지정 점 못 찾음"
        if D is None:
            _pub(color, src, ref, meas, z, rz, t, None, why)   # 실패해도 어디까지 잡았는지 보이게
            _b = _bail(why)
            if _b is None: continue
            return None, per, _b[1]
        per[src] = D
        last[src] = {"wall": meas["wall"], "pillars": meas["pillars"], "tcp": t, "z": z}
        _pub(color, src, ref, meas, z, rz, t, D)              # 매칭점 + Δ
    promote_ref.last[color] = last
    # 결합: XY 평균(불일치 게이트), rz 는 점 2개 카메라 우선 — roles 가 있으면 역할별로
    xy_keys = [k for k in per if roles.get(k, "both") in ("xy", "both")]
    rz_keys = [k for k in per if roles.get(k, "both") in ("rz", "both")]
    if not xy_keys:
        return None, per, "XY 담당 카메라 없음(roles)"
    for i in range(len(xy_keys)):
        for j in range(i + 1, len(xy_keys)):
            A, B_ = per[xy_keys[i]], per[xy_keys[j]]
            dd = math.dist(A["dmm"], B_["dmm"])
            if dd > COMBINE_TOL_MM:
                # ★9/11 사용자 지시("기준 화면을 찾아가라"): 매핑 오차는 기준 자리에서 멀수록 커진다
                #   (9/10 실측: 3.4mm 밖 불일치 1.5~1.9 / 기준 자리 0.099). 멀리서 판정하면 항상 걸린다.
                #   두 캠이 '같은 방향·크기 2배 이내' 이고 둘 다 FAR_STEP_MM 넘게 멀면 = 점 오인이 아니라 거리 탓
                #   → 작은 쪽 벡터로 한 발 다가가 재측정(불일치 게이트는 가까워진 뒤 그대로 적용). 방향이 다르면 즉시 정지(점 오인).
                a = math.hypot(*A["dmm"]); b = math.hypot(*B_["dmm"])
                dot = A["dmm"][0]*B_["dmm"][0] + A["dmm"][1]*B_["dmm"][1]
                same_dir = a > 0 and b > 0 and dot / (a*b) > 0.85
                # 불일치는 거리에 비례해 준다(15:37 실측 노랑: 거리 7.7→5 에 3.4→2.7, 약 40%) — 두 캠 배율이 서로 어긋난 것.
                #   허용 = max(1.5, 0.5×작은 쪽 거리): 기준 자리에선 1.5mm 그대로, 멀면 다가가며 재판정.
                # 15:43 실측(노랑 rz91): 불일치가 거리에 비례하지 않고 새캠≈손목×1.8 로 배율만 다르다(기준은 같은 프레임 0.046mm 일치).
                #   배율 차는 종점을 바꾸지 않는다(두 기준이 같은 프레임 → 둘 다 0 이 되는 자리는 하나). 멀리서는 방향·배율(2배 이내)만 검사.
                tol_here = max(COMBINE_TOL_MM, 0.5 * min(a, b))
                if same_dir and max(a, b) <= 2.0 * min(a, b) and min(a, b) > COMBINE_TOL_MM:
                    small = A if a <= b else B_
                    print(f"  ⚠ 카메라 불일치 {dd:.2f}mm (허용 {tol_here:.2f}=거리 {min(a,b):.1f}mm 의 절반) 같은 방향 → 작은 쪽({small['src']})으로 다가가 재측정", flush=True)
                    per_far = {small["src"]: small}
                    # ★9/13 17:05 B 파랑: 파지가 8.5mm 옆이라 초기 10.8mm, 벽이 0.77° 돌아 있었는데 멀다고 rz 를 보류한 채
                    #   XY 만 3mm 씩 다가감 → 회전이 남아 두 캠이 서로 다른 벽 점을 보므로 x 로 매번 ~2mm 어긋나다가
                    #   가까워지자 불일치 게이트에 걸려 정지. 두 점 카메라가 잰 rz 는 XY 불일치와 무관한 값이라 멀어도 같이 푼다.
                    _two = [per[k] for k in rz_keys if not per[k]["one_dot"]]
                    _drz = (sum(D["drz"] for D in _two) / len(_two)) if _two else 0.0
                    C = {"dmm": tuple(small["dmm"]), "drz": _drz, "n_src": 1,
                         "rz_from": ("2점 " + ",".join(D["src"] for D in _two) + "(멀어도 회전은 풂)") if _two else "보류(멀다)", "far": True}
                    return C, per, None
                return None, per, f"카메라 불일치({xy_keys[i]}↔{xy_keys[j]}) XY {dd:.2f}mm — 매핑/기준 의심, 정지"
    xs = [per[k]["dmm"] for k in xy_keys]
    mx = sum(v[0] for v in xs) / len(xs); my = sum(v[1] for v in xs) / len(xs)
    two = [per[k] for k in rz_keys if not per[k]["one_dot"]]
    if len(two) >= 2:
        da = max(abs(HG.wrap_deg(a["drz"] - b["drz"])) for a in two for b in two)
        if da > COMBINE_TOL_DEG:
            return None, per, f"카메라 rz 불일치 {da:.2f}° — 매핑/기준 의심, 정지"
    if two:
        drz = sum(D["drz"] for D in two) / len(two); rz_from = "2점(" + ",".join(D["src"] for D in two) + ")"
    elif rz_keys:
        drz = per[rz_keys[0]]["drz"]; rz_from = f"1점 {rz_keys[0]}(−θ 가정)"
    else:
        # rz 담당 카메라 없음(전부 xy/measure) = ★rz 고정 모드: 상대 회전을 아예 안 준다.
        #   9/6 17:4x 실측 — 절대 rz 명령은 0.01° 안에 정확히 도달, 상대 회전 누적은 177.85~179.90 로 흩어짐.
        #   rz 는 운반 단계의 절대 명령(슬롯 기준 rz + 베이스 Δyaw)으로 확정하고 정렬은 XY 만 맞춘다(사용자 설계, 9/2 홈포즈 방식과 동일).
        drz = 0.0; rz_from = "고정(절대 rz 유지)"
    return {"dmm": (mx, my), "drz": drz, "n_src": len(xy_keys), "rz_from": rz_from}, per, None


def fmt(D):
    return (f"[{D['src']}{' 점1개' if D.get('one_dot') else ''}] Δpx ({D['dpx'][0]:+.1f},{D['dpx'][1]:+.1f}) 각 {D['dang_img']:+.2f}° → "
            f"XY ({D['dmm'][0]:+.2f},{D['dmm'][1]:+.2f})mm rz {D['drz']:+.2f}° "
            f"(특징 {len(D['matched'])} s={D['sim']['s']:.3f} θ={D['sim']['theta']:+.2f}° rms {D['sim']['rms']:.1f}px {D['scale_mm_px']:.3f}mm/px)")


def _learn_move_K(color, srcs, roles, e0, tol_deg):
    """★시험 이동 2번으로 '명령 mm → 실제로 줄어든 mm' 행렬 K 를 그 자리에서 배운다.
    K = I 면 저장된 자가 맞는 것. 자가 틀렸거나(평면 차이) 벽이 죠 안에서 미끄러지면 K 가 작아진다.
    반환 (K, 마지막 측정 e). 못 배우면 (None, 마지막 e) — 호출부는 종전 경로로 간다."""
    cols, e_prev = [], np.array(e0, float)
    for k, ax in enumerate(("x", "y")):
        # ★시험 이동은 **오차가 줄어드는 쪽**으로 찌른다(반대로 찌르면 오차를 키워 뒤에서 상한에 걸린다)
        h = LEARN_STEP_MM * (1.0 if float(e_prev[k]) >= 0 else -1.0)
        d = [0.0, 0.0]; d[k] = h
        move_rel(d[0], d[1], 0.0); time.sleep(0.6)
        C, per, why = check(color, srcs, roles)
        if C is None:
            print(f"  (시험 이동 {ax}: 측정 실패 — K 학습 포기, 종전 방식)", flush=True)
            return None, e_prev
        e_now = np.array(C["dmm"], float)
        col = (e_prev - e_now) / h          # 1mm 명령했을 때 실제로 줄어든 mm
        print(f"  시험 이동 {ax} {h:+.1f}mm → 오차 ({e_prev[0]:+.2f},{e_prev[1]:+.2f}) → ({e_now[0]:+.2f},{e_now[1]:+.2f}) ⇒ K 열 ({col[0]:+.2f},{col[1]:+.2f})", flush=True)
        cols.append(col); e_prev = e_now
    K = np.array(cols).T
    try:
        sv = np.linalg.svd(K, compute_uv=False)
    except Exception:
        return None, e_prev
    cond = float(sv.max() / max(sv.min(), 1e-9))
    if not (LEARN_K_MIN <= float(sv.min()) and float(sv.max()) <= LEARN_K_MAX) or cond > LEARN_K_COND:
        print(f"  ⚠ 배운 K 특이값 {np.round(sv,2).tolist()} (비 {cond:.1f}) 이 허용 {LEARN_K_MIN}~{LEARN_K_MAX}·비 {LEARN_K_COND} 밖 "
              f"— 측정이 깨진 것으로 보고 정지(추측으로 움직이지 않는다)", flush=True)
        return "BAD", e_prev
    print(f"  ✅ 배운 K = {np.round(K,3).tolist()} (I 이면 자가 정확). 자 배율 ≈ {1/float(np.sqrt(abs(np.linalg.det(K)))):.2f}", flush=True)
    return K, e_prev


def align(color, dry=False, tol_mm=TOL_MM, tol_deg=TOL_DEG, srcs=None, roles=None):
    """보정 루프. 수렴 True / dry False / 실패 예외(호출자가 정지·보고). srcs 로 카메라 지정, roles 로 역할(xy/rz/both/measure)."""
    prev = None
    check.expo_done = False
    check.area_expo_done = False
    check.match_expo_done = False
    AREA_OK.clear()          # ★확인 결과는 이번 정렬에만 유효(다음 색·다음 자세로 새 나가면 안 된다)
    rz_fixed = False
    K = None
    learn_tried = False
    learn = color in LEARN_MOVE
    for it in range(MAX_ITER):
        C, per, why = check(color, srcs, roles)
        for D in per.values():
            print(f"  호버정렬 {it}: {fmt(D)}", flush=True)
        if C is None and (not rz_fixed) and ("불일치" in (why or "")) and not dry:
            # ★9/11 사용자 지시("멈추지 말고 자동으로 맞춰라"): 두 캠 불일치가 rz 차이에서 오는 경우가 실측됨
            #   (노랑: rz 기준 +91.01 / 지금 +91.93 → 새카메라가 TCP 축에서 떨어져 호를 그려 2.6mm 편차, rz 되돌리자 1.97→0.99mm).
            #   점 1개 카메라는 rz 를 못 재므로, 기준 rz 와 0.3° 넘게 다르면 한 번 기준 rz 로 돌리고 재측정한다(정렬 루프의 rz 스텝과 같은 동작).
            try:
                refs = json.load(open(REF)).get(color) or {}
                rz_ref = next((v["tcp"][5] for v in refs.values() if isinstance(v, dict) and v.get("tcp")), None)
                rz_now = st()["tcp"][5]
                d_rz = HG.wrap_deg(rz_ref - rz_now) if rz_ref is not None else 0.0
            except Exception as e:
                rz_ref, d_rz = None, 0.0
            rz_fixed = True
            if rz_ref is not None and 0.3 < abs(d_rz) <= 3.0:
                print(f"  ⚠ 두 캠 불일치 + rz 가 기준과 {d_rz:+.2f}° 다름 → 기준 rz {rz_ref:+.2f} 로 돌리고 재측정", flush=True)
                move_rel(0.0, 0.0, d_rz); time.sleep(0.8)
                continue
        if C is None:
            raise RuntimeError("호버 정렬 측정 실패: " + why)
        e_mm = math.hypot(*C["dmm"]); e_deg = abs(C["drz"])
        print(f"  호버정렬 {it}: 결합 XY ({C['dmm'][0]:+.2f},{C['dmm'][1]:+.2f}) rz {C['drz']:+.2f}° [{C['n_src']}캠, rz {C['rz_from']}]", flush=True)
        if e_mm <= tol_mm and e_deg <= tol_deg:
            print(f"  ✅ 호버 정렬 수렴 ({e_mm:.2f}mm, {e_deg:.2f}°)"); return True
        # ★9/7 18:3x 실측: 명령 (-0.75,+0.82)mm 에 실제 이동이 (+0.30,+1.30)mm — x 는 방향까지 반대였다.
        #   로봇의 최소 실행 이동량(≈0.8mm)보다 작은 명령은 제대로 실행되지 않아 잔차를 만들고,
        #   그 잔차가 다음 측정을 키워 '발산'으로 보인다. **실행할 수 없는 크기는 명령하지 않는다.**
        #   (게이트 완화가 아니다 — 정렬 결과는 그대로 정렬 온전성 게이트가 다시 검사한다.)
        if K is None and e_mm < MIN_EXEC_MM and e_deg <= tol_deg:
            print(f"  ✅ 호버 정렬 한계 수렴 ({e_mm:.2f}mm < 최소 실행 이동 {MIN_EXEC_MM}mm — 더 줄일 수 없음)")
            return True
        # 15:44 실기: 18.6→14.2mm 로 줄고 있는데 rz 0.05→0.17°(손목캠 잡음) 로 '발산' 오판 → 각은 0.3° 이하 변동은 무시
        if prev is not None and (e_mm > prev[0] * 1.2 + 0.2 or (e_deg > 0.3 and e_deg > prev[1] * 1.2 + 0.1)):
            raise RuntimeError(f"호버 정렬 발산({prev[0]:.2f}→{e_mm:.2f}mm, {prev[1]:.2f}→{e_deg:.2f}°) — 부호/매핑 의심, 정지")
        prev = (e_mm, e_deg)
        # ★9/11 사용자 지시("자 말고 이동으로 고치자"): 내벽은 시험 이동 2번으로 **명령 mm → 실제 줄어든 mm**(K)를 그 자리에서 배운다.
        #   K=I 면 자가 맞는 것. 9/11 실측에서는 자가 1.49배 작아 K≈0.67·I 였고, 파지 미끄러짐도 K 에 함께 들어온다.
        #   배운 K 가 이상하면(특이값 범위 밖) 쓰지 않고 종전 경로 그대로 — 새 정지 조건을 만들지 않는다.
        if learn and K is None and not learn_tried and not dry:
            learn_tried = True
            K, e_new = _learn_move_K(color, srcs, roles, np.array(C["dmm"], float), tol_deg)
            if isinstance(K, str):      # "BAD" — 배운 매핑이 깨졌다
                K = None
                raise RuntimeError("내벽 정렬: 시험 이동으로 잰 매핑이 깨졌다(점 검출 불안정/파지 미끄러짐 의심) — 재파지 권함")
            if e_new is not None:
                C = dict(C); C["dmm"] = (float(e_new[0]), float(e_new[1]))
                e_mm = math.hypot(*C["dmm"]); prev = (e_mm, e_deg)
                if e_mm <= tol_mm and e_deg <= tol_deg:
                    print(f"  ✅ 호버 정렬 수렴 ({e_mm:.2f}mm, {e_deg:.2f}°) — 시험 이동 중 도달"); return True
        want = np.array(C["dmm"], float)
        if K is not None:
            try:
                want = np.linalg.solve(K, want)
                print(f"    (배운 K 적용: 계산 {C['dmm'][0]:+.2f},{C['dmm'][1]:+.2f} → 명령 {want[0]:+.2f},{want[1]:+.2f}mm)", flush=True)
            except Exception:
                want = np.array(C["dmm"], float)
        # ★배운 K 가 있으면 '실행 가능한가' 는 계산값이 아니라 **명령값**으로 판단해야 한다.
        #   (자가 1.49배 작으면 계산 0.54mm 가 실제로는 명령 1.0mm — 충분히 실행된다)
        if K is not None and float(np.hypot(*want)) < MIN_EXEC_MM and e_deg <= tol_deg:
            print(f"  ✅ 호버 정렬 한계 수렴 (명령 {float(np.hypot(*want)):.2f}mm < 최소 실행 이동 {MIN_EXEC_MM}mm — 더 줄일 수 없음)")
            return True
        if float(np.hypot(*want)) > SANE_MAX_MM:
            raise RuntimeError(f"호버 정렬 명령 {float(np.hypot(*want)):.1f}mm > {SANE_MAX_MM}mm — 매핑/오매칭 의심, 정지")
        dx, dy = (max(-MAX_STEP_MM, min(MAX_STEP_MM, float(v))) for v in want)
        drz = max(-MAX_STEP_DEG, min(MAX_STEP_DEG, C["drz"])) if e_deg > tol_deg else 0.0
        if dry:
            print(f"    (dry) 이동 ({dx:+.2f},{dy:+.2f}) rz {drz:+.2f}"); return False
        move_rel(dx, dy, drz); time.sleep(0.6)
    raise RuntimeError(f"호버 정렬 {MAX_ITER}회 내 미수렴")


def probe_fixed(src, color, seeds, probe_mm=10.0):
    """고정카메라(newcam/side) 매핑: 벽 든 채 z440 에서 X·Y ±probe 조그 → 벽 점 이동(px)/mm. rot_sign 은 +2° 조그로 실측."""
    def wall_mid():
        pts = []
        for _ in range(3):
            w = wall_dots(grab(src), color, None, seeds, src)
            if len(w) != len(seeds):
                raise RuntimeError(f"{src} 벽 점 {len(w)}개(기대 {len(seeds)})")
            pts.append(np.array([p[:2] for p in sorted(w, key=lambda q: (q[1], q[0]))], float)); time.sleep(0.2)
        return np.mean(pts, axis=0)
    c0 = st()["tcp"]; P0 = wall_mid(); J = np.zeros((2, 2))
    for k, ax in enumerate(("X", "Y")):
        d = [0.0, 0.0]; d[k] = probe_mm
        move_rel(d[0], d[1], 0); time.sleep(0.5); Pp = wall_mid().mean(0)
        move_rel(-2 * d[0], -2 * d[1], 0); time.sleep(0.5); Pm = wall_mid().mean(0)
        move_rel(d[0], d[1], 0); time.sleep(0.4)
        J[:, k] = (Pp - Pm) / (2 * probe_mm)
        print(f"  {ax} ±{probe_mm}: 벽 점 이동 {(Pp - Pm)}px → J 열 {J[:, k]}")
    rot_sign = -1.0
    if len(seeds) >= 2:
        _, a0 = wall_mid_ang(P0); move_rel(0, 0, 2.0); time.sleep(0.5); _, a1 = wall_mid_ang(wall_mid()); move_rel(0, 0, -2.0)
        rot_sign = 1.0 if HG.wrap_deg(a1 - a0) > 0 else -1.0
        print(f"  rz +2° → 화면 각 {HG.wrap_deg(a1 - a0):+.2f}° → rot_sign {rot_sign:+.0f}")
    Jinv = np.linalg.inv(J)
    json.dump({"made": time.strftime("%Y-%m-%d %H:%M"), "tcp": c0, "z": c0[2], "J_px_per_mm": J.tolist(),
               "Jinv_mm_per_px": Jinv.tolist(), "rot_sign": rot_sign, "color": color, "seeds": seeds,
               "scale_mm_per_px": float(1.0 / math.sqrt(abs(np.linalg.det(J)))), "src": src}, open(SRC[src]["map"], "w"), indent=1)
    print(f"✅ {src} 매핑 저장 {SRC[src]['map']}: 축척 {1.0 / math.sqrt(abs(np.linalg.det(J))):.4f}mm/px, 직교 {math.degrees(math.atan2(J[1,0],J[0,0]))-math.degrees(math.atan2(J[1,1],J[0,1]))+90:+.1f}°")


def probe_arm(src, color, probe_mm=10.0):
    """손목 부착(moving="base") 카메라 매핑: X·Y ±probe 조그 → **베이스 특징(기둥 점)** 이동 px/mm, rz +2° → rot_sign.
    (probe_fixed 는 벽 점을 추적하므로 손목 부착 카메라에선 0px → 9/6 13:01 새카메라 184mm/px 오류의 원인)"""
    search = DET[src]["search"]
    def feats():
        img = grab(src); w = wall_dots(img, color, None, None, src)
        f = base_feats(img, w, src)
        if len(f) < 2:
            raise RuntimeError(f"{src} 베이스 특징 {len(f)}개(<2) — 후보 {[(c, round(x), round(y), int(a)) for c, x, y, a in f]}")
        return f
    F0 = feats(); c0 = st()["tcp"]; J = np.zeros((2, 2))
    def pairs(Fn):
        """F0↔Fn 매칭 후, 카메라에 붙어 같이 움직이는 고정물(케이블 등 — 이동이 거의 0)은 제외: 이동량 < 최대의 30%."""
        s_, d_, lab = match_feats(F0, Fn, search)
        if len(s_) < 1:
            raise RuntimeError(f"{src} 특징 매칭 0개")
        v = np.array(d_, float) - np.array(s_, float); mag = np.hypot(v[:, 0], v[:, 1]); keep = mag >= 0.3 * mag.max()
        if keep.sum() < len(s_):
            print(f"    고정물 제외 {[l for l, k in zip(lab, keep) if not k]}")
        return [p for p, k in zip(s_, keep) if k], [p for p, k in zip(d_, keep) if k]
    def shift(Fn):
        a, b = pairs(Fn)
        return np.mean(np.array(b, float) - np.array(a, float), axis=0), len(a)
    for k, ax in enumerate(("X", "Y")):
        d = [0.0, 0.0]; d[k] = probe_mm
        move_rel(d[0], d[1], 0); time.sleep(0.5); Pp, n1 = shift(feats())
        move_rel(-2 * d[0], -2 * d[1], 0); time.sleep(0.5); Pm, n2 = shift(feats())
        move_rel(d[0], d[1], 0); time.sleep(0.4)
        J[:, k] = (Pp - Pm) / (2 * probe_mm)
        print(f"  {ax} ±{probe_mm}: 베이스 특징 이동 {(Pp - Pm)}px (매칭 {n1}/{n2}) → J 열 {J[:, k]}")
    move_rel(0, 0, 2.0); time.sleep(0.5)
    a, b = pairs(feats())
    if len(a) < 2:
        raise RuntimeError(f"{src} rz 프로브: 유효 특징 {len(a)}개(<2)")
    th = similarity(a, b)[1]
    move_rel(0, 0, -2.0); time.sleep(0.3)
    rot_sign = 1.0 if th > 0 else -1.0          # delta(): drz = rot_sign·(−θ) 가 +2° 를 되돌려야 → rot_sign = sign(θ)
    print(f"  rz +2° → 특징 회전 θ {th:+.2f}° → rot_sign {rot_sign:+.0f} (손목캠 ROT_SIGN {ROT_SIGN['wrist']:+.0f} 와 같아야 정상)")
    if abs(abs(th) - 2.0) > 0.8:
        print(f"  ⚠ 특징 회전 {th:+.2f}° 가 2° 와 다름 — 매칭/검출 점검")
    Jinv = np.linalg.inv(J)
    json.dump({"made": time.strftime("%Y-%m-%d %H:%M"), "tcp": c0, "z": c0[2], "J_px_per_mm": J.tolist(),
               "Jinv_mm_per_px": Jinv.tolist(), "rot_sign": rot_sign, "color": color, "moving": "base", "theta_probe": th,
               "scale_mm_per_px": float(1.0 / math.sqrt(abs(np.linalg.det(J)))), "src": src}, open(SRC[src]["map"], "w"), indent=1)
    print(f"✅ {src} 매핑 저장(손목 부착) {SRC[src]['map']}: 축척 {1.0 / math.sqrt(abs(np.linalg.det(J))):.4f}mm/px, 직교 {math.degrees(math.atan2(J[1,0],J[0,0]))-math.degrees(math.atan2(J[1,1],J[0,1]))+90:+.1f}°")


def _seeds(argv):
    if "--wall" not in argv:
        return None
    v = [float(t) for t in argv[argv.index("--wall") + 1].split(",")]
    return [(v[i], v[i + 1]) for i in range(0, len(v), 2)]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    color = sys.argv[2] if len(sys.argv) > 2 else "blue"
    src = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else "wrist"
    if mode == "ref":
        save_ref(color, src, _seeds(sys.argv))
    elif mode == "check":
        check.expo_done = False
        check.match_expo_done = False
        C, per, why = check(color)
        for D in per.values(): print(fmt(D))
        print(f"결합 XY ({C['dmm'][0]:+.2f},{C['dmm'][1]:+.2f}) rz {C['drz']:+.2f}° [{C['n_src']}캠]" if C else "❌ " + why)
    elif mode == "align":
        align(color, dry="--dry" in sys.argv)
    elif mode == "detect":
        img = grab(src); w = wall_dots(img, color, None, _seeds(sys.argv), src); f = base_feats(img, w, src)
        print(f"[{src}] 벽 점 {[(round(x), round(y), a) for x, y, a in w]}  베이스 특징 {[(c, round(x), round(y), a) for c, x, y, a in f]}")
    elif mode == "probe":       # hover_align.py probe <newcam|side> <색> --wall x,y[,x,y]
        seeds = _seeds(sys.argv); psrc = color; pcolor = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else "blue"
        if psrc not in SRC or psrc == "wrist": print("probe 대상은 newcam 또는 side"); return
        if SRC[psrc]["moving"] == "base":
            probe_arm(psrc, pcolor); return
        if not seeds: print("--wall x,y[,x,y] 로 그 카메라 화면의 든 벽 점 좌표를 주세요"); return
        probe_fixed(psrc, pcolor, seeds)
    elif mode == "test":
        ref_img = cv2.imread(sys.argv[3]); now_img = cv2.imread(sys.argv[4])
        z = float(sys.argv[5]) if len(sys.argv) > 5 and not sys.argv[5].startswith("--") else 440.0
        rz = float(sys.argv[sys.argv.index("--rz") + 1]) if "--rz" in sys.argv else RZ_MAP
        m0, why = measure(ref_img, color, None, _seeds(sys.argv), src)
        if not m0: print("❌ 기준 프레임:", why); return
        print(f"기준: 벽 {[(round(x), round(y), a) for x, y, a in m0['wall']]}  특징 {[(c, round(x), round(y), a) for c, x, y, a in m0['pillars']]}")
        ref = {"wall": m0["wall"], "pillars": m0["pillars"], "z": z}
        m1, why = measure(now_img, color, ref, None, src)
        if not m1: print("❌ 지금 프레임:", why); return
        print(f"지금: 벽 {[(round(x), round(y), a) for x, y, a in m1['wall']]}  특징 {[(c, round(x), round(y), a) for c, x, y, a in m1['pillars']]}")
        D, why = delta(ref, m1, z, rz, src, tcp_now=st()["tcp"])
        print(fmt(D) if D else "❌ " + why)
        if D:
            for l in D["matched"]: print("   ", l)
        if "--save" in sys.argv and D:
            out = now_img.copy()
            for c, x, y, a in m1["pillars"]: cv2.circle(out, (int(x), int(y)), 14, (0, 255, 0), 2)
            for x, y, a in m1["wall"]: cv2.circle(out, (int(x), int(y)), 18, (255, 255, 255), 2)
            s_, d_, _ = match_feats(ref["pillars"], m1["pillars"])
            for p in apply_sim(similarity(s_, d_), ref["wall"]): cv2.drawMarker(out, (int(p[0]), int(p[1])), (0, 0, 255), cv2.MARKER_CROSS, 30, 2)
            cv2.imwrite(sys.argv[sys.argv.index("--save") + 1], out)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
