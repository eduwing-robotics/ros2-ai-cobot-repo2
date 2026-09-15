#!/usr/bin/env python3
"""
zk_escape_limit.py - 리밋 스위치에 얹힌 축을 살짝 빼낸다

왜 필요한가
  리밋에 물린 채로 원점복귀(DZRN)를 걸면 리밋 방향으로 더 밀게 되어
  기구 끝을 때린다("털털"). 교재도 명시: 碰到限位开关要反向运行,
  不然会卡死在限位.  A2는 이 방법으로 빼낸 뒤 원점복귀에 성공했다.

안전 설계 (PC 조그는 오늘이 처음이라 두껍게 건다)
  · 조그 비트는 레벨 방식 → 0.3초 펄스 후 무조건 OFF, 매 회 확인
  · try/finally 로 관련 M비트 전부 강제 OFF (예외·Ctrl+C·크래시 포함)
  · 누적 이동 상한 (기본 15°) — 넘으면 중단
  · 펄스 횟수 상한
  · X5(급정지) 감지 즉시 중단
  · 한 방향으로 2회 눌러도 각도가 안 변하면 방향을 뒤집어 재시도
    (방향 표기는 HMI 기준이라 실제 부호를 신뢰할 수 없음)
  · 목표 달성(리밋 OFF) 즉시 정지

조그 비트 출처: ~/Downloads/ZKBOT_로봇_제어_가이드.md (HMI에서 추출)
"""
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402

AXIS = {
    # 축: (리밋 X번호, 각도 D주소, 정방향비트들, 역방향비트들)
    "A1": (0, 1010, (1, 31, 101), (2, 103)),
    "A2": (1, 1030, (3, 41, 105), (4, 107)),
    "A3": (2, 1050, (5, 51, 109), (6, 111)),
}
ALL_JOG = tuple(range(1, 13)) + (31, 33, 41, 43, 51, 53) + tuple(range(101, 113))

PULSE_S = 0.3
MAX_TRAVEL_DEG = 15.0
MAX_PULSES = 20


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def all_off(zk):
    for _ in range(2):
        for b in ALL_JOG:
            zk.m_off(b)


def main():
    axis = sys.argv[1] if len(sys.argv) > 1 else "A3"
    if axis not in AXIS:
        sys.exit(f"축은 {list(AXIS)} 중 하나")
    xno, dno, fwd, rev = AXIS[axis]

    zk = ZK(ZPROF.resolve_from_argv().port)
    if not zk.link():
        sys.exit("링크 실패")

    try:
        xs = zk.x() or set()
        if 5 in xs:
            sys.exit("X5(급정지)가 눌려 있음 — 먼저 푸세요")
        if xno not in xs:
            print(f"{axis} 리밋(X{xno})이 이미 OFF입니다. 할 일 없음.")
            return

        a0 = ang(zk, dno)
        print(f"{axis} 리밋 탈출 시작   시작각도 {a0:.2f}°", flush=True)
        print(f"  펄스 {PULSE_S}s / 이동상한 {MAX_TRAVEL_DEG}° / 최대 {MAX_PULSES}회",
              flush=True)

        all_off(zk)
        order = [("역방향", rev), ("정방향", fwd)]   # 리밋 탈출은 역방향이 정석
        pulses = 0

        for label, bits in order:
            print(f"\n── {label} 시도 (비트 {list(bits)}) ──", flush=True)
            no_move = 0
            while pulses < MAX_PULSES:
                before = ang(zk, dno)

                for b in bits:
                    zk.m_on(b)
                t = time.time()
                while time.time() - t < PULSE_S:
                    time.sleep(0.05)
                all_off(zk)                       # ★ 반드시 즉시 해제
                pulses += 1

                time.sleep(0.15)
                xs = zk.x() or set()
                after = ang(zk, dno)

                if 5 in xs:
                    print("  ■ X5(급정지) 감지 — 중단", flush=True)
                    return

                moved = (None if (before is None or after is None)
                         else after - before)
                total = (None if (after is None or a0 is None) else after - a0)
                print(f"  {pulses:>2}회  {after if after is None else f'{after:9.2f}°'}"
                      f"  이번 {'----' if moved is None else f'{moved:+6.2f}°'}"
                      f"  누적 {'----' if total is None else f'{total:+7.2f}°'}"
                      f"  X{xno}={'ON' if xno in xs else 'OFF'}", flush=True)

                if xno not in xs:
                    print(f"\n  ★★ {axis} 리밋 탈출 성공 — X{xno} OFF", flush=True)
                    return

                if total is not None and abs(total) > MAX_TRAVEL_DEG:
                    print(f"  ■ 누적 이동 {abs(total):.1f}° > 상한 {MAX_TRAVEL_DEG}° — 중단",
                          flush=True)
                    return

                if moved is not None and abs(moved) < 0.05:
                    no_move += 1
                    if no_move >= 2:
                        print(f"  {label}으로는 각도가 안 변함 → 방향 전환", flush=True)
                        break
                else:
                    no_move = 0

        print("\n  탈출 실패 — 두 방향 모두 소득 없음", flush=True)

    finally:
        all_off(zk)
        xs = zk.x() or set()
        ms = zk.m(32) or set()
        stuck = [b for b in ALL_JOG if b in ms]
        print("\n── 마무리 ──", flush=True)
        print(f"  조그 비트 전부 해제  {'✅ 잔류 없음' if not stuck else f'🔴 잔류 {stuck}'}",
              flush=True)
        print(f"  각도 {ang(zk,dno)}", flush=True)
        print(f"  리밋 X0={0 in xs} X1={1 in xs} X2={2 in xs}  급정지={5 in xs}",
              flush=True)
        print(f"\n{zk.report()}", flush=True)
        zk.close()


if __name__ == "__main__":
    main()
