#!/usr/bin/env python3
"""
zk_dump.py - ZKBOT 전체 상태 덤프 (읽기 전용, 쓰기 일절 없음)

교육자료 5~8절에서 확정된 레지스터 지도를 그대로 읽는다.
  D8340/D8350/D8360 = A1/A2/A3 현재 펄스 위치 (★기본 FX 맵으론 접근 불가)
  D100Z0/D200Z0/D300Z0 = 티칭 포인트 X/Y/Z (스텝당 2워드씩 전진)
  D150Z0=기펌프, D400Z0=속도%, D450Z0=시간
  D30=현재 설입 스텝, D70=총 스텝, D40=자동 현재 포인트
  각도 θ = S*360/(n*i),  n=D72(펄스/회전), i=D74(감속비), D1060=n*i
"""
import struct
import sys

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D, names, XN, YN  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402


def rd_words(zk, dstart, nwords):
    """D 레지스터 연속 읽기 → 16bit 부호없는 리스트. 64바이트씩 끊어 읽는다."""
    out = []
    got = 0
    while got < nwords:
        chunk = min(32, nwords - got)          # 32워드 = 64바이트 (프로토콜 상한)
        b = zk.read(A_D + (dstart + got) * 2, chunk * 2)
        if b is None:
            out.extend([None] * chunk)
        else:
            out.extend(int.from_bytes(b[i * 2:i * 2 + 2], "little")
                       for i in range(chunk))
        got += chunk
    return out


def i32(lo, hi):
    if lo is None or hi is None:
        return None
    v = lo | (hi << 16)
    return v - (1 << 32) if v & 0x80000000 else v


def f32(lo, hi):
    if lo is None or hi is None:
        return None
    return struct.unpack("<f", struct.pack("<HH", lo, hi))[0]


def main():
    zk = ZK(ZPROF.resolve_from_argv().port)
    if not zk.link():
        sys.exit("링크 실패")

    print("=" * 66)
    print("1) 입출력")
    print("=" * 66)
    xs, ys = zk.x(), zk.y()
    print(f"  X ON : {names(xs, XN) or '-'}")
    print(f"  Y ON : {names(ys, YN) or '-'}")

    print()
    print("=" * 66)
    print("2) 모드 / 속도 / 기구 파라미터")
    print("=" * 66)
    w = rd_words(zk, 30, 60)          # D30~D89

    def g(d):
        return w[d - 30] if 30 <= d < 90 else None

    print(f"  D30 현재 설입 스텝   = {g(30)}")
    print(f"  D40 자동 현재 포인트 = {g(40)}")
    print(f"  D70 총 설입 스텝수   = {g(70)}   ← 티칭된 포인트 개수")
    print(f"  D76 크리프 속도      = {g(76)}")
    print(f"  D78 기펌프 상태      = {g(78)}")
    print(f"  D50 수동속도(float)  = {f32(g(50), g(51))}   (정수해석 {g(50)})")
    print(f"  D52 최고속도(float)  = {f32(g(52), g(53))}")
    print(f"  D60 설입속도%        = {g(60)}")
    print(f"  D72 펄스/회전(n)     = {g(72)}")
    print(f"  D74 감속비(i)        = {g(74)}")
    d90 = zk.d(90)
    print(f"  D90 모드             = {d90} "
          f"({ {0:'정지',1:'수동',2:'자동'}.get(d90,'?') })")

    print()
    print("=" * 66)
    print("3) 현재 위치 / 원점거리 / 이전점 차이  (D1000~D1250)")
    print("=" * 66)
    p = rd_words(zk, 1000, 64)        # D1000~D1063

    def q(d):
        return p[d - 1000] if 1000 <= d < 1064 else None

    print(f"  D1060 n*i            = {i32(q(1060), q(1061))}")
    print("  ── 관절 (가이드 검증: D1010/D1030/D1050 = 각도 float32) ──")
    for nm, da, dp in (("A1 베이스", 1010, 1000), ("A2 큰팔", 1030, 1020),
                       ("A3 작은팔", 1050, 1040)):
        ang = f32(q(da), q(da + 1))
        pul = i32(q(dp), q(dp + 1))
        print(f"    {nm:<9} 각도 D{da}={ang if ang is None else f'{ang:10.4f}°'}"
              f"   펄스 D{dp}={pul}")

    for label, base in (("원점까지 거리(6절)", 1100), ("이전점 차이(7절)", 1200)):
        r = rd_words(zk, base, 48)
        vals = [i32(r[o], r[o + 1]) for o in (0, 20, 40)]
        print(f"  {label:<18} X={vals[0]}  Y={vals[1]}  Z={vals[2]}")

    print()
    print("=" * 66)
    print("4) 티칭 포인트  (D100/D200/D300 + Z0, 스텝당 2워드)")
    print("=" * 66)
    total = g(70)
    show = max(int(total or 0), 12)
    show = min(show, 25)
    px = rd_words(zk, 100, show * 2)
    py = rd_words(zk, 200, show * 2)
    pz = rd_words(zk, 300, show * 2)
    pv = rd_words(zk, 400, show * 2)
    pa = rd_words(zk, 150, show * 2)

    ni = i32(q(1060), q(1061)) or 0

    def deg(pulse):
        if pulse is None or not ni:
            return "     -"
        return f"{pulse * 360.0 / ni:9.2f}°"

    print(f"  총 스텝 D70 = {total}   (아래 {show}스텝 표시)")
    print(f"  {'스텝':>4} {'X펄스':>10}{'':>2}{'A1각':>10} "
          f"{'Y펄스':>10}{'':>2}{'A2각':>10} {'Z펄스':>10}{'':>2}{'A3각':>10} "
          f"{'속도%':>6} {'펌프':>4}")
    for s in range(show):
        o = s * 2
        vx, vy, vz = i32(px[o], px[o + 1]), i32(py[o], py[o + 1]), i32(pz[o], pz[o + 1])
        vv, vp = i32(pv[o], pv[o + 1]), i32(pa[o], pa[o + 1])
        mark = " ←현재" if total and s == (g(40) or -1) else ""
        print(f"  {s:>4} {str(vx):>10}  {deg(vx)} {str(vy):>10}  {deg(vy)} "
              f"{str(vz):>10}  {deg(vz)} {str(vv):>6} {str(vp):>4}{mark}")

    print()
    print(zk.report())
    zk.close()


if __name__ == "__main__":
    main()
