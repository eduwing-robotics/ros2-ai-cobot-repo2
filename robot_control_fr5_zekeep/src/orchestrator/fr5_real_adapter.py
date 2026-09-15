#!/usr/bin/env python3
"""fr5_real_adapter.py — FR5(Fairino5) 실기 어댑터 (cell_orchestrator RealAdapter 의 fr5 위임 대상)

전제 스택: real_robot.launch.py (MoveIt2 + FairinoHardwareInterface, 도메인 73)
개통 순서 선행 필수: cmd_server → ActGripper(1,1) + Mode(0) + RobotEnable(1) → 정상종료 → real_robot.

■ 관절 이동 = MoveIt MoveGroup 액션 /move_action
  fr5_pose.py 로 실물 검증된 패턴 그대로: 관절각 제약 + 저속 스케일 + ★정지 대기.
  (8/10 실측: MoveGroup SUCCESS 시점 ≠ 실물 정지 시점 — SUCCESS 직후 8.4° 부족 사례.
   → joint_states 가 '안정'될 때까지 기다린 뒤 반환한다.)

■ 그리퍼 = 개조 hardware plugin 이 서빙하는 /fairino_remote_command_service  ★8/13 확정
  실기 스택에는 원래 /fairino_remote_command_service 가 없다(실측). 후보 검증 결과:
    ① gripper_controller FJT ........ ✘ mock_components 전용 = RViz/트윈 시각화만
       (fairino5_v6_robot.ros2_control.xacro 에 명시 — 실동작 아님)
    ② ros2_cmd_server 병행 .......... ✘ 8/7 실측: 로봇 RPC 단일 클라이언트 제약.
       cmd_server 와 FairinoHardwareInterface 동시 접속 불가(kill -9 시 세션 수 분 잠김)
    ③ 컨트롤러 XML-RPC(8080) 직결 .... ✘ 8/13 probe 실기 실패 확정 — TCP connect 는
       간헐 성공하나 XML-RPC 무응답(GetSDKVersion 포함). hardware plugin 의 상시
       세션이 RPC 서버를 점유. (Fr5Gripper 클래스는 참고용으로만 보존, mode="rpc")
    ④ 플랜B: hardware plugin 에 그리퍼 서비스 내장 ✔ 채택 (8/13 저녁 구현).
       fairino_hardware_v3_9_7 의 FairinoHardwareInterface 가 자기 SDK 세션으로
       /fairino_remote_command_service (기존 RemoteCmdInterface srv 재사용,
       cmd_server 와 같은 이름 = 런북·기존 클라이언트 호환)를 직접 서빙한다.
       그리퍼 계열 + ResetAllError 만 허용, 그 외 "-1:unsupported".
       SDK 호출은 read/write 루프와 뮤텍스 직렬화, MoveGripper 는 항상
       비블로킹(block=1) 강제 — ServoJ 8ms 스트림 굶김 방지.
       ★빌드 완료·실기 미적용: 내일 스택 재기동(전원 재투입 포함) 때 반영.
  ★MoveGripper 비동기 함정(8/12 비전 문서): 명령은 즉시 리턴한다(비블로킹 강제) —
    GetGripperMotionDone 폴링으로 완료 대기 필수. wait_done() 이 그 역할.
  fr5_gripper_mode: off(건주행: 스킵) | service(기본, 플랜B) | rpc(참고용·폐기 경로)

■ 자세 소스 = ~/fr5_data/poses.json (fr5_pose.py 의 티칭 DB, 관절각 rad 저장)
  ★현재 미티칭 상태 — 아래 6개 자세를 fr5_pose.py save 로 티칭해야 실기 구동 가능:
    observe(관찰) / pick_hover(집기 위 150mm 호버) / pick(집기) /
    reorient(자세 전환) / place_hover(삽입 위 호버) / insert(삽입)
  150mm 호버 경유·수직 접근자세 고정·저속 5~10% 는 티칭 자세에 반영한다
  (8/12 비전 pick&place 확립 패턴).

■ 오류코드 매핑 (계약 v0.2 §6 — FR5 계열 E1xx)
  E101 파지 실패(→HELD)  : 그리퍼 명령/완료실패, PICK phase 이동·계획 실패
  E102 삽입 걸림(→HELD)  : INSERT phase 실행 실패·타임아웃 (압입 금지, 관리자 확인)
  E103 통신 두절(→ABORTED): 액션서버/joint_states 부재, 그리퍼 RPC 연결 실패,
                            그 외 phase 이동 실패, 자세 미티칭, 모르는 phase
  ⚠계약 갭: FR5 '계획 실패' 전용 코드가 §6 에 없다 — E103 으로 임시 매핑(M5 제안 예정).
"""
import json
import math
import os
import time

# ---------------- 상수 (fr5_pose.py · 8/12 비전 실증값과 정합) ----------------
GROUP = "fairino5_v6_group"
JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
POSE_FILE = "/home/ar/fr5_data/poses.json"
MOVE_ACTION = "/move_action"
JS_TOPIC = "/joint_states"          # real_robot 스택 발행 (j1~j6 + mock finger_*)

SCALE_DEFAULT = 0.10                # 통상 이동 10% (티칭 재현 저속 원칙)
SCALE_SLOW = 0.05                   # 접근·삽입·후퇴 5%
GOAL_TOL_RAD = 0.01
SETTLE_TOL_RAD = math.radians(0.05)
SETTLE_COUNT = 5                    # 연속 5회 변화 없음 = 정지
SETTLE_TIMEOUT = 90.0
RESULT_TIMEOUT = 180.0

GRIPPER_ID = 1                      # 8/12 비전 실증값 그대로
ALARM_ERRNO = 73                    # 8/13 실측: 컨트롤러 알람 잔류 시 그리퍼 명령 차단
GRIPPER_OPEN = 100
GRIPPER_CLOSE = 15                  # 8/13 실측 보정: 0=완전닫힘·100=열림, 20에서 1cm 틈 → 15로 파지
                                    # (구값 80은 방향 착오 — 80이면 거의 열린 상태)
GRIPPER_SPEED = 10                  # 8/14: 속도 5 는 100→15 가 10s (타임아웃 12s 대비
                                    # 마진 2s 뿐) → 10 으로. force 는 그대로라 파지 세기 불변
GRIPPER_FORCE = 15                  # 저항 느끼면 멈추도록 낮게 = 살살 잡기
GRIPPER_MAXTIME_MS = 20000          # ★8/14 진범: 8000 은 "여유값"이 아니라 알람 트리거였다.
                                    # vel5 에서 100→15 가 10s → 명령 +8.000s 에 컨트롤러
                                    # 알람 → ServoJ 스트림 사망(오류 14 연속) → 이후 모든
                                    # 그리퍼 명령 errno=73. (8/13 "maxtime 은 무관" 은 오판)
                                    # 반드시 실제 풀스트로크 시간보다 넉넉히 클 것.
GRIPPER_DONE_TIMEOUT = 20.0         # 8/13 실측: 20→100 이 속도5에서 5s 초과 → 완화.
                                    # 8/14: 100→15 실측 10s → 12s 는 마진 부족, 20s 로

# phase → 실행 계획. ("move", 자세이름, 속도스케일) | ("grip", 목표위치%)
PHASE_PLAN = {
    "OBSERVE":        (("move", "observe", SCALE_DEFAULT),),
    "PICK":           (("move", "pick_hover", SCALE_DEFAULT),   # 150mm 호버 경유
                       ("grip", GRIPPER_OPEN),
                       ("move", "pick", SCALE_SLOW),            # 수직 하강 저속
                       ("grip", GRIPPER_CLOSE),                 # 파지 + 완료 대기
                       ("move", "pick_hover", SCALE_SLOW)),     # 들어올림
    "REORIENT":       (("move", "reorient", SCALE_DEFAULT),),
    "PLACE_APPROACH": (("move", "place_hover", SCALE_DEFAULT),),
    "INSERT":         (("move", "insert", SCALE_SLOW),),
    "RETREAT":        (("grip", GRIPPER_OPEN),                  # 놓고
                       ("move", "place_hover", SCALE_SLOW)),    # 위로 후퇴
}
REQUIRED_POSES = ("observe", "pick_hover", "pick",
                  "reorient", "place_hover", "insert")

# phase 별 이동·계획 실패 코드 (기본 E103)
PHASE_FAIL_CODE = {"PICK": "E101", "INSERT": "E102"}


class Fr5Error(Exception):
    """cell_orchestrator.CellError 와 같은 (code, detail) 꼴. 단독 실행/테스트용."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _unpack(res):
    """XML-RPC 응답을 리스트로 정규화. SDK out-param 꼴이 배열/스칼라 어느 쪽으로
    와도 첫 원소 = errno 로 다루기 위함 (probe 로 실제 꼴 확인 후 필요시 조정)."""
    if isinstance(res, (list, tuple)):
        return list(res)
    return [res]


class _GripperCompletion:
    """MoveGripper 비동기 함정 공용 처리 — motion_done() 을 가진 백엔드용."""

    def wait_done(self, timeout=GRIPPER_DONE_TIMEOUT, poll=0.1):
        """완료 폴링. fault/타임아웃 = E101."""
        t0 = time.monotonic()
        while True:
            fault, status = self.motion_done()
            if fault not in (0, None) and fault != 0:
                raise self.exc("E101", f"그리퍼 fault={fault}")
            if status in (1, 2, 3):   # 8/26: 2=물체 잡고 정지(파지 성공), 3=물체 놓침 — 둘 다 '동작 종료'
                return
            if time.monotonic() - t0 > timeout:
                raise self.exc("E101", f"그리퍼 완료 대기 타임아웃 {timeout}s "
                                       f"(마지막 status={status})")
            time.sleep(poll)


class Fr5Gripper(_GripperCompletion):
    """[참고용·폐기 경로] 컨트롤러 XML-RPC(8080, /RPC2) 직결 클라이언트.

    ★8/13 probe 실기 실패 확정 — hardware plugin 세션이 8080 RPC 서버를 점유해
    제2 클라이언트는 무응답. 플랜B(Fr5GripperService)로 대체됐다. 컨트롤러 FW
    변경 등으로 재시도할 때를 위해 보존 (fr5_gripper_mode:=rpc).
    """

    def __init__(self, ip="192.168.58.2", port=8080, http_timeout=3.0,
                 exc=Fr5Error, index=GRIPPER_ID):
        self.ip, self.port, self.http_timeout = ip, port, float(http_timeout)
        self.exc, self.index = exc, index

    # 테스트에서 이 메서드만 갈아끼우면 전 경로가 mock 된다
    def _call(self, method, *args):
        import xmlrpc.client

        class _T(xmlrpc.client.Transport):
            def __init__(self, timeout):
                super().__init__()
                self._timeout = timeout

            def make_connection(self, host):
                conn = super().make_connection(host)
                conn.timeout = self._timeout
                return conn

        url = f"http://{self.ip}:{self.port}/RPC2"
        try:
            with xmlrpc.client.ServerProxy(
                    url, transport=_T(self.http_timeout), allow_none=True) as p:
                return getattr(p, method)(*args)
        except (OSError, xmlrpc.client.ProtocolError,
                xmlrpc.client.Fault, xmlrpc.client.ResponseError) as e:
            raise self.exc("E103", f"FR5 그리퍼 RPC {method} 실패 ({url}): {e}")

    def config(self):
        """read-only 프로브 — 실기 검증 1단계. (company,device,softversion,bus)"""
        return _unpack(self._call("GetGripperConfig"))

    def move(self, pos, vel=GRIPPER_SPEED, force=GRIPPER_FORCE,
             max_time_ms=GRIPPER_MAXTIME_MS):
        """MoveGripper — ★즉시 리턴함. 반드시 wait_done() 으로 완료 대기할 것."""
        res = _unpack(self._call(
            "MoveGripper", int(self.index), int(pos), int(vel), int(force),
            int(max_time_ms), 0, 0, 0.0, 0, 0))   # block=0, 평행 그리퍼
        errno = res[0]
        if isinstance(errno, int) and errno != 0:
            raise self.exc("E101", f"MoveGripper(pos={pos}) errno={errno}")
        return res

    def motion_done(self):
        """GetGripperMotionDone → (fault, status). status 1=완료."""
        res = _unpack(self._call("GetGripperMotionDone"))
        if len(res) >= 3:
            errno, fault, status = res[0], res[1], res[2]
        elif len(res) == 2:
            errno, (fault, status) = 0, res
        else:
            errno, fault, status = res[0], 0, None
        if isinstance(errno, int) and errno != 0:
            raise self.exc("E101", f"GetGripperMotionDone errno={errno}")
        return fault, status


class Fr5GripperService(_GripperCompletion):
    """★기본 경로 (플랜B): 개조 FairinoHardwareInterface 가 서빙하는
    /fairino_remote_command_service (fairino_msgs/srv/RemoteCmdInterface) 경유.

    cmd_str = "MoveGripper(1,80,5,15,2000,1,0,0,0,0)" 꼴 (런북과 동일 포맷),
    cmd_res = "errno" 또는 "errno,out1,out2,..." (cmd_server 관례) /
              "-1:..." = 플러그인 거부 사유.
    plugin 이 block=1 을 강제하므로 move() 후 wait_done() 폴링 필수.
    """

    SERVICE = "/fairino_remote_command_service"

    def __init__(self, node, exc=Fr5Error, index=GRIPPER_ID, client=None,
                 call_timeout=10.0):
        self.node, self.exc, self.index = node, exc, index
        self.call_timeout = float(call_timeout)
        if client is not None:
            self.cli = client
        else:
            from fairino_msgs.srv import RemoteCmdInterface
            self.cli = node.create_client(
                RemoteCmdInterface, self.SERVICE,
                callback_group=getattr(node, "adapter_cb", None))

    def _call(self, cmd_str):
        if not self.cli.wait_for_service(timeout_sec=3.0):
            raise self.exc("E103",
                           f"그리퍼 서비스 없음({self.SERVICE}) — 개조 "
                           f"fairino_hardware 미적용? (재빌드 후 스택 재기동 필요)")
        rq = self.cli.srv_type.Request()
        rq.cmd_str = cmd_str
        fut = self.cli.call_async(rq)
        t0 = time.monotonic()
        while not fut.done():
            if time.monotonic() - t0 > self.call_timeout:
                raise self.exc("E103", f"그리퍼 서비스 응답 타임아웃: {cmd_str}")
            time.sleep(0.02)
        return fut.result().cmd_res

    def _vals(self, cmd_str):
        res = str(self._call(cmd_str)).strip()
        if res.startswith("-1:"):
            raise self.exc("E101", f"{cmd_str} 거부: {res}")
        try:
            return [int(v) for v in res.split(",")]
        except ValueError:
            raise self.exc("E101", f"{cmd_str} 응답 파싱 실패: {res!r}")

    def config(self):
        """read-only 프로브: (errno, company, device, softversion, bus)"""
        return self._vals("GetGripperConfig()")

    def recover(self):
        """errno=73(컨트롤러 알람 잔류) 자동 복구.

        8/13 실측: 알람이 남아 있으면 그리퍼 명령이 errno=73 으로 막힌다.
        ResetAllError() 로 알람은 지워지지만 알람이 떨어뜨린 RobotEnable 이
        복구되지 않아 팔은 CONTROL_FAILED(-4) 가 된다 → Mode(0)+RobotEnable(1)
        까지 해야 완전 복구. 8/14 플러그인에 Mode/RobotEnable 을 추가해
        스택을 내리지 않고(=전원 재투입 없이) 여기서 끝낼 수 있게 됐다.
        """
        steps = []
        for cmd in ("ResetAllError()", "Mode(0)", "RobotEnable(1)"):
            res = str(self._call(cmd)).strip()
            steps.append(f"{cmd}={res}")
            if res.startswith("-1:"):
                raise self.exc("E101",
                               f"알람 자동복구 실패({cmd} 거부: {res}) — 플러그인에 "
                               f"Mode/RobotEnable 미반영(8/14 빌드) 상태로 보임")
        self.node.get_logger().warning(
            "[fr5] 그리퍼 알람 자동복구: " + " ".join(steps))
        return steps

    def move(self, pos, vel=GRIPPER_SPEED, force=GRIPPER_FORCE,
             max_time_ms=GRIPPER_MAXTIME_MS, _retry=True):
        """★즉시 리턴(plugin 이 block=1 강제) — 반드시 wait_done() 으로 완료 대기.

        errno=73 이면 recover() 후 1회만 재시도한다(무한루프 방지)."""
        vals = self._vals(f"MoveGripper({self.index},{int(pos)},{int(vel)},"
                          f"{int(force)},{int(max_time_ms)},1,0,0,0,0)")
        if vals[0] == ALARM_ERRNO and _retry:
            self.recover()
            return self.move(pos, vel, force, max_time_ms, _retry=False)
        if vals[0] != 0:
            raise self.exc("E101", f"MoveGripper(pos={pos}) errno={vals[0]}")
        return vals

    def motion_done(self):
        """(fault, status). status 1=완료."""
        vals = self._vals("GetGripperMotionDone()")
        if vals[0] != 0:
            raise self.exc("E101", f"GetGripperMotionDone errno={vals[0]}")
        fault = vals[1] if len(vals) > 1 else 0
        status = vals[2] if len(vals) > 2 else None
        return fault, status


class Fr5RealAdapter:
    """RealAdapter 의 fr5 위임 대상. run_phase(phase, part, run) / stop().

    node 요구사항: get_logger()/declare_parameter()/get_parameter()/
    create_subscription() (+ 실 ActionClient 생성 시 adapter_cb).
    action_client / gripper 주입 가능 (mock 테스트용).
    """

    def __init__(self, node, cell_error=Fr5Error, action_client=None,
                 gripper=None, pose_file=POSE_FILE):
        self.node, self.exc = node, cell_error
        self._declare(node)
        self.settle_poll = 0.1          # 테스트에서 축소 가능
        self.settle_timeout = SETTLE_TIMEOUT
        self._js = None                 # 최근 관절각 [j1..j6] rad
        self._active_gh = None

        from sensor_msgs.msg import JointState
        from moveit_msgs.action import MoveGroup
        from moveit_msgs.msg import Constraints, JointConstraint
        self._mg, self._constraints, self._jc = MoveGroup, Constraints, JointConstraint
        node.create_subscription(JointState, JS_TOPIC, self._on_js, 10)
        if action_client is not None:
            self.cli = action_client
        else:
            from rclpy.action import ActionClient
            self.cli = ActionClient(node, MoveGroup, MOVE_ACTION,
                                    callback_group=node.adapter_cb)
        self._injected_gripper = gripper      # 테스트 주입 시 모드 무관 사용
        self._grip_backends = {}              # mode -> 백엔드 (lazy)
        try:
            with open(pose_file) as f:
                self.poses = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.poses = {}             # 미티칭 → _goto 에서 E103

    @staticmethod
    def _declare(node):
        for name, default in (("fr5_gripper_mode", "service"),  # off=건주행 게이트
                              ("fr5_controller_ip", "192.168.58.2")):
            try:
                node.declare_parameter(name, default)
            except Exception:           # 이미 선언됨 (재기동 없이 재생성 등)
                pass

    def _on_js(self, msg):
        d = dict(zip(msg.name, msg.position))
        if all(j in d for j in JOINTS):
            self._js = [d[j] for j in JOINTS]

    # ---------------- phase 실행 ----------------
    def run_phase(self, phase, part, run):
        plan = PHASE_PLAN.get(phase)
        if plan is None:
            raise self.exc("E103", f"FR5 가 모르는 phase {phase}")
        for step in plan:
            if step[0] == "move":
                self._goto(step[1], step[2], phase)
            else:
                self._grip(step[1], phase)

    def stop(self):
        try:
            gh = self._active_gh
            if gh is not None:
                gh.cancel_goal_async()
        except Exception:
            pass

    # ---------------- 그리퍼 ----------------
    def _grip_backend(self, mode):
        if self._injected_gripper is not None:
            return self._injected_gripper
        b = self._grip_backends.get(mode)
        if b is None:
            if mode == "service":       # ★기본: 개조 hardware plugin 서비스
                b = Fr5GripperService(self.node, exc=self.exc)
            else:                       # "rpc" — 참고용 폐기 경로 (8/13 probe 실패)
                ip = self._param("fr5_controller_ip", "192.168.58.2")
                b = Fr5Gripper(ip=ip, exc=self.exc)
            self._grip_backends[mode] = b
        return b

    def _param(self, name, default):
        """★8/13 실기 픽스: 오케스트레이터가 declare 안 한 파라미터를 -p 로만
        넘기면 get_parameter 가 예외 → Result 빈 채 ABORTED (건주행 1회차 실증).
        실노드는 없으면 declare(커맨드라인 -p 오버라이드 반영), FakeNode 는 그대로."""
        if hasattr(self.node, "has_parameter") and not self.node.has_parameter(name):
            # ★CLI 함정: -p x:=off 는 YAML 이 BOOL(False) 로 파싱 → 문자열 기본값과
            #   타입 충돌로 declare 가 예외(건주행 2회차 실증). dynamic_typing 허용.
            from rcl_interfaces.msg import ParameterDescriptor
            self.node.declare_parameter(
                name, default, ParameterDescriptor(dynamic_typing=True))
        return self.node.get_parameter(name).value

    def _grip(self, pos, phase):
        mode = self._param("fr5_gripper_mode", "service")
        if isinstance(mode, bool):          # off/on → False/True 로 온 경우 정규화
            mode = "service" if mode else "off"
        if mode == "off":
            self.node.get_logger().warning(
                f"[fr5] 그리퍼 스킵(fr5_gripper_mode=off, 건주행) — "
                f"{phase} pos={pos}")
            return
        if mode not in ("service", "rpc"):
            raise self.exc("E103", f"모르는 fr5_gripper_mode {mode!r}")
        g = self._grip_backend(mode)
        g.move(pos)
        g.wait_done()                   # ★비동기 함정 — 완료까지 대기
        self.node.get_logger().info(f"[fr5] 그리퍼 {pos}% 완료 ({phase}, {mode})")

    # ---------------- MoveGroup 이동 ----------------
    def _fail_code(self, phase):
        return PHASE_FAIL_CODE.get(phase, "E103")

    def _goto(self, pose_name, scale, phase, timeout=RESULT_TIMEOUT):
        if pose_name not in self.poses:
            raise self.exc("E103",
                           f"FR5 티칭 자세 '{pose_name}' 없음 — "
                           f"fr5_pose.py save {pose_name} 로 티칭 후 재시도 "
                           f"(필요 자세: {', '.join(REQUIRED_POSES)})")
        targets = self.poses[pose_name]["joints"]

        if not self.cli.wait_for_server(timeout_sec=3.0):
            raise self.exc("E103", "FR5 /move_action 없음 — real_robot.launch.py "
                                   "기동·개통 순서 확인")
        goal = self._mg.Goal()
        goal.request.group_name = GROUP
        goal.request.num_planning_attempts = 3
        goal.request.allowed_planning_time = 5.0
        goal.request.max_velocity_scaling_factor = float(scale)
        goal.request.max_acceleration_scaling_factor = float(scale)
        c = self._constraints()
        for name, pos in zip(JOINTS, targets):
            jc = self._jc()
            jc.joint_name = name
            jc.position = float(pos)
            jc.tolerance_above = GOAL_TOL_RAD
            jc.tolerance_below = GOAL_TOL_RAD
            jc.weight = 1.0
            c.joint_constraints.append(jc)
        goal.request.goal_constraints.append(c)
        goal.planning_options.plan_only = False

        fut = self.cli.send_goal_async(goal)
        t0 = time.monotonic()
        while not fut.done():
            if time.monotonic() - t0 > 10.0:
                raise self.exc("E103", f"FR5 '{pose_name}' 골 접수 타임아웃")
            time.sleep(0.05)
        gh = fut.result()
        if gh is None or not gh.accepted:
            raise self.exc(self._fail_code(phase),
                           f"FR5 '{pose_name}' 골 거부(계획 실패/리밋 밖) [{phase}]")
        self._active_gh = gh
        try:
            rfut = gh.get_result_async()
            while not rfut.done():
                if time.monotonic() - t0 > timeout:
                    gh.cancel_goal_async()
                    raise self.exc(self._fail_code(phase),
                                   f"FR5 '{pose_name}' 이동 타임아웃 {timeout}s "
                                   f"[{phase}]")
                time.sleep(0.1)
            code = rfut.result().result.error_code.val
            if code != 1:                          # 1 = SUCCESS
                raise self.exc(self._fail_code(phase),
                               f"FR5 '{pose_name}' 이동 실패 code={code} [{phase}]"
                               + (" — 삽입 걸림 의심, 압입 금지" if phase == "INSERT"
                                  else ""))
        finally:
            self._active_gh = None
        self._wait_settled(pose_name)

    def _wait_settled(self, pose_name):
        """MoveGroup SUCCESS ≠ 실물 정지 (8/10 실측) — joint_states 안정 대기."""
        t0 = time.monotonic()
        prev, stable = None, 0
        while True:
            now = list(self._js) if self._js else None
            if now and prev:
                if max(abs(a - b) for a, b in zip(now, prev)) < SETTLE_TOL_RAD:
                    stable += 1
                    if stable >= SETTLE_COUNT:
                        return
                else:
                    stable = 0
            prev = now
            if time.monotonic() - t0 > self.settle_timeout:
                self.node.get_logger().warning(
                    f"[fr5] '{pose_name}' 정지 대기 타임아웃 — 진행 "
                    f"(joint_states 수신={'O' if now else 'X'})")
                return
            time.sleep(self.settle_poll)


# ---------------- 단독 실행: [참고용] 8080 직결 검증 도구 ----------------
def _main():
    """⚠8/13 probe 실기 실패 확정 — 이 rpc 직결 도구는 참고용으로만 보존.
    ★플랜B(기본 경로) 검증은 스택 재기동 후 ros2 CLI 로 (런북 포맷 그대로):
      ros2 service call /fairino_remote_command_service \\
        fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'GetGripperConfig()'}"
      → cmd_res "0,..." 이면 개조 plugin 서비스 정상. 이어서
        "{cmd_str: 'MoveGripper(1,100,5,15,2000,1,0,0,0,0)'}" (open) →
        "{cmd_str: 'GetGripperMotionDone()'}" 폴링 → "0,0,1" = 완료.
    """
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    ip = sys.argv[sys.argv.index("--ip") + 1] if "--ip" in sys.argv \
        else "192.168.58.2"
    g = Fr5Gripper(ip=ip)
    if cmd == "probe":
        print("GetGripperConfig (read-only) ...")
        print("  응답:", g.config())
        print("GetGripperMotionDone (read-only) ...")
        print("  응답 raw:", _unpack(g._call("GetGripperMotionDone")))
        print("★8080 제2 클라이언트 접속 허용 확인됨 — open/close 로 실동작 검증 가능")
    elif cmd in ("open", "close"):
        pos = GRIPPER_OPEN if cmd == "open" else GRIPPER_CLOSE
        print(f"MoveGripper pos={pos} ...", g.move(pos))
        g.wait_done()
        print("완료")
    else:
        print(__doc__)


if __name__ == "__main__":
    _main()
