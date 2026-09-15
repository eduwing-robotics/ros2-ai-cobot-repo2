#!/usr/bin/env python3
"""
zk_autorun.py - 픽앤플레이스(티칭 재생) 실행 + 감시

교재 8절 자동모드 구조
  D90=2(자동) → M203(기동) + M21(단일순환)
  자동모드 1스텝은 강제 원점복귀, 이후 D70(총 스텝)만큼 티칭 포인트를 재생.
  각 스텝마다 D400Z0 속도 / D450Z0 지연 / D150Z0 기펌프 상태를 따른다.
  D40 = 현재 몇 번째 포인트.

★ 전제조건: 세 축 모두 원점 확보(M121/M122/M123).
  티칭값은 원점 기준 절대 펄스라 원점이 없으면 재생 = 엉뚱한 좌표로 돌진이다.
  원점이 없으면 이 스크립트는 시작을 거부한다.

안전
  · 원점 미확보 시 시작 거부
  · 소프트리밋 절대각 감시 (원점이 맞으므로 이제 유효하다)
  · X5(급정지) / 통신두절 즉시 중단
  · 타임아웃
  · finally 에서 M21/M22/M203 해제 + D90=0(정지) + 펌프·밸브 OFF
"""
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D, PUMP, VALVE                       # noqa: E402
from zk_safety import Guard, Abort, JOINT_D, HOME_FLAG      # noqa: E402
import zk_profiles as ZPROF                                 # noqa: E402

TIMEOUT = 120.0
M_START, M_ONESHOT, M_LOOP = 203, 21, 22
MODE_REQ = (219, 220, 218)     # D90 직접쓰기가 스냅백될 때 쓸 모드요청 비트 후보


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def angles(zk):
    return {j: ang(zk, d) for j, d in JOINT_D.items()}


def fmt(a):
    return "  ".join(f"{j}={'  ----' if a[j] is None else f'{a[j]:8.2f}'}"
                     for j in ("A1", "A2", "A3"))


def set_mode(zk, want):
    """D90을 want로. 직접 쓰기가 스냅백되면 모드요청 비트를 펄스한다."""
    zk.set_d(90, want)
    time.sleep(0.3)
    if zk.d(90) == want:
        return True
    for b in MODE_REQ:
        zk.m_on(b)
        time.sleep(0.3)
        zk.m_off(b)
        time.sleep(0.2)
        if zk.d(90) == want:
            print(f"  (모드 전환은 M{b} 펄스로 성립)", flush=True)
            return True
    return False


def stop_all(zk):
    for b in (M_ONESHOT, M_LOOP, M_START):
        for _ in range(2):
            zk.m_off(b)
    zk.y_off(PUMP)
    zk.y_off(VALVE)
    zk.set_d(90, 0)


def main():
    loop = "--loop" in sys.argv
    zk = ZK(ZPROF.resolve_from_argv().port)
    if not zk.link():
        sys.exit("링크 실패")

    aborted = None
    try:
        # ── 전제조건: 원점 ──
        ms = zk.m(32) or set()
        missing = [j for j, b in HOME_FLAG.items() if b not in ms]
        if missing:
            sys.exit(f"■ 시작 거부 — 원점 미확보 축: {missing}\n"
                     f"  티칭값은 원점 기준 절대 펄스입니다. 먼저 원점복귀하세요:\n"
                     f"    ~/zkbot_venv/bin/python ~/zk_home_run.py")

        guard = Guard(zk, homing=False)
        a0 = angles(zk)
        info = guard.preflight(a0)
        total = zk.d(70)
        print("── 사전 점검 ──", flush=True)
        print(f"  각도  {fmt(a0)}", flush=True)
        print(f"  원점 확보: {info['homed']}   티칭 스텝 D70={total}", flush=True)
        if info["out_of_range"]:
            print(f"  ⚠ 소프트리밋 밖: {info['out_of_range']}", flush=True)

        # ── 자동모드 ──
        print("\n── 자동모드 전환 ──", flush=True)
        if not set_mode(zk, 2):
            sys.exit(f"■ D90=2 전환 실패 (현재 {zk.d(90)}) — 자동모드로 못 들어감")
        print(f"  D90={zk.d(90)} (자동)", flush=True)

        print("\n── 기동 ──", flush=True)
        zk.m_on(M_START)
        time.sleep(0.2)
        zk.m_on(M_LOOP if loop else M_ONESHOT)
        print(f"  M{M_START}(기동) + M{M_LOOP if loop else M_ONESHOT}"
              f"({'연속순환' if loop else '단일순환'}) ON", flush=True)
        print("  ※ 자동모드 1스텝은 강제 원점복귀입니다\n", flush=True)

        prev = None
        t0 = time.time()
        while time.time() - t0 < TIMEOUT:
            a = angles(zk)
            guard.check(a)                      # 소프트리밋·급정지 감시

            ms = zk.m(32) or set()
            ys = zk.y() or set()
            step = zk.d(40)
            io = ",".join(n for i, n in ((12, "펌프"), (13, "밸브")) if i in ys) or "-"
            line = f"{fmt(a)}  스텝 {step}/{total}  [{io}]"
            if line != prev:
                print(f"  {time.time()-t0:6.1f}  {line}", flush=True)
                prev = line

            if not loop and M_ONESHOT not in ms and time.time() - t0 > 3:
                print("\n  ★★ 단일순환 완료 (M21 자동 리셋)", flush=True)
                break
            time.sleep(0.2)
        else:
            aborted = f"타임아웃 {TIMEOUT:.0f}초"

    except Abort as e:
        aborted = str(e)
        print(f"\n  ■ 안전 중단: {e}", flush=True)
    except SystemExit:
        raise
    finally:
        try:
            stop_all(zk)
            print("  정지 처리: M21/M22/M203 해제, D90=0, 펌프·밸브 OFF", flush=True)
            a = angles(zk)
            ms = zk.m(32) or set()
            xs = zk.x() or set()
            print("\n── 최종 ──", flush=True)
            print(f"  각도 {fmt(a)}", flush=True)
            print(f"  원점 " + " ".join(
                f"{j}={'ON' if b in ms else 'off'}" for j, b in HOME_FLAG.items()),
                flush=True)
            print(f"  D90={zk.d(90)}  급정지={5 in xs}", flush=True)
            if aborted:
                print(f"  중단 사유: {aborted}", flush=True)
            print(f"\n{zk.report()}", flush=True)
        finally:
            zk.close()


if __name__ == "__main__":
    main()
