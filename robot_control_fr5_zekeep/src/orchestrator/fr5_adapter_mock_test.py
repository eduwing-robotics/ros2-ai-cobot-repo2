#!/usr/bin/env python3
"""fr5_adapter_mock_test.py — Fr5RealAdapter 순수 mock 단위테스트 (rclpy init 없음)

★실기 스택이 도메인 73 에 떠 있어도 안전: 노드 생성·DDS 참가·네트워크 접속 일절 없음.
  (moveit_msgs/sensor_msgs 메시지 클래스 import 만 사용 — init 불필요)

실행:
    source /opt/ros/jazzy/setup.bash
    python3 ~/fr5_adapter_mock_test.py

검증 범위 = 8/11~12 mock 29/29 방식 재사용: 호출 순서·목표 각도·스케일 대조,
오류코드 매핑(E101/E102/E103), 게이트(fr5_gripper_mode) 동작, 그리퍼 RPC 파싱.
실제 이동은 검증하지 못한다 — 실물 검증은 별도 런북(단계 게이트) 절차로.
"""
import json
import math
import os
import sys
import tempfile
from types import SimpleNamespace as NS

sys.path.insert(0, "/home/ar")
import fr5_real_adapter as FRA
from fr5_real_adapter import (Fr5RealAdapter, Fr5Gripper, Fr5GripperService,
                              Fr5Error, _unpack)

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  🔴 {name}  {detail}")


def expect_err(name, code, fn, substr=""):
    try:
        fn()
    except Fr5Error as e:
        check(name, e.code == code and substr in e.detail,
              f"got {e.code}: {e.detail}")
        return
    check(name, False, "예외 없음")


# ---------------- fakes ----------------
class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(("info", m))

    def warning(self, m):
        self.lines.append(("warning", m))

    def error(self, m):
        self.lines.append(("error", m))


class FakeNode:
    def __init__(self):
        self.params, self.subs = {}, []
        self.logger = FakeLogger()
        self.adapter_cb = None

    def declare_parameter(self, name, default):
        self.params.setdefault(name, default)

    def get_parameter(self, name):
        return NS(value=self.params[name])

    def create_subscription(self, t, topic, cb, depth):
        self.subs.append((topic, cb))

    def get_logger(self):
        return self.logger


class FakeFuture:
    def __init__(self, result):
        self._r = result

    def done(self):
        return True

    def result(self):
        return self._r


class FakeGoalHandle:
    def __init__(self, accepted=True, error_code=1, result_done=True):
        self.accepted = accepted
        self.cancelled = False
        self._res = NS(result=NS(error_code=NS(val=error_code)))
        self._result_done = result_done

    def get_result_async(self):
        if not self._result_done:          # 영원히 안 끝나는 실행 (타임아웃 재현)
            return NS(done=lambda: False)
        return FakeFuture(self._res)

    def cancel_goal_async(self):
        self.cancelled = True


class FakeActionClient:
    def __init__(self, server_up=True, accepted=True, error_code=1,
                 result_done=True):
        self.server_up, self.accepted = server_up, accepted
        self.error_code, self.result_done = error_code, result_done
        self.goals = []

    def wait_for_server(self, timeout_sec):
        return self.server_up

    def send_goal_async(self, goal):
        self.goals.append(goal)
        return FakeFuture(FakeGoalHandle(self.accepted, self.error_code,
                                         self.result_done))


class FakeGripper:
    def __init__(self):
        self.calls = []

    def move(self, pos, **kw):
        self.calls.append(("move", pos))

    def wait_done(self, *a, **kw):
        self.calls.append(("wait",))


# ---------------- 공통 준비 ----------------
POSES = {n: {"joints": [round(math.radians(10 * i + k), 6)
                        for k in range(6)], "saved": "t"}
         for i, n in enumerate(FRA.REQUIRED_POSES)}
_tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                   dir=os.environ.get("TMPDIR") or None)
json.dump(POSES, _tmp)
_tmp.close()
RUN = NS(req_id="t", current_item=1)


def make_adapter(node=None, cli=None, grip=None, mode="service",
                 pose_file=_tmp.name):
    node = node or FakeNode()
    cli = cli or FakeActionClient()
    grip = grip if grip is not None else FakeGripper()
    a = Fr5RealAdapter(node, cell_error=Fr5Error, action_client=cli,
                       gripper=grip, pose_file=pose_file)
    node.params["fr5_gripper_mode"] = mode
    a.settle_poll, a.settle_timeout = 0.001, 0.05
    a._js = [0.0] * 6                     # 정지 상태 흉내 (settle 즉시 통과)
    return a, node, cli, grip


def goal_positions(goal):
    return [jc.position for jc in goal.request.goal_constraints[0].joint_constraints]


print("=== 1. phase 실행 계획 (호출 순서·각도·스케일) ===")
a, node, cli, grip = make_adapter()
a.run_phase("PICK", {}, RUN)
check("PICK 이동 3회(hover→pick→hover)", len(cli.goals) == 3)
check("PICK 그리퍼 open→close + 완료대기 2회",
      grip.calls == [("move", FRA.GRIPPER_OPEN), ("wait",),
                     ("move", FRA.GRIPPER_CLOSE), ("wait",)], str(grip.calls))
check("PICK 목표각 = 티칭 DB(rad) 일치",
      goal_positions(cli.goals[0]) == POSES["pick_hover"]["joints"]
      and goal_positions(cli.goals[1]) == POSES["pick"]["joints"])
check("PICK 하강·상승 저속 5%, 호버 접근 10%",
      abs(cli.goals[0].request.max_velocity_scaling_factor - 0.10) < 1e-9
      and abs(cli.goals[1].request.max_velocity_scaling_factor - 0.05) < 1e-9
      and abs(cli.goals[2].request.max_velocity_scaling_factor - 0.05) < 1e-9)
check("그룹·플랜 설정(fairino5_v6_group, plan_only=False)",
      cli.goals[0].request.group_name == "fairino5_v6_group"
      and cli.goals[0].planning_options.plan_only is False)

a, node, cli, grip = make_adapter()
for ph in ("OBSERVE", "PICK", "REORIENT", "PLACE_APPROACH", "INSERT", "RETREAT"):
    a.run_phase(ph, {}, RUN)
check("전체 6 phase 이동 8회·그립 3회 완주",
      len(cli.goals) == 8 and len([c for c in grip.calls if c[0] == "move"]) == 3)
check("RETREAT = 놓기(open) 후 후퇴", grip.calls[-2:] == [("move", FRA.GRIPPER_OPEN),
                                                          ("wait",)])

print("=== 2. 게이트·오류코드 매핑 ===")
a, node, cli, grip = make_adapter(mode="off")
a.run_phase("PICK", {}, RUN)
check("gripper off = 실동작 0건 + 이동은 수행",
      grip.calls == [] and len(cli.goals) == 3)
check("off 스킵 warning 로그",
      any("그리퍼 스킵" in m for lv, m in node.logger.lines if lv == "warning"))

a, node, cli, grip = make_adapter(mode="webapp")
expect_err("모르는 fr5_gripper_mode → E103", "E103",
           lambda: a.run_phase("PICK", {}, RUN), "fr5_gripper_mode")

a, *_ = make_adapter(pose_file="/nonexistent.json")
expect_err("미티칭 자세 → E103 + 티칭 안내", "E103",
           lambda: a.run_phase("OBSERVE", {}, RUN), "fr5_pose.py save")

a, *_ = make_adapter(cli=FakeActionClient(server_up=False))
expect_err("/move_action 부재 → E103", "E103",
           lambda: a.run_phase("OBSERVE", {}, RUN), "real_robot")

a, *_ = make_adapter(cli=FakeActionClient(accepted=False))
expect_err("골 거부(OBSERVE) → E103", "E103",
           lambda: a.run_phase("OBSERVE", {}, RUN), "골 거부")
a, *_ = make_adapter(cli=FakeActionClient(accepted=False))
expect_err("골 거부(PICK) → E101 파지 실패", "E101",
           lambda: a.run_phase("PICK", {}, RUN))
a, *_ = make_adapter(cli=FakeActionClient(error_code=-4))
expect_err("실행 실패(INSERT) → E102 삽입 걸림·압입 금지", "E102",
           lambda: a.run_phase("INSERT", {}, RUN), "압입 금지")
a, *_ = make_adapter(cli=FakeActionClient(error_code=-4))
expect_err("실행 실패(PLACE_APPROACH) → E103", "E103",
           lambda: a.run_phase("PLACE_APPROACH", {}, RUN))
a, *_ = make_adapter()
expect_err("모르는 phase → E103", "E103",
           lambda: a.run_phase("SUCTION", {}, RUN))

print("=== 3. stop = 활성 골 취소 ===")
a, *_ = make_adapter()
gh = FakeGoalHandle()
a._active_gh = gh
a.stop()
check("stop() 이 cancel_goal_async 호출", gh.cancelled)

print("=== 4. Fr5Gripper XML-RPC (전송부 mock) ===")


class MockGripper(Fr5Gripper):
    def __init__(self, responses):
        super().__init__(exc=Fr5Error)
        self.responses, self.sent = list(responses), []

    def _call(self, method, *args):
        self.sent.append((method, args))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise Fr5Error("E103", f"FR5 그리퍼 RPC {method} 실패: {r}")
        return r


check("_unpack 스칼라/배열 정규화",
      _unpack(0) == [0] and _unpack([0, 1, 2]) == [0, 1, 2])

g = MockGripper([0])
g.move(80)
check(f"MoveGripper 인자(1,80,{FRA.GRIPPER_SPEED},{FRA.GRIPPER_FORCE},"
      f"{FRA.GRIPPER_MAXTIME_MS},block=0,평행,0.0,0,0)",
      g.sent[0] == ("MoveGripper", (1, 80, FRA.GRIPPER_SPEED, FRA.GRIPPER_FORCE,
                                    FRA.GRIPPER_MAXTIME_MS, 0, 0, 0.0, 0, 0)),
      str(g.sent[0]))

g = MockGripper([4])
expect_err("MoveGripper errno≠0 → E101", "E101", lambda: g.move(100), "errno=4")

g = MockGripper([OSError("connection refused")])
expect_err("RPC 연결 실패 → E103", "E103", lambda: g.move(100), "RPC")

g = MockGripper([[0, 0, 0], [0, 0, 0], [0, 0, 1]])
g.wait_done(timeout=1.0, poll=0.001)
check("wait_done: status 1 까지 폴링(3회)", len(g.sent) == 3
      and all(m == "GetGripperMotionDone" for m, _ in g.sent))

g = MockGripper([[0, 1, 0]])
expect_err("그리퍼 fault → E101", "E101",
           lambda: g.wait_done(timeout=1.0, poll=0.001), "fault")

g = MockGripper([[0, 0, 0]] * 500)
expect_err("완료 대기 타임아웃 → E101", "E101",
           lambda: g.wait_done(timeout=0.02, poll=0.001), "타임아웃")

g = MockGripper([[0, 1]])                 # (fault,status) 2원소 응답 변형
check("motion_done 2원소 응답 파싱", g.motion_done() == (0, 1))

print("=== 5. Fr5GripperService (플랜B 기본 경로, 서비스 클라이언트 mock) ===")


class FakeSrvClient:
    """rclpy Client 흉내: srv_type.Request()/wait_for_service/call_async."""

    class _Req:
        cmd_str = ""

    srv_type = NS(Request=_Req)

    def __init__(self, responses, up=True):
        self.responses, self.up, self.sent = list(responses), up, []

    def wait_for_service(self, timeout_sec):
        return self.up

    def call_async(self, rq):
        self.sent.append(rq.cmd_str)
        return FakeFuture(NS(cmd_res=self.responses.pop(0)))


def make_srv_gripper(responses, up=True):
    c = FakeSrvClient(responses, up)
    return Fr5GripperService(FakeNode(), exc=Fr5Error, client=c), c


g, c = make_srv_gripper(["0"])
g.move(80)
check("service MoveGripper cmd_str = 런북 포맷 + block=1 강제",
      c.sent == [f"MoveGripper(1,80,{FRA.GRIPPER_SPEED},{FRA.GRIPPER_FORCE},"
                 f"{FRA.GRIPPER_MAXTIME_MS},1,0,0,0,0)"], str(c.sent))

g, c = make_srv_gripper(["0,0,0", "0,0,1"])
g.wait_done(timeout=1.0, poll=0.001)
check("service wait_done: GetGripperMotionDone 폴링 후 완료",
      c.sent == ["GetGripperMotionDone()"] * 2)

g, c = make_srv_gripper(["0,48,124,0,1"])
check("service config = read-only 프로브 파싱", g.config() == [0, 48, 124, 0, 1])

g, c = make_srv_gripper(["4"])
expect_err("service MoveGripper errno≠0 → E101", "E101",
           lambda: g.move(100), "errno=4")

# --- 8/14 신규: errno=73(알람 잔류) 자동복구 ---
g, c = make_srv_gripper(["73", "0", "0", "0", "0"])   # 73 → Reset/Mode/Enable → 재시도 성공
g.move(100)
check("errno73 → ResetAllError+Mode(0)+RobotEnable(1) 후 1회 재시도",
      c.sent[1:4] == ["ResetAllError()", "Mode(0)", "RobotEnable(1)"]
      and len(c.sent) == 5 and c.sent[4] == c.sent[0], str(c.sent))

g, c = make_srv_gripper(["73", "0", "0", "0", "73"])  # 복구해도 또 73 = 재시도 1회뿐
expect_err("errno73 재시도 후에도 73 → E101 (무한루프 없음)", "E101",
           lambda: g.move(100), "errno=73")

g, c = make_srv_gripper(["73", "-1:unsupported(...)"])  # 구버전 플러그인
expect_err("errno73 인데 Mode/RobotEnable 미반영 플러그인 → E101 안내", "E101",
           lambda: g.move(100), "8/14 빌드")

g, c = make_srv_gripper(["-1:unsupported(gripper_cmds_only:...)"])
expect_err("plugin 거부(-1:...) → E101", "E101",
           lambda: g.move(100), "거부")

g, c = make_srv_gripper(["garbage"])
expect_err("비정형 응답 → E101 파싱 실패", "E101",
           lambda: g.motion_done(), "파싱")

g, c = make_srv_gripper([], up=False)
expect_err("서비스 부재 → E103 (개조 plugin 미적용 안내)", "E103",
           lambda: g.move(100), "개조")

g, c = make_srv_gripper(["0,1,0"])
expect_err("service 그리퍼 fault → E101", "E101",
           lambda: g.wait_done(timeout=1.0, poll=0.001), "fault")

a, node, cli, grip = make_adapter(grip=None)     # 주입 없이 lazy 백엔드 확인
a._grip_backends["service"] = FakeGripper()      # 실제 생성 대신 사전 주입
a._injected_gripper = None
a.run_phase("RETREAT", {}, RUN)
check("adapter mode=service → service 백엔드 사용",
      a._grip_backends["service"].calls == [("move", FRA.GRIPPER_OPEN), ("wait",)])

# ---------------- 결과 ----------------
os.unlink(_tmp.name)
total = PASS + len(FAIL)
print(f"\n{'★' if not FAIL else '🔴'} 결과: {PASS}/{total} 통과"
      + (f"  실패: {FAIL}" if FAIL else ""))
sys.exit(0 if not FAIL else 1)
