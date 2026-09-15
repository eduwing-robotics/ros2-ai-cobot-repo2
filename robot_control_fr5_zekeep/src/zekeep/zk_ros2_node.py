#!/usr/bin/env python3
"""
zk_ros2_node.py - ZKBOT ROS2 노드 (패키지 빌드 없이 바로 실행)

    source /opt/ros/jazzy/setup.bash
    python3 ~/zk_ros2_node.py --ros-args -p robot:=zkbot1
    python3 ~/zk_ros2_node.py --ros-args -p robot:=zkbot2

  포트·조그비트는 robot 이름 하나로 zk_profiles 가 정한다(by-path 고정).
  ttyUSB 번호를 직접 쓰지 말 것 — 삽입 순서로 바뀌어 다른 로봇을 잡는다.
  꼭 강제해야 할 때만 -p port:=/dev/serial/by-path/...

퍼블리시
    /<robot>/joint_states   sensor_msgs/JointState   ← RViz·MoveIt2 가 그대로 먹는다 (라디안)
    /<robot>/status         std_msgs/String (JSON)   ← 리밋·급정지·원점·펌프 등 전체 상태
    /<robot>/ready          std_msgs/Bool            ← 원점 확보 + 급정지 해제 상태

서비스
    /<robot>/home           std_srvs/Trigger   원점복귀
    /<robot>/pump_on        std_srvs/Trigger
    /<robot>/pump_off       std_srvs/Trigger   (펌프 OFF + 밸브 1초 배기)
    /<robot>/refine_pick    std_srvs/Trigger   base_pick 정밀 다듬 (zk_refine, tol 0.15°)
    /<robot>/refine_place   std_srvs/Trigger   base_place 정밀 다듬 (carry 직후, 부품 문 채)
    /<robot>/stop           std_srvs/Trigger   모든 조그비트·출력 즉시 해제

액션 (MoveIt2 브릿지, 8/7 추가)
    /<robot>/zkbot_arm_controller/follow_joint_trajectory
        control_msgs/FollowJointTrajectory
    MoveIt2 Execute → 궤적에서 웨이포인트를 추려(관절 최대 15° 간격) 순서대로
    연속제어 조그로 하달한다. ★실시간 궤적 추종이 아니라 "웨이포인트 도착" 방식
    (6.6Hz 한계) — 축이 A1→A2→A3 순차로 움직이므로 경로가 계획과 정확히 일치하지
    않는다. 좁은 틈을 통과하는 계획은 실물에서 보장되지 않음을 전제로 쓸 것.

★ 설계 원칙
  ZKBOT은 피드백이 6.6Hz라 **실시간 토픽 제어(ros2_control)를 붙일 수 없다.**
  그래서 제어는 **서비스/액션 수준**으로만 노출하고, 토픽은 상태 보고 전용이다.
  joint_states 는 MoveIt2·RViz 시각화와 충돌검사에 그대로 쓸 수 있다.

★ 시리얼 대역 주의
  상태 폴링과 제어가 같은 링크를 공유한다. publish_hz 기본 2.0 을 크게 올리면
  제어에 쓸 여유가 없어진다.
"""
import json
import math
import struct
import subprocess
import sys
import threading

sys.path.insert(0, "/home/ar")

import rclpy                                              # noqa: E402
from rclpy.node import Node                               # noqa: E402
from rclpy.action import ActionServer, CancelResponse     # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor         # noqa: E402
from sensor_msgs.msg import JointState                    # noqa: E402
from std_msgs.msg import Bool, String                     # noqa: E402
from std_srvs.srv import Trigger                          # noqa: E402
from control_msgs.action import FollowJointTrajectory     # noqa: E402

from zkfx import ZK, A_D, PUMP, VALVE                     # noqa: E402
from zk_safety import JOINT_D, HOME_FLAG, JOINT_LIMITS, Guard, Abort  # noqa: E402
import zk_pose as ZP                                      # noqa: E402
import zk_profiles as ZPROF                               # noqa: E402

PY = "/home/ar/zkbot_venv/bin/python"
JOINT_NAMES = {"A1": "a1_joint", "A2": "a2_joint", "A3": "a3_joint"}
# 8/10 합의(frame_id 필수·workcell_frame 체계) + 계약 §8 트윈 데이터.
# 로봇 루트 링크명 = workcell_viz.launch.py 의 ZK1_ROOT/ZK2_ROOT 와 동일해야 한다.
# twin_bridge.py 가 FR5 /joint_states 에 base_link 를 채우는 것과 같은 원칙.
FRAME_IDS = {"zkbot1": "zk_base_link", "zkbot2": "zk2_base_link"}
XN = {0: "a1_limit", 1: "a2_limit", 2: "a3_limit",
      3: "start_btn", 4: "origin_btn", 5: "estop", 6: "infrared"}
MODE = {0: "STOP", 1: "MANUAL", 2: "AUTO"}
ALL_JOG = ZPROF.ALL_JOG


class ZkbotNode(Node):
    def __init__(self):
        super().__init__("zkbot")
        self.declare_parameter("robot", ZPROF.DEFAULT_ROBOT)
        self.declare_parameter("port", "")    # 비우면 프로파일의 by-path 포트
        self.declare_parameter("publish_hz", 2.0)
        self.declare_parameter("frame_id", "")   # 비우면 FRAME_IDS[robot]
        r = self.get_parameter("robot").value
        port_override = self.get_parameter("port").value
        hz = float(self.get_parameter("publish_hz").value)
        self.frame_id = (self.get_parameter("frame_id").value
                         or FRAME_IDS.get(r, "zk_base_link"))

        # ★ 2026-08-10 C1: 포트와 조그비트를 프로파일 하나로 묶는다.
        #   이전에는 노드가 port 파라미터를 받으면서도 서브프로세스에는 넘기지
        #   않았고, 그 스크립트들이 /dev/ttyUSB0 을 하드코딩해 **다른 로봇이
        #   움직였다**(8/8 실제 발생). 이제 프로파일이 유일한 출처다.
        try:
            self.prof = ZPROF.resolve(robot=r, port=port_override or None)
        except KeyError as e:
            raise RuntimeError(
                f"{e}\n  robot 파라미터는 프로파일 이름이어야 한다: "
                f"{list(ZPROF.PROFILES)}") from e
        ZP.apply_profile(self.prof)           # 조그비트·자세DB를 이 개체 것으로
        port = self.prof.port

        self.lock = threading.Lock()          # 시리얼은 한 번에 하나만
        self.busy = False                     # 외부 스크립트가 포트를 쓰는 중
        self.zk = ZK(port)
        if not self.zk.link():
            raise RuntimeError(f"PLC 링크 실패 ({r} {port})")
        self.get_logger().info(f"{r} 연결됨 ({port}), {hz}Hz 로 상태 발행")
        self.get_logger().info(
            "조그비트 " + "  ".join(
                f"{j}+{'+'.join(f'M{b}' for b in f)}"
                for j, (f, _) in self.prof.bits.items()))

        self.pub_js = self.create_publisher(JointState, f"/{r}/joint_states", 10)
        self.pub_st = self.create_publisher(String, f"/{r}/status", 10)
        self.pub_rd = self.create_publisher(Bool, f"/{r}/ready", 10)
        self.create_timer(1.0 / hz, self.tick)

        self.create_service(Trigger, f"/{r}/home", self.srv_home)
        self.create_service(Trigger, f"/{r}/carry", self.srv_carry)
        self.create_service(Trigger, f"/{r}/pump_on", self.srv_pump_on)
        self.create_service(Trigger, f"/{r}/pump_off", self.srv_pump_off)
        self.create_service(Trigger, f"/{r}/refine_pick", self.srv_refine_pick)
        self.create_service(Trigger, f"/{r}/refine_place", self.srv_refine_place)
        self.create_service(Trigger, f"/{r}/stop", self.srv_stop)

        # MoveIt2 브릿지 (긴 실행이라 별도 콜백그룹 — tick 은 논블로킹 lock 이라 안전)
        self._act_cb = ReentrantCallbackGroup()
        self.act_traj = ActionServer(
            self, FollowJointTrajectory,
            f"/{r}/zkbot_arm_controller/follow_joint_trajectory",
            execute_callback=self.exec_traj,
            cancel_callback=lambda req: CancelResponse.ACCEPT,
            callback_group=self._act_cb)

    # ---------- 상태 ----------
    def _ang(self, d):
        b = self.zk.read(A_D + d * 2, 4)
        return None if b is None else struct.unpack("<f", b)[0]

    def tick(self):
        if self.busy or not self.lock.acquire(blocking=False):
            return
        try:
            deg = {j: self._ang(d) for j, d in JOINT_D.items()}
            xs = self.zk.x()
            ys = self.zk.y()
            ms = self.zk.m(32)
            mode = MODE.get(self.zk.d(90), "?")
        finally:
            self.lock.release()

        now = self.get_clock().now().to_msg()
        js = JointState()
        js.header.stamp = now
        js.header.frame_id = self.frame_id   # 8/10 합의: frame_id 필수
        for j in ("A1", "A2", "A3"):
            if deg[j] is None:
                continue
            js.name.append(JOINT_NAMES[j])
            js.position.append(math.radians(deg[j]))   # ROS 관례: 라디안
        self.pub_js.publish(js)

        homed = {j: (b in ms) for j, b in HOME_FLAG.items()} if ms else None
        ready = bool(homed and all(homed.values()) and xs is not None and 5 not in xs)
        self.pub_rd.publish(Bool(data=ready))

        st = dict(self.zk.stats)
        tot = st["ok"] + st["csum"] + st["noresp"]
        self.pub_st.publish(String(data=json.dumps({
            "robot": self.prof.name,
            "port": self.prof.port,       # 어느 개체를 잡았는지 진단용
            "online": xs is not None,
            "ready": ready,
            "busy": self.busy,
            "joints_deg": {j: (None if v is None else round(v, 3))
                           for j, v in deg.items()},
            "joints_in_range": {
                j: (None if deg[j] is None else
                    JOINT_LIMITS[j][0] <= deg[j] <= JOINT_LIMITS[j][1])
                for j in deg},
            "homed": homed,
            "inputs": ({XN[i]: (i in xs) for i in XN} if xs is not None else None),
            "estop": (5 in xs) if xs is not None else None,
            "pump": (PUMP in ys) if ys is not None else None,
            "valve": (VALVE in ys) if ys is not None else None,
            "mode": mode,
            "link_quality_pct": (st["ok"] * 100 // tot) if tot else None,
        }, ensure_ascii=False)))

    # ---------- MoveIt2 브릿지 ----------
    WAYPOINT_MAX_DEG = 15.0     # 이보다 큰 관절 변화는 중간 웨이포인트를 실행
    FINAL_TOL_DEG = 0.5         # 최종 허용오차 (실측 반복정밀도 ±0.3°)
    MID_TOL_DEG = 2.0           # 중간 웨이포인트 허용오차 (느슨)

    @staticmethod
    def _pick_waypoints(points, name_idx):
        """궤적에서 실행할 웨이포인트 선별 — 연속 실행점 간 관절 최대변화 ≤ WAYPOINT_MAX_DEG."""
        if not points:
            return []
        chosen = []
        last = points[0]
        for pt in points[1:]:
            dmax = max(abs(math.degrees(pt.positions[i] - last.positions[i]))
                       for i in name_idx.values())
            if dmax >= ZkbotNode.WAYPOINT_MAX_DEG:
                chosen.append(pt)
                last = pt
        final = points[-1]
        if not chosen or chosen[-1] is not final:
            chosen.append(final)
        return chosen

    def exec_traj(self, goal_handle):
        traj = goal_handle.request.trajectory
        result = FollowJointTrajectory.Result()
        inv = {v: k for k, v in JOINT_NAMES.items()}          # a1_joint→A1
        try:
            name_idx = {inv[n]: i for i, n in enumerate(traj.joint_names) if n in inv}
        except KeyError:
            name_idx = {}
        if len(name_idx) != 3:
            result.error_code = FollowJointTrajectory.Result.INVALID_JOINTS
            result.error_string = f"관절명 불일치: {list(traj.joint_names)}"
            goal_handle.abort()
            return result

        wps = self._pick_waypoints(traj.points, name_idx)
        self.get_logger().info(
            f"MoveIt 궤적 수신: 원점 {len(traj.points)}점 → 실행 {len(wps)}웨이포인트")

        with self.lock:                    # 실행 동안 시리얼 독점 (tick 은 자동 스킵)
            self.busy = True
            try:
                ms = self.zk.m(32) or set()
                missing = [j for j, b in HOME_FLAG.items() if b not in ms]
                if missing:
                    raise Abort(f"원점 미확보: {missing}")
                guard = Guard(self.zk, homing=False)
                guard.preflight(ZP.angles(self.zk))

                for wi, pt in enumerate(wps):
                    if goal_handle.is_cancel_requested:
                        goal_handle.canceled()
                        result.error_string = "취소됨"
                        return result
                    last = (wi == len(wps) - 1)
                    tol = self.FINAL_TOL_DEG if last else self.MID_TOL_DEG
                    for j in ("A1", "A2", "A3"):
                        tgt = math.degrees(pt.positions[name_idx[j]])
                        ZP.goto_axis(self.zk, guard, j, tgt, tol)
                    fb = FollowJointTrajectory.Feedback()
                    fb.joint_names = list(traj.joint_names)
                    goal_handle.publish_feedback(fb)
                    self.get_logger().info(f"  웨이포인트 {wi+1}/{len(wps)} 도달")

                result.error_code = FollowJointTrajectory.Result.SUCCESSFUL
                goal_handle.succeed()
            except Abort as e:
                self.get_logger().error(f"브릿지 안전 중단: {e}")
                result.error_code = FollowJointTrajectory.Result.PATH_TOLERANCE_VIOLATED
                result.error_string = str(e)
                goal_handle.abort()
            except Exception as e:                      # noqa: BLE001
                self.get_logger().error(f"브릿지 실행 실패: {e}")
                result.error_string = str(e)
                goal_handle.abort()
            finally:
                ZP.all_off(self.zk)
                self.busy = False
        return result

    # ---------- 서비스 ----------
    def _run_script(self, script, *args, timeout=180):
        """움직이는 작업은 검증된 스크립트에 위임한다.
        같은 포트를 두 프로세스가 열면 안 되므로 우리 링크를 잠시 닫는다.

        ★ 포트를 반드시 인계한다. 이걸 빠뜨려서 8/8에 /zkbot1/home 이
          2호기를 원점복귀시켰다(서브프로세스가 ttyUSB0 하드코딩).
        """
        cmd = ([PY, f"/home/ar/{script}"] + list(args)
               + ["--robot", self.prof.name, "--port", self.prof.port])
        with self.lock:
            self.busy = True
            self.zk.close()
            try:
                self.get_logger().info(f"위임 실행: {script} → {self.prof.name}")
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                lines = [l for l in p.stdout.splitlines() if l.strip()]
                # 서브프로세스가 실제로 연 포트를 남긴다 — 인계가 됐는지
                # 로그만 보고 판정할 수 있어야 한다(8/8 사고의 재발 감시).
                if lines:
                    self.get_logger().info(f"  서브프로세스: {lines[0]}")
                return p.returncode == 0, " | ".join(lines[-3:])
            except Exception as e:
                return False, str(e)
            finally:
                self.zk = ZK(self.prof.port)
                self.zk.link()
                self.busy = False

    def srv_home(self, req, res):
        self.get_logger().info("원점복귀 요청")
        res.success, res.message = self._run_script("zk_home_run.py", "--force")
        return res

    def srv_carry(self, req, res):
        """정밀 운반 pick→lift→회전→place (zk_carry.py, 2026-08-11).
        부품 파지 구간 전용 — 2단계 스텝·A2/A3 교대로 부품 밀림 최소화.
        저속(5%) + 대이동이라 타임아웃을 길게 잡는다."""
        self.get_logger().info("정밀 운반 요청 (zk_carry)")
        res.success, res.message = self._run_script("zk_carry.py", timeout=600)
        return res

    # ── 자세 정밀 다듬 (2026-08-12 실물 확립) ──────────────────────────
    #   pick: FJT 도달 tol 0.5° 로 base_pick 에 도착하면 흡착판이 부품 위
    #     수 mm 떠서 **헛흡착**이 난다 — 진공 피드백이 없어 시스템은
    #     SUCCEEDED 로 완주해버린다.
    #   place: zk_carry 자체 종료 잔차가 A2/A3 +0.35~0.45° 로 남는다
    #     (마지막 0.5° 스텝 구간도 5% 관성에 걸림) → 삽입 뒤틀림.
    #     부품 문 채 다듬는 것이 8/12 삽입 성공의 결정적 요인.
    #
    #   ★위임 대상은 zk_axis 가 아니라 zk_refine.py 다. zk_axis(goto_axis)
    #     는 **잔차가 감속여유(lead) 안이면 phase 를 통째로 skip** 해서
    #     5%·lead 0.415° 로는 goto 착지 잔차(0.2~0.35°)에 손도 못 댄다
    #     (8/12 실증: 3축 전부 무동작 종료 = 다듬한 척). zk_refine 은
    #     통신지연 150ms 관성(5%≈0.42° / 2%≈0.17°)에 맞춰 속도를 낮춰가며
    #     왕복 수렴시킨다 → 실측 잔차 0.01~0.15°.
    #   각도는 하드코딩하지 않는다 — 자세DB(poses.json)가 출처(zk_refine 이 읽음).
    REFINE_TOL = 0.15           # °

    def _refine(self, pose, res):
        """자세DB의 pose 로 정밀 다듬 (zk_refine.py 위임)."""
        db = ZP.load()          # 자세DB = apply_profile 로 이 개체 것 (단일 출처)
        if pose not in db:
            res.success = False
            res.message = (f"티칭 자세 '{pose}' 없음 — "
                           f"zk_pose.py save 후 재시도 ({ZP.POSE_FILE})")
            return res
        self.get_logger().info(
            f"{pose} 정밀 다듬 요청 (zk_refine, tol {self.REFINE_TOL:g}°)")
        res.success, res.message = self._run_script(
            "zk_refine.py", "--pose", pose,
            "--tol", f"{self.REFINE_TOL:g}", timeout=300)
        return res

    def srv_refine_pick(self, req, res):
        return self._refine("base_pick", res)

    def srv_refine_place(self, req, res):
        return self._refine("base_place", res)

    def srv_pump_on(self, req, res):
        with self.lock:
            self.zk.y_off(VALVE)
            res.success = self.zk.y_on(PUMP)
        res.message = "펌프 ON" if res.success else "펌프 ON 실패"
        return res

    def srv_pump_off(self, req, res):
        import time
        with self.lock:
            self.zk.y_off(PUMP)
            time.sleep(0.3)
            self.zk.y_on(VALVE)
            time.sleep(1.0)          # 常开형이라 10초를 넘기지 않는다
            self.zk.y_off(VALVE)
            res.success = True
        res.message = "펌프 OFF + 밸브 1초 배기"
        return res

    def srv_stop(self, req, res):
        with self.lock:
            for _ in range(2):
                for b in ALL_JOG:
                    self.zk.m_off(b)
            self.zk.y_off(PUMP)
            self.zk.y_off(VALVE)
            res.success = True
        res.message = "조그비트·출력 전부 해제"
        self.get_logger().warn("STOP — 전 출력 해제")
        return res


def main():
    rclpy.init()
    node = None
    try:
        node = ZkbotNode()
        rclpy.spin(node, executor=MultiThreadedExecutor(num_threads=3))
    except KeyboardInterrupt:
        pass
    finally:
        if node:
            try:
                for _ in range(2):
                    for b in ALL_JOG:
                        node.zk.m_off(b)
                node.zk.y_off(PUMP)
                node.zk.y_off(VALVE)
                node.zk.close()
            except Exception:
                pass
        rclpy.shutdown()


if __name__ == "__main__":
    main()
