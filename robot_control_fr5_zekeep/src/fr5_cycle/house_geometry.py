#!/usr/bin/env python3
"""HOUSE 기하 + 목표 TCP 계산 (로봇 의존성 없음, 2026-09-04).

재설계의 3층 좌표계 [로봇]-[베이스 프레임]-[슬롯] / [그리퍼]-[벽 아래변 선] 을 순수 계산으로 구현한다.
수치 근거: cad/base/B_printbase.stl (tools/measure_house_stl.py 로 재현).

용어
- 베이스 프레임 B: 밑판 최소 모서리가 원점, 긴 변이 +x(210), 짧은 변이 +y(140), 위가 +z. 단위 mm.
- 로봇 프레임 R: FR5 베이스 좌표. 여기서는 2D(x, y, yaw)만 다룬다. z 는 별도(호버/하강 프로파일).
- 그리퍼 프레임 G: TCP 원점, TCP yaw 방향이 +x.

검증: python3 tools/test_house_geometry.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------- STL 실측 상수
PLATE_X, PLATE_Y, PLATE_T = 210.0, 140.0, 6.0        # 밑판
COLUMN_W, COLUMN_H = 10.0, 80.0                        # 모서리 기둥 10x10, z 6→86
LEG_H = 47.0                                           # 다리 → 기둥 꼭대기 = 책상 위 133
COLUMN_TOP_Z = LEG_H + PLATE_T + COLUMN_H              # 133.0
PLATE_TOP_Z = LEG_H + PLATE_T                          # 53.0
PANEL_T = 4.22                                         # 외벽 판 두께
WALL_H = 80.0                                          # 안착 시 윗변 = 기둥 꼭대기

# 기둥 중심 (베이스 프레임). 4점 직사각형 200 x 130.
COLUMN_CENTERS = ((5.0, 5.0), (205.0, 5.0), (205.0, 135.0), (5.0, 135.0))


@dataclass(frozen=True)
class Slot:
    name: str
    center: tuple[float, float]   # 벽 판 중심선의 중점 (B)
    angle_deg: float              # 벽 길이 방향 (B), 0 = +x, 90 = +y
    length: float                 # 벽 길이
    play_thickness: float         # 두께 방향 총 유격 (mm)
    play_length: float            # 길이 방향 총 유격 (mm)

    @property
    def yaw_tol_deg(self) -> float:
        """벽 중심 기준 회전 허용치. 양 끝이 두께방향 유격 안에 들어야 함."""
        return math.degrees(self.play_thickness / self.length)


# 긴벽(198): 쐐기 끝 + 삼각 노치(입구 5, 깊이 5). 공칭 4 mm 삽입 시 한쪽 유격 ≈ 1.0.
# 짧은벽(125): 직사각 노치(폭 5, 깊이 3). 5.0 - 4.22 = 0.78.
SLOTS = {
    "LONG_Y0":   Slot("LONG_Y0",   (105.0, 5.0),   0.0, 198.0, 1.0,  2.0),
    "LONG_Y140": Slot("LONG_Y140", (105.0, 135.0), 0.0, 198.0, 1.0,  2.0),
    "SHORT_X0":  Slot("SHORT_X0",  (4.5, 70.0),   90.0, 125.0, 0.78, 1.0),
    "SHORT_X210": Slot("SHORT_X210", (205.5, 70.0), 90.0, 125.0, 0.78, 1.0),
}


# ---------------------------------------------------------------- 2D 강체 변환
@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    yaw_deg: float

    def apply(self, p: tuple[float, float]) -> tuple[float, float]:
        c, s = math.cos(math.radians(self.yaw_deg)), math.sin(math.radians(self.yaw_deg))
        return (self.x + c * p[0] - s * p[1], self.y + s * p[0] + c * p[1])


def wrap_deg(a: float) -> float:
    """(-180, 180] 로 정규화."""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


# ---------------------------------------------------------------- 베이스 프레임 추정
def fit_base_pose(measured_R: list[tuple[float, float]],
                  model_B: tuple[tuple[float, float], ...] = COLUMN_CENTERS) -> tuple[Pose2D, float]:
    """모델점(B)과 측정점(R)의 대응(같은 순서)으로 2D 강체 변환 T_RB 를 최소제곱 추정.

    반환: (T_RB, rms 잔차 mm). 점은 3개 이상, 순서가 맞아야 한다(기둥 4개는 시계/반시계 순서를 맞출 것).
    """
    n = len(measured_R)
    assert n >= 3 and n == len(model_B)
    mx = sum(p[0] for p in model_B) / n
    my = sum(p[1] for p in model_B) / n
    rx = sum(p[0] for p in measured_R) / n
    ry = sum(p[1] for p in measured_R) / n
    sxx = sxy = syx = syy = 0.0
    for (bx, by), (qx, qy) in zip(model_B, measured_R):
        ax, ay, cx, cy = bx - mx, by - my, qx - rx, qy - ry
        sxx += ax * cx; sxy += ax * cy; syx += ay * cx; syy += ay * cy
    yaw = math.atan2(sxy - syx, sxx + syy)
    c, s = math.cos(yaw), math.sin(yaw)
    tx = rx - (c * mx - s * my)
    ty = ry - (s * mx + c * my)
    pose = Pose2D(tx, ty, math.degrees(yaw))
    res = 0.0
    for b, q in zip(model_B, measured_R):
        px, py = pose.apply(b)
        res += (px - q[0]) ** 2 + (py - q[1]) ** 2
    return pose, math.sqrt(res / n)


# ---------------------------------------------------------------- 목표 TCP
@dataclass(frozen=True)
class GripMeasure:
    """파지 후 측정: 그리퍼 프레임에서 본 벽 아래변 선."""
    center: tuple[float, float]   # 아래변 중점 (G)
    angle_deg: float              # 아래변 방향 (G), 그리퍼 +x 기준
    bottom_dz: float = 0.0        # TCP 대비 벽 아래변 z 오프셋(음수 = 아래). 하강 목표에 사용


def target_tcp(T_RB: Pose2D, slot: Slot, grip: GripMeasure, current_rz_deg: float) -> tuple[Pose2D, float]:
    """벽 아래변 선을 슬롯 중심선에 일치시키는 TCP (x, y, rz) 와 필요한 J6 증분.

    rz* = θ_B + φ_slot − φ_grip
    xy* = T_RB·p_slot − R(rz*)·p_grip     (회전 중심이 TCP 라 |p_grip|·Δθ 만큼의 이동을 포함)
    ΔJ6 = wrap(rz* − rz_now)              (항상 최소 각도, |ΔJ6| ≤ 180)
    """
    rz = wrap_deg(T_RB.yaw_deg + slot.angle_deg - grip.angle_deg)
    sx, sy = T_RB.apply(slot.center)
    c, s = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    gx = c * grip.center[0] - s * grip.center[1]
    gy = s * grip.center[0] + c * grip.center[1]
    return Pose2D(sx - gx, sy - gy, rz), wrap_deg(rz - current_rz_deg)


def wall_line_in_R(tcp: Pose2D, grip: GripMeasure) -> tuple[tuple[float, float], float]:
    """검증용: 이 TCP 에서 벽 아래변 중점과 방향이 로봇 프레임에서 어디인가."""
    return tcp.apply(grip.center), wrap_deg(tcp.yaw_deg + grip.angle_deg)


def alignment_ok(err_end_a: float, err_end_b: float, err_yaw_deg: float, slot: Slot,
                 margin: float = 0.6) -> bool:
    """호버 서보 종료 판정. 허용치의 margin 배 안이어야 함 (기본 60%)."""
    lim = slot.play_thickness / 2 * margin
    return abs(err_end_a) <= lim and abs(err_end_b) <= lim and abs(err_yaw_deg) <= slot.yaw_tol_deg * margin


if __name__ == "__main__":
    for s in SLOTS.values():
        print(f"{s.name:11s} center={s.center} angle={s.angle_deg:>4} L={s.length} "
              f"두께유격 {s.play_thickness} → yaw 허용 {s.yaw_tol_deg:.2f}°")
