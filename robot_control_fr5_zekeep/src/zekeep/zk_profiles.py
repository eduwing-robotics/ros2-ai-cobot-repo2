#!/usr/bin/env python3
"""
zk_profiles.py - 로봇 개체별 설정 (포트·조그비트·자세DB) 단일 출처

왜 필요한가 (2026-08-08 사고)
  두 대를 동시에 연결한 뒤 `/zkbot1/home` 을 호출했더니 **2호기가 원점복귀**했다.
  zk_ros2_node 는 포트를 파라미터로 받으면서도 서브프로세스(zk_home_run.py)에는
  넘기지 않았고, 그 스크립트는 `/dev/ttyUSB0` 을 하드코딩하고 있었다.
  ttyUSB 번호는 어댑터 삽입 순서로 바뀌므로(현재 ttyUSB0=2호기),
  "1호기에게 시킨 명령이 2호기에게 갔다".

  → 포트와 조그비트를 **로봇 이름 하나로** 묶어 여기서만 정하고,
    모든 스크립트와 서브프로세스가 이 값을 받아 쓴다.

★ 포트는 by-path 물리경로로 고정한다
  ttyUSB 번호는 USB 재열거 때마다 바뀐다(하루 3번 겪음). by-path 는 메인보드의
  물리 USB 슬롯에 대응하므로 재부팅·재삽입에도 안 변한다.
  usb-0:7 = 2호기 / usb-0:4 = 1호기
    · 2026-08-08 펌프 소리로 1호기=usb-0:8 확인
    · 2026-08-10 usb-0:8 포트가 하드웨어 불량(ch341 probe -71 반복)으로 판정되어
      1호기를 usb-0:4 로 이설. 이때 ttyUSB 번호가 서로 뒤바뀌었으나(1호기=ttyUSB1,
      2호기=ttyUSB0) by-path 기준이라 코드 수정은 이 slot 값 한 줄뿐이었다.

★ 개체 식별자 D70 (보조 안전장치)
  D70 레지스터 값이 개체마다 다르다 — 1호기 11 / 2호기 10 (08-08, 08-10 동일 확인).
  포트가 또 바뀌었는데 slot 을 안 고치면 엉뚱한 로봇을 잡게 되므로, 연결 후
  verify_identity() 로 대조해 **경고**한다. 차단하지 않는 이유는 D70 의 용도가
  불명(래더가 쓰는 값일 수 있음)이라 값이 바뀔 가능성을 배제할 수 없기 때문이다.

★ 조그비트가 개체마다 다르다 (래더 버전 차이, 2026-08-08 실증)
  1호기 양수는 M101/M181 계열, 2호기 양수는 M31/M101 계열을 쓴다.
  2호기에는 M181/M183/M185 자체가 없다. 음수 비트는 두 대가 같다.
  ★ 비트를 틀리면 축이 안 움직이거나 엉뚱한 축이 움직인다 — 반드시 개체값을 쓸 것.

사용
    from zk_profiles import resolve
    prof = resolve()                      # 기본 zkbot1
    prof = resolve(robot="zkbot2")
    prof = resolve(argv=sys.argv)         # --robot / --port 인자 해석
    zk = ZK(prof.port)

환경변수로 임시 덮어쓰기 (실기 디버깅용)
    ZKBOT1_PORT / ZKBOT2_PORT
"""
from __future__ import annotations

import os
import sys

DEFAULT_ROBOT = "zkbot1"

_BY_PATH = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:{slot}:1.0-port0"

# 조그 비트: {축: (양수방향 비트들, 음수방향 비트들)}
# '양수' = 각도 카운터가 + 로 가는 쪽 (원점 0 에서 가동범위는 음의 방향이므로
#  실사용은 대부분 음수 방향이다. 양수는 원점으로 되돌아오는 쪽.)
BITS_ZKBOT1 = {
    # 1호기 HMI 실측(2026-08-06), 여섯 조합 전부 실증 완료
    "A1": ((1, 101, 181), (2, 103)),
    "A2": ((3, 105, 183), (4, 107)),
    "A3": ((5, 109, 185), (6, 111)),
}
BITS_ZKBOT2 = {
    # 2호기 HMI 실측(2026-08-08, 팀원). M181/183/185 는 2호기 래더에 없다.
    "A1": ((1, 31, 101), (2, 103)),
    "A2": ((3, 41, 105), (4, 107)),
    "A3": ((5, 51, 109), (6, 111)),
}

# 전체 해제용 — 두 개체의 비트를 모두 포함한다.
# 해제는 넓게 하는 편이 안전하다(없는 비트를 꺼도 무해).
ALL_JOG = tuple(range(1, 13)) + (31, 33, 41, 43, 51, 53) \
    + tuple(range(101, 113)) + tuple(range(181, 187))

ORDER = ("A1", "A2", "A3")


class Profile:
    """로봇 한 대의 설정. 값은 읽기 전용으로 다룰 것."""

    def __init__(self, name, slot, bits, pose_file, home_speed=100.0):
        self.name = name
        self.slot = slot
        self.bits = bits
        self.pose_file = pose_file
        # 원점복귀 D50(%) — D2002 원점속도 = D50% × D52(10000Hz). 8/26 ZK2 "덜덜" 진범:
        #   100% = 10kHz 는 가감속 100ms 인 2호기 스테퍼가 탈조(카운터만 -218° 유령주행).
        #   1호기는 8/10 가감속 300ms 라 100% 로도 버팀. 2호기는 원본값 10%(1000Hz) 사용.
        self.home_speed = home_speed
        self._port_override = os.environ.get(f"{name.upper()}_PORT")

    @property
    def port(self):
        return self._port_override or _BY_PATH.format(slot=self.slot)

    def __repr__(self):
        return f"<Profile {self.name} port={self.port}>"


PROFILES = {
    # slot: 2026-08-10 usb-0:8 하드웨어 불량으로 1호기를 usb-0:4 로 이설
    #       2026-08-19 실장이 usb-0:14 로 옮겨져 있어 slot 4→14 갱신
    #       (usb-0:8 은 현재 컨베이어 아두이노가 사용 — 불량 이력 슬롯이니 주의)
    # id_d70 개체식별은 2026-08-11 제거 — D70 은 "총 티칭 스텝수"라 티칭하면
    # 값이 바뀐다(8/10 해독). 개체 구분은 by-path slot 고정만으로 한다.
    #       2026-08-24 usb-0:9 로 재이설(사용자 재연결, ttyUSB0) — slot 14→9
    "zkbot1": Profile("zkbot1", slot=9, bits=BITS_ZKBOT1,
                      pose_file="/home/ar/zkbot_data/poses.json"),
    "zkbot2": Profile("zkbot2", slot=7, bits=BITS_ZKBOT2,
                      pose_file="/home/ar/zkbot_data/poses_zkbot2.json",
                      home_speed=10.0),     # 8/26: 100% 원점복귀 = 탈조 덜덜(실측)
}


def get(robot=DEFAULT_ROBOT):
    if robot not in PROFILES:
        raise KeyError(f"모르는 로봇 '{robot}' — 가능한 값: {list(PROFILES)}")
    return PROFILES[robot]


def resolve(argv=None, robot=None, port=None):
    """--robot / --port 를 해석해 Profile 을 돌려준다.

    우선순위: 명시 인자 > argv > 기본값.
    --port 만 준 경우 로봇 이름은 기본값이지만 포트는 준 값을 쓴다
    (기존 스크립트 호출 방식과의 하위호환).
    """
    if argv:
        if robot is None and "--robot" in argv:
            robot = argv[argv.index("--robot") + 1]
        if port is None and "--port" in argv:
            port = argv[argv.index("--port") + 1]

    prof = get(robot or DEFAULT_ROBOT)
    if port:
        # 포트만 바꾼 사본 — 원본 PROFILES 를 오염시키지 않는다
        prof = Profile(prof.name, prof.slot, prof.bits, prof.pose_file)
        prof._port_override = port
    return prof


def resolve_from_argv(argv=None, announce=True):
    """스크립트용 한 줄 헬퍼 — sys.argv 의 --robot/--port 를 해석한다.

        prof = ZPROF.resolve_from_argv()
        zk = ZK(prof.port)

    announce=True 면 어느 개체를 잡았는지 첫 줄에 찍는다. 두 대가 붙어 있을 때
    이 한 줄이 없으면 엉뚱한 로봇을 움직이고도 모른다(8/8 사고).
    ROS2 노드는 서브프로세스 stdout 의 첫 줄로 인계 여부를 판정하므로,
    이 출력은 스크립트의 다른 출력보다 앞서야 한다.
    """
    prof = resolve(argv=sys.argv if argv is None else argv)
    if announce:
        print(f"[{prof.name}] {prof.port}", flush=True)
    return prof


def describe():
    out = ["로봇 프로파일"]
    for n, p in PROFILES.items():
        exists = "✅" if os.path.exists(p.port) else "❌없음"
        out.append(f"  {n}  slot usb-0:{p.slot}  {exists}")
        out.append(f"      {p.port}")
        out.append("      비트 " + "  ".join(
            f"{j}+{'+'.join(f'M{b}' for b in f)}" for j, (f, _) in p.bits.items()))
        out.append(f"      자세DB {p.pose_file}")
    return "\n".join(out)


if __name__ == "__main__":
    print(describe())
