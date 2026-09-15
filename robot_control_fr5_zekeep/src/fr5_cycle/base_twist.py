#!/usr/bin/env python3
"""베이스 틀어짐 측정 — ArUco(고정 흰 판) 기준 (9/5, 사용자 아이디어).

왜 필요한가:
  ZK 가 밑판을 놓으므로 **베이스는 언제든 비틀리거나 움직인다**. 반면 ArUco 가 붙은 흰 판은 고정이다.
  그런데 지금까지 베이스 자세를 '화면 좌표'로 재 왔다 — 그러면 로봇이 관측자세로 돌아올 때 생긴
  작은 오차가 **베이스가 움직인 것과 구분되지 않는다**(둘 다 화면에서 밑판이 밀린 것으로 보인다).

원리(사용자 제안 그대로):
  고정 마커를 자로 삼아, 그 자 위에서 기둥 색점이 기준 때보다 얼마나 벗어났는지를 본다.
    ① 기준 시점: 마커 코너들 + 기둥 4점을 저장
    ② 측정 시점: 현재 마커 코너를 기준 시점 마커 코너에 겹치는 변환 T 를 구한다
       (공통 id 의 코너 전부로 2D 강체+축척 맞춤 — 카메라가 조금 움직여도 흡수)
    ③ 그 T 로 현재 기둥 점을 기준 좌표계로 옮긴 뒤, 기준 기둥 점과 비교
  → 남는 차이가 **순수한 베이스 틀어짐**(로봇 복귀 오차는 ②에서 상쇄된다).

  python3 base_twist.py ref            # 지금 상태를 기준으로 저장
  python3 base_twist.py run [n]        # 기준 대비 베이스 틀어짐 측정(n회 평균)
  python3 base_twist.py check          # 마커·기둥 검출 상태만 확인
"""
import sys, json, math, time, os
import urllib.request as UR
import numpy as np

sys.path.insert(0, "/home/ar/bf2_console/tools")
import base_depth_corner as B
import pillar_dots as PD
import house_geometry as HG

CAM = "http://127.0.0.1:8766"
REF = "/home/ar/bf2_console/base_twist_ref.json"
MIN_MARKERS = 3           # 이보다 적으면 자가 흔들려 판정 불가
MM_PER_PX_FALLBACK = 0.4066


def markers():
    """{id: [(x,y)×4]} — 마커 코너(서브픽셀)."""
    try:
        d = json.loads(UR.urlopen(CAM + "/aruco", timeout=5).read())
    except Exception as e:
        return None, f"마커 조회 실패 {e}"
    ms = d.get("markers") or []
    if not ms:
        return None, "마커 0개"
    return {int(m["id"]): [(float(x), float(y)) for x, y in m["corners"]] for m in ms}, None


def pillars():
    """기둥 색점 4개 [(x,y,area,color)] + 밑판 사각형."""
    img, grid = B.grab_pair()
    if img is None:
        return None, None, "프레임 없음"
    try:
        m, why = B.detect_rect(img, grid)
    except Exception as e:
        m, why = None, str(e)
    rect = None
    if m:
        rect = PD.plate_rect(m)
    if rect is None:
        return None, None, f"밑판 사각형 실패({why})"
    pts, why2 = PD.four_corners(img, rect)
    if not pts or len(pts) < 4:
        return None, rect, why2 or "기둥 4점 미검출"
    return pts, rect, None


def fit_similarity(src, dst):
    """src → dst 로 옮기는 2D 강체+축척 (회전 R, 축척 s, 평행이동 t).
    반환 (s, theta_rad, tx, ty, rms). 점 대응은 순서대로."""
    P = np.asarray(src, float); Q = np.asarray(dst, float)
    cp, cq = P.mean(0), Q.mean(0)
    P0, Q0 = P - cp, Q - cq
    sxx = float((P0[:, 0] * Q0[:, 0]).sum() + (P0[:, 1] * Q0[:, 1]).sum())
    sxy = float((P0[:, 0] * Q0[:, 1]).sum() - (P0[:, 1] * Q0[:, 0]).sum())
    th = math.atan2(sxy, sxx)
    den = float((P0 ** 2).sum())
    s = math.hypot(sxx, sxy) / den if den > 0 else 1.0
    c, sn = math.cos(th), math.sin(th)
    R = np.array([[c, -sn], [sn, c]])
    t = cq - s * (R @ cp)
    pred = (s * (R @ P.T).T) + t
    rms = float(np.sqrt(((pred - Q) ** 2).sum(1).mean()))
    return s, th, t[0], t[1], rms


def apply_sim(pts, s, th, tx, ty):
    c, sn = math.cos(th), math.sin(th)
    return [(s * (c * x - sn * y) + tx, s * (sn * x + c * y) + ty) for x, y in pts]


def board_frame_transform(cur_m, ref_m):
    """현재 마커 → 기준 마커 로 겹치는 변환. 공통 id 의 코너 전부 사용."""
    ids = sorted(set(cur_m) & set(ref_m))
    if len(ids) < MIN_MARKERS:
        return None, f"공통 마커 {len(ids)}개(<{MIN_MARKERS}) — 기준자 부족"
    src, dst = [], []
    for i in ids:
        src += cur_m[i]; dst += ref_m[i]
    s, th, tx, ty, rms = fit_similarity(src, dst)
    return {"s": s, "th": th, "tx": tx, "ty": ty, "rms_px": rms, "ids": ids}, None


def pose_from_pillars(pts, mm_per_px):
    """색 이름표가 붙은 기둥 4점 → 베이스 pose(mm). base_pose_check 와 같은 대응 규칙."""
    import base_pose_check as BPC
    by = {}
    for p in pts:
        by.setdefault(p[3] if len(p) > 3 else "?", []).append(p[:2])
    if "yellow" not in by or "red" not in by or len(by.get("blue", [])) < 2:
        return None, "색 구성 부족"
    ye, re_ = by["yellow"][0], by["red"][0]
    blues = by["blue"][:2]
    b_y = min(blues, key=lambda b: math.dist(b, ye))
    b_r = [b for b in blues if b is not b_y][0]
    meas = [ye, re_, b_r, b_y]
    model = [BPC.COLOR_MAP["yellow"], BPC.COLOR_MAP["red"],
             BPC.BLUE_FOR["red"], BPC.BLUE_FOR["yellow"]]
    pose, rms = HG.fit_base_pose([(p[0] * mm_per_px, p[1] * mm_per_px) for p in meas], tuple(model))
    return {"pose": pose, "rms": rms}, None


def measure(ref=None):
    m, why = markers()
    if m is None:
        return None, why
    pts, rect, why2 = pillars()
    if pts is None:
        return None, why2
    out = {"markers": {str(k): v for k, v in m.items()},
           "pillars": [[p[0], p[1], p[2], p[3]] for p in pts]}
    if ref is None:
        return out, None
    ref_m = {int(k): v for k, v in ref["markers"].items()}
    T, whyT = board_frame_transform(m, ref_m)
    if T is None:
        return None, whyT
    moved = apply_sim([(p[0], p[1]) for p in pts], T["s"], T["th"], T["tx"], T["ty"])
    labeled = [(x, y, pts[i][2], pts[i][3]) for i, (x, y) in enumerate(moved)]
    mmpx = ref.get("mm_per_px") or MM_PER_PX_FALLBACK
    now, w1 = pose_from_pillars(labeled, mmpx)
    was, w2 = pose_from_pillars([(p[0], p[1], p[2], p[3]) for p in ref["pillars"]], mmpx)
    if now is None or was is None:
        return None, w1 or w2
    dyaw = HG.wrap_deg(now["pose"].yaw_deg - was["pose"].yaw_deg)
    dx = now["pose"].x - was["pose"].x
    dy = now["pose"].y - was["pose"].y
    out.update(T=T, dx=dx, dy=dy, dyaw=dyaw,
               dpos=math.hypot(dx, dy), fit_rms=now["rms"])
    return out, None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        m, why = markers()
        print(f"마커: {len(m) if m else 0}개 {sorted(m) if m else why}")
        pts, rect, why2 = pillars()
        print(f"기둥: {len(pts) if pts else 0}점 {why2 or 'OK'}")
        if pts:
            for p in pts:
                print(f"   {p[3]:7} ({p[0]:7.1f},{p[1]:7.1f})")
        return

    if mode == "ref":
        cur, why = measure(None)
        if cur is None:
            print("❌", why); return
        cur["made"] = time.strftime("%Y-%m-%d %H:%M")
        try:
            import color_lock as CL
            cur["tcp"] = json.loads(UR.urlopen("http://127.0.0.1:8765/status", timeout=5).read())["robots"]["fr5"]["tcp"]
        except Exception:
            cur["tcp"] = None
        # 축척: 기둥 긴 변 200mm 로 자가 보정
        by = {}
        for p in cur["pillars"]:
            by.setdefault(p[3], []).append((p[0], p[1]))
        try:
            bb = by["blue"]
            cur["mm_per_px"] = 200.0 / math.dist(bb[0], bb[1])
        except Exception:
            cur["mm_per_px"] = MM_PER_PX_FALLBACK
        json.dump(cur, open(REF, "w"), ensure_ascii=False, indent=1)
        print(f"✅ 기준 저장 {REF}")
        print(f"   마커 {len(cur['markers'])}개 · 기둥 4점 · 축척 {cur['mm_per_px']:.4f}mm/px")
        return

    if mode == "run":
        if not os.path.exists(REF):
            print("❌ 기준 없음 — 먼저 `base_twist.py ref`"); return
        ref = json.load(open(REF))
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 5
        rows, fails = [], []
        for i in range(n):
            r, why = measure(ref)
            if r is None:
                fails.append(why); print(f"  [{i+1}/{n}] 실패: {why}"); continue
            rows.append(r)
            print(f"  [{i+1}/{n}] Δx {r['dx']:+7.3f}  Δy {r['dy']:+7.3f}  Δyaw {r['dyaw']:+7.4f}°   "
                  f"(마커맞춤 rms {r['T']['rms_px']:.2f}px, 마커 {len(r['T']['ids'])}개)")
        if not rows:
            print(f"\n❌ 측정 실패 — {set(fails)}"); return
        import statistics as st
        dx = [r["dx"] for r in rows]; dy = [r["dy"] for r in rows]; dw = [r["dyaw"] for r in rows]
        print(f"\n━━ 베이스 틀어짐 (ArUco 고정판 기준, n={len(rows)}, 실패 {len(fails)}) ━━")
        print(f"  Δx   {st.mean(dx):+7.3f} mm   σ {st.pstdev(dx):.3f}")
        print(f"  Δy   {st.mean(dy):+7.3f} mm   σ {st.pstdev(dy):.3f}")
        print(f"  Δyaw {st.mean(dw):+7.4f}°     σ {st.pstdev(dw):.4f}")
        print(f"  이동량 {math.hypot(st.mean(dx), st.mean(dy)):.3f} mm")
        print(f"  마커 맞춤 rms {st.mean([r['T']['rms_px'] for r in rows]):.2f}px "
              f"(이게 크면 기준자 자체가 흔들린 것 — 판정 신뢰 저하)")
        # 슬롯 허용치(긴벽 ±0.5mm·0.29°) 대비
        print(f"\n  긴벽 허용 ±0.5mm·0.29° 대비: 위치 {math.hypot(st.mean(dx), st.mean(dy))/0.5*100:.0f}% · "
              f"각 {abs(st.mean(dw))/0.29*100:.0f}%")
        return

    print(__doc__)


if __name__ == "__main__":
    main()
