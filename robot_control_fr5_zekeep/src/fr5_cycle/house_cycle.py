#!/usr/bin/env python3
"""house_cycle.py — 조립식 주택 벽 삽입 사이클 (9/6 재설계, 사용자 설계 4단계 + 버튼)  :8776

설계(사용자, 9/6 11:2x):
  1. base above(관측자세 z650, 빈 손): ArUco(고정 자) 대비 기둥 4점이 얼마나 움직였나 → 곧 꽂을 벽의 자리(x, y, yaw).
  2. rack above: 그 벽의 양끝점 → 중앙에 그리퍼(벌림 30) → 벽별 보정(사용자 버튼으로 기억) → z 하강 → 파지
     → 든 벽의 색점 기록(좋은 파지 서명과 비교해 편차를 3단계에 반영).
  2'. 1 의 자리로 운반 → z_seat+85(=z440) → 기둥점↔벽점 관계를 '잘 들어갔을 때' 기준과 비교, 다르면 맞춘다.
  3. 사용자 허락 → 버튼으로 수직 하강(막힘 감시). x·y·yaw 는 그 전에 다 맞아 있어야 한다.

원칙: 상태 파일은 state/house/ 에 새로(옛 golden/anchor/ref 안 읽음). 검출기·하강감시·호버정렬은 어제 실기 검증된 부품을 그대로 쓴다.
      어떤 예외든 stop 이 먼저. 기둥 꼭대기 아래(z_seat+85 미만)에서 XY·rz 이동 금지. 자동 재시도 없음(정지·보고, 사용자 판단).
      랙 위 벽은 항상 같은 방향으로 놓는다(사용자 결정) → 벽별 보정 부호가 유지된다.

버튼(벽별): 슬롯 기준 저장(1/2 안착 TCP, 2/2 관측 페어링) · 랙 보정 저장 · 좋은 파지 서명 저장 · z440 기준 저장 · 고정캠 매핑(newcam/side)
동작:  ▶ 사이클(1→2→2' 자동, 필요한 기준이 없으면 그 자리에서 멈추고 버튼을 기다림) · ⬇ 하강 · ⛔ 중단 · 관측자세로

9/6 보강 5건:
  ① 하강 후 안착 판정이 seated 가 아니면 **그리퍼를 열지 않고 그 자리에서 정지**(SEAT FAIL) → [⛔중단](그대로 정지, 벽은 사용자가)
     또는 [▶계속](사용자가 육안으로 안착 확인 → 개방·상승, 기준 승격은 없음). seated 일 때만 promote_ref·슬롯 기준 승격·개방·상승.
  ② 하강 게이트: 정렬 완료 TCP ↔ 하강 직전 TCP 가 XY 0.5mm / rz 0.3° 안 + z ±3 + 정렬 후 15분 이내(넘으면 재정렬 요구) + 색 일치.
  ③ 안착 성공 시 슬롯 기준 자동 승격(안착 TCP x·y·rz + 이번 사이클 베이스; z 는 티칭값 유지) — 이전 값은 history(최대 5) 보존.
  ④ run4(op=run4 / --auto): blue→yellow→red→red_s 연속, 벽마다 빈손 베이스 재측정, 하강은 매번 [⬇ 하강] 버튼에서 대기. 하나라도 정지·실패면 전체 중단.
  ⑤ slot_both: 슬롯 기준 1/2(벽 물고 TCP) → "벽 놓고 그리퍼 빼기" 대기(▶계속) → 2/2 를 한 버튼으로. 빈손(그리퍼 ≤ 닫힘값 또는 벽 점 0)이면 1/2 거부.

  python3 house_cycle.py            # 서버 :8776  (http://<ip>:8776/)
  python3 house_cycle.py --auto     # 서버 기동 + run4 를 바로 대기열에 (각 벽 하강은 버튼)
"""
import sys, os, json, math, time, threading, queue, traceback
import urllib.request as UR
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import numpy as np

sys.path.insert(0, "/home/ar/bf2_console/tools")
import place_calc as PC          # 부품: find_base_4pts / rack_ends / _grip_ok / grasp_measure / save_grasp_sig_now / descend_monitored / held_wall_dots
import hover_align as HA         # 부품: align / save_ref / promote_ref / probe_fixed / wall_dots / grab
import house_geometry as HG
import slot_target as STG        # 부품: pillars_px / base_pose_robot / load_map
import base_twist as BT          # 부품: markers / board_frame_transform / apply_sim  (ArUco 고정 자)
import base_depth_corner as BDC  # 부품: depth_mask / detect_rect (INNER_WALL_MODE = 내벽 전용 대응 스위치)
import side_seat as SSEAT       # 부품: 측면캠(:8771) 안착 판정 — 고정캠이라 로봇 자세와 무관(red_s 용)
import fork_carry as FC          # 부품: 출하 리프트(포크 손잡이째 집을 출하지로) — 9/9 사용자 "리프트도 하우스 사이클에 넣어줘"

# ★9/9 A타입 착수 준비: 기준 파일이 색 이름(blue/yellow/red/red_s/red_in)으로만 구분돼 있어서
#   A타입에도 파랑 벽이 있으면 티칭하는 순간 B타입 파랑 기준을 덮어쓴다(어제 반나절 작업이 날아간다).
#   → 집 타입별로 디렉터리를 나누고 state/house 는 **활성 타입을 가리키는 심볼릭 링크**로 둔다.
#     ref_view·fork_carry·side_seat·hover_view 가 state/house 를 하드코딩하고 있어서,
#     링크 방식이면 그 도구들을 하나도 안 고치고 전환된다.
#   ⚠ 링크는 눈에 안 보인다 = 어느 타입인지 헷갈리면 그게 곧 사고다.
#     그래서 기동 로그·상태·UI 에 활성 타입을 항상 띄운다.
STATE = os.environ.get("HOUSE_STATE", "/home/ar/bf2_console/state/house")
STATE_ROOT = os.path.dirname(os.path.abspath(STATE))
HOUSE_TYPES = ("b", "a")


def house_type():
    """지금 활성인 집 타입. state/house 링크가 가리키는 곳으로 판단한다."""
    try:
        tgt = os.path.basename(os.path.realpath(STATE))       # house_b / house_a
        if tgt.startswith("house_"):
            return tgt[len("house_"):]
    except Exception:
        pass
    return "?"


def switch_house(t):
    """활성 집 타입 전환. 기준이 섞이지 않게 링크만 갈아 끼운다(파일은 안 옮긴다)."""
    t = str(t).strip().lower()
    if t not in HOUSE_TYPES:
        raise Gate(f"모르는 집 타입 {t!r} — {HOUSE_TYPES} 중 하나")
    with LOCK:
        if S.get("busy"):
            raise Gate("작업 중에는 집 타입을 바꾸지 않는다")
    dst = os.path.join(STATE_ROOT, f"house_{t}")
    if not os.path.isdir(dst):
        raise Gate(f"{dst} 없음 — 먼저 만들 것")
    link = os.path.join(STATE_ROOT, "house")
    if not os.path.islink(link):
        raise Gate(f"{link} 가 심볼릭 링크가 아니다 — 수동 확인 필요(실수로 덮어쓸 위험)")
    tmp = link + ".swap"
    os.symlink(f"house_{t}", tmp)
    os.replace(tmp, link)                                     # 원자적 교체
    log(f"══ 집 타입 전환: {house_type().upper()} (기준 {os.path.realpath(STATE)})")
    return house_type()
os.makedirs(os.path.join(STATE, "logs"), exist_ok=True)
F = {k: os.path.join(STATE, v) for k, v in {
    "slot": "slot_ref.json",        # 색별 {seat_tcp, base(x,y,yaw 로컬), made}
    "rack": "rack_ref.json",        # 색별 {Pc0, Tg0, ang0, len0_px, z_pick, grip_open, grip_close, offset{along,across}}
    "rack_map": "rack_map.json",    # 랙 관측자세 화면→로봇 Jinv(mm/px)
    "rack_pose": "rack_pose.json",  # 랙 관측자세 TCP
    "sig": "grasp_sig.json",        # 좋은 파지 서명(place_calc by_color 스키마)
    "hover": "hover_ref.json",      # z440 기준 관계(hover_align 스키마)
    "aruco": "aruco_ref.json",      # 관측자세 ArUco 기준 코너
    "base_last": "base_last.json",
}.items()}
PC.GRASP_REF = F["sig"]            # ★부품이 새 상태 파일을 보게 전환(옛 grasp_ref_0905/hover_ref_0905 안 읽음)
HA.REF = F["hover"]

PORT = 8776
OBS = PC.OBS                                    # [200,-330,650,180,0,180]
SAFE_Z = PC.SAFE_Z                              # 650
HOVER_Z = PC.HOVER_Z                            # 478
COLORS = ("blue", "yellow", "red", "red_s", "red_in", "blue_in", "yellow_in")
# ★9/10 A타입 내벽 = 파랑·노랑 두 장(사용자). B타입 내벽 red_in 과 같은 "내벽 전용 경로"(INNER_WALL_MODE·랙 벽점 검사 시점)를 쓴다.
INNER_WALLS = {"red_in", "blue_in", "yellow_in"}   # ★9/7 red_in = 내벽(흰 바탕 빨간 점 3개, 새 랙)
GRIP_OPEN = 30                                  # 사용자 설계: 벌림 30 으로 내려온다
RACK_RZ_FOLLOW = False                          # 랙 위 벽 각을 rz 로 따라갈지(부호 미검증 → 기본 끔, 각은 보고만)
# ★9/8 책상이 2mm 올라간 상태. 안착 z 를 바꾸면 정렬 높이(안착+HOVER_DZ)까지 따라 움직여
#   z440 기준과 어긋나 정렬이 아예 못 돈다 → **하강 정지 높이만** 따로 둔다.
#   None 이면 평소대로 슬롯 기준의 안착 z 까지 내려간다. 값을 주면 그 높이에서 멈춘다(더 깊이 안 감).
# ★9/10: A타입 내벽(blue_in·yellow_in)은 **넣지 않는다** — 무감시 하강은 9/2 기둥 파손과 같은 조건이고,
#   자리도 아직 티칭 전이다. red_in 의 무감시는 9/8 WB5500 때 결정된 것이라 그것도 재확인 대상(메모리 🔴).
# ★9/10 사용자 지시(A): yellow_in 은 벽이 짧아 **든 벽 점이 어느 자세에서도 안 보인다** → 밀림을 볼 수단이 없다.
#   감시가 원리적으로 불가능하므로 무감시로 내린다. 밑판 접촉 위험은 사용자 확인에 의존.
#   (blue_in 은 든 벽 점 2개가 보이므로 감시 그대로 켜 둔다)
JAM_FREE = {"red_in", "yellow_in"}      # ★막힘 감시 없이 내리는 색(사용자 비접촉 확인). 외벽 4색은 절대 넣지 말 것
DESCEND_STOP_Z = None      # 9/8 14:3x 사용자 확정 안착 z(블루 356) 반영 → 정지 높이 해제
# ★9/8 17:1x 사용자 지시("하강을 내가 누르니까 목표 z 가면 완료 판정 내고 올라가 — 그래야 풀사이클을 한 번에"):
#   하강 버튼을 누른 것 자체가 사람의 확인이다. 목표 z 까지 **막힘 없이** 내려갔으면 완료로 보고 그대로 개방·상승한다.
#   단 카메라가 적극적으로 "잘못 앉았다(not_seated)"고 판정하면 그건 그대로 멈춘다 — unknown(판정 불가)만 통과.
#   기준 승격은 하지 않는다(카메라로 확인된 게 아니므로 오늘 잡은 기준을 덮어쓰지 않는다).
# ★안착 판정 불가(unknown)를 자동 완료로 넘길지 — 재기동 없이 바꾸도록 파일에 둔다(9/9).
#   True(본선): 목표 z 까지 막힘 없이 갔으면 그대로 개방·상승. 벽마다 [계속] 을 누를 필요가 없다.
#   False(티칭): 그리퍼를 문 채 SEAT FAIL 로 선다 — 그 자세에서만 [슬롯 기준 1/2] 을 찍을 수 있다.
#   not_seated(카메라가 적극적으로 잘못 앉았다고 함)는 어느 쪽이든 정지. 기준 승격도 하지 않는다.
SEAT_AUTO_F = os.path.join(STATE, "seat_auto.json")


def seat_unknown_auto():
    try:
        return bool(json.load(open(SEAT_AUTO_F)).get("auto", True))
    except Exception:
        return True                      # 파일이 없으면 본선 동작(True)


def set_seat_auto(v):
    on = str(v).lower() in ("1", "true", "on", "y", "yes")
    jsave(SEAT_AUTO_F, {"auto": on, "made": time.strftime("%Y-%m-%d %H:%M")})
    log(f"══ 안착 판정 불가 자동완료: {'ON(본선 — [계속] 불필요)' if on else 'OFF(티칭 — 벽 문 채 정지)'}")
    return on
HOVER_DZ = {"red_in": 102.0, "blue_in": 100.0, "yellow_in": 100.0}   # 안착 z 위로 얼마에서 정렬하나(기본 85.0). red_in=338+102=440 = 기준을 찍은 높이
RACK_ANG_MAX = 3.0                              # 랙 위 벽 각 변화 상한(넘으면 벽이 삐뚤게 놓인 것 → 정지)
ARUCO_WARN_MM, ARUCO_WARN_DEG = 1.5, 0.3        # 고정 자 대비 카메라 복귀 오차 경고
GRASP_GATE_MM, GRASP_GATE_DEG = PC.GRASP_GATE_MM, 0.7   # 1.0 / 0.7 — 9/6 14시: 파랑 파지 각 +0.43~0.54° 가 4회 연속이고 4회 모두 삽입 성공 → 정상 파지 범위. 0.3 은 서명 촬영 각 편차였음
GRASP_GATE_BLOCKING = False   # ★9/10 사용자 지시: 파지 편차 초과 시 멈추지 않고 기록만(누를 버튼은 하강 하나)
PRECORR_MAX_MM = 0.0   # 9/13 18:3x 사용자 판단: 선보정 끄고 시험(원래 1.0) — 정렬이 흡수                            # 파지 편차 선보정 상한(부호 미검증 — 호버 정렬이 나머지를 흡수)
PRECORR_RZ = False                              # 13:36·14:01 실기 2회: rz 선보정 −0.43° 를 사용자가 매번 정확히 되돌림(rz 180) → 끔
SEAT_NEWCAM_TOL_PX = 12                         # 새카메라 안착 판정: 기둥 점이 안착 기준 자리에서 이 px 안이면 seated(≈2.5mm)
SPD_MOVE, SPD_DESC, SPD_SEAT = PC.SPD_MOVE, PC.SPD_DESC, PC.SPD_SEAT   # 30 / 10 / 3
ALIGN_GATE_MM, ALIGN_GATE_DEG = 0.5, 0.3        # ②하강 직전 TCP 가 정렬 완료 TCP 와 이만큼 안이어야(XY 거리 / rz)
MIN_EXEC_MM = 0.8                               # 로봇이 실제로 실행하는 최소 이동량(9/1 실증 0.6~1.15) — 그 미만은 1mm 되돌기로
ALIGN_MAX_AGE_S = 15 * 60                       # ②정렬 완료 후 이 시간이 지나면 하강 거부(베이스가 움직였을 수 있음 → 재정렬)
SLOT_HISTORY_MAX = 5                            # ③슬롯 기준 승격 시 보존하는 이전 값 개수
# ★9/10: A타입 6벽 연속 — 외벽 4 → 내벽 2(파랑 먼저). 외벽→내벽 순서는 필수(내벽이 밑판 깊이 검출을 가름).
# ★9/11: 집 타입마다 벽 구성이 다르다. A = 외벽4+내벽2(파랑/노랑), B = 외벽4+내벽1(red_in).
_RUN_ORDER_A = ("blue", "yellow", "red", "red_s", "blue_in", "yellow_in")
_RUN_ORDER_B = ("blue", "yellow", "red", "red_s", "red_in")
def run_order():
    return _RUN_ORDER_B if house_type() == "b" else _RUN_ORDER_A
# ★9/11 사용자 지시: 외벽 사이클 / 내벽 사이클 분리 — 외벽 4색은 A·B 공통, 내벽만 집 타입별.
OUTER_ORDER = ("blue", "yellow", "red", "red_s")
def outer_order():
    return OUTER_ORDER
def inner_order():
    return tuple(c for c in run_order() if c in INNER_WALLS)
RUN4_ORDER = _RUN_ORDER_A   # 하위호환(직접 참조하는 옛 코드용) — 실제 사용은 run_order()
# 13:53 실기 2회: 손목캠↔새카메라 불일치 2.1mm 반복. 사용자 육안 자리와 비교하면 새카메라가 두 번 다 가까웠음(rz 특히).
#   손목캠은 든 벽 윗점이 프레임 가장자리(x≈1025)라 원근·죠 안 기울기에 민감 → 파랑은 새카메라 단독 정렬(손목캠은 참고 출력).
ALIGN_SRCS = {"blue": ("newcam",)}                # 색별 정렬 카메라(없으면 가용 전부)
# 14:33 실기: 랙 비틀림(죠 안 +0.99°)을 새카메라 1점이 못 봐 사용자 2.35mm/0.32° 조그. 손목캠 벽 2점이 죠 안 회전을 보지만
#   손목캠 기준(13:47)이 삽입 후 밀린 벽 각으로 찍혀 rz +0.5° 편향 → 우선 "measure"(측정·성공 시 승격만) 로 한 사이클 재기준 후 "rz" 로 승격.
RZ_MEASURE_WARN = 0.6                            # 정렬 중 손목캠이 재는 벽 회전이 이만큼 넘으면 경고(죠 안에서 벽이 돌아감 = 재파지 신호. rz 를 억지로 돌려 맞추지 않는다)
ALIGN_MAX_MOVE_TWOCAM_MM = 15.0  # ★9/11: 두 캠 xy 색 상한(불일치 게이트가 파지 불량 담당)
ALIGN_MAX_MOVE_MM = 4.0    # ★9/7: 정렬이 슬롯 기준에서 이만큼 넘게 옮기면 정지 — 든 벽 점(가까움)과 기둥(멀리)의 시차로 파지 오차가 2배 증폭되는 구조라, 큰 이동은 신뢰할 수 없다(재파지가 답)
# ★9/10 사용자 지시: 파랑도 두 캠 조종으로. 새카메라 단독이던 19:07 에 혼자 4.4mm 를 끌고 가
#   (wrist +3.28 vs newcam +5.07mm 로 2mm 갈렸는데 막을 게 없었다) 기둥을 시야 밖으로 밀어냈다.
#   둘 다 xy 면 COMBINE_TOL_MM(1.5mm) 불일치 게이트가 걸린다 — red_s 에서 실제로 잘못된 이동을 막았다.
# ★9/10: 집 타입마다 카메라 사정이 다르다(밑판 자리·기둥 가림). 오늘 A타입에 맞춰 바꾼 값을
#   B타입에 그대로 물리면 검증 안 된 설정으로 도는 셈이라 타입별로 나눈다.
# ★9/11 A yellow·red_s 두 캠 xy(사용자 지시, B 와 동일) — 옆면 점 위험은 두 캠 불일치 1.5mm 게이트가 담당; 손목 단독은 오늘 1.3mm 오정렬을 못 잡았다
_ROLES_A = {"blue":   {"newcam": "xy", "wrist": "xy"},     # 9/10 저녁: 각 캠 기둥 1점 지정 후 두 캠 조종(검증 0.030mm)
            "yellow": {"wrist": "xy", "newcam": "xy"},# 벽 옆면 빨간 점 = 파지 이상 감지기
            "red":    {"wrist": "xy", "newcam": "xy"},     # 9/10 저녁: 두 캠 조종(검증 0.043mm)
            "red_s":  {"wrist": "xy", "newcam": "xy"},# 옆면 노란 점은 높이↔가로 혼동이 있어 조종 금지
            "red_in": {"wrist": "xy"},
            "blue_in": {"wrist": "xy"},                    # 새카메라 안 씀(사용자). 밑판 노랑 3점 + 든 벽 2점
            "yellow_in": {"wrist": "xy"}}                  # 밑판 기준 모드(든 벽 점 없음)
# ★9/10 밤 사용자 지시 "A 성공한 방식으로 바꾸자": B타입도 두 캠 조종으로.
#   A타입에서 통한 조합 = **각 카메라에 진짜 밑판 기둥 1점 지정 + 두 캠이 서로 검증**.
#   한쪽만 조종하면 그 기준이 낡아도 막을 게 없다(21:04 B파랑: 새카메라 단독으로 6mm 끌고 가 기둥에 박힘).
#   기준이 둘 다 있는 색만 두 캠으로 두고, 새카메라 기준이 없는 색은 손목캠 단독 유지.
_ROLES_B = {"blue":   {"wrist": "both", "newcam": "measure"},   # ★9/13 19:1x 사용자 결정: XY·rz 조종 = 손목캠 단독, 새카메라는 참고
            "yellow": {"wrist": "both", "newcam": "measure"},   #   (실측: 밑판 항은 두 캠 0.39mm 일치, 벽 항만 갈림 → 불일치는 정지 사유 아님)
            "red":    {"wrist": "both", "newcam": "measure"},   #   rz 고정 모드 해제: 손목캠 벽 점 2개면 rz 도 풂(1개면 각 0 이라 회전 없음)
            "red_s":  {"wrist": "both", "newcam": "measure"},
            "red_in": {"wrist": "both", "newcam": "measure"}}    # 9/13 16:58 새카메라 기준 추가(파랑 기둥 938,570 + 빨간 벽점 885,586, 검증 0.08mm)


class _RolesByHouse(dict):
    """ALIGN_ROLES.get(color) 를 그대로 쓰되, 지금 집 타입에 맞는 표를 본다."""
    def _t(self):
        return _ROLES_B if house_type() == "b" else _ROLES_A
    def get(self, k, d=None):   return self._t().get(k, d)
    def __getitem__(self, k):   return self._t()[k]
    def __contains__(self, k):  return k in self._t()
    def keys(self):             return self._t().keys()
    def items(self):            return self._t().items()


ALIGN_ROLES = _RolesByHouse()

# 파지 편차 게이트도 타입별 — A는 사용자 지시로 비차단, B는 검증된 예전 동작(정지) 유지
def apply_speed_profile():
    """★9/11: A·B 속도 통일(사용자 지시). 저속 2%·외벽 채널 15%·SAFE 이동 35%.
    (9/11 오전엔 A만 올리고 B 동결이었으나, A 검증 후 B도 같게 통일)"""
    PC.SPD_SLOW = 2            # 9/13 17:0x: 10 으로 올렸다가 3mm 걸음마다 덜컥거려 원복
    PC.SPD_CHANNEL_OUTER = 15
    return 35


def spd_move():
    return apply_speed_profile()


def grasp_gate_blocking():
    # ★9/10 밤: A타입에서 비차단으로 두고 6벽을 완주했다(안전망은 정렬 4mm·두 캠 1.5mm·막힘 감시).
    #   사용자 지시로 B타입도 같게 — 누를 버튼은 하강 하나.
    return False

# 정렬 수렴 후 사용자 조그 4회(14:33·14:51·15:31·15:57): dx −0.89/−0.04/−1.00/−0.99, dy −2.18/−1.02/−2.03/−2.50, drz +0.32/+0.88/+0.93/+0.65
#   → 부호가 전부 같고 크기도 비슷 → 사용자 지시("일률적이면 보정값") 대로 정렬 뒤 고정 보정(로봇 프레임, rz≈180 기준). nudge_log 로 잔차 계속 감시.
POST_ALIGN_OFFSET = {"blue": (0.0, 0.0, 0.0)}   # ★9/7 18:5x 빨강 −0.5mm 철회: 최소 실행 이동량(0.8mm)보다 작아
#   왕복 처리를 타는데 실제로는 +1.0mm 움직여 정렬 온전성 6.0mm 로 멈췄다. 이만한 보정은 z440 기준을 그 자리에서
#   다시 찍어 반영해야 한다(정렬이 한 번에 그 자리로 간다).   # 15:57 사용자 조그 자리(=이 보정값 자리)에서 채널 입구 막힘 → 보정 보류(0). 카메라 자리로 하강 시험 후 결정   # 15:35 재기준(사용자 자리·이 파지) 후 손목캠 rz 담당. 조그 3회 모두 손목캠 rz 방향 일치(+1.09/+0.99/+1.26 vs 사용자 +0.32/+0.88/+0.93)


# ------------------------------------------------------------------ 상태·로그
class Abort(Exception): pass
class Gate(Exception): pass

LOCK = threading.Lock()
ABORT = threading.Event()
RESUME = threading.Event()
S = {"stage": "IDLE", "color": None, "busy": False, "wait": None, "log": [], "err": None,
     "base": None, "rack": None, "grasp": None, "target": None, "align": None, "seat": None,
     "gates": {}, "tcp": None, "grip": None, "run4": None, "made": time.strftime("%H:%M:%S")}
_logf = None


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOCK:
        S["log"].append(line); S["log"] = S["log"][-300:]
    if _logf:
        _logf.write(line + "\n"); _logf.flush()


def set_stage(stage, wait=None, **kw):
    with LOCK:
        S["stage"] = stage; S["wait"] = wait; S.update(kw)
    log(f"── {stage}" + (f" (대기: {wait})" if wait else ""))
    try:                                                     # ★9/12 기준점 오버레이용 단계 기록(표시 전용)
        import refpts_overlay as _RO
        _RO.set_stage(stage, S.get("color"))
        if str(stage).startswith("1 BASE"):
            _RO.clear()                                      # 새 사이클 — 지난 매칭점 지움
    except Exception:
        pass


def jload(path, default=None):
    return json.load(open(path)) if os.path.exists(path) else default


def jsave(path, d):
    json.dump(d, open(path, "w"), ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 로봇(중단 가능 이동) — 부품들이 이걸 쓰도록 주입
def _wait_still(timeout=30):
    """이동이 멈출 때까지(busy 해제 + TCP 정지) 대기."""
    t0 = time.time(); prev = None
    while time.time() - t0 < timeout:
        if ABORT.is_set():
            PC.post("stop", {"dry_run": False}); raise Abort()
        time.sleep(0.25); s = PC.st(); c = s["tcp"]
        if prev is not None and not s["busy"] and max(abs(c[i] - prev[i]) for i in range(3)) < 0.02:
            return c
        prev = c
    return PC.st()["tcp"]


def move(tcp, tol=0.6, timeout=60, tag=""):
    if ABORT.is_set():
        raise Abort()
    r = PC.post("move_tcp", {"tcp": tcp, "dry_run": True})
    if r.get("result") != "dry_run":
        raise RuntimeError(f"{tag} dry_run 거부 {r}")
    r = PC.post("move_tcp", {"tcp": tcp, "dry_run": False})
    if r.get("result") not in ("started", "ok"):
        raise RuntimeError(f"{tag} 이동 거부 {r}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ABORT.is_set():
            PC.post("stop", {"dry_run": False}); raise Abort()
        s = PC.st()
        if s.get("frozen"):
            raise RuntimeError(f"{tag} 동결")
        c = s["tcp"]
        if max(abs(c[i] - tcp[i]) for i in range(3)) <= tol and abs(HG.wrap_deg(c[5] - tcp[5])) <= 0.3 and not s["busy"]:
            time.sleep(0.4)
            log(f"  ✓ {tag} ({c[0]:.1f},{c[1]:.1f},{c[2]:.1f}) rz{c[5]:+.2f}")
            return c
        time.sleep(0.2)
    # 19:41 실기: 목표에서 1.04mm 떨어진 곳에 서면 그보다 작은 이동은 로봇이 실행하지 않아 스스로 못 좁힘(최소 실행 이동량).
    #   남은 오차가 작으면 1mm 되돌기(초과해 갔다가 되돌아오기)로 한 번 마무리한 뒤 판정한다.
    c = PC.st()["tcp"]; rem = [tcp[i] - c[i] for i in range(3)]; n = math.hypot(rem[0], rem[1])
    if n <= 3.0 and abs(rem[2]) <= 3.0 and not PC.st().get("frozen"):
        log(f"  {tag} 잔차 {n:.2f}mm — 1mm 되돌기로 마무리")
        PC.speed(SPD_SEAT)
        ux, uy = (rem[0] / n, rem[1] / n) if n > 1e-6 else (0.0, 0.0)
        over = [tcp[0] + ux * 1.0, tcp[1] + uy * 1.0, tcp[2] + (1.0 if rem[2] >= 0 else -1.0), tcp[3], tcp[4], tcp[5]]
        try:
            PC.post("move_tcp", {"tcp": over, "dry_run": False}); _wait_still(30)
            PC.post("move_tcp", {"tcp": list(tcp), "dry_run": False}); _wait_still(30)
        except Exception as ex:
            log(f"  되돌기 실패: {ex}")
        c = PC.st()["tcp"]
        if max(abs(c[i] - tcp[i]) for i in range(3)) <= max(tol, 1.2):
            log(f"  ✓ {tag}(되돌기) ({c[0]:.1f},{c[1]:.1f},{c[2]:.1f}) rz{c[5]:+.2f}")
            return c
    raise RuntimeError(f"{tag} 미도달 목표{[round(v, 1) for v in tcp[:3]]} 현재{[round(v, 1) for v in PC.st()['tcp'][:3]]}")


PC.move = move                                   # find_base_4pts / descend_monitored 등이 중단 가능 이동을 쓴다
_ha_move_rel = HA.move_rel
def _move_rel_guard(dx, dy, drz, tol=0.3, timeout=40):
    """hover_align 의 상대이동을 이 파일의 중단 가능 move(move_tcp+dry_run+TCP 폴링) 로. 1% 속도."""
    if ABORT.is_set():
        raise Abort()
    c = PC.st()["tcp"]
    PC.speed(1)
    n = math.hypot(dx, dy)
    if 0.0 < n < MIN_EXEC_MM:
        # 14:47 실기: −0.4mm 상대이동을 로봇이 버림(9/1 실증: 최소 실행 이동량 0.6~1.15mm, 브리지는 started) → 40s 미도달 정지.
        # 9/3 '1mm 되돌기': 같은 방향으로 (d+1mm) 갔다가 1mm 되돌아 순이동 d 를 만든다(두 이동 모두 ≥1mm).
        ux, uy = dx / n, dy / n
        move([c[0] + dx + ux * 1.0, c[1] + dy + uy * 1.0, c[2], c[3], c[4], HG.wrap_deg(c[5] + drz)], tol=tol, timeout=timeout, tag=f"상대({dx:+.2f},{dy:+.2f})+1mm 되돌기 1/2")
        c2 = PC.st()["tcp"]
        return move([c2[0] - ux * 1.0, c2[1] - uy * 1.0, c2[2], c2[3], c2[4], c2[5]], tol=tol, timeout=timeout, tag="되돌기 2/2")
    tgt = [c[0] + dx, c[1] + dy, c[2], c[3], c[4], HG.wrap_deg(c[5] + drz)]
    return move(tgt, tol=tol, timeout=timeout, tag=f"상대({dx:+.1f},{dy:+.1f},{drz:+.1f}°)")
HA.move_rel = _move_rel_guard


def check_align_sanity(color, cur):
    """정렬 결과가 슬롯 기준에서 너무 멀면 정지. 시차 증폭(파지 오차 2배)으로 잘못 간 경우를 잡는다."""
    ref = (jload(F["slot"]) or {}).get(color)
    if not ref:
        return
    B = jload(F["base_last"]) or {}
    br = ref.get("base") or {}
    dx = cur[0] - (ref["seat_tcp"][0] + (B.get("x", 0) - br.get("x", 0)))
    dy = cur[1] - (ref["seat_tcp"][1] + (B.get("y", 0) - br.get("y", 0)))
    d = math.hypot(dx, dy)
    # ★9/11 사용자 지시("베이스가 움직여도 돌게·바꿔봐"): 두 카메라가 xy 로 같이 보는 색은 상한 15mm.
    #   4mm 는 '파지 오차 2배 증폭' 감지용인데, 베이스 측정 오차(rms 2.3 → 5~7mm)도 같이 막아 매번 재티칭하게 만들었다.
    #   파지 불량은 두 캠이 다른 값(COMBINE_TOL_MM 1.5mm 불일치)을 내서 align() 안에서 잡힌다 — 베이스 이동은 두 캠이 같은 값(실측 0.1mm).
    #   단일 카메라 색(red_s·red_in)은 교차검증이 없어 4mm 유지.
    roles = ALIGN_ROLES.get(color) or {}
    n_xy = sum(1 for r in roles.values() if r in ("xy", "both"))   # ★9/13 19:28: both 도 조종 카메라(안 세어 "xy캠 0" 으로 4mm 상한이 걸렸음)
    lim = ALIGN_MAX_MOVE_TWOCAM_MM   # ★9/13 19:3x 사용자 결정 "전부 손목캠이 수렴하면 진행": 조종 캠 수와 무관하게 15mm(초과는 오매칭 급만 정지)
    if d > lim:
        raise Gate(f"정렬 결과가 슬롯 기준(베이스 이동 반영)에서 {d:.1f}mm 벗어남 (dx {dx:+.1f} dy {dy:+.1f}, 허용 {lim}, xy캠 {n_xy}) — "
                   f"파지가 기준과 많이 달라 정렬이 시차로 증폭했을 수 있음. 다시 집는 것을 권함")
    log(f"  정렬 온전성: 슬롯 기준 대비 {d:.2f}mm (허용 {lim}, xy캠 {n_xy})")


def rz_measure_check(color):
    """rz 고정 모드에서 손목캠이 재는 벽 회전만 보고. 크면 '죠 안에서 벽이 돌아감' → 재파지 권고(rz 를 돌려 맞추지 않는다)."""
    if (ALIGN_ROLES.get(color) or {}).get("wrist") != "measure":
        return
    try:
        _, per, _ = HA.check(color)
        D = per.get("wrist")
        if D and not D.get("one_dot"):
            mark = "  ⚠ 재파지 권고" if abs(D["drz"]) > RZ_MEASURE_WARN else ""
            log(f"  (rz 참고) 손목캠이 보는 벽 회전 {D['drz']:+.2f}° — rz 는 절대값 유지{mark}")
    except Exception as ex:
        log(f"  (rz 참고 실패: {ex})")


def post_align_offset(color):
    """정렬 수렴 뒤 사용자 통계 보정(POST_ALIGN_OFFSET). 로봇 프레임 mm/°. 기록만 남기고 이동은 1mm 되돌기 규칙을 탄다."""
    off = POST_ALIGN_OFFSET.get(color)
    if not off or all(abs(v) < 1e-6 for v in off):
        return
    dx, dy, drz = off
    log(f"  정렬 후 사용자 통계 보정 적용: dx {dx:+.2f} dy {dy:+.2f} mm drz {drz:+.2f}°")
    _move_rel_guard(dx, dy, drz)


def wait_user(what):
    """버튼 대기. RESUME 또는 ABORT."""
    RESUME.clear()
    with LOCK:
        S["wait"] = what
    log(f"⏸ 사용자 대기: {what}")
    while not RESUME.wait(0.2):
        if ABORT.is_set():
            raise Abort()
    if ABORT.is_set():                 # ★abort 는 RESUME 도 함께 set 하므로(대기 깨우기) 깨어난 직후 반드시 ABORT 우선 — 아니면 '계속' 으로 오인
        raise Abort()
    with LOCK:
        S["wait"] = None


def up_to_safe():
    cur = PC.st()["tcp"]
    if cur[2] < SAFE_Z - 1:
        PC.speed(spd_move()); move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="상승 SAFE")


def goto_obs():
    up_to_safe(); PC.speed(spd_move()); move(OBS, tag="관측자세"); PC.speed(1)


# ------------------------------------------------------------------ 씨앗(첫 기동 시 1회): 랙 자세·매핑·벽별 파지 z 는 로봇 자세 수준의 사실이라 가져온다
def seed_state():
    if not os.path.exists(F["rack_pose"]):
        src = jload("/home/ar/bf2_console/rack_observe_pose.json")
        if src: jsave(F["rack_pose"], dict(src, note="seed 9/5 rack_observe_pose")); log("seed: rack_pose")
    if not os.path.exists(F["rack_map"]):
        rc = jload("/home/ar/bf2_console/rack_calib.json") or {}
        if rc.get("blue", {}).get("Jinv"):
            jsave(F["rack_map"], {"Jinv_mm_per_px": rc["blue"]["Jinv"], "note": "seed 9/5 ±10mm probe @rack observe (0.322mm/px)"}); log("seed: rack_map")
    if not os.path.exists(F["rack"]):
        rc = jload("/home/ar/bf2_console/rack_calib.json") or {}
        cal = (jload("/home/ar/bf2_console/dot_calib.json") or {}).get("refs", {})
        pr = jload("/home/ar/bf2_console/pick_ref_0905.json") or {}
        out = {}
        for c in COLORS:
            if c not in rc: continue
            z = (pr.get(c) or {}).get("tcp", cal.get(c, {}).get("pick_tcp_taught", [0, 0, 342]))[2]
            out[c] = {"Pc0": rc[c]["Pc0"], "Tg0": rc[c]["Tg0"], "ang0": rc[c]["ang0"], "len0_px": rc[c]["len0_px"],
                      "z_pick": z, "grip_open": GRIP_OPEN, "grip_close": (pr.get(c) or {}).get("grip_close", cal.get(c, {}).get("grip_close", 13)),
                      "offset": {"along": 0.0, "across": 0.0}, "note": "seed 9/5 사용자 시연 중앙(파랑·빨강·red_s)/taught(노랑) — 랙 보정 버튼으로 덮어씀"}
        if out: jsave(F["rack"], out); log(f"seed: rack_ref {list(out)}")


# ------------------------------------------------------------------ 1단계: 베이스(빈 손, ArUco 자 보정)
def aruco_correct(px4):
    """고정 ArUco 자로 카메라 복귀 오차를 상쇄한 기둥 px. 기준 없으면 저장만 제안, 마커 부족이면 미보정."""
    m, why = BT.markers()
    if m is None:
        return px4, {"applied": False, "why": why}
    ref = jload(F["aruco"])
    if not ref:
        return px4, {"applied": False, "why": "ArUco 기준 없음(슬롯 기준 2/2 저장 시 자동 생성)", "n": len(m)}
    T, whyT = BT.board_frame_transform(m, {int(k): v for k, v in ref["markers"].items()})
    if T is None:
        return px4, {"applied": False, "why": whyT, "n": len(m)}
    mmpx = 0.4135
    shift_mm = math.hypot(T["tx"], T["ty"]) * mmpx; dth = math.degrees(T["th"]) if abs(T["th"]) < 6.3 else T["th"]
    fixed = BT.apply_sim([(p[0], p[1]) for p in px4], T["s"], T["th"], T["tx"], T["ty"])
    out = [(x, y) + tuple(p[2:]) for (x, y), p in zip(fixed, px4)]
    info = {"applied": True, "ids": T["ids"], "rms_px": round(T["rms_px"], 2), "shift_mm": round(shift_mm, 2), "dth_deg": round(dth, 3), "s": round(T["s"], 4)}
    if shift_mm > ARUCO_WARN_MM or abs(dth) > ARUCO_WARN_DEG:
        log(f"  ⚠ ArUco 자: 카메라가 기준 대비 {shift_mm:.2f}mm/{dth:+.2f}° 벗어남 — 마운트/복귀 오차 의심(보정은 적용)")
    return out, info


WB_FIX = 4600.0            # ★9/8 실증: WB 5500 이면 노출 250 에서 파랑 든 벽 점 면적이 1823 → 258 로 무너진다
WB_TOL = 50.0


def check_wb():
    """★9/8: 손목캠 WB 가 4600 인지 사이클마다 확인하고, 어긋나 있으면 되돌린다.
    카메라가 재기동되면 WB 가 기동 스크립트 기본값으로 돌아가는데 아무도 그걸 안 봤고,
    그 상태에서는 파랑 벽 점이 조각나 막힘 감시가 조각을 붙잡는다(오늘 파랑 하강 정지의 진짜 원인).
    설정을 푸는 게 아니라 **측정 조건을 원래대로 되돌리는** 것이라 색 구분 없이 전 사이클에 적용한다."""
    try:
        e = json.loads(UR.urlopen("http://127.0.0.1:8766/expo", timeout=5).read().decode())
        wb, awb = float(e.get("wb", 0)), float(e.get("awb", 0))
        if abs(wb - WB_FIX) > WB_TOL or awb:
            UR.urlopen(f"http://127.0.0.1:8766/expo?wb={WB_FIX:.0f}&awb=0", timeout=8).read()
            e2 = json.loads(UR.urlopen("http://127.0.0.1:8766/expo", timeout=5).read().decode())
            log(f"  ⚠ 손목캠 WB {wb:.0f}(자동 {awb:.0f}) — 기준 {WB_FIX:.0f} 로 되돌림 → {float(e2.get('wb',0)):.0f}")
    except Exception as ex:
        log(f"  (WB 확인 실패: {ex})")


def stage_base(color=None):
    # ★9/7 21:1x 사용자 지시("내벽은 외벽 다 끝나고 넣을 거니까 그때만 키게 해"):
    #   내벽이 밑판 윗면을 가로질러 두 칸으로 끊는 문제 대응은 **red_in 사이클에서만** 켠다.
    #   외벽 4색은 이 값이 항상 False 라 base_depth_corner 의 예전 경로를 그대로 탄다.
    # ★9/10 실측으로 정정: 이 보정은 "내벽 색을 나르는 중"이 아니라 **내벽이 실제로 밑판에 꽂혀 있을 때** 필요하다.
    #   내벽은 마지막에 꽂으므로 자기 사이클의 베이스 측정 시점엔 아직 랙에 있다 → 켜면 없는 내벽을 있다고 보고
    #   밑판 조각을 합치려다 사각형이 깨진다(실측: OFF 4/4 검출 / ON 은 전 노출·게인에서 최고 1/4).
    #   → 평소대로 끄고 재고, **모자랄 때만** 내벽 모드로 한 번 더 본다(내벽 색에서만 — 외벽은 내벽보다 먼저 꽂으므로 만날 일이 없다).
    BDC.INNER_WALL_MODE = False
    check_wb()
    """관측자세 z650 빈 손 → 기둥 4점(탐색 포함) → ArUco 보정 → 베이스 자세(로컬 로봇축 mm, 원점=관측 화면중심)."""
    set_stage("1 BASE")
    cur = PC.st()["tcp"]
    if max(abs(cur[i] - OBS[i]) for i in range(3)) > 2.0:
        goto_obs()
    Jinv, _mp = STG.load_map()
    try:
        px4, dxy = PC.find_base_4pts(False)                   # 부품(건강게이트·노출사다리·탐색 이동 포함)
    except Exception as _e_out:
        if color not in INNER_WALLS:
            raise
        log(f"  기둥 검출 실패({_e_out}) → 내벽이 이미 꽂힌 경우로 보고 INNER_WALL_MODE 로 재시도")
        BDC.INNER_WALL_MODE = True
        try:
            px4, dxy = PC.find_base_4pts(False)
            log("  ✓ 내벽 모드에서 검출 성공(밑판이 내벽으로 갈려 있었음)")
        except Exception:
            BDC.INNER_WALL_MODE = False
            raise _e_out                                       # 원래 사유로 보고한다
    info = {"applied": False, "why": "탐색 이동으로 카메라가 관측자세를 벗어남 → 자 미적용"}
    if abs(dxy[0]) + abs(dxy[1]) < 0.5:
        px4, info = aruco_correct(px4)
    pose, rms, _ = STG.base_pose_robot(px4, Jinv, (0.0, 0.0), (640.0, 360.0))
    if abs(dxy[0]) + abs(dxy[1]) > 0.01:
        pose = HG.Pose2D(pose.x + dxy[0], pose.y + dxy[1], pose.yaw_deg)
    prev = jload(F["base_last"])
    d = None
    if prev:
        d = (pose.x - prev["x"], pose.y - prev["y"], HG.wrap_deg(pose.yaw_deg - prev["yaw"]))
        log(f"  베이스 이동(직전 {prev['made']} 대비): Δx {d[0]:+.2f} Δy {d[1]:+.2f} Δyaw {d[2]:+.3f}°")
    B = {"x": pose.x, "y": pose.y, "yaw": pose.yaw_deg, "rms": rms, "aruco": info, "px4": [list(p) for p in px4],
         "made": time.strftime("%Y-%m-%d %H:%M:%S"), "delta_prev": d}
    jsave(F["base_last"], B)
    log(f"  베이스 x {pose.x:.2f} y {pose.y:.2f} yaw {pose.yaw_deg:+.3f}° rms {rms:.2f}mm  ArUco {info}")
    with LOCK: S["base"] = B
    return B


def slot_target(color, B, grasp=None):
    """벽별 슬롯 기준(손 안착 TCP + 그때 베이스) 을 지금 베이스로 강체 이동 → 목표 (x, y, rz, z_seat).
    파지 편차(있으면) 는 상한 PRECORR_MAX_MM 안에서 선보정(부호 미검증 — 호버 정렬이 최종)."""
    ref = (jload(F["slot"]) or {}).get(color)
    if not ref:
        raise Gate(f"{color} 슬롯 기준 없음 — 손으로 안착 → 로봇으로 물고 '슬롯 기준 1/2' → 놓고 관측자세 → '슬롯 기준 2/2'")
    br = ref["base"]; seat = ref["seat_tcp"]
    dyaw = HG.wrap_deg(B["yaw"] - br["yaw"])
    # 15:44 실기: 베이스 x/y 는 관측 화면중심 원점의 로컬 mm, seat 는 로봇 좌표 → 그대로 빼면 회전 중심이 ~400mm 어긋나
    #   Δyaw 0.5°→3.6mm, 0.8°→6.3mm, 2°→14mm 오차(그동안 카메라 정렬이 흡수). 로컬 원점 ≈ 관측자세 XY(카메라 오프셋 수십 mm 는 미보정).
    ox, oy = OBS[0], OBS[1]
    sx, sy = seat[0] - (ox + br["x"]), seat[1] - (oy + br["y"])
    c, s = math.cos(math.radians(dyaw)), math.sin(math.radians(dyaw))
    tx, ty = (ox + B["x"]) + c * sx - s * sy, (oy + B["y"]) + s * sx + c * sy
    rz = HG.wrap_deg(seat[5] + dyaw)
    pre = (0.0, 0.0, 0.0)
    if grasp and grasp.get("ok"):
        ac, al, da = grasp["across_mm"], grasp["along_mm"], grasp["dang"]
        n = math.hypot(ac, al)
        if n > PRECORR_MAX_MM:
            ac, al = ac * PRECORR_MAX_MM / n, al * PRECORR_MAX_MM / n
        cr, sr = math.cos(math.radians(rz)), math.sin(math.radians(rz))
        tx -= cr * ac - sr * al; ty -= sr * ac + cr * al
        if PRECORR_RZ:
            rz = HG.wrap_deg(rz - da)
        else:
            da = 0.0
        pre = (ac, al, da)
    T = {"x": tx, "y": ty, "rz": rz, "z_seat": seat[2], "dyaw": dyaw, "precorr": pre, "ref_made": ref["made"]}
    log(f"  목표 [{color}] x {tx:.2f} y {ty:.2f} rz {rz:+.2f} (베이스 Δyaw {dyaw:+.3f}°, 선보정 {pre[0]:+.2f}/{pre[1]:+.2f}mm {pre[2]:+.2f}°)")
    with LOCK: S["target"] = T
    return T


# ------------------------------------------------------------------ 2단계: 랙 양끝 → 중앙 → 벽별 보정 → 파지 → 서명 편차
def rack_axes(e, Jinv):
    """벽 축 단위벡터(로봇 프레임): along = p1→p2, across = 그 수직."""
    u = np.array([e["p2"][0] - e["p1"][0], e["p2"][1] - e["p1"][1]], float)
    u = Jinv @ u; u = u / (np.linalg.norm(u) + 1e-9)
    return u, np.array([-u[1], u[0]])


# ★9/12 사용자 지시: "점 세 개인데 2개만 보이면 밑으로 살짝 위로 살짝 왔다 갔다 하며 점을 찾아라".
#   가운데 점을 못 보면 '끝점 중점' 으로 물러나는데, 점 간격이 불균등해(9/6 실측 38.8/53.5/63.1/29.7mm)
#   파지점이 벽 아래쪽으로 치우친다 — 빨강에서 "너무 밑에 잡힌다" 로 나타났다.
#   반사광이 점을 지우는 경우가 많아 카메라를 벽 축으로 조금만 옮겨도 점이 살아난다.
RACK_MIDDOT_NUDGE_MM = (6.0, -6.0, 12.0, -12.0)   # 벽 축(화면 위·아래) 탐색 폭 — 작게, 번갈아


def rack_middot_search(color, e, rr, rp, Jinv, L0, dxy):
    """가운데 점이 안 잡혀 '끝점 중점' 으로 물러난 경우에만, 카메라를 벽 축으로 ±조금 오가며 다시 본다.
    찾으면 (그 측정, 그 자리의 카메라 오프셋) — 못 찾으면 원래 자리로 돌아와 받은 값 그대로 돌려준다(게이트 완화 아님)."""
    want = PC.WALL_DOT_N.get(color)
    if not want or e.get("grip_mode") == "mid_dot":
        return e, dxy
    ua, _ux = rack_axes(e, Jinv)
    home = list(PC.st()["tcp"]); J = np.linalg.inv(Jinv)
    # ★9/13 18:40 노랑 사고: 점 3/5 로 가운데를 못 봐 끝점 중점으로 얕게 물었고 들어올리다 벽이 빠졌다.
    #   그때 로봇만 ±12mm 움직였을 뿐 **노출은 한 번도 안 바꿨다** — 사용자 지시("점 검출 안 되면 노출부터 조정")
    #   이자 철칙("검출 실패를 로봇 이동으로 해결하지 말 것)"과도 어긋난다.
    #   → 제자리에서 노출 사다리를 먼저 돌리고, 그래도 안 되면 그때만 종전 ±mm 탐색으로 간다.
    keep_ex = HA.current_expo()
    log(f"  가운데 점 미검출(점 {e.get('n_dots','?')}/{want}) → 먼저 제자리 노출 사다리")
    x_hint0 = rr["Pc0"][0] + float((J @ np.array(dxy))[0])
    for ex in (500, 417, 333, 250, 167, 83):
        if keep_ex is not None and abs(float(ex) - float(keep_ex)) < 1.0:
            continue
        HA.set_expo(ex); time.sleep(getattr(HA, "EXPO_SETTLE_S", 1.4))
        e2, n2, _ = rack_measure(color, L0, x_hint0)
        if not e2:
            log(f"    노출 {ex}: 측정 실패"); continue
        okl = abs(e2["len_px"] - L0) <= RACK_LEN_TOL * L0
        log(f"    노출 {ex}: 점 {e2.get('n_dots','?')}개 길이 {e2['len_px']:.0f}px "
            f"기준={'가운데 점' if e2.get('grip_mode') == 'mid_dot' else '끝점 중점'}")
        if e2.get("grip_mode") == "mid_dot" and okl:
            log(f"  ✅ 가운데 점 확보(노출 {ex}, 로봇 안 움직임) — 파지점 {e['mid'][1]:.0f}px → {e2['mid'][1]:.0f}px")
            try:                                  # 통한 노출은 기준에 기억시켜 다음 사이클은 처음부터 맞춘다
                _all = jload(F["rack"]) or {}
                if _all.get(color, {}).get("expo") != ex:
                    _all.setdefault(color, {})["expo"] = ex
                    jsave(F["rack"], _all); rr["expo"] = ex
                    log(f"  (랙 노출 {ex} 를 {color} 기준에 저장)")
            except Exception:
                pass
            return e2, dxy
    if keep_ex is not None:
        HA.set_expo(keep_ex); time.sleep(getattr(HA, "EXPO_SETTLE_S", 1.4))
    log(f"  노출 사다리로도 못 찾음 → 벽 축으로 "
        f"±{max(RACK_MIDDOT_NUDGE_MM):.0f}mm 오가며 가운데 점 재탐색")
    for mm in RACK_MIDDOT_NUDGE_MM:
        PC.speed(spd_move())
        move([home[0] + float(ua[0]) * mm, home[1] + float(ua[1]) * mm, home[2]] + home[3:],
             tag=f"가운데 점 탐색 {mm:+.0f}mm")
        time.sleep(0.4)
        cur = PC.st()["tcp"]; d2 = (cur[0] - rp[0], cur[1] - rp[1])
        e2, n2, _ = rack_measure(color, L0, rr["Pc0"][0] + float((J @ np.array(d2))[0]))
        if not e2:
            log(f"    {mm:+.0f}mm: 측정 실패"); continue
        okl = abs(e2["len_px"] - L0) <= RACK_LEN_TOL * L0
        log(f"    {mm:+.0f}mm: 점 {e2.get('n_dots','?')}개 길이 {e2['len_px']:.0f}px "
            f"기준={'가운데 점' if e2.get('grip_mode') == 'mid_dot' else '끝점 중점'}")
        if e2.get("grip_mode") == "mid_dot" and okl:
            log(f"  ✅ 가운데 점 확보({mm:+.0f}mm 자리) — 파지점 {e['mid'][1]:.0f}px → {e2['mid'][1]:.0f}px")
            return e2, d2
    PC.speed(spd_move()); move(home, tag="가운데 점 탐색 실패 → 원위치")
    log("  가운데 점 못 찾음 — 끝점 중점으로 진행(파지점이 아래로 치우칠 수 있음)")
    return e, dxy


# ★9/7 실측(red_s, 노출 500 에서 10회): 5회가 끝점 하나를 놓쳐 길이 362→274/185px 로 줄고 중앙이 44~89px 밀린다.
#   길이 10% 게이트는 274px 은 걸러도 341px(-5.4%) 은 통과시켜 파지점을 3.3mm 틀리게 잡았다.
#   → 기준 길이 ±RACK_LEN_TOL 에 드는 측정만 모아 중앙값을 쓴다(채택 5회 산포 x0.6 y1.1px).
RACK_LEN_TOL = 0.02
RACK_NEED = 3
RACK_TRIES = 10


def rack_measure(color, L0, x_hint, need=RACK_NEED, tries=RACK_TRIES, tol=RACK_LEN_TOL):
    """끝점 소실 측정을 버리고 채택분 중앙값을 돌려준다. 채택 need 개 미만이면 (None, 채택수, 마지막)."""
    import statistics as _s
    good = []; last = None
    for _ in range(tries):
        e = PC.rack_ends(color, x_hint=x_hint)
        if not e:
            continue
        last = e
        if abs(e["len_px"] - L0) <= tol * L0:
            good.append(e)
            if len(good) >= need and len(good) >= 3:
                break
    if len(good) < need:
        return None, len(good), last
    ang = [((g["ang"] + 90.0) % 180.0) - 90.0 for g in good]
    out = {"mid": (_s.median([g["mid"][0] for g in good]), _s.median([g["mid"][1] for g in good])),
           "len_px": _s.median([g["len_px"] for g in good]), "ang": _s.median(ang),
           "n_dots": good[-1]["n_dots"],
           "p1": (_s.median([g["p1"][0] for g in good]), _s.median([g["p1"][1] for g in good])),
           "p2": (_s.median([g["p2"][0] for g in good]), _s.median([g["p2"][1] for g in good])),
           "n_ok": len(good)}
    ys = [g["mid"][1] for g in good]
    out["spread_px"] = float(max(ys) - min(ys))
    return out, len(good), last


def stage_rack(color, teach_rack=False):
    set_stage("2 RACK", color=color)
    # ★9/7 21:1x 사용자 지시("내벽하고 외벽 조건 따로 써"): 외벽 4색은 예전 그대로 **사이클 맨 앞**에서 검사한다.
    #   내벽(red_in)만 랙 SAFE 도착 후(=그리퍼 여는 순간 직전)에 검사한다 — 맨 앞에서 보면 로봇이 베이스 위에
    #   서 있을 때 기둥 빨간 점(785,522)을 '든 벽'으로 오인해 헛정지하기 때문(20:36 실측).
    if color not in INNER_WALLS and PC.held_wall_dots_expo(color):
        raise Gate("그리퍼에 벽이 이미 있습니다 — 랙으로 가면 여기서 열어 떨어뜨립니다. "
                   "벽을 먼저 내려놓거나 [▶ 든 채로 3단계부터] 로 진행하세요")
    rr = (jload(F["rack"]) or {}).get(color)
    if not rr:
        raise Gate(f"{color} 랙 기준 없음")
    Jinv = np.array(jload(F["rack_map"])["Jinv_mm_per_px"], float)
    rp = jload(F["rack_pose"])["tcp"]
    up_to_safe(); PC.speed(spd_move())
    move([rp[0], rp[1], SAFE_Z, 180.0, 0.0, 180.0], tag="랙 위 SAFE")
    # ★9/7 사고: 벽을 문 채 사이클을 시작하면 여기서 그리퍼를 열어 벽을 떨어뜨린다 → 열기 직전에 확인.
    #   ★20:1x: 이 검사를 사이클 맨 앞에서 하면 로봇이 베이스 위에 서 있을 때 기둥 빨간 점(785,522)을
    #   "든 벽"으로 오인해 헛정지한다. 랙 SAFE 로 온 뒤(베이스가 화면 밖) 여는 순간 직전에 본다.
    if color in INNER_WALLS and PC.held_wall_dots_expo(color):
        raise Gate("그리퍼에 벽이 이미 있습니다 — 여기서 열면 떨어집니다. "
                   "벽을 먼저 내려놓거나 [▶ 든 채로 3단계부터] 로 진행하세요")
    log(f"  그리퍼 열기 → {PC.gripper(rr.get('grip_open', GRIP_OPEN))}")
    move(rp, tag="랙 관측자세")
    # ★9/7 실측: 랙 관측 노출이 색마다 다르다(red_s 는 500 이라야 양끝이 붙는다). 노출이 낮으면
    #   점이 하나만 잡혀 카메라를 45mm 옮겨 재관측하는데, 그 이동만으로 각 0.33°·파지 1.4mm 가 틀어진다.
    #   → 색별로 '통했던 노출'을 기준에 저장해 두고 관측 전에 먼저 맞춘다.
    if rr.get("expo"):
        try:
            if abs(float(HA.current_expo() or 0) - float(rr["expo"])) > 1.0:
                HA.set_expo(float(rr["expo"])); time.sleep(getattr(HA, "EXPO_SETTLE_S", 1.4))   # ★9/12: 0.6s 는 전환 중 프레임(9/10 실증)
            log(f"  (랙 관측 노출 {float(rr['expo']):.0f} 로 맞춤 — 기준 촬영과 같은 노출)")
        except Exception as ex:
            log(f"  (랙 노출 맞춤 실패: {ex})")
    # ★9/8 측면캠 안착 판정용 '꽂기 전' 스냅샷. 지금 로봇은 랙에 있어 베이스 화면에 안 들어온다 = 깨끗한 한 장.
    #   나중에 벽을 놓고 물러난 뒤 한 장 더 떠서, 새로 생긴 점 = 이 벽의 점으로 가른다.
    try:
        with LOCK: S["side_pre"] = SSEAT.snapshot()
        log(f"  (측면캠 꽂기 전 스냅샷 {len(S['side_pre'])}점)")
    except Exception as ex:
        with LOCK: S["side_pre"] = None
        log(f"  (측면캠 스냅샷 실패: {ex})")
    L0 = rr["len0_px"]; dxy = (0.0, 0.0); e = None
    for rnd in range(3):                                          # 14:42 실기: 벽이 프레임 아래끝(y707/720)까지 밀려 끝점 잘림 → 길이 −16% 정지 → 카메라를 옮겨 재관측
        cur = PC.st()["tcp"]; dxy = (cur[0] - rp[0], cur[1] - rp[1])
        J = np.linalg.inv(Jinv)
        x_hint = rr["Pc0"][0] + float((J @ np.array(dxy))[0])
        e, n_ok, last = rack_measure(color, L0, x_hint)           # 부품 + 끝점소실 배제 중앙값
        if e:
            log(f"  (랙 측정: 길이 ±{RACK_LEN_TOL*100:.0f}% 채택 {n_ok}회 중앙값 — 중앙 세로산포 {e['spread_px']:.1f}px)")
        else:
            log(f"  (랙 측정: 채택 {n_ok}회 < {RACK_NEED} — 끝점 소실 잦음, 노출 사다리로 재시도)")
            e = None
        if not e:
            # 9/7: 랙은 베이스보다 멀고 어두워 관측자세 저노출(42~83)에선 색점이 안 보인다(파랑 0개) → 노출 사다리로 찾는다.
            keep = HA.current_expo()
            for ex in (500, 417, 333, 250, 167, 83, 42):   # 9/7: red_s 는 빨간 점이 작고 어두워 500 이라야 양끝이 안정적으로 붙는다
                HA.set_expo(ex); time.sleep(getattr(HA, "EXPO_SETTLE_S", 1.4))                  # ★9/12: 사다리도 같은 대기
                e2, n2, _ = rack_measure(color, L0, x_hint)
                if e2:
                    log(f"  (랙 관측: 노출 {ex} 로 벽 양끝 확보 — 채택 {n2}회 길이 {e2['len_px']:.0f}px)")
                    e = e2
                    try:                                   # 통한 노출은 기준에 기억시켜 다음 사이클은 처음부터 맞춘다
                        _all = jload(F["rack"]) or {}
                        if _all.get(color, {}).get("expo") != ex:
                            _all.setdefault(color, {})["expo"] = ex
                            jsave(F["rack"], _all); rr["expo"] = ex
                            log(f"  (랙 노출 {ex} 를 {color} 기준에 저장)")
                    except Exception:
                        pass
                    break
            else:
                if keep: HA.set_expo(keep)
        ok = bool(e) and abs(e["len_px"] - L0) <= RACK_LEN_TOL * L0
        if ok:
            break
        c, n = PC._rack_color_center(color, x_hint)
        if c is None:
            raise Gate("랙에서 벽 양끝을 못 잡음 — 벽이 뒤집혔거나 점 가림. 랙에 다시 놓기")
        d = Jinv @ np.array([640.0 - c[0], 360.0 - c[1]]); nrm = float(np.hypot(*d))
        if nrm > 50.0: d = d * (50.0 / nrm)
        if nrm < 3.0:
            break                                                 # 이미 중앙인데도 안 맞음 → 아래서 게이트
        log(f"  랙 {color} 점 {n}개 중심 px ({c[0]:.0f},{c[1]:.0f}) — 끝점 잘림 의심(길이 {e['len_px'] if e else 0:.0f}px vs {L0:.0f}) → 카메라 ({d[0]:+.1f},{d[1]:+.1f})mm 이동 재관측 [{rnd+1}/3]")
        PC.speed(spd_move()); move([cur[0] + float(d[0]), cur[1] + float(d[1]), rp[2]] + list(rp[3:]), tag="랙 재관측 XY"); time.sleep(0.5)
    if not e:
        raise Gate("랙에서 벽 양끝을 못 잡음 — 벽이 뒤집혔거나 점 가림. 랙에 다시 놓기")
    if abs(e["len_px"] - L0) > RACK_LEN_TOL * L0:
        raise Gate(f"벽 길이 불일치 {e['len_px']:.0f}px vs 기준 {L0:.0f}px ({(e['len_px']-L0)/L0*100:+.1f}%) — 끝점 미검출, 파지 금지")
    e, dxy = rack_middot_search(color, e, rr, rp, Jinv, L0, dxy)   # ★9/12 가운데 점 재탐색(위·아래 조금씩)
    if abs(dxy[0]) + abs(dxy[1]) > 0.5:
        log(f"  (랙 재관측 카메라 오프셋 Δ ({dxy[0]:+.1f},{dxy[1]:+.1f})mm 반영)")
    # 15:39 실기: 양끝 p1/p2 순서가 x 몇 px 차이로 뒤집혀 각 +178° (방향 반전) → '삐뚤게 놓임' 오판. 벽은 방향 없는 선분:
    #   p1 = 화면 위쪽(y 작은) 끝으로 고정, 각은 (−90,90] 로 정규화.
    if e["p1"][1] > e["p2"][1]:
        e = dict(e, p1=e["p2"], p2=e["p1"])
    e["ang"] = ((e["ang"] + 90.0) % 180.0) - 90.0
    dang = HG.wrap_deg(e["ang"] - (((rr["ang0"] + 90.0) % 180.0) - 90.0))
    dang = ((dang + 90.0) % 180.0) - 90.0
    if abs(dang) > RACK_ANG_MAX:
        raise Gate(f"랙 위 벽 각 변화 {dang:+.2f}° > {RACK_ANG_MAX}° — 벽이 삐뚤게 놓임")
    dmm = Jinv @ np.array([e["mid"][0] - rr["Pc0"][0], e["mid"][1] - rr["Pc0"][1]])
    gx, gy = rr["Tg0"][0] - dmm[0] + dxy[0], rr["Tg0"][1] - dmm[1] + dxy[1]   # 검증된 부호(rack_grip_xy 와 동일) + 재관측 카메라 오프셋(rack_find 와 동일 식)
    ua, ux = rack_axes(e, Jinv)
    off = rr.get("offset") or {"along": 0.0, "across": 0.0}
    gx += off["along"] * ua[0] + off["across"] * ux[0]
    gy += off["along"] * ua[1] + off["across"] * ux[1]
    rz = 180.0
    if RACK_RZ_FOLLOW:
        rz = HG.wrap_deg(180.0 + dang)
    rot = [180.0, 0.0, rz]
    R = {"mid": e["mid"], "len_px": e["len_px"], "ang": e["ang"], "dang": dang, "n_dots": e["n_dots"], "grip_xy": (gx, gy), "offset": off, "made": time.strftime("%H:%M:%S")}
    try:                                                     # ★9/12 랙 단계 기준점(손목캠): 기준 파지중심 Pc0 ↔ 지금 양끝·중점
        import refpts_overlay as _RO
        _RO.publish_pts(color, "wrist", "2 RACK",
                        ref=[["Pc0 grip", "green", rr["Pc0"][0], rr["Pc0"][1]]],
                        now=[["p1", "yellow", e["p1"][0], e["p1"][1]], ["p2", "yellow", e["p2"][0], e["p2"][1]],
                             ["mid", "white", e["mid"][0], e["mid"][1]]],
                        note=f"len {e['len_px']:.0f}/{L0:.0f}px  ang {dang:+.2f}deg  dots {e.get('n_dots','?')}")
    except Exception:
        pass
    with LOCK: S["rack"] = R
    _gm = {"mid_dot": "가운데 점", "ends_mid": "끝점 중점(가운데 점 미검출)"}.get(e.get("grip_mode", "ends_mid"), "?")
    log(f"  랙: 파지기준={_gm} ({e['mid'][0]:.0f},{e['mid'][1]:.0f}) 점 {e.get('n_dots','?')}개 길이 {e['len_px']:.0f}px 각 {e['ang']:+.2f}°(Δ{dang:+.2f}) → 파지 XY ({gx:.2f},{gy:.2f}) 보정 along {off['along']:+.2f} across {off['across']:+.2f}")
    zp = rr["z_pick"]
    PC.speed(spd_move()); move([gx, gy, rp[2]] + rot, tag="파지 XY 위")
    PC.speed(SPD_DESC); move([gx, gy, zp + 40] + rot, tag="픽 −40")
    PC.speed(SPD_SEAT); move([gx, gy, zp] + rot, tol=0.8, tag="픽 자세")
    if teach_rack:
        # 사용자 조그로 진짜 중앙 → '랙 보정 저장' 버튼(현재 TCP − 계산 TCP 를 벽 축으로 분해해 저장) → 계속
        with LOCK: S["rack"]["calc_xy"] = (gx, gy); S["rack"]["axes"] = (ua.tolist(), ux.tolist())
        wait_user("랙 파지 티칭: 그리퍼를 벽 진짜 중앙으로 조그한 뒤 [랙 보정 저장] → 계속")
        cur = PC.st()["tcp"]; gx, gy = cur[0], cur[1]
    g_close = rr["grip_close"]
    gr = PC.gripper(g_close); log(f"  그리퍼 닫기 {g_close} → 실측 {gr}")
    ok, why = PC._grip_ok(gr, g_close, color)                     # 부품(실측>닫힘 또는 손목캠 벽 점)
    if not ok:
        raise Gate("빈 파지 의심(" + why + ")")
    log("  파지 판정 OK: " + why)
    PC.speed(SPD_DESC); move([gx, gy, zp + 125] + rot, tag="들어올림")
    log(f"  (참고) 들어올린 뒤 그리퍼 {PC.grip_read()}")
    return grasp_check(color, dang, g_close, gr)


# ★9/10 사용자 지시: "yellow_in 은 벽이 짧아 들어올린 자세에서 점이 안 보인다. 그냥 잡으면 잘 잡은 것이니 검사하지 마라."
#   랙 들어올림 자세(z453)에서 든 벽 점이 0개라 서명 촬영도 편차 측정도 원리적으로 불가능하다.
#   → 이 색은 파지 편차 게이트를 건너뛴다(선보정 없음). 그리퍼 실측 > 닫힘값 검사는 그대로 살아 있고,
#     **슬롯 자리(z440)에서는 점 2개가 보이므로 호버 정렬·막힘 감시는 정상 작동한다.**
NO_GRASP_SIG = {"yellow_in"}


def grasp_check(color, rack_dang, g_close, gr):
    if color in NO_GRASP_SIG:
        log(f"  (파지 편차 검사 생략 [{color}] — 짧은 벽이라 들어올린 자세에서 점이 안 보임. 선보정 없음)")
        return None
    # ★9/7 실측: 같은 파지·같은 자세에서 노출만 바꿔도 든 벽 점 중심이 12.8px(≈2.5mm) 움직인다(면적 541↔1733).
    #   서명을 찍은 노출과 다른 노출에서 재면 '가짜 파지 편차'가 나온다 → 서명 노출로 맞추고 잰다.
    try:
        _sig = (PC.load_grasp_ref(color) or {})
        _e = _sig.get("expo")
        if _e:
            import hover_align as _HA
            if abs(float(_HA.current_expo() or 0) - float(_e)) > 1.0:
                _HA.set_expo(float(_e)); time.sleep(getattr(_HA, "EXPO_SETTLE_S", 1.4))         # ★9/12
                log(f"  (파지 편차: 서명 촬영 노출 {float(_e):.0f} 로 맞춤)")
        else:
            log("  (파지 편차: 서명에 촬영 노출이 없음 — 값 신뢰도 낮음, 재촬영 권장)")
    except Exception as ex:
        log(f"  (파지 편차 노출 정렬 실패: {ex})")
    """든 벽 점 ↔ 좋은 파지 서명. 서명 없으면 사용자 확인 후 저장(버튼). 게이트 1.0mm/0.3°."""
    if PC.load_grasp_ref(color) is None:
        wait_user(f"{color} 좋은 파지 서명 없음: 파지가 정상이면 [파지 서명 저장] → 계속 (아니면 중단)")
    g, info = PC.grasp_measure(color, rack_dang=rack_dang)      # 부품(2점/1점, 축척 핀홀 상수)
    if g is None and "든 벽 점 0" in str(info):
        # 9/7: 든 벽 점이 보이는 노출은 조명에 따라 바뀐다 — 사다리로 찾은 뒤 재측정(멈추기 전에 노력)
        if PC.held_wall_dots_expo(color):
            g, info = PC.grasp_measure(color, rack_dang=rack_dang)
    if g is None:
        raise Gate("파지 편차 측정 불가: " + str(info))
    G = {"ok": True, "across_mm": info["across_mm"], "along_mm": info["along_mm"], "dang": info["dang"], "how": info["how"], "grip": gr}
    with LOCK: S["grasp"] = G
    log(f"  파지 편차({info['how']}): 가로 {info['across_mm']:+.2f} 길이 {info['along_mm']:+.2f}mm 각 {info['dang']:+.2f}°")
    if abs(info["along_mm"]) > GRASP_GATE_MM or abs(info["across_mm"]) > GRASP_GATE_MM or abs(info["dang"]) > GRASP_GATE_DEG:
        # 13:27 실기: 위치 −0.1mm 인데 각 +0.44° 로 정지(랙 위 벽이 −0.55° 돌아 있고 파지 rz 180 고정). z440 정렬이 회전을 보정하므로
        # 즉시 정지 대신 사용자 선택: [▶계속]=이 파지로 진행(z440 에서 보정) / [⛔중단]=정지(벽은 사용자가 랙으로).
        # ★9/10 사용자 지시: "다음 사이클부터 내가 누를 버튼은 하강뿐이어야 한다."
        #   파지 편차는 **선보정 + z440 정렬**이 고치는 값이라 여기서 사람을 세울 이유가 없다.
        #   진짜 위험은 뒤의 세 안전망이 잡는다: ①정렬 온전성 4mm ②두 캠 불일치 1.5mm ③막힘 감시.
        if grasp_gate_blocking():
            wait_user(f"파지 편차 게이트 초과({GRASP_GATE_MM}mm/{GRASP_GATE_DEG}°): 가로 {info['across_mm']:+.2f} 길이 {info['along_mm']:+.2f}mm 각 {info['dang']:+.2f}° — "
                      f"[▶계속]=이 파지로 진행(z440 정렬이 보정) / [⛔중단]=정지 후 재파지")
        else:
            log(f"  ⚠ 파지 편차 게이트 초과({GRASP_GATE_MM}mm/{GRASP_GATE_DEG}°) — 기록만 하고 진행(정렬이 보정)")
        G["gate_override"] = True
    return G


# ------------------------------------------------------------------ 2'단계: 운반 → z_seat+85 → 호버 정렬(기둥점↔벽점 기준 관계)
def stage_carry_hover(color, T):
    set_stage("2' CARRY", color=color)
    rot = [180.0, 0.0, T["rz"]]
    up_to_safe(); PC.speed(spd_move())
    move([T["x"], T["y"], SAFE_Z] + rot, tag="목표 위 SAFE(rz 정렬)")
    log(f"  (참고) 운반 후 그리퍼 {PC.grip_read()}")
    PC.speed(SPD_DESC); move([T["x"], T["y"], HOVER_Z] + rot, tag="호버 z478")
    # ★9/8: 정렬 기준은 다섯 색 모두 z440 에서 찍혔는데 운반 높이는 '안착+85' 로 계산한다.
    #   외벽은 안착이 351~355 라 436~440 이 나와 허용 ±3mm 에 우연히 들어갔지만, 내벽은 안착 338 →
    #   423 으로 17mm 어긋나 "높이 z423 ≠ 기준 z440" 로 정렬이 아예 못 돌았다.
    #   색별 높이차를 둔다. 외벽 4색은 .get 이 85.0 을 돌려주므로 예전 값 그대로다.
    zh = T["z_seat"] + HOVER_DZ.get(color, 85.0)
    PC.speed(SPD_SEAT); move([T["x"], T["y"], zh] + rot, tag=f"기둥 위 z{zh:.0f}")
    set_stage("2' HOVER ALIGN", color=color)
    if HA.load_ref(color) is None:
        wait_user(f"{color} z{zh:.0f} 기준 없음: 콘솔 조그로 육안 정렬 후 [z440 기준 저장] → 계속")
    A = {"done": False}
    try:
        roles = ALIGN_ROLES.get(color); srcs = list(roles) if roles else ALIGN_SRCS.get(color)
        if srcs:
            try:
                _, per, _ = HA.check(color)                         # 참고: 전 카메라 측정치 로그
                for D in per.values(): log(f"  (참고) {HA.fmt(D)}")
            except Exception as ex: log(f"  (참고 측정 실패: {ex})")
            log(f"  정렬 카메라: {srcs} 역할 {roles or 'both'}")
        HA.EXPECT_TCP = [T["x"], T["y"]]                       # ★9/11: 예측 이동 매칭의 기준점 = 이번 계산 목표
        try:
            HA.align(color, srcs=srcs, roles=roles)            # 부품(지정 카메라·역할, 수렴/발산/불일치 게이트)
        except Exception as ex:
            # ★9/13 17:2x 사용자 지시(촬영): "기준점하고 많이 벗어나면 기준점으로 그냥 보내" — 정렬이 못 맞추면(불일치·검출·미수렴)
            #   멈추지 않고 그 색의 호버 기준 자리(오늘 사용자가 확인한 들어가는 자리, hover_ref[*].tcp)로 보내고 WAIT DESCEND 로 넘긴다.
            #   하강은 사용자 버튼이므로 자리가 이상하면 거기서 멈출 수 있다. 어떤 정렬 실패였는지는 로그에 그대로 남긴다.
            HA.EXPECT_TCP = None
            refs = HA.load_ref(color) or {}
            rt = next((v["tcp"] for k in ("wrist", "newcam") for v in [refs.get(k)] if isinstance(v, dict) and v.get("tcp")), None)
            if rt is None:
                raise
            log(f"  ⚠ 정렬 실패 → 기준 자리로 보냄 (사유: {ex})")
            cur = PC.st()["tcp"]
            PC.speed(SPD_SEAT); move([rt[0], rt[1], cur[2], cur[3], cur[4], rt[5]], tag=f"기준 자리 ({rt[0]:.1f},{rt[1]:.1f}) rz{rt[5]:+.2f}")
            log(f"  기준 자리 도착 — 정렬 없이 하강 대기(자리 확인 후 [⬇ 하강])")
        finally:
            HA.EXPECT_TCP = None
        try:
            post_align_offset(color)
            rz_measure_check(color)
        except Exception as ex:
            log(f"  (정렬 후 점검 생략: {ex})")
        A["done"] = True
    finally:
        HA.restore_expo()
    cur = PC.st()["tcp"]
    A.update(x=cur[0], y=cur[1], rz=cur[5], z=cur[2], made_t=time.time(), made=time.strftime("%H:%M:%S"))
    with LOCK: S["align"] = A
    log(f"  정렬 후 TCP x {cur[0]:.2f} y {cur[1]:.2f} rz {cur[5]:+.2f} z {cur[2]:.1f}")
    # ★9/7 14:57 파랑: 정렬이 슬롯 기준에서 6.0mm 옮겨 놓아 안 들어갔다. 이 게이트는 그때 이미 코드에 있었는데
    #   어디서도 호출되지 않아 못 잡았다(로그에 '정렬 온전성' 줄이 한 번도 없음) → 여기서 호출한다.
    check_align_sanity(color, cur)
    return A


# ------------------------------------------------------------------ 3단계: 사용자 버튼 하강(막힘 감시) → 안착 판정 → (seated 만) 승격·놓기
def descend_gate(color):
    """②하강 직전 게이트. 정렬 완료 상태 + 같은 색 + 정렬 후 15분 이내 + z 일치 + 정렬 TCP 와 XY/rz 일치."""
    with LOCK:
        T = S.get("target"); A = S.get("align"); col = S.get("color")
    cur0 = PC.st()["tcp"]
    if not T:                                                   # 서버 재시작 등으로 목표가 없으면 슬롯 기준의 z_seat 만 가져온다(사용자 수동 정렬 전제)
        ref = (jload(F["slot"]) or {}).get(color)
        if not ref:
            raise Gate(f"{color} 슬롯 기준 없음 — 하강 불가")
        T = {"x": None, "y": None, "rz": None, "z_seat": ref["seat_tcp"][2], "user_fallback": True}
        with LOCK: S["target"] = T; S["color"] = color
        col = color
        log(f"  목표 없음 → 슬롯 기준 z_seat {T['z_seat']:.1f} 만 사용(사용자 수동 정렬)")
    if not (A and A.get("done")):
        # 13:3x 실기: 카메라 불일치로 정렬이 안 돈 상태에서 사용자가 육안으로 맞춤 → 설계 3단계(사용자 허락 후 버튼 하강) 그대로,
        # 지금 TCP 를 '사용자 정렬 완료' 로 간주. 단 z 는 z_seat+85 ±3 이어야.
        # ★9/8: 이 경로(정렬 상태 없이 수동 하강)의 높이 창도 색별 정렬 높이(HOVER_DZ)를 따라가야 한다.
        #   내벽은 안착 340.5 + 102 = 442.5 에서 정렬하는데 상한이 '안착+88'(=428.5) 이라 창 밖이 됐다.
        #   외벽 4색은 HOVER_DZ 기본 85 → 상한 88 로 예전과 동일.
        _zhi = T["z_seat"] + HOVER_DZ.get(color, 85.0) + 3.0
        if not (T["z_seat"] - 1.0 <= cur0[2] <= _zhi):
            raise Gate(f"정렬 상태 없음 + 지금 z{cur0[2]:.0f} 가 z{T['z_seat']-1:.0f}~{_zhi:.0f} 밖 — 수동 정렬이면 정렬 높이(또는 채널 안)에서 누르세요")
        A = {"done": True, "by": "user", "x": cur0[0], "y": cur0[1], "z": cur0[2], "rz": cur0[5], "made_t": time.time(), "made": time.strftime("%H:%M:%S")}
        with LOCK: S["align"] = A
        log(f"  정렬 상태 없음 → 사용자 수동 정렬 TCP ({cur0[0]:.2f},{cur0[1]:.2f}) rz{cur0[5]:+.2f} 를 정렬 완료로 간주")
    if T.get("x") is not None:                                  # 계산 목표 대비 실제 하강 자리 차이를 항상 기록(반복되면 보정값 후보)
        nd = (cur0[0] - T["x"], cur0[1] - T["y"], HG.wrap_deg(cur0[5] - T["rz"]))
        rec = {"made": time.strftime("%Y-%m-%d %H:%M:%S"), "color": color, "by": A.get("by", "align"), "target": [T["x"], T["y"], T["rz"]],
               "descend_tcp": [round(v, 3) for v in cur0], "descend_minus_target": [round(v, 3) for v in nd], "precorr": T.get("precorr"), "dyaw": T.get("dyaw")}
        try:
            open(os.path.join(STATE, "nudge_log.jsonl"), "a").write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception: pass
        log(f"  하강 자리 − 계산 목표: dx {nd[0]:+.2f} dy {nd[1]:+.2f} mm drz {nd[2]:+.2f}° ({A.get('by', 'align')}) → nudge_log")
    if col != color:
        raise Gate(f"하강 색 불일치: 정렬된 벽은 {col}, 요청은 {color}")
    age = time.time() - float(A.get("made_t") or 0.0)
    if age > ALIGN_MAX_AGE_S:
        raise Gate(f"정렬 후 {age/60:.0f}분 경과(> {ALIGN_MAX_AGE_S//60}분) — 베이스가 움직였을 수 있음, 사이클 다시(재정렬)")
    cur = PC.st()["tcp"]
    # ★9/8: 상한은 '정렬 높이 + 여유 3mm' 다. 정렬 높이가 색마다 다르므로(HOVER_DZ) 그걸 따라간다.
    #   외벽 4색은 HOVER_DZ 기본 85.0 → 상한 88 로 예전과 동일. 내벽만 102+3=105 가 된다.
    z_hi = T["z_seat"] + HOVER_DZ.get(color, 85.0) + 3.0
    if not (T["z_seat"] - 1.0 <= cur[2] <= z_hi):        # 막힘 후퇴 뒤 재하강·안착 높이(사용자 조그) 허용
        raise Gate(f"지금 z{cur[2]:.0f} 가 z{T['z_seat']-1:.0f}~{z_hi:.0f} 밖 — 정렬 후 움직였음, 사이클 다시")
    dxy = math.hypot(cur[0] - A["x"], cur[1] - A["y"]); drz = abs(HG.wrap_deg(cur[5] - A["rz"]))
    if dxy > ALIGN_GATE_MM or drz > ALIGN_GATE_DEG:
        # 14:33 실기: 랙 비틀림(죠 안 +0.99°)을 새카메라 1점 정렬이 못 봐서 사용자가 2.35mm/0.32° 조그 → 설계대로 사용자 판단이 최종.
        # 거부 대신 '사용자 후보정' 으로 기록하고 지금 TCP 로 하강. (정렬 TCP 는 align_tcp 로 보존 → nudge_log 에서 카메라 vs 사용자 비교)
        if dxy > 8.0 or drz > 2.0:
            raise Gate(f"정렬 TCP 와 {dxy:.1f}mm/{drz:.1f}° 차이 — 너무 큼(8mm/2°), 사이클 다시")
        log(f"  ⚠ 정렬 후 사용자 조그 ΔXY {dxy:.2f}mm(dx {cur[0]-A['x']:+.2f}, dy {cur[1]-A['y']:+.2f}) Δrz {HG.wrap_deg(cur[5]-A['rz']):+.2f}° → 사용자 자리로 하강(기록)")
        try:
            rec = {"made": time.strftime("%Y-%m-%d %H:%M:%S"), "color": color, "by": "user_after_align",
                   "align_tcp": [A["x"], A["y"], A["rz"]], "descend_tcp": [round(v, 3) for v in cur],
                   "user_minus_align": [round(cur[0] - A["x"], 3), round(cur[1] - A["y"], 3), round(HG.wrap_deg(cur[5] - A["rz"]), 3)],
                   "grasp": S.get("grasp")}
            open(os.path.join(STATE, "nudge_log.jsonl"), "a").write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception: pass
        A = dict(A, x=cur[0], y=cur[1], rz=cur[5], z=cur[2], by="user_after_align", made_t=time.time())
        with LOCK: S["align"] = A
    else:
        log(f"  하강 게이트 OK: ΔXY {dxy:.2f}mm Δrz {drz:.2f}° 정렬 {age/60:.1f}분 전")
    return T, A, cur


def seat_check(color=None):
    """안착 판정. ①seat_verify(손목캠 밑판 사각형 — z355 에선 원리적으로 못 봄, 14:03 실기 2회 unknown)
    ②새카메라: 안착 기준 사진(pose_refs/<색>.json seat.cams.newcam.dots, 12:35 사용자 손 안착)의 기둥 점 자리와 지금 기둥 점 비교.
    카메라가 손목에 붙어 있으므로 '안착 TCP ↔ 베이스' 관계가 맞으면 기둥이 같은 px 에 온다. 실패해도 예외 대신 unknown."""
    try:
        import seat_verify as SV
        seat = SV.verify()
    except Exception as ex:
        seat = {"state": "unknown", "why": str(ex)}
    seat = {k: v for k, v in (seat or {}).items() if not str(k).startswith("_")}
    if str(seat.get("state", "")).startswith("seated") or not color:
        return seat
    try:
        pr = jload(os.path.join(STATE, "pose_refs", f"{color}.json")) or {}
        cams = ((pr.get("seat") or {}).get("cams") or {})
        wall_c = "red" if color == "red_s" else color
        # 20:0x: 노랑 자리에선 새카메라가 기둥을 못 봄 → 기준이 있는 카메라를 쓴다(newcam 우선, 없으면 wrist).
        src = next((k for k in ("newcam", "wrist") if [p for c, lst in ((cams.get(k) or {}).get("dots") or {}).items()
                                                       if c != wall_c for p in (lst or []) if len(p) >= 2]), None)
        if not src:
            seat["why"] = str(seat.get("why")) + " | 안착 기준(newcam/wrist) 없음"; return seat
        dots = (cams.get(src) or {}).get("dots") or {}
        # 20:49 실기: 화면 가장자리에 걸친 점은 잘려서 중심이 30px 씩 튄다(빨강 (15,404)) → 기준에서 제외.
        EDGE = 60
        refs = [(c, p[0], p[1]) for c, lst in dots.items() if c != wall_c for p in (lst or [])
                if len(p) >= 2 and EDGE <= p[0] <= 1280 - EDGE and EDGE <= p[1] <= 720 - EDGE]
        img = HA.grab(src)
        res = []
        for c, rx, ry in refs:
            now = HA.base_feats(img, [], src) if src == "wrist" else HA._blobs(img, c, 20, src)
            now = [(x, y, a) for cc, x, y, a in now if cc == c] if src == "wrist" else now
            cand = [q for q in now if math.hypot(q[0] - rx, q[1] - ry) <= 60]
            if cand:
                q = min(cand, key=lambda q: math.hypot(q[0] - rx, q[1] - ry)); res.append((c, round(rx), round(ry), round(q[0]), round(q[1]), math.hypot(q[0] - rx, q[1] - ry)))
        if len(res) < max(1, min(2, len(refs))):
            seat["why"] = str(seat.get("why")) + f" | {src}: 기준 기둥 {[(c, round(x), round(y)) for c, x, y in refs]} 근처(60px)에 점 부족({len(res)})"; return seat
        worst = max(r[5] for r in res)
        desc = " ".join(f"{c}({rx},{ry})→({x},{y}) {d:.1f}px" for c, rx, ry, x, y, d in res)
        if worst <= SEAT_NEWCAM_TOL_PX:
            return {"state": f"seated_{src}", "why": f"{src} 기둥 점 {len(res)}개가 안착 기준 자리와 일치: {desc} (허용 {SEAT_NEWCAM_TOL_PX}px)", "px": worst}
        return {"state": "unknown", "why": f"{src} 기둥 점이 안착 기준에서 {worst:.1f}px (> {SEAT_NEWCAM_TOL_PX}): {desc}", "px": worst}
    except Exception as ex:
        seat["why"] = str(seat.get("why")) + f" | 새카메라 판정 실패: {ex}"
        return seat


def promote_seat_ref(color):
    """안착 성공 시 새카메라 안착 기준(pose_refs/<색>.json seat.cams.newcam.dots)도 지금 프레임으로 승격.
    14:28 실기: 12:35 손 안착 사진 기준이 슬롯 승격마다 멀어져 11.2px(허용 12) 까지 감 → 성공 자리를 따라가게."""
    try:
        f = os.path.join(STATE, "pose_refs", f"{color}.json")
        pr = jload(f) or {}
        # 9/7: 노랑·red_s 는 안착 자세에서 새카메라가 기둥을 못 본다 → 그 색의 정렬 카메라를 쓰고, 비면 다른 쪽으로.
        wall_c = "red" if color == "red_s" else color
        order = list(ALIGN_ROLES.get(color) or ("newcam", "wrist"))
        order += [x for x in ("newcam", "wrist") if x not in order]
        src, dots = None, None
        for cand in order:
            img = HA.grab(cand)
            d = {c: [[float(q[0]), float(q[1]), float(q[2])] for q in HA._blobs(img, c, 20, cand)
                     if 300 <= q[0] <= 1220 and 60 <= q[1] <= 660] for c in ("blue", "yellow", "red")}   # x<300 은 새카메라 왼쪽 케이블 반사 구역
            # ★9/7 19:1x·19:36 실기: 승격된 안착 기준에 빨강이 4~6개 들어와(밑판 가장자리·반사) 판정이 늘 실패했다.
            #   안착 자세에서 한 꼭짓점이 주는 점은 색당 1~2개다 → 3개 이상인 색은 반사로 보고 통째로 버린다.
            for c in list(d):
                if len(d[c]) > 2:
                    log(f"  (안착 기준: {c} {len(d[c])}개 — 반사로 보고 제외)")
                    d[c] = []
            if sum(len(v) for c, v in d.items() if c != wall_c) >= 2:      # 벽 색 말고 기둥 점 2개 이상
                src, dots = cand, d; break
        if not dots:
            log("  ⚠ 안착 기준 촬영 실패: 어느 카메라에도 기둥 점 2개 이상이 없음"); return False   # 가장자리 점은 잘려 중심이 튐 → 저장 단계에서 제외
        seat = pr.setdefault("seat", {}); cams = seat.setdefault("cams", {})
        prev = cams.get(src)
        hist = ([{k: v for k, v in prev.items() if k != "history"}] + list((prev or {}).get("history") or []))[:3] if prev else []
        cams[src] = {"dots": dots, "made": time.strftime("%Y-%m-%d %H:%M"), "note": "안착 성공 사이클에서 자동 승격", "history": hist}
        seat["tcp"] = PC.st()["tcp"]
        jsave(f, pr)
        log(f"  ✅ 안착 기준 승격({src}): " + " ".join(f"{c}{[(round(x), round(y)) for x, y, a in lst]}" for c, lst in dots.items() if lst))
    except Exception as ex:
        log(f"  ⚠ 안착 기준 승격 실패: {ex}")


def promote_slot_ref(color, seat_tcp, B, note="자동 승격: 안착 성공 TCP + 이번 사이클 베이스"):
    """③안착 성공한 TCP + 이번 사이클 베이스 측정을 slot_ref 로 승격. 이전 값은 history(최대 SLOT_HISTORY_MAX) 로 보존."""
    if not B:
        log("  ⚠ 슬롯 기준 승격 생략: 이번 사이클 베이스 측정이 없음"); return False
    d = jload(F["slot"]) or {}
    prev = d.get(color)
    hist = []
    if prev:
        hist = [{k: v for k, v in prev.items() if k != "history"}] + list(prev.get("history") or [])
    d[color] = {"seat_tcp": [round(float(v), 3) for v in seat_tcp], "base": {"x": B["x"], "y": B["y"], "yaw": B["yaw"]},
                "base_rms": B.get("rms"), "made": time.strftime("%Y-%m-%d %H:%M"), "note": note, "history": hist[:SLOT_HISTORY_MAX]}
    jsave(F["slot"], d)
    log(f"✅ 슬롯 기준 승격 [{color}] seat {[round(v, 1) for v in seat_tcp[:3]]} rz {seat_tcp[5]:+.2f} ↔ 베이스 ({B['x']:.2f},{B['y']:.2f},{B['yaw']:+.3f}°)  history {len(hist[:SLOT_HISTORY_MAX])}건")
    return True


def side_seat_after(color):
    """★9/8 신설: 벽을 놓고 로봇이 SAFE 로 물러난 뒤 측면캠으로 확인한다.
    측면캠은 고정이라 로봇 자세와 무관하고 벽 자체를 직접 본다 — 안착 높이에서 기둥을 못 보는
    red_s·red_in 도 판정할 수 있다. **덧붙이는 검사라 실패해도 사이클에 영향을 주지 않는다**(로그만).
    기준이 없으면 이번 자리로 학습하고, 있으면 판정한다."""
    with LOCK:
        pre = S.get("side_pre")
    try:
        have = bool((json.load(open(SSEAT.REF)) if os.path.exists(SSEAT.REF) else {}).get(color))
    except Exception:
        have = False
    try:
        if not have:
            if not pre:
                log("  (측면캠 기준 학습 생략: 꽂기 전 스냅샷 없음)"); return
            r = SSEAT.save_ref(color, pre)
            log(f"  ✅ 측면캠 안착 기준 학습 [{color}]: 벽 점 {r['wall']}개 {r['wall_px']} · 기준점 {r['anchors']}개")
        else:
            v = SSEAT.verify(color)
            with LOCK: S["side_seat"] = v
            mark = "✅" if v.get("state") == "seated_side" else ("🛑" if v.get("state") == "not_seated" else "  ")
            log(f"  {mark} 측면캠 판정 [{color}]: {v.get('state')} — {v.get('why')}")
    except Exception as ex:
        log(f"  (측면캠 판정 건너뜀: {ex})")


def release_and_rise(color, rr):
    set_stage("3 RELEASE", color=color)
    log(f"  그리퍼 열기 → {PC.gripper(rr.get('grip_open', GRIP_OPEN))}")
    cur = PC.st()["tcp"]
    PC.speed(SPD_SEAT); move([cur[0], cur[1], cur[2] + 30] + list(cur[3:]), tag="수직 +30")
    PC.speed(SPD_DESC); move([cur[0], cur[1], HOVER_Z] + list(cur[3:]), tag="호버 z478")
    PC.speed(spd_move()); move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="SAFE"); PC.speed(1)
    side_seat_after(color)


def stage_descend(color):
    """③하강 → 안착 판정. seated: promote_ref + 슬롯 기준 승격 + 개방 + 상승.
    아니면 ①그리퍼 유지·그 자리 정지(SEAT FAIL) → [⛔중단] 또는 [▶계속](사용자 육안 확인 → 개방·상승, 승격 없음). 반환: seated."""
    T, A, cur = descend_gate(color)
    set_stage("3 DESCEND", color=color)
    rr = (jload(F["rack"]) or {}).get(color) or {}
    # ★9/10 사고: 슬롯 기준 파일의 안착 z 를 고쳤는데 **돌고 있던 사이클이 메모리의 옛 값(z440)을 들고 있어**
    #   z440 에서 "안착 z 도달"로 판정하고 그리퍼를 열어 벽을 85mm 위에서 떨어뜨렸다.
    #   → 하강 직전에 **파일에서 다시 읽고**, 지금 높이와 너무 가까우면(=내려갈 거리가 없으면) 거부한다.
    _ref = (jload(F["slot"]) or {}).get(color) or {}
    _zs_file = (_ref.get("seat_tcp") or [None, None, None])[2]
    if _zs_file is not None and abs(_zs_file - T["z_seat"]) > 0.5:
        log(f"  ⚠ 안착 z 갱신: 메모리 {T['z_seat']:.2f} → 파일 {_zs_file:.2f} (사이클 중 슬롯 기준이 바뀜)")
        T["z_seat"] = float(_zs_file)
    z_now = PC.st()["tcp"][2]
    if z_now - T["z_seat"] < 20.0:
        raise Gate(f"안착 z {T['z_seat']:.1f} 가 지금 높이 z{z_now:.1f} 와 {z_now - T['z_seat']:.1f}mm 밖에 차이 안 남 — "
                   f"슬롯 기준의 안착 z 가 잘못됐을 수 있다(하강 거부). slot_ref 확인 필요")
    z_tgt = T["z_seat"]
    if DESCEND_STOP_Z is not None and DESCEND_STOP_Z > z_tgt:
        log(f"  ⚠ 하강 정지 높이 z{DESCEND_STOP_Z:.0f} 적용 — 안착 z{z_tgt:.0f} 까지 내려가지 않는다(사용자 확인용)")
        z_tgt = DESCEND_STOP_Z
    apply_speed_profile()                     # ★9/11 집 타입별 저속/채널 속도
    if color in JAM_FREE:
        # ★9/8 사용자 확인 "안 닿는 거 내가 확인했고 그냥 내리면 돼" — 내벽만. 외벽 4색은 종전 감시 그대로.
        log(f"  ⚠ [{color}] 막힘 감시 없이 수직 하강(사용자가 비접촉 확인) — 외벽 경로와 분리")
        PC.descend_plain(color, cur[0], cur[1], [180.0, 0.0, cur[5]], z_tgt, rr.get("grip_close", 13))
    else:
        PC.descend_monitored(color, cur[0], cur[1], [180.0, 0.0, cur[5]], z_tgt, rr.get("grip_close", 13))   # 부품(3mm 단계·벽점 밀림·놓침·정체 → stop+25mm)
    set_stage("3 SEAT CHECK", color=color)
    seat = seat_check(color)
    seated = str(seat.get("state", "")).startswith("seated")
    with LOCK: S["seat"] = seat
    log(f"  안착 판정: {seat}")
    auto_done = False                    # ★unknown 을 자동 완료로 넘겼는가(= run4 를 계속 이어가도 되는가)
    if not seated:
        _unknown = str(seat.get("state", "")) == "unknown"
        if seat_unknown_auto() and _unknown:
            auto_done = True
            # 하강 버튼 = 사람의 확인. 막힘 없이 목표 z 까지 갔으므로 멈추지 않고 개방·상승한다(기준 승격 없음).
            log(f"  안착 판정 불가(unknown) — 목표 z 까지 막힘 없이 도달 → 완료로 보고 개방·상승 (기준 승격 없음)")
            log(f"    (사유: {seat.get('why')})")
        else:
            set_stage("SEAT FAIL", color=color)
            log(f"🛑 안착 미확인({seat.get('state')}: {seat.get('why')}) — 그리퍼 유지, 그 자리 정지. "
                "[⛔중단]=이대로 정지(벽은 사용자가 처리) / [▶계속]=사용자가 안착을 육안 확인 → 그리퍼 열고 상승(기준 승격 없음)")
            wait_user("SEAT FAIL — 그리퍼 유지, 사용자 판단 대기: [⛔중단] 또는 [▶계속](안착 육안 확인 시)")
            log("  사용자 [▶계속]: 안착 육안 확인으로 간주 → 개방·상승 (기준 승격 없음)")
        if not (((jload(os.path.join(STATE, "pose_refs", f"{color}.json")) or {}).get("seat") or {}).get("cams")):
            log("  (안착 기준이 아직 없는 색 → 지금 앉은 자리로 최초 촬영: 로봇은 움직이지 않음)")
            promote_seat_ref(color)          # 부트스트랩 1회. 이후는 seated 성공 시에만 갱신
        with LOCK: S["seat"] = dict(seat, user_override=True)
    else:
        at = PC.st()["tcp"]
        if A.get("by") == "user_after_align":                       # 정렬 후 사용자가 조그한 사이클: 카메라 기준(z440·안착)은 승격하지 않음(정렬 자리≠성공 자리)
            log("  (카메라 기준 승격 생략: 정렬 후 사용자 조그 — 슬롯 기준만 사용자 자리로 승격)")
        else:
            if not HA.promote_ref(color):                           # 성공 사이클 정렬 상태 → 다음 z440 기준(측정된 카메라 전부)
                log("  (z440 기준 승격 없음: 이번 정렬의 마지막 측정이 없음)")
            promote_seat_ref(color)                                 # 새카메라 안착 기준도 성공 자리로
        with LOCK: B = S.get("base")
        promote_slot_ref(color, [at[0], at[1], T["z_seat"], at[3], at[4], at[5]], B)   # z 는 티칭값 유지(접촉 조기정지 z 승격 시 위로 표류 방지)
    release_and_rise(color, rr)
    set_stage("DONE" if seated else "DONE (안착 판정 불가·목표 z 도달로 완료)", color=color)
    # ★9/8 사용자 지시("하강만 내가 누를게 — 멈추는 거 없이 4벽 연속"): 목표 z 까지 막힘 없이 내려간 뒤
    #   판정만 불가(unknown)인 경우는 run4 를 이어간다. 판정이 **not_seated** 로 나오면 종전대로 멈춘다.
    #   기준 승격은 위에서 seated 일 때만 하므로, 이어간다고 해서 unknown 이 기준으로 올라가지는 않는다.
    return seated or auto_done


def stage_descend_reteach(color):
    """③' 하강 → 안착 판정 → seated 면 **든 채로** z_seat+85 로 올려 z440 기준(손목·새카메라)을 이 자리에서 재촬영 → 다시 내려 놓고 상승.
    13:4x 사용자 지시: 정렬 루프가 안 돈 채 사용자가 육안으로 맞춘 자리 → 성공하면 그 관계를 기준 사진으로 박아 다음부터 카메라가 재현."""
    T, A, cur = descend_gate(color)
    rr = (jload(F["rack"]) or {}).get(color) or {}
    if cur[2] <= T["z_seat"] + 2.0:
        log(f"  이미 안착 높이 z{cur[2]:.1f}(사용자 조그) → 하강 생략, 안착 판정부터")
    else:
        set_stage("3 DESCEND", color=color)
        apply_speed_profile()
        PC.descend_monitored(color, cur[0], cur[1], [180.0, 0.0, cur[5]], T["z_seat"], rr.get("grip_close", 13))
    set_stage("3 SEAT CHECK", color=color)
    seat = seat_check(color)
    seated = str(seat.get("state", "")).startswith("seated")
    with LOCK: S["seat"] = seat
    log(f"  안착 판정: {seat}")
    if not seated:
        set_stage("SEAT FAIL", color=color)
        log(f"🛑 안착 미확인({seat.get('state')}: {seat.get('why')}) — 그리퍼 유지, 그 자리 정지. "
            "[⛔중단]=정지 / [▶계속]=사용자 육안 확인 → 슬롯 기준만 갱신하고 놓기(15:35 사고: 판정 unknown 인 벽을 들어올려 재촬영하다 베이스 딸려가 재하강 끼임 → 재촬영 생략)")
        wait_user("SEAT FAIL — 그리퍼 유지: [⛔중단] 또는 [▶계속](안착 육안 확인 시 → 재촬영 없이 놓기)")
        log("  사용자 [▶계속]: 안착 육안 확인 → 재촬영 생략, 슬롯 기준 갱신 후 놓기")
        if not (((jload(os.path.join(STATE, "pose_refs", f"{color}.json")) or {}).get("seat") or {}).get("cams")):
            log("  (안착 기준이 아직 없는 색 → 지금 앉은 자리로 최초 촬영: 로봇은 움직이지 않음)")
            promote_seat_ref(color)          # 부트스트랩 1회만. 이후 사이클은 seated 성공 시에만 갱신
        with LOCK: S["seat"] = dict(seat, user_override=True); B = S.get("base")
        at = PC.st()["tcp"]
        promote_slot_ref(color, [at[0], at[1], T["z_seat"], at[3], at[4], at[5]], B or jload(F["base_last"]), note="사용자 육안 안착(판정 unknown) → 슬롯 기준 갱신, 카메라 기준 재촬영 생략")
        release_and_rise(color, rr)
        set_stage("DONE (안착 미확인·사용자 개방·재촬영 생략)", color=color)
        return False
    at = PC.st()["tcp"]
    with LOCK: B = S.get("base")
    if not B:
        B = jload(F["base_last"]); log(f"  베이스: base_last({(B or {}).get('made')}) 사용")
    seat_tcp = [at[0], at[1], T["z_seat"], at[3], at[4], at[5]]
    promote_slot_ref(color, seat_tcp, B, note="사용자 육안 정렬 자리에서 안착 성공 → 슬롯 기준 갱신")
    promote_seat_ref(color)
    _pending_seat[color] = list(seat_tcp); jsave(_PENDING_F, _pending_seat)
    set_stage("3' RETEACH z440", color=color)
    zh = T["z_seat"] + HOVER_DZ.get(color, 85.0)   # ★9/10: 85 고정 → 색별 정렬 높이
    PC.speed(SPD_SEAT); move([at[0], at[1], zh] + list(at[3:]), tag=f"든 채 z{zh:.0f} (기준 재촬영)")
    time.sleep(0.8)
    for src in ("wrist", "newcam"):
        try:
            teach_hover(color, src)
        except Exception as ex:
            log(f"  ⚠ z440 기준 재촬영 실패 [{src}]: {ex}")
    # 15:35 사고: 들어올릴 때 베이스가 딸려 움직이면 같은 XY 재하강은 기둥 꼭대기를 찍음 → 재하강 전 정렬 한 번 더(방금 찍은 기준으로)
    set_stage("3' RE-ALIGN", color=color)
    roles = ALIGN_ROLES.get(color); srcs = list(roles) if roles else ALIGN_SRCS.get(color)
    HA.align(color, srcs=srcs, roles=roles)
    at = PC.st()["tcp"]
    log(f"  재하강 전 정렬 완료 TCP ({at[0]:.2f},{at[1]:.2f}) rz{at[5]:+.2f}")
    set_stage("3' RE-DESCEND", color=color)
    apply_speed_profile()
    PC.descend_monitored(color, at[0], at[1], [180.0, 0.0, at[5]], T["z_seat"], rr.get("grip_close", 13))
    seat2 = seat_check(color); log(f"  재안착 판정: {seat2}")
    release_and_rise(color, rr)
    set_stage("DONE (기준 재촬영)", color=color)
    return True


# ------------------------------------------------------------------ 사이클
def run_cycle(color, teach_rack=False):
    global _logf
    if _logf:
        try: _logf.close()
        except Exception: pass
    _logf = open(os.path.join(STATE, "logs", f"cycle_{color}_{time.strftime('%m%d_%H%M%S')}.log"), "a")
    with LOCK:
        S.update(color=color, err=None, base=None, rack=None, grasp=None, target=None, align=None, seat=None)
    B = stage_base(color)                                          # 빈손 베이스 재측정(벽마다)
    slot_target(color, B)                                          # 기준 없으면 여기서 정지(픽 전에 안다)
    G = stage_rack(color, teach_rack)
    T = slot_target(color, B, G)
    stage_carry_hover(color, T)
    set_stage("WAIT DESCEND", wait="[⬇ 하강] 버튼 (x·y·yaw 확인 후)", color=color)


def run_held(color):
    """든 채로 3단계부터(13:27 실기: 파지 게이트 정지 후 사용자 '잘 잡았다' → 재파지 없이 운반·z440 정렬·하강 대기).
    필요: 이번 사이클 베이스(base_last, 20분 이내) + 파지 편차(메모리 S 또는 재시작 전 저장한 last_state) + 그리퍼가 벽을 물고 있음."""
    with LOCK:
        G = S.get("grasp"); S.update(color=color, err=None, target=None, align=None, seat=None)
    if color in NO_GRASP_SIG:
        # ★9/10: 짧은 벽이라 들어올린 자세에서 점이 안 보인다 → 편차를 잴 수 없다. 선보정 없이 진행(사용자 지시).
        G = None
        log(f"  (파지 편차 생략 [{color}] — 짧은 벽, 선보정 없음)")
    elif not G:
        # 9/7 사고: 서버 재시작으로 이번 파지 기록이 비면 어제 파랑 스냅샷(2점)을 물려받아 red_s 에 엉뚱한 선보정이 들어갔다.
        #   같은 색·같은 방식일 때만 재사용하고, 아니면 지금 다시 잰다.
        ls = jload(os.path.join(STATE, "last_state_1327.json")) or {}
        cand = ls.get("grasp")
        if cand and ls.get("color") == color:
            G = cand; log(f"  파지 편차: 재시작 전 저장분 사용(같은 색) {G}")
        else:
            rr0 = (jload(F["rack"]) or {}).get(color) or {}
            rd = (S.get("rack") or {}).get("dang")
            g2, info2 = PC.grasp_measure(color, rack_dang=rd)
            if g2 is None and "든 벽 점 0" in str(info2) and PC.held_wall_dots_expo(color):
                g2, info2 = PC.grasp_measure(color, rack_dang=rd)
            if g2 is None:
                raise Gate(f"파지 편차 재측정 실패: {info2} — [▶사이클] 로 처음부터")
            G = {"ok": True, "across_mm": info2["across_mm"], "along_mm": info2["along_mm"],
                 "dang": info2["dang"], "how": info2["how"], "grip": PC.grip_read()}
            log(f"  파지 편차 재측정({G['how']}): 가로 {G['across_mm']:+.2f} 길이 {G['along_mm']:+.2f}mm 각 {G['dang']:+.2f}°")
    if not G and color not in NO_GRASP_SIG:
        raise Gate("파지 편차 기록 없음 — [▶사이클] 로 처음부터")
    B = jload(F["base_last"])
    if not B:
        raise Gate("베이스 측정 없음 — [▶사이클] 로 처음부터")
    age = time.time() - time.mktime(time.strptime(B["made"], "%Y-%m-%d %H:%M:%S"))
    if age > 20 * 60:
        raise Gate(f"베이스 측정이 {age/60:.0f}분 전 — [▶사이클] 로 처음부터(빈손 재측정)")
    rr = (jload(F["rack"]) or {}).get(color) or {}
    g = PC.grip_read()
    if g.isdigit() and int(g) <= rr.get("grip_close", 13):
        # 9/7: red_s 는 얇아 물어도 그리퍼 값이 닫힘값 그대로(8→8) — 손목캠 든 벽 점으로 확인(descend_monitored 와 같은 규칙)
        w = PC.held_wall_dots_expo(color)
        if not w and color not in NO_GRASP_SIG:
            raise Gate(f"그리퍼 {g} ≤ 닫힘값 + 손목캠 든 벽 점 0 — 벽을 물고 있지 않음")
        if not w:
            # ★9/10 사용자 지시: 짧은 벽은 들어올린 자세에서 점이 원리적으로 안 보인다 → 이 확인을 못 한다.
            log(f"  ⚠ [{color}] 파지 확인 불가(그리퍼 {g} = 닫힘값, 든 벽 점 0) — 짧은 벽이라 검사 생략, 사용자 확인에 의존")
        else:
            log(f"  (그리퍼 {g} = 닫힘값이지만 손목캠 든 벽 점 {len(w)}개 → 물고 있음)")
    with LOCK: S["base"] = B; S["grasp"] = G
    if G:
        log(f"══ 든 채로 3단계부터 [{color}]: 베이스 {B['made']} 파지 편차 가로 {G['across_mm']:+.2f} 길이 {G['along_mm']:+.2f} 각 {G['dang']:+.2f}°")
    else:
        log(f"══ 든 채로 3단계부터 [{color}]: 베이스 {B['made']} (파지 편차 생략)")
    T = slot_target(color, B, G)
    stage_carry_hover(color, T)
    set_stage("WAIT DESCEND", wait="[⬇ 하강] 버튼 (x·y·yaw 확인 후)", color=color)


def goto_calc_target(color):
    """★9/10 기준 재촬영용 — 정렬이 옮긴 자리에서 '계산 목표'(슬롯 기준 + 지금 베이스) XY 로 되돌린다.

    호버 기준을 **정렬 편향이 섞인 자리**가 아니라 **정답 자리**에서 찍기 위한 것.
    (red_s 는 기준을 빈 베이스에서 찍어 매 사이클 dy +2.1mm 로 끌려갔다 → 편향을 구조적으로 0 으로.)
    z·rz·그리퍼는 건드리지 않는다. 벽은 아직 공중이라 베이스에 닿을 위험이 없다."""
    with LOCK:
        T = S.get("target"); col = S.get("color")
    if not T or T.get("x") is None:
        raise Gate("계산 목표 없음 — 사이클이 WAIT DESCEND 인 상태에서만 쓴다")
    if col != color:
        raise Gate(f"색 불일치: 정렬된 벽은 {col}, 요청은 {color}")
    cur = PC.st()["tcp"]
    zh = T["z_seat"] + HOVER_DZ.get(color, 85.0)
    if abs(cur[2] - zh) > 3.0:
        raise Gate(f"지금 z{cur[2]:.0f} 가 정렬 높이 z{zh:.0f}±3 밖 — 호버에서만")
    d = math.hypot(cur[0] - T["x"], cur[1] - T["y"])
    if d > 8.0:
        raise Gate(f"계산 목표까지 {d:.1f}mm — 너무 멀다(8mm 상한), 사이클 다시")
    set_stage("→ 계산 목표", color=color)
    log(f"  계산 목표 복귀: ({cur[0]:.2f},{cur[1]:.2f}) → ({T['x']:.2f},{T['y']:.2f})  Δ {d:.2f}mm")
    PC.speed(SPD_SEAT)
    move([T["x"], T["y"], cur[2], cur[3], cur[4], cur[5]], tag="계산 목표 복귀")
    at = PC.st()["tcp"]
    log(f"  ✓ 복귀 TCP ({at[0]:.2f},{at[1]:.2f}) 잔차 {math.hypot(at[0]-T['x'], at[1]-T['y']):.2f}mm "
        f"— 이 자리에서 [z440 기준 저장] 후 하강")
    with LOCK:
        A = S.get("align")
        if A:
            A.update(x=at[0], y=at[1], rz=at[5], by="calc_target")
    set_stage("WAIT DESCEND (계산 목표 복귀)", color=color)


def align_here(color):
    """든 채 z440 근처(±12)에서 정렬만 다시(SAFE 왕복 없음): z_seat+85 로 수직 이동 → ALIGN_SRCS 카메라로 정렬 → WAIT DESCEND."""
    ref = (jload(F["slot"]) or {}).get(color)
    if not ref:
        raise Gate(f"{color} 슬롯 기준 없음")
    zs = ref["seat_tcp"][2]; zh = zs + HOVER_DZ.get(color, 85.0)   # ★9/10: 85 고정 → 색별 정렬 높이(내벽 100)
    cur = PC.st()["tcp"]
    if abs(cur[2] - zh) > 12.0:
        raise Gate(f"지금 z{cur[2]:.0f} — z{zh:.0f}±12 에서만(든 채)")
    g = PC.grip_read(); rr = (jload(F["rack"]) or {}).get(color) or {}
    if g.isdigit() and int(g) <= rr.get("grip_close", 13):
        # 9/7: red_s 는 얇아 물어도 그리퍼 값이 닫힘값 그대로(8→8) — 손목캠 든 벽 점으로 확인
        if not PC.held_wall_dots_expo(color):
            if color not in NO_GRASP_SIG:
                raise Gate(f"그리퍼 {g} ≤ 닫힘값 + 손목캠 든 벽 점 0 — 벽을 물고 있지 않음")
            log(f"  ⚠ [{color}] 파지 확인 불가 — 짧은 벽이라 검사 생략, 사용자 확인에 의존")
        else:
            log(f"  (그리퍼 {g} = 닫힘값이지만 손목캠 든 벽 점 있음 → 물고 있음)")
    with LOCK:
        S.update(color=color, err=None, align=None, seat=None)
        if not S.get("target"):
            S["target"] = {"x": None, "y": None, "rz": None, "z_seat": zs, "user_fallback": True}
    set_stage("2' HOVER ALIGN(재)", color=color)
    PC.speed(SPD_SEAT); move([cur[0], cur[1], zh] + list(cur[3:]), tag=f"z{zh:.0f}")
    A = {"done": False}
    with LOCK: S["align"] = A
    roles = ALIGN_ROLES.get(color); srcs = list(roles) if roles else ALIGN_SRCS.get(color)
    try:
        _, per, _ = HA.check(color)
        for D in per.values(): log(f"  (참고) {HA.fmt(D)}")
    except Exception as ex: log(f"  (참고 측정 실패: {ex})")
    log(f"  정렬 카메라: {srcs or '가용 전부'} 역할 {roles or 'both'}")
    HA.align(color, srcs=srcs, roles=roles)
    post_align_offset(color)
    c = PC.st()["tcp"]
    A.update(done=True, x=c[0], y=c[1], rz=c[5], z=c[2], made_t=time.time(), made=time.strftime("%H:%M:%S"), by="align")
    with LOCK: S["align"] = A
    log(f"  ✅ 정렬 완료 TCP ({c[0]:.2f},{c[1]:.2f}) rz{c[5]:+.2f}")
    set_stage("WAIT DESCEND", wait="[⬇ 하강] 버튼 (x·y·yaw 확인 후)", color=color)


def run_multi(order=None):
    order = order or run_order()
    """④4벽 연속. 벽마다 run_cycle(빈손 베이스 재측정 포함) → [⬇ 하강] 버튼 대기 → stage_descend.
    어느 벽이든 Gate/Abort/예외/안착 미확인이면 그 자리에서 전체 중단(다음 벽 진행 없음)."""
    order = [c for c in order if c in COLORS]
    with LOCK:
        S["run4"] = {"order": order, "idx": 0, "done": [], "active": True}
    log(f"══ run4 시작: {' → '.join(order)} (벽마다 빈손 베이스 재측정, 하강은 매번 버튼)")
    try:
        for i, color in enumerate(order):
            with LOCK: S["run4"]["idx"] = i
            if i > 0:
                g = PC.grip_read(); gc = ((jload(F["rack"]) or {}).get(order[i - 1]) or {}).get("grip_close", 13)
                if g.isdigit() and int(g) <= gc:
                    raise Gate(f"run4: 그리퍼 {g} ≤ 닫힘값 {gc} — 빈손이 아님, [{color}] 진행 금지")
            log(f"══ run4 {i+1}/{len(order)} [{color}] — 빈손 베이스 재측정부터")
            run_cycle(color)
            wait_user(f"[⬇ 하강] 버튼 (x·y·yaw 확인 후) — run4 {i+1}/{len(order)} {color}")
            if not stage_descend(color):
                raise Gate(f"run4 중단: [{color}] 안착 미확인(사용자 개방) — 남은 {order[i+1:]} 진행 안 함")
            with LOCK: S["run4"]["done"].append(color)
        set_stage("RUN4 DONE")
        log(f"✅ run4 완료: {order}")
    finally:
        with LOCK:
            if S.get("run4"): S["run4"]["active"] = False


# ------------------------------------------------------------------ 티칭 버튼
_PENDING_F = os.path.join(STATE, "pending_seat.json")      # 1/2 는 서버 재시작에도 살아남게 파일로(12:48 재시작 때 유실 경험)
_pending_seat = jload(_PENDING_F) or {}


def teach_slot_tcp(color):
    """1/2: 로봇이 손 안착 벽을 물고 있는 지금 TCP 저장."""
    cur = PC.st()["tcp"]; _pending_seat[color] = list(cur); jsave(_PENDING_F, _pending_seat)
    log(f"  슬롯 기준 1/2 [{color}] 안착 TCP {[round(v, 2) for v in cur]} (다음: 놓고 관측자세 → 2/2)")


def teach_slot_base(color):
    """2/2: 관측자세에서 베이스 측정 → 1/2 의 TCP 와 페어링. ArUco 기준이 없으면 지금 마커로 생성."""
    seat = _pending_seat.get(color)
    if not seat:
        raise Gate("먼저 [슬롯 기준 1/2] 로 안착 TCP 를 저장")
    cur = PC.st()["tcp"]
    if max(abs(cur[i] - OBS[i]) for i in range(3)) > 2.0:      # 13:16 사고: 관측자세 밖에서 ArUco 기준을 찍어 자가 41mm/축척 0.885 로 어긋남 → 베이스 rms 12.6mm
        goto_obs()
    if not os.path.exists(F["aruco"]):
        m, why = BT.markers()
        if m and len(m) >= 3:
            jsave(F["aruco"], {"markers": {str(k): v for k, v in m.items()}, "tcp": PC.st()["tcp"], "made": time.strftime("%Y-%m-%d %H:%M")})
            log(f"  ArUco 기준 저장(마커 {sorted(m)})")
        else:
            log(f"  ⚠ ArUco 기준 미생성: {why or f'마커 {len(m)}개(<3)'} — 자 보정 없이 진행")
    B = stage_base(color)
    d = jload(F["slot"]) or {}
    d[color] = {"seat_tcp": seat, "base": {"x": B["x"], "y": B["y"], "yaw": B["yaw"]}, "base_rms": B["rms"], "made": time.strftime("%Y-%m-%d %H:%M")}
    jsave(F["slot"], d); _pending_seat.pop(color, None); jsave(_PENDING_F, _pending_seat)
    log(f"✅ 슬롯 기준 저장 [{color}] seat {[round(v, 1) for v in seat[:3]]} rz {seat[5]:+.1f} ↔ 베이스 ({B['x']:.1f},{B['y']:.1f},{B['yaw']:+.2f}°)")


def teach_slot_both(color):
    """⑤슬롯 기준 1/2 + 2/2 한 버튼. 1/2 는 벽을 물고 있어야(그리퍼 > 닫힘값 그리고 손목캠 벽 점 ≥1) — 빈손이면 거부.
    1/2 저장 → 사용자가 그리퍼 열고 벽에서 빼낸 뒤 [▶계속] → (그리퍼 열림 확인) → 관측자세 베이스 측정 → 2/2 저장."""
    set_stage("TEACH SLOT 1/2", color=color)
    gc = ((jload(F["rack"]) or {}).get(color) or {}).get("grip_close", 13)
    g = PC.grip_read()
    if g.isdigit() and int(g) <= gc:
        if color not in NO_GRASP_SIG:
            raise Gate(f"슬롯 기준 1/2 거부: 그리퍼 {g} ≤ 닫힘값 {gc} — 벽을 물고 있지 않음(빈손)")
        log(f"  ⚠ [{color}] 파지 확인 불가(그리퍼 {g} = 닫힘값) — 짧은 벽이라 검사 생략, 사용자 확인에 의존")
    if not PC.held_wall_dots_expo(color):
        raise Gate(f"슬롯 기준 1/2 거부: 손목캠에 든 {color} 벽 점 0 — 벽을 물고 있어야 함(빈손)")
    teach_slot_tcp(color)
    wait_user(f"[{color}] 1/2 저장됨 — 그리퍼를 열어 벽을 놓고 벽에서 빼낸 뒤 [▶계속] (로봇이 관측자세로 올라가 2/2 측정)")
    g = PC.grip_read()
    if g.isdigit() and int(g) <= gc:
        raise Gate(f"슬롯 기준 2/2 거부: 그리퍼 {g} 아직 닫힘(≤ {gc}) — 벽을 놓고 빼낸 뒤 [슬롯 기준 2/2] 로 마무리 (1/2 는 저장돼 있음)")
    set_stage("TEACH SLOT 2/2", color=color)
    teach_slot_base(color)


def teach_rack_offset(color):
    # 9/7 사고: 들어올린 뒤(z467) 눌러 z_pick 이 467 로 저장됨 → 픽 높이 근처에서만 허용.
    rr0 = (jload(F["rack"]) or {}).get(color) or {}
    zp0 = rr0.get("z_pick")
    _z = PC.st()["tcp"][2]
    if zp0 and abs(_z - float(zp0)) > 25.0:
        raise Gate(f"랙 보정 저장 거부: 지금 z{_z:.0f} 가 파지 높이 z{float(zp0):.0f} 에서 25mm 넘게 벗어남(들어올린 뒤에 누른 것 아닌지)")
    """WAIT(랙 티칭) 중: 현재 TCP − 계산 TCP 를 벽 축(along/across) 으로 분해해 저장."""
    with LOCK:
        R = S.get("rack") or {}
    if "calc_xy" not in R:
        raise Gate("랙 티칭 대기 상태가 아님")
    cur = PC.st()["tcp"]; d = np.array([cur[0] - R["calc_xy"][0], cur[1] - R["calc_xy"][1]])
    ua, ux = np.array(R["axes"][0]), np.array(R["axes"][1])
    prev = R.get("offset") or {"along": 0.0, "across": 0.0}
    off = {"along": float(prev["along"] + d @ ua), "across": float(prev["across"] + d @ ux)}
    rr = jload(F["rack"]); rr[color]["offset"] = off; rr[color]["offset_made"] = time.strftime("%Y-%m-%d %H:%M")
    rr[color]["z_pick"] = cur[2]
    jsave(F["rack"], rr)
    log(f"✅ 랙 보정 저장 [{color}] along {off['along']:+.2f} across {off['across']:+.2f}mm (조그 {d[0]:+.2f},{d[1]:+.2f}) z_pick {cur[2]:.1f}")


def teach_grasp_sig(color):
    rr = (jload(F["rack"]) or {}).get(color) or {}
    pts = PC.save_grasp_sig_now(color, rr.get("grip_close", 13), PC.grip_read())      # 부품(by_color 스키마 → F["sig"])
    if not pts:
        raise Gate("손목캠에 든 벽 점이 없음")


def fixed_cam_seeds(src, color):
    """고정캠(newcam/side)의 든 벽 점 씨앗 자동 선택. hover_align.wall_dots 는 고정캠에서 씨앗 없이는 항상 [] 이므로
    (CLI 는 --wall x,y 를 받음) 여기서 고른다: 베이스 특징 면적 상한(feat_area hi)보다 큰 그 색 점 = 카메라에 가까운 든 벽 점.
    없으면 최대 점이 2위의 2배 이상일 때만 채택. 애매하면 후보 목록과 함께 거부(사용자가 hover_view 로 확인)."""
    img = HA.grab(src)
    d = HA.DET[src]
    color = HA.wall_color(color, src)          # ★9/10: (색,카메라)별 벽 점 색 예외 — red_s+새카메라 = 옆면 노랑 점
    pts = sorted([q for q in HA._blobs(img, color, d["amin_wall"], src) if q[2] >= d["amin_wall"]], key=lambda q: -q[2])
    if not pts:
        raise Gate(f"{src} 에 {color} 점이 하나도 없음(벽 든 채 z440 이어야 함)")
    big = [q for q in pts if q[2] > d["feat_area"][1]]
    if big:
        pick = big[:2]
    elif len(pts) == 1 or pts[0][2] >= 2.0 * pts[1][2]:
        pick = pts[:1]
    else:
        raise Gate(f"{src} 에 {color} 든 벽 점을 못 고름 — 후보 {[(round(q[0]), round(q[1]), int(q[2])) for q in pts[:5]]} (면적 상한 {d['feat_area'][1]}, hover_view :8775 로 확인)")
    seeds = [(float(q[0]), float(q[1])) for q in pick]
    log(f"  {src} 든 {color} 벽 점 씨앗 {[(round(x), round(y)) for x, y in seeds]} 면적 {[int(q[2]) for q in pick]} (후보 {len(pts)})")
    return seeds


def teach_hover_all(color):
    """★9/10 사용자 지시: "사진 찍으라 할 때 한꺼번에 다 찍어라."
    오늘 문제의 상당수가 **손목캠과 새카메라를 다른 시각·다른 자리에서 찍어서** 생겼다
    (red_s: 손목 20:00 / 새카메라 16:34 → 1.8mm, 다시 0.9mm 차이로 1.67mm 불일치).
    로봇을 움직이지 않고 지금 자리에서 두 카메라를 연속 저장한 뒤, 곧바로 검증해서 불일치를 보고한다."""
    t0 = PC.st()["tcp"]
    log(f"── 기준 일괄 촬영 [{color}] @ ({t0[0]:.2f},{t0[1]:.2f},{t0[2]:.1f}) rz{t0[5]:+.2f}")
    done, fail = [], []
    for src in ("wrist", "newcam"):
        try:
            teach_hover(color, src); done.append(src)
        except Exception as ex:
            fail.append(src); log(f"  ⚠ [{src}] 실패: {ex}")
    t1 = PC.st()["tcp"]
    if max(abs(t1[i] - t0[i]) for i in range(3)) > 0.2:
        log(f"  ⚠ 촬영 중 로봇이 움직였다({t0[:2]} → {t1[:2]}) — 두 기준이 다른 자리가 됐을 수 있다")
    if not done:
        raise Gate(f"기준 일괄 촬영 실패({', '.join(fail)})")
    # 저장 직후 자기검증
    try:
        HA.check.expo_done = False
        HA.check.match_expo_done = False
        C, per, why = HA.check(color, srcs=done, roles={s: "xy" for s in done})
        for s, m in (per or {}).items():
            log(f"  검증 [{s}] Δ ({m['dmm'][0]:+.3f},{m['dmm'][1]:+.3f})mm rms {m['sim']['rms']:.1f}px")
        if per and len(per) == 2:
            a, b = per[done[0]]["dmm"], per[done[1]]["dmm"]
            dd = math.hypot(a[0] - b[0], a[1] - b[1])
            log(f"  ★ 두 카메라 불일치 {dd:.3f}mm " + ("✅" if dd <= 0.5 else f"⚠ (게이트 {HA.COMBINE_TOL_MM}mm)"))
    except Exception as ex:
        log(f"  (검증 실패: {ex})")
    log(f"✅ 기준 일괄 촬영 [{color}] 완료: {', '.join(done)}" + (f" · 실패 {', '.join(fail)}" if fail else ""))


def teach_hover(color, src="wrist"):
    seeds = None if src == "wrist" else fixed_cam_seeds(src, color)
    HA.save_ref(color, src, seeds)                                  # 부품
    log(f"✅ z440 기준 저장 [{color}/{src}]")


def probe_cam(src, color):
    if HA.SRC[src]["moving"] == "base":                             # 손목 부착 카메라(새카메라): 베이스 특징 추적
        log(f"  {src} 매핑 시작(손목 부착): 베이스 특징 ±10mm 조그 + rz ±2°")
        PC.speed(1); HA.probe_arm(src, color); return
    seeds = fixed_cam_seeds(src, color)
    log(f"  {src} 매핑 시작: 벽 점 {[(round(x), round(y)) for x, y in seeds]} ±10mm 조그")
    HA.probe_fixed(src, color, seeds)                               # 부품(J·rot_sign 실측 → cam2robot_<src>.json)


# ------------------------------------------------------------------ 출하 리프트(포크 운반)
LIFT_GRIP_MIN = 20    # 그리퍼 실측이 이보다 작으면 벽을 문 채(외벽 7~13·내벽 6)일 수 있다 → 리프트 금지(리프트는 z≥400 에서 그리퍼를 연다)


def _fork_move(tcp, tol=0.6, timeout=60, tag=""):
    """fork_carry 는 검증된 PC.move 를 그대로 쓴다(house_cycle.move 의 1mm 되돌기·속도 변경을 섞지 않는다). 시작 전 중단만 본다.
    이동 중 [⛔중단]은 handle_cmd 가 브리지에 stop 을 바로 보내고, PC.move 는 미도달로 예외 → 워커가 STOPPED 처리."""
    if ABORT.is_set():
        raise Abort()
    return PC.move(tcp, tol=tol, timeout=timeout, tag=tag)


def stage_lift():
    """완성된 집을 포크 손잡이째 출하지로. 절차·게이트는 전부 fork_carry.carry() 것(9/8 사용자 지정, 9/9 부호·홈 수정판)."""
    g = PC.grip_read()
    if g.isdigit() and int(g) < LIFT_GRIP_MIN:
        raise Gate(f"리프트 금지: 그리퍼 {g} < {LIFT_GRIP_MIN} — 벽을 물고 있을 수 있음(먼저 놓고 빈손으로)")
    c = PC.st()["tcp"]
    if c[2] < FC.OPEN_MIN_Z_START:
        raise Gate(f"리프트 금지: z{c[2]:.0f} < {FC.OPEN_MIN_Z_START:.0f} — 손잡이 위 높이(z440 이상)로 올린 뒤")
    set_stage("LIFT", color=None)
    with LOCK: S["err"] = None
    import types
    FC.log = log                                                       # 포크 로그를 사이클 로그/화면으로
    FC.PC = types.SimpleNamespace(move=_fork_move, speed=PC.speed, gripper=PC.gripper, st=PC.st, grip_read=PC.grip_read, post=PC.post)
    log("══ 출하 리프트: 손잡이 위 z440 → 파란 점 보정 → z280 파지 47 → [속도10] z550→rz90→x-400→y-100→x-700→z285 → 개방 → z575 → 홈")
    FC.carry(grasp=True)
    set_stage("LIFT DONE")
    log("✅ 출하 리프트 완료 — 집은 출하지 (-700,-100), 로봇은 홈")


# ------------------------------------------------------------------ 명령 처리(워커 1개: 로봇을 움직이는 명령은 순차)
Q = queue.Queue()


def worker():
    while True:
        op, arg = Q.get()
        with LOCK:
            S["busy"] = True; S["err"] = None
        try:
            ABORT.clear()
            if op == "start": run_cycle(arg["color"], arg.get("teach_rack", False))
            elif op == "resume_held": run_held(arg["color"])
            elif op == "align_here": align_here(arg["color"])
            elif op == "to_target": goto_calc_target(arg["color"])
            elif op == "descend": stage_descend(arg["color"])
            elif op == "descend_reteach": stage_descend_reteach(arg["color"])
            elif op == "goto_obs": set_stage("GOTO OBS"); goto_obs(); set_stage("IDLE")
            elif op == "slot2": set_stage("TEACH SLOT 2/2"); teach_slot_base(arg["color"]); set_stage("IDLE")
            elif op == "slot_both": teach_slot_both(arg["color"]); set_stage("IDLE")
            elif op == "run4": run_multi(arg.get("order") or run_order())
            elif op == "probe": set_stage(f"PROBE {arg['src']}"); probe_cam(arg["src"], arg["color"]); set_stage("IDLE")
            elif op == "lift": stage_lift()
        except Abort:
            try: PC.post("stop", {"dry_run": False})
            except Exception: pass
            set_stage("STOPPED", err="사용자 중단"); log("⛔ 중단 — 로봇 정지. 벽은 사용자가 처리")
        except Gate as g:
            try: PC.post("stop", {"dry_run": False})
            except Exception: pass
            set_stage("STOPPED", err=str(g)); log(f"🛑 게이트 정지: {g}")
        except Exception as ex:
            try: PC.post("stop", {"dry_run": False})
            except Exception: pass
            set_stage("STOPPED", err=str(ex)); log("❌ 예외 정지: " + str(ex)); traceback.print_exc()
        finally:
            try: PC.speed(1)
            except Exception: pass
            with LOCK: S["busy"] = False


def handle_cmd(q):
    op = q.get("op", [""])[0]; color = q.get("color", [None])[0]; src = q.get("src", ["wrist"])[0]
    if color is not None and color not in COLORS:
        return {"ok": False, "err": f"색 {color}?"}
    if op == "abort":
        ABORT.set(); RESUME.set()
        try: PC.post("stop", {"dry_run": False})
        except Exception as e: return {"ok": False, "err": f"stop 실패 {e}"}
        return {"ok": True}
    if op == "resume":
        RESUME.set(); return {"ok": True}
    with LOCK:
        r4 = S.get("run4"); in_run4_wait = bool(r4 and r4.get("active") and S.get("stage") == "WAIT DESCEND" and S.get("wait"))
    if op == "descend" and in_run4_wait:                             # ④run4 는 워커가 점유 중 → 하강 버튼 = 계속
        RESUME.set(); return {"ok": True, "note": "run4: 하강 진행"}
    if op in ("run_outer", "run_inner"):                            # ★9/11 외벽/내벽 사이클 = run4 의 순서만 다름
        q = dict(q); q["order"] = [",".join(outer_order() if op == "run_outer" else inner_order())]; op = "run4"
    if op in ("start", "descend", "goto_obs", "slot2", "probe", "run4", "slot_both", "resume_held", "descend_reteach", "align_here", "to_target", "lift"):
        if S["busy"]:
            return {"ok": False, "err": "실행 중 — 먼저 중단"}
        order = [c for c in (q.get("order", [""])[0] or "").split(",") if c] or list(run_order())
        if op == "run4" and any(c not in COLORS for c in order):
            return {"ok": False, "err": f"run4 순서 {order}?"}
        Q.put((op, {"color": color, "src": src, "order": order, "teach_rack": q.get("teach", ["0"])[0] == "1"})); return {"ok": True}
    try:                                                            # 로봇 이동 없는 즉시 명령
        if op == "house": return {"ok": True, "house_type": switch_house(q.get("type", [""])[0])}
        if op == "seat_auto": return {"ok": True, "seat_auto": set_seat_auto(q.get("on", ["1"])[0])}
        if op == "slot1": teach_slot_tcp(color)
        elif op == "rack_offset": teach_rack_offset(color)
        elif op == "sig": teach_grasp_sig(color)
        elif op == "hover_ref": teach_hover(color, src)
        elif op == "hover_all": teach_hover_all(color)
        else: return {"ok": False, "err": f"op {op}?"}
        return {"ok": True}
    except Exception as ex:
        log(f"⚠ {op}: {ex}"); return {"ok": False, "err": str(ex)}


def snapshot():
    # ★활성 집 타입을 항상 같이 내보낸다 — 어느 타입 기준으로 도는지 화면에서 바로 보이게.
    with LOCK:
        d = dict(S)
    d["house_type"] = house_type()
    d["seat_auto"] = seat_unknown_auto()
    d["state_dir"] = os.path.realpath(STATE)
    try:
        s = PC.st(); d["tcp"] = [round(v, 2) for v in s["tcp"]]; d["grip"] = s.get("gripper"); d["frozen"] = s.get("frozen")
    except Exception as e:
        d["tcp"] = None; d["bridge_err"] = str(e)
    refs = {}
    slot = jload(F["slot"]) or {}; rack = jload(F["rack"]) or {}; sig = (jload(F["sig"]) or {}).get("by_color", {}); hov = jload(F["hover"]) or {}
    for c in COLORS:
        refs[c] = {"slot": c in slot, "rack": c in rack, "rack_offset": bool(rack.get(c, {}).get("offset_made")), "sig": c in sig, "hover": sorted((hov.get(c) or {}).keys())}
    d["refs"] = refs
    d["maps"] = {"newcam": os.path.exists(HA.SRC["newcam"]["map"]), "side": os.path.exists(HA.SRC["side"]["map"]), "aruco_ref": os.path.exists(F["aruco"])}
    d["gates"] = {"정렬 완료": bool((d.get("align") or {}).get("done")), "파지 게이트": bool((d.get("grasp") or {}).get("ok")),
                  "베이스 이번 사이클": bool(d.get("base")), "동결 아님": not d.get("frozen"),
                  "대기 없음(하강 대기 제외)": d.get("wait") is None or d.get("stage") == "WAIT DESCEND"}
    return d


PAGE = r"""<!doctype html><meta charset=utf-8><title>HOUSE CYCLE</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>/* ★9/8: 로그(pre)만 키움 — 나머지는 원래 크기 */
body{font:16px system-ui;margin:12px;background:#111;color:#eee}
/* ★9/12 사용자 지시: 중요 버튼(.big)만 크게, 나머지는 전부 같은 크기로 — 글자 길이와 무관하게 폭 통일 */
button{margin:3px;padding:9px 10px;font-size:16px;min-width:200px;max-width:200px;min-height:46px;
       white-space:normal;line-height:1.2;text-align:center;vertical-align:top;overflow-wrap:anywhere}
.big{font-size:20px;padding:12px 16px;min-width:310px;max-width:310px;min-height:62px;font-weight:700}.st{font-size:24px;margin:6px 0}.ok{color:#5f5}.no{color:#f66}.wait{color:#fc3}
select,input{font-size:16px;padding:4px}
/* ★9/8: 색 선택은 크게, 버튼은 기능별로 색을 나눠 구분 */
#color{font-size:26px;font-weight:800;padding:8px 14px;background:#222;color:#fff;
       border:3px solid #6cf;border-radius:8px;min-width:180px}
button{border:0;border-radius:6px;color:#fff;background:#3a3f47;cursor:pointer}
button:hover{filter:brightness(1.25)}
.run {background:#1565c0}          /* 파랑 = 사이클 실행 */
.down{background:#e67e00}          /* 주황 = 하강(로봇이 벽을 밀어 넣음) */
.stop{background:#b00020}          /* 빨강 = 중단 */
.go  {background:#2e7d32}          /* 초록 = 계속 */
.move{background:#455a64}          /* 회색 = 단순 이동 */
.teach{background:#6a1b9a}         /* 보라 = 기준 저장(티칭) */
.probe{background:#00695c}         /* 청록 = 캠 매핑 */
/* ★9/7 밤: 노트북·휴대폰에서 조작하려고 좁은 화면 대응 추가(표시만 바뀜, 동작 로직은 그대로).
   손가락으로 누르는 화면이라 버튼을 키우고, 로그·표는 가로로 넘치지 않게 각자 스크롤시킨다. */
@media (max-width:820px){
  body{margin:8px;font-size:18px}
  button{font-size:19px;padding:12px 16px;margin:3px 2px;min-width:0;max-width:none;width:100%;box-sizing:border-box}
  .big{font-size:21px;padding:15px 20px;width:100%;min-width:0;max-width:none;box-sizing:border-box}
  .st{font-size:22px}
  select,input{font-size:19px;padding:9px}
  pre{height:260px;font-size:22px}
  table{display:block;overflow-x:auto;white-space:nowrap;max-width:100%}
  img{max-width:100%;height:auto}
}
pre{background:#000;padding:8px;height:340px;overflow:auto;font-size:24px;line-height:1.35}table{border-collapse:collapse}td,th{border:1px solid #444;padding:2px 8px}
.card{display:inline-block;vertical-align:top;background:#1c1c1c;padding:8px;margin:4px;border-radius:6px;min-width:260px}</style>
<h2>HOUSE CYCLE <small id=tcp></small></h2>
<div class=st>단계: <b id=stage>-</b> <span id=wait class=wait></span></div>
<div>색: <select id=color onchange="try{localStorage.setItem('hc_color',this.value)}catch(e){}"><option>blue<option>yellow<option>red<option>red_s<option>red_in<option>blue_in<option>yellow_in</select>
 <button class="big run" onclick="cmd('start')">▶ 사이클(1→2→2')</button>
 <button class="run" onclick="cmd('start',{teach:1})">▶ 사이클 + 랙 파지 티칭</button>
 <button class="run" onclick="cmd('resume_held')">▶ 든 채로 3단계부터(운반→z440 정렬→하강 대기)</button>
 <button class="run" onclick="cmd('align_here')">▶ 여기서 정렬만 다시(z440, 든 채)</button>
 <button class="teach" onclick="cmd('to_target')">◎ 계산 목표로 복귀(기준 재촬영용)</button>
 <button class="big run" onclick="if(confirm('외벽 사이클 blue→yellow→red→red_s (A·B 공통)? 벽마다 빈손 베이스 재측정, 하강은 매번 [⬇ 하강] 버튼'))cmd('run_outer')">▶ 외벽 사이클(4색)</button>
 <button class="big run" onclick="if(confirm('내벽 사이클 (A: blue_in→yellow_in / B: red_in)? 외벽 4색이 다 꽂힌 뒤에만'))cmd('run_inner')">▶ 내벽 사이클</button>
 <button class="run" onclick="if(confirm('전체 사이클 외벽4→내벽 (지금 집 타입 순서)?'))cmd('run4')">▶ 전체(외벽+내벽)</button>
 <button class="big run" onclick="if(confirm('출하 리프트? 집에 포크 손잡이 끼워져 있고 출하지(-700,-100) 비었나. 로봇은 빈손·z440 이상'))cmd('lift')">🏠 리프트(출하)</button>
 <button class="big down" id=desc onclick="if(confirm('수직 하강? x·y·yaw 확인했나'))cmd('descend')">⬇ 하강(3)</button>
 <button class="down" onclick="if(confirm('하강 → 안착 성공 시 든 채로 z440 올려 기준 재촬영 → 재하강·놓기?'))cmd('descend_reteach')">⬇ 하강+성공 시 z440 기준 재촬영</button>
 <button class="big stop" onclick="cmd('abort')">⛔ 중단</button>
 <button class="go" onclick="cmd('resume')">▶ 계속</button>
 <button class="move" onclick="cmd('goto_obs')">관측자세로</button></div>
<div class=card><b>티칭(선택한 색)</b><br>
 <button class="teach" onclick="cmd('slot_both')">슬롯 기준 1/2+2/2 한 버튼 (물고 저장 → 놓고 ▶계속 → 측정)</button><br>
 <button class="teach" onclick="cmd('slot1')">슬롯 기준 1/2 (안착 TCP)</button> <button class="teach" onclick="cmd('slot2')">슬롯 기준 2/2 (관측 페어링)</button><br>
 <button class="teach" onclick="cmd('rack_offset')">랙 보정 저장</button> <button class="teach" onclick="cmd('sig')">좋은 파지 서명 저장</button><br>
 <button class="teach" onclick="cmd('hover_all')">◎ z440 기준 일괄 저장(두 캠 동시+검증)</button>
 <button class="teach" onclick="cmd('hover_ref',{src:'wrist'})">z440 기준 저장(손목)</button>
 <button class="teach" onclick="cmd('hover_ref',{src:'newcam'})">(새카메라)</button> <button class="teach" onclick="cmd('hover_ref',{src:'side'})">(측면)</button><br>
 <button class="probe" onclick="cmd('probe',{src:'newcam'})">고정캠 매핑: 새카메라</button> <button class="probe" onclick="cmd('probe',{src:'side'})">측면캠</button></div>
<div class=card><b>집 타입</b>
 <span id=htype style="font-size:30px;font-weight:bold;padding:2px 14px;border-radius:6px;background:#1565c0;color:#fff">-</span>
 <span id=hdir style="font-size:15px;color:#666"></span><br>
 <button class="teach" onclick="if(confirm('집 타입을 B(기존 5벽)로 바꿀까요? 기준 세트가 통째로 바뀝니다'))cmd('house',{type:'b'})">B타입으로</button>
 <button class="teach" onclick="if(confirm('집 타입을 A로 바꿀까요? 기준 세트가 통째로 바뀝니다 — A타입 기준은 아직 비어 있습니다'))cmd('house',{type:'a'})">A타입으로</button>
 <div style="font-size:14px;color:#a33;margin-top:4px">티칭 저장은 <b>지금 선택된 타입</b>의 기준에 들어갑니다. 저장 전에 위 배지를 확인하세요.</div></div>
<div class=card><b>안착 판정 불가 처리</b>
 <span id=sauto style="font-size:22px;font-weight:bold;padding:2px 10px;border-radius:6px;background:#555;color:#fff">-</span><br>
 <button class="run" onclick="cmd('seat_auto',{on:'1'})">자동완료 ON (본선)</button>
 <button class="teach" onclick="cmd('seat_auto',{on:'0'})">OFF (티칭: 벽 문 채 정지)</button>
 <div style="font-size:14px;color:#666;margin-top:4px">ON = 목표 z 도달 시 [계속] 없이 개방·상승. 슬롯 기준 1/2 을 찍으려면 OFF.</div></div>
<div class=card><b>게이트</b><div id=gates></div></div>
<div class=card><b>기준 보유</b><div id=refs></div></div>
<div class=card><b>수치</b><div id=nums></div></div>
<pre id=log></pre>
<script>
const $=id=>document.getElementById(id);
async function cmd(op,extra={}){const p=new URLSearchParams({op,color:$('color').value,...extra});const r=await fetch('/cmd?'+p).then(r=>r.json());if(!r.ok)alert(r.err);}
function f(o){return o?JSON.stringify(o,(k,v)=>typeof v==='number'?+v.toFixed(2):v).replace(/[{}"]/g,'').replace(/,/g,'  '):'-'}
try{const _c=localStorage.getItem('hc_color'); if(_c) $('color').value=_c;}catch(e){}   /* 9/7: 서버 재시작마다 색이 blue 로 리셋돼 엉뚱한 색으로 동작하는 사고가 반복 — 마지막 선택을 기억 */
async function poll(){try{const s=await fetch('/state').then(r=>r.json());
 $('stage').textContent=s.stage+(s.err?'  ✖ '+s.err:'')+(s.run4?`  [run4 ${s.run4.idx+1}/${s.run4.order.length} ${s.run4.order[s.run4.idx]} 완료:${s.run4.done.join(',')||'-'}${s.run4.active?'':' 종료'}]`:'');
 $('sauto').textContent=s.seat_auto?'ON':'OFF'; $('sauto').style.background=s.seat_auto?'#1b7f3b':'#8a5a00';
 $('htype').textContent=(s.house_type||'?').toUpperCase(); $('htype').style.background=(s.house_type==='a')?'#b8860b':'#1565c0'; $('hdir').textContent=s.state_dir||'';
 $('stage').style.color=s.stage.startsWith('SEAT FAIL')?'#f66':'';$('wait').textContent=s.wait?'⏸ '+s.wait:'';
 $('tcp').textContent=s.tcp?`tcp ${s.tcp.slice(0,3).join(',')} rz${s.tcp[5]} grip ${s.grip}${s.frozen?' ❄FROZEN':''}`:'브리지 없음';
 $('gates').innerHTML=Object.entries(s.gates).map(([k,v])=>`<div class=${v?'ok':'no'}>${v?'✔':'✘'} ${k}</div>`).join('');
 /* UI 는 정렬·동결만 본다(서버 descend_gate 가 최종). 재시작으로 파지·베이스 기록이 비어도 열리게. */
 const allok=((s.gates['정렬 완료']!==false)&&(s.gates['동결 아님']!==false)&&s.stage==='WAIT DESCEND')||((s.stage==='IDLE'||s.stage==='STOPPED')&&!s.busy&&s.tcp&&s.tcp[2]>=354&&s.tcp[2]<=443);$('desc').disabled=!allok;$('desc').style.opacity=allok?1:.4;  // 13:38: 사용자 수동 정렬(z440±3, IDLE/STOPPED)도 하강 허용 — 서버 게이트가 최종
 $('refs').innerHTML='<table><tr><th>색<th>슬롯<th>랙보정<th>서명<th>z440</tr>'+Object.entries(s.refs).map(([c,r])=>`<tr><td>${c}<td>${r.slot?'✔':'✘'}<td>${r.rack_offset?'✔':(r.rack?'seed':'✘')}<td>${r.sig?'✔':'✘'}<td>${r.hover.join('/')||'✘'}`).join('')+'</table>'
  +`<div>매핑 newcam ${s.maps.newcam?'✔':'✘'} · side ${s.maps.side?'✔':'✘'} · ArUco기준 ${s.maps.aruco_ref?'✔':'✘'}</div>`;
 $('nums').innerHTML=`<div>베이스: ${f(s.base&&{x:s.base.x,y:s.base.y,yaw:s.base.yaw,rms:s.base.rms})}</div><div>랙: ${f(s.rack&&{len_px:s.rack.len_px,dang:s.rack.dang,xy:s.rack.grip_xy})}</div>
  <div>파지: ${f(s.grasp)}</div><div>목표: ${f(s.target&&{x:s.target.x,y:s.target.y,rz:s.target.rz,dyaw:s.target.dyaw})}</div><div>정렬: ${f(s.align&&{x:s.align.x,y:s.align.y,rz:s.align.rz,z:s.align.z,made:s.align.made})}</div><div>안착: ${f(s.seat&&{state:s.seat.state,why:s.seat.why,user_override:s.seat.user_override})}</div>`;
 $('log').textContent=s.log.slice(-60).join('\n');$('log').scrollTop=1e9;}catch(e){}}
setInterval(poll,1000);poll();
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/": return self._send(200, PAGE, "text/html")
        if u.path == "/state": return self._send(200, json.dumps(snapshot(), ensure_ascii=False))
        if u.path == "/cmd": return self._send(200, json.dumps(handle_cmd(q), ensure_ascii=False))
        if u.path == "/health": return self._send(200, json.dumps({"ok": True, "stage": S["stage"]}))
        self._send(404, "{}")


if __name__ == "__main__":
    seed_state()
    threading.Thread(target=worker, daemon=True).start()
    if "--auto" in sys.argv:                                        # ④서버 기동과 함께 run4 를 대기열에(하강은 버튼)
        Q.put(("run4", {"order": list(run_order())})); log("--auto: run4 대기열 등록 (벽마다 하강은 [⬇ 하강] 버튼)")
    log(f"house_cycle :{PORT}  ★집타입 {house_type().upper()}  기준={os.path.realpath(STATE)}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
