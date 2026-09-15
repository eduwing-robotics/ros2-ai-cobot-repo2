#!/usr/bin/env python3
"""
cell_orchestrator.py - D1 셀 Task 실행기 (계약서 v0.2 + v0.3 Final 합의)

    source /opt/ros/jazzy/setup.bash
    source ~/fr5_jazzy_test_ws/install/setup.bash        # cell_interfaces
    python3 ~/cell_orchestrator.py                       # 기본 exec_mode:=sim
    python3 ~/cell_orchestrator.py --ros-args -p exec_mode:=sim -p sim_phase_sec:=0.3

역할 (8/8 FMS 역할분담 개정 반영):
    S1~S9 최상위 공정 FSM 은 FMS 소유. 이 노드는 Task "1건"을 받아
    로봇 하위 시퀀스를 돌리고 SUCCEEDED/FAILED/CANCELED 만 반환한다.
    FMS="무엇을·언제" / 셀="어떻게".

인터페이스 (전부 계약서 v0.2 스키마)
    Action  /cell/execute_task   cell_interfaces/ExecuteTask
    Service /cell/control        cell_interfaces/CellControl  PAUSE|RESUME|ABORT|RESET
    Topic   /cell/status         std_msgs/String(JSON) 1Hz 고정 재발행 = 하트비트
    Topic   /cell/event          std_msgs/String(JSON) 비동기 (STEP_DONE 등)

★ 설계 원칙 (이전 프로젝트 실증 패턴 재사용)
  - 상태 전이는 change_state() 한 곳에서만 (7/13 ESTOP 래치 초크포인트 패턴)
  - PAUSE 는 즉시 ACK 후 "현재 안전 단위(phase) 완료" 시점에 HELD 진입 — 파지물 낙하 방지
  - HELD 에서 자동 재개 금지 (7/9 교훈) — RESUME 명시 요청만
  - 완료 판정은 Action Result 단독. status 의 progress 는 표시용
  - 멱등: 같은 req_id 재수신 = 재실행 금지. 완료건은 저장된 Result 반환,
    진행 중이면 그 실행에 붙어 같은 Result 를 반환
  - ABORT/Cancel = status "CANCELED" / 장비·비전 오류 = "FAILED" (서버 검토 반영)
  - 오류 후 자동 재개 금지 — RESET/RESUME 는 관리자(FMS) 요청만

★ exec_mode
  sim  : 로봇을 전혀 건드리지 않고 phase 를 시간 시뮬레이션. FMS·트윈 연동 개발용.
         inject_error 파라미터로 오류 시나리오 재현 가능 (D5):
           ros2 param set /cell_orchestrator inject_error "E201@SUCTION"
  real : ZK = zk_ros2_node(zkbot1/zkbot2) 서비스·액션 경유 — 실물검증 완료(8/11~12).
         FR5 = MoveIt2 /move_action + 그리퍼는 개조 hardware plugin 이 서빙하는
         /fairino_remote_command_service (플랜B, fr5_real_adapter.py 도크스트링 참조.
         8080 직결은 8/13 probe 실패로 폐기). 8/13 구현·★실기 미검증 —
         자세 티칭·그리퍼 서비스 확인 후 단계 게이트로 검증할 것.
         FR5 는 개통 순서(cmd_server→ActGripper→Mode(0)→RobotEnable→real_robot) 선행 필수.
         fr5_gripper_mode: service(기본) | off(건주행: 그리퍼 스킵) | rpc(참고용 폐기 경로).

로봇 상태 감시 (status.robots.*.connected)
  /fr5/joint_states · /zkbot1/joint_states · /zkbot2/joint_states 구독,
  5초 무수신 = connected:false (계약 §4 두절 기준과 동일값).
"""
import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import String

from cell_interfaces.action import ExecuteTask
from cell_interfaces.srv import CellControl, GetPickPose

VER = "0.3"

# 셀 담당 task_type 7종 → (담당 로봇, phase 시퀀스)  — 계약 §2 / FSM 설계 §2.2
TASK_TABLE = {
    "MATERIAL_FEED":      ("zk1", ["MOVE", "SUCTION", "MOVE", "RELEASE", "MOVE"]),
    "INSTALL_FLOOR":      ("zk2", ["MOVE", "SUCTION", "MOVE", "RELEASE", "MOVE"]),
    "INSTALL_FURNITURE":  ("zk2", ["MOVE", "SUCTION", "MOVE", "RELEASE", "MOVE"]),
    "INSTALL_ROOF":       ("zk2", ["MOVE", "SUCTION", "MOVE", "RELEASE", "MOVE"]),
    "INSTALL_INNER_WALL": ("fr5", ["OBSERVE", "PICK", "REORIENT",
                                   "PLACE_APPROACH", "INSERT", "RETREAT"]),
    "INSTALL_OUTER_WALL": ("fr5", ["OBSERVE", "PICK", "REORIENT",
                                   "PLACE_APPROACH", "INSERT", "RETREAT"]),
    "INSTALL_WINDOW_DOOR": ("fr5", ["OBSERVE", "PICK", "REORIENT",
                                    "PLACE_APPROACH", "INSERT", "RETREAT"]),
}

# ★v0.3 (2026-09-09 서버팀 합의): PAUSE ACK 은 "어떻게 세울 것인가"만 말한다.
#   실제 정지 완료의 authority 는 /cell/status 의 cell_state == "HELD".
#   실제로 멈춘 지점(held_at)은 서비스 응답이 아니라 /cell/status 의 hold 블록에 싣는다.
STOP_MODES = ("IMMEDIATE", "AT_PHASE_BOUNDARY", "DEFERRED_UNSAFE", "NOT_APPLICABLE")
HELD_AT = ("PHASE_BOUNDARY", "STEP_BOUNDARY", "HOVER", "OBSERVE", "MID_MOTION")

# phase 경계까지의 **최악** 예상 지연(ms) — ACK 의 eta_ms 산출용. UX 문구 분기에만 쓰인다.
#   근거: 그리퍼 풀스트로크 11초 실측(8/14) · 삽입은 3mm 스텝이라 스텝 경계까지 ≤1초(9/5~).
PHASE_ETA_MS = {
    "MOVE": 8000, "OBSERVE": 2000, "PICK": 12000, "REORIENT": 6000,
    "PLACE_APPROACH": 8000, "INSERT": 1000, "RETREAT": 6000,
    "SUCTION": 5000, "RELEASE": 5000,
}
PHASE_ETA_DEFAULT = 10000

CELL_STATES = ("IDLE", "EXECUTE", "HELD", "ABORTED", "FAULT")

# 오류코드 → 셀 상태 전이 (계약 §6). 표에 없는 코드는 FAULT.
ERROR_STATE = {
    "E101": "HELD", "E102": "HELD", "E103": "ABORTED",
    "E201": "HELD", "E202": "ABORTED", "E203": "ABORTED", "E204": "ABORTED",
    "E301": "HELD", "E401": "ABORTED",
    "E501": "ABORTED", "E502": "ABORTED", "E503": "ABORTED",
    "E504": "ABORTED", "E505": "ABORTED", "E506": "ABORTED",
}


class PauseInterrupt(Exception):
    """★v0.3 C5: **PAUSE 로 인해** 진행 중인 모션이 끊겼다는 신호. 오류가 아니다.

    서버팀 요청(2026-09-09 V2): FR5 즉시정지의 MoveGroup 골 취소가 /cell/execute_task 의
    CANCELED/FAILED 로 전파되면 안 된다. 그래서 PAUSE 로 인한 중단은 CellError 와 **다른 예외**로
    올려서, Task 종료 판정(_finish)을 아예 타지 않고 HELD 루프로만 들어가게 한다.
    ABORT·장비오류와 경로를 공유하면 정확히 그 사고가 난다."""

    def __init__(self, phase, where):
        super().__init__(f"PAUSE during {phase} ({where})")
        self.phase, self.where = phase, where


class CellError(Exception):
    """phase 실행 실패. code=계약 §6, detail=사람이 읽는 설명."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class TaskRun:
    """실행 1건의 공유 상태. 멱등 재수신 골이 이 객체에 붙는다."""

    def __init__(self, goal):
        self.goal = goal
        self.req_id = goal.req_id
        self.done = threading.Event()
        self.result = None            # ExecuteTask.Result — done 세트 후에만 유효
        # 진행 상황 (status/feedback 공용)
        self.phase = ""
        self.current_item = 0
        self.total_items = 0
        self.progress = 0.0
        self.robot = TASK_TABLE.get(goal.task_type, ("", []))[0]
        self.completed = []           # 완료 슬롯
        # ★v0.3 C7: 지금 흡착으로 부품을 물고 있는가. SUCTION 성공 → True, RELEASE 성공 → False.
        #   진공에는 피드백이 없어서(E201 검출 불가) 이 플래그가 유일한 근거다 —
        #   "명령을 보냈다"는 사실일 뿐 실제로 붙어 있다는 보장이 아니라는 점을 잊지 말 것.
        self.holding_suction = False
        # ★v0.3 C9: FR5 가 부품(벽)을 물고 있는가. PICK 성공 → True, RETREAT 완료 → False.
        self.holding_part = False
        # ★v0.3 C9: 이번 phase 실행이 "일시정지에서 재개된 것"인가. 어댑터가 이 값을 보고
        #   벽을 든 채면 정렬 높이로 되올라가 다시 측정한 뒤 내려간다(계약 §4-1).
        #   한 번 소비되면 어댑터가 False 로 되돌린다.
        self.resume_from_hold = False


class SimAdapter:
    """로봇을 건드리지 않는 시간 시뮬레이션. inject_error 로 오류 재현."""

    def __init__(self, node):
        self.node = node

    def run_phase(self, robot, phase, part, run):
        if run.resume_from_hold:
            # ★C9: 실기 FR5 는 여기서 '정렬 높이로 되올라가 재측정' 을 먼저 한다(계약 §4-1).
            #   sim 은 그 사실만 남긴다. 한 번 쓰면 소비한다.
            run.resume_from_hold = False
            self.node.get_logger().info(
                f"[sim] 일시정지 재개 — {phase} 를 처음부터 다시 실행"
                + (" (부품 든 채 → 실기라면 정렬 높이 복귀 후 재측정)"
                   if (run.holding_part or run.holding_suction) else ""))
        spec = self.node.get_parameter("inject_error").value
        if spec:
            code, _, at = spec.partition("@")
            if not at or at == phase:
                # 1회성 — 재시도(RESUME 후 재실행)에서 또 터지지 않게 소거
                self.node.set_parameters(
                    [rclpy.parameter.Parameter("inject_error", value="")])
                raise CellError(code, f"sim 주입 오류 ({phase}, part={part})")
        # ★v0.3 C5: 한 번에 자지 않고 잘게 쪼개 pause_now 를 본다 — 실기의 '3mm 스텝 경계' 에 대응.
        #   실기 RealAdapter 도 같은 규칙이다(스텝 사이에서만 끊는다 → 벽이 죠에서 밀리지 않는다).
        total = float(self.node.get_parameter("sim_phase_sec").value)
        slice_s = 0.05
        done = 0.0
        while done < total:
            if self.node.pause_now.is_set():
                where = "STEP_BOUNDARY" if phase == "INSERT" else "MID_MOTION"
                raise PauseInterrupt(phase, where)
            time.sleep(min(slice_s, total - done))
            done += slice_s

    def stop(self, robot, reason="ABORT"):
        # sim 은 실제로 세울 게 없다. 사유만 기록해 둔다(로그 대조용).
        self.node.last_stop_reason = reason

    def verify_held(self, robot, run):
        """sim: inject_error 로 '재개했더니 부품이 없더라' 를 재현할 수 있게만 해 둔다."""
        spec = self.node.get_parameter("inject_error").value
        if spec.startswith("E201@RESUME"):
            self.node.set_parameters([rclpy.parameter.Parameter("inject_error", value="")])
            return False, "sim 주입: 재개 시 부품 없음"
        if spec.startswith("UNVERIFIABLE@RESUME"):
            # ★실기 ZK 진공처럼 '검증 수단 자체가 없는' 경우를 sim 에서도 돌려보기 위한 주입.
            self.node.set_parameters([rclpy.parameter.Parameter("inject_error", value="")])
            return None, "sim 주입: 검증 수단 없음"
        return True, "sim"


class RealAdapter:
    """★ZK = 실물검증 완료(8/11~12). FR5 = 8/13 구현·실기 미검증 (fr5_real_adapter 위임).

    선행 조건:
      ZK  : 로봇별 zk_ros2_node 기동
        python3 ~/zk_ros2_node.py --ros-args -p robot:=zkbot1   (zk2 도 동일)
      FR5 : real_robot.launch.py (개통 순서 cmd_server→ActGripper→정상종료→real_robot)
        관절 = MoveGroup /move_action, 그리퍼 = 개조 FairinoHardwareInterface 가
        서빙하는 /fairino_remote_command_service (플랜B — 8080 직결은 probe 실패 폐기.
        규명 근거·검증 절차 = fr5_real_adapter.py 도크스트링).
        fr5_gripper_mode:=off 로 그리퍼 스킵(건주행 게이트), 기본 service.
        자세 6종(observe/pick_hover/pick/reorient/place_hover/insert)은
        fr5_pose.py save 로 티칭 필요 — 현재 미티칭.
    MOVE 는 티칭 자세 시퀀스로 실행 (부품 슬롯별 자세는 배치 확정 후 확장):
        MOVE#1 base_above→base_pick→/refine_pick 다듬
        MOVE#2 /carry(정밀 운반)→/refine_place 다듬  ← RELEASE 이전
        MOVE#3 합성 상승(A1 place·A2/A3 lift)→base_above
    실물 검증 전에는 exec_mode:=sim 으로만 운용할 것 (운영 원칙:
    실기 검증은 사용자 몫 — 코드가 됐다고 실물이 된다고 단정하지 않는다).
    """

    ZK_NS = {"zk1": "zkbot1", "zk2": "zkbot2"}
    MOVE_SEQ = {1: ("base_above", "base_pick"),      # 접근 → 집기 자세
                2: ("base_lift", "base_place"),      # 들기 → 놓기 자세
                3: ("base_above",)}                  # 복귀

    def __init__(self, node):
        self.node = node
        from std_srvs.srv import Trigger
        from rclpy.action import ActionClient
        from control_msgs.action import FollowJointTrajectory
        import sys as _sys
        if "/home/ar" not in _sys.path:
            _sys.path.insert(0, "/home/ar")
        import zk_profiles as ZPROF
        from fr5_real_adapter import Fr5RealAdapter
        self.fr5 = Fr5RealAdapter(node, cell_error=CellError)
        self._fjt = FollowJointTrajectory
        self.cli = {}
        self.traj = {}
        self.poses = {}
        for r, ns in self.ZK_NS.items():
            self.cli[r] = {
                s: node.create_client(Trigger, f"/{ns}/{s}",
                                      callback_group=node.adapter_cb)
                for s in ("home", "pump_on", "pump_off", "stop", "carry",
                          "refine_pick", "refine_place")}
            self.traj[r] = ActionClient(
                node, FollowJointTrajectory,
                f"/{ns}/zkbot_arm_controller/follow_joint_trajectory",
                callback_group=node.adapter_cb)
            try:                                   # 자세 DB = 프로파일 소유 경로
                with open(ZPROF.get(ns).pose_file) as f:
                    self.poses[r] = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self.poses[r] = {}                 # 미티칭 → _goto 에서 E202
        self._move_key = None                      # (req_id, item) — MOVE 몇 번째인지
        self._move_no = 0

    def _call(self, robot, srv, timeout=120.0):
        cli = self.cli[robot][srv]
        if not cli.wait_for_service(timeout_sec=3.0):
            raise CellError("E103" if robot == "fr5" else "E202",
                            f"{robot} {srv} 서비스 없음 (노드 미기동?)")
        fut = cli.call_async(cli.srv_type.Request())
        t0 = time.monotonic()
        while not fut.done():
            if time.monotonic() - t0 > timeout:
                raise CellError("E202", f"{robot} {srv} 응답 타임아웃")
            time.sleep(0.1)
        res = fut.result()
        if not res.success:
            raise CellError("E201" if "pump" in srv else "E202",
                            f"{robot} {srv} 실패: {res.message}")

    def _goto(self, robot, pose_name, timeout=150.0):
        """티칭 자세(이름) 또는 각도 dict 로 이동 (FJT 단일 웨이포인트)."""
        if isinstance(pose_name, dict):
            db = {"<합성자세>": pose_name}
            pose_name = "<합성자세>"
        else:
            db = self.poses.get(robot, {})
        if pose_name not in db:
            raise CellError("E202", f"{robot} 티칭 자세 '{pose_name}' 없음 — "
                                    f"zk_pose.py save 로 티칭 후 재시도")
        from trajectory_msgs.msg import JointTrajectoryPoint
        from builtin_interfaces.msg import Duration
        ac = self.traj[robot]
        if not ac.wait_for_server(timeout_sec=3.0):
            raise CellError("E202", f"{robot} 궤적 액션 없음 (zk_ros2_node 미기동?)")
        goal = self._fjt.Goal()
        goal.trajectory.joint_names = ["a1_joint", "a2_joint", "a3_joint"]
        pt = JointTrajectoryPoint()
        pt.positions = [math.radians(float(db[pose_name][k]))
                        for k in ("A1", "A2", "A3")]
        pt.time_from_start = Duration(sec=0)
        goal.trajectory.points = [pt]
        fut = ac.send_goal_async(goal)
        t0 = time.monotonic()
        while not fut.done():
            if time.monotonic() - t0 > 5.0:
                raise CellError("E202", f"{robot} 궤적 골 접수 타임아웃")
            time.sleep(0.05)
        gh = fut.result()
        if not gh.accepted:
            raise CellError("E202", f"{robot} 궤적 골 거부")
        rfut = gh.get_result_async()
        while not rfut.done():
            if time.monotonic() - t0 > timeout:
                gh.cancel_goal_async()
                raise CellError("E202", f"{robot} '{pose_name}' 이동 타임아웃 {timeout}s")
            time.sleep(0.1)
        res = rfut.result().result
        if res.error_code != 0:
            raise CellError("E202", f"{robot} '{pose_name}' 이동 실패 "
                                    f"code={res.error_code} {res.error_string}")

    def verify_held(self, robot, run):
        """★재개 전 '아직 물고 있는가' 확인. (ok, why) — ok=None 이면 **검증 수단이 없다**는 뜻이고,
        호출자는 그것을 통과로 취급하지 않고 기록만 한다(모르는 것을 안다고 하지 않는다).

        FR5 : 손목캠으로 든 벽의 색점을 본다. 실제 판정기는 bf2_console/place_calc 의
              held_wall_dots 계열(9/7~9/8 실기로 다듬은 것)이고, 여기서는 그 결과를
              위임받는다. 아직 배선 전이라 None 을 돌려준다 — ★C9 남은 작업.
        ZK  : 진공에 피드백이 없다(계약 §6 E201 을 셀이 검출 불가, 8/11 기록).
              카메라로 흡착판 아래 부품을 보는 수단이 아직 없어 원리적으로 None 이다.
              근본 해결은 진공 압력 스위치 추가."""
        if robot == "fr5":
            fn = getattr(self.fr5, "verify_held", None)
            if fn is None:
                return None, "FR5 어댑터에 verify_held 미배선 (place_calc.held_wall_dots 위임 예정)"
            return fn(run)
        return None, "ZK 진공은 피드백이 없다 — 압력 스위치 추가 전까지 검증 불가"

    def run_phase(self, robot, phase, part, run):
        if robot == "fr5":
            return self.fr5.run_phase(phase, part, run)
        if phase == "SUCTION":
            self._call(robot, "pump_on")
        elif phase == "RELEASE":
            self._call(robot, "pump_off")
        elif phase == "MOVE":
            key = (run.req_id, run.current_item)   # 부품이 바뀌면 MOVE 카운트 리셋
            if key != self._move_key:
                self._move_key, self._move_no = key, 0
            self._move_no += 1
            if self._move_no == 2:
                # ★부품 파지 구간 = 정밀 운반 (zk_carry: 2단계 스텝·A2/A3 교대,
                #   8/10 실측 0.55mm·밀림 최소 기법). 저속이라 타임아웃 김.
                self._call(robot, "carry", timeout=600.0)
                # ★carry 직후 place 다듬 (8/12 실물): carry 자체 종료 잔차가
                #   A2/A3 +0.35~0.45° 로 남아(마지막 0.5° 스텝도 5% 관성에 걸림)
                #   가이드에 안 들어간다. 부품 문 채 다듬는 것이 삽입 성공의
                #   결정적 요인이었다 — RELEASE(pump_off) 이전에 수행해야 한다.
                self._call(robot, "refine_place", timeout=300.0)
                return
            if self._move_no == 3:
                # ★복귀 전 상승 — 노드 축순서(A1 먼저)로는 place 높이에서 회전해
                #   방금 놓은 부품을 낮은 스윕으로 칠 수 있다("먼저 들고 돌기" 원칙).
                #   A1 은 그대로 두고 A2/A3 만 들기 높이로 올린 합성 자세를 경유.
                db = self.poses.get(robot, {})
                if "base_lift" in db and "base_place" in db:
                    self._goto(robot, {"A1": db["base_place"]["A1"],
                                       "A2": db["base_lift"]["A2"],
                                       "A3": db["base_lift"]["A3"]})
                self._goto(robot, "base_above")
                return
            seq = self.MOVE_SEQ.get(self._move_no)
            if seq is None:
                raise CellError("E202", f"MOVE 시퀀스 초과({self._move_no})")
            for pose in seq:
                self._goto(robot, pose)
            if self._move_no == 1:
                # ★pick 정밀 다듬 (8/11 발견·8/12 실물 확립): FJT tol 0.5°
                #   도착으로는 흡착판이 부품 위 수 mm 떠서 헛흡착 — 진공 피드백이
                #   없어 SUCCEEDED 로 완주해버린다. 노드 /refine_pick 이
                #   poses.json base_pick 값으로 A3→A2→A1 을 tol 0.15° 다듬는다
                #   (A1 포함 이유 = 집기 A1 ±0.4° 산포가 놓기 yaw 랜덤의 원인).
                self._call(robot, "refine_pick", timeout=300.0)
        else:
            raise CellError("E202", f"ZK 가 모르는 phase {phase}")

    def stop(self, robot, reason="ABORT"):
        """reason: PAUSE | ABORT | ERROR — ★어느 쪽이든 모션을 세우는 동작 자체는 같지만,
        호출자가 그 뒤에 무엇을 하는지가 다르다(PAUSE 는 Task 를 끝내지 않는다). 여기서는
        사유를 기록만 하고, 종료 판정은 전적으로 호출자가 한다."""
        self.node.last_stop_reason = reason
        try:
            if robot == "fr5":
                self.fr5.stop()
            elif robot in self.cli:
                self.cli[robot]["stop"].call_async(
                    self.cli[robot]["stop"].srv_type.Request())
        except Exception:
            pass


class CellOrchestrator(Node):
    def __init__(self):
        super().__init__("cell_orchestrator")
        self.declare_parameter("exec_mode", "sim")
        self.declare_parameter("sim_phase_sec", 0.5)
        self.declare_parameter("inject_error", "")     # "E201" 또는 "E201@SUCTION"
        self.declare_parameter("stale_sec", 5.0)       # connected 판정 (계약 §4)
        # ★v0.3: 즉시정지(C5) 가 구현되기 전까지는 false — immediate 요청에도 phase 경계에서 선다.
        #   false 인 동안 ACK 는 AT_PHASE_BOUNDARY 로 **사실대로** 나간다(거짓 ACK 금지).
        self.declare_parameter("immediate_pause_enabled", False)
        # ★v0.3 서버팀 합의: 흡착 상태 Pause 최대 시간. 진공에 피드백이 없어(E201 검출 불가)
        #   Pause 가 길어지면 부품이 조용히 떨어져도 아무도 모른다 — 그 공백을 시간으로 막는다.
        #   60.0 은 임시 후보값. 실기 장시간 흡착 검증 후 확정. 0 = 무제한(권장하지 않음).
        self.declare_parameter("suction_hold_timeout_sec", 60.0)
        # ★v0.3 C8 서버팀 합의: RESUME 전에 부품이 아직 붙어 있는지 비전으로 재검증. 이상 시 FAULT.
        self.declare_parameter("revalidate_on_resume", True)
        # --- 비전 연동 (v0.3 Final 확정판, 8/11) ---
        self.declare_parameter("vision_enabled", True)
        self.declare_parameter("vision_test_mode", True)   # 캘리브 전 = true 고정
        # 셀 활성 캘리브 버전 — 응답 calibration_version 과 대조·차단 주체 = 셀 (8/11 확정).
        # 스텁 버전과 기본 일치시켜 개발 단계 통과. Hand-Eye 후 실버전으로 교체.
        self.declare_parameter("cell_calib_version", "handeye_00000000_v0")
        self.declare_parameter("vision_timeout_sec", 3.0)
        self.declare_parameter("freshness_sec", 1.0)       # baseline 1.0s (E505)
        self.declare_parameter("max_pick_dist_m", 1.5)     # 셀 검증(임시): 카메라 기준 거리 상한
        # ── 후보 검증 체인 (8/14) — 게이트는 전부 기본 OFF.
        #    배치·좌표 변환이 확정되기 전에 켜면 정상 후보를 잘못 떨군다.
        self.declare_parameter("use_workspace_check", False)
        self.declare_parameter("use_ik_check", False)
        self.declare_parameter("use_install_clearance", False)
        self.declare_parameter("grasp_offset_file", "/home/ar/cell_data/grasp_offsets.json")
        self.declare_parameter("slot_box_file", "/home/ar/cell_data/slot_boxes.json")
        for _r, _reach in (("fr5", 0.85), ("zk1", 0.40), ("zk2", 0.40)):
            self.declare_parameter(f"{_r}_base_xyz", [0.0, 0.0, 0.0])
            self.declare_parameter(f"{_r}_reach_min", 0.15)
            self.declare_parameter(f"{_r}_reach_max", _reach)   # ⚠8/18 실측 필요
            self.declare_parameter(f"{_r}_z_min", -0.20)
            self.declare_parameter(f"{_r}_z_max", 1.00)
        self._grasp_off_cache = None
        self._slot_box_cache = None

        self.lock = threading.Lock()       # 상태 전이·활성 태스크 보호
        self.cell_state = "IDLE"
        self.state_reason = ""             # FAULT/ABORTED 사유 (status.error 로 노출)
        self.hold_info = None              # v0.3: HELD 일 때만 채워지는 status.hold 블록
        self.last_stop_reason = ""         # v0.3 C6: 마지막 stop() 의 사유(PAUSE|ABORT|ERROR)
        self.pause_now = threading.Event()  # v0.3 C5: 진행 중인 모션을 지금 끊으라는 신호
        # ★V6(ACK↔status 정합): ACK 에서 약속한 정지 방식과 그 PAUSE 의 req_id 를 기억해 둔다.
        #   hold 블록이 이 값을 그대로 실어야 서버가 "약속대로 섰는지" 대조할 수 있다.
        self.pause_stop_mode = ""
        self.pause_req_id = ""
        self.pause_immediate = False       # 이번 PAUSE 가 immediate 요청이었는가
        self.active = None                 # TaskRun
        self.results = {}                  # req_id -> ExecuteTask.Result (멱등 캐시)
        self.pause_req = False
        self.abort_req = False
        self.seq = 0
        self.ev_seq = 0

        self.adapter_cb = ReentrantCallbackGroup()
        mode = self.get_parameter("exec_mode").value
        self.adapter = SimAdapter(self) if mode == "sim" else RealAdapter(self)

        # 로봇 감시 — joint_states 최신 수신 시각과 값
        self.robot_seen = {}               # name -> (monotonic, [joints])
        for name, topic in (("fr5", "/fr5/joint_states"),
                            ("zk1", "/zkbot1/joint_states"),
                            ("zk2", "/zkbot2/joint_states")):
            # ★8/13: fr5 는 twin_bridge 가 best_effort 로 발행 — reliable 구독은
            #   QoS 비호환으로 수신 0 (connected 오판). 감시는 최신값이면 충분.
            from rclpy.qos import QoSProfile, ReliabilityPolicy
            self.create_subscription(
                JointState, topic,
                lambda m, n=name: self.robot_seen.__setitem__(
                    n, (time.monotonic(), [round(p, 4) for p in m.position])),
                QoSProfile(depth=10,
                           reliability=ReliabilityPolicy.BEST_EFFORT))

        self.vision_cli = self.create_client(
            GetPickPose, "/vision/get_pick_pose", callback_group=self.adapter_cb)

        self.pub_status = self.create_publisher(String, "/cell/status", 10)
        self.pub_event = self.create_publisher(String, "/cell/event", 10)
        # ★9/10 서버 요청: 그리퍼 개도를 /cell/status 로 함께 보낸다(기존 필드 불변, 추가만).
        #   ★1Hz 상태 발행을 막지 않도록 **별도 스레드**가 브리지를 폴링하고, tick_status 는 캐시만 읽는다
        #     (브리지가 그리퍼 동작 중 5초 멈추는 일이 있다 — 9/10 실측. 그때 상태 발행이 끊기면 안 된다).
        self._grip = {"grip": None, "grip_real": None, "grip_real_age_s": None, "t": 0.0}
        threading.Thread(target=self._grip_poller, daemon=True).start()
        self.create_timer(1.0, self.tick_status)

        self.create_service(CellControl, "/cell/control", self.srv_control)
        self.act = ActionServer(
            self, ExecuteTask, "/cell/execute_task",
            execute_callback=self.execute_task,
            goal_callback=self.goal_cb,
            cancel_callback=lambda gh: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup())
        self.get_logger().info(f"cell_orchestrator 시작 exec_mode={mode}")

    # ---------- 상태 전이 초크포인트 ----------
    def change_state(self, new, reason=""):
        assert new in CELL_STATES, new
        with self.lock:
            old = self.cell_state
            if old == new:
                return
            self.cell_state = new
            self.state_reason = reason
        self.get_logger().info(f"cell_state {old} -> {new}  {reason}")

    # ---------- v0.3 정지 방식 판단 ----------
    def _plan_stop(self, immediate):
        """ACK 에 실을 (stop_mode, eta_ms). ★결과가 아니라 **약속**이다.
        실제 정지 완료는 /cell/status 의 cell_state == HELD 로 확인한다(서버팀 합의 2026-09-09).

        지금은 즉시정지 경로(C5)가 없으므로 immediate=true 라도 AT_PHASE_BOUNDARY 로 **사실대로** 답한다.
        구현되면 여기서 '현재 phase 가 즉시정지 가능 구간인가'를 보고 IMMEDIATE / DEFERRED_UNSAFE 를 가른다."""
        run = self.active
        phase = run.phase if run is not None else None
        eta = PHASE_ETA_MS.get(phase, PHASE_ETA_DEFAULT)
        if not immediate:
            return "AT_PHASE_BOUNDARY", eta
        if not bool(self.get_parameter("immediate_pause_enabled").value):
            return "AT_PHASE_BOUNDARY", eta
        # (C5 구현 시) 즉시정지 불가 구간 = 그리퍼 개폐·흡착 동작 — 가장 가까운 safe point 로
        if phase in ("PICK", "SUCTION", "RELEASE"):
            return "DEFERRED_UNSAFE", eta
        return "IMMEDIATE", 0

    # ---------- /cell/control ----------
    def srv_control(self, req, res):
        cmd = req.cmd.strip().upper()
        st = self.cell_state
        ok, detail = False, ""
        stop_mode, eta_ms = "NOT_APPLICABLE", 0     # PAUSE 외에는 정지 방식이라는 개념이 없다
        if cmd == "PAUSE":
            if st == "EXECUTE":
                self.pause_req = True
                self.pause_immediate = bool(getattr(req, "immediate", False))
                stop_mode, eta_ms = self._plan_stop(self.pause_immediate)
                self.pause_stop_mode = stop_mode
                self.pause_req_id = req.req_id
                ok = True
                if stop_mode == "IMMEDIATE":
                    # ★지금 세운다. 사유를 PAUSE 로 태그해야 Task 종료 경로를 안 탄다(C6).
                    run = self.active
                    self.pause_now.set()
                    try:
                        self.adapter.stop(run.robot if run else "", reason="PAUSE")
                    except Exception as ex:
                        self.get_logger().warning(f"즉시정지 stop() 실패: {ex}")
                    detail = "현재 모션까지 즉시 감속 정지"
                elif stop_mode == "DEFERRED_UNSAFE":
                    detail = ("지금 구간은 즉시정지 불가 — 가장 가까운 safe point 에서 정지 "
                              "(그리퍼 개폐·흡착 동작은 중단하면 복구 불가 알람)")
                else:
                    detail = "현재 안전 단위 완료 후 HELD 진입"
                    if self.pause_immediate:
                        # ★거짓 ACK 금지: immediate 를 받아놓고 못 세우면 그 사실을 그대로 말한다.
                        detail += " (immediate 요청 — 즉시정지 경로 미구현, v0.3 C5)"
            elif st == "HELD":
                ok, detail = True, "이미 HELD"
                stop_mode, eta_ms = "NOT_APPLICABLE", 0
            else:
                detail = f"PAUSE 불가 (state={st})"
        elif cmd == "RESUME":
            if st == "HELD" and self.active is not None:
                self.pause_req = False
                self.pause_immediate = False
                self.pause_now.clear()
                self.pause_stop_mode = ""
                self.hold_info = None
                self.change_state("EXECUTE", "RESUME")
                ok = True
            elif st == "HELD":
                # 오류(E101/E201 등)로 HELD 가 된 경우 — 태스크는 이미 FAILED 로
                # 종료됐다. 재개할 것이 없으니 RESET 으로 IDLE 복귀 후 재발주.
                detail = "재개할 태스크 없음 (오류 HELD) — RESET 후 Task 재발주"
            else:
                detail = f"RESUME 불가 (state={st})"
        elif cmd == "ABORT":
            if st in ("EXECUTE", "HELD"):
                self.abort_req = True
                self.pause_req = False     # HELD 대기 중이면 깨워서 중단시킨다
                self.pause_now.clear()
                self.hold_info = None
                ok, detail = True, "현재 태스크 안전 중단"
            else:
                detail = f"ABORT 불가 (state={st})"
        elif cmd == "RESET":
            if st == "HELD" and self.active is None:   # 오류 HELD 탈출구
                self.hold_info = None
                self.change_state("IDLE", "RESET")
                ok = True
            elif st in ("ABORTED", "FAULT"):
                if self.active is not None:
                    detail = "태스크 종료 처리 중 — 잠시 후 재시도"
                else:
                    self.abort_req = False
                    self.change_state("IDLE", "RESET")
                    ok = True
            elif st == "IDLE":
                ok, detail = True, "이미 IDLE"
            else:
                detail = f"RESET 불가 (state={st}) — 실행 중이면 ABORT 먼저"
        else:
            detail = f"모르는 cmd {req.cmd!r}"
        res.accepted = ok
        res.cell_state = self.cell_state
        res.detail = detail
        res.stop_mode = stop_mode
        res.eta_ms = str(eta_ms)
        if not ok:
            self.get_logger().warning(f"/cell/control {cmd} 거부: {detail}")
        return res

    # ---------- Action goal 접수 ----------
    def goal_cb(self, goal):
        if goal.task_type not in TASK_TABLE:
            self.get_logger().error(f"모르는 task_type {goal.task_type!r} — 거부")
            return GoalResponse.REJECT
        if not goal.req_id:
            self.get_logger().error("req_id 없음 — 거부")
            return GoalResponse.REJECT
        with self.lock:
            # 멱등 재수신(완료·진행 중)은 수락해서 저장/공유 Result 를 돌려준다
            if goal.req_id in self.results:
                return GoalResponse.ACCEPT
            if self.active is not None:
                if goal.req_id == self.active.req_id:
                    return GoalResponse.ACCEPT
                self.get_logger().error(
                    f"태스크 실행 중({self.active.req_id[:8]}) — 새 req_id 거부")
                return GoalResponse.REJECT
            if self.cell_state not in ("IDLE",):
                self.get_logger().error(
                    f"cell_state={self.cell_state} — Goal 거부 (RESET 필요)")
                return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    # ---------- Action 실행 ----------
    def execute_task(self, gh):
        goal = gh.request
        # 멱등: 완료건 재수신 → 저장된 Result 그대로
        cached = self.results.get(goal.req_id)
        if cached is not None:
            self.get_logger().info(f"멱등 재수신(완료) {goal.req_id[:8]} — 캐시 반환")
            gh.succeed()
            return cached
        with self.lock:
            if self.active is not None and self.active.req_id == goal.req_id:
                run = self.active          # 진행 중 재수신 → 그 실행에 붙는다
            else:
                run = TaskRun(goal)
                self.active = run
        if run.goal is not goal:           # 붙은 골: 원 실행 완료까지 대기
            self.get_logger().info(f"멱등 재수신(진행 중) {goal.req_id[:8]} — 대기")
            run.done.wait()
            gh.succeed()
            return run.result
        return self._run_task(gh, run)

    def _run_task(self, gh, run):
        goal = run.goal
        try:
            parts = json.loads(goal.parts_json) if goal.parts_json.strip() else []
            assert isinstance(parts, list)
        except Exception:
            return self._finish(gh, run, "FAILED", "E301",
                                f"parts_json 파싱 실패: {goal.parts_json!r}")
        if not parts:
            parts = [{"slot": "?", "class": "?"}]   # BOM 미확정 단계의 빈 Goal 허용
        robot, phases = TASK_TABLE[goal.task_type]
        run.total_items = len(parts)
        self.change_state("EXECUTE", f"{goal.task_type} {goal.req_id[:8]}")
        self.get_logger().info(
            f"Task 시작 {goal.task_type} job={goal.job_id} step={goal.step_id} "
            f"items={len(parts)} robot={robot}")

        total_steps = len(parts) * len(phases)
        step = 0
        for i, part in enumerate(parts, start=1):
            run.current_item = i
            for phase in phases:
              # ★v0.3 C5: PAUSE 로 phase 가 중간에 끊기면 그 phase 를 **다시 시작**한다(계약 §4).
              #   우리 phase 는 전부 절대 목표(측정→목표 계산→이동)라 이어붙이기보다 재실행이 안전하고
              #   결과도 같다. 벽을 든 채였다면 재실행이 곧 "정렬 높이로 되올라가 다시 측정"이다(§4-1).
              while True:
                # --- 안전 단위 경계: 중단·일시정지·취소는 여기서만 판단 ---
                # ★재개 전 재검증(C8)이 여기서 CellError 를 올릴 수 있다. 감싸지 않으면
                #   액션 골이 Result 없이 abort 돼 서버가 error_code 도 못 받는다(9/9 실측).
                try:
                    r = self._boundary(gh, run, robot, next_phase=phase)
                except CellError as e:
                    return self._fail(gh, run, robot, e)
                if r is not None:
                    return r
                run.phase = phase
                self._feedback(gh, run)
                try:
                    self.adapter.run_phase(robot, phase, part, run)
                    if phase == "SUCTION":
                        run.holding_suction = True
                    elif phase == "RELEASE":
                        run.holding_suction = False
                    elif phase == "PICK":
                        run.holding_part = True
                    elif phase == "RETREAT":
                        run.holding_part = False
                    # OBSERVE = 관찰 자세 도착·정지 후 비전 요청 (v0.3 §10)
                    if (phase == "OBSERVE"
                            and self.get_parameter("vision_enabled").value):
                        cand = self._vision_pick(part, run)
                        self.get_logger().info(
                            f"pick 후보 확정 id={cand.candidate_id} "
                            f"q={cand.quality:.2f} yaw={cand.yaw_deg:.1f}"
                            f"({'ok' if cand.yaw_valid else cand.orientation_source}) "
                            f"[{cand.position.x:.3f},{cand.position.y:.3f},"
                            f"{cand.position.z:.3f}]@{cand.frame_id}")
                # ★_hold_mid_motion 이 올리는 CellError(재개 전 재검증 실패)는 아래
                #   except CellError 가 받는다 — run_phase 와 같은 try 안이라 자동으로 처리된다.
                except PauseInterrupt as pi:
                    # ★서버팀 요청 V2: 여기서 절대 _finish() 를 타면 안 된다.
                    #   PAUSE 로 인한 모션 취소는 Task 종료가 아니다 — ExecuteTask 는 살아 있고
                    #   HELD 로만 들어간다. ABORT·오류와 예외 타입이 다른 이유가 이것이다.
                    r = self._hold_mid_motion(gh, run, robot, pi)
                    if r is not None:
                        return r            # HELD 중에 ABORT/Cancel 이 온 경우만 종료
                    continue                # RESUME → 같은 phase 재시작
                except CellError as e:
                    return self._fail(gh, run, robot, e)
                step += 1
                run.progress = step / total_steps
                break
            slot = part.get("slot")
            if slot:
                run.completed.append(slot)
        try:
            r = self._boundary(gh, run, robot)  # 마지막 phase 후 ABORT 반영
        except CellError as e:
            return self._fail(gh, run, robot, e)
        if r is not None:
            return r
        self.publish_event("STEP_DONE", goal.step_id, robot, "",
                           f"{goal.task_type} 완료")
        self.change_state("IDLE", "task done")
        return self._finish(gh, run, "SUCCEEDED", "", "")

    def _on_resume(self, run, phase):
        """★v0.3 C8·C9 — HELD 를 빠져나와 phase 를 다시 돌기 **직전**에 부르는 훅.

        C8 (RESUME 전 Vision 재검증, 서버팀 합의 2026-09-09):
          멈춰 있는 동안 부품이 떨어졌을 수 있다. 특히 흡착은 피드백이 없어(E201 검출 불가)
          "붙어 있다"는 근거가 명령 이력뿐이다. 재개 전에 눈으로 한 번 더 본다.
          이상이면 CellError 를 올려 FAULT 로 간다(계속 진행하지 않는다).

        C9 (벽을 든 채 재개):
          run.resume_from_hold 를 세워 어댑터에 알린다. 어댑터는 이 값이 True 면
          **정렬 높이로 되올라가 다시 측정한 뒤** 내려간다(§4-1). 멈춘 자리에서 이어
          내려가면 그 사이 밑판이 밀린 것을 못 본 채로 밀어 넣게 된다.
        """
        run.resume_from_hold = True
        if not (run.holding_part or run.holding_suction):
            return                      # 빈 손이면 재검증할 것이 없다
        if not bool(self.get_parameter("revalidate_on_resume").value):
            self.get_logger().warning(
                "재개 전 Vision 재검증이 꺼져 있다 — 부품이 떨어졌어도 모른 채 진행한다")
            return
        held = "흡착" if run.holding_suction else "그리퍼"
        ok, why = self.adapter.verify_held(run.robot, run)
        if ok is None:
            # ★검증 수단이 없는 조합. '통과'로 처리하지 않고 그 사실을 남긴다.
            self.get_logger().warning(f"재개 전 {held} 파지 재검증 불가 — {why}")
            self.publish_event("RESUME_UNVERIFIED", run.goal.step_id, run.robot, "",
                               f"{held} 파지 재검증 불가: {why}")
            return
        if not ok:
            raise CellError("E201", f"재개 전 {held} 파지 재검증 실패: {why}")
        self.get_logger().info(f"재개 전 {held} 파지 재검증 OK — {why} → {phase} 재시작")

    def _hold_mid_motion(self, gh, run, robot, pi):
        """★v0.3 C5: phase **중간**에서 PAUSE 로 멈춘 뒤의 HELD 대기.
        계속해도 되면 None, ABORT/Cancel 로 끝내야 하면 Result 를 돌려준다.
        _finish() 를 타지 않는 것이 핵심 — Task 는 살아 있어야 한다(서버팀 V2)."""
        self.pause_req = False
        self.pause_now.clear()
        self.hold_info = {
            "task_req_id": run.goal.req_id,
            "pause_req_id": self.pause_req_id,
            "stop_mode": self.pause_stop_mode or "IMMEDIATE",
            "held_at": pi.where,          # MID_MOTION | STEP_BOUNDARY
            "phase": pi.phase,            # 재개하면 이 phase 를 처음부터 다시 돈다
            "since": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "resumable": True,
        }
        self.change_state("HELD", f"PAUSE(immediate) during {pi.phase}")
        self.get_logger().info(f"즉시정지 HELD — {pi.phase} 중간({pi.where}). "
                               f"RESUME 시 {pi.phase} 를 다시 시작한다")
        t_hold = time.monotonic()
        suction_to = float(self.get_parameter("suction_hold_timeout_sec").value)
        while self.cell_state == "HELD":
            if self.abort_req or gh.is_cancel_requested:
                self.hold_info = None
                return self._boundary(gh, run, robot, next_phase=pi.phase)
            if suction_to > 0 and run.holding_suction and time.monotonic() - t_hold > suction_to:
                self.hold_info = None
                self.change_state("FAULT", f"흡착 유지 Pause 가 {suction_to:.0f}초 초과 — 낙하 위험")
                return self._finish(gh, run, "FAILED", "E201",
                                    f"흡착 Pause 타임아웃 {suction_to:.0f}s 초과 (진공 피드백 없음)")
            time.sleep(0.1)
        self.hold_info = None
        self._on_resume(run, pi.phase)
        return None

    def _boundary(self, gh, run, robot, next_phase=None):
        """phase 사이 경계 처리. 계속이면 None, 종료면 Result.
        next_phase = 이 경계를 통과하면 **다음에 돌 phase**. HELD 로 들어갈 때
        "재개하면 무엇부터 도는가"를 status.hold.phase 로 정직하게 싣기 위해 받는다
        (run.phase 는 직전에 끝난 phase 라 여기서 쓰면 서버가 헷갈린다)."""
        if self.abort_req:
            self.abort_req = False
            self.adapter.stop(robot, reason="ABORT")
            self.change_state("ABORTED", "ABORT by FMS")
            return self._finish(gh, run, "CANCELED", "", "ABORT 요청으로 중단")
        if gh.is_cancel_requested:
            self.adapter.stop(robot, reason="ABORT")
            self.change_state("IDLE", "action cancel")
            return self._finish(gh, run, "CANCELED", "", "Action Cancel 로 중단")
        if self.pause_req:
            self.pause_req = False
            # ★v0.3: 실제로 멈춘 지점을 여기서 기록한다(서비스 ACK 가 아니라 /cell/status 로 나간다).
            #   지금은 phase 경계에서만 서므로 항상 PHASE_BOUNDARY. C5 구현 시 STEP_BOUNDARY 등이 생긴다.
            self.hold_info = {
                "task_req_id": run.goal.req_id,          # 어느 Task 가 멈췄는지
                "pause_req_id": self.pause_req_id,       # 어느 PAUSE 요청으로 멈췄는지
                "stop_mode": self.pause_stop_mode or "AT_PHASE_BOUNDARY",   # ACK 에서 약속한 방식 그대로
                "held_at": "PHASE_BOUNDARY",
                "phase": next_phase or run.phase,   # 재개하면 여기서부터 돈다
                "since": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "resumable": True,
            }
            self.change_state("HELD", "PAUSE")
            t_hold = time.monotonic()
            suction_to = float(self.get_parameter("suction_hold_timeout_sec").value)
            while self.cell_state == "HELD":
                if self.abort_req or gh.is_cancel_requested:
                    return self._boundary(gh, run, robot, next_phase)
                # ★C7: 흡착으로 부품을 문 채 Pause 가 길어지면 조용히 떨어질 수 있다 → FAULT
                if suction_to > 0 and run.holding_suction and time.monotonic() - t_hold > suction_to:
                    self.hold_info = None
                    self.change_state("FAULT", f"흡착 유지 Pause 가 {suction_to:.0f}초 초과 — 낙하 위험")
                    return self._finish(gh, run, "FAILED", "E201",
                                        f"흡착 Pause 타임아웃 {suction_to:.0f}s 초과 (진공 피드백 없음)")
                time.sleep(0.1)
            self.hold_info = None
            # ★C8·C9: HELD 를 빠져나와 다시 돌기 직전. 재검증 실패면 CellError → FAULT.
            self._on_resume(run, next_phase or run.phase)
        return None

    # ---------- 비전 요청·검증 (OBSERVE, v0.3 Final 확정판) ----------
    def _call_vision(self, part):
        """서비스 1회 호출. 응답 또는 CellError(E505=미응답)."""
        if not self.vision_cli.wait_for_service(timeout_sec=2.0):
            # ⚠계약 갭: 비전 노드 다운 코드가 §6 에 없다 — "신선한 측정을 얻지
            # 못함"으로 보고 E505 로 임시 매핑 (M5 에서 전용 코드 제안 예정).
            raise CellError("E505", "비전 서비스 없음 (/vision/get_pick_pose)")
        rq = GetPickPose.Request()
        rq.ver = "0.3"
        rq.req_id = str(uuid.uuid4())
        rq.part_code = str(part.get("part_code", part.get("slot", "")))
        rq.class_name = str(part.get("class", ""))
        rq.zone_id = str(part.get("zone", ""))
        rq.max_candidates = 0                       # 0 = 기본(3)
        rq.test_mode = bool(self.get_parameter("vision_test_mode").value)
        fut = self.vision_cli.call_async(rq)
        t0 = time.monotonic()
        timeout = float(self.get_parameter("vision_timeout_sec").value)
        while not fut.done():
            if time.monotonic() - t0 > timeout:
                raise CellError("E505", f"비전 응답 타임아웃 {timeout}s")
            time.sleep(0.02)
        return fut.result()

    # ── 후보 검증 체인 (2026-08-14 구현) ────────────────────────────
    # 비전은 "부품이 어디 있다"까지만 준다. 그것을 로봇이 실제로 갈 수 있는
    # 것으로 바꾸는 전부가 셀 몫이다. 전부 탈락하면 E204(셀 소유 오류코드).
    #
    # ★단계: ①형식 ②파지점 오프셋 ③작업영역 ④IK 해 존재 ⑤채택
    #   ①TF 변환(capture_stamp 시점, camera→workcell)은 SCANPOSE/Hand-Eye
    #    확정 후 _to_workcell() 에 채운다 — 그 전까지는 항등(비전 좌표 그대로).
    #    현재 스텁이 production_valid=false 라 실기 유입은 어차피 차단돼 있다.

    def _to_workcell(self, c):
        """후보 좌표를 workcell_frame 기준으로. ★SCANPOSE/Hand-Eye 확정 후 구현.

        지금은 항등 반환 + frame_id 를 그대로 물려준다. 변환이 들어갈 자리를
        한 곳으로 모아둬서, 확정되면 이 함수만 고치면 되게 한다."""
        p = c.position
        return (p.x, p.y, p.z), (c.frame_id or "")

    def _grasp_point(self, xyz, yaw_deg, class_name):
        """파지점 = 부품 기준점 + (yaw 만큼 회전한) class 별 오프셋.

        ★역할 경계(8/14 비전 협의안 A-3): 비전은 부품의 position+yaw+class 만
          주고, "그 부품의 어디를 잡는가"는 셀이 정한다. 파지점은 그리퍼 폭·
          파지력·무게중심에 좌우되는 로봇 쪽 지식이고, class 별 고정값이며,
          그리퍼를 바꿨을 때 비전 코드를 고치게 되면 안 되기 때문이다.
        ⚠오프셋 값은 8/18 실물 실측 후 확정. 파일이 없으면 전부 0(=기준점 그대로)
          이라 기존 동작과 같다. 값은 ~/cell_data/grasp_offsets.json 에서 읽는다
          (코드 수정 없이 화요일 실측값을 채워넣기 위함).
        ⚠비전의 position 정의(바운딩박스 중심/윗면/마스크중심)가 확정돼야
          오프셋 표가 의미를 갖는다 — 비전 협의안 A-2 회신 대기 중."""
        off = self._grasp_offsets().get(class_name)
        if not off:
            return xyz                              # 미정의 class = 기준점 그대로
        dx, dy, dz = off
        th = math.radians(yaw_deg or 0.0)
        return (xyz[0] + dx * math.cos(th) - dy * math.sin(th),
                xyz[1] + dx * math.sin(th) + dy * math.cos(th),
                xyz[2] + dz)

    def _grasp_offsets(self):
        if self._grasp_off_cache is None:
            path = str(self.get_parameter("grasp_offset_file").value)
            try:
                with open(path) as f:
                    raw = json.load(f)
                self._grasp_off_cache = {
                    k: tuple(float(v) for v in val[:3]) for k, val in raw.items()}
                self.get_logger().info(
                    f"파지점 오프셋 {len(self._grasp_off_cache)}건 로드: {path}")
            except FileNotFoundError:
                self._grasp_off_cache = {}
                self.get_logger().warn(
                    f"파지점 오프셋 파일 없음({path}) — 기준점을 그대로 파지점으로 사용. "
                    f"8/18 실측 후 채울 것")
            except Exception as e:                  # 형식 오류는 조용히 넘기지 않는다
                self._grasp_off_cache = {}
                self.get_logger().error(f"파지점 오프셋 로드 실패({path}): {e}")
        return self._grasp_off_cache

    def _in_workspace(self, robot, xyz):
        """작업영역 검사 — (통과여부, 사유). 로봇 밑동 기준 반경·높이.

        ⚠기본값은 보수적 초기값이다. 8/18 배치 확정 후 실측해서 파라미터로
          덮어쓸 것(reach_max 는 카탈로그값이 아니라 '흡착/그리퍼 달린 상태의
          실사용 반경'이어야 한다)."""
        bx, by, bz = (float(v) for v in
                      self.get_parameter(f"{robot}_base_xyz").value)
        r_min = float(self.get_parameter(f"{robot}_reach_min").value)
        r_max = float(self.get_parameter(f"{robot}_reach_max").value)
        z_min = float(self.get_parameter(f"{robot}_z_min").value)
        z_max = float(self.get_parameter(f"{robot}_z_max").value)
        r = math.sqrt((xyz[0] - bx) ** 2 + (xyz[1] - by) ** 2)
        if r > r_max:
            return False, f"reach {r:.3f}>{r_max}"
        if r < r_min:
            return False, f"reach {r:.3f}<{r_min}(밑동 근접)"
        if not (z_min <= xyz[2] - bz <= z_max):
            return False, f"z {xyz[2]-bz:.3f} 범위밖[{z_min},{z_max}]"
        return True, ""

    def _validate_candidates(self, res, part=None, robot="fr5"):
        """셀 측 후보 검증. 통과 후보 or CellError(E204).

        비전은 품질 내림차순으로 준다(계약) → 첫 통과 후보가 최선.
        탈락 사유를 전부 모아 E204 detail 에 넣는다(현장 진단용)."""
        max_d = float(self.get_parameter("max_pick_dist_m").value)
        use_ws = bool(self.get_parameter("use_workspace_check").value)
        cls = str((part or {}).get("class", ""))
        reasons = []
        for c in res.candidates:
            p = c.position
            # ① 형식
            if not c.pose_valid:
                reasons.append(f"#{c.candidate_id}:pose_invalid")
                continue
            if not all(math.isfinite(v) for v in (p.x, p.y, p.z)):
                reasons.append(f"#{c.candidate_id}:non_finite")
                continue
            if math.sqrt(p.x**2 + p.y**2 + p.z**2) > max_d:
                reasons.append(f"#{c.candidate_id}:dist>{max_d}m")
                continue
            # ② 좌표 변환 + 파지점
            xyz, _frame = self._to_workcell(c)
            g = self._grasp_point(xyz, getattr(c, "yaw_deg", 0.0), cls)
            # ③ 작업영역 (게이트 OFF 면 건너뜀 — 배치 실측 전 기본 OFF)
            if use_ws:
                ok, why = self._in_workspace(robot, g)
                if not ok:
                    reasons.append(f"#{c.candidate_id}:{why}")
                    continue
            # ④ IK 해 — MoveIt /compute_ik 위임. 서비스 없으면 건너뛴다
            #    (중복 구현하지 않는다. 어차피 실행도 MoveIt 이 한다)
            ok, why = self._ik_reachable(robot, g)
            if not ok:
                reasons.append(f"#{c.candidate_id}:{why}")
                continue
            # ⑤ 채택
            self.get_logger().info(
                f"후보 채택 #{c.candidate_id} q={getattr(c,'quality',0):.2f} "
                f"파지점=[{g[0]:.3f},{g[1]:.3f},{g[2]:.3f}] class={cls or '-'}")
            return c
        raise CellError("E204", "후보 전부 셀 검증 탈락: " + ",".join(reasons))

    def _ik_reachable(self, robot, xyz):
        """IK 해 존재 확인 — (통과여부, 사유).

        ★게이트 OFF(기본) 면 무조건 통과. MoveIt /compute_ik 연결은 SCANPOSE
          확정 후 좌표가 실제 로봇 좌표계로 들어올 때 의미가 생긴다. 그 전에
          켜면 항등변환된 카메라 좌표로 IK 를 물어보는 꼴이라 무의미하다."""
        if not bool(self.get_parameter("use_ik_check").value):
            return True, ""
        return True, ""                             # ★SCANPOSE 확정 후 구현

    def _check_install_clearance(self, slot, run):
        """설치(Place/Insert) 쪽 간섭 검사 — (통과여부, 사유).

        ★이 검사가 셀 고유인 이유: '지금까지 무엇이 설치됐는가'는 비전이
          원리적으로 알 수 없다. 비전은 컨베이어 위 부품만 본다. 설치 상태는
          run.completed(=completed_json) 에만 있다.
        ★수직 낙하 전용 제약(v3 패널 공법)이라 간섭 판정이 단순해진다 —
          목표 slot 바로 위 수직 통로가 이미 설치된 것과 겹치는지만 보면 된다.
        ⚠slot 별 점유 영역(AABB)은 8/18 배치·티칭 후 실측해서 채운다. 표가
          비어 있으면 통과(기존 동작과 동일)."""
        boxes = self._slot_boxes()
        me = boxes.get(slot)
        if not me:
            return True, ""                         # 미정의 slot = 검사 불가 → 통과
        for done in run.completed:
            other = boxes.get(done)
            if not other or done == slot:
                continue
            if self._xy_overlap(me, other) and other["z_top"] > me["z_bot"]:
                return False, f"{done} 이(가) {slot} 수직 통로를 막음"
        return True, ""

    @staticmethod
    def _xy_overlap(a, b):
        return not (a["x_max"] <= b["x_min"] or b["x_max"] <= a["x_min"] or
                    a["y_max"] <= b["y_min"] or b["y_max"] <= a["y_min"])

    def _slot_boxes(self):
        if self._slot_box_cache is None:
            path = str(self.get_parameter("slot_box_file").value)
            try:
                with open(path) as f:
                    self._slot_box_cache = json.load(f)
                self.get_logger().info(
                    f"slot 점유영역 {len(self._slot_box_cache)}건 로드: {path}")
            except FileNotFoundError:
                self._slot_box_cache = {}
            except Exception as e:
                self._slot_box_cache = {}
                self.get_logger().error(f"slot 점유영역 로드 실패({path}): {e}")
        return self._slot_box_cache

    def _vision_pick(self, part, run):
        """get_pick_pose 요청 → 검증 → 후보 반환. 재시도 정책 = 계약 §6.
        E501/E505/E506 재요청 2회 · E504 1회(관찰자세 변경은 실기 전 TODO) · E503 즉시."""
        cell_ver = str(self.get_parameter("cell_calib_version").value)
        test_mode = bool(self.get_parameter("vision_test_mode").value)
        fresh = float(self.get_parameter("freshness_sec").value)
        retry_left = {"E501": 2, "E502": 2, "E505": 2, "E506": 2, "E504": 1}
        while True:
            if self.abort_req:
                raise CellError("E204", "ABORT 중 비전 요청 중단")  # boundary 가 마무리
            res = self._call_vision(part)
            err = ""
            if res.error_code:
                err = res.error_code
            elif res.calibration_version != cell_ver:
                # 버전 대조·차단 주체 = 셀 (8/11 확정). 재시도 무의미 → 즉시.
                raise CellError("E503", f"캘리브 버전 불일치 vision="
                                        f"{res.calibration_version} cell={cell_ver}")
            elif not test_mode and not res.production_valid:
                raise CellError("E503", "production_valid=false 응답 — 실기 사용 차단")
            elif not res.pose_valid or not res.candidates:
                err = "E506"
            else:
                # Freshness: 후보 공통 capture_stamp (같은 프레임 합의)
                c0 = res.candidates[0].capture_stamp
                age = (self.get_clock().now().nanoseconds
                       - (c0.sec * 10**9 + c0.nanosec)) / 1e9
                if age > fresh:
                    err = "E505"
                else:
                    return self._validate_candidates(res, part)  # E204 는 여기서 즉시 raise
            if err == "E503" or retry_left.get(err, 0) <= 0:
                raise CellError(err, f"비전 실패({err}) 재시도 소진 — {res.detail}")
            retry_left[err] -= 1
            self.get_logger().warning(
                f"비전 {err} → 재요청 (남은 {retry_left[err]})")
            time.sleep(0.2)

    def _fail(self, gh, run, robot, e):
        self.adapter.stop(robot)
        nxt = ERROR_STATE.get(e.code, "FAULT")
        self.publish_event("ROBOT_FAULT", run.goal.step_id, robot, e.code, e.detail)
        # HELD 계열 오류(파지 실패 등)도 자동 재시도는 없다 — Result 는 FAILED 로
        # 확정하고, 셀 상태만 관리자 개입 대기 상태로 남긴다 (§6 자동 재개 금지).
        self.change_state(nxt, f"{e.code} {e.detail}")
        return self._finish(gh, run, "FAILED", e.code, e.detail)

    def _finish(self, gh, run, status, code, detail):
        res = ExecuteTask.Result()
        res.status = status
        res.error_code = code
        res.detail = detail
        res.completed_json = json.dumps(run.completed, ensure_ascii=False)
        self.results[run.req_id] = res
        with self.lock:
            if self.active is run:
                self.active = None
        run.result = res
        run.done.set()
        if status == "CANCELED" and gh.is_cancel_requested:
            gh.canceled()
        elif status == "SUCCEEDED":
            gh.succeed()
        else:
            gh.abort() if status == "FAILED" else gh.succeed()
        self.get_logger().info(
            f"Task 종료 {run.req_id[:8]} {status} {code} {detail}")
        return res

    def _feedback(self, gh, run):
        fb = ExecuteTask.Feedback()
        fb.phase = run.phase
        fb.current_item = run.current_item
        fb.total_items = run.total_items
        fb.progress = float(run.progress)
        fb.robot = run.robot
        gh.publish_feedback(fb)

    # ---------- 그리퍼 상태 폴링(브리지) ----------
    GRIP_URL = "http://127.0.0.1:8765/status"
    GRIP_STALE_S = 5.0          # 브리지를 이 시간 넘게 못 읽으면 전부 null(옛 값을 최신인 척 보내지 않는다)
    GRIP_REAL_MAX_AGE_S = 30.0  # ★9/10: 브리지가 준 '실측' 자체가 이보다 낡으면 grip_real 은 null.
    #   실측은 8/19 서보 굶김 사고 때문에 **유휴에서만·2.5s 스로틀·기본 OFF** 다(bridge_server._safe_grip_read 7중 게이트).
    #   그래서 값이 몇 시간씩 낡을 수 있는데(실제 171409s=47시간 관측), 그대로 내보내면
    #   유니티 디지털 트윈이 옛 개도를 현재값으로 그린다. 나이를 보고 null 로 끊는다 — age 는 그대로 실어 보낸다.

    def _grip_poller(self):
        """브리지에서 FR5 그리퍼 개도를 읽어 캐시한다. 실패는 조용히 넘기고 값만 낡게 둔다."""
        import urllib.request as _u
        while True:
            try:
                d = json.loads(_u.urlopen(self.GRIP_URL, timeout=1.5).read())
                f = (d.get("robots") or {}).get("fr5") or {}
                gr = f.get("gripper_real")
                self._grip = {"grip": f.get("gripper"),
                              "grip_real": int(gr) if isinstance(gr, (int, float)) or (isinstance(gr, str) and str(gr).isdigit()) else None,
                              "grip_real_age_s": round(float(f["gripper_real_age"]), 1) if f.get("gripper_real_age") is not None else None,
                              "t": time.monotonic()}
            except Exception:
                pass
            time.sleep(1.0)

    def _grip_fields(self):
        """상태에 실을 그리퍼 3필드. 폴링이 끊겼으면 전부 null, 실측만 낡았으면 grip_real 만 null."""
        g = self._grip
        if not g.get("t") or time.monotonic() - g["t"] > self.GRIP_STALE_S:
            return {"grip": None, "grip_real": None, "grip_real_age_s": None}
        out = {k: g[k] for k in ("grip", "grip_real", "grip_real_age_s")}
        age = out.get("grip_real_age_s")
        if age is None or age > self.GRIP_REAL_MAX_AGE_S:
            out["grip_real"] = None          # 나이(age)는 남겨 둔다 — 왜 null 인지 서버가 알 수 있게
        return out

    # ---------- /cell/status 1Hz ----------
    def tick_status(self):
        self.seq += 1
        stale = float(self.get_parameter("stale_sec").value)
        now = time.monotonic()
        robots = {}
        for name in ("fr5", "zk1", "zk2"):
            seen = self.robot_seen.get(name)
            connected = bool(seen and now - seen[0] < stale)
            state = "IDLE"
            run = self.active
            if run is not None and run.robot == name and run.phase:
                state = run.phase
            robots[name] = {"state": state, "connected": connected,
                            "joints": seen[1] if seen else [], "error": None}
            if name == "fr5":
                # grip=명령값 / grip_real=실측(없거나 낡으면 null) / grip_real_age_s=실측이 몇 초 전 값인지.
                #   ★"부품을 잡고 있나"는 grip_real 로 판단해야 한다 — 명령값은 보냈다는 뜻일 뿐이다.
                robots[name].update(self._grip_fields())
        run = self.active
        active = None
        if run is not None:
            g = run.goal
            active = {"req_id": g.req_id, "job_id": g.job_id, "step_id": g.step_id,
                      "task_type": g.task_type, "phase": run.phase,
                      "current_item": run.current_item,
                      "total_items": run.total_items,
                      "progress": round(run.progress, 3), "robot": run.robot}
        msg = {"ver": VER, "seq": self.seq,
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "cell_state": self.cell_state,
               # ★v0.3: 실제로 멈춘 지점. HELD 일 때만 채워지고 그 외에는 null.
               #   서버는 이 블록과 cell_state=="HELD" 로 "정말 멈췄는지"를 판단한다(ACK 가 아니라).
               "hold": self.hold_info if self.cell_state == "HELD" else None,
               "active_task": active,
               "robots": robots,
               "conveyor": {"running": False},
               "error": self.state_reason
                        if self.cell_state in ("ABORTED", "FAULT") else None}
        self.pub_status.publish(String(data=json.dumps(msg, ensure_ascii=False)))

    # ---------- /cell/event ----------
    def publish_event(self, event, step_id, robot, code, detail):
        self.ev_seq += 1
        msg = {"ver": VER, "event": event, "step_id": step_id, "robot": robot,
               "code": code, "detail": detail,
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "seq": self.ev_seq}
        self.pub_event.publish(String(data=json.dumps(msg, ensure_ascii=False)))


def main():
    rclpy.init()
    node = CellOrchestrator()
    ex = MultiThreadedExecutor(num_threads=8)
    ex.add_node(node)
    try:
        ex.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()


if __name__ == "__main__":
    main()
