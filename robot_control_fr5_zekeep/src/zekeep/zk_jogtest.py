#!/usr/bin/env python3
"""
zk_jogtest.py - 조그 코일 M1~M6 정밀 검증 (체크섬 검증된 관측)

각 코일을 하나씩 강제ON 하고 1.5초간 Y 출력을 빠르게 표본화한다.
펄스/방향 출력이 한 번이라도 잡히면 그 축이 실제로 구동된 것.

    python3 zk_jogtest.py            # M1~M6 전부
    python3 zk_jogtest.py 1 2        # M1, M2 만
    python3 zk_jogtest.py --sec 3    # 유지시간 변경
"""
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, XN, YN, PULSE_DIR, names  # noqa: E402

LABEL = {1: "A1 베이스 정회전", 2: "A1 베이스 역회전",
         3: "A2 큰팔 정회전", 4: "A2 큰팔 역회전",
         5: "A3 작은팔 정회전", 6: "A3 작은팔 역회전"}

args = [a for a in sys.argv[1:] if a.isdigit()]
coils = [int(a) for a in args] or [1, 2, 3, 4, 5, 6]
sec = 1.5
if "--sec" in sys.argv:
    sec = float(sys.argv[sys.argv.index("--sec") + 1])

z = ZK()
if not z.link():
    sys.exit("[에러] PLC 링크 실패")
print("링크 ✅")

z.all_off()
if z.estop():
    z.close()
    sys.exit("★ 급정지가 걸려 있습니다. 풀고 다시 실행하세요.")

base_y = z.y() or set()
print(f"기준 Y: {names(base_y, YN) or '없음'}")
print(f"기준 X: {names(z.x() or set(), XN) or '없음'}")
d50 = z.d(50)
print(f"D50 속도: {d50}%")
if d50 is not None and d50 < 10:
    z.set_d(50, 30)
    print(f"  → 30%로 상향 (읽기확인 {z.d(50)}%)")
print()

results = []
try:
    for n in coils:
        print(f"▶ M{n}  {LABEL.get(n,'?')}   {sec}초")
        ok = z.m_on(n)
        t0 = time.time()
        latched = None
        seen = set()
        samples = 0
        while time.time() - t0 < sec:
            y = z.y()
            if y is not None:
                seen |= y
                samples += 1
            if latched is None:
                mm = z.m(8)
                if mm is not None:
                    latched = n in mm
            if z.estop():
                print("   ★ 급정지 감지 — 중단")
                break
        z.m_off(n)
        z.m_off(n)
        new = seen - base_y
        moved = bool(new & PULSE_DIR)
        print(f"   강제ON:{'ACK' if ok else '실패'}  코일래치:{'O' if latched else 'X'}"
              f"  표본:{samples}")
        print(f"   관측된 Y: {names(new, YN) or '없음'}"
              f"   {'★ 구동됨!' if moved else ''}")
        results.append((n, moved, latched, sorted(names(new, YN))))
        time.sleep(0.4)
finally:
    z.all_off()
    print()
    print("=" * 58)
    for n, moved, latched, ys in results:
        mark = "✅ 구동" if moved else ("· 래치만" if latched else "❌ 무반응")
        print(f"  M{n} {LABEL.get(n,''):<14} {mark:<10} {ys or ''}")
    print("=" * 58)
    print(" ", z.report())
    print(f"  최종 Y: {names(z.y() or set(), YN) or '없음'}")
    z.close()
