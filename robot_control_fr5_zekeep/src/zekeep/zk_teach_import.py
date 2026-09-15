#!/usr/bin/env python3
"""
zk_teach_import.py - HMI 에서 设入(저장)한 티칭 스텝을 poses.json 으로 가져온다

왜 필요한가 (2026-08-21 사용자 요청)
  자세 하나 딸 때마다 PC 로 걸어와 "저장해줘" 하는 게 비효율이다.
  리모컨에서 조그 → 设入 → 下一步 를 반복해 여러 자세를 몰아 딴 뒤,
  이 도구로 한 번에 가져온다. 펌프 상태까지 함께 저장된다.

    python3 zk_teach_import.py --robot zkbot1                       # 목록만 보기
    python3 zk_teach_import.py --robot zkbot1 --from 0 --names base_pick,base_lift,base_place
    python3 zk_teach_import.py --robot zkbot1 --step 3 --name base_hover

★ 안전
  · 읽기 전용 + poses.json 쓰기만 한다. 로봇을 움직이지 않는다.
  · 원점 미확보면 경고한다 (펄스값의 기준이 없으면 각도가 의미 없다).
  · 덮어쓸 이름이 있으면 이전 값을 함께 출력한다.
  · 소프트리밋 밖 값은 🔴 표시 — 저장은 하되 goto 가 거부한다는 뜻이다.
"""
import json, struct, sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D
from zk_safety import JOINT_LIMITS, HOME_FLAG
import zk_pose as P
import zk_profiles as ZPROF

PULSES_PER_DEG = 6400 * 10 / 360.0        # 177.78 (8/10 해독)
BASE = {"A1": 100, "A2": 200, "A3": 300}  # D100+2n / D200+2n / D300+2n
PUMP_BASE, SPEED_BASE, DWELL_BASE = 150, 400, 450

def arg(n, d=None):
    return sys.argv[sys.argv.index(n)+1] if n in sys.argv else d

def d32(zk, d):
    b = zk.read(A_D + d*2, 4)
    return None if b is None else struct.unpack("<i", b)[0]

def d16(zk, d):
    b = zk.read(A_D + d*2, 2)
    return None if b is None else struct.unpack("<h", b)[0]

def read_step(zk, n):
    out = {}
    for j, base in BASE.items():
        p = d32(zk, base + 2*n)
        out[j] = None if p is None else round(p / PULSES_PER_DEG, 3)
    out["pump"] = d16(zk, PUMP_BASE + 2*n)
    out["speed"] = d16(zk, SPEED_BASE + 2*n)
    out["dwell"] = d16(zk, DWELL_BASE + 2*n)
    return out

def flag(s):
    bad = [j for j in BASE if s[j] is not None and not (JOINT_LIMITS[j][0] <= s[j] <= JOINT_LIMITS[j][1])]
    return "  🔴리밋밖 " + ",".join(bad) if bad else ""

def main():
    prof = P.apply_profile(ZPROF.resolve_from_argv())
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"■ 링크 실패 ({prof.name}) — PLC 전원과 USB 를 확인하세요")
    total, cur = d16(zk, 70), d16(zk, 30)
    ms = zk.m(32) or set()
    homed = [j for j, b in HOME_FLAG.items() if b in ms]
    print(f"  [{prof.name}] 총 스텝 D70={total}   현재 설입 스텝 D30={cur}")
    if len(homed) < 3:
        print(f"  ⚠ 원점 미확보 {sorted(set(BASE)-set(homed))} — 펄스값의 기준이 없어 각도를 신뢰할 수 없습니다")

    if arg("--step") is not None:
        idx = [int(arg("--step"))]
    elif arg("--from") is not None:
        n = len((arg("--names") or "").split(",")) if arg("--names") else max(0,(total or 0)-int(arg("--from")))
        idx = list(range(int(arg("--from")), int(arg("--from"))+n))
    else:
        idx = list(range(0, min(total or 0, 25)))

    steps = {}
    print("\n  스텝   A1        A2        A3      펌프 속도% 지연")
    for n in idx:
        s = read_step(zk, n); steps[n] = s
        f = lambda v: "  ----" if v is None else f"{v:8.3f}"
        print(f"   {n:2d}  {f(s['A1'])} {f(s['A2'])} {f(s['A3'])}   {s['pump']}   {s['speed']}   {s['dwell']}{flag(s)}")

    names = [x.strip() for x in (arg("--names") or (arg("--name") or "")).split(",") if x.strip()]
    if not names:
        print("\n  (이름을 주면 poses.json 에 넣습니다: --names base_pick,base_lift,...)")
        return
    if len(names) != len(idx):
        sys.exit(f"\n■ 이름 {len(names)}개 vs 스텝 {len(idx)}개 — 개수가 맞아야 합니다")

    db = P.load()
    for n, nm in zip(idx, names):
        s = steps[n]
        if any(s[j] is None for j in BASE):
            print(f"  ■ 스텝 {n} 읽기 실패 — {nm} 건너뜀"); continue
        if nm in db:
            o = db[nm]
            print(f"  ↻ {nm} 덮어씀  이전 A1={o['A1']:.3f} A2={o['A2']:.3f} A3={o['A3']:.3f}")
        db[nm] = {"A1": s["A1"], "A2": s["A2"], "A3": s["A3"],
                  "pump": s["pump"],
                  "saved": f"HMI 设入 스텝{n} 가져옴 {time.strftime('%Y-%m-%d %H:%M')}"}
        print(f"  ✅ {nm:14} A1={s['A1']:8.3f} A2={s['A2']:8.3f} A3={s['A3']:8.3f}  펌프={s['pump']}")
    P.save_db(db)
    print(f"\n  → {prof.pose_file}")

if __name__ == "__main__":
    main()
