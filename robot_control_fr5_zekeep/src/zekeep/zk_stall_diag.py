#!/usr/bin/env python3
"""
zk_stall_diag.py - "일정 각도 이상 안 올라가고 덜덜거림" 원인 판별 (읽기 전용)

★ 이 스크립트는 PLC에 아무것도 쓰지 않는다. 강제 ON/OFF도 하지 않는다.
   조그는 사람이 HMI(또는 별도 도구)로 하고, 이 스크립트는 옆에서 관찰만 한다.

판별 원리
    조그로 A2를 올리는 동안 실측 관절각(D1030 float32)을 계속 읽는다.

    ① 각도가 계속 변하는데 팔은 안 움직인다
       → 래더는 펄스를 내보내는 중, 모터가 못 따라감 = 탈조
       = 토크/전원/기구 문제 (전기·기계 쪽을 봐야 함)

    ② 각도가 특정 값에서 딱 멈춘다
       → 래더가 스스로 펄스를 끊은 것 = 소프트리밋/로직 문제
       = 그 멈춘 값이 곧 소프트리밋 경계 (코드·설정 쪽을 봐야 함)

    두 경우의 해결책이 완전히 다르므로 이걸 먼저 가른다.

사용법
    python3 zk_stall_diag.py            # 현재 상태 1회 스냅샷
    python3 zk_stall_diag.py --watch    # 연속 관찰 (Ctrl+C로 종료)
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D, A_D8, names, XN, YN  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402

# 가이드(ZKBOT_로봇_제어_가이드.md)에서 HMI 화면값과 대조 검증된 주소
JOINTS = [("A1(베이스)", 1010), ("A2(큰팔)", 1030), ("A3(작은팔)", 1050)]
MODE_NAME = {0: "STOP", 1: "MANUAL", 2: "AUTO"}


def read_angle(zk, dnum):
    """D{n}:D{n+1} 을 float32(도)로. 실패 시 None."""
    b = zk.read(A_D + dnum * 2, 4)
    if b is None:
        return None
    try:
        return struct.unpack("<f", b)[0]
    except struct.error:
        return None


def link_health(zk, n=10):
    """D8001 == 24320 을 n회 읽어 통신 품질을 먼저 잰다."""
    good = 0
    for _ in range(n):
        b = zk.read(A_D8 + 1 * 2, 2)
        if b is not None and int.from_bytes(b, "little") == 24320:
            good += 1
    return good, n


def snapshot(zk):
    xs = zk.x()
    ys = zk.y()
    ms = zk.m(32)
    d90 = zk.d(90)
    angles = [(nm, read_angle(zk, dn)) for nm, dn in JOINTS]
    return xs, ys, ms, d90, angles


def show_snapshot(zk):
    xs, ys, ms, d90, angles = snapshot(zk)

    print("── 입력 X ─────────────────────────────")
    print(f"  ON: {names(xs, XN) or '(없음)'}")
    if xs is not None:
        print(f"  급정지(X5): {'★ON — 이게 켜져 있으면 래더가 전부 무시한다' if 5 in xs else 'OFF (정상)'}")
        for i, nm in ((0, "A1"), (1, "A2"), (2, "A3")):
            print(f"  {nm} 리밋(X{i}): {'★ON — 이미 리밋에 닿아 있음' if i in xs else 'OFF'}")

    print("── 출력 Y ─────────────────────────────")
    print(f"  ON: {names(ys, YN) or '(없음)'}")
    if ys is not None:
        ena = [nm for i, nm in ((8, "A1_ENA"), (9, "A2_ENA"), (10, "A3_ENA")) if i in ys]
        print(f"  축 인에이블: {ena or '★ 전부 OFF — 모터에 전류가 안 걸린 상태일 수 있음'}")

    print("── 모드 ───────────────────────────────")
    print(f"  D90 = {d90} ({MODE_NAME.get(d90, '?')})"
          f"{'   ★ STOP 모드면 조그가 안 먹을 수 있다' if d90 == 0 else ''}")
    if ms is not None:
        for n, nm in ((200, "STOP"), (201, "MANUAL"), (202, "AUTO")):
            print(f"  M{n}({nm}): {'ON' if n in ms else 'off'}")

    print("── 실측 관절각 ────────────────────────")
    for nm, v in angles:
        print(f"  {nm:>10}: {'읽기실패' if v is None else f'{v:9.4f}°'}")
    return angles


def watch(zk, hz=4.0):
    print("\n관찰 시작. 이제 조그로 A2/A3를 올려 보세요. (Ctrl+C 종료)")
    print("각도가 '변하는데 팔이 안 움직이면' 탈조, '딱 멈추면' 소프트리밋입니다.\n")
    print(f"{'경과':>6} {'A1':>10} {'A2':>10} {'A3':>10}  {'ΔA2':>8} {'ΔA3':>8}  리밋/급정지")
    prev = {}
    frozen = {"A2(큰팔)": 0, "A3(작은팔)": 0}
    t0 = time.time()
    period = 1.0 / hz
    while True:
        loop = time.time()
        xs = zk.x()
        cells, deltas = [], []
        for nm, dn in JOINTS:
            v = read_angle(zk, dn)
            cells.append("     ----" if v is None else f"{v:10.4f}")
            if nm in frozen:
                if v is None or nm not in prev:
                    deltas.append("    ----")
                else:
                    dv = v - prev[nm]
                    deltas.append(f"{dv:+8.4f}")
                    # 0.002° 미만이면 사실상 정지로 본다
                    frozen[nm] = frozen[nm] + 1 if abs(dv) < 0.002 else 0
            if v is not None:
                prev[nm] = v

        flags = []
        if xs:
            for i, nm in ((0, "A1리밋"), (1, "A2리밋"), (2, "A3리밋")):
                if i in xs:
                    flags.append(nm)
            if 5 in xs:
                flags.append("★급정지")
        print(f"{time.time()-t0:6.1f} {' '.join(cells)}  {' '.join(deltas)}  "
              f"{','.join(flags) if flags else '-'}")

        for nm, cnt in frozen.items():
            if cnt == int(hz * 3):  # 3초간 정지
                print(f"       └ {nm} 각도가 3초째 그대로입니다 "
                      f"→ 조그를 누르고 있는 중이라면 래더가 펄스를 끊은 것 "
                      f"(소프트리밋 의심, 경계값 {prev.get(nm)})")

        time.sleep(max(0, period - (time.time() - loop)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="연속 관찰 모드")
    ap.add_argument("--robot", default=ZPROF.DEFAULT_ROBOT)
    ap.add_argument("--port", default=None)
    ap.add_argument("--hz", type=float, default=4.0)
    args = ap.parse_args()

    try:
        args.port = ZPROF.resolve(robot=args.robot, port=args.port).port
        zk = ZK(args.port)
    except Exception as e:
        sys.exit(f"[에러] 포트를 못 엽니다: {e}\n"
                 f"  · 어댑터가 꽂혀 있는지: lsusb | grep -i 1a86\n"
                 f"  · 권한: sudo chmod 666 {args.port}")

    if not zk.link():
        zk.close()
        sys.exit("[에러] PLC 링크 실패(ENQ→ACK 없음). PLC 전원과 배선을 확인하세요.")

    good, tot = link_health(zk)
    print(f"통신 품질: D8001 정상 {good}/{tot}")
    if good < tot:
        print("  ⚠ 100%가 아닙니다. 커넥터를 다시 꽂고 다시 재세요.")
        print("    (8/5에 헐거운 커넥터 때문에 깨진 프레임을 '상태 변화'로 오독한 전례가 있습니다)")
    print()

    try:
        show_snapshot(zk)
        if args.watch:
            watch(zk, args.hz)
    except KeyboardInterrupt:
        print("\n중단")
    finally:
        print(f"\n{zk.report()}")
        zk.close()


if __name__ == "__main__":
    main()
