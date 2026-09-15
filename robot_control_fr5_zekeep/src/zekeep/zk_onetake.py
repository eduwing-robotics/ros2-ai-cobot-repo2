#!/usr/bin/env python3
"""
zk_onetake.py - 밑판 1장 원테이크 (ZK1 팔레트→컨베이어, ZK2 컨베이어→조립대)

    python3 zk_onetake.py              # 전 구간
    python3 zk_onetake.py --only zk1   # ZK1 만
    python3 zk_onetake.py --only zk2   # ZK2 만
    python3 zk_onetake.py --dry        # 실행 계획만 출력

★ 수직이 필요한 구간만 zk_vlift 를 쓴다 (pick→lift 인출, hover→place 안착, place→후퇴).
  나머지는 goto. 8/20 실측: 수직 저속 Δr 0.05~0.43mm / 손조그 55.8mm.
★ 각 단계 후 실제 도달각을 찍는다 — "명령 보냈다 = 움직였다" 로 판정하지 않는다.
"""
import subprocess, sys, time

DRY = "--dry" in sys.argv
ONLY = sys.argv[sys.argv.index("--only")+1] if "--only" in sys.argv else None

def run(desc, args):
    print(f"\n▶ {desc}\n    $ {' '.join(args)}", flush=True)
    if DRY: return True
    r = subprocess.run([sys.executable] + args, cwd="/home/ar",
                       capture_output=True, text=True, timeout=700)
    tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-3:]
    for l in tail: print("    " + l, flush=True)
    if r.returncode != 0:
        print(f"    🔴 실패 (rc={r.returncode}) — 중단", flush=True)
        if r.stderr.strip(): print("    " + r.stderr.strip()[-300:], flush=True)
        return False
    return True

def goto(rb, pose):   return run(f"[{rb}] goto {pose}", ["zk_pose.py","goto",pose,"--robot",rb])
def vlift(rb, mm, s): return run(f"[{rb}] 수직 {mm:+}mm", ["zk_vlift.py","up","--rise",str(mm),
                                 "--robot",rb,"--loaded","--yes","--speed","5","--z-step",str(s)])
def refine(rb, pose): return run(f"[{rb}] 잔차 다듬 {pose}", ["zk_refine.py","--robot",rb,"--pose",pose,"--tol","0.15"])
def pump(rb, on):
    if on: return run(f"[{rb}] 흡착 ON", ["zk_io.py","pump","on","--robot",rb])
    ok = run(f"[{rb}] 흡착 OFF", ["zk_io.py","pump","off","--robot",rb])
    return ok and run(f"[{rb}] 배기 2초", ["zk_io.py","valve","2","--robot",rb])

L1_=L2_=200.0; Q20_,Q30_=7.741,-18.932
def _fk(a2,a3):
    import math
    q2,q3=math.radians(Q20_-a2),math.radians(Q30_-a3)
    return (L1_*math.sin(q2)+L2_*math.cos(q3), L1_*math.cos(q2)-L2_*math.sin(q3))

def place_drop(rb):
    """hover→place 낙차를 저장된 자세에서 직접 구한다.
    ★하드코딩 금지 — 로봇마다·재티칭마다 달라진다(2026-08-21: ZK1 60→15mm)."""
    import json, os
    f = "/home/ar/zkbot_data/poses.json" if rb=="zkbot1" else "/home/ar/zkbot_data/poses_zkbot2.json"
    d = json.load(open(f))
    hv, pl = d["base_hover"], d["base_place"]
    dz = _fk(pl["A2"],pl["A3"])[1] - _fk(hv["A2"],hv["A3"])[1]
    dr = _fk(pl["A2"],pl["A3"])[0] - _fk(hv["A2"],hv["A3"])[0]
    da1 = pl["A1"] - hv["A1"]
    if dz > 0:
        raise SystemExit(f"■ {rb}: base_place 가 base_hover 보다 위에 있다 (Δz {dz:+.1f}) — 자세 확인 필요")
    if abs(da1) > 0.3 or abs(dr) > 2.0:
        print(f"  ⚠ {rb}: hover→place 가 순수 수직이 아님 (ΔA1 {da1:+.2f}°, Δr {dr:+.1f}mm)"
              f" — 수직 하강 후 다듬이 옆으로 끕니다", flush=True)
    return round(-dz, 1)

def cycle(rb, place_vertical_mm):
    """base_above → pick → 흡착 → 수직인출 → above → hover → 안착 → 해제 → 수직후퇴 → above"""
    seq = [
        lambda: goto(rb,"base_above"),
        lambda: goto(rb,"base_pick"),
        lambda: refine(rb,"base_pick"),      # ★8/12 교훈: goto 잔차(0.2~0.35°≈2mm)를 지운다.
                                             #   흡착은 접촉이 생명이라 pick 다듬이 특히 중요.
        lambda: pump(rb, True),
        lambda: (time.sleep(2) if not DRY else None) or True,
        lambda: vlift(rb, 28, 3),
        lambda: goto(rb,"base_above"),
        lambda: goto(rb,"base_hover"),
        lambda: vlift(rb, -place_vertical_mm, 4),
        lambda: refine(rb,"base_place"),     # ★수직하강은 시작점 잔차를 물려받는다 →
                                             #   티칭한 base_place 각도로 수렴시킨 뒤 놓는다.
        lambda: pump(rb, False),
        lambda: vlift(rb, place_vertical_mm, 4),
        lambda: goto(rb,"base_above"),
    ]
    for step in seq:
        if not step():
            print(f"\n🔴 {rb} 시퀀스 중단", flush=True); return False
    print(f"\n✅ {rb} 사이클 완료", flush=True); return True

print("="*60); print("  밑판 원테이크" + ("  [DRY RUN]" if DRY else "")); print("="*60)
if ONLY in (None,"zk1"):
    dz = place_drop("zkbot1"); print(f"  [zkbot1] hover→place 낙차 = {dz}mm (자세에서 계산)")
    if not cycle("zkbot1", dz): sys.exit(1)
if ONLY in (None,"zk2"):
    dz = place_drop("zkbot2"); print(f"  [zkbot2] hover→place 낙차 = {dz}mm (자세에서 계산)")
    if not cycle("zkbot2", dz): sys.exit(1)
print("\n" + "="*60); print("  ★ 원테이크 완료"); print("="*60)
