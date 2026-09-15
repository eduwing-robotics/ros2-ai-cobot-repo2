#!/usr/bin/env python3
"""
zk_safety.py - ZKBOT 안전 가드 (공용 모듈)

★ 어떤 움직임 명령을 내리는 스크립트든 이 가드를 통과시킬 것.
   2026-08-06에 A3가 리밋 스위치를 못 만난 채 -161°까지 밀려가
   기구 끝에 박은 사고(턱턱)의 재발 방지용이다.

── 소프트리밋의 근거 ──
제조사 매뉴얼(`PLC机器人操作说明书中文.pdf` p7 产品极限位置尺寸图)에는
치수만 있고 관절 각도 범위가 없다. 그래서 **공장 출하 티칭 포인트 11개
실측값**을 근거로 삼는다. 이 값들은 공장이 실제로 가르쳐 놓은 자세이므로
"도달 가능함이 증명된" 각도다.

    A1 0 ~ -150°   A2 0 ~ -50°   A3 0 ~ -80°   (D100/D200/D300 실측)

여기에 여유를 얹어 소프트리밋을 잡았다. 좁게 잡아 일찍 멈추는 것은
안전하지만 넓게 잡으면 기구를 때린다 → 의심스러우면 좁게.
실측으로 진짜 가동범위를 확인한 뒤에만 넓힐 것.

── 스케일 (사양서 + 실측 일치) ──
스텝각 1.8° → 6400펄스/회전(모터축), 감속비 10 → 64000펄스/출력축 회전
  = 177.78 펄스/도.  실측(D1000↔D1010 등 3축 교차검증)과 정확히 일치.
"""

PULSES_PER_DEG = 177.78

# (최소각, 최대각) — 원점(리밋)이 0이고 가동범위는 음의 방향이다
#
# 2026-08-06 저녁 갱신: 티칭 11포인트는 전체 가동범위를 다 훑지 않았다.
#   사용자가 원점복귀 후 실제로 도달한 픽 자세가 A1 −180.47° / A2 −42.14° / A3 −101.12°
#   로, 기존 추정치(A1 −155 / A3 −85)를 넘었는데도 아무 문제가 없었다.
#   → 실측 도달점 + 여유로 넓힌다. 추정보다 실측이 우선이다.
JOINT_LIMITS = {
    "A1": (-190.0, 2.0),   # 실측 도달 -180.47°
    "A2": (-55.0, 2.0),    # 실측 -42.14°, 여유 충분해 유지
    "A3": (-110.0, 2.0),   # 실측 도달 -101.12°
}

# 원점복귀 중 한 축이 이만큼 움직였는데도 리밋을 못 만나면
# 리밋 스위치 고장으로 보고 중단한다 (가동범위 + 여유)
MAX_HOMING_TRAVEL_DEG = {"A1": 170.0, "A2": 70.0, "A3": 95.0}

# ★ 이동거리 상한만으로는 2026-08-06 사고를 못 막는다.
#   A3는 27.6°만 움직이고 기구를 때렸다 — 상한(95°)에 한참 못 미친다.
#   진짜 위험 신호는 "시작 시점에 이미 카운터가 검증범위를 48° 벗어나 있었다"는 것이다.
#   카운터가 그만큼 틀어졌다는 건 이미 탈조가 누적됐다는 뜻이고,
#   그 상태로 원점복귀를 돌리면 팔이 어디 있는지 모르는 채로 미는 것이 된다.
#   → 시작 전에 이만큼 벗어나 있으면 거부한다. 사람이 눈으로 확인하고 force로 풀 것.
MAX_START_EXCESS_DEG = 20.0

JOINT_D = {"A1": 1010, "A2": 1030, "A3": 1050}   # 각도 float32 주소
HOME_FLAG = {"A1": 121, "A2": 122, "A3": 123}    # "원점에 있음" M비트
LIMIT_X = {"A1": 0, "A2": 1, "A3": 2}            # 리밋 스위치 입력


class Abort(Exception):
    """안전 조건 위반 — 호출부는 즉시 모든 동작 비트를 끌 것."""


class Guard:
    """움직임 명령 전/중에 계속 호출하는 가드.

    homing=True 면 소프트리밋 대신 '이동거리 상한'으로 판정한다.
    원점을 아직 못 잡은 축은 카운터가 실제 위치와 다르므로
    절대각으로 판정하면 안 되기 때문이다.
    """

    def __init__(self, zk, homing=False, force=False):
        self.zk = zk
        self.homing = homing
        self.force = force
        self.start = {}
        self.homed = set()

    # ---------- 시작 전 점검 ----------
    def preflight(self, angles):
        xs = self.zk.x()
        if xs is None:
            raise Abort("입력 읽기 실패 — 통신 확인")
        if 5 in xs:
            raise Abort("X5(급정지)가 눌려 있음")

        good = sum(1 for _ in range(10) if self._d8001_ok())
        if good < 9:
            raise Abort(f"링크 불안정 (D8001 {good}/10) — 커넥터 확인")

        self.start = dict(angles)
        ms = self.zk.m(32) or set()
        self.homed = {j for j, b in HOME_FLAG.items() if b in ms}

        warn, severe = [], []
        for j, v in angles.items():
            if v is None:
                continue
            lo, hi = JOINT_LIMITS[j]
            if not (lo <= v <= hi):
                excess = max(lo - v, v - hi)
                warn.append(f"{j}={v:.1f}° (허용 {lo}~{hi}, {excess:.1f}° 초과)")
                if excess > MAX_START_EXCESS_DEG:
                    severe.append(f"{j} {excess:.1f}° 초과")

        if severe and not self.force:
            raise Abort(
                "시작 거부 — 카운터가 검증범위를 크게 벗어남: " + ", ".join(severe) +
                f"\n  (허용 초과 {MAX_START_EXCESS_DEG}°). 이 상태로 원점복귀를 돌리면"
                "\n   팔의 실제 위치를 모르는 채 미는 것이 되어 기구를 때린다."
                "\n  → 해당 축 리밋 스위치가 살아있는지 먼저 확인하고,"
                "\n    팔을 눈으로 보며 수동 조그로 리밋 근처까지 데려온 뒤 재시도."
                "\n    그래도 강행하려면 force=True.")

        return {"homed": sorted(self.homed), "out_of_range": warn,
                "limits_on": sorted(i for i in (0, 1, 2) if i in xs)}

    def _d8001_ok(self):
        b = self.zk.read(0x0E00 + 2, 2)
        return b is not None and int.from_bytes(b, "little") == 24320

    # ---------- 주기 점검 ----------
    def check(self, angles):
        xs = self.zk.x()
        if xs is None:
            raise Abort("입력 읽기 실패 — 통신 두절")
        if 5 in xs:
            raise Abort("X5(급정지) 감지")

        # ★ 원점을 이미 잡은 축은 이동거리 판정에서 제외한다.
        #   DZRN은 원점복귀에 성공하는 순간 카운터를 0으로 리셋하는데,
        #   그 점프를 "이동"으로 세면 오판으로 중단시킨다.
        #   (2026-08-06 실제로 A3가 -436.75→0.00 리셋된 것을 436.8° 이동으로
        #    잘못 읽고 중단시켰다. 리셋은 성공의 증거지 위험 신호가 아니다.)
        done = set()
        if self.homing:
            ms = self.zk.m(32) or set()
            done = {j for j, b in HOME_FLAG.items() if b in ms}

        for j, v in angles.items():
            if v is None or j in done:
                continue

            if self.homing:
                # 원점 미확보 축은 절대각을 믿을 수 없다 → 이동거리로 본다
                s = self.start.get(j)
                if s is None:
                    continue
                travel = abs(v - s)
                cap = MAX_HOMING_TRAVEL_DEG[j]
                if travel > cap and LIMIT_X[j] not in xs:
                    raise Abort(
                        f"{j}가 {travel:.1f}° 움직였는데 리밋(X{LIMIT_X[j]})을 "
                        f"못 만남 (상한 {cap}°) — 리밋 스위치 고장 또는 역방향 의심")
            else:
                lo, hi = JOINT_LIMITS[j]
                if not (lo <= v <= hi):
                    raise Abort(f"{j}={v:.1f}° 소프트리밋 이탈 (허용 {lo}~{hi})")
        return True


def deg_to_pulse(deg):
    return int(round(deg * PULSES_PER_DEG))


def pulse_to_deg(p):
    return p / PULSES_PER_DEG


def clamp(joint, deg):
    lo, hi = JOINT_LIMITS[joint]
    return max(lo, min(hi, deg))


def describe():
    out = ["소프트리밋 (근거: 공장 티칭 포인트 실측 + 여유)"]
    for j, (lo, hi) in JOINT_LIMITS.items():
        out.append(f"  {j}: {lo:+7.1f}° ~ {hi:+5.1f}°   "
                   f"({deg_to_pulse(lo):+7d} ~ {deg_to_pulse(hi):+5d} 펄스)")
    out.append(f"스케일: {PULSES_PER_DEG} 펄스/도 (= 6400펄스/회전 × 감속비10 ÷ 360)")
    out.append("원점복귀 이동거리 상한: " +
               ", ".join(f"{j} {v}°" for j, v in MAX_HOMING_TRAVEL_DEG.items()))
    return "\n".join(out)


if __name__ == "__main__":
    print(describe())
