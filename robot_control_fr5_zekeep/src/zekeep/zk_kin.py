#!/usr/bin/env python3
"""
zk_kin.py - ZKBOT 순/역기구학 (A1 회전면 내 2D)

★ 왜 필요한가 (2026-08-10 사용자 실측 관찰)
  A3 만 올리면 팔 끝이 원호를 그려 **앞뒤로 밀린다** → 흡착한 부품이 끌린다.
  A2·A3 를 함께 조절해야 수직에 가깝게 움직인다. 손으로 번갈아 누르던 것을
  계산으로 대체한다.

구조 (zkbot.urdf 실측값)
  a2 관절 : base 기준 (x=-0.00427, z=0.14301),  axis (0,1,0)
  a3 관절 : a2  기준 (x= 0.15505, z=0.12633),  axis (0,1,0)
  tool+tcp: a3  기준 (x= 0.15781, z=-0.22331), fixed

  A1 은 Z축 회전이라 **높이에 영향이 없다** → Z 제어에서 제외.
  따라서 A1 회전면 안의 2D(수평거리 r, 높이 z) 문제로 환원된다.

Y축 회전 행렬 (XZ 평면):
    x' =  x·cosθ + z·sinθ
    z' = -x·sinθ + z·cosθ
"""
import math

# URDF 실측 링크 파라미터
A2_ORIGIN = (-0.00427, 0.14301)     # base → a2
L1 = (0.15505, 0.12633)             # a2 → a3
L2 = (0.15781, -0.22331)            # a3 → zk_tcp (tool_joint + tcp_joint 합)


def _rot(v, th):
    """Y축 회전 (XZ 평면). th 는 라디안."""
    x, z = v
    return (x * math.cos(th) + z * math.sin(th),
            -x * math.sin(th) + z * math.cos(th))


def fk(a2_deg, a3_deg):
    """관절각(도) → zk_tcp 의 (수평거리 r, 높이 z). base_link 기준, A1 회전면 내.

    r 은 A1 축(수직축)으로부터의 수평 거리다. A1 각도와 무관하다.

    ★ 부호 규약 (2026-08-10 실물 검증)
      URDF 의 회전 방향과 PLC 카운터의 부호가 **반대**다.
      실측: A3 를 +2.86° 움직였더니 흡착판이 **올라갔다**.
      URDF 그대로 계산하면 내려가는 것으로 나오므로 각도를 반전해서 쓴다.
      이 한 줄을 빠뜨리면 "올려"가 "내려"가 되어 부품·지그를 찍는다.
    """
    t2 = math.radians(-a2_deg)
    t3 = math.radians(-a3_deg)
    p = A2_ORIGIN
    d1 = _rot(L1, t2)
    d2 = _rot(L2, t2 + t3)
    return (p[0] + d1[0] + d2[0], p[1] + d1[1] + d2[1])


def jacobian(a2_deg, a3_deg, h=1e-4):
    """수치 야코비안 ∂(r,z)/∂(a2,a3).  단위: m/도"""
    r0, z0 = fk(a2_deg, a3_deg)
    r2, z2 = fk(a2_deg + h, a3_deg)
    r3, z3 = fk(a2_deg, a3_deg + h)
    return ((r2 - r0) / h, (r3 - r0) / h,
            (z2 - z0) / h, (z3 - z0) / h)


def ik_delta(a2_deg, a3_deg, dr, dz, iters=60, tol=1e-6):
    """현재 각도에서 (dr, dz) 만큼 TCP 를 옮기는 목표 각도를 구한다.

    수직 이동만 원하면 dr=0 을 준다. 뉴턴 반복으로 수렴시킨다.
    돌려주는 값: (a2_target, a3_target) 또는 수렴 실패 시 None.
    """
    r0, z0 = fk(a2_deg, a3_deg)
    tr, tz = r0 + dr, z0 + dz
    a2, a3 = a2_deg, a3_deg
    for _ in range(iters):
        r, z = fk(a2, a3)
        er, ez = tr - r, tz - z
        if abs(er) < tol and abs(ez) < tol:
            return (a2, a3)
        j11, j12, j21, j22 = jacobian(a2, a3)
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-12:          # 특이자세 — 더 못 푼다
            return None
        da2 = (j22 * er - j12 * ez) / det
        da3 = (-j21 * er + j11 * ez) / det
        # 발산 방지: 한 번에 5도 이상 못 움직이게 제한
        m = max(abs(da2), abs(da3))
        if m > 5.0:
            da2, da3 = da2 * 5.0 / m, da3 * 5.0 / m
        a2 += da2
        a3 += da3
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        a2, a3 = float(sys.argv[1]), float(sys.argv[2])
        r, z = fk(a2, a3)
        print(f"  A2={a2:.3f}° A3={a3:.3f}°  →  수평거리 r={r*1000:.1f}mm  높이 z={z*1000:.1f}mm")
        j = jacobian(a2, a3)
        print(f"  야코비안 (mm/도):  ∂r/∂A2={j[0]*1000:+.2f}  ∂r/∂A3={j[1]*1000:+.2f}")
        print(f"                     ∂z/∂A2={j[2]*1000:+.2f}  ∂z/∂A3={j[3]*1000:+.2f}")
    else:
        print(__doc__)
