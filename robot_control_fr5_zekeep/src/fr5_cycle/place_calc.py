#!/usr/bin/env python3
"""계산 목표 삽입 러너 — "베이스가 움직여도 벽을 꽂는다" 3단계 (9/5 저녁).

골든 '자세'로 가지 않는다. 매 실행마다:
  ① 관측자세에서 기둥 색점 → 베이스 자세(로봇 좌표)         (slot_target)
  ② 그 벽의 슬롯 목표 TCP (x, y, rz) 계산                     (house_geometry.target_tcp)
  ③ 픽(골든 파지 좌표, 파지 실측 게이트) → 운반(30%) → 목표 위 호버 z478 → [옵션] 안착 하강
  하강은 --seat 를 줄 때만. 기본은 호버에서 정지해 사람이/카메라가 확인한다.

  python3 place_calc.py plan <색>              # 로봇 안 움직이고 목표만 출력
  python3 place_calc.py run  <색> [--seat] [--no-pick]
      --no-pick : 이미 물고 있으면 픽 생략(현재 자세에서 운반 시작)
      --seat    : 호버 후 안착 z 까지 천천히 하강(3%→1%), 기둥 꼭대기 아래로는 XY 보정 없음(C2)

안전: 이동은 전부 dry_run 선검사 · TCP 도달 폴링 · 동결 시 즉시 정지 · 실패 시 부품 자동 복귀 없음(철칙).
"""
import sys, json, time, math, os
import urllib.request as UR
import numpy as np

sys.path.insert(0, "/home/ar/bf2_console/tools")
import house_geometry as HG
try:
    import refpts_overlay as _RO      # ★9/12 기준점 오버레이(표시 전용)
except Exception:
    _RO = None

BR = "http://127.0.0.1:8765"
CAL = "/home/ar/bf2_console/dot_calib.json"
OBS = [200.0, -330.0, 650.0, 180.0, 0.0, 180.0]      # 관측자세(매핑 기준)
SAFE_Z, HOVER_Z = 650.0, 478.0
# ★9/10 A타입 내벽 2장 신설(blue_in·yellow_in). 안착 z 는 **티칭 전 임시값** — slot_ref 가 생기면 그걸 쓴다.
SEAT_Z = {"blue": 355.0, "yellow": 354.0, "red": 353.0, "red_s": 351.0, "red_in": 338.0,
          "blue_in": 340.0, "yellow_in": 340.0}
SPD_MOVE, SPD_DESC, SPD_SEAT = 30, 10, 3


def post(a, b):
    r = UR.Request(f"{BR}/fr5/{a}", json.dumps(b).encode(), {"Content-Type": "application/json"})
    return json.loads(UR.urlopen(r, timeout=45).read())


def st():
    return json.loads(UR.urlopen(BR + "/status", timeout=6).read())["robots"]["fr5"]


def speed(v):
    post("speed", {"value": v, "dry_run": False}); time.sleep(0.2)


def wait_idle(t=30):
    t0 = time.time(); time.sleep(0.3)
    while time.time() - t0 < t and st()["busy"]:
        time.sleep(0.2)


def _post_retry409(a, b, tag=""):
    """★9/13: 409 Conflict = 컨트롤러가 앞 명령을 아직 물고 있다는 뜻이고, 이 명령은 **실행되지 않았다**.
    그러므로 앞 명령이 끝나길 기다렸다 한 번 더 보내는 것은 중복 실행이 아니다(안 보내면 사이클이 멈춘다)."""
    try:
        return post(a, b)
    except UR.HTTPError as e:
        if e.code != 409:
            raise
        print(f"  ⚠ {tag} 409 Conflict — 앞 명령이 끝나길 기다렸다 한 번 재시도", flush=True)
        wait_idle(20)
        time.sleep(0.8)
        return post(a, b)


def move(tcp, tol=0.6, timeout=60, tag=""):
    r = _post_retry409("move_tcp", {"tcp": tcp, "dry_run": True}, tag)
    if r.get("result") != "dry_run":
        raise RuntimeError(f"{tag} dry_run 거부 {r}")
    r = _post_retry409("move_tcp", {"tcp": tcp, "dry_run": False}, tag)
    if r.get("result") not in ("started", "ok"):
        raise RuntimeError(f"{tag} 이동 거부 {r}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = st()
        if s.get("frozen"):
            raise RuntimeError(f"{tag} 동결")
        c = s["tcp"]
        if max(abs(c[i] - tcp[i]) for i in range(3)) <= tol and abs(HG.wrap_deg(c[5] - tcp[5])) <= 0.3 and not s["busy"]:
            time.sleep(0.4)
            print(f"  ✓ {tag} ({c[0]:.1f},{c[1]:.1f},{c[2]:.1f}) rz{c[5]:+.2f}", flush=True)
            return c
        time.sleep(0.2)
    raise RuntimeError(f"{tag} 미도달 목표{[round(v,1) for v in tcp[:3]]} 현재{[round(v,1) for v in st()['tcp'][:3]]}")


def grip_read():
    return str(post("grip_read", {"dry_run": True})["result"])


GRIP_SAME_TOL = 2          # ★9/13: 실측이 목표와 이만큼 안이면 '이미 그 상태' 로 보고 명령을 보내지 않는다

def gripper(pos):
    """★9/13 사고: 이미 열려 있는 그리퍼(30)에 또 '열기 30' 을 보내 409 Conflict →
    컨트롤러 앱 사망(controller_dead, 전원 재투입만이 해법). 9/10 에 함정으로 기록해 두고도
    코드로 막지 않아 오늘 재발했다. 그리퍼는 제어 루프를 2~5초 점유하므로 불필요한 명령 자체가 위험.
    → 목표와 실측이 GRIP_SAME_TOL 안이면 **명령을 생략**하고 실측을 그대로 돌려준다."""
    want = int(pos)
    try:
        cur = int(float(grip_read()))
    except Exception:
        cur = None
    if cur is not None and abs(cur - want) <= GRIP_SAME_TOL:
        print(f"  (그리퍼 이미 {cur} ≈ 목표 {want} — 명령 생략, 409 방지)", flush=True)
        return str(cur)
    post("gripper", {"pos": want, "dry_run": False}); wait_idle()
    got = grip_wait(want)
    time.sleep(0.4)
    return str(got) if got is not None else grip_read()


GRIP_SETTLE_TOL = 2        # 실측이 목표와 이만큼 안이면 도달로 본다
GRIP_WAIT_S     = 15.0     # 풀스트로크 실측 11초(8/14) + 여유
GRIP_STILL_S    = 1.2      # 값이 이만큼 안 변하면 더 못 가는 것(벽을 문 상태)
GRIP_START_S    = 3.0      # 명령 뒤 이만큼은 '아직 출발 안 했을 수 있다' 로 보고 정지 판정을 미룬다


def grip_wait(want, timeout=GRIP_WAIT_S):
    """★9/13 409 의 진범: `그리퍼 열기 30` 과 다음 move_tcp 가 **같은 초**에 나가 409 Conflict(15:09, 15:42:06).
    wait_idle() 은 관절 busy 만 보므로 그리퍼가 도는 동안에도 곧바로 돌아온다(그리퍼 풀스트로크 실측 11초).
    → 실측값이 목표에 닿거나 **더 이상 변하지 않을 때까지** 기다린다.
      벽을 물면 목표(7)에 못 닿고 14 에서 멈추므로 '도달' 만 기다리면 안 되고 '멈춤' 도 완료로 봐야 한다."""
    t0 = time.time(); last = None; last_t = t0; moved = False
    while time.time() - t0 < timeout:
        try:
            cur = int(float(grip_read()))
        except Exception:
            cur = None
        if cur is not None:
            if abs(cur - want) <= GRIP_SETTLE_TOL:
                return cur
            if last is None:
                last, last_t = cur, time.time()
            elif cur != last:
                last, last_t, moved = cur, time.time(), True
            elif time.time() - last_t >= GRIP_STILL_S and (moved or time.time() - t0 >= GRIP_START_S):
                # ★출발 전 정지를 '다 됐다' 로 오판하면 409 를 그대로 되풀이한다 —
                #   한 번이라도 움직인 뒤의 정지이거나, 출발 유예(GRIP_START_S)를 넘긴 뒤에만 완료로 본다.
                return cur
        time.sleep(0.25)
    print(f"  ⚠ 그리퍼 {timeout:.0f}초 안에 안 멎음(목표 {want}, 마지막 {last}) — 그대로 진행", flush=True)
    return last


GRASP_REF = "/home/ar/bf2_console/grasp_ref_0905.json"
# ★파지 편차 부호(가정, 첫 실주행에서 검증할 것):
#   손목캠 화면 +x  = 로봇 −X = (rz 180 파지 자세에서) 그리퍼 +x = 벽을 '가로지르는' 방향(across)
#   손목캠 화면 +y  = 로봇 −Y = 그리퍼 −y = 벽 '길이' 방향(along) 의 반대
ACROSS_SIGN, ALONG_SIGN = +1.0, -1.0


def load_grasp_ref(color):
    """색별 공칭 파지 서명. 없으면 None (→ 공칭 파지로 진행, 경고)."""
    if not os.path.exists(GRASP_REF):
        return None
    ref = json.load(open(GRASP_REF))
    # ★9/6 버그: 최상위(9/5 17:04 파랑, taught 픽 기준)를 by_color 보다 먼저 돌려줘서 --grasp-teach 로 다시 쓴 by_color.blue 가
    #   무시됐다(저장 직후 "길이 −1.93mm" = 옛 중점 437.4 ↔ 새 458.9 의 21.5px). 저장은 항상 by_color 에 하므로 by_color 우선.
    bc = (ref.get("by_color") or {}).get(color)
    if bc and "cam1_wall_mid" in bc:
        return bc
    if ref.get("color") == color and "cam1_wall_mid" in ref:
        return ref
    return None


def _held_blobs(img, ranges, area, x_min, x_max=1270, y_min=8, y_max=715, merge_px=45.0):
    """든 벽의 색점 — ★9/7 실측(red_s): 점이 작고 어두워 같은 점이 한 프레임에서는 면적 712 한 덩어리,
    다음 프레임에서는 338+258 두 조각으로 갈라진다. 면적 하한 500 을 조각 하나씩 재면 '벽 점 0개'가 되어
    파지 편차를 못 재고 정지한다 → 하한의 1/4 이상 조각을 먼저 모아 merge_px 안이면 합친 뒤 면적을 판정한다.
    밝기 가중 중심을 쓰므로 합쳐도 중심 정의는 그대로다."""
    import cv2, numpy as np
    m = _mask_ranges(img, ranges)
    n, lab, stt, cen = cv2.connectedComponentsWithStats(m)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    frag = []
    floor = max(100.0, area[0] * 0.25)
    H, W = m.shape[:2]
    x_max = min(x_max, W - 2); y_max = min(y_max, H - 2)
    for i in range(1, n):
        a = int(stt[i, 4]); x, y = cen[i]
        if a < floor or a > area[1] * 2 or x < x_min or x > x_max or y < y_min or y > y_max:
            continue
        # ★9/7: 파랑 서명이 y 695.8(창 상한 700)에서 찍혀 점 아래가 잘렸다 → 중심이 위로 밀리고 면적이 준다.
        #   프레임 가장자리에 닿은 덩어리는 경고만 남기고 쓴다(버리면 '벽 점 0개'가 되어 더 나쁘다).
        x0b, y0b, wb, hb = int(stt[i, 0]), int(stt[i, 1]), int(stt[i, 2]), int(stt[i, 3])
        if x0b <= 1 or y0b <= 1 or x0b + wb >= W - 1 or y0b + hb >= H - 1:
            print(f"    ⚠ 벽 점이 프레임 가장자리에 걸림({x0b},{y0b},{wb}x{hb}) — 중심이 밀릴 수 있음", flush=True)
        clipped = 1.0 if (x0b <= 1 or y0b <= 1 or x0b + wb >= W - 1 or y0b + hb >= H - 1) else 0.0
        ys, xs = np.nonzero(lab == i); w = g[ys, xs].astype(float) + 1
        frag.append([float((xs * w).sum()), float((ys * w).sum()), float(w.sum()), a, clipped, float(y0b)])
    frag.sort(key=lambda q: -q[3])
    groups = []
    for f in frag:
        cx, cy = f[0] / f[2], f[1] / f[2]
        for gr in groups:
            gx, gy = gr[0] / gr[2], gr[1] / gr[2]
            if math.hypot(cx - gx, cy - gy) <= merge_px:
                gr[0] += f[0]; gr[1] += f[1]; gr[2] += f[2]; gr[3] += f[3]
                gr[4] = max(gr[4], f[4]); gr[5] = min(gr[5], f[5]); break
        else:
            groups.append(list(f))
    # ★9/8: (x, y, 면적) 뒤에 (잘림여부, bbox 상단 y) 를 덧붙인다. 기존 코드는 앞 3개만 쓰므로 영향 없다.
    pts = [(gr[0] / gr[2], gr[1] / gr[2], int(gr[3]), gr[4] > 0.5, gr[5]) for gr in groups if area[0] <= gr[3] <= area[1]]
    pts.sort(key=lambda q: q[1])
    return pts


def wall_dots_cam1(ref):
    """물고 있는 벽의 색점 2개(손목캠). ref["cam1_wall_detect"] 규칙(색별)."""
    import cv2, numpy as np
    cfg = ref["cam1_wall_detect"]
    b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
    return _held_blobs(img, cfg.get("hsv_ranges") or [(cfg["hsv_lo"], cfg["hsv_hi"])], cfg["area"], cfg["x_min"])


def grasp_measure(color="blue", rack_dang=None):
    """설계 2단계: 잡은 벽이 그리퍼에 어떻게 물렸나 — 공칭 파지 서명 대비 편차 → GripMeasure.
    점 2개면 위치+각, 점 1개(노랑·red_s)면 위치만(각은 랙 관측 각차 rack_dang 로). 축척은 뎁스 실측(d_w/fx).
    반환 (grip, info) / 실패 (None, why)."""
    ref = load_grasp_ref(color)
    if ref is None:
        return None, f"{color} 공칭 파지 서명 없음(`place_calc.py grasp {color} <개도>` 로 1회 기록)"
    pts = wall_dots_cam1(ref)
    n_ref = len(ref.get("cam1_wall_pd") or [])
    if len(pts) < 1:
        return None, "든 벽 점 0개"
    scale = ref.get("wall_scale_mm_per_px") or wall_scale(color)
    if len(pts) >= 2 and n_ref >= 2:
        (x1, y1, *_), (x2, y2, *_) = pts[0], pts[-1]   # ★9/10: wall_dots_cam1 은 5개(x,y,면적,잘림,y_top) — 개수에 안 흔들리게
        ang = math.degrees(math.atan2(x2 - x1, y2 - y1)); mid = ((x1 + x2) / 2, (y1 + y2) / 2)
        dang = HG.wrap_deg(ang - ref["cam1_wall_ang_deg"]); how = "2점"
    else:
        # 1점: 기준 점 자리에 가장 가까운 점 하나로 위치만. 각은 랙 관측 각차(없으면 0).
        # ★9/7 실기(파랑 −16.43mm): 기준이 2점인데 이번에 1점만 잡히면 그 1점을 '두 점의 중점'과 비교해
        #   간격의 절반(425px×0.09≈19mm)이 통째로 가짜 편차가 됐다 → 기준 '점' 중 가장 가까운 것과 비교한다.
        rp = ref.get("cam1_wall_pd") or []
        if len(rp) >= 2:
            base_pt = min(rp, key=lambda t: math.hypot(pts[0][0] - t[0], pts[0][1] - t[1]))
            rx, ry = float(base_pt[0]), float(base_pt[1])
            how_note = "1점(기준 2점 중 가까운 쪽)"
        else:
            rx, ry = ref["cam1_wall_mid"]
            how_note = "1점"
        q = min(pts, key=lambda t: math.hypot(t[0] - rx, t[1] - ry)); mid = (q[0], q[1])
        # ★9/7 16:48: 서명은 '위쪽 점'(1027,279), 측정은 '아래쪽 점'(1005,680) 하나만 잡혀
        #   서로 다른 점을 비교해 두 점 간격 425px 이 그대로 −37.97mm 로 나왔다.
        #   이런 '점 정체 불일치'는 조용히 숫자로 내지 말고 멈춘다(게이트는 엄격하게).
        _d = math.hypot(q[0] - rx, q[1] - ry)
        if _d > DOT_IDENTITY_MAX_PX:
            return None, (f"든 벽 점이 서명의 점과 {_d:.0f}px 떨어짐(> {DOT_IDENTITY_MAX_PX:.0f}) — "
                          f"서명 (%.0f,%.0f) vs 측정 (%.0f,%.0f): 다른 점을 본 것. "
                          f"사이클이 재는 자세(랙 위 들어올림)에서 [좋은 파지 서명 저장] 으로 다시 찍을 것"
                          % (rx, ry, q[0], q[1]))
        ref_mid = (rx, ry)
        dang = rack_dang if rack_dang is not None else 0.0; how = how_note + ("+랙각" if rack_dang is not None else "")
    if len(pts) >= 2 and n_ref >= 2:
        ref_mid = tuple(ref["cam1_wall_mid"])
    dx = mid[0] - ref_mid[0]; dy = mid[1] - ref_mid[1]
    across = ACROSS_SIGN * dx * scale; along = ALONG_SIGN * dy * scale
    grip = HG.GripMeasure(center=(across, along), angle_deg=90.0 + dang, bottom_dz=0.0)
    return grip, {"dx_px": dx, "dy_px": dy, "dang": dang, "across_mm": across, "along_mm": along, "scale": scale, "how": how}


# ★9/7 실측: 든 벽 점 면적은 색마다 크게 다르다(노랑 2703 · 파랑 1650 · red_s 617~756).
#   red_s 는 하한 500 이 경계에 걸려 프레임마다 '벽 점 0개'가 되어 파지 편차를 못 재고 정지했다.

DOT_IDENTITY_MAX_PX = 150.0   # 측정한 벽 점이 서명의 점에서 이보다 멀면 '다른 점' → 편차 계산 금지

GRASP_GATE_MM, GRASP_GATE_DEG = 1.0, 0.3     # 설계 2단계 게이트(9/4 v2.1). 넘으면 정지·보고, 자동 재파지 금지
HELD_TOP_DEPTH_OFFSET = 273.0   # 든 벽 윗변 뎁스(mm) = SEAT_Z − 273  (아래 유도)


def wall_scale(color):
    """든 벽 윗변 평면의 손목캠 축척(mm/px) — **뎁스도, 점 간격 가정도 안 쓴다**(9/6 새벽 검증).
    유도: 관측자세 z650 에서 기둥꼭대기 뎁스 377mm(실측) → TCP z 에서 기둥꼭대기 뎁스 = 377−(650−z).
          안착 z_seat 에서 벽 윗변 = 기둥 꼭대기이므로 TCP z 에서 벽 윗변은 기둥 꼭대기보다 (z−z_seat) 위
          → 벽 윗변 뎁스 = 377−650+z−(z−z_seat) = z_seat−273 (z 무관 상수). 파랑 82 · 노랑 81 · 빨강 80 · red_s 78mm.
          축척 = 뎁스/fx, fx = 377/0.4135 = 912px.
    교차검증(9/6): 파랑 든 점 간격 425.9px × 0.0899 = 38.3mm ↔ 랙 관측 실측 인접 간격 38.8mm (차 1.3%).
          (랙 실측 5점 간격 38.8/53.5/63.1/29.7mm → '균등 간격' 가정은 틀렸음, 폐기)
    ★D435 최소 거리(1280×720 에서 ~280mm)보다 훨씬 가까워 든 벽 뎁스는 원리적으로 안 나온다 — 사용자 관찰과 일치."""
    return (SEAT_Z[color] - HELD_TOP_DEPTH_OFFSET) / FX_PX


def save_grasp_sig_now(color, grip_cmd, gr):
    """★든 상태에서 지금 손목캠 벽 점을 공칭 파지 서명으로 저장(그리퍼 조작 없음). 축척은 wall_scale(color) 상수."""
    import cv2, numpy as np
    amin = HELD_AREA_MIN.get(color, 500)
    # 9/6 20:2x: x_min 600 이라 랙 쪽 빨간 점(x 659)까지 서명에 섞였다(빨강 각 −55° 오저장) → 든 벽 영역(HELD_BOX x≥820)만.
    # 9/7: 측정(wall_dots_cam1)과 같은 조각합치기를 쓰고, ★한 프레임이 아니라 여러 프레임 중앙값으로 찍는다
    #      (red_s 실측: 같은 파지에서 면적 510~756, 중심 ±3px 로 흔들려 단발 서명이 그 잡음을 기준에 박는다).
    cand = []
    for _ in range(7):
        b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        q = _held_blobs(img, WALL_DOT_HSV[color], [amin, 6000], HELD_X_MIN.get(color, 820))
        if q:
            cand.append(q)
        time.sleep(0.15)
    pts = []
    if cand:
        import statistics as _s
        nmode = _s.mode([len(q) for q in cand])
        sel = [q for q in cand if len(q) == nmode]
        print(f"  서명 프레임 {len(cand)}/7 검출, 점 {nmode}개 프레임 {len(sel)}개 중앙값 사용")
        for i in range(nmode):
            pts.append((_s.median([q[i][0] for q in sel]), _s.median([q[i][1] for q in sel]), int(_s.median([q[i][2] for q in sel]))))
    if not pts:
        print("  ⚠ 서명 저장 실패: 벽 점 0개"); return None
    if len(pts) >= 2:
        (x1, y1, *_), (x2, y2, *_) = pts[0], pts[-1]   # ★9/10: wall_dots_cam1 은 5개(x,y,면적,잘림,y_top) — 개수에 안 흔들리게
        ang = math.degrees(math.atan2(x2 - x1, y2 - y1)); L = math.hypot(x2 - x1, y2 - y1); mid = [(x1 + x2) / 2, (y1 + y2) / 2]
    else:
        ang, L, mid = 0.0, 0.0, [pts[0][0], pts[0][1]]
    ref = json.load(open(GRASP_REF)) if os.path.exists(GRASP_REF) else {}
    try:
        import color_lock as _CL
        _expo = (_CL.current_settings() or {}).get("exposure")
    except Exception:
        _expo = None
    # ★9/7: 같은 파지라도 노출이 다르면 점 중심이 12.8px 밀린다 → 촬영 노출을 반드시 남겨 측정 때 되맞춘다.
    ref.setdefault("by_color", {})[color] = {"made": time.strftime("%Y-%m-%d %H:%M"), "grip_cmd": grip_cmd, "grip_real": gr, "tcp": st()["tcp"], "expo": _expo,
        "cam1_wall_pd": [[q[0], q[1], q[2]] for q in pts], "cam1_wall_ang_deg": ang, "cam1_wall_gap_px": L, "cam1_wall_mid": mid,
        "cam1_wall_detect": {"hsv_ranges": [[list(a), list(b)] for a, b in (WALL_DOT_HSV[color] if isinstance(WALL_DOT_HSV[color], list) else [WALL_DOT_HSV[color]])], "area": [amin, 6000], "x_min": HELD_X_MIN.get(color, 820)},
        "note": "랙 중앙 파지(rack_calib) 상태에서 --grasp-teach 로 저장"}
    json.dump(ref, open(GRASP_REF, "w"), ensure_ascii=False, indent=1)
    print(f"  ✅ [{color}] 파지 서명 저장({len(pts)}점): 각 {ang:+.2f}° 간격 {L:.0f}px 중점 ({mid[0]:.0f},{mid[1]:.0f})")
    return pts


# ★뎁스 기반 벽 계측(9/5 밤, 사용자 설계 "잡으면 뎁스로 벽 길이를 재서 검산"). ⚠ 실기 미검증(로봇 꺼진 뒤 작성).
#   fx 는 관측 매핑(z650 기둥꼭대기 뎁스 377mm ↔ 0.4135mm/px)에서 역산: fx = 377/0.4135 ≈ 912px.
#   ① 든 벽(호버·랙 위): 손목캠 프레임에 벽 **전체가 안 들어온다**(9/5 파랑 z440 프레임: 흰 판이 위아래 프레임 밖,
#      198mm ≈ 1650px > 720px). 그래서 길이는 못 재고, **뎁스 d_w → 실측 축척(d_w/fx)** 과 **벽 축 각(PCA)** 만 잰다.
#      축척은 grasp_measure 의 가상값 0.12 를 대체하고, 축 각은 점 1개 벽(노랑·red_s)의 파지 회전 관측에 쓴다.
#   ② 랙 관측자세(z556, 벽 전체 5점 보임): 색점 축 주변 뎁스 돌출 띠의 양 끝 = 물리적 양 끝 → 길이 검산.
#      색점 두 끝 중간 ≠ 물리 중앙(~9mm) 문제를 색점과 독립인 방법으로 교차검증한다.
FX_PX = 377.0 / 0.4134670689041742
WALL_LEN_MM = {"blue": 198.0, "red": 198.0, "yellow": 125.0, "red_s": 125.0}
LEN_GATE_MM = 4.0
HELD_DEPTH_MAX = 250.0          # 이보다 먼 점은 든 벽이 아님(밑판·책상·랙)
HELD_BOX_DEPTH = (600, 0, 1279, 719)


def _depthgrid(x0, y0, x1, y1, nx, ny):
    import numpy as np
    g = json.loads(UR.urlopen(f"http://127.0.0.1:8766/depthgrid?x0={x0}&y0={y0}&x1={x1}&y1={y1}&nx={nx}&ny={ny}&r=2", timeout=8).read())
    return np.array([(p["x"], p["y"], p["d"]) for p in g["pts"] if p["d"]], float)


def held_wall_depth(color, nx=68, ny=72):
    """든 벽 윗면 뎁스 → {"d_w", "mm_px", "ang_img", "span_px", "n"} 또는 (None, why). 길이는 프레임 클리핑으로 못 잰다."""
    import numpy as np
    x0, y0, x1, y1 = HELD_BOX_DEPTH
    try:
        pts = _depthgrid(x0, y0, x1, y1, nx, ny)
    except Exception as e:
        return None, f"depthgrid 실패: {e}"
    pts = pts[(pts[:, 2] > 40) & (pts[:, 2] < HELD_DEPTH_MAX)]
    if len(pts) < 30:
        return None, f"든 벽 뎁스 점 {len(pts)}개(<30) — 벽을 안 들었거나 뎁스 무효"
    d_w = float(np.median(pts[:, 2]))
    if not (80.0 <= d_w <= 300.0):
        return None, f"든 벽 뎁스 {d_w:.0f}mm 비현실(검은 벽 뎁스 불량 의심)"
    near = pts[np.abs(pts[:, 2] - d_w) < 12.0]
    c = near[:, :2].mean(0); X = near[:, :2] - c
    w, v = np.linalg.eigh(X.T @ X); ax = v[:, int(np.argmax(w))]
    proj = X @ ax
    return {"d_w": d_w, "mm_px": d_w / FX_PX, "ang_img": math.degrees(math.atan2(ax[1], ax[0])),
            "span_px": float(proj.max() - proj.min()), "center_px": (float(c[0]), float(c[1])), "n": int(len(near))}, None


def rack_len_depth(color, ends, band_px=60, step_px=6.0):
    """랙 관측자세에서 색점 양 끝(ends["p1"],["p2"]) 축 주변 뎁스 띠 → 물리적 길이·중심. RACK_MAP 축척 사용.
    반환 {"len_mm","len_px","center_px","dot_len_px","d_top"} 또는 (None, why)."""
    import numpy as np
    (x1, y1), (x2, y2) = ends["p1"], ends["p2"]
    ux, uy = x2 - x1, y2 - y1; L = math.hypot(ux, uy) or 1.0; ux, uy = ux / L, uy / L
    ext = 0.35 * L                                        # 양 끝 바깥으로 35% 더 본다(점이 끝이 아닐 수 있음)
    xs = [x1 - ux * ext, x2 + ux * ext]; ys = [y1 - uy * ext, y2 + uy * ext]
    bx0 = int(max(0, min(xs) - band_px)); bx1 = int(min(1279, max(xs) + band_px))
    by0 = int(max(0, min(ys) - band_px)); by1 = int(min(719, max(ys) + band_px))
    # 격자 해상도: 축 방향 끝 판정 오차 = 한 칸이므로 x·y 모두 step_px(≈6px≈2mm) 로 깐다(점 수 ≤ ~4000)
    nx = int(min(220, max(8, (bx1 - bx0) / step_px))); ny = int(min(220, max(8, (by1 - by0) / step_px)))
    try:
        pts = _depthgrid(bx0, by0, bx1, by1, nx, ny)
    except Exception as e:
        return None, f"depthgrid 실패: {e}"
    pts = pts[(pts[:, 2] > 100) & (pts[:, 2] < 900)]
    if len(pts) < 40:
        return None, "뎁스 점 부족"
    # 축 수직거리 band 안 점만, 뎁스 최빈 대역(벽 윗면 = 가장 가까운 큰 덩어리)
    rel = pts[:, :2] - np.array([x1, y1]); along = rel @ np.array([ux, uy]); perp = rel @ np.array([-uy, ux])
    m = np.abs(perp) < 25.0
    if m.sum() < 20:
        return None, "축 띠 안 뎁스 점 부족"
    d = pts[m, 2]; hist, edges = np.histogram(d, bins=int(max(5, (d.max() - d.min()) / 3.0)))
    d_top = 0.5 * (edges[np.argmax(hist)] + edges[np.argmax(hist) + 1])
    top = m & (np.abs(pts[:, 2] - d_top) < 8.0)
    a = along[top]
    if len(a) < 10:
        return None, "벽 윗면 대역 점 부족"
    step = abs(ux) * (bx1 - bx0) / nx + abs(uy) * (by1 - by0) / ny     # 축 방향 격자 한 칸
    a0, a1 = float(a.min()) - step / 2, float(a.max()) + step / 2
    scale = float(np.hypot(*np.array(json.load(open(RACK_MAP))["Jinv_mm_per_px"])[:, 0]))
    cx, cy = x1 + ux * (a0 + a1) / 2, y1 + uy * (a0 + a1) / 2
    return {"len_px": a1 - a0, "len_mm": (a1 - a0) * scale, "center_px": (float(cx), float(cy)),
            "dot_len_px": L, "d_top": float(d_top), "n": int(top.sum())}, None


def rack_len_check(color, ends, strict=False):
    """랙 길이 검산: 뎁스 물리 길이 vs 기지 길이, 뎁스 중심 vs 색점 중간. strict 면 게이트(예외)."""
    m, why = rack_len_depth(color, ends)
    if m is None:
        print("  ⚠ 랙 뎁스 길이 측정 불가:", why)
        if strict: raise RuntimeError("랙 뎁스 길이 측정 불가: " + why)
        return None
    L0 = WALL_LEN_MM[color]; dl = m["len_mm"] - L0
    off = math.dist(m["center_px"], ends["mid"])
    print(f"  랙 뎁스 검산[{color}]: 물리 길이 {m['len_mm']:.1f}mm (기지 {L0:.0f}, Δ{dl:+.1f}) · 뎁스중심↔색점중간 {off:.1f}px · 윗면 {m['d_top']:.0f}mm · 점 {m['n']}")
    if abs(dl) > LEN_GATE_MM:
        msg = f"랙 벽 길이 불일치 Δ{dl:+.1f}mm > {LEN_GATE_MM} — 끝 가림/겹침/다른 벽 의심"
        if strict: raise RuntimeError(msg)
        print("  ⚠", msg, "(보고만, --len-gate 로 게이트화)")
    return m


PICK_REF = "/home/ar/bf2_console/pick_ref_0905.json"
WALL_DOT_HSV = {                       # 물고 있는 벽의 점 색(손목캠, 카메라 가까움 → 밝고 큼)
    "blue":   ((95, 150, 140), (115, 255, 255)),
    "yellow": ((15, 80, 110), (38, 255, 255)),
    # 9/6 20:2x 실측(빨강 긴 벽 든 손목캠): 빨강 점 H 가 175 를 넘어가 조각남(306+240px) → 상한 179 로 하나(2424px).
    #   0~8 구간까지 더해도 차이 없어 단일 범위 유지(place_calc 6곳이 lo,hi 튜플을 그대로 씀).
    # ★9/7 18:0x 사용자 지시로 색 실측(랙 캡쳐에서 점 화소 직접 측정):
    #   빨강 벽 점의 실제 색상은 H 3 / 10 / 17 로 **0쪽**이다. 범위가 135~179 하나뿐이라 점 가장자리
    #   몇 화소만 걸려 9+29+9 조각으로 갈라졌고(맨 위 점), 면적 하한 40 을 못 넘어 사라졌다.
    #   빨강은 색상환에서 0과 179 양쪽에 걸치므로 두 구간을 모두 본다. 노랑(H15~)과 겹치지 않게 상한 12.
    "red":    [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    "red_s":  [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    "red_in": [((135, 90, 55), (179, 255, 255)), ((0, 90, 55), (12, 255, 255))],
    # ★9/10 A타입 내벽: 점 색이 파랑/노랑 → 같은 이름의 외벽 범위를 그대로 쓴다(스티커가 같은 것).
    "blue_in": ((100, 120, 90), (130, 255, 255)),
    # ★9/10 저녁: 든 벽 점 실측 H 21~22 · S 196~210 · V 97~98 → V 하한 120 에만 걸려 사라졌다.
    #   S 가 200 대라 흰 면·반사와 확실히 구분되므로 **V 하한만** 조명에 맞춘다(H·S 는 그대로 = 구분력 유지).
    "yellow_in": ((15, 90, 80), (40, 255, 255)),
}


def _mask_ranges(img, ranges):
    """HSV 구간(튜플 하나 또는 리스트)의 합집합 마스크. 빨강은 색상환 0쪽과 179쪽이 둘 다 필요하다."""
    import cv2, numpy as np
    rs = ranges if isinstance(ranges, list) else [ranges]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = None
    for lo, hi in rs:
        mm = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        m = mm if m is None else cv2.bitwise_or(m, mm)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def wall_mask(img, color):
    return _mask_ranges(img, WALL_DOT_HSV[color])


def pick_pose(color):
    """랙 파지 자세: 사용자가 9/5 수정한 값(pick_ref_0905)이 있으면 우선, 없으면 dot_calib 골든."""
    refs = json.load(open(CAL))["refs"][color]
    pick, hover, g_open, g_close = refs["pick_tcp_taught"], refs["golden_pick"]["golden_tcp"], refs["grip_open"], refs["grip_close"]
    if os.path.exists(PICK_REF):
        pr = json.load(open(PICK_REF)).get(color)
        if pr:
            pick = pr["tcp"]; hover = [pick[0], pick[1], pick[2] + 125.0] + list(pick[3:])
            g_close = pr.get("grip_close", g_close); g_open = pr.get("grip_open", g_open)
    return pick, hover, g_open, g_close


def capture_grasp_sig(color, grip_cmd):
    """★사용자가 랙에서 자세를 맞춘 뒤 '잡아': 닫기 → 실측 → 벽 점 서명 저장 → 파지 TCP 기록."""
    import cv2, numpy as np
    tcp0 = st()["tcp"]
    gr = gripper(grip_cmd); print(f"  그리퍼 닫기 {grip_cmd} → 실측 {gr}")
    time.sleep(0.4)
    b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
    m = wall_mask(img, color)
    n, lab, stt, cen = cv2.connectedComponentsWithStats(m); g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY); pts = []
    for i in range(1, n):
        a = int(stt[i, 4]); x, y = cen[i]
        if not (500 <= a <= 6000) or x < 600 or x > 1260 or y < 20 or y > 700:
            continue
        ys, xs = np.nonzero(lab == i); w = g[ys, xs].astype(float) + 1
        pts.append((float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum()), a))
    pts.sort(key=lambda q: q[1])
    print(f"  벽 점({color}): " + " ".join(f"({q[0]:.0f},{q[1]:.0f})a{q[2]}" for q in pts))
    cv2.imwrite(f"/tmp/robot_cell/grasp_{color}.jpg", img)
    ref = json.load(open(GRASP_REF)) if os.path.exists(GRASP_REF) else {}
    ref.setdefault("by_color", {})
    if len(pts) >= 1:
        if len(pts) >= 2:
            (x1, y1, *_), (x2, y2, *_) = pts[0], pts[-1]   # ★9/10: wall_dots_cam1 은 5개(x,y,면적,잘림,y_top) — 개수에 안 흔들리게
            ang = math.degrees(math.atan2(x2 - x1, y2 - y1)); L = math.hypot(x2 - x1, y2 - y1); mid = [(x1 + x2) / 2, (y1 + y2) / 2]
        else:
            ang, L, mid = 0.0, 0.0, [pts[0][0], pts[0][1]]          # 1점(노랑·red_s): 위치 서명만
        ref["by_color"][color] = {"made": time.strftime("%Y-%m-%d %H:%M"), "grip_cmd": grip_cmd, "grip_real": gr, "tcp": tcp0,
            "cam1_wall_pd": [[q[0], q[1], q[2]] for q in pts], "cam1_wall_ang_deg": ang, "cam1_wall_gap_px": L,
            "cam1_wall_mid": mid,
            "cam1_wall_detect": {"hsv_lo": list(lo), "hsv_hi": list(hi), "area": [500, 6000], "x_min": 600}}
        print(f"  서명 저장({len(pts)}점): 각 {ang:+.2f}° 간격 {L:.0f}px 중점 ({mid[0]:.0f},{mid[1]:.0f})")
    else:
        print("  ⚠ 벽 점 0개 — 서명 저장 안 함")
    json.dump(ref, open(GRASP_REF, "w"), ensure_ascii=False, indent=1)
    pr = json.load(open(PICK_REF)) if os.path.exists(PICK_REF) else {}
    pr[color] = {"tcp": tcp0, "grip_close": int(grip_cmd), "grip_real": gr, "made": time.strftime("%Y-%m-%d %H:%M"),
                 "note": "사용자 수정 랙 파지 자세(9/5) — dot_calib 골든은 그대로"}
    json.dump(pr, open(PICK_REF, "w"), ensure_ascii=False, indent=1)
    print(f"  랙 파지 TCP 기록 {[round(v,1) for v in tcp0[:3]]} → pick_ref_0905.json")
    return gr


# ★벽별 파지·안착 보정(9/5 교차검증 실측): 파랑 골든만 기준으로 다른 벽 골든 안착 TCP 를 예측했을 때의 잔차.
#   공칭(중앙 파지) 예측 − 실제 골든 = 그 벽의 "치우친 파지 + 안착 특성". 골든 시점 베이스 yaw −90.03° 기준의
#   로봇 프레임 Δ 를 베이스 프레임으로 바꿔 저장하고, 실행 시 현재 베이스 yaw 로 돌려 적용한다.
#   노랑 +X 치우침 사고(9/5 18:0x)의 직접 원인 = 이 보정을 빼고 공칭으로 계산한 것.
GOLDEN_YAW = -90.032
RZ_BIAS = 0.40      # ★9/5 실측: 노랑 +0.42°, 빨강 +0.40° 로 일관된 yaw 편향 → 일괄 보정
WALL_DELTA_ROBOT = {"blue": (0.0, 0.0), "yellow": (-1.27, 1.81),
                    "red": (-4.51, 3.50), "red_s": (229.0 - 234.92, -511.5 + 510.56)}


def wall_delta_now(color, yaw_now):
    dx, dy = WALL_DELTA_ROBOT.get(color, (0.0, 0.0))
    a = math.radians(yaw_now - GOLDEN_YAW)          # 골든 때 대비 베이스가 돈 만큼 같이 돌린다
    return (dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a))


USE_LEGACY_DELTA = False   # ★벽별 상수 보정(WALL_DELTA/RZ_BIAS)은 파지 치우침을 상수화한 것 → 호버 정렬이 있으면 끈다(설계 3단계)
BASE_LAST = "/home/ar/bf2_console/base_pose_last.json"


BASE_RMS_MAX = 2.5         # 9/7: 기둥 4점이 완전한 직사각형이 아니라 rms 1.55~2.00 이 정상(8/8 재현) — 2.0 기준은 경계에 걸려 1/4 로 떨어졌다
RETRY_IN_PLACE = 4         # ★제자리에서 다시 보는 횟수(로봇 안 움직임, 1회차에 노출 사다리 포함)
BASE_SEARCH_MOVE = False   # ★★사용자 철칙(9/10): 베이스 못 찾아도 로봇을 움직여 찾지 않는다. 멈추고 알린다.
SEARCH_ROUNDS = 4          # 베이스 4점 탐색 최대 라운드
SEARCH_MAX_TOTAL_MM = 70.0 # ★관측자세에서 이보다 멀리 가면 탐색 중단(9/7: 반사광 쫓아 117mm 이탈)
SEARCH_CLIP_MM = 60.0      # 한 번에 옮기는 카메라 XY 상한
LOOK_UP_MM = 80.0          # 아무것도 안 보이면 이만큼 올라가 넓게 본다(관측만, 측정은 z650 에서)


def _cam_shift_to_center(px_center, Jinv, p0=(640.0, 360.0), scale=1.0):
    """화면점 px_center 를 화면 중심 p0 로 가져오는 로봇 XY 이동량(px_to_robot 부호 규칙과 동일)."""
    d = (Jinv * scale) @ np.array([p0[0] - px_center[0], p0[1] - px_center[1]], float)
    n = float(np.hypot(*d))
    if n > SEARCH_CLIP_MM:
        d = d * (SEARCH_CLIP_MM / n)
    return float(d[0]), float(d[1])


def _all_color_blobs_center(img):
    """탐색용 색점 중심. 9/7 사고: 직사광 반사(면적 1600~2000, 화면 끝)만 잡히는데 그걸 쫓아가
    로봇이 관측자세에서 117mm 벗어남 → ①가장자리 60px 제외 ②면적 상한 800 ③기둥 색 최소 2종."""
    import pillar_dots as PD
    d = PD.detect(img, None)
    pts, cols = [], set()
    for c, lst in d.items():
        for p in lst:
            if 30 < p[2] < 800 and 60 <= p[0] <= 1220 and 60 <= p[1] <= 660:
                pts.append(p[:2]); cols.add(c)
    if len(pts) < 2 or len(cols) < 2:            # 기둥은 노랑·빨강·파랑이 섞여 있다. 한 색만 보이면 반사광 의심
        return None, 0
    return (float(np.mean([q[0] for q in pts])), float(np.mean([q[1] for q in pts]))), len(pts)


def find_base_4pts(holding=False):
    """★기둥 4점이 다 보일 때까지 찾는다(사용자 지시: 안 되면 멈추지 말고 찾는 방법을 마련).
    라운드마다: 건강 게이트(노출 사다리 포함) → 4/4 면 끝. 아니면
      · 3점 보임 → 그 중심을 화면 중심으로 오게 XY 이동(z650 유지)  · 그 미만 → z+80 올라가 색점 중심을 찾아 XY 이동 후 z650 복귀
    측정은 항상 z650 에서 하고, 카메라가 OBS 에서 옮겨간 만큼(Δ)은 base pose 에 더한다(px_to_robot 유도: 진짜 = C + Jinv(p0−p') + Δ).
    반환 (px4, Δxy). 끝까지 못 찾으면 예외."""
    import slot_target as STG, hover_align as HA
    Jinv, _mp = STG.load_map()
    # ★9/10 사용자 지시 "로봇이 밀리잖아 — 이런 일 없도록":
    #   탐색 이동은 카메라를 관측자세에서 밀어내고, 그 자세로 잰 베이스는 ArUco 자를 못 쓴다
    #   (9/10 09:42 red_s: 3분 30초 탐색 → 자 미적용 → 베이스가 앞 세 색과 0.8~0.9mm 어긋남).
    #   실패는 대개 일시적이므로 **움직이기 전에 제자리에서 먼저 다시 본다.**
    for _t in range(RETRY_IN_PLACE):
        px4, _w = STG.pillars_px(mask_held=holding)
        if px4 and len(px4) == 4:
            if _t:
                print(f"  (제자리 재시도 {_t + 1}회에 4점 — 로봇 안 움직임)")
            return px4, (0.0, 0.0)
        if _t == 0:
            health_gate()                      # 노출 사다리 — 로봇은 움직이지 않는다
        time.sleep(0.6)

    # ★★9/10 사용자 철칙: "로봇 밀리는·흔들리는 자세 하지 말아줘".
    #   탐색 이동은 카메라를 관측자세에서 밀어내고, 그 자리로 잰 베이스는 ArUco 자를 못 써서
    #   기준이 0.8~0.9mm 어긋난다(9/10 09:42 red_s 실측). 그래서 **기본은 움직이지 않고 멈춘다.**
    #   정말 필요할 때만 BASE_SEARCH_MOVE=True 로 켠다(그 경우에도 끝나면 관측자세로 돌아온다).
    if not BASE_SEARCH_MOVE:
        px_n, _w = STG.pillars_px(n=3, mask_held=holding)
        raise RuntimeError(
            f"베이스 기둥 4점 미검출(지금 {len(px_n) if px_n else 0}점) — 로봇을 움직이지 않고 정지했다. "
            "조명·반사·벽 가림을 확인하고 다시 누를 것(탐색 이동이 필요하면 BASE_SEARCH_MOVE 를 켤 것)")

    _home_needed = False
    for r in range(SEARCH_ROUNDS):
        _c = st()["tcp"]
        if math.hypot(_c[0] - OBS[0], _c[1] - OBS[1]) > SEARCH_MAX_TOTAL_MM:
            speed(SPD_MOVE); move([OBS[0], OBS[1], OBS[2]] + list(OBS[3:]), tag="탐색 한계 초과 → 관측자세 복귀")
            raise RuntimeError(f"베이스 탐색이 관측자세에서 {SEARCH_MAX_TOTAL_MM:.0f}mm 넘게 벗어남 — 조명/반사 의심(직사광이면 블라인드)")
        cur = st()["tcp"]
        if abs(cur[2] - OBS[2]) > 1.0:
            speed(SPD_MOVE); move([cur[0], cur[1], OBS[2]] + list(OBS[3:]), tag="관측 높이 z650"); cur = st()["tcp"]
        dxy = (cur[0] - OBS[0], cur[1] - OBS[1])
        if health_gate():
            px4, why = STG.pillars_px(mask_held=holding)
            if px4 and len(px4) == 4:
                if abs(dxy[0]) + abs(dxy[1]) > 0.5:
                    # ★밀린 자리에서 그대로 쓰지 않는다 — 관측자세로 돌아가 한 번 더 본다.
                    #   거기서 4점이 잡히면 그 값을 쓴다(Δ0, ArUco 자 적용 가능). 안 잡히면 밀린 값을 쓰되 경고.
                    print(f"  탐색으로 4점 확보(Δ {dxy[0]:+.1f},{dxy[1]:+.1f}mm) → 관측자세로 복귀해 재측정")
                    speed(SPD_MOVE); move([OBS[0], OBS[1], OBS[2]] + list(OBS[3:]), tag="관측자세 복귀(재측정)")
                    time.sleep(0.6)
                    for _t in range(RETRY_IN_PLACE):
                        p2, _w2 = STG.pillars_px(mask_held=holding)
                        if p2 and len(p2) == 4:
                            print("  ✓ 관측자세에서 4점 재확보 — 밀린 좌표를 쓰지 않는다")
                            return p2, (0.0, 0.0)
                        time.sleep(0.6)
                    print("  ⚠ 관측자세에선 여전히 4점 미달 → 탐색 자리 값 사용(ArUco 자 미적용)")
                    speed(SPD_MOVE); move([OBS[0] + dxy[0], OBS[1] + dxy[1], OBS[2]] + list(OBS[3:]), tag="탐색 자리 복귀")
                    time.sleep(0.5)
                return px4, dxy
        px, why = STG.pillars_px(n=3, mask_held=holding)
        if px and len(px) >= 3:
            c = (float(np.mean([q[0] for q in px])), float(np.mean([q[1] for q in px])))
            dx, dy = _cam_shift_to_center(c, Jinv)
            print(f"  기둥 {len(px)}점만 보임(중심 px {c[0]:.0f},{c[1]:.0f}) → 카메라 ({dx:+.1f},{dy:+.1f}) 이동해 재탐색 [{r+1}/{SEARCH_ROUNDS}]")
            speed(SPD_MOVE); move([cur[0] + dx, cur[1] + dy, cur[2]] + list(cur[3:]), tag="탐색 XY"); time.sleep(0.5)
            _home_needed = True
            continue
        # 3점도 안 보임 → 올라가서 넓게 본다
        speed(SPD_MOVE); move([cur[0], cur[1], OBS[2] + LOOK_UP_MM] + list(cur[3:]), tag=f"탐색 상승 z{OBS[2] + LOOK_UP_MM:.0f}"); time.sleep(0.6)
        c, n = _all_color_blobs_center(HA.grab("wrist"))
        if c is None:
            print(f"  z{OBS[2] + LOOK_UP_MM:.0f} 에서도 색점 {n}개 — 베이스가 시야에 없음 [{r+1}/{SEARCH_ROUNDS}]")
            move([cur[0], cur[1], OBS[2]] + list(cur[3:]), tag="z650 복귀")
            continue
        dx, dy = _cam_shift_to_center(c, Jinv, scale=(D_OBS_PILLAR + LOOK_UP_MM) / D_OBS_PILLAR)
        print(f"  z{OBS[2] + LOOK_UP_MM:.0f} 색점 {n}개 중심 ({c[0]:.0f},{c[1]:.0f}) → 카메라 ({dx:+.1f},{dy:+.1f}) 이동 후 z650 재탐색 [{r+1}/{SEARCH_ROUNDS}]")
        move([cur[0] + dx, cur[1] + dy, OBS[2] + LOOK_UP_MM] + list(cur[3:]), tag="탐색 XY(상공)")
        move([cur[0] + dx, cur[1] + dy, OBS[2]] + list(cur[3:]), tag="z650 복귀"); time.sleep(0.5)
    raise RuntimeError(f"베이스 기둥 4점을 {SEARCH_ROUNDS}라운드 탐색에도 못 찾음(노출 사다리·XY 재중심·상공 관측 포함) — 정지")


D_OBS_PILLAR = 377.0


def measure_base(holding=False):
    """설계 ①: 관측자세(z650)에서 베이스 자세(로봇 좌표). 4점이 안 보이면 find_base_4pts 가 카메라를 옮겨 찾는다.
    ★매 사이클 빈 손으로 호출(직전 삽입이 베이스를 밀 수 있음). 직전 측정과의 차이를 함께 보고."""
    import slot_target as STG
    cur = st()["tcp"]
    if max(abs(cur[i] - OBS[i]) for i in range(3)) > 2.0:
        speed(SPD_MOVE)
        move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="상승 SAFE")
        move(OBS, tag="관측자세")
        speed(1)
    Jinv, mp = STG.load_map()
    px4, dxy = find_base_4pts(holding)
    try:                                                     # ★9/12 베이스 단계 기준점(손목캠): 밑판 기둥 4점
        if _RO is not None and px4:
            _RO.publish_pts("base", "wrist", "1 BASE",
                            now=[[f"P{i+1} {p[3] if len(p)>3 else ''}", (p[3] if len(p)>3 else "green"), p[0], p[1]] for i, p in enumerate(px4)],
                            note=f"base pillars {len(px4)}/4")
    except Exception:
        pass
    a = json.load(open(STG.ANCH))
    pose, rms, _ = STG.base_pose_robot(px4, Jinv, tuple(a["C"]), tuple(a["p0"]))
    if abs(dxy[0]) + abs(dxy[1]) > 0.01:
        pose = HG.Pose2D(pose.x + dxy[0], pose.y + dxy[1], pose.yaw_deg)
    prev = json.load(open(BASE_LAST)) if os.path.exists(BASE_LAST) else None
    if prev:
        print(f"  베이스 이동(직전 측정 대비): Δx {pose.x - prev['x']:+.2f} Δy {pose.y - prev['y']:+.2f} Δyaw {HG.wrap_deg(pose.yaw_deg - prev['yaw']):+.3f}°  (직전 {prev['made']})")
    json.dump({"x": pose.x, "y": pose.y, "yaw": pose.yaw_deg, "rms": rms, "made": time.strftime("%Y-%m-%d %H:%M:%S")}, open(BASE_LAST, "w"))
    print(f"  베이스(로봇) x {pose.x:.2f} y {pose.y:.2f} yaw {pose.yaw_deg:+.3f}° rms {rms:.2f}mm  (기준점 {a['color']} {a['made']})")
    return {"base": pose, "rms": rms, "anchor": a["color"], "anchor_made": a["made"]}


def target_for(color, B, grip=None):
    """베이스 자세 B + 파지 측정 grip → 그 벽의 목표 TCP (x, y, rz)."""
    import slot_target as STG
    pose = B["base"]
    slot = HG.SLOTS[STG.WALL_SLOT[color]]
    rz_ref = json.load(open(CAL))["refs"][color]["insert_tcp"][5]
    tgt, _dj = HG.target_tcp(pose, slot, grip or STG.grip_nominal(color), rz_ref)
    rz = HG.wrap_deg(STG.rz_line_sym(tgt.yaw_deg, rz_ref) + (RZ_BIAS if USE_LEGACY_DELTA else 0.0))
    ddx, ddy = wall_delta_now(color, pose.yaw_deg) if USE_LEGACY_DELTA else (0.0, 0.0)
    return dict(B, x=tgt.x + ddx, y=tgt.y + ddy, rz=rz, delta=(ddx, ddy))


def plan(color, grip=None, holding=False):
    return target_for(color, measure_base(holding), grip)


def _grip_ok(gr, g_close, color):
    """파지 판정: 실측 > 닫힘값이면 물었음. 같으면(red_s 는 물어도 8→8) 손목캠 벽 점으로 2차 판정."""
    try:
        gv = int(gr)
    except Exception:
        gv = -1
    if gv > g_close:
        return True, f"그리퍼 {gr} > 닫힘 {g_close}"
    held = held_wall_dots(color)
    if held:
        return True, f"그리퍼 {gr} = 닫힘값이지만 손목캠 벽 점 {len(held)}개"
    return False, f"그리퍼 {gr}, 닫힘 {g_close}, 벽 점 0"


def run(color, seat=False, do_pick=True, target=None, align=True, len_gate=False, grasp_gate=True, grasp_teach=False, align_stop=False):
    """★설계 4단계 고정 순서(9/5 밤, 사용자 설계):
      ① 빈 손 베이스 재확인(관측자세)  — 매 사이클. 4점 안 보이면 카메라를 옮겨 찾는다(find_base_4pts)
      ② 랙 재관측 → 벽 중앙 → 하강 파지 → 파지 판정  — 매 픽. 양끝 안 보이면 카메라를 옮겨 찾는다(rack_find)
      ③ 들어올림 → 파지 편차 측정(게이트) → ①의 베이스로 목표 TCP → 운반 → 호버 z478 → z+85  (파지 재판정은 닫을 때 1회만)
      ④ z+85 에서 두 카메라 호버 정렬(기둥 기준 상대) → 정렬된 TCP 로 막힘감시 하강 → 안착 시 기준 자동 승격
    target=(x,y,rz) 를 주면 ①③ 계산을 건너뛰고 그 목표로(디버그용). --no-pick 은 이미 든 상태(①은 든 채 마스크 측정)."""
    pick, hover, g_open, g_close = pick_pose(color)
    grip = None; rack_dang = None
    tgt_rot = None
    try:
        # ---------- ① 베이스 재확인
        if target is not None:
            P = {"x": target[0], "y": target[1], "rz": target[2], "delta": (0.0, 0.0), "anchor": "직접지정", "anchor_made": "-",
                 "base": HG.Pose2D(0.0, 0.0, 0.0), "rms": 0.0}; B = P
            print("  ⚠ 목표 직접 지정 — 베이스 측정 생략")
        elif do_pick:
            print("① 베이스 재확인(빈 손)"); B = measure_base(holding=False)
        else:
            print("① 베이스 재확인(든 채, 마스크)"); B = measure_base(holding=True)
            g, info = grasp_measure(color)
            if g is None and "든 벽 점 0" in str(info):
                held_wall_dots_expo(color)          # 노출 사다리로 벽 점을 찾은 뒤 재측정
                g, info = grasp_measure(color)
            if g: grip = g; print(f"  파지 편차({info['how']}): 가로 {info['across_mm']:+.2f} 길이 {info['along_mm']:+.2f}mm 각 {info['dang']:+.2f}°")
            else: print("  ⚠ 파지 편차 측정 불가:", info)
        # ---------- ② 랙 재관측 → 파지
        if do_pick:
            print("② 랙 재관측 → 중앙 파지")
            speed(SPD_MOVE); cur = st()["tcp"]
            move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="상승 SAFE")
            obs = json.load(open(RACK_OBS))["tcp"]
            move([obs[0], obs[1], SAFE_Z, 180.0, 0.0, 180.0], tag="랙 위 SAFE")
            print("  그리퍼 열기 →", gripper(g_open))
            move(obs, tag="랙 중앙 관측자세")
            tg = rack_find(color)                         # ★새 프레임으로 벽 중앙·길이 게이트, 양끝 안 보이면 카메라 옮겨 재탐색
            if tg is None:
                raise RuntimeError("랙 관측에서 벽 양끝을 끝내 못 잡음(3라운드 탐색 후) — 파지 중단")
            gx, gy, rack_dang, e = tg
            print(f"  랙: 벽 중앙 ({e['mid'][0]:.0f},{e['mid'][1]:.0f}) 길이 {e['len_px']:.0f}px 각 {e['ang']:+.2f}° → 파지 XY ({gx:.1f},{gy:.1f}) 각차 {rack_dang:+.2f}°")
            rack_len_check(color, e, strict=len_gate)     # 뎁스 물리 길이 검산(색점과 독립, 보고만 — 검은 랙에서 뎁스 불안정)
            rot = [180.0, 0.0, 180.0]                     # ★랙 하강은 항상 rz 180(9/5: −177 잔류 → 3° 물림·동결)
            speed(SPD_MOVE); move([gx, gy, obs[2]] + rot, tag="파지 XY 위(관측 높이)")
            speed(SPD_DESC); move([gx, gy, pick[2] + 40] + rot, tag="픽 −40")
            speed(SPD_SEAT); move([gx, gy, pick[2]] + rot, tol=0.8, tag="픽 자세")
            gr = gripper(g_close); print("  그리퍼 닫기 →", gr)
            ok, why = _grip_ok(gr, g_close, color)
            if not ok:
                raise RuntimeError("빈 파지 의심(" + why + ") — 정지")
            print("  파지 판정 OK:", why)
            # ---------- ③ 미끄러짐 확인 → 파지 편차 → 목표
            print("③ 파지 검증 → 목표 계산")
            speed(SPD_DESC); move([gx, gy, hover[2]] + rot, tag="들어올림")
            print(f"  (참고) 들어올린 뒤 그리퍼 {grip_read()}")           # 재판정 없음(사용자: 실제 이탈 사례 0)
            hd, _w = held_wall_depth(color)                 # 참고 출력만(뎁스 불신)
            if hd: print(f"  (참고) 든 벽 뎁스 {hd['d_w']:.0f}mm · 축 {hd['ang_img']:+.2f}° · 폭 {hd['span_px']:.0f}px")
            if grasp_teach:
                # ★서명 티칭: 랙 중앙 파지가 곧 공칭 파지. 지금 든 점을 서명으로 저장(+파랑이면 축척 캘리브), 게이트 생략
                save_grasp_sig_now(color, g_close, grip_read())
                grasp_gate = False
            g, info = grasp_measure(color, rack_dang=rack_dang)
            if g is None:
                if grasp_gate:
                    raise RuntimeError("파지 편차 측정 불가: " + str(info) + " — 정지(--no-grasp-gate 로만 우회)")
                print("  ⚠ 파지 편차 측정 불가:", info, "→ 공칭 파지")
            else:
                grip = g
                print(f"  파지 편차({info['how']}): 가로 {info['across_mm']:+.2f} 길이 {info['along_mm']:+.2f}mm 각 {info['dang']:+.2f}° (축척 {info['scale']:.4f})")
                if grasp_gate and (abs(info['along_mm']) > GRASP_GATE_MM or abs(info['across_mm']) > GRASP_GATE_MM or abs(info['dang']) > GRASP_GATE_DEG):
                    raise RuntimeError(f"파지 편차 게이트 초과(길이 {info['along_mm']:+.2f} 가로 {info['across_mm']:+.2f}mm 각 {info['dang']:+.2f}°) — 정지, 재파지는 사용자 판단")
        if target is None:
            P = target_for(color, B, grip)                # ★①의 베이스(빈 손 측정) + 파지 측정 → 목표. 든 채 재측정 안 함
        print(f"  목표 [{color}] x {P['x']:.2f} y {P['y']:.2f} rz {P['rz']:+.2f}  호버 z{HOVER_Z:.0f} 안착 z{SEAT_Z[color]:.0f}"
              + ("" if not USE_LEGACY_DELTA else f"  (구 상수보정 {P['delta'][0]:+.1f},{P['delta'][1]:+.1f})"))
        tgt_rot = [180.0, 0.0, P["rz"]]
        speed(SPD_MOVE); cur = st()["tcp"]
        move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="상승 SAFE")
        move([P["x"], P["y"], SAFE_Z] + tgt_rot, tag="목표 위 SAFE(rz 정렬)")
        print(f"  (참고) 운반 후 그리퍼 {grip_read()}")                 # 재판정 없음(사용자 지시)
        speed(SPD_DESC); move([P["x"], P["y"], HOVER_Z] + tgt_rot, tag="목표 호버 z478")
        if not seat and not align_stop:
            print("호버 정지. --seat 를 주면 z+85 에서 호버 정렬 후 하강, --align-stop 이면 정렬까지만."); return
        # ---------- ④ 호버 정렬 → 하강
        zs = SEAT_Z[color]
        speed(SPD_SEAT); move([P["x"], P["y"], zs + 85] + tgt_rot, tag="기둥 꼭대기 위 z+85")
        print("④ 호버 정렬(두 카메라, 기둥 기준 상대)")
        import hover_align as HA
        if align:
            if HA.load_ref(color) is None:
                raise RuntimeError(f"{color} 호버 기준 없음 — z+85 에서 사용자 정렬 확인 후 `hover_align.py ref {color}` (--no-align 으로만 우회)")
            try:
                HA.align(color)                            # 미수렴·발산·측정 실패·카메라 불일치 = 예외 → 정지
            finally:
                HA.restore_expo()                          # z440 용 노출(83 등)을 관측 노출로 되돌림(다음 ① 측정 보호)
            cur = st()["tcp"]                              # ★정렬로 움직인 TCP 로 하강(옛 P 로 내리면 정렬이 되돌아감)
            P["x"], P["y"] = cur[0], cur[1]; tgt_rot = [180.0, 0.0, cur[5]]
            print(f"  정렬 후 하강 기준 x {cur[0]:.2f} y {cur[1]:.2f} rz {cur[5]:+.2f}")
        else:
            print("  ⚠ 호버 정렬 생략(--no-align)")
        if align_stop:
            print("★ z440 정렬 완료 — 하강은 사용자 허락 대기(수동 하강). 로봇 정지."); return
        descend_monitored(color, P["x"], P["y"], tgt_rot, zs, g_close)
        if align:
            HA.promote_ref(color)                          # ★안착 성공 사이클의 정렬 상태를 다음 기준으로
    except Exception as e:
        post("stop", {"dry_run": False})
        print("❌ 정지:", e)
    finally:
        speed(1)
        s_ = st(); print("현재 tcp", [round(v, 1) for v in s_["tcp"]], "grip", s_.get("gripper"), "frozen", s_.get("frozen"))


# ★단일 출처(9/7 17:0x: 같은 이름이 두 군데 있어 앞엣것이 무시되던 것을 정리).
#   파랑 200 — 랙 위 z465 에서 위쪽 점은 expo167 에 면적 273 이라 하한 500 에 걸려 버려졌고,
#   그 탓에 늘 '화면 바닥에 잘린 아래쪽 점'만 남아 중심이 흔들렸다.
HELD_AREA_MIN = {"blue": 200, "yellow": 500, "red": 400, "red_s": 250, "red_in": 250,
                 "blue_in": 200, "yellow_in": 150}
# ★9/7 20:1x 오진 기록: red_in 이 "벽 점 0개"라 x_min 을 740 으로 내렸더니 잡히긴 했는데,
#   그건 든 벽이 아니라 **베이스 기둥의 빨간 점**(785,522)이었다 — 벽은 이미 떨어져 있었다.
#   x≥820 은 바로 그 베이스를 배제하려고 있는 값이다. 완화 철회. 색별 예외가 필요하면 여기에만 둔다.
# ★9/10 A타입 내벽(노랑): 든 벽 점이 x≈764 · 면적 ~210 이라 외벽 기준(x≥820 · 면적 250)에 걸린다.
#   자세가 달라 창이 안 맞는 것이므로 색별로 넓힌다(hover_align.HELD_BOX_BY_COLOR 와 같은 취지).
HELD_X_MIN = {"yellow_in": 700}
HELD_NEAR_PX = 90.0


def held_wall_dots_jam(color):
    """★9/8 신설 — 막힘 감시 전용 벽 점.

    문제: 든 벽 점이 화면 아래끝에 걸리면(내벽·파랑) 하강할수록 아래가 더 잘려
      **보이는 부분의 무게중심이 위로 튄다**. 벽은 안 움직였는데 밀린 것처럼 보인다.
      9/8 실측(내벽): z+9 까지 bbox 높이 56 으로 일정 → z+6 에서 34 로 급감,
      같은 순간 '이동 14.1px' 로 잡혀 안착 6mm 앞에서 막힘 오판.
    해법: **잘린 점만** y 를 무게중심 대신 bbox 상단으로 바꾼다. 위쪽은 잘림과 무관해 안 흔들린다.
      잘리지 않은 점은 지금 그대로(무게중심). 기준·감시가 같은 함수를 쓰므로 정의가 자동으로 일치한다.
    ※ 이 함수는 막힘 감시에서만 쓴다 — 정렬·파지 판정·서명은 기존 held_wall_dots 그대로.
    """
    # ★9/8 철회: 처음엔 '잘린 점은 bbox 상단을 추적'으로 고쳤는데 효과가 없었다 —
    #   점이 아래로 나가면서 잘리면 남은 조각의 상단도 같이 내려간다(z+6 에서 28.3px, 그대로 오판).
    #   그래서 위치 정의는 원래대로 두고, 대신 '면적'을 같이 돌려줘 신뢰도 판단에 쓴다.
    return [(q[0], q[1], q[2]) for q in held_wall_dots(color)]


def held_wall_dots_expo(color, ladder=(167, 250, 83, 333, 42, 20)):
    """★9/7: 든 벽 점이 보이는 노출은 조명에 따라 오르내린다(같은 날 83 에서 보이다가 나중엔 250 에서만 보임).
    한 방향으로만 올리지 말고 사다리 전체를 훑는다. 찾으면 그 노출을 유지(이어지는 하강 감시도 같은 노출이어야)."""
    w = held_wall_dots(color)
    if w:
        return w
    import hover_align as HA
    for e in ladder:
        try:
            HA.set_expo(e)
            time.sleep(getattr(HA, 'EXPO_SETTLE_S', 1.4))   # ★9/10: 전환 중 프레임 방지
        except Exception:
            break
        w = held_wall_dots(color)
        if w:
            print(f"  (든 벽 점: 노출 {e} 로 올려 {len(w)}개)", flush=True)
            return w
    return []


def held_wall_dots(color):
    """물고 있는 벽의 색점(손목캠) — 막힘 감시·파지 판정용.
    ★9/6 정정: 면적 순으로 고르면 노랑 벽을 든 z440 에서 우상 **노란 기둥 점**(면적 ~600)이 든 벽 점으로 섞여 들어와
      하강 중 기둥 px 이동을 '막힘'으로 오판한다. 파지 서명(grasp_ref)의 점 자리 ±HELD_NEAR_PX 안의 점을 우선 고르고,
      서명이 없을 때만 면적 순(그때도 x≥600·면적 하한은 색별)."""
    import cv2, numpy as np
    b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
    m = wall_mask(img, color)
    # ★9/7 17:12: 이 함수만 y 상한 700 을 쓰고 서명 쪽(_held_blobs)은 715 라, 새 파랑 서명 점(y 701.3)이
    #   1.3px 차이로 여기서만 배제돼 "막힘 감시용 벽 점 없음 — 하강 금지" 가 났다. 검출 경로를 하나로 합친다.
    amin = HELD_AREA_MIN.get(color, 400)
    ref = load_grasp_ref(color)
    anchors = [(q[0], q[1]) for q in (ref or {}).get("cam1_wall_pd") or []]
    # ★9/7 20:36 헛정지: 서명이 없으면 면적순 폴백으로 떨어지는데 그 창이 x≥600 이라, 랙 위에서
    #   **랙에 꽂힌 벽**의 점(x≈725~732)을 "그리퍼에 벽이 있다"로 세어 그리퍼 열기 게이트가 헛섰다.
    #   서명이 있으면 그 점 근처만 고르므로 창은 넓어도 되지만, 서명이 없을 때는 든 벽 영역(x≥820)만 본다
    #   — 저장된 네 색 서명의 든 벽 점이 전부 x 983~1029 이라 이게 실측에 맞는 창이다.
    x_min = min(600, HELD_X_MIN.get(color, 820)) if anchors else HELD_X_MIN.get(color, 820)
    pts = _held_blobs(img, WALL_DOT_HSV[color], [amin, 6000], x_min)
    if anchors:
        out = []
        for ax, ay in anchors:
            c = [q for q in pts if math.hypot(q[0] - ax, q[1] - ay) <= HELD_NEAR_PX]
            if c:
                out.append(min(c, key=lambda q: math.hypot(q[0] - ax, q[1] - ay)))
        return out
    pts.sort(key=lambda q: -q[2])
    return pts[:2]


JAM_PX = 6.0        # (구) 누적 임계. 13:41 실기: 채널 마찰로 3mm 마다 0.6~0.9px 씩 서서히 밀려 z+9 에서 누적 6.5px → 오판 정지(9/2 성공 삽입도 접촉 6~8px 이었음)
# ★9/7 15:19 오판: z+76 에서 단계 +5.1px 로 정지. 같은 날 성공한 하강(14:46)의 단계 잡음 최대가 4.7px 이라
#   임계 5.0 은 잡음 바닥에 붙어 있었다. 진짜 막힘은 단계당 ≈33px, 마찰 밀림은 0.6~0.9px 로 사이가 넓다.
#   → 단계 임계를 8.0 으로 올리고(진짜 막힘의 1/4), 누적 15px(≈1.4mm) guard 는 그대로 두어 안전을 유지한다.
JAM_STEP_PX = 5.0   # 한 단계(3mm) 안에서 이만큼 튀면 막힘 (9/7 사용자 지시로 8.0 → 5.0 원복: 게이트는 엄격하게, 대신 재확인을 3프레임 중앙값으로 보강)
# ★9/7 사용자 지적("왜 355까지 안 들어가?"): 벽은 3mm 내려갈 때마다 마찰로 ≈1px 씩 꾸준히 밀린다.
#   하강을 z+98 에서 시작하면 33단계 동안 33px 이 쌓여 **누적 15px 은 바닥 전에 반드시 넘는다**.
#   그 지점이 옛 성공창(바닥 8mm)에 걸리면 z+7 을 '안착'이라 부르고 멈춰 벽이 7mm 덜 들어갔다.
#   → 누적을 하강 전체가 아니라 **최근 JAM_WIN_STEPS 단계 창**으로 본다(마찰 5px vs 진짜 이탈 15px+).
# ★9/8 19:07 결론 정정(사용자 육안 "안 내려가 6MM에서"): 면적 반토막(2639→955→395→261→0)은
#   '점이 화면 밖으로 빠지는 잡음' 이 아니라 **벽이 죠 안에서 위로 밀려 올라가는 실물 현상**이었다.
#   내벽은 z+6 부근에서 물리적으로 막히고, 로봇이 계속 내려가니 벽만 그리퍼 안에서 위로 밀린다
#   → 벽 점이 화면 아래로 빠져 면적이 줄고 중심이 튄다. 즉 **막힘 판정이 처음부터 옳았다.**
#   내가 넣었던 '면적 붕괴 프레임 건너뛰기'와 '무감시 마지막 하강'은 막힌 벽을 계속 눌러
#   기둥 파손(9/2 사고)으로 가는 길이라 철회한다. 면적 급감은 이제 **막힘 징후로 기록만** 한다.
#   → 남은 진짜 과제는 검출이 아니라 '내벽이 마지막 6mm 를 왜 못 들어가는가'(XY/rz/깊이).
SIG_AREA_OK = 0.6    # 감시 기준 노출 선택: 서명 면적의 이 비율 이상이면 '온전한 점' 으로 본다
JAM_AREA_KEEP = 0.65   # 기준 면적 대비 이 미만 = 벽이 죠 안에서 밀려 올라가는 중(로그 기록용, 판정은 종전 그대로)
JAM_TOTAL_PX = 15.0 # 창 안에서 이만큼(≈1.4mm) 밀리면 죠에서 빠지는 중 → 막힘
JAM_WIN_STEPS = 5   # 누적을 보는 창(단계 수) — 15mm 구간
SEAT_TOUCH_MM = 3.0 # 안착 z 로부터 이 안에서의 밀림만 '바닥 접촉(성공)' 으로 본다(8.0 → 3.0 으로 조임)
# ★9/7 사용자 지시: "처음 3mm 씩 6번, 그다음 10mm 씩" — 위험 구간은 벽 밑동이 기둥 사이로 들어가는
#   진입부(기둥 86mm → 하강 시작 z+100 부터 여섯 걸음이 그 구간)다. 채널에 들어가면 기둥이 잡아 준다.
#   단, 바닥 근처는 '안착 접촉'을 3mm 안에서 잡아야 벽을 누르지 않으므로 다시 잘게 간다.
JAM_STEP = 3.0          # 진입부·바닥부 하강 단위(mm)
JAM_STEP_FAST = 10.0    # 채널 안(중간 구간) 하강 단위(mm)
JAM_FINE_HEAD = 6       # 처음 이 횟수만큼은 JAM_STEP
JAM_FINE_TAIL_MM = 15.0 # 안착 z 로부터 이 높이 아래는 다시 JAM_STEP

# ★9/10 사용자 지시: "모든 내벽은 외벽과 다르게 350까지 빨리(속도 15), 340까지 3mm씩 느리게".
#   내벽은 기둥 사이로 들어가는 게 아니라 밑판 홈에 꽂히므로 외벽의 진입부 서행(JAM_FINE_HEAD 6단계)이 필요없다.
#   대신 **안착 10mm 전부터**는 3mm·저속으로 바꿔 바닥 접촉을 놓치지 않는다. 막힘 감시는 그대로 켜져 있다.
INNER_WALLS_DESC = {"red_in", "blue_in", "yellow_in"}
INNER_FINE_TAIL_MM = 10.0   # 안착 z + 이 높이부터 3mm·저속 (안착 340 → 350 부터)
INNER_SPD_CHANNEL = 15      # 그 위 구간 속도(%)
# ★9/11 사용자 지시(A타입부터): 진입부·안착부 저속 1%→2%, 외벽 채널 10%→15%.
#   하강 82초 중 1% 걸음 14개가 63초였다(카메라 확인은 걸음당 0.15초뿐). 값은 house_cycle 이 집 타입별로 세팅한다.
#   B타입은 9/1 동결값(1 / 10) 유지.
SPD_SLOW = 1               # 진입부·안착부(3mm 걸음) 속도(%)
SPD_CHANNEL_OUTER = 10     # 외벽 채널(10mm 걸음) 속도(%)


def descend_monitored(color, x, y, rot, zs, g_close):
    """★막힘 감시 하강(9/5 사고 후 신설). 채널 진입부터 안착까지 JAM_STEP 씩.
    매 단계: ①TCP 도달(정체=막힘) ②그리퍼(놓침) ③손목캠 벽 점 이동(죠 안에서 밀림=막힘).
    하나라도 걸리면 즉시 정지 → 25mm 상승 → 예외. 절대 계속 밀지 않는다."""
    # ★9/8 저녁 실측(사용자 지적 "저녁이라 점 검출이 안 되는 거 아니냐" — 맞았다):
    #   파랑을 문 채 같은 자리에서 노출만 바꿔 보니 250 에서 면적 236(조각) / 333 에서 2015(서명 1672 와 일치).
    #   옛 사다리는 83→167→250→333 순서로 '점이 1개라도 나오는 첫 노출' 에서 멈춰 그 **조각**을 감시 기준으로 잡았고,
    #   중심이 18px 어긋난 채 내려가다 조각이 사라져 '벽 점 소실' 로 섰다. 벽은 잘 들어가고 있었다.
    #   → 게이트가 아니라 검출 입력이 틀린 것이므로, ①색마다 서명을 찍은 노출을 먼저 쓰고
    #     ②그래도 서명 면적의 SIG_AREA_OK 에 못 미치면 사다리를 끝까지 돌려 **면적이 가장 큰 노출**을 고른다.
    import color_lock as CL
    _sig = load_grasp_ref(color) or {}
    _pd = _sig.get("cam1_wall_pd") or []
    area_sig = sum(float(q[2]) for q in _pd if len(q) > 2)
    cand = []
    if _sig.get("expo"):
        cand.append(float(_sig["expo"]))
    cand += [e for e in (333.0, 500.0, 250.0, 167.0, 800.0, 83.0) if e not in cand]
    best = None                                   # (노출, 면적, 점들)
    for ex in cand:
        try:
            CL.expo(set=ex); time.sleep(0.9)
        except Exception:
            break
        d = held_wall_dots_jam(color)
        a = sum(float(q[2]) for q in d)
        print(f"  (막힘 감시 노출 {ex:.0f} → 벽 점 {len(d)}개 면적 {a:.0f})", flush=True)
        if d and (best is None or a > best[1]):
            best = (ex, a, d)
        if best and area_sig and best[1] >= SIG_AREA_OK * area_sig:
            break                                 # 서명 면적에 근접 — 더 돌릴 필요 없다
    ref = []
    if best:
        if best[0] != cand[0] or len(cand) == 1:
            try:
                CL.expo(set=best[0]); time.sleep(0.9)
            except Exception:
                pass
        ref = held_wall_dots_jam(color) or best[2]
        a_now = sum(float(q[2]) for q in ref)
        warn = ""
        if area_sig and a_now < SIG_AREA_OK * area_sig:
            warn = f"  ⚠ 서명 {area_sig:.0f} 의 {a_now/area_sig*100:.0f}% 뿐 — 조명이 부족하다"
        print(f"  (막힘 감시 노출 {best[0]:.0f} 채택 · 벽 점 {len(ref)}개 면적 {a_now:.0f}"
              + (f" / 서명 {area_sig:.0f}" if area_sig else "") + ")" + warn, flush=True)
    if not ref:
        raise RuntimeError("막힘 감시용 벽 점이 손목캠에 없음 — 하강 금지")
    ref_c = (sum(p[0] for p in ref) / len(ref), sum(p[1] for p in ref) / len(ref))
    print(f"  감시 기준 벽 점 {[(round(p[0]),round(p[1])) for p in ref]}")
    ref_area = [float(p[2]) for p in ref]                      # ★기준 면적 — 화면 밖으로 나가는지 판단에 쓴다
    d_prev = 0.0
    d_hist = [0.0]                      # 창 누적용 이력
    z0 = st()["tcp"][2]
    # 15:35·15:57 실기: 두 막힘 모두 'z440→z_seat+60 첫 25mm 무감시 이동' 에서 발생(채널 입구) → 지금 높이에서 바로 JAM_STEP 씩 감시하며 내려간다.
    _inner = color in INNER_WALLS_DESC

    def _step_mm(z_now, n_done):
        # 9/7 사용자 지시: 기둥 진입 안전장치는 예전 그대로 — 처음 JAM_FINE_HEAD 단계는 3mm,
        #   바닥 근처도 3mm, 그 사이 채널 안에서만 10mm. (높이 기준 진입 밴드는 사용자 요청으로 철회)
        if _inner:
            # ★내벽: 안착 +INNER_FINE_TAIL_MM 까지 큰 걸음(경계에 정확히 착지) → 그 아래는 3mm
            h = z_now - zs
            if h <= INNER_FINE_TAIL_MM:
                return JAM_STEP
            return min(JAM_STEP_FAST, h - INNER_FINE_TAIL_MM)
        if n_done < JAM_FINE_HEAD or (z_now - zs) <= JAM_FINE_TAIL_MM + JAM_STEP_FAST:
            return JAM_STEP
        return JAM_STEP_FAST

    # ★9/9 사용자 제안: 채널 안 자유 구간(10mm 스텝)만 속도를 올린다. 진입부(처음 JAM_FINE_HEAD)와
    #   안착부(마지막 JAM_FINE_TAIL_MM)는 1% 그대로 — 9/5 기둥 파손도, 오늘 막힘 4건도 전부 진입부(z+73~76)였다.
    #   판정은 정지 상태에서 재므로 속도와 무관하고, 바뀌는 것은 접촉 순간의 관성뿐이다.
    SPD_CHANNEL = INNER_SPD_CHANNEL if _inner else SPD_CHANNEL_OUTER
    n_step = 0
    z = max(zs, z0 - _step_mm(z0, n_step))
    speed(SPD_SLOW)
    _spd_now = SPD_SLOW
    try:
        while True:
            _want = SPD_SLOW if _step_mm(z, n_step) == JAM_STEP else SPD_CHANNEL
            if _want != _spd_now:
                speed(_want); _spd_now = _want
                print(f"    (하강 속도 {_want}% — {'채널 안 자유 구간' if _want != SPD_SLOW else '진입부/안착부'})")
            move([x, y, z] + rot, tol=0.8, timeout=25, tag=f"z+{z - zs:.0f}")
            g = grip_read()
            cur = held_wall_dots_jam(color)
            # ★빈 손 = 닫힘값 그대로(빨강은 물어도 +1 뿐). red_s 는 물어도 8→8 이라 그리퍼 값으로는 못 가림
            #   → 그리퍼 값이 닫힘값이면 '손목캠 벽 점 소실' 일 때만 놓침으로 판정.
            if g.isdigit() and int(g) <= g_close and not cur:
                raise RuntimeError(f"벽 놓침(그리퍼 {g}, 벽 점 없음)")
            if cur:
                # 15:35 실기: 점 하나가 잠깐 안 잡히면 무게중심이 200px 튀어 '막힘 187px' 오판 → 점별 최근접 매칭 이동량(≤60px)으로.
                ds = []
                for q in cur:
                    r0 = min(ref, key=lambda r: math.hypot(q[0] - r[0], q[1] - r[1]))
                    dq = math.hypot(q[0] - r0[0], q[1] - r0[1])
                    if dq <= 60.0:
                        ds.append(dq)
                if not ds:
                    print(f"    그리퍼 {g} · 벽 점 {len(cur)}개가 기준 근처(60px)에 없음 → 정지", flush=True)
                    raise RuntimeError("막힘 감시 불가(벽 점이 기준 자리에서 사라짐)")
                a_now = sum(float(q[2]) for q in cur)
                a_ref = sum(ref_area) or 1.0
                if a_now < JAM_AREA_KEEP * a_ref:
                    print(f"    그리퍼 {g} · 벽 점 면적 {a_now:.0f} < 기준 {a_ref:.0f}×{JAM_AREA_KEEP:.2f}"
                          f" — 벽이 죠 안에서 위로 밀려 올라가는 중(막힘 징후)", flush=True)
                d = max(ds)
                print(f"    그리퍼 {g} · 벽 점 이동 {d:.1f}px" + (f" (매칭 {len(ds)}/{len(ref)})" if len(ds) != len(ref) else ""), flush=True)
                step = d - d_prev
                d_win = d - d_hist[max(0, len(d_hist) - JAM_WIN_STEPS)]   # 최근 창 안에서의 밀림
                # 9/7 오판: z+76(채널 밖)에서 0.4→7.8px 로 한 프레임만 튀어 정지. 검출 잡음 한 번에 멈추지 않게 재확인한다.
                if step > JAM_STEP_PX or d_win > JAM_TOTAL_PX:
                    # 재확인은 1프레임이 아니라 3프레임 중앙값으로 — 검출 잡음 한 번에 멈추지 않게.
                    d2s = []
                    for _ in range(3):
                        time.sleep(0.25)
                        cur2 = held_wall_dots_jam(color)
                        if not cur2:
                            continue
                        c2 = (sum(p[0] for p in cur2) / len(cur2), sum(p[1] for p in cur2) / len(cur2))
                        r0 = min(ref, key=lambda r: math.hypot(c2[0] - r[0], c2[1] - r[1]))
                        d2s.append(math.hypot(c2[0] - r0[0], c2[1] - r0[1]))
                    if d2s:
                        import statistics as _s
                        d2 = _s.median(d2s)
                        if d2 - d_prev <= JAM_STEP_PX and (d2 - d_hist[max(0, len(d_hist) - JAM_WIN_STEPS)]) <= JAM_TOTAL_PX:
                            print(f"    (재확인 3프레임 중앙값: {d:.1f}px → {d2:.1f}px — 잡음으로 보고 진행)", flush=True)
                            d = d2; step = d - d_prev; d_win = d - d_hist[max(0, len(d_hist) - JAM_WIN_STEPS)]
                d_prev = d; d_hist.append(d)
                if step > JAM_STEP_PX or d_win > JAM_TOTAL_PX:
                    # ★9/5 실증: 채널 끝까지 내려간 뒤 마지막 1mm 에서 7.5px 밀림 = 밑동이 밑판에 닿은 '안착 접촉'.
                    #   바닥 근처(z_seat+8 이내)의 밀림은 막힘이 아니라 성공 신호 → 멈추고 성공 처리(더 누르지 않음).
                    if z <= zs + SEAT_TOUCH_MM:
                        print(f"  ★안착 접촉: 바닥 근처에서 벽 밀림 {d:.1f}px(단계 {step:+.1f}) → 정지(성공)", flush=True)
                        return
                    raise RuntimeError(f"막힘: 벽이 죠 안에서 단계 {step:+.1f}px / 최근{JAM_WIN_STEPS}단계 {d_win:.1f}px(≈{d_win*0.09:.1f}mm) 밀림, 전체 누적 {d:.1f}px (z+{z - zs:.0f})")
                elif d > JAM_PX:
                    print(f"    (마찰 밀림 누적 {d:.1f}px, 단계 {step:+.1f}px — 진행)", flush=True)
            else:
                print(f"    그리퍼 {g} · 벽 점 소실 → 정지", flush=True)
                raise RuntimeError("막힘 감시 불가(벽 점 소실)")
            if z <= zs + 0.01:
                break
            n_step += 1
            z = max(zs, z - _step_mm(z, n_step))
        print(f"  ★안착 z 도달, 그리퍼 {grip_read()}")
    except Exception as e:
        post("stop", {"dry_run": False}); time.sleep(0.5)
        cur = st()["tcp"]
        print(f"  ❌ {e} → 25mm 상승")
        move([cur[0], cur[1], cur[2] + 25.0] + list(cur[3:]), tol=1.0, timeout=30, tag="후퇴 +25")
        raise



def descend_plain(color, x, y, rot, zs, g_close):
    """★내벽 전용 무감시 수직 하강(9/8 사용자 확인: "안 닿는 거 내가 확인했고 그냥 내리면 돼").
    내벽은 z+6 부근에서 손목캠 벽 점이 화면 아래로 빠져 막힘으로 읽히는데 실물은 닿는 데가 없다.
    외벽 4색은 descend_monitored 그대로 — 이 경로는 red_in 에서만 쓴다.
    감시는 안 하지만 ①단계 하강 ②그리퍼 값 ③TCP 도달 실패(정체)는 그대로 본다."""
    z0 = st()["tcp"][2]
    n_step = 0

    def _step_mm(z_now, n_done):
        if n_done < JAM_FINE_HEAD or (z_now - zs) <= JAM_FINE_TAIL_MM + JAM_STEP_FAST:
            return JAM_STEP
        return JAM_STEP_FAST

    z = max(zs, z0 - _step_mm(z0, n_step))
    speed(SPD_SLOW)                 # ★9/11: 집 타입별 저속(A 2% / B 1%)
    try:
        while True:
            move([x, y, z] + rot, tol=0.8, timeout=25, tag=f"z+{z - zs:.0f}")
            g = grip_read()
            print(f"    그리퍼 {g} · (내벽 무감시 하강)", flush=True)
            if z <= zs + 0.01:
                break
            n_step += 1
            z = max(zs, z - _step_mm(z, n_step))
        print(f"  ★안착 z 도달, 그리퍼 {grip_read()}")
    except Exception as e:
        post("stop", {"dry_run": False}); time.sleep(0.5)
        cur = st()["tcp"]
        print(f"  ❌ {e} → 25mm 상승")
        move([cur[0], cur[1], cur[2] + 25.0] + list(cur[3:]), tol=1.0, timeout=30, tag="후퇴 +25")
        raise

def jamtest(color, secs=40):
    """★막힘 감시 벤치 테스트(로봇 정지, 채널 밖): 벽을 든 채 사용자가 죠 안에서 벽을 살짝 밀면
    벽 점 이동이 JAM_PX 를 넘는지 확인. 넘으면 'STOP' 판정 출력(실제로는 정지·상승 호출)."""
    ref = held_wall_dots(color)
    if not ref:
        print("벽 점 없음 — 벽을 물고 있어야 한다"); return
    rc = (sum(p[0] for p in ref) / len(ref), sum(p[1] for p in ref) / len(ref))
    print(f"기준 벽 점 {[(round(p[0]),round(p[1])) for p in ref]} — {secs}초 동안 벽을 죠 안에서 살짝 밀어 보세요")
    t0 = time.time(); worst = 0.0; fired = False
    while time.time() - t0 < secs:
        cur = held_wall_dots(color)
        if not cur:
            print(f"  {time.time()-t0:4.1f}s 벽 점 소실 → STOP 판정"); fired = True
        else:
            c = (sum(p[0] for p in cur) / len(cur), sum(p[1] for p in cur) / len(cur))
            d = math.hypot(c[0] - rc[0], c[1] - rc[1]); worst = max(worst, d)
            flag = "  ← STOP 판정" if d > JAM_PX else ""
            if d > JAM_PX: fired = True
            print(f"  {time.time()-t0:4.1f}s 이동 {d:5.1f}px (≈{d*0.12:4.2f}mm) 그리퍼 {grip_read()}{flag}", flush=True)
        time.sleep(0.5)
    print(f"최대 이동 {worst:.1f}px · 감지 {'작동' if fired else '미작동(밀지 않았거나 문턱 미달)'}")


RACK_REF = "/home/ar/bf2_console/rack_ref_0905.json"
RACK_MAP = "/home/ar/bf2_console/cam2robot_rack.json"
BASE_EXPO_F = "/home/ar/bf2_console/state/house/base_expo.json"   # 4/4 로 통과한 노출·게인 기억(9/9)


def _remember_base_expo():
    """건강 게이트를 4/4 로 통과한 지금 설정을 저장. 다음 벽 BASE 가 이 값부터 시도한다."""
    import color_lock as CL
    try:
        c = CL.current_settings()
        g = None
        if os.path.exists(CL.STORE):
            g = (json.load(open(CL.STORE)).get("apply") or {}).get("gain")
        json.dump({"exposure": c.get("exposure"), "gain": g, "bright": c.get("bright"),
                   "made": time.strftime("%Y-%m-%d %H:%M")}, open(BASE_EXPO_F, "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass


FLICKER_LADDER = (8, 20, 42, 83, 167, 250, 333, 417)   # 9/7 아침 직사광: 83 도 포화(밝기 224) → 저노출 8·20·42 추가(실측 42 에서 검출 최대)
GAIN_LADDER = (16, 64)                        # 노출 사다리로 안 되면 gain 도 바꿔 본다(낮 16 / 밤 64)


def rack_dots(color, n=4):
    """랙 픽 호버(z467)에서 벽의 색점(카메라 정면 아래). n 프레임 평균, 면적 큰 2개."""
    import cv2, numpy as np
    acc = []
    for _ in range(n):
        b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        m = wall_mask(img, color)
        nn, lab, stt, cen = cv2.connectedComponentsWithStats(m); pts = []
        for i in range(1, nn):
            a = int(stt[i, 4]); x, y = cen[i]
            if 60 <= a <= 3000 and 20 <= x <= 1260 and 20 <= y <= 700:
                pts.append((float(x), float(y), a))
        pts.sort(key=lambda q: -q[2]); acc.append(pts[:2]); time.sleep(0.1)
    good = [p for p in acc if len(p) == 2]
    if len(good) < max(2, n // 2):
        return None
    good = [sorted(p, key=lambda q: q[1]) for p in good]
    import statistics as st_
    return [(st_.mean(g[i][0] for g in good), st_.mean(g[i][1] for g in good)) for i in range(2)]


RACK_OBS = "/home/ar/bf2_console/rack_observe_pose.json"


def _cluster_walls(pts, min_dots=2, x_gap=60.0):
    """색점들을 x-간격으로 벽 클러스터로 나눈다(같은 색 벽이 여러 슬롯이면 x 로 갈림).
    각 클러스터는 세로로 늘어선 점 min_dots 개 이상이어야 벽으로 인정(고립 유령 제거)."""
    if not pts:
        return []
    ps = sorted(pts, key=lambda p: p[0]); groups = [[ps[0]]]
    for p in ps[1:]:
        if p[0] - groups[-1][-1][0] <= x_gap:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [g for g in groups if len(g) >= min_dots]


# ★9/7 사용자 확정: 파지 위치는 '양 끝점의 중점'이 아니라 **가운데 점**이다.
#   파랑 = 점 5개 중 3번째 점, 나머지(노랑·빨강·red_s) = 점 4개 중 가운데(2·3번 점의 중점).
#   9/6 실측으로 점 간격이 38.8/53.5/63.1/29.7mm 로 균등하지 않음이 이미 확인됐다 → 끝점 중점은 5mm 치우친다.
#   이 정의를 쓰면 벽이 랙에서 움직여도 파지점이 벽을 그대로 따라간다(사용자 목표).
#   9/7 16:1x 랙 관측 실측(노출 333·500 동일): blue 5점 · yellow 3점 · red 2점 · red_s 2점.
#   빨강 계열은 가운데 스티커가 검출되지 않는다 → 가운데 점을 강제하면 전 프레임이 버려진다.
#   따라서 '기대 개수가 잡히면 가운데 점, 아니면 끝점 중점' 으로 물러난다(로그로 어느 쪽을 썼는지 남긴다).
WALL_DOT_N = {"blue": 5, "yellow": 3, "red": 3, "red_s": 3, "red_in": 3, "blue_in": 3, "yellow_in": 3}   # 9/7 18:1x 색 범위 수정 후 빨강 계열도 3점이 안정적으로 잡힘
RACK_FRAG_MERGE_PX = 18.0                  # 랙 색점 조각 합치기 반경(빨강 맨 위 점이 9+29+9 세 조각으로 갈라짐)
X_WIN_PX = 45.0                            # x_hint 기준 이 안의 점만 그 벽의 것으로 본다(실측 벽 하나의 x 폭 6~10px)


def _grip_point(dots, p1, p2):
    """벽 축을 따라 정렬한 뒤 가운데 점(홀수) 또는 가운데 두 점의 중점(짝수)."""
    ax = (p2[0] - p1[0], p2[1] - p1[1])
    L = math.hypot(*ax) or 1.0
    ax = (ax[0] / L, ax[1] / L)
    srt = sorted(dots, key=lambda q: (q[0] - p1[0]) * ax[0] + (q[1] - p1[1]) * ax[1])
    k = len(srt)
    if k % 2 == 1:
        return (srt[k // 2][0], srt[k // 2][1])
    a, b = srt[k // 2 - 1], srt[k // 2]
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def rack_ends(color, n=4, x_hint=None):
    """★랙 관측자세에서 벽의 색점 → 양 끝점 중앙. 같은 색 벽이 여럿(red_s/red)이면 x_hint 로 고른다.
    x_hint 없으면 점이 가장 많은(가장 뚜렷한) 벽 클러스터. 고립 유령점은 클러스터에서 배제."""
    import cv2, numpy as np, math as _m
    acc = []
    for _ in range(n):
        b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        m = wall_mask(img, color)
        nn, lab, stt, cen = cv2.connectedComponentsWithStats(m); pts = []
        for i in range(1, nn):
            a = int(stt[i, 4]); x, y = cen[i]
            if 40 < a < 3000 and 20 < x < 1260 and 20 < y < 700:
                pts.append((float(x), float(y)))
        # ★9/7: 빨강 긴 벽(x≈852)과 red_s(x≈763)는 같은 색이고 90px 밖에 안 떨어져 있어,
        #   x-간격 60px 체이닝으로 한 클러스터가 되곤 한다(실측: red_s 가 x808 에서 3점·길이 −10%).
        #   벽 하나의 점들은 x 로 6~10px 안에 모이므로, x_hint 가 있으면 ±X_WIN 밖은 먼저 버린다.
        if x_hint is not None:
            pts = [q for q in pts if abs(q[0] - x_hint) <= X_WIN_PX]
        walls = _cluster_walls(pts)
        if not walls:
            continue
        if x_hint is not None:
            walls.sort(key=lambda g: abs(sum(p[0] for p in g) / len(g) - x_hint))
        else:
            walls.sort(key=lambda g: -len(g))          # 점 가장 많은 벽
        g = walls[0]
        e = max(((_m.dist(g[i], g[j]), i, j) for i in range(len(g)) for j in range(i + 1, len(g))))
        want = WALL_DOT_N.get(color)
        p1_, p2_ = g[e[1]], g[e[2]]
        if want and len(g) == want:
            gp = _grip_point(g, p1_, p2_); mode = "mid_dot"
        else:
            gp = ((p1_[0] + p2_[0]) / 2.0, (p1_[1] + p2_[1]) / 2.0); mode = "ends_mid"
        acc.append((p1_, p2_, len(g), gp, mode))
        time.sleep(0.1)
    if len(acc) < max(1, (n + 1) // 2):
        return None
    import statistics as st_
    # ★9/7 실측(red_s): 4프레임 중 1프레임에서 끝점 하나가 사라져 span 362→318px 이 되는데,
    #   그대로 평균 내면 길이 −3%(게이트 통과) + 중앙 1.8mm 이동이라는 '조용한 오차'가 된다.
    #   → 프레임 길이의 중앙값에서 4% 넘게 벗어난 프레임은 끝점 소실로 보고 버린다.
    if len(acc) >= 3:
        Ls = [math.hypot(a[1][0] - a[0][0], a[1][1] - a[0][1]) for a in acc]
        med = st_.median(Ls)
        keep = [a for a, L in zip(acc, Ls) if med <= 0 or abs(L - med) <= 0.04 * med]
        if len(keep) >= max(1, (n + 1) // 2):
            acc = keep
    mids = [((a[0][0] + a[1][0]) / 2, (a[0][1] + a[1][1]) / 2) for a in acc]
    L = [math.hypot(a[1][0] - a[0][0], a[1][1] - a[0][1]) for a in acc]
    ang = [math.degrees(math.atan2(a[1][0] - a[0][0], a[1][1] - a[0][1])) for a in acc]
    p1 = (st_.mean(a[0][0] for a in acc), st_.mean(a[0][1] for a in acc))
    p2 = (st_.mean(a[1][0] for a in acc), st_.mean(a[1][1] for a in acc))
    grip = (st_.median([a[3][0] for a in acc]), st_.median([a[3][1] for a in acc]))
    modes = [a[4] for a in acc]
    mode = "mid_dot" if modes.count("mid_dot") > len(modes) / 2 else "ends_mid"
    if mode == "ends_mid":                                # 가운데 점을 못 써서 물러난 경우만 알린다
        grip = (st_.mean(m[0] for m in mids), st_.mean(m[1] for m in mids))
    return {"mid": grip, "grip_mode": mode,               # ★파지 기준 = 가운데 점(없으면 끝점 중점)
            "ends_mid": (st_.mean(m[0] for m in mids), st_.mean(m[1] for m in mids)),
            "len_px": st_.mean(L), "ang": st_.mean(ang), "n_dots": acc[-1][2], "p1": p1, "p2": p2}


def save_rack_obs():
    json.dump({"tcp": st()["tcp"], "made": time.strftime("%Y-%m-%d %H:%M")}, open(RACK_OBS, "w"), indent=1)
    print("랙 관측자세 저장:", [round(v, 1) for v in st()["tcp"][:3]])


def rack_probe():
    """★랙 호버에서 화면→로봇 매핑 1회 실측(관측자세와 같은 ±10mm 방법). 벽 점을 특징으로 쓴다."""
    import numpy as np, json as _j
    cur = st()["tcp"]; color = "blue"
    base = rack_ends(color)
    if not base:
        raise RuntimeError("랙 벽 끝점 검출 실패 — 파란 벽이 랙 관측자세에서 보여야 함")
    res = {}
    speed(10)
    for name, d in (("+X", (10, 0)), ("-X", (-10, 0)), ("+Y", (0, 10)), ("-Y", (0, -10))):
        move([cur[0] + d[0], cur[1] + d[1], cur[2]] + list(cur[3:]), tag=f"probe {name}")
        res[name] = rack_ends(color)["mid"]
        if not res[name]:
            raise RuntimeError(f"probe {name} 끝점 실패")
    move(cur, tag="복귀")
    dXpx = [(res["+X"][k] - res["-X"][k]) / 20.0 for k in range(2)]
    dYpx = [(res["+Y"][k] - res["-Y"][k]) / 20.0 for k in range(2)]
    J = np.array([[dXpx[0], dYpx[0]], [dXpx[1], dYpx[1]]]); Jinv = np.linalg.inv(J)
    _j.dump({"made": time.strftime("%Y-%m-%d %H:%M"), "tcp": cur, "J_px_per_mm": J.tolist(), "Jinv_mm_per_px": Jinv.tolist(),
             "scale_mm_per_px": float(1 / np.sqrt(abs(np.linalg.det(J))))}, open(RACK_MAP, "w"), indent=1)
    print(f"  랙 매핑 J={np.round(J, 3).tolist()} 축척 {1/np.sqrt(abs(np.linalg.det(J))):.4f}mm/px 저장")


def rack_ref(color):
    """사용자 승인 파지 자세 위 호버(z+125)에서 벽 점 2개를 '정렬 기준'으로 저장."""
    pts = rack_dots(color)
    if not pts:
        print("❌ 벽 점 2개 검출 실패"); return
    ref = json.load(open(RACK_REF)) if os.path.exists(RACK_REF) else {}
    mid = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
    ang = math.degrees(math.atan2(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
    ref[color] = {"made": time.strftime("%Y-%m-%d %H:%M"), "tcp": st()["tcp"], "pts": pts, "mid": mid, "ang": ang}
    json.dump(ref, open(RACK_REF, "w"), ensure_ascii=False, indent=1)
    print(f"✅ 랙 정렬 기준 저장 [{color}] 중점 ({mid[0]:.1f},{mid[1]:.1f}) 각 {ang:+.2f}°")


GRIP_AXIS_PX = "/home/ar/bf2_console/grip_axis_px.json"    # 색별 그리퍼 축 픽셀(랙 뷰), 중앙 파지 기준


RACK_CALIB = "/home/ar/bf2_console/rack_calib.json"     # 색별 (관측중앙픽셀 Pc0, 검증파지XY Tg0) 한 쌍


def rack_grip_calib(color):
    """★핵심 캘리브: 지금 관측 중앙자세에서 벽 중앙픽셀 Pc0 을 재고, 검증 파지 XY(Tg0)와 짝지어 저장.
    이후 파지: 관측 → 중앙픽셀 Pc → Tg = Tg0 − Jinv·(Pc − Pc0). 벽이 랙에서 비틀려도 추종."""
    import numpy as np
    e = rack_ends(color)
    if not e:
        print("  벽 양끝 검출 실패"); return
    pick, _h, _o, _c = pick_pose(color)
    mp = json.load(open(RACK_MAP)); obs = json.load(open(RACK_OBS))["tcp"]
    d = json.load(open(RACK_CALIB)) if os.path.exists(RACK_CALIB) else {}
    d[color] = {"Pc0": list(e["mid"]), "Tg0": [pick[0], pick[1]], "ang0": e["ang"], "len0_px": e["len_px"],
                "obs_tcp": obs, "Jinv": mp["Jinv_mm_per_px"], "made": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(d, open(RACK_CALIB, "w"), ensure_ascii=False, indent=1)
    print(f"✅ [{color}] 랙 캘리브: 관측중앙 ({e['mid'][0]:.1f},{e['mid'][1]:.1f}) ↔ 파지XY ({pick[0]:.1f},{pick[1]:.1f}) 각 {e['ang']:+.2f}°")


def rack_grip_xy(color, dxy=(0.0, 0.0)):
    """관측 중앙자세에서 현재 벽 중앙 → 파지 XY(캘리브 기반). dxy = 카메라가 관측자세에서 옮겨간 로봇 XY(탐색 후).
    유도: Pc' = Pc + J·Δ → Tg = Tg0 − Jinv(Pc'−Pc0) + Δ. x_hint 도 J·Δ 만큼 옮겨서 같은 벽을 고른다. 반환 (Tgx, Tgy, dang) 또는 None."""
    import numpy as np
    if not os.path.exists(RACK_CALIB):
        return None
    cal = json.load(open(RACK_CALIB)).get(color)
    if not cal:
        return None
    J = np.linalg.inv(np.array(cal["Jinv"]))
    hint_shift = J @ np.array(dxy, float)
    e = rack_ends(color, x_hint=cal["Pc0"][0] + float(hint_shift[0]))   # ★같은 색 여러 벽이면 캘리브 x 로 그 벽 선택
    if not e:
        return None
    # ★길이 게이트(9/5 실증): 벽 끝점을 하나 놓치면 길이가 짧게 나오고 '중앙'이 치우쳐 엉뚱한 곳을 문다
    #   (476px vs 캘리브 567px → 중앙 이탈 파지). 캘리브 길이와 ±10% 넘게 다르면 파지 금지.
    L0 = cal.get("len0_px")
    if L0 and abs(e["len_px"] - L0) > 0.10 * L0:
        print(f"  ❌ 벽 길이 불일치: 지금 {e['len_px']:.0f}px vs 캘리브 {L0:.0f}px "
              f"({(e['len_px']-L0)/L0*100:+.0f}%) — 끝점 미검출 의심, 파지 중단")
        return None
    Jinv = np.array(cal["Jinv"]); dpx = np.array([e["mid"][0] - cal["Pc0"][0], e["mid"][1] - cal["Pc0"][1]])
    dmm = Jinv @ dpx                                  # 벽이 화면에서 dpx 옮겨짐 = 세계에서 −dmm → 파지 XY 도 그만큼
    Tg = (cal["Tg0"][0] - dmm[0] + dxy[0], cal["Tg0"][1] - dmm[1] + dxy[1])
    return Tg[0], Tg[1], HG.wrap_deg(e["ang"] - cal["ang0"])


def _rack_color_center(color, x_hint, radius=220.0):
    """탐색용: 그 색 점 중 x_hint 근처(±radius) 것들의 중심 px. (양끝이 안 보여도 보이는 점으로 방향을 잡는다)"""
    import cv2
    b = UR.urlopen("http://127.0.0.1:8766/raw", timeout=5).read()
    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
    m = wall_mask(img, color)
    n, lab, stt, cen = cv2.connectedComponentsWithStats(m)
    pts = [(float(cen[i][0]), float(cen[i][1])) for i in range(1, n) if 40 < stt[i, 4] < 3000 and abs(cen[i][0] - x_hint) <= radius]
    if not pts:
        return None, 0
    return (float(np.mean([q[0] for q in pts])), float(np.mean([q[1] for q in pts]))), len(pts)


def rack_find(color, max_round=3):
    """★랙 양끝이 안 보이면 카메라를 옮겨 다시 본다(사용자 지시). 라운드마다 rack_grip_xy 시도 → 실패 시
    보이는 점들의 중심을 화면 중심으로(같은 높이 XY 이동, ≤50mm) → 재시도. 점이 하나도 없으면 z+60 상공에서 찾아 이동.
    반환 (Tgx, Tgy, dang, e) 또는 None. Tg 에는 카메라 오프셋이 이미 반영돼 있다."""
    import numpy as np
    obs = json.load(open(RACK_OBS))["tcp"]
    cal = json.load(open(RACK_CALIB)).get(color) if os.path.exists(RACK_CALIB) else None
    if not cal:
        print(f"  ⚠ {color} 랙 캘리브 없음"); return None
    Jinv = np.array(cal["Jinv"]); J = np.linalg.inv(Jinv)
    for r in range(max_round):
        cur = st()["tcp"]; dxy = (cur[0] - obs[0], cur[1] - obs[1])
        tg = rack_grip_xy(color, dxy)
        if tg is not None:
            e = rack_ends(color, x_hint=cal["Pc0"][0] + float((J @ np.array(dxy))[0]))
            if abs(dxy[0]) + abs(dxy[1]) > 0.5:
                print(f"  (랙 탐색 카메라 오프셋 Δ ({dxy[0]:+.1f},{dxy[1]:+.1f})mm 반영)")
            return tg[0], tg[1], tg[2], e
        x_hint = cal["Pc0"][0] + float((J @ np.array(dxy))[0])
        c, n = _rack_color_center(color, x_hint)
        if c is None:
            speed(SPD_MOVE); move([cur[0], cur[1], cur[2] + 60] + list(cur[3:]), tag="랙 탐색 상승 +60"); time.sleep(0.5)
            c, n = _rack_color_center(color, x_hint, radius=400)
            if c is None:
                print(f"  랙 상공에서도 {color} 점 없음 [{r+1}/{max_round}]"); move(cur, tag="랙 관측 높이 복귀"); continue
            d = Jinv @ np.array([640.0 - c[0], 360.0 - c[1]]) * ((cur[2] + 60 - 340) / (cur[2] - 340))   # 대략 축척(랙 벽 윗면 z≈340)
        else:
            d = Jinv @ np.array([640.0 - c[0], 360.0 - c[1]])
        nrm = float(np.hypot(*d))
        if nrm > 50.0:
            d = d * (50.0 / nrm)
        print(f"  랙 {color} 점 {n}개(중심 px {c[0]:.0f},{c[1]:.0f}) → 양끝 못 봄 → 카메라 ({d[0]:+.1f},{d[1]:+.1f}) 이동 재관측 [{r+1}/{max_round}]")
        speed(SPD_MOVE); move([cur[0] + float(d[0]), cur[1] + float(d[1]), obs[2]] + list(obs[3:]), tag="랙 탐색 XY"); time.sleep(0.5)
    return None


def rack_center_align(color, tol_px=1.5, max_iter=4):
    """★사용자 설계: 랙 위에서 벽 양 끝점(두 색점) → 중앙 픽셀을 그리퍼 축 픽셀에 맞춘다 → 벽 기하 중앙을 문다.
    그리퍼 축 픽셀 GRIP_AXIS_PX[color] 는 '중앙을 물었을 때 벽-중앙 픽셀이 있던 자리'로 1회 캘리브(성공 삽입에서).
    없으면 rack_ref 로 폴백."""
    import numpy as np
    if not os.path.exists(RACK_MAP):
        print("  랙 매핑 없음 → 골든 자세"); return None
    Jinv = np.array(json.load(open(RACK_MAP))["Jinv_mm_per_px"])
    ga = json.load(open(GRIP_AXIS_PX)).get(color) if os.path.exists(GRIP_AXIS_PX) else None
    if ga is None:
        print(f"  {color} 그리퍼 축 픽셀 미캘리브 → rack_align(중점 기준) 폴백"); return rack_align(color, tol_px, max_iter)
    speed(3)
    for it in range(max_iter):
        e = rack_ends(color)
        if not e:
            print("  벽 양끝 검출 실패 → 중단"); return None
        mid = e["mid"]
        dpx = (ga[0] - mid[0], ga[1] - mid[1]); err = math.hypot(*dpx)
        print(f"  랙 중앙정렬 {it}: 벽중앙 ({mid[0]:.0f},{mid[1]:.0f}) → 그리퍼축 ({ga[0]:.0f},{ga[1]:.0f})  오차 {err:.1f}px")
        if err <= tol_px:
            return mid
        dmm = np.clip(Jinv @ np.array(dpx), -6, 6); c = st()["tcp"]
        move([c[0] + dmm[0], c[1] + dmm[1], c[2]] + list(c[3:]), tol=0.5, tag=f"    중앙보정 ({dmm[0]:+.2f},{dmm[1]:+.2f})")
    return mid


def calib_grip_axis(color):
    """★현재 파지(사용자가 중앙을 문 상태) 위 랙 호버에서 벽 양끝 중앙 픽셀을 그리퍼 축으로 저장."""
    pts = rack_dots(color)
    if not pts:
        print("  벽 끝점 검출 실패"); return
    mid = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
    d = json.load(open(GRIP_AXIS_PX)) if os.path.exists(GRIP_AXIS_PX) else {}
    d[color] = {"px": list(mid), "made": time.strftime("%Y-%m-%d %H:%M"), "tcp": st()["tcp"]}
    json.dump(d, open(GRIP_AXIS_PX, "w"), ensure_ascii=False, indent=1)
    print(f"✅ [{color}] 그리퍼 축 픽셀 = 벽중앙 ({mid[0]:.1f},{mid[1]:.1f}) 저장")


def rack_align(color, tol_px=1.5, max_iter=4):
    """★픽 호버에서 벽 점 중점을 기준 픽셀에 맞춘다 → 랙이 비틀려도 벽의 같은 지점을 문다."""
    import numpy as np
    if not (os.path.exists(RACK_REF) and os.path.exists(RACK_MAP)):
        print("  랙 정렬 기준/매핑 없음 → 골든 자세 그대로 파지"); return None
    ref = json.load(open(RACK_REF)).get(color)
    if not ref:
        print(f"  {color} 랙 정렬 기준 없음 → 골든 자세 그대로"); return None
    Jinv = np.array(json.load(open(RACK_MAP))["Jinv_mm_per_px"])
    speed(3)
    for it in range(max_iter):
        pts = rack_dots(color)
        if not pts:
            print("  벽 점 검출 실패 → 정렬 중단"); return None
        mid = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
        dpx = (ref["mid"][0] - mid[0], ref["mid"][1] - mid[1])
        err = math.hypot(*dpx)
        ang = math.degrees(math.atan2(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
        print(f"  랙 정렬 {it}: 중점 오차 {err:.1f}px  각 차 {HG.wrap_deg(ang - ref['ang']):+.2f}°")
        if err <= tol_px:
            return mid
        # 점이 기준 쪽으로 dpx 만큼 가야 함 = 로봇을 Jinv·dpx 만큼(부호는 관측 매핑과 동일: 로봇 ΔX → 점 +J·ΔX)
        dmm = Jinv @ np.array(dpx)
        dmm = np.clip(dmm, -6, 6)
        c = st()["tcp"]
        move([c[0] + dmm[0], c[1] + dmm[1], c[2]] + list(c[3:]), tol=0.5, tag=f"    보정 ({dmm[0]:+.2f},{dmm[1]:+.2f})")
    return mid


def health_gate(max_try=4):
    """★검출 건강 게이트 + 적응 노출: 관측자세에서 4프레임 중 3프레임 ≥3점·rms<2mm 이어야 통과.
    실패하면 플리커 안전값 사이에서 기둥이 가장 많이 잡히는 노출로 바꾼다(바닥 미만 저장 금지)."""
    import slot_target as STG, color_lock as CL
    def score():
        """★4점 전부 검출된 프레임만 유효(3점 폴백은 go/no-go 에서 불인정 — 사용자 지시)."""
        ok = 0; rms_l = []
        for _ in range(4):
            px, why = STG.pillars_px(n=1)
            if px and len(px) == 4:
                Jinv, _m = STG.load_map(); a = json.load(open(STG.ANCH))
                pose, rms, _ = STG.base_pose_robot(px, Jinv, tuple(a["C"]), tuple(a["p0"]))
                if rms < BASE_RMS_MAX:
                    ok += 1; rms_l.append(rms)
        return ok, (sum(rms_l) / len(rms_l) if rms_l else 99)
    # ★9/9 사용자 제안: 직전에 4/4 로 통과한 노출·게인을 기억했다가 먼저 적용한다.
    #   BASE 사이에 랙 관측(417)·서명(333~500)·하강 감시 사다리(83~800)가 노출을 바꿔놓아서,
    #   다음 벽의 BASE 가 매번 첫 판정에 실패하고 8단계 사다리로 떨어졌다(오늘 19회 중 7회, 70~89s).
    #   실패해도 종전 사다리로 가므로 손해가 없다(최악 +1.5s, 최선 −60s).
    if os.path.exists(BASE_EXPO_F):
        try:
            _m = json.load(open(BASE_EXPO_F)); _cur = CL.current_settings().get("exposure")
            if _m.get("exposure") and float(_m["exposure"]) != float(_cur or 0):
                CL.expo(**{k: v for k, v in (("set", _m.get("exposure")), ("gain", _m.get("gain"))) if v})
                time.sleep(1.5)
                print(f"  기억한 베이스 노출 적용: {_m.get('exposure')} (gain {_m.get('gain')}, {_m.get('made')})")
        except Exception as _e:
            print(f"  (기억 노출 적용 실패 — 무시하고 진행: {_e})")
    ok, rms = score()
    if ok >= 3:
        print(f"  건강 게이트 통과 ({ok}/4, rms {rms:.2f})")
        _remember_base_expo()
        return True
    cur = CL.current_settings().get("exposure")
    print(f"  건강 게이트 미달 ({ok}/4) → 노출 탐색 (현재 {cur})")
    best = (ok, cur)
    for ex in FLICKER_LADDER:
        if ex == cur:
            continue
        CL.expo(set=ex); time.sleep(1.5); o, r = score(); print(f"    노출 {ex}: {o}/4 rms {r:.2f}")
        if o > best[0]:
            best = (o, ex)
    cur_gain = (json.load(open(CL.STORE)).get("apply") or {}).get("gain") if os.path.exists(CL.STORE) else None
    best_gain = cur_gain
    if best[0] < 3:
        # ★gain 2차 패스(9/6): 직사광 낮엔 gain 16, 밤엔 64 가 필요 — 노출만으로 안 되면 gain 을 바꿔 사다리 재시도
        for g in GAIN_LADDER:
            if g == cur_gain:
                continue
            for ex in FLICKER_LADDER:
                CL.expo(set=ex, gain=g); time.sleep(1.5); o, r = score(); print(f"    gain {g} 노출 {ex}: {o}/4 rms {r:.2f}")
                if o > best[0]:
                    best = (o, ex); best_gain = g
    CL.expo(set=best[1], **({"gain": best_gain} if best_gain else {})); time.sleep(1.2)
    if best[0] >= 3:
        stf = json.load(open(CL.STORE)) if os.path.exists(CL.STORE) else {}
        ap = {"set": best[1]}
        if best_gain: ap["gain"] = best_gain
        stf.update(apply=ap, made=time.strftime("%Y-%m-%d %H:%M"), note="health_gate 적응 노출/게인")
        json.dump(stf, open(CL.STORE, "w"), ensure_ascii=False, indent=1)
        print(f"  노출 {best[1]} gain {best_gain} 채택·저장 ({best[0]}/4)"); return True
    print("  ❌ 어떤 노출/게인에서도 기둥 검출 부족 — 정지(직사광이면 블라인드)"); return False


def release_and_retreat(color):
    """안착된 벽을 놓고(그리퍼 열기) 관측자세로 복귀. 부품은 그 자리에 둔다."""
    refs = json.load(open(CAL))["refs"][color]
    print("  그리퍼 열기 →", gripper(refs["grip_open"]))
    speed(SPD_SEAT); cur = st()["tcp"]
    move([cur[0], cur[1], cur[2] + 30] + list(cur[3:]), tag="벽 위로 +30")
    speed(SPD_DESC); move([cur[0], cur[1], HOVER_Z] + list(cur[3:]), tag="호버 z478")
    speed(SPD_MOVE); move([cur[0], cur[1], SAFE_Z] + list(cur[3:]), tag="SAFE")
    move(OBS, tag="관측자세"); speed(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    color = sys.argv[2] if len(sys.argv) > 2 else "yellow"
    if mode == "plan":
        P = plan(color)
        print(f"베이스(로봇) x {P['base'].x:.2f} y {P['base'].y:.2f} yaw {P['base'].yaw_deg:+.3f}° rms {P['rms']:.2f}mm")
        print(f"목표 [{color}] x {P['x']:.2f} y {P['y']:.2f} rz {P['rz']:+.2f}  (기준점 {P['anchor']} {P['anchor_made']})")
    elif mode == "run":
        tgt = None
        if "--target" in sys.argv:
            i = sys.argv.index("--target"); tgt = (float(sys.argv[i + 1]), float(sys.argv[i + 2]), float(sys.argv[i + 3]))
        run(color, seat="--seat" in sys.argv, do_pick="--no-pick" not in sys.argv, target=tgt,
            align="--no-align" not in sys.argv, len_gate="--len-gate" in sys.argv, grasp_gate="--no-grasp-gate" not in sys.argv,
            grasp_teach="--grasp-teach" in sys.argv, align_stop="--align-stop" in sys.argv)
    elif mode == "held_depth":          # 벽 든 채(어느 높이든): 뎁스·축척·축 각
        m, why = held_wall_depth(color)
        print("❌ " + why if m is None else f"[{color}] 든 벽 뎁스 {m['d_w']:.0f}mm · {m['mm_px']:.4f}mm/px · 축 {m['ang_img']:+.2f}° · 폭 {m['span_px']:.0f}px · 중심 ({m['center_px'][0]:.0f},{m['center_px'][1]:.0f}) · 점 {m['n']}")
    elif mode == "rack_len":            # 랙 관측자세에서: 색점 양끝 + 뎁스 물리 길이 검산
        cal = json.load(open(RACK_CALIB)).get(color) if os.path.exists(RACK_CALIB) else None
        e = rack_ends(color, x_hint=cal["Pc0"][0] if cal else None)
        if not e: print("❌ 색점 양끝 검출 실패")
        else:
            print(f"[{color}] 색점: 중앙 ({e['mid'][0]:.0f},{e['mid'][1]:.0f}) 길이 {e['len_px']:.0f}px 각 {e['ang']:+.2f}°")
            rack_len_check(color, e, strict=False)
    elif mode == "release":
        release_and_retreat(color)
    elif mode == "nudge":
        # 현재 높이에서 로봇 X/Y(mm)·rz(°) 소폭 이동 — 기둥 위 높이에서만 쓸 것(채널 안 XY 이동 금지)
        dx = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        dy = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        drz = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
        c = st()["tcp"]
        if c[2] < 430.0:
            print(f"❌ z {c[2]:.1f} < 430 (기둥 꼭대기 아래) — XY 이동 금지"); sys.exit(1)
        if abs(dx) > 6 or abs(dy) > 6 or abs(drz) > 3:
            print("❌ 한 번에 6mm/3° 이하로"); sys.exit(1)
        speed(1); move([c[0] + dx, c[1] + dy, c[2], c[3], c[4], HG.wrap_deg(c[5] + drz)], tol=0.5, tag=f"nudge {dx:+.1f},{dy:+.1f},{drz:+.1f}")
        print("그리퍼", grip_read())
    elif mode == "rack_probe":
        rack_probe()
    elif mode == "rack_ref":
        rack_ref(color)
    elif mode == "calib_grip_axis":
        calib_grip_axis(color)
    elif mode == "rack_calib":
        rack_grip_calib(color)
    elif mode == "rack_gripxy":
        r=rack_grip_xy(color); print("파지XY", tuple(round(v,2) for v in r[:2]),"각차",round(r[2],2)) if r else print("캘리브/검출 없음")
    elif mode == "jamtest":
        jamtest(color, int(sys.argv[3]) if len(sys.argv) > 3 else 40)
    elif mode == "grasp":
        capture_grasp_sig(color, int(sys.argv[3]) if len(sys.argv) > 3 else 15)
    else:
        print(__doc__)
