#!/usr/bin/env python3
"""슬롯 목표 TCP 계산 — "베이스가 움직여도 벽을 꽂는다" 의 2단계 (9/5).

입력 3가지를 잇는다:
  ① 라이브 베이스 자세: 관측자세에서 기둥 색점 4개(px) → 밑판 좌표계
  ② 화면→로봇 매핑: `cam2robot_observe.json` (±10mm 소이동 실측, J px/mm)
  ③ 평행이동 기준점(anchor): "이 벽을 이 슬롯에 손으로 안착시켰을 때 TCP 가 여기였고,
     그때 기둥 점은 화면에서 여기였다" 1건 → 카메라 광축의 로봇 좌표 상수 확정

원리:
  로봇을 ΔX 움직이면 화면 특징이 Δpx = J·ΔX 만큼 움직인다(실측, 회전 0.1°·직교 0.0°).
  따라서 어떤 화면점 p 의 로봇 XY = C + Jinv·(p − p0)  (C, p0 = 기준점에서 확정되는 상수쌍)
  기둥 4점을 로봇 XY 로 옮겨 밑판 자세 T_RB(x, y, yaw) 를 맞추고,
  house_geometry.target_tcp(T_RB, slot, grip, rz_now) 로 목표 (x, y, rz)·ΔJ6 를 얻는다.

벽 ↔ 슬롯 (골든 로봇좌표·매핑 부호로 확인 9/5):
  blue → LONG_Y0 (노랑·빨강 기둥 쪽, 로봇 x≈170)   red → LONG_Y140 (파랑·파랑 쪽, x≈297)
  yellow → SHORT_X0 (빨강–파랑r 짧은 변, y≈−309)   red_s → SHORT_X210 (노랑–파랑y, y≈−512)

  python3 slot_target.py check                 # 라이브 베이스 자세(로봇 정렬 mm) 출력
  python3 slot_target.py anchor <색>           # 지금 TCP(손 안착 상태)+기둥 px 를 기준점으로 저장
  python3 slot_target.py anchor-golden <색>    # 임시: 골든 insert_tcp 를 기준점으로(베이스가 9/2 이후 안 움직였다고 가정)
  python3 slot_target.py target <색> [rz_now]  # 라이브 베이스 자세로 그 벽의 목표 TCP 계산(+골든과 비교)
"""
import sys, json, math, os, time
import urllib.request as UR
import numpy as np

sys.path.insert(0, "/home/ar/bf2_console/tools")
import base_depth_corner as B
import pillar_dots as PD
import house_geometry as HG

BR = "http://127.0.0.1:8765"
CAL = "/home/ar/bf2_console/dot_calib.json"
MAP = "/home/ar/bf2_console/cam2robot_observe.json"
ANCH = "/home/ar/bf2_console/slot_anchor.json"
# ★로봇 프레임(y 위로 +)은 화면(y 아래로 +)과 손방향이 반대(det J < 0). 화면 순서 [노랑, 빨강, 파랑r, 파랑y] 를
#   로봇 mm 로 옮기면 모델 대응이 x 거울로 바뀐다: 노랑(205,5)·빨강(5,5)·파랑r(5,135)·파랑y(205,135).
#   (9/5 실측: 화면 순서 그대로 쓰니 rms 130mm, 노랑·red_s 목표가 서로 자리바꿈)
MODEL_R = ((205.0, 5.0), (5.0, 5.0), (5.0, 135.0), (205.0, 135.0))
WALL_SLOT = {"blue": "LONG_Y0", "red": "LONG_Y140", "yellow": "SHORT_X0", "red_s": "SHORT_X210"}
WALL_LEN = {"blue": 198.0, "red": 198.0, "yellow": 125.0, "red_s": 125.0}
# 모델 꼭짓점 순서 = pillar_dots.CORNER_MODEL = ((5,5) 노랑, (205,5) 빨강, (205,135) 파랑, (5,135) 파랑)
MODEL = PD.CORNER_MODEL


def status():
    return json.loads(UR.urlopen(BR + "/status", timeout=6).read())["robots"]["fr5"]


def load_map():
    m = json.load(open(MAP))
    return np.array(m["Jinv_mm_per_px"], float), m


HELD_WALL_BOX = (900, 330, 1280, 720)      # 관측자세(rz 180)에서 '든 벽'이 보이는 손목캠 영역(x0,y0,x1,y1) — 9/5 실측(빨강 box 990,390~)


def pillars_px(n=6, mask_held=False):
    """mask_held=True: 벽을 든 채 재측정 — 든 벽 영역의 뎁스 점을 빼서 밑판 검출이 벽을 밑판으로 잡지 않게 한다(9/5 실패 원인)."""
    acc = [[] for _ in range(4)]
    for _ in range(n + 4):                      # ★detect_rect 가 빈 마스크로 예외를 낼 수 있어 프레임 단위로 넘긴다
        img, grid = B.grab_pair()
        if mask_held and grid and "pts" in grid:
            x0, y0, x1, y1 = HELD_WALL_BOX
            grid = dict(grid); grid["pts"] = [q for q in grid["pts"] if not (x0 <= q["x"] <= x1 and y0 <= q["y"] <= y1)]
        try:
            m, _w = B.detect_rect(img, grid)
        except Exception:
            time.sleep(0.15); continue
        rect = PD.plate_rect(m)
        if rect is None:
            time.sleep(0.15); continue
        pts, why = PD.four_corners(img, rect)
        if pts and len(pts) >= 3:
            for p in pts:
                acc[int(p[4])].append((p[0], p[1]))
        time.sleep(0.1)
    have = [i for i in range(4) if len(acc[i]) >= 3]
    if len(have) < 3:
        return None, f"기둥 검출 부족(꼭짓점별 {[len(a) for a in acc]})"
    return [(*np.mean(np.array(acc[i]), axis=0), i) for i in have], None


def px_to_robot(px, Jinv, C, p0):
    """화면점 → 로봇 XY (mm). C = p0 에 대응하는 로봇 XY.
    ★부호(9/5 실측): 로봇이 ΔX 움직이면 화면점은 p + J·ΔX 로 간다. 점 P 를 화면 중심 c 로
    가져오려면 ΔX = Jinv·(c − p) 만큼 움직여야 하므로 P 의 로봇 위치 = X0 + Jinv·(c − p).
    Jinv·(p − c) 로 쓰면 두 축이 다 뒤집혀(180° 회전) 긴벽·짧은벽 목표가 반대편에 찍힌다."""
    d = Jinv @ np.array([p0[0] - px[0], p0[1] - px[1]], float)
    return (C[0] + d[0], C[1] + d[1])


def base_pose_robot(px4, Jinv, C, p0):
    """px4: [(x,y)] 4개(모델 순서) 또는 [(x,y,idx)] 3~4개(idx=모델 꼭짓점). 3점이면 해당 모델 꼭짓점만으로 맞춘다."""
    if px4 and len(px4[0]) >= 3:
        idx = [int(p[2]) for p in px4]; pts = [(p[0], p[1]) for p in px4]
        model = tuple(MODEL_R[i] for i in idx)
    else:
        pts = list(px4); model = MODEL_R
    meas = [px_to_robot(p, Jinv, C, p0) for p in pts]
    pose, rms = HG.fit_base_pose(meas, model)
    return pose, rms, meas


def base_pose_local(px4, Jinv):
    """기준점 없이도 되는 것: 로봇 축에 정렬된 mm 좌표(원점=화면 중심). 자세의 yaw·형상 검증용."""
    return base_pose_robot(px4, Jinv, (0.0, 0.0), (640.0, 360.0))


def grip_nominal(color):
    """파지 공칭: 벽 중앙을 물었다고 가정(그리퍼 중심 = 벽 아래변 중점, 각 0). 실측 보정은 grip_measure."""
    # φ_grip=90°: 벽 판이 TCP x축과 직각으로 물림(골든 rz: 긴벽 180 = θ_B(−90)+0−90, 짧은벽 +90 = −90+90−90+180 대칭)
    return HG.GripMeasure(center=(0.0, 0.0), angle_deg=90.0, bottom_dz=0.0)


def rz_line_sym(rz, ref):
    """벽 아래변 '선'은 180° 대칭 → rz 를 ref 에 가장 가까운 동치각으로."""
    best = rz
    for k in (-2, -1, 0, 1, 2):
        cand = rz + 180.0 * k
        if abs(HG.wrap_deg(cand - ref)) < abs(HG.wrap_deg(best - ref)):
            best = cand
    return HG.wrap_deg(best)


def solve_anchor(color, tcp_seat, px4, Jinv, grip=None):
    """손 안착 TCP 와 그때의 기둥 px 로 상수 (C, p0) 확정.
    안착 상태에서 target_tcp 가 tcp_seat 를 돌려주도록 C 를 푼다(선형이라 1회 보정으로 정확)."""
    p0 = (640.0, 360.0)
    C = np.array([0.0, 0.0])
    slot = HG.SLOTS[WALL_SLOT[color]]
    rz_seat = float(tcp_seat[5])
    g = grip or grip_nominal(color)        # ★안착 순간의 실제 파지(벽이 조그 중 물림 안에서 돌 수 있음)
    for _ in range(3):
        pose, rms, _m = base_pose_robot(px4, Jinv, tuple(C), p0)
        tgt, _dj = HG.target_tcp(pose, slot, g, rz_seat)
        C = C + np.array([tcp_seat[0] - tgt.x, tcp_seat[1] - tgt.y])
    pose, rms, _m = base_pose_robot(px4, Jinv, tuple(C), p0)
    tgt, _dj = HG.target_tcp(pose, slot, g, rz_seat)
    resid = math.hypot(tcp_seat[0] - tgt.x, tcp_seat[1] - tgt.y)
    return {"C": C.tolist(), "p0": list(p0), "resid_mm": resid, "fit_rms_mm": rms,
            "yaw_deg": pose.yaw_deg, "rz_seat": rz_seat}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    Jinv, mp = load_map()
    px4, why = (None, None)
    if mode != "anchor-tcp":                    # ★anchor-tcp 는 1단계에 저장한 px 를 쓴다(안착 자세선 기둥이 안 보임)
        px4, why = pillars_px()
        if px4 is None:
            print("❌", why); return
    if mode == "check":
        pose, rms, meas = base_pose_local(px4, Jinv)
        print(f"기둥 px: " + " ".join(f"({p[0]:.1f},{p[1]:.1f})" for p in px4))
        print(f"베이스 자세(로봇축 정렬, 원점=화면중심): x {pose.x:+.2f} y {pose.y:+.2f} yaw {pose.yaw_deg:+.3f}°  rms {rms:.2f}mm")
        print(f"매핑: 축척 {mp['scale_mm_per_px']:.4f}mm/px · 로봇+X→화면 {mp['angX_deg']:+.1f}° · +Y→화면 {mp['angY_deg']:+.1f}° ({mp['made']})")
        if os.path.exists(ANCH):
            a = json.load(open(ANCH))
            pose2, rms2, _ = base_pose_robot(px4, Jinv, tuple(a["C"]), tuple(a["p0"]))
            print(f"기준점({a['color']}, {a['made']}) 적용 시 로봇좌표: x {pose2.x:.1f} y {pose2.y:.1f} yaw {pose2.yaw_deg:+.3f}°")
        return

    color = sys.argv[2] if len(sys.argv) > 2 else "blue"
    if color not in WALL_SLOT:
        print("색:", list(WALL_SLOT)); return
    slot = HG.SLOTS[WALL_SLOT[color]]

    if mode == "anchor-px":
        # ★1단계: 관측자세에서 기둥 px 저장(매핑이 관측자세 기준). 로봇 TCP 는 2단계에서.
        cur = status()["tcp"]; err = max(abs(cur[i] - mp["tcp"][i]) for i in range(3))
        if err > 2.0:
            print(f"❌ 관측자세가 아님(차이 {err:.1f}mm) — 매핑 자세 {mp['tcp'][:3]} 에서 찍어야 한다"); return
        json.dump({"color": color, "px4": [list(p) for p in px4], "made": time.strftime("%Y-%m-%d %H:%M"),
                   "observe_tcp": cur}, open(ANCH + ".px", "w"), indent=1)
        pose, rms, _ = base_pose_local(px4, Jinv)
        print(f"✅ 1단계 저장 [{color}] 기둥 px " + " ".join(f"({p[0]:.1f},{p[1]:.1f})" for p in px4)
              + f"  밑판 rms {rms:.2f}mm yaw {pose.yaw_deg:+.3f}°")
        print("   → 로봇을 안착된 벽을 물고 있는 자세로 옮긴 뒤 `anchor-tcp %s`" % color); return

    if mode == "anchor-tcp":
        st = json.load(open(ANCH + ".px"))
        if st["color"] != color:
            print(f"❌ 1단계는 {st['color']} 였음"); return
        tcp = status()["tcp"]; px_saved = [tuple(p) for p in st["px4"]]
        grip = None; ginfo = None
        try:
            import place_calc as PC
            grip, ginfo = PC.grasp_measure(color)
        except Exception as e:
            ginfo = str(e)
        if grip is not None:
            print(f"   안착 순간 파지 편차 반영: 가로 {ginfo['across_mm']:+.2f}mm 길이 {ginfo['along_mm']:+.2f}mm 각 {ginfo['dang']:+.2f}°")
        else:
            print(f"   파지 편차 측정 불가({ginfo}) → 공칭 파지로 풀이")
        a = solve_anchor(color, tcp, px_saved, Jinv, grip)
        a["grip_at_seat"] = (ginfo if isinstance(ginfo, dict) else None)
        a.update(color=color, slot=slot.name, tcp_seat=list(tcp), px4=st["px4"], made=time.strftime("%Y-%m-%d %H:%M"),
                 source=f"손 안착 후 로봇 파지 TCP(2단계) · px 는 {st['made']} 관측자세", observe_tcp=mp["tcp"])
        json.dump(a, open(ANCH, "w"), ensure_ascii=False, indent=1)
        print(f"✅ 기준점 저장 [{color} → {slot.name}]  안착 TCP {[round(v,1) for v in tcp[:3]]} rz {tcp[5]:+.1f}")
        print(f"   C={np.round(a['C'],2).tolist()}  잔차 {a['resid_mm']:.3f}mm  밑판 rms {a['fit_rms_mm']:.2f}mm  yaw {a['yaw_deg']:+.3f}°"); return

    if mode in ("anchor", "anchor-golden"):
        if mode == "anchor":
            tcp = status()["tcp"]; src = "손 안착 TCP(현재)"
        else:
            cal = json.load(open(CAL))
            tcp = cal["refs"][color]["insert_tcp"]; src = f"골든 insert_tcp({cal['refs'][color].get('insert_note','')[:40]})"
        a = solve_anchor(color, tcp, px4, Jinv)
        a.update(color=color, slot=slot.name, tcp_seat=list(tcp), px4=[list(p) for p in px4],
                 made=time.strftime("%Y-%m-%d %H:%M"), source=src, observe_tcp=mp["tcp"])
        json.dump(a, open(ANCH, "w"), ensure_ascii=False, indent=1)
        print(f"✅ 기준점 저장 [{color} → {slot.name}]  출처: {src}")
        print(f"   C={np.round(a['C'],2).tolist()}  잔차 {a['resid_mm']:.3f}mm  밑판 맞춤 rms {a['fit_rms_mm']:.2f}mm  yaw {a['yaw_deg']:+.3f}°")
        return

    if mode == "target":
        if not os.path.exists(ANCH):
            print("❌ 기준점 없음 — `anchor <색>` 또는 `anchor-golden <색>`"); return
        a = json.load(open(ANCH))
        rz_now = float(sys.argv[3]) if len(sys.argv) > 3 else float(status()["tcp"][5])
        pose, rms, _ = base_pose_robot(px4, Jinv, tuple(a["C"]), tuple(a["p0"]))
        tgt, dj6 = HG.target_tcp(pose, slot, grip_nominal(color), rz_now)
        rz_t = rz_line_sym(tgt.yaw_deg, rz_now); dj6 = HG.wrap_deg(rz_t - rz_now)
        tgt = HG.Pose2D(tgt.x, tgt.y, rz_t)
        print(f"베이스(로봇): x {pose.x:.2f} y {pose.y:.2f} yaw {pose.yaw_deg:+.3f}°  rms {rms:.2f}mm  (기준점 {a['color']}, {a['made']})")
        print(f"목표 [{color} → {slot.name}]: x {tgt.x:.2f}  y {tgt.y:.2f}  rz {tgt.yaw_deg:+.2f}°   ΔJ6 {dj6:+.2f}° (rz_now {rz_now:+.1f})")
        try:
            g = json.load(open(CAL))["refs"][color]["insert_tcp"]
            d = math.hypot(tgt.x - g[0], tgt.y - g[1]); drz = HG.wrap_deg(tgt.yaw_deg - g[5])
            print(f"골든 insert_tcp {[round(v,1) for v in g[:2]]} rz {g[5]:+.0f} 대비: Δ {d:.2f}mm · Δrz {drz:+.2f}°"
                  + ("  ← 기준점 자신(0 이어야 정상)" if a["color"] == color else "  ← 독립 검증(9/2 이후 베이스 이동+파지차 포함)"))
        except Exception:
            pass
        return
    print(__doc__)


if __name__ == "__main__":
    main()
