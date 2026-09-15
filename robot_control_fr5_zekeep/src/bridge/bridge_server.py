"""
BF2 Robot Bridge — bf2_robot_console.html ↔ ZK1/ZK2/FR5

실행: pip install fastapi uvicorn --break-system-packages
      python bridge_server.py                 (mock)
      BF2_REAL=1 python bridge_server.py      (실기)

실기 경로 (2026-08-24, 기존 검증 자산 재사용)
  ZK  : zkfx.ZK 시리얼 직결 + zk_profiles(포트 단일출처) + zk_pose 프리미티브.
        move/jog 는 goto_axis 와 같은 레벨-제어(끊어치기 금지) + 2% 저속 수렴(zk_refine
        검증 경로)이며, GUI STOP 을 받을 수 있게 stop 이벤트 체크만 추가한 사본이다.
        원점복귀는 zk_home_run.py 서브프로세스(가드 포함)에 위임 — 포트는 닫았다 재연다.
  FR5 : SDK 직결 금지(cmd_server 가 SDK 점유, 단일 클라이언트 규칙) — ROS2 경로만.
        관절이동 = MoveIt /move_action (fr5_pose.py 와 동일 골 구성),
        그리퍼/알람복구 = /fairino_remote_command_service (fr5_real_adapter 의
        Fr5GripperService 재사용: max_time 20000ms, errno73 자동복구 내장).
        → 실기 전 필수: ~/fr5_up.sh 로 스택 기동 + ROS2 환경 source 된 셸에서 실행.

★ 병행 사용 금지: 브리지가 ZK 시리얼을 잡은 동안 zk_*.py CLI 를 직접 돌리지 말 것
  (같은 tty 에 두 프로세스가 쓰면 프레임이 섞인다). 브리지는 실기 명령을 처음 받을 때
  연결한다 — status 폴링만으로는 포트를 열지 않는다.
★ 진공 피드백 없음(E201 원리적 검출 불가): state 의 vacuum 은 "명령 누적값"이다.
★ FR5 TCP 좌표: GetActualTCPPose 는 cmd_server 점유라 미노출 — tcp 는 0 고정
  (트윈 FK 로 위치만 표시). 실측 필요 시 펜던트로 볼 것.

API (HTML 계약 — 콘솔 인수인계 문서 §3)
  GET  /status                         → {mode, robots:{...}}  (+robots[*].busy 추가)
  POST /{robot}/jog|move|home|stop|speed  /fr5/move_tcp|gripper  /zk?/vacuum   GET /log
  dry_run=true 면 검증·로그만. stop 은 dry 와 무관하게 항상 실행.
  실기 move/jog/home 은 즉시 "started" 를 돌려주고 백그라운드로 움직인다
  (mock 적분기와 같은 비동기 감각 — 완료는 status 폴링으로 본다). 동작 중 재명령 = 409.
"""
import os
import math
import signal
import subprocess
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

REAL = os.environ.get("BF2_REAL") == "1"
sys.path.insert(0, "/home/ar")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Busy(Exception):
    """동작 중 재명령 — 409 로 매핑."""


# ZK 소프트리밋 (zk_safety.JOINT_LIMITS 와 동일값 — mock 에서도 같은 검증을 받도록 복사.
#  실기는 _goto 안에서 zk_safety 원본으로 한 번 더 검사한다)
ZK_LIMITS = ((-190.0, 2.0), (-55.0, 2.0), (-110.0, 2.0))

# ---------------- MOCK ----------------
class MockRobot:
    def __init__(self, n, home):
        self.n, self.home = n, home
        self.j = list(home); self.tgt = list(home); self.speed = 5
        self.tcp = [0.0]*6; self.grip = 50; self.vac = False; self.manual = False
        self.connected = True
        threading.Thread(target=self._loop, daemon=True).start()
    def _loop(self):
        while True:
            v = 0.6*self.speed*0.05
            self.j = [t if abs(t-c) < v else c+(v if t > c else -v) for c, t in zip(self.j, self.tgt)]
            time.sleep(0.05)
    def jog(self, ax, d):        self.tgt[ax] = self.j[ax]+d
    def move(self, js):          self.tgt = list(js)
    def move_tcp(self, tcp):     self.tcp = list(tcp)
    def cart_jog(self, ax, d):
        if self.n != "fr5": raise NotImplementedError("cart_jog 는 FR5 전용")
        self.tcp[int(ax)] += float(d)
    def go_home(self):           self.tgt = list(self.home)
    def stop(self):              self.tgt = list(self.j)
    def set_speed(self, v):      self.speed = v
    def gripper(self, pos):      self.grip = pos
    def plugin(self, body=None):
        """9/1: 원시 SDK/플러그인 명령 1개 실행 — 진단·페이로드 등록용.
        조회(Get*/Is*)는 항상 허용, 그 외는 body.force=true 필요(오조작 방지).
        ⚠동작 중에는 거부(단일 클라이언트 원칙 + ServoJ 스트림 보호)."""
        body = body or {}
        cmd = str(body.get("cmd", "")).strip()
        if not cmd:
            raise RuntimeError("body.cmd 필요")
        readonly = cmd.startswith(("Get", "Is"))
        if not readonly and not body.get("force"):
            raise RuntimeError(f"쓰기 명령은 force=true 필요: {cmd}")
        if self.busy:
            raise Busy("fr5 동작 중 — 정지 후 재시도")
        self._ensure()
        return f"{cmd} = {str(self._svc()._call(cmd)).strip()}"

    def grip_read(self):         return self.grip
    def vacuum(self, on):        self.vac = on
    def lift(self, mm):
        if self.n.startswith("zk"):
            from zk_vlift import fk, ik
            r0, z0 = fk(self.j[1], self.j[2])
            a2, a3 = ik(r0, z0 + float(mm), self.j[1], self.j[2])
            self.tgt[1], self.tgt[2] = a2, a3
        else:
            raise NotImplementedError("리프트는 ZK 전용")
    def set_manual(self, on, body=None):  self.manual = bool(on)
    def state(self):
        d = {"connected": self.connected, "busy": False,
             "joints": [round(x, 3) for x in self.j]}
        if self.n == "fr5": d.update(tcp=self.tcp, gripper=self.grip, manual=self.manual)
        else: d.update(vacuum=self.vac)
        return d


# ---------------- ZK 실기 ----------------
class ZKReal:
    """ZEKEEP 3축 (FX3U PLC 시리얼).

    zk1/zk2 → zkbot 프로파일 매핑은 기본 zk1=zkbot1, zk2=zkbot2.
    ★배치가 바뀐 이력(8/20 하루 두 번)이 있으니 실기 전 어느 개체가 어느 셀에
      있는지 눈으로 확인하고, 다르면 BF2_ZK1_PROFILE/BF2_ZK2_PROFILE 로 바꿀 것.
    """
    def __init__(self, name, profile_name):
        self.name, self.profile_name = name, profile_name
        self.zk = None; self.prof = None
        self.speed = 5.0
        self.vac = False                     # 명령 누적값 — 진공 피드백 없음(E201 검출 불가)
        self._joints = [0.0, 0.0, 0.0]
        self._have_joints = False
        self._ser = threading.Lock()         # 시리얼은 한 번에 한 스레드만
        self._stop = threading.Event()
        self.busy = False
        self._proc = None                    # 원점복귀 서브프로세스
        self.connected = False

    # -- 연결 (실기 명령 첫 수신 시에만 — status 폴링으로는 포트를 열지 않는다) --
    def _ensure(self):
        if self.zk:
            return
        import zk_profiles as ZPROF
        from zkfx import ZK
        self.prof = ZPROF.get(self.profile_name)
        zk = ZK(self.prof.port)
        if not zk.link():
            zk.close()
            raise RuntimeError(f"{self.profile_name} 링크 실패 ({self.prof.port}) — "
                               f"전원/케이블/다른 프로세스 점유 확인")
        self.zk = zk
        self.connected = True
        rec(self.name, "connect", {}, f"{self.profile_name} {self.prof.port}")

    def _refresh(self):
        """관절각 캐시 갱신 (호출측이 _ser 를 쥔 상태여야 함)."""
        import zk_pose as P
        from zk_safety import JOINT_D
        a = {j: P.ang(self.zk, d) for j, d in JOINT_D.items()}
        if all(v is not None for v in a.values()):
            self._joints = [a["A1"], a["A2"], a["A3"]]
            self._have_joints = True

    def poll(self):
        """유휴 시 상태 갱신 — 동작 중이거나 미연결이면 건드리지 않는다."""
        if not self.zk or self.busy:
            return
        if self._ser.acquire(blocking=False):
            try:
                self._refresh()
            except Exception:
                pass
            finally:
                self._ser.release()

    # -- 백그라운드 동작 공통 --
    def _start(self, fn, label):
        if self.busy:
            raise Busy(f"{self.name} 동작 중 — STOP 후 재시도")
        self.busy = True
        self._stop.clear()
        def run():
            import zk_pose as P
            try:
                fn()
                rec(self.name, label, {}, "done")
            except Exception as e:
                self.last_err = {"t": time.time(), "msg": str(e)}
                rec(self.name, label, {}, f"ERR {e}")
            finally:
                try:
                    if self.zk:
                        with self._ser:
                            P.all_off(self.zk)   # 어떤 경우에도 조그비트 전량 OFF
                            self._refresh()
                except Exception:
                    pass
                self.busy = False
        threading.Thread(target=run, daemon=True).start()
        return "started"

    def _goto(self, joint, target, tol=0.3):
        """zk_pose.goto_axis 의 레벨-제어 루프 + STOP 이벤트 체크.

        phase = (조그속도%, 감속여유 lead) — 1차는 설정 속도, 이후 2% 저속 수렴 2회
        (zk_refine 8/12 실증: 5% 관성 0.42° → 2% 관성 0.17° 로 왕복 수렴).
        lead 공식 max(speed*0.083, 0.3) 은 zk_axis 실측 오버슛 계수 그대로.
        """
        import zk_pose as P
        from zk_safety import JOINT_D, JOINT_LIMITS, Abort
        zk = self.zk
        d = JOINT_D[joint]
        fwd, rev = self.prof.bits[joint]
        lo, hi = JOINT_LIMITS[joint]
        if not (lo <= target <= hi):
            raise Abort(f"{joint} 목표 {target:.2f}°가 소프트리밋({lo}~{hi}) 밖")
        spd = min(max(self.speed, 1.0), 30.0)      # D50 30% 초과는 미검증 — 상한 고정
        for pct, lead in ((spd, max(spd*0.083, 0.3)), (2.0, 0.17), (2.0, 0.17)):
            cur = P.ang(zk, d)
            if cur is None:
                raise Abort(f"{joint} 각도 읽기 실패")
            err = target - cur
            if abs(err) <= tol:
                break
            if abs(err) <= lead:                   # 이 속도로는 더 못 다가감 → 다음(저속) 단계
                continue
            P.set_speed(zk, pct)
            dps = max(1.0, (zk.d(2002) or 1000) / 177.78)
            bits = fwd if err > 0 else rev
            for b in bits:
                zk.m_on(b)
            t0, limit_s = time.time(), abs(err)/dps + 4.0
            try:
                while time.time() - t0 < limit_s:
                    if self._stop.is_set():
                        raise Abort("STOP 요청")
                    v = P.ang(zk, d)
                    if v is None:
                        continue
                    self._joints[("A1", "A2", "A3").index(joint)] = v   # GUI 실시간 반영
                    xs = zk.x() or set()
                    if 5 in xs:
                        raise Abort("X5(급정지) 감지")
                    if not (lo - 2 <= v <= hi + 2):
                        raise Abort(f"{joint} 소프트리밋 이탈 {v:.2f}°")
                    if abs(target - v) <= lead or (target - v > 0) != (err > 0):
                        break
            finally:
                for _ in range(2):
                    for b in bits:
                        zk.m_off(b)
            P.settle(zk, d)                        # 감속 정지 확인 후 다음 단계

    @staticmethod
    def _warn_a1(delta):
        if abs(delta) > 5.0:
            rec("sys", "warn", {}, "⚠ A1 회전은 높은 형상에서만(8/21 규칙 — 낮은 자세 "
                                   "회전으로 비상정지 이력). 자세 확인 후 진행할 것")

    # -- API --
    def jog(self, ax, delta):
        self._ensure()
        joint = ("A1", "A2", "A3")[ax]
        if joint == "A1":
            self._warn_a1(delta)
        def fn():
            import zk_pose as P
            from zk_safety import JOINT_D
            with self._ser:
                cur = P.ang(self.zk, JOINT_D[joint])
                if cur is None:
                    raise RuntimeError(f"{joint} 각도 읽기 실패")
                self._goto(joint, cur + delta)
        return self._start(fn, "jog")

    def move(self, js):
        self._ensure()
        cur_a1 = self._joints[0] if self._have_joints else None
        if cur_a1 is not None:
            self._warn_a1(js[0] - cur_a1)
        def fn():
            with self._ser:
                # A3→A2→A1 순서 (8/11 확립: A1 마지막 다듬이 yaw 랜덤 성분 제거)
                for joint, tgt in (("A3", js[2]), ("A2", js[1]), ("A1", js[0])):
                    self._goto(joint, float(tgt), tol=0.2)
        return self._start(fn, "move")

    def lift(self, mm):
        """현재 반경을 유지하며 흡착판을 수직 이동한다.

        zk_vlift.py의 실증 경로를 기존 시리얼 연결 안에서 재사용한다.
        별도 프로세스 실행은 같은 tty 이중 점유 때문에 금지한다.
        """
        self._ensure()
        rise = float(mm)
        def fn():
            import zk_pose as P
            from zk_safety import Abort, Guard, JOINT_D, JOINT_LIMITS
            from zk_vlift import fk, ik
            with self._ser:
                cur = {j: P.ang(self.zk, d) for j, d in JOINT_D.items()}
                if any(v is None for v in cur.values()):
                    raise Abort(f"각도 읽기 실패: {cur}")
                guard = Guard(self.zk, homing=False)
                guard.preflight(cur)
                r0, z0 = fk(cur["A2"], cur["A3"])
                zt = z0 + rise
                if zt < -52.0:
                    raise Abort(f"목표 z {zt:.1f}mm < 검증 하한 -52.0mm")
                count = max(1, int(math.ceil(abs(rise) / 1.5)))
                plan = []
                a2, a3 = cur["A2"], cur["A3"]
                for i in range(1, count + 1):
                    z = z0 + rise * i / count
                    a2, a3 = ik(r0, z, a2, a3)
                    for joint, value in (("A2", a2), ("A3", a3)):
                        lo, hi = JOINT_LIMITS[joint]
                        if not (lo <= value <= hi):
                            raise Abort(f"리프트 계획 {joint}={value:.2f}°가 리밋({lo}~{hi}) 밖")
                    plan.append((a2, a3))
                pct = min(max(self.speed, 1.0), 30.0)
                for a2, a3 in plan:
                    if self._stop.is_set():
                        raise Abort("STOP 요청")
                    P.goto_two(self.zk, guard, {"A2": a2, "A3": a3},
                               lead=max(pct * 0.083, 0.2), pct=pct,
                               bits=self.prof.bits)  # ★개체별 조그비트 — 기본값은 zkbot1(8/8 사고 유형)
                    self._goto("A2", a2, tol=0.2)
                    self._goto("A3", a3, tol=0.2)
                    self._refresh()
        return self._start(fn, "lift")

    def go_home(self):
        """원점복귀 = zk_home_run.py 위임 (Guard·이동거리상한·M120 finally OFF 포함).
        같은 tty 를 두 프로세스가 못 잡으므로 포트를 닫았다가 끝나면 다시 연다."""
        self._ensure()
        def fn():
            with self._ser:
                zk, self.zk = self.zk, None
                self.connected = False
                zk.close()
                try:
                    self._proc = subprocess.Popen(
                        [sys.executable, "/home/ar/zk_home_run.py",
                         "--robot", self.profile_name])
                    rc = self._proc.wait(timeout=180)   # 원점 90s + 먼 자세 여유
                finally:
                    self._proc = None
                    self._ensure()                       # 포트 재연결
                self._refresh()
                if rc != 0:
                    raise RuntimeError(f"원점복귀 종료코드 {rc} — zk_home_run 로그 확인")
        return self._start(fn, "home")

    def stop(self):
        self._stop.set()                                 # _goto 루프가 다음 주기에 비트 OFF
        p = self._proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)                 # zk_home_run finally 가 M120 OFF
        if not self.busy and self.zk:
            import zk_pose as P
            with self._ser:
                P.all_off(self.zk)
        return "stopping" if self.busy else "ok"

    def set_speed(self, v):
        self.speed = float(min(max(v, 1), 30))
        if self.speed != v:
            rec(self.name, "speed", {"value": v}, f"30% 상한 적용 → {self.speed}")
        return f"{self.speed:.0f}%"

    def vacuum(self, on):
        self._ensure()
        if not self._ser.acquire(timeout=1.0):
            raise Busy(f"{self.name} 동작 중 — 흡착 명령 보류")
        try:
            from zkfx import PUMP, VALVE
            if on:                                       # zk_pickplace 검증 시퀀스
                self.zk.y_off(VALVE); time.sleep(0.2)
                self.zk.y_on(PUMP)
            else:
                self.zk.y_off(PUMP); time.sleep(0.3)
                self.zk.y_on(VALVE); time.sleep(1.0)     # 파기(블로우) 1초
                self.zk.y_off(VALVE)
            self.vac = bool(on)
        finally:
            self._ser.release()

    def move_tcp(self, tcp):
        raise NotImplementedError("ZK 에 카테시안 이동 없음")
    def set_manual(self, on):
        raise NotImplementedError("ZK 수동조작은 리모컨으로 — ★모드 전환이 원점을 날리는 개체 있음(8/21), PC/리모컨 상호예고 규칙 준수")
    def gripper(self, pos):
        raise NotImplementedError("ZK 는 흡착(vacuum)만")

    def state(self):
        e = getattr(self, "last_err", None)
        return {"connected": self.connected, "busy": self.busy,
                "stale": not self._have_joints,
                "joints": [round(x, 3) for x in self._joints],
                "err": e if e and time.time() - e["t"] < 15 else None,
                "vacuum": self.vac}                      # 명령 누적값(피드백 없음)


# ---------------- FR5 실기 ----------------
class FR5Real:
    """Fairino FR5 — ROS2 경로 전용.

    선행: ~/fr5_up.sh (cmd_server 개통순서 → real_robot) + ROS2 env source.
    관절이동 = /move_action (fr5_pose.py 골 구성 그대로),
    그리퍼 = /fairino_remote_command_service (Fr5GripperService:
             max_time 20000ms 고정·errno73 → ResetAllError+Mode+RobotEnable 자동복구).
    """
    POSE_FILE = "/home/ar/fr5_data/poses.json"

    def __init__(self):
        self.node = None; self.gripsvc = None
        self.speed = 5
        self.grip_pos = 0                    # 명령값 (그리퍼 위치 readback)
        self._grip_real = None               # 8/27 실측(안전 폴링) 값
        self._grip_real_t = 0.0              # 실측 시각
        self._grip_watch_until = 0.0         # 이 시각까지만 실측 폴링(콘솔이 grip_watch 로 연장, 방치 방지)
        self._grip_last_read = 0.0           # 마지막 실측 시도 시각(스로틀)
        self._sdk_lock = threading.Lock()    # 8/27 실측 폴링 전용 논블로킹 락(ServoJ/명령과 겹침 방지)
        self._last_motion_t = 0.0            # 마지막 모션 시각(유휴 판정)
        self.frozen = False                  # 8/27 동결 워치독 상태
        self.freeze_kind = None              # controller_dead | comms | None
        self._freeze_probe_t = 0.0
        self.GRIP_READ_INTERVAL = 2.5        # 실측 최소 간격(초) — 8/19 사고(0.2s=5Hz)의 12배 여유
        self.GRIP_IDLE_GUARD = 1.0           # 모션 후 이만큼 지나야 유휴로 간주
        self.manual = False                  # 수동조작(드래그 티칭) 모드
        self._stop = threading.Event()
        self.busy = False
        self.connected = False
        self._joints_deg = [0.0]*6
        self._have_joints = False
        self._pick_go = threading.Event()      # 8/26 dot_pick z400 정지 → 하강 신호
        self._place_go = threading.Event()     # 8/27 dot_place 호버 정지 → 삽입 신호 (자동 통과 없음)

    def _ensure(self):
        if self.node:
            # 9/1: 브리지가 FR5 스택보다 먼저 뜨면 아래 5초 대기가 실패해 예외로 빠지는데,
            #      self.node 는 이미 만들어진 뒤라 이후 호출이 전부 여기서 즉시 반환된다.
            #      → 관절은 정상 수신되는데 connected 만 False 로 굳어 콘솔에 "FR5 미연결"이 뜬다.
            if not self.connected and self.node.cur is not None:
                self.connected = True
            return
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            import fr5_pose
        except ImportError as e:
            raise RuntimeError(f"ROS2 환경 미설정({e}) — fr5 워크스페이스 source 후 "
                               f"브리지를 다시 띄울 것")
        if not rclpy.ok():
            rclpy.init()
        self.node = fr5_pose.Fr5Pose()
        # /joint_states 동결 감지용 — 마지막 수신 시각 기록(8/24: read 루프가 SDK 에
        # 얼어붙으면 발행이 멈추고 MoveIt 이 전부 즉시 ABORTED 되는데 아무 표시가 없었다)
        from sensor_msgs.msg import JointState
        self._js_t = time.time()
        self.node.create_subscription(JointState, "/joint_states",
                                      lambda m: setattr(self, "_js_t", time.time()), 10)
        # 팀 서버(셀 오케스트레이터/FMS) 감시 — 계약 v0.2: /cell/status = 1Hz 하트비트(JSON)
        from std_msgs.msg import String as StrMsg
        self._cell_t = 0.0
        self._cell_state = ""
        def _on_cell(m):
            self._cell_t = time.time()
            try:
                import json as _j
                self._cell_state = _j.loads(m.data).get("state", "")
            except Exception:
                pass
        self.node.create_subscription(StrMsg, "/cell/status", _on_cell, 10)
        ex = SingleThreadedExecutor()
        ex.add_node(self.node)
        threading.Thread(target=ex.spin, daemon=True).start()
        t0 = time.time()
        while self.node.cur is None and time.time() - t0 < 5.0:
            time.sleep(0.1)
        if self.node.cur is None:
            raise RuntimeError("/joint_states 수신 실패 — real_robot 스택(~/fr5_up.sh) 확인")
        self.connected = True
        try:  # 브리지 재시작 시 실상태 복원 (플러그인 상태는 스택에 있음)
            self.manual = str(self._svc()._call("IsInDragTeach()")).strip().endswith(",1")
            res = str(self._svc()._call("GetGripperCurPosition()")).strip()   # '0,fault,pos'
            v = res.split(",")
            if v[0] == "0" and len(v) >= 3:
                self.grip_pos = int(v[2])
            # ⚠연속 폴링 금지: 그리퍼 조회 5Hz 가 ServoJ 스트림을 굶겨 팔이 죽는다(8/19 실증).
            #   기동 1회 + 명령 시 갱신만. 콘솔 밖에서 그리퍼를 움직일 경로는 없으므로 충분.
        except Exception:
            pass
        rec("fr5", "connect", {}, f"ROS2 ok (manual={self.manual})")

    def poll(self):
        # FR5 는 ROS2 구독이라 "보기만" 하는 연결이 안전 — 브리지가 뜨면 바로 붙어서
        # 명령 없이도 트윈이 실기 자세를 따라가게 한다 (5초 간격 재시도, 스택 없으면 조용히 대기)
        if not self.node:
            now = time.time()
            if now - getattr(self, "_last_try", 0) > 5.0:
                self._last_try = now
                try:
                    self._ensure()
                except Exception:
                    return
            else:
                return
        if self.node and self.node.cur:
            import math
            self._joints_deg = [math.degrees(v) for v in self.node.cur]
            self._have_joints = True
            stale = time.time() - getattr(self, "_js_t", time.time())
            if stale > 4.0:
                # 8/27 동결 워치독: 4s+ 무수신이면 동결로 판정하고 8080(컨트롤러 앱)을 프로브해 유형 분류.
                #   ①8080 refused = 컨트롤러 앱 사망(STOP→동결형) → 전원 재투입만  ②8080 살아있음 = 통신/랜선형 → 스택 재기동/fr5_rescue
                if time.time() - getattr(self, "_freeze_probe_t", 0) > 3.0:
                    self._freeze_probe_t = time.time()
                    self.frozen = True
                    self.freeze_kind = self._probe_freeze()
                e = getattr(self, "last_err", None)
                kind = getattr(self, "freeze_kind", "?")
                cure = ("전원 재투입 필요(컨트롤러 앱 사망) — 콘솔 [🧊 동결 해제]" if kind == "controller_dead"
                        else "스택 재기동/랜선 확인 — 콘솔 [🚑 복구]" if kind == "comms"
                        else "fr5_up.sh 재기동")
                if not e or "동결" not in e.get("msg", "") or time.time() - e["t"] > 8:
                    self.last_err = {"t": time.time(),
                                     "msg": f"🧊 동결 {stale:.0f}s ({kind}) — 이동 전부 즉시 ABORTED. {cure}",
                                     "freeze": True, "kind": kind}
            elif getattr(self, "frozen", False):
                self.frozen = False; self.freeze_kind = None   # 수신 재개 → 동결 해제
            try:
                self._tcp = self._fk_tcp()   # MoveIt FK — SDK 를 안 거치므로 ServoJ 무관
            except Exception:
                pass
            self._safe_grip_read()           # 8/27 안전 실측(엄격한 게이트, 기본 OFF)

    def _probe_freeze(self):
        """동결 유형 판정 — FR5 컨트롤러 8080(웹/명령 채널) TCP 연결 시도.
        refused/timeout = 컨트롤러 앱 사망(전원 필요) · 연결됨 = 통신/랜선형(스택 재기동)."""
        import socket
        ip = os.environ.get("FR5_IP", "192.168.58.2")
        try:
            s = socket.create_connection((ip, 8080), timeout=1.0); s.close()
            return "comms"            # 8080 은 살아있는데 joint_states 만 멈춤 = 통신/랜선/스택
        except (ConnectionRefusedError, OSError):
            return "controller_dead"  # 8080 거부/무응답 = 컨트롤러 앱 사망 → 전원 재투입

    def _safe_grip_read(self):
        """★그리퍼 실시간 실측 — 8/19 사고(5Hz 폴링이 ServoJ 굶겨 팔 사망) 절대 재발 방지용 7중 게이트.
        기본 OFF(watch 안 켜면 아무것도 안 함) · 유휴에서만 · 2.5s 스로틀 · 논블로킹 락 · 느리면 자동 OFF."""
        now = time.time()
        if now >= self._grip_watch_until:               # ① opt-in: 콘솔이 grip_watch 로 켠 창 안에서만
            return
        if self.busy or self.manual:                    # ② 계획 모션 중/수동모드 금지
            return
        if now - self._last_motion_t < self.GRIP_IDLE_GUARD:   # ③ 모션 직후 유예(ServoJ 홀드 안정화)
            return
        if now - self._grip_last_read < self.GRIP_READ_INTERVAL:  # ④ 강한 스로틀
            return
        if getattr(self, "last_err", None) and now - self.last_err["t"] < 12:  # ⑤ 동결/알람 최근이면 금지
            return
        if not self._sdk_lock.acquire(blocking=False):   # ⑥ 논블로킹 — ServoJ/다른 SDK 뒤에 줄서지 않음
            return
        try:
            self._grip_last_read = now
            t_read = time.time()
            res = str(self._svc()._call("GetGripperCurPosition()")).strip()
            dt = time.time() - t_read
            v = res.split(",")
            if v[0] == "0" and len(v) >= 3:
                self._grip_real = int(v[2]); self._grip_real_t = time.time()
                self.grip_pos = self._grip_real       # 명령값 표시도 실측으로 동기화
            if dt > 0.5:                               # ⑦ 워치독: 읽기가 느리면(서보 경합 징후) 즉시 실측 OFF
                self._grip_watch_until = 0.0
                rec("fr5", "grip_watch", {}, f"⚠ 그리퍼 읽기 {dt*1000:.0f}ms 지연 — 실측 자동중단(서보 보호)")
        except Exception as e:
            self._grip_watch_until = 0.0               # 예외도 즉시 OFF
            rec("fr5", "grip_watch", {}, f"⚠ 그리퍼 읽기 실패 {e} — 실측 자동중단")
        finally:
            self._sdk_lock.release()
        # 실측 직후 joint_states 가 동결되면(서보 굶김 징후) 실측 OFF
        if time.time() - getattr(self, "_js_t", now) > 3.0:
            self._grip_watch_until = 0.0
            rec("fr5", "grip_watch", {}, "⚠ 실측 후 관절상태 지연 — 실측 자동중단(서보 보호)")

    def grip_watch(self, sec=30.0):
        """콘솔 그리퍼 패널이 열려있는 동안 주기 호출 → 실측 폴링 창 연장(기본 30s 뒤 자동 만료)."""
        self._grip_watch_until = time.time() + float(sec)
        return {"until": round(self._grip_watch_until, 1), "interval": self.GRIP_READ_INTERVAL}

    def _start(self, fn, label):
        if self.busy:
            raise Busy("fr5 동작 중 — STOP 후 재시도")
        self.busy = True
        self._stop.clear()
        def run():
            try:
                fn()
                rec("fr5", label, {}, "done")
            except Exception as e:
                self.last_err = {"t": time.time(), "msg": str(e)}
                rec("fr5", label, {}, f"ERR {e}")
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()
        return "started"

    def _movej_rad(self, tgt_rad):
        self._last_motion_t = time.time()    # 8/27 그리퍼 실측 유휴게이트용
        """fr5_pose.goto 와 같은 골 구성 + STOP 시 골 취소.
        ★MoveGroup SUCCESS ≠ 실물 정지 — 반환 전 joint_states 안정 대기(8/10 실측)."""
        import math
        from moveit_msgs.action import MoveGroup
        from moveit_msgs.msg import Constraints, JointConstraint
        import fr5_pose as FP
        scale = min(max(self.speed, 1), 30) / 100.0     # 콘솔 상한 30% (티칭 저속 원칙)
        g = MoveGroup.Goal()
        g.request.group_name = FP.GROUP
        g.request.num_planning_attempts = 3
        g.request.allowed_planning_time = 5.0
        g.request.max_velocity_scaling_factor = scale
        g.request.max_acceleration_scaling_factor = scale
        c = Constraints()
        for name, pos in zip(FP.JOINTS, tgt_rad):
            jc = JointConstraint()
            jc.joint_name = name; jc.position = float(pos)
            jc.tolerance_above = jc.tolerance_below = 0.002   # 0.11° — 카테시안 옆 표류 억제(8/24)
            jc.weight = 1.0
            c.joint_constraints.append(jc)
        g.request.goal_constraints.append(c)
        g.planning_options.plan_only = False

        cli = self.node.cli
        if not cli.wait_for_server(timeout_sec=10.0):
            raise RuntimeError("/move_action 없음 — real_robot 스택 확인")
        fut = cli.send_goal_async(g)
        t0 = time.time()
        while not fut.done():
            if time.time() - t0 > 30:
                raise RuntimeError("골 전송 타임아웃")
            time.sleep(0.05)
        gh = fut.result()
        if gh is None or not gh.accepted:
            raise RuntimeError("골 거부(계획 실패 또는 리밋 밖)")
        rf = gh.get_result_async()
        t0, cancelled = time.time(), False
        while not rf.done():
            if self._stop.is_set() and not cancelled:
                gh.cancel_goal_async()
                cancelled = True
            if time.time() - t0 > 180:
                raise RuntimeError("MoveGroup 결과 타임아웃")
            time.sleep(0.05)
        self._wait_settled()
        if cancelled:
            raise RuntimeError("STOP 으로 취소됨")
        res = rf.result()
        if not (res and res.result.error_code.val == 1):
            raise RuntimeError(f"MoveGroup 실패 code="
                               f"{res.result.error_code.val if res else '?'}")
        self._assert_moved(tgt_rad)

    def _assert_moved(self, tgt_rad):
        """★무동작 감지(8/24 — "성공 로그를 믿지 마라"): 컨트롤러 알람이면 ServoJ 가
        죽어 MoveGroup 이 SUCCESS 를 내고도 실물이 그대로다(오류14, 8/10 동형).
        목표까지 3° 이상 남았는데 '완료'면 알람으로 판정해 사용자에게 알린다."""
        import math
        cur = list(self.node.cur)
        err = max(abs(a - b) for a, b in zip(cur, tgt_rad))
        if err > math.radians(3.0):
            raise RuntimeError(
                f"실물 무동작(목표까지 {math.degrees(err):.1f}° 남음) — 컨트롤러 알람 의심"
                f"(ServoJ 오류14). 복구: 조작모드의 자동모드 버튼(ResetAllError 포함) → "
                f"안 되면 로봇 전원 재투입 후 fr5_up.sh")

    def _wait_settled(self):
        """0.1s 간격 샘플, 연속 5회 변화 0.05° 미만 = 정지 (fr5_pose 8/14 픽스 로직)."""
        import math
        tol = math.radians(0.05)
        prev, stable, t0 = None, 0, time.time()
        while time.time() - t0 < 90.0:
            time.sleep(0.1)
            now = self.node.cur and list(self.node.cur)
            if not now:
                continue
            if prev and max(abs(a-b) for a, b in zip(now, prev)) < tol:
                stable += 1
                if stable >= 5:
                    return
            else:
                stable = 0
            prev = now

    def _svc(self):
        if self.gripsvc is None:
            import fr5_real_adapter as FRA
            self.gripsvc = FRA.Fr5GripperService(self.node)
        return self.gripsvc

    def _switch_jtc(self, activate):
        """fairino5_controller(JTC) 활성/비활성 — 드래그 중 JTC 가 옛 궤적점을 계속
        지령하면 해제 순간 점프백하므로 반드시 내려야 한다. STRICT(2)."""
        from controller_manager_msgs.srv import SwitchController
        if not hasattr(self, "_sw_cli"):
            self._sw_cli = self.node.create_client(SwitchController,
                                                   "/controller_manager/switch_controller")
        if not self._sw_cli.wait_for_service(timeout_sec=3.0):
            raise RuntimeError("/controller_manager/switch_controller 없음")
        rq = SwitchController.Request()
        (rq.activate_controllers if activate else rq.deactivate_controllers).append("fairino5_controller")
        rq.strictness = 1   # BEST_EFFORT — 이미 원하는 상태면 통과(멱등, 자동복귀 반복 호출 대비)
        fut = self._sw_cli.call_async(rq)
        t0 = time.time()
        while not fut.done():
            if time.time() - t0 > 10:
                raise RuntimeError("switch_controller 타임아웃")
            time.sleep(0.05)
        if not fut.result().ok:
            raise RuntimeError(f"fairino5_controller {'activate' if activate else 'deactivate'} 실패")

    def set_manual(self, on, body=None):
        """수동(드래그 티칭) ↔ 자동 전환 — 8/24 v2.

        핵심(실증): DragTeachSwitch(1)=0 이어도 ①JTC 홀드 지령 ②ServoJ 8ms 스트림이
        팔을 계속 붙잡아 드래그가 안 된다. 그래서 순서가:
          수동 = JTC 비활성 → Mode(1) → DragTeachSwitch(1)(플러그인이 스트림도 정지)
                 → IsInDragTeach 로 실상태 확인
          자동 = DragTeachSwitch(0)(플러그인이 현재자세 재동기화 후 스트림 재개)
                 → Mode(0) → RobotEnable(1) → JTC 활성(현재 자세 홀드)
        """
        self._ensure()
        if self.busy:
            raise Busy("fr5 동작 중 — 정지 후 모드 전환")
        svc = self._svc()
        results = []
        def call(c, must=True):
            res = str(svc._call(c)).strip()
            results.append(f"{c}={res}")
            if must and res.startswith("-1:"):
                raise RuntimeError(f"{c} 플러그인 거부({res}) — 진행분: {' '.join(results)}")
            return res
        self._cart_ref = None
        if on:
            self._switch_jtc(False)
            try:
                call("Mode(1)")
                # 8/25: J6 등 손목 관절이 손으로 안 돌던 원인 = 마찰보상 미적용.
                # 계수 [0~1]. ★J1~J3 는 0.5 로 낮춤 — 전관절 1.0 첫 실기에서 드래그 중
                # J2 충돌감지 알람 재발(페이로드 추정치 오차 + 과보상 의심). 손목만 1.0.
                # 8/30: 손목 1.0 은 과보상 → 드래그 중 팔이 떠는(자려진동) 원인.
                #   특히 그리퍼가 비어 있으면 등록 페이로드(0.7kg)보다 실부하가 가벼워 더 과해진다.
                #   body.friction_level=[6개] 로 조절, body.friction=false 면 보상 자체를 끈다
                #   (손목이 뻑뻑해지는 대신 절대 안 떤다).
                lv = (body or {}).get("friction_level") or FRICTION_LEVEL
                if (body or {}).get("friction", True):
                    call("SetFrictionValue_level(" + ",".join(str(v) for v in lv) + ")", must=False)
                    call("FrictionCompensationOnOff(1)", must=False)
                else:
                    call("FrictionCompensationOnOff(0)", must=False)
                call("DragTeachSwitch(1)")
                st = call("IsInDragTeach()", must=False)
                if not st.endswith(",1"):
                    raise RuntimeError(f"드래그 미진입(IsInDragTeach={st}) — 진행분: {' '.join(results)}")
            except Exception:
                for c in ("DragTeachSwitch(0)", "Mode(0)", "RobotEnable(1)"):
                    try: call(c, must=False)
                    except Exception: pass
                try: self._switch_jtc(True)
                except Exception: pass
                raise
            self.manual = True
        else:
            call("DragTeachSwitch(0)", must=False)
            call("FrictionCompensationOnOff(0)", must=False)
            call("Mode(0)")
            call("RobotEnable(1)", must=False)
            self._switch_jtc(True)
            self.manual = False
        return " ".join(results)

    def _no_manual(self):
        if self.manual:
            raise RuntimeError("수동조작(드래그 티칭) 모드 중 — 해제 후 이동 명령")

    # -- API --
    def jog(self, ax, delta):
        self._ensure()
        self._no_manual()
        self._cart_ref = None
        import math
        def fn():
            cur = list(self.node.cur)
            cur[ax] += math.radians(delta)
            self._movej_rad(cur)
        return self._start(fn, "jog")

    def move(self, js_deg):
        self._ensure()
        self._no_manual()
        self._cart_ref = None
        import math
        def fn():
            self._movej_rad([math.radians(v) for v in js_deg])
        return self._start(fn, "move")

    # ---- 카테시안: MoveIt /compute_fk·/compute_ik 경유 (8/24) ----
    # GetActualTCPPose 는 cmd_server 점유로 못 쓰지만, MoveIt 스택이 FK/IK 서비스를
    # 제공하므로: TCP 표시 = FK(1Hz poll), 카테시안 이동 = IK 해 → 검증된 조인트 경로.
    # 기준 링크 = wrist3_link(플랜지, SRDF tip). 그리퍼 끝이 아님에 주의.
    @staticmethod
    def _quat_to_rpy(x, y, z, w):
        import math
        r = math.atan2(2*(w*x + y*z), 1 - 2*(x*x + y*y))
        s = max(-1.0, min(1.0, 2*(w*y - z*x)))
        p = math.asin(s)
        yaw = math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
        return r, p, yaw

    @staticmethod
    def _rpy_to_quat(r, p, y):
        import math
        cr, sr = math.cos(r/2), math.sin(r/2)
        cp, sp = math.cos(p/2), math.sin(p/2)
        cy, sy = math.cos(y/2), math.sin(y/2)
        return (sr*cp*cy - cr*sp*sy, cr*sp*cy + sr*cp*sy,
                cr*cp*sy - sr*sp*cy, cr*cp*cy + sr*sp*sy)

    def _wait_fut(self, fut, timeout, what):
        t0 = time.time()
        while not fut.done():
            if time.time() - t0 > timeout:
                raise RuntimeError(f"{what} 타임아웃")
            time.sleep(0.03)
        return fut.result()

    def _fk_tcp(self):
        """현재 관절 → wrist3_link pose. 반환 [x,y,z(mm), rx,ry,rz(deg)]."""
        import math
        from moveit_msgs.srv import GetPositionFK
        if not hasattr(self, "_fk_cli"):
            self._fk_cli = self.node.create_client(GetPositionFK, "/compute_fk")
        if not self._fk_cli.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("/compute_fk 없음")
        rq = GetPositionFK.Request()
        rq.header.frame_id = "base_link"
        rq.fk_link_names = ["wrist3_link"]
        rq.robot_state.joint_state.name = ["j1", "j2", "j3", "j4", "j5", "j6"]
        rq.robot_state.joint_state.position = list(self.node.cur)
        res = self._wait_fut(self._fk_cli.call_async(rq), 5.0, "FK")
        if res.error_code.val != 1 or not res.pose_stamped:
            raise RuntimeError(f"FK 실패 code={res.error_code.val}")
        p = res.pose_stamped[0].pose
        r, pt, yw = self._quat_to_rpy(p.orientation.x, p.orientation.y,
                                      p.orientation.z, p.orientation.w)
        return [p.position.x*1000, p.position.y*1000, p.position.z*1000,
                math.degrees(r), math.degrees(pt), math.degrees(yw)]

    def _ik_joints(self, tcp):
        """[x,y,z(mm), rx,ry,rz(deg)] → 관절해(rad, 현재 자세 시드). 해 없으면 예외."""
        import math
        from moveit_msgs.srv import GetPositionIK
        if not hasattr(self, "_ik_cli"):
            self._ik_cli = self.node.create_client(GetPositionIK, "/compute_ik")
        if not self._ik_cli.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("/compute_ik 없음")
        rq = GetPositionIK.Request()
        r = rq.ik_request
        r.group_name = "fairino5_v6_group"
        r.robot_state.joint_state.name = ["j1", "j2", "j3", "j4", "j5", "j6"]
        r.robot_state.joint_state.position = list(self.node.cur)   # 시드 = 현재(팔꿈치 반전 방지)
        r.avoid_collisions = True
        r.pose_stamped.header.frame_id = "base_link"
        p = r.pose_stamped.pose
        p.position.x, p.position.y, p.position.z = tcp[0]/1000, tcp[1]/1000, tcp[2]/1000
        qx, qy, qz, qw = self._rpy_to_quat(math.radians(tcp[3]), math.radians(tcp[4]),
                                           math.radians(tcp[5]))
        p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w = qx, qy, qz, qw
        res = self._wait_fut(self._ik_cli.call_async(rq), 5.0, "IK")
        if res.error_code.val != 1:
            raise RuntimeError(f"IK 해 없음(code={res.error_code.val}) — 도달 불가 자세이거나 충돌")
        d = dict(zip(res.solution.joint_state.name, res.solution.joint_state.position))
        return [d[j] for j in ("j1", "j2", "j3", "j4", "j5", "j6")]

    def _move_cart_line(self, tcp):
        self._last_motion_t = time.time()    # 8/27 그리퍼 실측 유휴게이트용
        """카테시안 직선 이동 — MoveIt /compute_cartesian_path + /execute_trajectory.

        IK+MoveJ(관절보간)는 경로가 호를 그려 Z 하강이 좌우로 출렁인다(8/24 사용자 관찰).
        SDK MoveL 은 단일클라이언트 규칙상 불가 — 스택의 직선보간이 동등 해법.
        경로 5mm 스텝·충돌회피, 90% 미만 도달이면 예외(특이점/리밋)."""
        import math
        from moveit_msgs.srv import GetCartesianPath
        from moveit_msgs.action import ExecuteTrajectory
        from rclpy.action import ActionClient
        if not hasattr(self, "_cp_cli"):
            self._cp_cli = self.node.create_client(GetCartesianPath, "/compute_cartesian_path")
            self._exec_cli = ActionClient(self.node, ExecuteTrajectory, "/execute_trajectory")
        if not self._cp_cli.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("/compute_cartesian_path 없음")
        rq = GetCartesianPath.Request()
        rq.header.frame_id = "base_link"
        rq.group_name = "fairino5_v6_group"
        rq.link_name = "wrist3_link"
        rq.start_state.joint_state.name = ["j1", "j2", "j3", "j4", "j5", "j6"]
        rq.start_state.joint_state.position = list(self.node.cur)
        from geometry_msgs.msg import Pose
        p = Pose()
        p.position.x, p.position.y, p.position.z = tcp[0]/1000, tcp[1]/1000, tcp[2]/1000
        qx, qy, qz, qw = self._rpy_to_quat(math.radians(tcp[3]), math.radians(tcp[4]), math.radians(tcp[5]))
        p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w = qx, qy, qz, qw
        rq.waypoints = [p]
        rq.max_step = 0.005
        rq.jump_threshold = 2.0          # IK 해 점프(팔꿈치 반전) 차단
        rq.avoid_collisions = True
        scale = min(max(self.speed, 1), 30) / 100.0
        if hasattr(rq, "max_velocity_scaling_factor"):
            rq.max_velocity_scaling_factor = scale
            rq.max_acceleration_scaling_factor = scale
        res = self._wait_fut(self._cp_cli.call_async(rq), 10.0, "직선경로 계산")
        if res.error_code.val != 1 or res.fraction < 0.9:
            raise RuntimeError(f"직선경로 {res.fraction*100:.0f}% 만 도달 가능(특이점/리밋) — 조인트 이동으로 우회 필요")
        if not self._exec_cli.wait_for_server(timeout_sec=5.0):
            raise RuntimeError("/execute_trajectory 없음")
        goal = ExecuteTrajectory.Goal()
        goal.trajectory = res.solution
        fut = self._exec_cli.send_goal_async(goal)
        gh = self._wait_fut(fut, 10.0, "궤적 전송")
        if gh is None or not gh.accepted:
            raise RuntimeError("궤적 거부")
        rf = gh.get_result_async()
        t0, cancelled = time.time(), False
        while not rf.done():
            if self._stop.is_set() and not cancelled:
                gh.cancel_goal_async(); cancelled = True
            if time.time() - t0 > 180:
                raise RuntimeError("궤적 실행 타임아웃")
            time.sleep(0.05)
        self._wait_settled()
        if cancelled:
            raise RuntimeError("STOP 으로 취소됨")
        pts = res.solution.joint_trajectory.points
        if pts:
            self._assert_moved(list(pts[-1].positions))

    def move_tcp(self, tcp):
        self._ensure()
        self._no_manual()
        # 8/26 가드: (0,0,0) 근방·베이스 안쪽 목표 거부 — 콘솔이 status 수신 전 전부 0 을 보낸 사고
        if sum(float(v)**2 for v in tcp[:3]) ** 0.5 < 150:
            raise RuntimeError(f"TCP 목표 {[round(float(v),1) for v in tcp[:3]]} 가 베이스 150mm 안 — 거부(콘솔 미수신 0 의심)")
        def fn():
            t = [float(v) for v in tcp]
            self._cart_ref = list(t)
            try:
                self._move_cart_line(t)          # 직선 우선
            except Exception as e:
                rec("fr5", "cart_line", {}, f"직선 실패({e}) → 관절보간 폴백")
                self._movej_rad(self._ik_joints(t))
        return self._start(fn, "move_tcp")

    def cart_jog(self, axis, delta):
        """카테시안 조그: 축 하나만 delta 이동.

        ★연속 조그는 직전 '명령 목표'를 기준으로 누적한다(8/24) — 매번 실측 FK 에서
        출발하면 관절 도달오차가 매 스텝 X/Y 로 새어 '양옆 흔들림'이 된다.
        조인트 계열 명령(jog/move/home/manual)이 끼어들면 기준을 버리고 FK 재시작."""
        self._ensure()
        self._no_manual()
        def fn():
            ref = getattr(self, "_cart_ref", None)
            # 8/26: 미세 조그(≤5mm)는 항상 실측 FK 기준 — 누적 캐시가 어긋나 방향/양이 틀리는 사고 반복
            if abs(float(delta)) <= 5.0:
                ref = None
            if ref:
                # ★스테일 기준 가드(8/25 실사고): 스택 재기동·알람 후엔 동결 직전의
                # '명령 목표'가 남아 실제 자세와 크게 어긋날 수 있다 — +30 이 +103 이 됐다.
                # 실측 FK 와 20mm 이상 벌어져 있으면 기준을 버리고 실측에서 출발한다.
                try:
                    act = self._fk_tcp()
                    if max(abs(float(ref[i]) - float(act[i])) for i in range(3)) > 20.0:
                        rec("fr5", "cart_jog", {},
                            "누적 기준이 실측과 20mm+ 어긋남(재기동/알람 후) → 실측 FK 재시작")
                        ref = None
                except Exception:
                    ref = None
            cur = list(ref) if ref else self._fk_tcp()
            cur[int(axis)] += float(delta)
            self._cart_ref = list(cur)
            try:
                self._move_cart_line(cur)        # 직선 우선
            except Exception as e:
                rec("fr5", "cart_line", {}, f"직선 실패({e}) → 관절보간 폴백")
                self._movej_rad(self._ik_joints(cur))
        return self._start(fn, "cart_jog")

    def go_home(self):
        """홈 = fr5_data/poses.json 의 'home' 티칭값(라디안). 하드코딩 금지(T4) —
        미티칭이면 티칭부터 하라고 알려준다."""
        self._ensure()
        self._no_manual()
        self._cart_ref = None
        import json
        def fn():
            try:
                with open(self.POSE_FILE) as f:
                    db = json.load(f)
            except FileNotFoundError:
                raise RuntimeError(f"{self.POSE_FILE} 없음 — fr5_pose.py save home 먼저")
            if "home" not in db:
                raise RuntimeError("'home' 자세 미티칭 — fr5_pose.py save home 먼저")
            e = db["home"]
            js = e["joints"] if isinstance(e, dict) else e   # fr5_pose 포맷 = {"joints":[rad...],...}
            self._movej_rad([float(v) for v in js])
        return self._start(fn, "home")

    def stop(self):
        self._stop.set()                    # _movej_rad 루프가 골 취소
        return "stopping" if self.busy else "ok"

    def set_speed(self, v):
        self.speed = int(min(max(v, 1), 30))
        if self.speed != v:
            rec("fr5", "speed", {"value": v}, f"30% 상한 적용 → {self.speed}")
        return f"{self.speed}%"

    def gripper(self, pos):
        self._ensure()
        def fn():
            self._svc().move(int(pos))      # 비동기 함정 — 즉시 리턴
            self.gripsvc.wait_done()        # GetGripperMotionDone 폴링으로 완료 대기 (._svc 로 보장됨)
            self.grip_pos = int(pos)
            # 8/25: 완료 직후 1회 실측 — 부품을 물면 실제 벌림이 명령값보다 크다(덜 닫힘).
            # 표시·Recorder 캡처가 실측값을 갖도록 갱신(단발 읽기라 8/19 굶김과 무관).
            try:
                res = str(self._svc()._call("GetGripperCurPosition()")).strip()
                v = res.split(",")
                if v[0] == "0" and len(v) >= 3:
                    self.grip_pos = int(v[2])
            except Exception:
                pass
        return self._start(fn, "gripper")

    # ─────────────────────────────────────────────────────────────────────
    # 8/27 place(밑판 삽입) — pick 보다 위험(충돌) → 2단 보정 + 삽입 전 무조건 정지(사람 [▶ 삽입])
    #   1차(observe_place z720): 벽 색에 대응하는 기둥 2점 → 선 각도=yaw · 중점=XY (place_scale 실측 축척)
    #   2차(hover = 삽입점 +25mm): 티칭 때 캡처한 '호버 화면의 점들' 을 기준으로 재보정 (hover_scale 자동 실측)
    #   벽 yaw 는 그리퍼에 고정(파지 때 벽과 직각) → 손목각 = 기둥선각 ± 90 + rz_trim(티칭 학습). 벽 점은 안 봐도 됨.
    #   설정: cal["place_lines"][색] = 기둥 2색 (기본: 파랑벽→노랑·빨강 / 초록벽→파랑·초록 — 사용자 지정 8/26)
    # ─────────────────────────────────────────────────────────────────────
    PLACE_HOVER_DZ = 25.0
    PLACE_LINES_DEFAULT = {"blue": ["yellow", "red"], "green": ["blue", "green"],
                           "red": ["green", "red"], "yellow": ["blue", "yellow"]}   # ★red/yellow 짧은 벽은 가정 — 확인 필요

    def _dots_now(self, n=10, roi=None, sleep=0.08):
        """n 프레임 관측 → {kind: [(px,py,area) 클러스터 평균 ...]} (40px 클러스터, 60% 이상 등장만)."""
        import json as _j, urllib.request, statistics as S
        raw = {}
        got = 0
        for _ in range(n):
            try:
                d = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=3))
                got += 1
                for t in d["dots"]:
                    if roi and not (roi[0] <= t["px"] <= roi[2] and roi[1] <= t["py"] <= roi[3]): continue
                    raw.setdefault(t["kind"], []).append((t["px"], t["py"], t["area"]))
            except Exception:
                pass
            time.sleep(sleep)
        out = {}
        for k, pts in raw.items():
            cl = []
            for q in pts:
                for c in cl:
                    if abs(q[0]-c[0][0]) < 40 and abs(q[1]-c[0][1]) < 40: c.append(q); break
                else: cl.append([q])
            out[k] = [(S.mean([q[0] for q in c]), S.mean([q[1] for q in c]), S.mean([q[2] for q in c]))
                      for c in cl if len(c) >= max(1, int(got*0.6))]
        return out

    def _place_posts_now(self, cal, line, n=10):
        """기둥 2점(line=[색A,색B]) — place_posts 기준 픽셀에 가장 가까운 같은 색 점(±120px, ROI 안). 없으면 None."""
        roi = cal.get("place_roi"); posts = cal.get("place_posts") or {}
        dots = self._dots_now(n=n, roi=roi)
        out = []
        for k in line:
            cand = dots.get(k, [])
            if k in posts:
                ex, ey = posts[k]
                cand = [c for c in cand if (c[0]-ex)**2 + (c[1]-ey)**2 < 120**2]
                cand.sort(key=lambda c: (c[0]-ex)**2 + (c[1]-ey)**2)
            else:
                cand.sort(key=lambda c: -c[2])
            if not cand: return None
            out.append([round(cand[0][0], 1), round(cand[0][1], 1)])
        return out

    @staticmethod
    def _wrap90(a):
        while a > 90: a -= 180
        while a <= -90: a += 180
        return a

    def _theta_scale(self, M, p1, p2):
        """두 픽셀점 선의 베이스 각도(°) — M(px/mm 2x2) 역행렬로 mm 사상."""
        import math
        det = M[0][0]*M[1][1] - M[0][1]*M[1][0]
        Mi = [[M[1][1]/det, -M[0][1]/det], [-M[1][0]/det, M[0][0]/det]]
        dx, dy = p2[0]-p1[0], p2[1]-p1[1]
        return self._wrap90(math.degrees(math.atan2(Mi[1][0]*dx + Mi[1][1]*dy, Mi[0][0]*dx + Mi[0][1]*dy)))

    def _mm_from_dpx(self, M, dpx):
        """픽셀차 → 팔 보정 mm (= -Minv@Δpx, M 은 '팔 +1mm 이동 시 점 픽셀 변화' 로 실측한 값)."""
        det = M[0][0]*M[1][1] - M[0][1]*M[1][0]
        Mi = [[M[1][1]/det, -M[0][1]/det], [-M[1][0]/det, M[0][0]/det]]
        return [-(Mi[0][0]*dpx[0] + Mi[0][1]*dpx[1]), -(Mi[1][0]*dpx[0] + Mi[1][1]*dpx[1])]

    def _move_abs_settle(self, t, tol=0.15):
        """카테시안 직선 이동 + 도달 확인(명령 직후 busy stale 함정 회피)."""
        self._cart_ref = list(t); self._move_cart_line(t)
        for _ in range(40):
            cur = self._fk_tcp()
            if max(abs(cur[i]-t[i]) for i in range(3)) < tol: break
            time.sleep(0.15)
        time.sleep(0.6)
        return self._fk_tcp()

    def _measure_scale_here(self, kinds, roi=None, step=3.0):
        """지금 높이의 px/mm 실측: 절대이동 ±step 으로 X·Y 각각 이동, 보이는 점(kinds)의 픽셀 변화 평균. 원위치 복귀."""
        import statistics as S
        base = self._fk_tcp()
        def snap():
            d = self._dots_now(n=8, roi=roi)
            return {k: max(v, key=lambda c: c[2])[:2] for k, v in d.items() if k in kinds and v}
        p0 = snap()
        tx = list(base); tx[0] += step; self._move_abs_settle(tx); px = snap()
        self._move_abs_settle(base)
        ty = list(base); ty[1] += step; self._move_abs_settle(ty); py = snap()
        self._move_abs_settle(base)
        ks = [k for k in p0 if k in px and k in py]
        if not ks: return None
        ax = S.mean([(px[k][0]-p0[k][0])/step for k in ks]); ay = S.mean([(px[k][1]-p0[k][1])/step for k in ks])
        bx = S.mean([(py[k][0]-p0[k][0])/step for k in ks]); by = S.mean([(py[k][1]-p0[k][1])/step for k in ks])
        return {"M": [[round(ax, 3), round(bx, 3)], [round(ay, 3), round(by, 3)]], "kinds": ks, "z": round(base[2], 1)}

    def dot_place_teach(self, body=None):
        """벽을 물고 '삽입된 자세' 에 손으로 놓은 상태에서 호출 — 삽입 TCP·오프셋 기록 → +25 호버에서 기준 캡처·축척 실측
        → observe_place 로 올라가 기둥선 기준·손목각 보정 학습. 끝나면 벽을 물고 observe_place 에 있다."""
        import json as _j, math
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        line = (cal.get("place_lines") or {}).get(ck) or self.PLACE_LINES_DEFAULT[ck]
        obs_tcp = cal["observe_place_tcp"]; obs_j = cal["observe_place_joints"]
        if not cal.get("place_scale", {}).get("valid"):
            raise RuntimeError("place_scale(관측 높이 축척) 실측값 없음 — 먼저 측정")
        self._ensure()
        cur = self._fk_tcp()
        ref = cal.setdefault("refs_place", {}).setdefault(ck, {})
        hover_dz = float(body.get("hover_dz", ref.get("hover_dz", self.PLACE_HOVER_DZ)))
        ref.update({"line": line, "insert_tcp": [round(v, 2) for v in cur],
                    "insert_joints": [round(math.degrees(v), 3) for v in self.node.cur],
                    "place_offset": [round(cur[i]-obs_tcp[i], 2) for i in range(3)],
                    "place_rz": round(cur[5], 2), "hover_dz": hover_dz})
        _j.dump(cal, open(CAL, "w"), indent=1, ensure_ascii=False)
        rec("fr5", "dot_place_teach", {}, f"[{ck}] 삽입 자세 기록 tcp {ref['insert_tcp'][:3]} rz {ref['place_rz']} · 오프셋 {ref['place_offset']} · 기둥선 {line}")
        def fn():
            sp0 = self.speed; self.speed = max(1, min(3, sp0))
            try:
                # ★호버에서는 '수직 이동만' — 벽이 기둥 홈에 물려 있어 옆으로 움직이면 걸린다(8/27).
                #   축척(px/mm)은 별도 액션 dot_place_scale 로 '빈 그리퍼' 상태에서 잰다.
                hov = list(cur); hov[2] += hover_dz
                self._move_abs_settle(hov)
                snap = self._cam_snap(f"/home/ar/bf2_console/refs/place_{ck}_hover_ref.jpg")
                dots = self._dots_now(n=12)
                # 물고 있는 벽의 점은 카메라와 함께 움직여 픽셀이 고정(8/27 실측: 100mm 올려도 (991→999,248)) → 'wall' 태그.
                #   2차 XY 는 rigid(기둥·밑판) 점만 쓰고, wall 점은 "다르게 물렸다" 감지용.
                hover_dots = [{"kind": k, "px": round(c[0], 1), "py": round(c[1], 1), "area": round(c[2]),
                               "role": ("wall" if (k == ck and c[0] > 600) else "rigid")}
                              for k, v in dots.items() for c in v]
                nw = sum(1 for d in hover_dots if d["role"] == "wall")
                rec("fr5", "dot_place_teach", {}, f"[{ck}] 호버(z{hov[2]:.0f}, +{hover_dz:.0f}) 기준 점 {len(hover_dots)}개(벽 {nw}·강체 {len(hover_dots)-nw}) 캡처 {snap} — 축척은 dot_place_scale 로 별도")
                sc = (ref.get("hover_scale") if abs(float((ref.get("hover_scale") or {}).get("z", -1)) - hov[2]) < 3 else None)
                # 관측 자세로: 먼저 수직 상승 → movej
                up = list(hov); up[2] = obs_tcp[2]; self._move_abs_settle(up)
                self._movej_rad([math.radians(v) for v in obs_j]); time.sleep(0.8)
                posts = self._place_posts_now(cal, line, n=12)
                if not posts:
                    raise RuntimeError(f"[{ck}] observe_place 에서 기둥선 {line} 미검출")
                M = cal["place_scale"]["M"]
                th = self._theta_scale(M, posts[0], posts[1])
                mid = [round((posts[0][0]+posts[1][0])/2, 1), round((posts[0][1]+posts[1][1])/2, 1)]
                def _dd(a, b): return abs((a - b + 180.0) % 360.0 - 180.0)
                auto = min((th + 90.0, th - 90.0), key=lambda x: _dd(x, ref["place_rz"]))
                trim = round(((ref["place_rz"] - auto + 180.0) % 360.0) - 180.0, 3)
                c2 = _j.load(open(CAL)); r2 = c2.setdefault("refs_place", {}).setdefault(ck, {})
                r2.update(ref)
                r2.update({"hover_ref": {"z": round(hov[2], 1), "dots": hover_dots, "img": snap},
                           "hover_scale": sc, "post_ref": {"pts": posts, "mid": mid, "theta": round(th, 3)},
                           "rz_trim": trim, "made": time.strftime("%Y-%m-%d %H:%M")})
                _j.dump(c2, open(CAL, "w"), indent=1, ensure_ascii=False)
                rec("fr5", "dot_place_teach", {}, f"[{ck}] ★ 기준 저장: 기둥 {posts} 중점 {mid} 각 {th:.2f}° · 손목 보정 {trim:+.2f}° (티칭 rz {ref['place_rz']} / 자동 {auto:.2f})")
            finally:
                self.speed = sp0
        self._start(fn, "dot_place_teach")
        return f"[{ck}] 삽입 자세 기록 → 호버 기준·축척 → observe_place 기준 저장 중"

    def dot_place_scale(self, body=None):
        """호버 높이 px/mm 실측 — ★빈 그리퍼로만(벽을 물고 있으면 홈에 걸림). observe_place → 호버 자세 → ±3mm 실측 → observe_place 복귀."""
        import json as _j, math
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        ref = (cal.get("refs_place") or {}).get(ck) or {}
        if "insert_tcp" not in ref: raise RuntimeError(f"[{ck}] place 티칭 먼저")
        self._ensure()
        try:
            g = int(str(self._svc()._call("GetGripperCurPosition()")).strip().split(",")[-1])
        except Exception:
            g = None
        if g is not None and g < 30 and not body.get("force"):
            raise RuntimeError(f"그리퍼 실측 {g} — 벽을 물고 있으면 호버 옆이동이 홈에 걸림. 빈 그리퍼로 실행(강제: force)")
        obs_j = cal["observe_place_joints"]
        def fn():
            sp0 = self.speed; self.speed = max(1, min(2, sp0))          # 8/27: 축척 측정도 2%
            try:
                self._movej_rad([math.radians(v) for v in obs_j]); time.sleep(0.6)
                hov = list(ref["insert_tcp"]); hov[2] += float(ref.get("hover_dz", self.PLACE_HOVER_DZ))
                top = list(hov); top[2] = cal["observe_place_tcp"][2]
                self._move_abs_settle(top); self._move_abs_settle(hov)
                kinds = sorted({d["kind"] for d in (ref.get("hover_ref") or {}).get("dots", [])}) or ["blue", "yellow", "red", "green"]
                sc = self._measure_scale_here(kinds)
                if not sc: raise RuntimeError("호버에서 점 미검출 — 축척 실측 실패")
                c2 = _j.load(open(CAL)); c2["refs_place"][ck]["hover_scale"] = {**sc, "made": time.strftime("%Y-%m-%d %H:%M")}
                _j.dump(c2, open(CAL, "w"), indent=1, ensure_ascii=False)
                rec("fr5", "dot_place_scale", {}, f"[{ck}] 호버 축척 {sc['M']} (점 {sc['kinds']}, z{sc['z']})")
                self._move_abs_settle(top)
                self._movej_rad([math.radians(v) for v in obs_j])
            finally:
                self.speed = sp0
        self._start(fn, "dot_place_scale")
        return f"[{ck}] 호버 축척 실측 시작(빈 그리퍼)"

    def dot_place(self, body=None):
        """벽을 물고 있는 상태에서: observe_place → 1차(기둥선 yaw+XY) → 호버 → 2차(호버 기준) → ★정지(삽입 신호 필수) → 2% 하강 → 그리퍼 열기 → 후퇴."""
        import json as _j, math
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        ref = (cal.get("refs_place") or {}).get(ck) or {}
        if "place_offset" not in ref or "post_ref" not in ref:
            raise RuntimeError(f"[{ck}] place 기준 없음 — 벽을 물고 삽입 자세에 놓은 뒤 'place 티칭' 먼저")
        line = ref["line"]; M = cal["place_scale"]["M"]; obs_j = cal["observe_place_joints"]
        hover_dz = float(ref.get("hover_dz", self.PLACE_HOVER_DZ))
        self._ensure()
        PLACE_SPEED = int(body.get("speed", cal.get("place_speed", 2)))      # 8/27 사용자 요청: place 전 구간 저속(기본 2%)
        INSERT_SPEED = int(body.get("insert_speed", cal.get("place_insert_speed", 1)))   # 삽입 하강 1%
        def fn():
            sp0 = self.speed
            try:
                self._place_go.clear()
                self.speed = max(1, min(PLACE_SPEED, 5))
                rec("fr5", "dot_place", {}, f"[{ck}] 속도 {self.speed}% (삽입 하강 {INSERT_SPEED}%)")
                # ── 0. observe_place ──
                self._movej_rad([math.radians(v) for v in obs_j]); time.sleep(0.8)
                # ── 1차: 기둥선 yaw ──
                posts = self._place_posts_now(cal, line, n=10)
                if not posts: raise RuntimeError(f"[{ck}] 기둥선 {line} 미검출 — 밑판 위치/조명 확인")
                th = self._theta_scale(M, posts[0], posts[1]); th_ref = ref["post_ref"]["theta"]
                dth = self._wrap90(th - th_ref)
                rec("fr5", "dot_place", {}, f"[{ck}] 기둥선 {line} 각 {th:.2f}° 기준 {th_ref:.2f}° → Δ{dth:+.2f}°")
                if abs(dth) > 15: raise RuntimeError(f"[{ck}] yaw 편차 {dth:.1f}° > 15° — 오검출/밑판 이탈 의심, 중단")
                if abs(dth) >= 0.05:
                    cur = self._fk_tcp(); cur[5] += dth; self._move_abs_settle(cur)
                    posts = self._place_posts_now(cal, line, n=8) or posts
                    th2 = self._theta_scale(M, posts[0], posts[1])
                    rec("fr5", "dot_place", {}, f"[{ck}] yaw 회전 {dth:+.2f}° → 잔차 {self._wrap90(th2-th_ref):+.2f}°")
                # ── 1차: XY (기둥 중점) ──
                mid_ref = ref["post_ref"]["mid"]; d_tot = [0.0, 0.0]
                for it in range(3):
                    mid = [(posts[0][0]+posts[1][0])/2, (posts[0][1]+posts[1][1])/2]
                    dpx = [mid[0]-mid_ref[0], mid[1]-mid_ref[1]]
                    d_mm = self._mm_from_dpx(M, dpx)
                    mag = max(abs(v) for v in d_mm)
                    rec("fr5", "dot_place", {}, f"[{ck}] [{it+1}] 중점 Δpx {[round(v,1) for v in dpx]} → Δmm {[round(v,2) for v in d_mm]}")
                    if mag > 40: raise RuntimeError(f"[{ck}] 보정량 {mag:.0f}mm > 40mm — 오검출 의심, 중단")
                    if mag < 0.4: break
                    cur = self._fk_tcp(); cur[0] += d_mm[0]; cur[1] += d_mm[1]; self._move_abs_settle(cur)
                    d_tot[0] += d_mm[0]; d_tot[1] += d_mm[1]
                    posts = self._place_posts_now(cal, line, n=8)
                    if not posts:
                        rec("fr5", "dot_place", {}, f"[{ck}] 재관측 실패 — 적용분 {[round(v,1) for v in d_tot]}mm 로 종료"); break
                aligned = self._fk_tcp()
                # ── 호버 목표: 정렬된 관측 TCP + 오프셋(Δyaw 회전) ──
                off = ref["place_offset"]; r = math.radians(dth)
                ox = off[0]*math.cos(r) - off[1]*math.sin(r); oy = off[0]*math.sin(r) + off[1]*math.cos(r)
                def _dd(a, b): return abs((a - b + 180.0) % 360.0 - 180.0)
                th_now = self._theta_scale(M, posts[0], posts[1])
                rz_auto = min((th_now + 90.0, th_now - 90.0), key=lambda x: _dd(x, ref["place_rz"] + dth))
                # 8/30: body.rz_extra = 글로벌캠으로 계산한 '그리퍼 안 벽 각도' 편차 보정
                #   (손목캠은 물고 나면 벽 점 하나뿐이라 yaw 를 못 잰다 → 글로벌캠이 그 구멍을 메움)
                rz_extra = float((body or {}).get("rz_extra", 0.0))
                if abs(rz_extra) > 3.0:
                    rec("fr5", "dot_place", {}, f"[{ck}] ⚠ rz_extra {rz_extra:+.2f}° > 3° — 오계측 의심, 무시")
                    rz_extra = 0.0
                rz_t = rz_auto + float(ref.get("rz_trim", 0.0)) + rz_extra
                if rz_extra:
                    rec("fr5", "dot_place", {}, f"[{ck}] 글로벌캠 yaw 보정 rz_extra {rz_extra:+.2f}° 적용")
                rec("fr5", "dot_place", {}, f"[{ck}] 손목각: 기둥선 {th_now:.2f}° → rz {rz_t:.2f}° (티칭 {ref['place_rz']}, trim {ref.get('rz_trim',0):+.2f}°)")
                insert_z = aligned[2] + off[2]
                hov = list(aligned); hov[0] += ox; hov[1] += oy; hov[5] = rz_t
                # ★8/30: rx·ry 를 observe_place 자세에서 물려받던 결함(파지와 동일) —
                #   티칭한 삽입 손목 자세가 매번 버려졌다. observe_place 는 수직 대비 1.16° 기울어 있어
                #   벽 밑동이 80mm 아래에서 2.2mm 옆으로 가고, 카메라까지 같이 기울어 밑판 점을 기준
                #   픽셀에 맞추려다 팔이 X-9.4/Y+5.8mm 나 옆으로 갔다(8/30 실측) → 기둥 위에 얹힘.
                _it = ref.get("insert_tcp")
                if _it and cal.get("place_use_taught_rxry", True):
                    _d = ((abs(_it[3]) - abs(aligned[3])) ** 2 + (_it[4] - aligned[4]) ** 2) ** 0.5
                    if _d <= 5.0:
                        hov[3], hov[4] = float(_it[3]), float(_it[4])
                        rec("fr5", "dot_place", {}, f"[{ck}] 손목 자세 = 티칭 삽입값 rx {_it[3]:.2f} ry {_it[4]:.2f} "
                                                    f"(관측자세 rx {aligned[3]:.2f} ry {aligned[4]:.2f}, 차 {_d:.2f}°) "
                                                    f"— 수직 대비 {((180-abs(_it[3]))**2 + _it[4]**2)**0.5:.2f}°")
                    else:
                        rec("fr5", "dot_place", {}, f"[{ck}] ⚠ 티칭 rx/ry 가 관측자세와 {_d:.1f}° 차이 — 오기록 의심, 관측자세 유지")
                self._move_abs_settle(hov)                       # 관측 높이에서 XY·rz 먼저
                hov[2] = insert_z + hover_dz
                self._move_abs_settle(hov)                       # 수직 하강 (전 구간 PLACE_SPEED)
                # ── 2차: 높이별 기준 재보정 (8/28 사용자 설계) ──
                #   refs_place[ck]["hover_refs"] = [{z, dots, M, tcp, img}, ...] — 성공 삽입 자세에서 수직으로 올리며 캡처(dot_place_refs).
                #   590 → … → 571(기둥 진입 직전, 마지막 보정) 순으로 내려가며 매 높이에서 강체 점 yaw + XY 를 기준 픽셀에 맞춘다.
                #   571 아래(기둥 사이)는 옆이동 금지 → 보정 없이 1% 수직 하강만.
                self._cp_geom = {}          # 8/30: 높이별 벽↔밑판 기하(성공 시 승격용)
                cps = list(ref.get("hover_refs") or [])
                if not cps and ref.get("hover_ref") and (ref.get("hover_scale") or {}).get("M"):
                    cps = [{"z": float(ref["hover_ref"]["z"]), "dots": ref["hover_ref"]["dots"], "M": ref["hover_scale"]["M"]}]
                cps.sort(key=lambda c: -float(c["z"]))
                HOV_TOL_PX = float(cal.get("place_hover_tol_px", 1.0))
                if not cps:
                    rec("fr5", "dot_place", {}, f"[{ck}] 호버 기준/축척 없음 — 2차 보정 생략")
                for ci, cp in enumerate(cps):
                    z_cp = float(cp["z"]); hsc = cp["M"]; cdots = cp["dots"]
                    cur = self._fk_tcp()
                    if abs(cur[2] - z_cp) > 0.3:
                        cur[2] = z_cp; self._move_abs_settle(cur)
                    # 벽 물림 검사(첫 체크포인트에서만): 벽 점 픽셀이 기준과 다르면 비뚤게/밀려 물린 것
                    if ci == 0:
                        now0 = self._dots_now(n=8)
                        for rd in [d for d in cdots if d.get("role") == "wall"]:
                            cand = [c for c in now0.get(rd["kind"], []) if c[0] > 600]
                            if not cand:
                                rec("fr5", "dot_place", {}, f"[{ck}] ⚠ 벽 점 미검출(기준 {[rd['px'], rd['py']]}) — 벽 물림 확인 필요"); continue
                            c = min(cand, key=lambda q: (q[0]-rd["px"])**2 + (q[1]-rd["py"])**2)
                            dw = ((c[0]-rd["px"])**2 + (c[1]-rd["py"])**2) ** 0.5
                            if dw > 8:
                                rec("fr5", "dot_place", {}, f"[{ck}] ⚠ 벽 점 {dw:.0f}px 이동(기준 {[rd['px'], rd['py']]} → {[round(c[0]), round(c[1])]}) — 벽이 다르게 물림 의심, 눈으로 확인")
                            else:
                                rec("fr5", "dot_place", {}, f"[{ck}] 벽 물림 OK: 벽 점 Δ{dw:.1f}px")
                    rigid = [d for d in cdots if d.get("role", "rigid") == "rigid"]
                    # ── ★8/30 벽 기준 정렬(wall-referenced) ──
                    #  종전: 강체(밑판) 점을 기준 픽셀로 되돌림 = "카메라를 밑판에 맞춤".
                    #  그런데 카메라와 물고 있는 벽은 한 몸이라, 팔을 움직여도 화면 속 벽 점은 안 움직인다
                    #  → 벽이 그리퍼 안에서 티칭 때와 다르게 물리면(1mm 급) 그 오차를 원리적으로 못 보고
                    #    그대로 기둥 위에 얹혀 걸린다(8/28 빨강 6연속·8/30 파랑 재현).
                    #  대책: 벽 점의 기준 대비 편차 ΔW 를 강체 목표에 더한다 → 목표 P* = P_ref + ΔW.
                    #    (벽−밑판) 상대 배치가 성공 때와 같아진다. 8/30 실증: 걸리던 파지가 X+0.36/Y+0.97mm
                    #    보정만으로 z571→491 한 번에 안착.
                    W = [0.0, 0.0]
                    if cal.get("place_wall_ref", True):
                        wrefs = [d for d in cdots if d.get("role") == "wall"]
                        if wrefs:
                            _now = self._dots_now(n=8); ws = []
                            for rd in wrefs:
                                cand = [c for c in _now.get(rd["kind"], []) if c[0] > 600
                                        and (c[0]-rd["px"])**2 + (c[1]-rd["py"])**2 < 60**2]
                                if cand:
                                    c = min(cand, key=lambda q: (q[0]-rd["px"])**2 + (q[1]-rd["py"])**2)
                                    ws.append((c[0]-rd["px"], c[1]-rd["py"]))
                            if ws:
                                W = [sum(w[0] for w in ws)/len(ws), sum(w[1] for w in ws)/len(ws)]
                                # ★8/30 축척 게인 — 벽 점은 카메라에 d_w(≈90mm) 붙어 있고 밑판 점은 ≈220mm.
                                #   같은 1mm 라도 벽 점이 2~2.4배 크게 움직인다. 벽 편차(px)를 밑판 목표(px)에
                                #   그대로 더하면 그만큼 과보정 → 반대편 기둥에 얹힌다(8/30 빨강 실패 진범).
                                #   d_w = 관측깊이 + pick_offset_z,  f = (end_gap_px/point_span_mm)*관측깊이
                                #   s_w = f/d_w,  gain = s_p/s_w   (s_p = 이 높이의 밑판 축척)
                                gain = (cal.get("place_wall_ref_gain") or {}).get(ck)
                                if gain is None:
                                    try:
                                        pr = (cal.get("refs") or {}).get(ck) or {}
                                        dep = float(pr["target_obs"][2]) - float(pr.get("ref_lift_mm") or 0.0)
                                        d_w = dep + float(pr["pick_offset"][2])
                                        f_px = (float(pr["end_gap_px"]) / float(pr["point_span_mm"])) * dep
                                        s_w = f_px / d_w
                                        s_p = max(abs(hsc[0][0]), abs(hsc[0][1]), abs(hsc[1][0]), abs(hsc[1][1]))
                                        gain = s_p / s_w
                                    except Exception as e:
                                        gain = 0.45
                                        rec("fr5", "dot_place", {}, f"[{ck}] 게인 계산 실패({e}) → 기본 0.45")
                                gain = max(0.15, min(1.2, float(gain)))
                                W = [W[0]*gain, W[1]*gain]
                                if max(abs(v) for v in W) > 20:
                                    rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} ⚠ 벽 점 편차 {[round(v,1) for v in W]}px > 20px "
                                                                "— 오검출/벽 이탈 의심, 벽 기준 보정 생략")
                                    W = [0.0, 0.0]
                                else:
                                    _wmm = self._mm_from_dpx(hsc, W)
                                    rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} 벽 기준(게인 {gain:.2f}): 파지 편차 "
                                                                f"{[round(v,1) for v in ws[0]]}px → 적용 {[round(v,1) for v in W]}px "
                                                                f"= X{_wmm[0]:+.2f} Y{_wmm[1]:+.2f}mm")
                                    # 사용자 요청(8/30): 성공 때의 '벽 점 ↔ 밑판 점' 직선거리·각도를 기준으로 확인
                                    import math as _m
                                    for rd in [d for d in cdots if d.get("role", "rigid") == "rigid"]:
                                        wr = wrefs[0]
                                        cand = [c for c in _now.get(rd["kind"], [])
                                                if (c[0]-rd["px"])**2 + (c[1]-rd["py"])**2 < 100**2]
                                        if not cand: continue
                                        cn = min(cand, key=lambda q: (q[0]-rd["px"])**2 + (q[1]-rd["py"])**2)
                                        wn = (wr["px"] + ws[0][0], wr["py"] + ws[0][1])
                                        dr = (_m.hypot(wr["px"]-rd["px"], wr["py"]-rd["py"]),
                                              _m.degrees(_m.atan2(wr["py"]-rd["py"], wr["px"]-rd["px"])))
                                        dn = (_m.hypot(wn[0]-cn[0], wn[1]-cn[1]),
                                              _m.degrees(_m.atan2(wn[1]-cn[1], wn[0]-cn[0])))
                                        rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} 벽↔{rd['kind']} 거리 "
                                                                    f"{dn[0]:.1f}px (기준 {dr[0]:.1f}, Δ{dn[0]-dr[0]:+.1f}) · "
                                                                    f"각 {dn[1]:.2f}° (기준 {dr[1]:.2f}, Δ{dn[1]-dr[1]:+.2f}°)")
                                        # 사용자 요청(8/30): z 높이별 이 값을 기억해 두고, 삽입이 성공하면 기준으로 승격
                                        g = self._cp_geom.setdefault(round(z_cp), {"z": z_cp, "pairs": []})
                                        g["pairs"] = [q for q in g["pairs"] if q["kind"] != rd["kind"]]
                                        g["pairs"].append({"kind": rd["kind"], "dist_px": round(dn[0], 1),
                                                           "ang_deg": round(dn[1], 2),
                                                           "plate_px": [round(cn[0], 1), round(cn[1], 1)],
                                                           "wall_px": [round(wn[0], 1), round(wn[1], 1)]})
                            else:
                                rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} ⚠ 벽 점 미검출 — 벽 기준 보정 생략(종전 방식)")
                    def _match(now):
                        dl = []; pairs = []
                        for rd in rigid:
                            tx, ty = rd["px"] + W[0], rd["py"] + W[1]      # 8/30: 목표 = 기준 + 파지 편차
                            cand = [c for c in now.get(rd["kind"], []) if (c[0]-tx)**2 + (c[1]-ty)**2 < 100**2]
                            if cand:
                                c = min(cand, key=lambda q: (q[0]-tx)**2 + (q[1]-ty)**2)
                                dl.append((c[0]-tx, c[1]-ty)); pairs.append(((rd["px"], rd["py"]), (c[0], c[1])))
                        return dl, pairs
                    for it in range(4):
                        dl, pairs = _match(self._dots_now(n=8))
                        if not dl:
                            rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} ⚠ 기준 점 미매칭(기준 {len(rigid)}개) — 이 높이 보정 생략, 눈으로 확인"); break
                        # yaw: 강체 점 중 가장 먼 두 점의 선각(축척 M 으로 베이스 사상) 기준 vs 지금
                        if len(pairs) >= 2 and it < 3:
                            import itertools as _it
                            (ra, ca), (rb, cb) = max(_it.combinations(pairs, 2),
                                                     key=lambda pr: (pr[0][0][0]-pr[1][0][0])**2 + (pr[0][0][1]-pr[1][0][1])**2)
                            th_r = self._theta_scale(hsc, ra, rb); th_n = self._theta_scale(hsc, ca, cb)
                            dth_h = ((th_n - th_r + 90.0) % 180.0) - 90.0
                            if abs(dth_h) > 3.0:
                                rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} ⚠ yaw 편차 {dth_h:+.2f}° > 3° — 오검출 의심, yaw 생략")
                            elif abs(dth_h) >= 0.05:
                                cur = self._fk_tcp(); cur[5] += dth_h; self._move_abs_settle(cur)
                                rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} yaw 보정 {it+1}: {th_r:.2f}°→{th_n:.2f}° 회전 {dth_h:+.2f}°")
                                dl, pairs = _match(self._dots_now(n=8))
                                if not dl: break
                        dpx = [sum(d[0] for d in dl)/len(dl), sum(d[1] for d in dl)/len(dl)]
                        d_mm = self._mm_from_dpx(hsc, dpx); mag = max(abs(v) for v in d_mm)
                        dist = (dpx[0]**2 + dpx[1]**2) ** 0.5
                        if dist < HOV_TOL_PX:
                            rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} 검증 OK: {len(dl)}점 Δ{dist:.1f}px ({mag:.2f}mm)"); break
                        if it == 3:
                            rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} 잔차 {dist:.1f}px ({mag:.2f}mm) — 반복 한계, 다음 높이로"); break
                        if mag > 6: d_mm = [v*6/mag for v in d_mm]
                        cur = self._fk_tcp(); cur[0] += d_mm[0]; cur[1] += d_mm[1]; self._move_abs_settle(cur)
                        rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} XY 보정 {it+1}: {len(dl)}점 Δpx {[round(v,1) for v in dpx]} → {[round(v,2) for v in d_mm]}mm")
                    # ★8/30 사용자 기준: 높이마다 a·b·c·d(각 점의 화면 오른쪽끝/아래끝까지 거리)와
                    #   e(벽↔밑판 거리)를 기준과 대조해 기록. 마지막 높이(기둥 진입 직전)에서
                    #   벽 점 편차가 크면 = 파지가 기준과 다르다 = 보정이 근사치뿐 → 삽입 중단.
                    try:
                        _now2 = self._dots_now(n=8)
                        _lines = []
                        for d in cdots:
                            _c = [q for q in _now2.get(d["kind"], [])
                                  if (q[0]-d["px"])**2 + (q[1]-d["py"])**2 < 120**2]
                            if not _c: _lines.append(f"{d['kind'][:3]}미검출"); continue
                            _m = min(_c, key=lambda q: (q[0]-d["px"])**2 + (q[1]-d["py"])**2)
                            _lines.append(f"{'벽' if d.get('role') == 'wall' else d['kind'][:3]}"
                                          f" →우 {1280-_m[0]:.0f}({(1280-_m[0])-(1280-d['px']):+.0f})"
                                          f" →하 {720-_m[1]:.0f}({(720-_m[1])-(720-d['py']):+.0f})")
                        rec("fr5", "dot_place", {}, f"[{ck}] z{z_cp:.0f} a·b·c·d: " + " | ".join(_lines))
                        if ci == len(cps) - 1 and cal.get("place_wall_gate", True):
                            _wr = [d for d in cdots if d.get("role") == "wall"]
                            if _wr:
                                _c = [q for q in _now2.get(_wr[0]["kind"], [])
                                      if q[0] > 600 and (q[0]-_wr[0]["px"])**2 + (q[1]-_wr[0]["py"])**2 < 60**2]
                                if _c:
                                    _m = min(_c, key=lambda q: (q[0]-_wr[0]["px"])**2 + (q[1]-_wr[0]["py"])**2)
                                    _dv = ((_m[0]-_wr[0]["px"])**2 + (_m[1]-_wr[0]["py"])**2) ** 0.5
                                    _lim = float(cal.get("place_wall_dev_max_px", 10.0))
                                    _mm = self._mm_from_dpx(hsc, [_m[0]-_wr[0]["px"], _m[1]-_wr[0]["py"]])
                                    if _dv > _lim:
                                        raise RuntimeError(
                                            f"[{ck}] 벽 점이 기준에서 {_dv:.1f}px 벗어남(한계 {_lim:.0f}px) "
                                            f"= 파지가 기준 대비 X{_mm[0]:+.2f} Y{_mm[1]:+.2f}mm 다름 — "
                                            f"보정은 근사치뿐이라 기둥에 얹힐 위험. 재파지 권장(삽입 중단, 팔은 호버)")
                                    rec("fr5", "dot_place", {}, f"[{ck}] ★ 벽 점 게이트 통과: 기준 대비 {_dv:.1f}px "
                                                                f"(X{_mm[0]:+.2f} Y{_mm[1]:+.2f}mm, 한계 {_lim:.0f}px)")
                                else:
                                    rec("fr5", "dot_place", {}, f"[{ck}] ⚠ 벽 점 미검출 — 게이트 판정 불가")
                    except RuntimeError:
                        raise
                    except Exception as _e:
                        rec("fr5", "dot_place", {}, f"[{ck}] a·b·c·d·e 기록 실패({_e})")
                # ── ★ 삽입 전 정지: 자동 통과 없음 ──
                rec("fr5", "dot_place", {}, f"[{ck}] 호버 정지 z{self._fk_tcp()[2]:.0f} — 눈으로 확인 후 [▶ 삽입] (최대 900s, 자동 진행 없음)")
                t0 = time.time()
                while not self._place_go.is_set():
                    if self._stop.is_set(): raise RuntimeError("STOP 으로 취소됨")
                    if time.time() - t0 > 900: raise RuntimeError(f"[{ck}] 삽입 신호 900s 타임아웃 — 중단(팔은 호버)")
                    time.sleep(0.1)
                # ── 삽입: 수직 하강(INSERT_SPEED, 기본 1%) → 그리퍼 열기 → 후퇴 ──
                self.speed = max(1, min(INSERT_SPEED, 3))
                z_floor = float(ref["insert_tcp"][2]) - float(cal.get("place_z_floor_margin", 3.0))
                if insert_z < z_floor:
                    rec("fr5", "dot_place", {}, f"[{ck}] ⚠ 삽입 z{insert_z:.1f} < 하한 {z_floor:.1f}(티칭 {ref['insert_tcp'][2]}) → 하한으로 제한 (8/28 z455 눌림 사고 방지)")
                    insert_z = z_floor
                down = self._fk_tcp(); down[2] = insert_z; self._move_abs_settle(down, tol=0.3)
                rec("fr5", "dot_place", {}, f"[{ck}] 삽입 하강 완료 z{insert_z:.1f} → 그리퍼 열기")
                self._svc().move(int(body.get("open", 50))); self.gripsvc.wait_done()
                self.speed = max(1, min(PLACE_SPEED, 5))
                up = self._fk_tcp(); up[2] += 60.0; self._move_abs_settle(up, tol=0.3)
                rec("fr5", "dot_place", {}, f"[{ck}] ★ place 완료 — 후퇴 z{up[2]:.0f}")
            finally:
                self.speed = sp0
        self._start(fn, "dot_place")
        return f"[{ck}] dot_place 시작 (observe_place → 기둥선 정렬 → 호버 2차 → 삽입 대기)"

    def dot_place_refs(self, body=None):
        """8/28 사용자 설계: '성공한 삽입 자세'(벽을 문 채, 지금 TCP) 에서 수직으로만 올리며 높이별 기준을 캡처.
        body.zs = [571,575,580,585,590] (기본). 각 높이: 점 픽셀(10프레임 중앙값)·사진·TCP. 축척(M)은 최저·최고 높이에서
        강체 점만으로 ±3mm 실측(면적최대점=벽점 오염 방지), 사이 높이는 선형보간. 끝나면 최고 높이에서 벽을 문 채 정지."""
        import json as _j, math, statistics as S
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        zs = sorted(float(z) for z in (body.get("zs") or cal.get("place_ref_zs") or [571, 575, 580, 585, 590]))
        self._ensure(); self._no_manual()
        ref = cal.setdefault("refs_place", {}).setdefault(ck, {})
        cur0 = self._fk_tcp()
        obs_tcp = cal["observe_place_tcp"]
        ref.update({"insert_tcp": [round(v, 2) for v in cur0], "place_rz": round(cur0[5], 2),
                    "place_offset": [round(cur0[i]-obs_tcp[i], 2) for i in range(3)],
                    "insert_joints": [round(math.degrees(v), 3) for v in self.node.cur]})
        _j.dump(cal, open(CAL, "w"), indent=1, ensure_ascii=False)
        rec("fr5", "dot_place_refs", {}, f"[{ck}] 성공 삽입 자세 기록 tcp {ref['insert_tcp'][:3]} rz {ref['place_rz']} → 높이 {zs} 기준 캡처 시작")
        def _dots_med(n=10):
            d = self._dots_now(n=n)
            out = []
            for k, v in d.items():
                for c in v:
                    out.append({"kind": k, "px": round(c[0], 1), "py": round(c[1], 1), "area": round(c[2]),
                                "role": ("wall" if (k == ck and c[0] > 600) else "rigid")})
            return out
        def _rigid_pts(dl): return [(d["px"], d["py"]) for d in dl if d["role"] == "rigid"]
        def _scale_rigid(base, step=3.0):
            p0 = _rigid_pts(_dots_med(8))
            def _shift(axis):
                t = list(base); t[axis] += step; self._move_abs_settle(t); p1 = _rigid_pts(_dots_med(8)); self._move_abs_settle(base)
                dd = []
                for a in p0:
                    b = min(p1, key=lambda q: (q[0]-a[0])**2 + (q[1]-a[1])**2) if p1 else None
                    if b and (b[0]-a[0])**2 + (b[1]-a[1])**2 < 60**2: dd.append(((b[0]-a[0])/step, (b[1]-a[1])/step))
                return dd
            sx = _shift(0); sy = _shift(1)
            if not sx or not sy: return None
            return [[round(S.mean(v[0] for v in sx), 3), round(S.mean(v[0] for v in sy), 3)],
                    [round(S.mean(v[1] for v in sx), 3), round(S.mean(v[1] for v in sy), 3)]]
        def fn():
            sp0 = self.speed; self.speed = 2
            try:
                out = []
                for z in zs:
                    t = list(cur0); t[2] = z
                    tcp = self._move_abs_settle(t); time.sleep(0.8)
                    dl = _dots_med(10)
                    snap = self._cam_snap(f"/home/ar/bf2_console/refs/place_{ck}_z{int(z)}_ref.jpg")
                    nw = sum(1 for d in dl if d["role"] == "wall"); nr = len(dl) - nw
                    M = None
                    if z in (zs[0], zs[-1]):
                        M = _scale_rigid(list(t))
                        if M: rec("fr5", "dot_place_refs", {}, f"[{ck}] z{z:.0f} 축척 실측 M={M}")
                    out.append({"z": z, "tcp": [round(v, 2) for v in tcp], "dots": dl, "img": snap, "M": M})
                    rec("fr5", "dot_place_refs", {}, f"[{ck}] z{z:.0f} 기준 캡처: 강체 {nr}·벽 {nw} 점 — {[(d['kind'], d['px'], d['py']) for d in dl if d['role']=='rigid']}")
                # 축척 보간 (1/(z-zs) 대신 선형 — 20mm 구간이라 오차 <1%)
                lo = next((o for o in out if o["M"]), None); hi = next((o for o in reversed(out) if o["M"]), None)
                for o in out:
                    if o["M"] is None and lo and hi and hi["z"] != lo["z"]:
                        f = (o["z"] - lo["z"]) / (hi["z"] - lo["z"])
                        o["M"] = [[round(lo["M"][r][c] + f*(hi["M"][r][c]-lo["M"][r][c]), 3) for c in range(2)] for r in range(2)]
                    elif o["M"] is None and (lo or hi):
                        o["M"] = (lo or hi)["M"]
                # 8/28 저녁: 관측 기준(기둥선 각·중점·rz_trim)도 여기서 저장 — 새 색은 teach 없이 이 액션 하나로 place 준비 완료
                line = (cal.get("place_lines") or {}).get(ck) or self.PLACE_LINES_DEFAULT[ck]
                obs_j = cal["observe_place_joints"]
                up = list(cur0); up[2] = cal["observe_place_tcp"][2]; self._move_abs_settle(up)
                self._movej_rad([math.radians(v) for v in obs_j]); time.sleep(0.8)
                posts = self._place_posts_now(cal, line, n=12)
                post_ref = None; trim = None
                if posts:
                    Mo = cal["place_scale"]["M"]
                    th = self._theta_scale(Mo, posts[0], posts[1])
                    mid = [round((posts[0][0]+posts[1][0])/2, 1), round((posts[0][1]+posts[1][1])/2, 1)]
                    def _dd(a, b): return abs((a - b + 180.0) % 360.0 - 180.0)
                    auto = min((th + 90.0, th - 90.0), key=lambda x: _dd(x, cur0[5]))
                    trim = round(((cur0[5] - auto + 180.0) % 360.0) - 180.0, 3)
                    post_ref = {"pts": posts, "mid": mid, "theta": round(th, 3)}
                    rec("fr5", "dot_place_refs", {}, f"[{ck}] 관측 기준: 기둥 {posts} 중점 {mid} 각 {th:.2f}° · 손목 보정 {trim:+.2f}°")
                else:
                    rec("fr5", "dot_place_refs", {}, f"[{ck}] ⚠ observe_place 에서 기둥선 {line} 미검출 — post_ref 미저장(기존 유지)")
                c2 = _j.load(open(CAL)); r2 = c2.setdefault("refs_place", {}).setdefault(ck, {})
                if post_ref: r2.update({"line": line, "post_ref": post_ref, "rz_trim": trim})
                top = out[-1]
                r2.update({"hover_refs": out, "hover_ref": {"z": top["z"], "dots": top["dots"], "img": top["img"], "tcp": top["tcp"]},
                           "hover_scale": ({"M": top["M"], "z": top["z"], "kinds": ["rigid"]} if top["M"] else r2.get("hover_scale")),
                           "hover_dz": round(top["z"] - cur0[2], 1),
                           "refs_made": time.strftime("%Y-%m-%d %H:%M") + f" 성공 삽입 z{cur0[2]:.1f} 에서 수직 캡처 {[int(z) for z in zs]}"})
                _j.dump(c2, open(CAL, "w"), indent=1, ensure_ascii=False)
                rec("fr5", "dot_place_refs", {}, f"[{ck}] ★ 높이별 기준 {len(out)}개 + 관측 기준 저장 (hover_dz {r2['hover_dz']}) — 벽 문 채 observe_place 정지")
            finally:
                self.speed = sp0
        self._start(fn, "dot_place_refs")
        return f"[{ck}] 높이별 기준 캡처 시작 {zs}"

    def dot_pick_teach(self, body=None):
        """8/26: 지금 TCP 를 <kind> 벽의 pick 자세로 기록 — 관측 자세(캘리브 observe_tcp) 기준 상대 오프셋.
        사용법: 부품을 정위치에 두고 dot_align(ref) 저장 → 수동으로 그리퍼를 그 벽 파지 위치에 놓고 호출."""
        import json as _j
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        self._ensure()
        ref = cal.setdefault("refs", {}).setdefault(ck, {})
        cur = self._fk_tcp()
        obs = cal["observe_tcp"]
        ref["pick_offset"] = [round(cur[i] - obs[i], 2) for i in range(3)]      # base XYZ mm
        ref["pick_rz"] = round(cur[5], 2)
        ref["pick_tcp_taught"] = [round(v, 2) for v in cur]
        ref["pick_joints_taught"] = [round(math.degrees(v), 3) for v in self.node.cur]
        ref.pop("check_px", None)
        ref.pop("above_center_px", None)   # 8/26: 파지점 바뀌면 above 재보정 기준도 무효 → 다음 파지 때 재학습
        _j.dump(cal, open(CAL, "w"), indent=1, ensure_ascii=False)
        rec("fr5", "dot_pick_teach", {}, f"[{ck}] pick 오프셋 {ref['pick_offset']} rz {ref['pick_rz']}")
        # 8/26 사용자 지적: 오프셋(티칭 순간 벽 위치)과 기준 픽셀(예전 저장)이 어긋나면 그 차이가 매번 남는다
        #   → 티칭 직후 그리퍼 열고 +150 올린 뒤 관측 자세에서 같은 색 기준을 재저장(resave_ref 기본 True)
        if body.get("resave_ref", True):
            import threading as _th
            def _later():
                try:
                    t0 = time.time()
                    while self.busy and time.time() - t0 < 30: time.sleep(0.1)
                    if self.manual: self.set_manual(False)
                    t0 = time.time()
                    while self.busy and time.time() - t0 < 30: time.sleep(0.1)
                    # 8/30: 하드코딩 50 → 이 색의 grip_open 사용. 그리퍼 교체 후 50 이 옆 벽에
                    #   닿을 수 있어 사용자가 잡아준 개도를 그대로 쓴다(최대 GRIP_MAX=50).
                    self.gripper(min(GRIP_MAX, int(ref.get("grip_open", 50))))
                    t0 = time.time()
                    while self.busy and time.time() - t0 < 30: time.sleep(0.1)
                    self.cart_jog(2, 150.0)
                    t0 = time.time()
                    while self.busy and time.time() - t0 < 60: time.sleep(0.1)
                    self.dot_align({"ref": 1, "kind": ck, "station": "pick"})
                    t0 = time.time()                       # 8/27: dot_align 은 비동기 → 기준 저장 끝나고 읽어야 함(안 그러면 옛 θ 로 계산)
                    time.sleep(1.0)
                    while self.busy and time.time() - t0 < 60: time.sleep(0.2)
                    time.sleep(0.5)
                    # 8/27: 손으로 맞춘 손목각과 '벽선각±90°' 자동값의 차이를 보정항으로 학습.
                    #   (그리퍼·벽 기하가 정확히 직각이 아니라 1~2° 차이 — 안 남기면 수동 조정분이 매번 버려짐)
                    c2 = _j.load(open(CAL)); r2 = c2.get("refs", {}).get(ck, {})
                    # ★8/30: pick_offset 을 '저장된' observe_tcp 기준으로 잡던 계통오차 제거.
                    #   기준 픽셀을 저장한 그 순간의 '실제' 관측 TCP 로 다시 계산한다.
                    #   (관측 자세는 매번 0.5~1mm 다르게 서고, 기준 픽셀도 5px≈1.6mm 씩 흔들린다.
                    #    저장 픽셀과 실제 TCP 를 같은 순간의 짝으로 묶어야 그 차이가 상쇄된다.)
                    obs_real = self._fk_tcp()
                    off_new = [round(cur[i] - obs_real[i], 2) for i in range(3)]
                    off_old = r2.get("pick_offset")
                    r2["pick_offset"] = off_new
                    r2["pick_offset_basis"] = [round(v, 2) for v in obs_real]
                    c2["refs"][ck] = r2; _j.dump(c2, open(CAL, "w"), indent=1, ensure_ascii=False)
                    if off_old:
                        rec("fr5", "dot_pick_teach", {}, f"[{ck}] 오프셋 실측 관측TCP 기준 재계산 {off_old} → {off_new} "
                                                         f"(차 {[round(off_new[i]-off_old[i], 2) for i in range(3)]}mm)")
                    th2 = r2.get("target_theta_deg"); rzt = r2.get("pick_rz")
                    if th2 is not None and rzt is not None:
                        def _dd(a, b): return abs((a - b + 180.0) % 360.0 - 180.0)
                        auto = min((th2 + 90.0, th2 - 90.0), key=lambda x: _dd(x, rzt))
                        r2["yaw_trim_deg"] = round(((rzt - auto + 180.0) % 360.0) - 180.0, 3)
                        c2["refs"][ck] = r2; _j.dump(c2, open(CAL, "w"), indent=1, ensure_ascii=False)
                        rec("fr5", "dot_pick_teach", {}, f"[{ck}] 손목각 보정 학습 yaw_trim_deg {r2['yaw_trim_deg']:+.2f}° (티칭 rz {rzt} / 자동 {auto:.2f})")
                except Exception as e:
                    rec("fr5", "dot_pick_teach", {}, f"[{ck}] 기준 재저장 실패: {e}")
            _th.Thread(target=_later, daemon=True).start()
            return f"[{ck}] offset={ref['pick_offset']} rz={ref['pick_rz']} → 그리퍼 열고 올려 관측에서 기준 재저장 중"
        return f"[{ck}] offset={ref['pick_offset']} rz={ref['pick_rz']}"

    def _cam_snap(self, path):
        """손목캠 /stream 에서 JPEG 1장 저장(소스 무관: rs/udp/ros). 실패 시 None."""
        import urllib.request
        try:
            r = urllib.request.urlopen("http://localhost:8766/stream", timeout=5)
            buf = b""
            for _ in range(200):
                buf += r.read(8192)
                a = buf.find(b"\xff\xd8"); b = buf.find(b"\xff\xd9", a + 2)
                if a >= 0 and b > 0:
                    open(path, "wb").write(buf[a:b+2]); return path
        except Exception:
            pass
        return None

    def _refine_low(self, ck, ref, cal, pause_z, body=None):
        """8/27 사용자 요청: z380(파지 직전) 에서 '한 번 더' 검증+보정.
        잘 잡혔을 때의 z380 카메라 화면·점 픽셀을 기준(ref["low_ref"])으로 저장해 두고, 이후 매번 z380 에서
        같은 색 점(기준에 최근접)을 그 픽셀로 보내도록 XY 보정(최대 2회, 회당 ≤6mm). 기준이 없으면 후보를 돌려주고
        파지 성공(상태 2) 뒤 dot_pick 이 기준으로 확정한다. 반환: {"px","img","z","n"} 후보 또는 None."""
        import json as _j, math, urllib.request
        body = body or {}
        def wrap90(a):          # ±90° 랩 (dot_align 의 동명 내부함수와 같은 규약 — 스코프가 달라 여기 재정의)
            while a > 90: a -= 180
            while a <= -90: a += 180
            return a
        # 8/27: 이 높이의 px/mm 는 '실측'이 있으면 그것을 쓴다(모델 추정은 z380 에서 7.66 vs 실측 8.0 로 4% 틀림).
        #   low_scale = {"380": {"M": [[dpx_x/mmX, dpx_x/mmY],[dpx_y/mmX, dpx_y/mmY]], "rz": 측정 당시 손목각}}
        ls = (cal.get("low_scale") or {}).get(str(int(float(pause_z))))
        if ls and ls.get("M"):
            M2 = ls["M"]; ratio = 1.0; rz_base = float(ls.get("rz", cal.get("calib_rz", 0.0)))
        else:
            M = cal["M_obs_per_mm"]
            z_obs = float((cal.get("observe_tcp") or [0, 0, 550])[2])
            d_obs = float((cal.get("target_obs") or [0, 0, 287])[2])
            d_low = max(40.0, d_obs - (z_obs - float(pause_z)))
            ratio = d_obs / d_low
            M2 = [[M[0][0], M[0][1]], [M[1][0], M[1][1]]]; rz_base = float(cal.get("calib_rz", 0.0))
        det = M2[0][0]*M2[1][1] - M2[0][1]*M2[1][0]
        Mi = [[M2[1][1]/det, -M2[0][1]/det], [-M2[1][0]/det, M2[0][0]/det]]
        rz_now = self._fk_tcp()[5]; drz = math.radians(((rz_now - rz_base + 180) % 360) - 180)
        cr, sr = math.cos(drz), math.sin(drz)

        def seen_px():
            for _ in range(8):
                try:
                    dd = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=5))
                    pts = [(t["px"], t["py"]) for t in dd["dots"] if t["kind"] == ck]
                    if pts: return pts
                except Exception:
                    pass
                time.sleep(0.15)
            return []

        lref = ref.get("low_ref") if not body.get("relearn_low") else None
        if lref and abs(float(lref.get("z", pause_z)) - float(pause_z)) > 2:
            rec("fr5", "dot_pick", {}, f"[{ck}] low_ref 는 z{lref.get('z')} 기준 — 지금 z{float(pause_z):.0f} 와 달라 미사용")
            lref = None
        pts = seen_px()
        if not pts:
            rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 검증: {ck} 점 0개 — 보정 불가")
            return None
        def img_theta(ps):
            """같은 색 점 2개 이상 → 가장 먼 두 점을 잇는 선의 '이미지 각'(°, ±90 랩).
            이미지 각 = 손목(카메라)과 벽의 상대 각도 → 기준과 같게 만들면 손목각이 기준과 같아진다."""
            if len(ps) < 2: return None
            a, b = max(((i, j) for i in range(len(ps)) for j in range(i+1, len(ps))),
                       key=lambda ij: (ps[ij[0]][0]-ps[ij[1]][0])**2 + (ps[ij[0]][1]-ps[ij[1]][1])**2)
            if (ps[a][0]-ps[b][0])**2 + (ps[a][1]-ps[b][1])**2 < 60**2:
                return None                      # 너무 가까운 두 점은 각도 신뢰 못함
            return wrap90(math.degrees(math.atan2(ps[b][1]-ps[a][1], ps[b][0]-ps[a][0])))

        if not lref:
            snap = self._cam_snap(f"/home/ar/bf2_console/refs/{ck}_z{int(float(pause_z))}_cand.jpg")
            # 기준 없음: 화면 중앙에 가장 가까운 점을 후보로(그리퍼 바로 아래 = 파지점 쪽)
            c = min(pts, key=lambda q: (q[0]-640)**2 + (q[1]-360)**2)
            th_i = img_theta(pts)
            rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 검증 기준 없음 → 후보 px {list(c)} ({len(pts)}개 중"
                                       + (f", 선각 {th_i:.2f}°" if th_i is not None else ", 1점이라 yaw 기준 없음") + f") 캡처 {snap} — 파지 성공 시 기준 확정")
            return {"px": [float(c[0]), float(c[1])], "pts": [[float(q[0]), float(q[1])] for q in pts],
                    "theta_img": (None if th_i is None else round(th_i, 3)),
                    "img": snap, "z": float(pause_z), "n": len(pts)}
        rp = lref["px"]
        # ── ①' z380 yaw 재정렬 (사용자 8/27): 기준 화면의 선각과 지금 선각을 맞춘다.
        #     이미지 각 = 손목 대비 벽 각도 → 기준과 같게 하면 손목각이 기준 파지 때와 동일해진다.
        th_ref_i = lref.get("theta_img")
        th_cur_i = img_theta(pts)
        if th_ref_i is not None and th_cur_i is not None:
            dth = wrap90(th_cur_i - th_ref_i) * float(cal.get("yaw_sign", 1))
            if abs(dth) > 15:
                rec("fr5", "dot_pick", {}, f"[{ck}] ⚠ z{float(pause_z):.0f} yaw 편차 {dth:+.2f}° > 15° — 오검출 의심, yaw 생략")
            elif abs(dth) >= 0.05:
                cur = self._fk_tcp(); cur[5] += dth
                self._cart_ref = list(cur); self._move_cart_line(cur); time.sleep(0.5)
                pts2 = seen_px()
                th2 = img_theta(pts2) if pts2 else None
                res = None if th2 is None else wrap90(th2 - th_ref_i)
                rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} yaw 재정렬 {dth:+.2f}° (기준 선각 {th_ref_i:.2f}° → 관측 {th_cur_i:.2f}°)"
                                           + (f" 잔차 {res:+.2f}°" if res is not None else ""))
                if pts2: pts = pts2
            else:
                rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} yaw 일치({dth:+.2f}°)")
        elif th_ref_i is None and th_cur_i is not None:
            rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 기준에 선각 없음(1점 기준) — yaw 생략")
        elif th_cur_i is None:
            rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 점 {len(pts)}개 — 2점 미만이라 yaw 생략(XY 만)")
        for it in range(2):
            near = min(pts, key=lambda q: (q[0]-rp[0])**2 + (q[1]-rp[1])**2)
            dpx = [near[0]-rp[0], near[1]-rp[1]]
            dist = (dpx[0]**2 + dpx[1]**2) ** 0.5
            if dist > 150:
                rec("fr5", "dot_pick", {}, f"[{ck}] ⚠ z{float(pause_z):.0f} 검증: 기준 {rp} 최근접 {list(near)} {dist:.0f}px — 다른 점 의심, 보정 생략(눈으로 확인)")
                return None
            v = [Mi[0][0]*dpx[0] + Mi[0][1]*dpx[1], Mi[1][0]*dpx[0] + Mi[1][1]*dpx[1]]
            v = [v[0]/ratio, v[1]/ratio]
            d_mm = [-(cr*v[0] - sr*v[1]), -(sr*v[0] + cr*v[1])]     # 손목 yaw 차이만큼 회전(캘리브 rz 기준)
            mag = max(abs(d_mm[0]), abs(d_mm[1]))
            if mag < 0.25 or dist < 2:
                rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 검증 OK: 기준 {rp} 현재 {list(near)} Δ{dist:.1f}px ({mag:.2f}mm) — 보정 불필요")
                return None
            if mag > 6:
                d_mm = [d_mm[0]*6/mag, d_mm[1]*6/mag]
            cur = self._fk_tcp(); cur[0] += d_mm[0]; cur[1] += d_mm[1]
            self._cart_ref = list(cur); self._move_cart_line(cur); time.sleep(0.4)
            rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 보정 {it+1}: Δpx {[round(x,1) for x in dpx]} → {[round(x,2) for x in d_mm]}mm (축척 x{ratio:.2f}, rz차 {math.degrees(drz):+.1f}°)")
            pts = seen_px()
            if not pts:
                rec("fr5", "dot_pick", {}, f"[{ck}] 보정 후 점 0개 — 중단"); return None
        near = min(pts, key=lambda q: (q[0]-rp[0])**2 + (q[1]-rp[1])**2)
        dist = ((near[0]-rp[0])**2 + (near[1]-rp[1])**2) ** 0.5
        rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 보정 후 잔차 {dist:.1f}px (기준 {rp} 현재 {list(near)})")
        return None

    def _refine_above(self, ck, ref, cal, station="pick"):
        """8/26 비주얼 서보잉: 파지점 바로 위(above·관측과 같은 높이)에서 벽 가운데 점을 다시 관측,
        above 기준(ref["above_center_px"]) 대비 픽셀차를 2x2 역행렬로 mm 보정. above 기준이 없으면
        지금 관측을 기준으로 학습(각 색 첫 파지 시 자동). 카메라가 파지점 바로 위라 티칭·접근 누적오차가
        이 자리에서 상쇄된다. 반환: 보정 mm(list) / "learned" / None(관측부족·과도)."""
        import urllib.request, json as _j
        M_obs = cal["M_obs_per_mm"]
        two_lower = bool(ref.get("two_dot_lower"))
        nom = (cal.get("target_obs") or [0, 0, 287])[2]

        def center_px():
            d = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=5))
            same = sorted([t for t in d["dots"] if t["kind"] == ck], key=lambda t: -t["area"])
            if two_lower:                          # 초록: 위+가운데 2점, 아래(py 큰)=가운데
                two = sorted(same[:2], key=lambda t: t["py"])
                if len(two) == 2:
                    return [two[1]["px"], two[1]["py"]]
                return None
            # 3점: 일직선+등간격 조합에서 가운데
            from itertools import combinations
            best, bsc = None, None
            for tri in combinations(same[:6], 3):
                pts = sorted([(t["px"], t["py"]) for t in tri], key=lambda q: q[1])
                (x0, y0), (x1, y1), (x2, y2) = pts
                gap = ((x2-x0)**2 + (y2-y0)**2) ** 0.5
                if not (250 <= gap <= 650): continue
                mx, my = (x0+x2)/2, (y0+y2)/2
                sc = ((x1-mx)**2 + (y1-my)**2) ** 0.5 + 2*abs((x2-x0)*(y0-y1)-(x0-x1)*(y2-y0))/gap
                if bsc is None or sc < bsc: best, bsc = pts, sc
            if best is not None and bsc < 40:
                return [best[1][0], best[1][1]]     # 가운데 점
            return None

        acc = []
        last_seen = []
        for _ in range(24):
            try:
                c = center_px()
                try:      # 8/27 진단: 실패 시 "무엇이 보였는지" 남기기 (0프레임 원인 규명용)
                    d = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=3))
                    last_seen = [(t["px"], t["py"], int(t["area"])) for t in d["dots"] if t["kind"] == ck]
                except Exception:
                    pass
            except Exception:
                c = None
            if c: acc.append(c)
            if len(acc) >= 6: break
            time.sleep(0.15)
        if len(acc) < 3:
            tcp = [round(v, 1) for v in self._fk_tcp()[:3]]
            rec("fr5", "dot_pick", {}, f"[{ck}] above 재보정 생략 — 가운데 점 {len(acc)}프레임(부족) · above tcp {tcp} · "
                                       f"보인 {ck} 점 {len(last_seen)}개 {last_seen[:6]}")
            return None
        cur_px = [sum(a[i] for a in acc)/len(acc) for i in range(2)]
        aref = ref.get("above_center_px")
        if not aref:
            ref["above_center_px"] = [round(v, 1) for v in cur_px]
            _j.dump(cal, open("/home/ar/bf2_console/dot_calib.json", "w"), indent=1, ensure_ascii=False)
            rec("fr5", "dot_pick", {}, f"[{ck}] above 기준 학습 {ref['above_center_px']} (첫 파지)")
            return "learned"
        dpx = [cur_px[0]-aref[0], cur_px[1]-aref[1]]
        M2 = [[M_obs[0][0], M_obs[0][1]], [M_obs[1][0], M_obs[1][1]]]
        det = M2[0][0]*M2[1][1] - M2[0][1]*M2[1][0]
        Mi2 = [[M2[1][1]/det, -M2[0][1]/det], [-M2[1][0]/det, M2[0][0]/det]]
        d_mm = [-(Mi2[0][0]*dpx[0] + Mi2[0][1]*dpx[1]),
                -(Mi2[1][0]*dpx[0] + Mi2[1][1]*dpx[1])]
        mag = max(abs(d_mm[0]), abs(d_mm[1]))
        if mag > 15:
            rec("fr5", "dot_pick", {}, f"[{ck}] above 재보정 {mag:.1f}mm > 15 — 과도, 생략")
            return None
        if mag < 0.2:
            rec("fr5", "dot_pick", {}, f"[{ck}] above 재보정 불필요(<0.2mm)")
            return d_mm
        cur = self._fk_tcp(); cur[0] += d_mm[0]; cur[1] += d_mm[1]
        self._cart_ref = list(cur); self._move_cart_line(cur); time.sleep(0.3)
        rec("fr5", "dot_pick", {}, f"[{ck}] above 재보정 Δpx {[round(v,1) for v in dpx]} → {[round(v,2) for v in d_mm]}mm")
        return d_mm

    def dot_pick(self, body=None):
        """8/26: 정렬(Δ·yaw) → 상대 오프셋으로 벽 위 접근(관측 높이) → 하강 → 그리퍼 → 실측 게이트.
        헛집음(실측이 명령값까지 닫힘)이면 예외. grip 명령값 body.grip(기본 10), 게이트 임계 body.gate_min(기본 25)."""
        import json as _j
        import urllib.request
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        ck = body.get("kind") or cal["kind"]
        ref = cal.get("refs", {}).get(ck) or {}
        if "pick_offset" not in ref:
            raise RuntimeError(f"[{ck}] pick 오프셋 없음 — 수동으로 파지 위치에 놓고 'pick 티칭' 먼저")
        grip_cmd = int(body.get("grip", ref.get("grip_close", 10)))
        grip_open = int(body.get("grip_open", ref.get("grip_open", 100)))     # 8/26: 하강 시 100 벌림
        descend_speed = int(body.get("descend_speed", ref.get("descend_speed", 2)))   # 하강만 저속(기본 2%)
        gate_min = float(body.get("gate_min", 25))
        align_start = self.dot_align({"kind": ck, "station": "pick"})
        # dot_align 은 스레드로 돌므로 완료 대기
        def fn():
            la = getattr(self, "last_align", None) or {}
            if la.get("residual_px") is None and la.get("yaw_deg") is None and not la.get("d_mm"):
                raise RuntimeError("정렬 결과 없음 — dot_align 실패")
            dyaw = float(la.get("yaw_deg") or 0.0)
            cur = self._fk_tcp()                      # 보정된 관측 TCP
            off = ref["pick_offset"]
            c, s_ = math.cos(math.radians(dyaw)), math.sin(math.radians(dyaw))
            ox, oy = off[0]*c - off[1]*s_, off[0]*s_ + off[1]*c   # 오프셋도 yaw 만큼 회전
            # 접근 전 그리퍼 벌림(기본 50) — 벽 윗변이 손가락 사이로 들어오게
            self._svc().move(grip_open); self.gripsvc.wait_done()
            # 파지 손목각 = 티칭한 pick_rz + 보정 yaw (관측 rz 가 아님 — 8/26 티칭 시 2.7° 돌려 잡음)
            rz_taught = float(ref.get("pick_rz", cur[5])) + dyaw
            th_now = la.get("theta_deg")
            if th_now is not None and not ref.get("yaw_from_teach", False):
                # 8/26: 손 티칭으로 1~2° 못 맞춤 → 그리퍼를 벽선(베이스 절대각)에 수직으로 자동 정렬.
                #   후보 θ+90k 중 티칭 rz 에 가장 가까운 것(180° 대칭·잡는 방향 선택은 티칭이 담당)
                cands = [th_now + 90.0 * k for k in range(-4, 5)]
                def ddeg(a, b): return abs((a - b + 180.0) % 360.0 - 180.0)
                rz_auto = min(cands, key=lambda c: ddeg(c, rz_taught))
                rec("fr5", "dot_pick", {}, f"손목각 자동: 벽 {th_now:.2f}° → rz {rz_auto:.2f}° (티칭 {rz_taught:.2f}°, 차 {((rz_auto-rz_taught+180)%360-180):+.2f}°)")
                rz_t = rz_auto + float(ref.get("yaw_trim_deg", 0.0))
            else:
                rz_t = rz_taught + float(ref.get("yaw_trim_deg", 0.0))
            above = list(cur); above[0] += ox; above[1] += oy; above[5] = rz_t
            # ★8/30: rx·ry 를 '관측 자세' 에서 물려받던 결함 — 티칭한 손목 자세가 매번 버려졌다.
            #   관측 자세는 수직(rx=±180, ry=0) 대비 1.46° 기울어 있어(rx 178.76/ry 0.79),
            #   자동 파지는 항상 기운 손목으로 벽을 물었다 → 벽 밑동이 80mm 아래에서 2.0mm 옆으로
            #   → 기둥 윗면에 얹혀 삽입 실패(8/28 빨강 6연속·8/30 재현). 사용자가 "4번 조인트가
            #   바닥과 평행하지 않은 느낌"이라고 짚어 발견.
            tt = ref.get("pick_tcp_taught")
            if tt and cal.get("pick_use_taught_rxry", True):
                d_ang = ((abs(tt[3]) - abs(cur[3])) ** 2 + (tt[4] - cur[4]) ** 2) ** 0.5
                if d_ang <= 5.0:
                    above[3], above[4] = float(tt[3]), float(tt[4])
                    rec("fr5", "dot_pick", {}, f"[{ck}] 손목 자세 = 티칭값 rx {tt[3]:.2f} ry {tt[4]:.2f} "
                                               f"(관측자세 rx {cur[3]:.2f} ry {cur[4]:.2f}, 차 {d_ang:.2f}°) "
                                               f"— 수직 대비 {((180-abs(tt[3]))**2 + tt[4]**2)**0.5:.2f}°")
                else:
                    rec("fr5", "dot_pick", {}, f"[{ck}] ⚠ 티칭 rx/ry 가 관측자세와 {d_ang:.1f}° 차이 — 오기록 의심, 관측자세 유지")
            self._cart_ref = list(above); self._move_cart_line(above); time.sleep(0.4)
            # 8/26 비주얼 서보잉: 파지점 바로 위에서 재관측 정밀보정(누적오차 상쇄). 초록 2점 모드도 자동 적용.
            # 8/27: above 재보정은 기본 OFF — above 에서 벽 끝점이 화각 밖(3점 중 2점만 보임, 실측)이라 3점 판정 불가.
            #   대신 z380 검증(_refine_low, 학습 기준 최근접 점 방식)이 더 가까운 높이에서 같은 역할. 켜려면 body.refine_above=true
            if body.get("refine_above") and not body.get("no_refine"):
                self._refine_above(ck, ref, cal)
                above = self._fk_tcp(); above[5] = rz_t
            down = list(above); down[2] += off[2] - float(la.get("lift_mm", 0.0))   # 8/26: Z 올려 관측했으면 그만큼 더 내려가 원래 파지 높이로
            # 8/26 사용자 요청: 파지점 전 pause_z(기본 400) 에서 정지 → /fr5/dot_pick_continue(콘솔 [▶ 하강]) 올 때까지 대기
            pause_z = body.get("pause_z", cal.get("pick_pause_z", 400.0))
            low_cand = None
            if pause_z is not None and float(pause_z) > down[2] + 5:
                mid = list(above); mid[2] = float(pause_z)
                self._cart_ref = list(mid); self._move_cart_line(mid); time.sleep(0.3)
                self._pick_go.clear()
                # 8/27: z380 '한 번 더' 검증+보정 (기준 = 잘 잡혔을 때의 화면·픽셀, 파지 성공 시 자동 확정)
                low_cand = None
                if not body.get("no_low_refine"):
                    try:
                        low_cand = self._refine_low(ck, ref, cal, float(pause_z), body)
                    except Exception as e:
                        rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 검증 오류 {e} — 건너뜀")
                    now_tcp = self._fk_tcp(); down[0], down[1] = now_tcp[0], now_tcp[1]   # 보정된 XY 로 하강
                # 8/26 사용자 지적: 잡기 전 재검출 — 이 자세에서 그 색 점이 보이는지 + 티칭 때 픽셀(check_px)과 비교
                seen = []
                try:
                    for _ in range(6):
                        dd = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=5))
                        seen = [(t["px"], t["py"]) for t in dd["dots"] if t["kind"] == ck]
                        if seen: break
                        time.sleep(0.2)
                except Exception:
                    pass
                chk = ref.get("check_px")
                near = None
                if seen and chk:
                    near = min(seen, key=lambda q: (q[0]-chk[0])**2 + (q[1]-chk[1])**2)
                    dist = ((near[0]-chk[0])**2 + (near[1]-chk[1])**2) ** 0.5
                    rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 재검출 {len(seen)}개, 기준 {chk} 최근접 {near} 거리 {dist:.0f}px")
                    if dist > 80:   # 8/26: z400 화각이 좁아 가운데↔끝점이 바뀌면 100px+ 튐 → 중단 대신 경고(사용자가 눈으로 확인)
                        rec("fr5", "dot_pick", {}, f"[{ck}] ⚠ 재검출 {dist:.0f}px 벗어남(기준 {chk}) — 눈으로 확인 후 하강")
                else:
                    rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 재검출 {len(seen)}개 {seen[:3]}" + ("" if chk else " (기준 없음 — '하강' 시 학습)"))
                    if not seen:
                        raise RuntimeError(f"[{ck}] 재검출 0개 — 이 자세에서 {ck} 점이 안 보임. 중단(팔은 z{float(pause_z):.0f})")
                rec("fr5", "dot_pick", {}, f"[{ck}] z{float(pause_z):.0f} 정지 — 확인 후 하강 신호 대기(최대 900s)")
                t0 = time.time()
                while not self._pick_go.is_set():
                    if self._stop.is_set(): raise RuntimeError("STOP 으로 취소됨")
                    if time.time() - t0 > 900: raise RuntimeError(f"[{ck}] 하강 신호 900s 타임아웃 — 중단(팔은 z{float(pause_z):.0f})")
                    time.sleep(0.1)
                if seen and not chk:      # 사용자가 확인하고 하강 → 이 픽셀을 기준으로 학습
                    ref["check_px"] = list(seen[0]) if near is None else list(near)
                    cal["refs"][ck] = ref; _j.dump(cal, open(CAL, "w"), indent=1, ensure_ascii=False)
                    rec("fr5", "dot_pick", {}, f"[{ck}] check_px 학습 {ref['check_px']}")
            sp0 = self.speed; self.speed = max(1, min(descend_speed, sp0))   # 하강 구간만 저속
            try:
                self._cart_ref = list(down); self._move_cart_line(down); time.sleep(0.3)
                # 8/27 사용자 요청: 파지 직전 정지 — 눈으로 보고 더 깊이 내릴지 결정. 콘솔 [▼ 더] 로 5mm 씩 하강, [▶ 잡기] 로 진행.
                #   grip_pause=True(기본은 body 로 켬). 추가 하강분은 pick_offset Z 에 영구 반영(다음부터 그 깊이).
                if body.get("grip_pause"):
                    self._pick_go.clear()
                    z_before = self._fk_tcp()[2]
                    rec("fr5", "dot_pick", {}, f"[{ck}] 파지 직전 정지 z{z_before:.1f} — [▼ 더]=5mm 하강 / [▶ 잡기]=파지 (최대 900s)")
                    t0 = time.time()
                    while not self._pick_go.is_set():
                        if self._stop.is_set(): raise RuntimeError("STOP 으로 취소됨")
                        if time.time() - t0 > 900: raise RuntimeError(f"[{ck}] 파지 신호 900s 타임아웃 — 중단")
                        time.sleep(0.1)
                    z_after = self._fk_tcp()[2]
                    dz = z_after - z_before
                    if abs(dz) > 0.3:
                        cal2 = _j.load(open(CAL)); r2 = cal2["refs"].setdefault(ck, {})
                        r2["pick_offset"][2] = round(r2["pick_offset"][2] + dz, 2)
                        _j.dump(cal2, open(CAL, "w"), indent=1, ensure_ascii=False)
                        rec("fr5", "dot_pick", {}, f"[{ck}] 추가 하강 {dz:+.1f}mm → pick_offset Z {r2['pick_offset'][2]} 영구 반영")
            finally:
                self.speed = sp0
            if body.get("no_grip"):
                self.last_pick = {"kind": ck, "yaw": dyaw, "no_grip": True, "tcp": [round(v, 1) for v in down]}
                rec("fr5", "dot_pick", {}, f"[{ck}] 하강 완료(파지 생략) tcp {[round(v,1) for v in down[:3]]}")
                return
            self._svc().move(grip_cmd)          # 비동기 → 완료 폴링
            self.gripsvc.wait_done()
            real = None
            try:
                res = str(self._svc()._call("GetGripperCurPosition()")).strip()
                v = res.split(",")
                if v[0] == "0" and len(v) >= 3:
                    real = int(v[2]); self.grip_pos = real
            except Exception:
                pass
            # 8/26 게이트 = 그리퍼 상태코드(PGE: 1=목표도달(빈손) 2=물체 잡고 정지 3=물체 놓침). 얇은 흰 벽은 실측 9 로도 물림(사용자 확인)
            st = None
            try:
                _f, st = self.gripsvc.motion_done()
            except Exception:
                pass
            rec("fr5", "dot_pick", {}, f"[{ck}] 그리퍼 명령 {grip_cmd} → 실측 {real}, 상태 {st} (2=물림 1=빈손 3=놓침)")
            if st == 1 or (st is None and real is not None and real <= grip_cmd):
                raise RuntimeError(f"[{ck}] 헛집음 — 그리퍼 상태 {st}, 실측 {real} (명령 {grip_cmd})")
            if st == 3:
                raise RuntimeError(f"[{ck}] 물체 놓침(상태 3) — 실측 {real}")
            self.last_pick = {"kind": ck, "yaw": dyaw, "grip_real": real, "tcp": [round(v, 1) for v in down]}
            if low_cand:
                # 8/27: 잘 잡힌 파지의 z380 화면·픽셀을 이 색의 기준으로 확정 → 다음부터 매번 이 픽셀로 보정
                import shutil, os as _os
                img = low_cand.get("img")
                if img and _os.path.exists(img):
                    dst = img.replace("_cand.jpg", "_ref.jpg"); shutil.copy(img, dst); low_cand["img"] = dst
                low_cand["made"] = time.strftime("%Y-%m-%d %H:%M")
                cal2 = _j.load(open(CAL)); cal2.setdefault("refs", {}).setdefault(ck, {})["low_ref"] = low_cand
                _j.dump(cal2, open(CAL, "w"), indent=1, ensure_ascii=False)
                rec("fr5", "dot_pick", {}, f"[{ck}] ★ z{low_cand['z']:.0f} 기준 확정 px {low_cand['px']} 화면 {low_cand.get('img')}")
        import threading
        def runner():
            # 8/26: 대기는 _start 바깥에서 — _start 가 fn 실행 전에 busy 를 검사하므로 안에서 기다리면 즉시 Busy
            t0 = time.time()
            while self.busy and time.time() - t0 < 180:
                time.sleep(0.1)
            if self.busy:
                rec("fr5", "dot_pick", {}, "ERR 정렬이 180s 안에 안 끝남"); return
            le = getattr(self, "last_err", None)
            if le and time.time() - le["t"] < 5:
                rec("fr5", "dot_pick", {}, f"ERR 정렬 실패로 중단: {le['msg']}"); return
            try:
                self._start(fn, "dot_pick")
            except Exception as e:
                rec("fr5", "dot_pick", {}, f"ERR {e}")
        threading.Thread(target=runner, daemon=True).start()
        return "dot_pick started (정렬 후 접근·하강·파지)"

    def dot_align(self, body=None):
        """도트 파지 보정 v3 (8/26): 벽 하나당 점 3개 — 가운데 = 파지점(XYZ 평행이동),
        양끝 = 벽의 yaw(양끝을 잇는 선 방향). observe 이동 → 관측 → ①yaw 보정(TCP rz 회전)
        → ②XY 보정(가운데 점, 최대 2회 반복수렴) → 잔차 보고. 하강·파지는 하지 않는다.

        dot_calib.json 키:
          kind            : 점 색 (center_kind/end_kinds 없으면 이 색 3개를 기하로 분류)
          center_kind     : 가운데 점 색 (선택)     end_kinds: 양끝 점 색 2개 (선택)
          target_obs      : 기준 가운데 점 (px,py,depth)   target_theta_deg: 기준 yaw(베이스 사상)
          yaw_sign        : ±1 (부호 실증 후 확정. 기본 +1)
        body.ref=1 → 지금 자세·지금 부품 위치를 기준으로 저장(티칭 갱신)."""
        import math
        import urllib.request
        import json as _j
        body = body or {}
        CAL = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CAL))
        self._ensure()
        self._no_manual()
        Minv = cal["Minv_mm_per_obs"]
        M_obs = cal["M_obs_per_mm"]
        ck = body.get("kind") or cal.get("center_kind") or cal["kind"]
        ek = cal.get("end_kinds")            # None 이면 같은 색 3개를 기하로 분류
        # 8/26 사진: 벽마다 "같은 색 3점"(흰=파랑/노랑, 검정=빨강/초록/노랑) → 색이 곧 부품.
        # 기준(target)은 색별로 다르다 → cal["refs"][kind]. (구버전 최상위 target_obs 는 cal["kind"] 전용)
        refs = cal.setdefault("refs", {})
        station = body.get("station") or "pick"          # pick: 절대각 / place: 기둥선 상대각
        rkey = f"{ck}@{station}" if station != "pick" else ck
        ref = refs.get(rkey) or ({"target_obs": cal.get("target_obs"),
                                  "target_theta_deg": cal.get("target_theta_deg"),
                                  "observe_joints": cal.get("observe_joints")}
                                 if (ck == cal.get("kind") and station == "pick") else {})
        # place 관측 자세는 body.observe_joints(콘솔이 현재 관절을 넘김) > ref > 전역
        # 8/26: pick 은 전역 관측 자세(cal.observe_joints) 단일 — 색별 refs 의 observe_joints 는 쓰지 않는다
        #   (z525→z550 변경 후 refs 에 남은 옛 관절로 이동해 기준을 저장하는 사고 발생)
        if station == "pick":
            obs_joints = body.get("observe_joints") or cal["observe_joints"]
        else:
            obs_joints = (body.get("observe_joints") or ref.get("observe_joints")
                          or cal.get("observe_place_joints") or cal["observe_joints"])
        roi = cal.get("place_roi") if station != "pick" else None    # 기둥 점은 밑판 ROI 안에서만

        def wrap90(a):
            while a > 90: a -= 180
            while a <= -90: a += 180
            return a

        def theta_base(p1, p2):
            """두 픽셀점을 잇는 선의 각도(°) — 캘리브 역행렬로 베이스 XY 로 사상해서 계산."""
            dx, dy = p2[0]-p1[0], p2[1]-p1[1]
            bx = Minv[0][0]*dx + Minv[0][1]*dy
            by = Minv[1][0]*dx + Minv[1][1]*dy
            return wrap90(math.degrees(math.atan2(by, bx)))

        # 기준선(8/26 사용자 안): 기둥-기둥 점을 이은 선 vs 벽 3점 선 → 같은 프레임에서 yaw 비교.
        #   line_kinds = ["red","green"](서로 다른 색 기둥 2개) 또는 ["blue"](같은 색 2점, 가장 큰 둘)
        # ★픽 랙엔 노랑/빨강 점 벽이 같이 있어 기둥선 오인 → 기준선은 place 스테이션에서만
        lk = (body.get("line_kinds") or ref.get("line_kinds")) if station != "pick" else None
        # 기둥 점과 벽 점이 같은 색일 수 있음(초록 벽 ↔ 파랑·초록 기둥, 8/26): 벽 점은 카메라에
        # 가까워 면적이 크다 → 벽으로 쓴 점을 제외(used)한 나머지 중 최대 면적을 기둥 점으로.

        def line_pts(dots, used):
            if not lk: return None
            pool = [t for t in dots if not any(t is u for u in used)]
            if roi:
                pool = [t for t in pool if roi[0] <= t["px"] <= roi[2] and roi[1] <= t["py"] <= roi[3]]
            pool.sort(key=lambda t: -t["area"])
            posts = cal.get("place_posts") if station != "pick" else None
            e = []
            for k in lk:
                if posts and k in posts:      # 관측자세 고정 → 예상 픽셀에 가장 가까운 같은 색 점(±120px)
                    ex, ey = posts[k]
                    near = [t for t in pool if t["kind"] == k and (t["px"]-ex)**2 + (t["py"]-ey)**2 < 120**2]
                    cand = min(near, key=lambda t: (t["px"]-ex)**2 + (t["py"]-ey)**2, default=None)
                else:
                    cand = next((t for t in pool if t["kind"] == k), None)
                if cand is None: return None
                pool = [t for t in pool if t is not cand]
                e.append(cand)
            if len(e) == 1:    # 같은 색 2점 = ["blue"] 표기
                cand = next((t for t in pool if t["kind"] == lk[0]), None)
                if cand is None: return None
                e.append(cand)
            return [[t["px"], t["py"]] for t in e]

        def pick_dots(d):
            """한 프레임에서 (center[px,py,depth] or None, ends[[px,py],[px,py]] or None, used[dots])."""
            dots = d["dots"]
            self._center_is_real = False
            if ek:
                c = [t for t in dots if t["kind"] == ck and t.get("depth_mm")]
                e = [next((t for t in dots if t["kind"] == k), None) for k in ek]
                if not c: return None
                ends = [[t["px"], t["py"]] for t in e] if all(e) else None
                self._center_is_real = True
                return [c[0]["px"], c[0]["py"], c[0]["depth_mm"]], ends, [c[0]] + [t for t in e if t]
            same = sorted([t for t in dots if t["kind"] == ck], key=lambda t: -t["area"])
            # 8/27: 같은 색 스티커가 다른 열(다른 부품)에도 있어 오탐(초록 x≈725, 진짜 열 x≈769) → 관측자세 x밴드로 원천 차단.
            #   cal["pick_xband"][색] = [xmin, xmax] (관측 자세에서만; place/기준저장엔 미적용)
            xb = (cal.get("pick_xband") or {}).get(ck) if station == "pick" else None
            if xb:
                # 8/28: Z 올려 재관측하면 화면이 중심으로 수축 → 밴드도 같은 배율(k)로 중심 기준 축소
                #   (안 하면 3점 실패→Z+60/120/180 전부 밴드 밖으로 실패하는 결함, 8/28 실증)
                _k = float(getattr(self, "_xband_k", 1.0) or 1.0)
                _lo = 640.0 + (float(xb[0]) - 640.0) / _k; _hi = 640.0 + (float(xb[1]) - 640.0) / _k
                same = [t for t in same if _lo <= t["px"] <= _hi]
            if station == "pick" and ref.get("two_dot_lower"):
                # 8/26 초록: 아래 끝점이 조명에 흐려 3점 불안정 → 안정적인 위+가운데 2점만 사용.
                #   아래쪽(py 큰)=가운데 파지점, 두 점 잇는 선=yaw. Z 안 올려 체계오차 없음.
                # 8/27: 점이 3개 보이면 '면적 큰 둘' 이 위+아래끝이 될 수 있다(→ 아래 끝점을 파지점으로 오인, 68mm 사고).
                #   기준 간격(end_gap_px = 위↔가운데)에 가장 가까운 짝을 고른다. 후보가 2개뿐이면 종전대로.
                from itertools import combinations as _cmb
                cand = same[:4]
                exp = ref.get("end_gap_px")
                if len(cand) >= 3 and exp:
                    def _gap(pr):
                        return ((pr[0]["px"]-pr[1]["px"])**2 + (pr[0]["py"]-pr[1]["py"])**2) ** 0.5
                    pair = min(_cmb(cand, 2), key=lambda pr: abs(_gap(pr) - float(exp)))
                    if abs(_gap(pair) - float(exp)) > 0.35 * float(exp):
                        rec("fr5", "dot_align", {}, f"[{ck}] 2점 모드: 기준 간격 {float(exp):.0f}px 에 맞는 짝 없음(최근접 {_gap(pair):.0f}px) — 3점 경로로")
                        pair = None
                else:
                    pair = tuple(cand[:2]) if len(cand) >= 2 else None
                two = sorted(pair, key=lambda t: t["py"]) if pair else []
                if len(two) == 2:
                    top, low = two
                    self._center_is_real = True
                    dep = low.get("depth_mm") or (cal.get("target_obs") or [0, 0, 287])[2]
                    ends = [[top["px"], top["py"]], [low["px"], low["py"]]]
                    return [low["px"], low["py"], dep], ends, two
            # place 에서 같은 색 기둥 점이 섞이면(초록) 벽 점은 큰 것부터 — 가운데가 가리면 2개만
            n_wall = 3 if station == "pick" or not (lk and ck in lk) else 2
            if station == "pick" and len(same) >= 3:
                # 8/26: "면적 큰 3개" 는 다른 물체의 같은 색 점이 섞이면 오염(초록 296px·파랑 580px 사고)
                #   → 후보(최대 6) 중 '일직선 + 등간격' 점수가 최고인 3점 조합을 택하고, 간격 300~480px 만 인정
                from itertools import combinations
                best, best_sc = None, None
                for tri in combinations(same[:6], 3):
                    pts3 = sorted([(t["px"], t["py"]) for t in tri], key=lambda q: q[1])
                    (x0, y0), (x1, y1), (x2, y2) = pts3
                    gap = ((x2-x0)**2 + (y2-y0)**2) ** 0.5
                    if not (250 <= gap <= 650): continue    # 벽 길이 색마다 다름: 초록≈295 · 빨강/노랑≈372 · 파랑(흰 긴벽)≈588px
                    # 중점 이탈(가운데 점이 양끝 중점에서 얼마나 먼가) + 직선 이탈(가운데 점의 선분 거리)
                    mx, my = (x0+x2)/2, (y0+y2)/2
                    mid_off = ((x1-mx)**2 + (y1-my)**2) ** 0.5
                    line_off = abs((x2-x0)*(y0-y1) - (x0-x1)*(y2-y0)) / gap
                    sc = mid_off + 2*line_off
                    if best_sc is None or sc < best_sc:
                        best, best_sc = tri, sc
                if best is not None and best_sc < 40:
                    same = list(best)
                else:
                    same = []      # 신뢰할 3점 조합 없음 → 아래 2점/None 경로
            same = same[:n_wall]
            if len(same) >= 3:
                pts = [[t["px"], t["py"]] for t in same]
                # 주축(가장 먼 두 점) 방향으로 정렬 → 바깥 둘 = 양끝, 가운데 = 파지점
                mx = sum(q[0] for q in pts)/3; my = sum(q[1] for q in pts)/3
                far = max(((i, j) for i in range(3) for j in range(i+1, 3)),
                          key=lambda ij: (pts[ij[0]][0]-pts[ij[1]][0])**2 + (pts[ij[0]][1]-pts[ij[1]][1])**2)
                ax = (pts[far[1]][0]-pts[far[0]][0], pts[far[1]][1]-pts[far[0]][1])
                order = sorted(range(3), key=lambda i: (pts[i][0]-mx)*ax[0] + (pts[i][1]-my)*ax[1])
                self._center_is_real = True     # 8/26: 진짜 3점(가운데 실검출)
                cd = same[order[1]]
                dep = cd.get("depth_mm")
                nom = (cal.get("target_obs") or [0, 0, 287])[2]
                if not dep:
                    ds = [t["depth_mm"] for t in (same[order[0]], same[order[2]]) if t.get("depth_mm")]
                    ds = [v for v in ds if abs(v - nom) <= 40]
                    dep = min(ds, key=lambda v: abs(v - nom)) if ds else None
                if not dep:
                    # 8/26: pick 은 Z 에 뎁스 안 씀(pick_use_depth False) → 뎁스 None 이어도 기준깊이로 통과
                    if not cal.get("pick_use_depth", False):
                        dep = nom
                    else:
                        return None
                e0, e2 = pts[order[0]], pts[order[2]]
                # 8/27: 기본은 "center"(가운데 점) — 기준 target_obs 가 가운데 점 픽셀로 저장돼 있어서,
                #   측정만 중점으로 바꾸면 스티커 편심(14,27px ≈ 4.5/8.6mm)이 그대로 이동으로 나온다(실측).
                #   중점 모드를 쓰려면 dot_align(ref) 로 기준부터 중점으로 다시 저장할 것.
                if station == "pick" and cal.get("pick_xy_mode", "center") == "mid":
                    # 8/27 사용자 지적: 파지점은 '양끝점의 중점' — 가운데 스티커는 검증용(일직선·등간격)으로만 쓴다.
                    #   (가운데 스티커 붙인 위치 오차가 그대로 파지 오차가 되던 것 제거)
                    return [(e0[0]+e2[0])/2.0, (e0[1]+e2[1])/2.0, dep], [e0, e2], same
                return [cd["px"], cd["py"], dep], [e0, e2], same
            if len(same) == 2:
                ends = [[same[0]["px"], same[0]["py"]], [same[1]["px"], same[1]["py"]]]
                if station == "pick":
                    # 8/26: pick 에서 2점만 보이면(끝점이 화면 경계·가림) 양끝 중점을 가운데로 대체 → XY 보정 유지
                    #   (벽 점은 대칭 배치라 중점 = 가운데 점). 단 두 점 간격이 벽 길이의 절반쯤(가운데+끝)이면 오판 → 간격 검사
                    gap = ((ends[0][0]-ends[1][0])**2 + (ends[0][1]-ends[1][1])**2) ** 0.5
                    exp = ref.get("end_gap_px")            # 기준 저장 때의 양끝 간격
                    if exp is None or abs(gap - exp) < exp * 0.25:
                        ds = [t["depth_mm"] for t in same if t.get("depth_mm")]
                        nom = (cal.get("target_obs") or [0, 0, 287])[2]
                        dep = min(ds, key=lambda v: abs(v - nom)) if ds else nom
                        cx, cy = (ends[0][0]+ends[1][0])/2.0, (ends[0][1]+ends[1][1])/2.0
                        self._center_is_real = False    # 2점 중점 대체 (가운데 점 미검출)
                        return [cx, cy, dep], ends, same
                return None, ends, same           # place: 가운데 점은 그리퍼에 가림 → 양끝 2점 = yaw 전용
            c = [t for t in same if t.get("depth_mm")]
            if not c: return None
            return [c[0]["px"], c[0]["py"], c[0]["depth_mm"]], None, c[:1]

        def mean_ang(ths):
            if len(ths) < 3: return None
            # 각도 평균은 ±90 경계 때문에 벡터 평균(2θ)
            sx = sum(math.cos(math.radians(2*t)) for t in ths)
            sy = sum(math.sin(math.radians(2*t)) for t in ths)
            return wrap90(math.degrees(math.atan2(sy, sx)) / 2)

        def read_obs(n=5, require3=False):
            """n 프레임 평균 → (center[3], theta_deg or None).
            require3=True 면 가운데 점 '실검출'(2점 중점 대체 불가)만 center 로 인정."""
            cs, ths, tls = [], [], []
            for _ in range(n * 4):
                d = _j.load(urllib.request.urlopen("http://localhost:8766/dots", timeout=5))
                got = pick_dots(d)
                if got:
                    c, ends, used = got
                    if c and (self._center_is_real or not require3): cs.append(c)
                    if ends:
                        ths.append(theta_base(ends[0], ends[1]))
                        self._last_end_gap = round(((ends[0][0]-ends[1][0])**2 + (ends[0][1]-ends[1][1])**2) ** 0.5, 1)
                    lp = line_pts(d["dots"], used)
                    if lp: tls.append(theta_base(lp[0], lp[1]))
                    if max(len(cs), len(ths)) >= n: break
                time.sleep(0.15)
            need = require3
            if (need and len(cs) < 3) or (not need and len(cs) < 3 and len(ths) < 3):
                raise RuntimeError(f"{ck} 점 검출 부족(center {len(cs)}/ends {len(ths)}"
                                   + (", 가운데 실검출 필요" if need else "") + ") — 화각/조명 확인")
            # 8/30 ★ 프레임 평균 금지 — 오검출이 '평균'으로 섞여 존재하지 않는 점을 만든다.
            #   파랑 사고: 프레임마다 가운데를 y358/y639 로 번갈아 골라 평균 486 → Y 39mm 엉뚱한 곳으로 이동.
            #   중앙값에서 12px 넘게 벗어난 프레임은 버리고, 남은 게 3개 미만이면 '불안정'으로 안전 중단.
            c = None
            if len(cs) >= 3:
                import statistics as _st
                _mx = _st.median([v[0] for v in cs]); _my = _st.median([v[1] for v in cs])
                keep = [v for v in cs if abs(v[0]-_mx) <= 12 and abs(v[1]-_my) <= 12]
                if len(keep) < 3:
                    _sp = max(max(abs(v[0]-_mx), abs(v[1]-_my)) for v in cs)
                    raise RuntimeError(f"{ck} 가운데 점이 프레임마다 다름(중앙값 대비 최대 {_sp:.0f}px, "
                                       f"일치 {len(keep)}/{len(cs)}) — 오검출 의심, 중단")
                if len(keep) < len(cs):
                    rec("fr5", "dot_align", {}, f"프레임 {len(cs)-len(keep)}/{len(cs)} 개 이상치 제외(중앙값 {_mx:.0f},{_my:.0f})")
                c = [sum(v[i] for v in keep)/len(keep) for i in range(3)]
                rec("fr5", "dot_align", {}, f"관측 가운데 ({c[0]:.1f},{c[1]:.1f}) depth {c[2]:.0f} · 양끝간격 {self._last_end_gap}px")
                # 8/30: 양끝 간격이 기준과 크게 다르면 '다른 짝'을 벽으로 오인한 것 → 이동 전에 중단.
                #   (Z 올려 관측하면 화면이 수축하므로 _xband_k 로 기대값을 축소)
                _eg = ref.get("end_gap_px") if station == "pick" else None
                if _eg and self._last_end_gap:
                    _exp = float(_eg) / float(getattr(self, "_xband_k", 1.0) or 1.0)
                    if abs(self._last_end_gap - _exp) > 0.25 * _exp:
                        raise RuntimeError(f"{ck} 양끝 간격 {self._last_end_gap}px 이 기준 {_exp:.0f}px 과 다름 "
                                           f"— 다른 점 짝 오인 의심, 중단")
            th, tl = mean_ang(ths), mean_ang(tls)
            if lk:
                if tl is None:
                    rec("fr5", "dot_align", {}, f"기준선 {lk} 미검출 → 절대각으로 폴백")
                elif th is not None:
                    th = wrap90(th - tl)     # 상대각: 벽선 − 기준선
                    rec("fr5", "dot_align", {}, f"기준선 {lk} {tl:.2f}° · 벽선−기준선 = {th:+.2f}°")
            return c, th

        def _focus(kinds):
            try:
                q = "clear=1" if not kinds else "kinds=" + ",".join(sorted(set(kinds)))
                urllib.request.urlopen(f"http://localhost:8766/focus?{q}", timeout=2).read()
            except Exception:
                pass

        def fn():
            _focus([ck] + list(lk or []))          # 8/26 이 작업에 필요한 색만 검출
            try:
                return _fn_body()
            finally:
                _focus(None)

        def _fn_body():
            nonlocal ref
            self._movej_rad([math.radians(v) for v in obs_joints])
            time.sleep(0.6)
            lift_back = 0.0; lifted = 0.0
            self._xband_k = 1.0                         # 8/28: 관측 높이 = 배율 1
            REQ3 = (station == "pick")    # 8/26: pick(기준저장 포함)은 가운데 점 실검출 필수 → 2점뿐이면 Z 올림
            try:
                obs, th = read_obs(require3=REQ3)
            except RuntimeError as e_first:
                if station != "pick":
                    raise
                # 8/26 사용자 안: 3점 안 보이면 Z 를 +STEP 씩(최대 MAXLIFT) 올려 화각 넓히며 재시도.
                #   뎁스로 깊이(nom+lift) 를 알므로 축척(Minv) 을 그 비율로, 기준 픽셀은 화면중심 기준 축소.
                #   성공 자세에서 접근하고 하강폭(off[2])은 그 자세 기준이라 원래 관측 높이 복귀 불필요.
                STEP = float(cal.get("pick_lift_step_mm", 60.0))
                MAXLIFT = float(cal.get("pick_lift_max_mm", 200.0))
                nom = (cal.get("target_obs") or [0, 0, 287])[2]
                lifted = 0.0; obs = None; th = None; last = e_first
                while lifted < MAXLIFT - 1:
                    LIFT = min(STEP, MAXLIFT - lifted)
                    cur0 = self._fk_tcp(); up = list(cur0); up[2] += LIFT
                    self._cart_ref = list(up); self._move_cart_line(up); time.sleep(0.6)
                    lifted += LIFT
                    self._xband_k = (nom + lifted) / nom     # 8/28: x밴드 축소 배율(read_obs 안에서 사용)
                    rec("fr5", "dot_align", {}, f"3점 실패 → Z+{lifted:.0f} 재관측 (x밴드 ×1/{self._xband_k:.2f})")
                    try:
                        obs, th = read_obs(require3=REQ3); break
                    except RuntimeError as e2:
                        last = e2; continue
                if obs is None:
                    raise RuntimeError(f"[{ck}] Z+{lifted:.0f} 까지 올려도 3점 미검출 — 벽이 화각(랙) 밖일 수 있음. 중단({last})")
                k = (nom + lifted) / nom                    # 픽셀/mm 는 깊이에 반비례 → Minv 를 k 배
                for r in range(2):
                    for c_ in range(2):
                        Minv[r][c_] *= k
                if ref.get("target_obs"):
                    t = ref["target_obs"]
                    ref = dict(ref); ref["target_obs"] = [640 + (t[0]-640)/k, 360 + (t[1]-360)/k, t[2]]
            if body.get("ref"):
                r_new = {"target_obs": [round(v, 1) for v in obs] if obs else None,
                         "end_gap_px": getattr(self, "_last_end_gap", None),
                         "ref_lift_mm": lifted,
                         "target_theta_deg": None if th is None else round(th, 2),
                         "target_rz": round(self._fk_tcp()[5], 2),
                         "observe_joints": [round(math.degrees(v), 3) for v in self.node.cur],
                         "line_kinds": lk,
                         "made": time.strftime("%Y-%m-%d %H:%M")}
                refs[rkey] = {**refs.get(rkey, {}), **r_new}     # 8/26: pick_offset·grip 파라미터 보존(통째 교체 사고)
                _j.dump(cal, open(CAL, "w"), indent=1, ensure_ascii=False)
                rec("fr5", "dot_align", {}, f"[{rkey}] 기준 저장: center={r_new['target_obs']} θ={r_new['target_theta_deg']}°")
                self.last_align = {"ref": True, "kind": rkey, **r_new}
                return
            if not ref.get("target_obs") and ref.get("target_theta_deg") is None:
                raise RuntimeError(f"[{rkey}] 기준 없음 — 부품을 똑바로 놓고 '기준 저장' 먼저")
            tgt = ref.get("target_obs")
            th_ref = ref.get("target_theta_deg")
            result = {"kind": rkey, "yaw_deg": None, "yaw_residual_deg": None,
                      "theta_deg": (None if th is None else round(th, 2))}   # 8/26 벽선 절대각(베이스) — pick 손목각 자동계산용
            # ── ① yaw 보정 (양끝 점 둘 다 보일 때만) ──
            if th is not None and th_ref is not None:
                dth = wrap90(th - th_ref) * float(cal.get("yaw_sign", 1))
                rec("fr5", "dot_align", {}, f"yaw 관측 {th:.2f}° 기준 {th_ref:.2f}° → Δ{dth:+.2f}°")
                if abs(dth) > 15:
                    raise RuntimeError(f"yaw 편차 {dth:.1f}° > 15° — 오검출/부품 이탈 의심, 중단")
                result["yaw_deg"] = round(dth, 2)      # 8/26 사용자 요구: yaw 는 항상 적용(생략돼도 값은 pick 손목각에 반영)
                if abs(dth) >= 0.05:
                    cur = self._fk_tcp()
                    cur[5] += dth
                    self._cart_ref = list(cur)
                    self._move_cart_line(cur)
                    time.sleep(0.6)
                    obs, th2 = read_obs()
                    res_th = None if th2 is None else wrap90(th2 - th_ref)
                    if th2 is not None: result["theta_deg"] = round(th2, 2)
                    result["yaw_deg"] = round(dth, 2)
                    result["yaw_residual_deg"] = None if res_th is None else round(res_th, 2)
                    rec("fr5", "dot_align", {}, f"yaw 회전 {dth:+.2f}° → 잔차 {res_th}°")
                    # 8/26: ±90° 경계(벽이 거의 수직)에서 각도 부호가 튀어 오탐 → 중단 대신 경고 (yaw 는 여러 색 실증 완료)
                    if res_th is not None and abs(res_th) > 3.0 and abs(res_th) > abs(dth):
                        rec("fr5", "dot_align", {}, f"⚠ yaw 잔차 {res_th:+.2f}°(초기 {dth:+.2f}°) 안 줄음 — 경계노이즈 가능, 계속")
            elif th_ref is not None:
                rec("fr5", "dot_align", {}, "양끝 점 미검출 → yaw 생략, 평행이동만")
            # ── ② XY(Z) 평행이동 보정 — 가운데 점, 최대 2회 반복 ──
            d_tot = [0.0, 0.0, 0.0]; res_px = None
            if obs is None or not tgt:
                rec("fr5", "dot_align", {}, "가운데 점 없음(가림/미저장) → yaw 만 보정하고 종료")
                self.last_align = result
                return
            for it in range(3):          # 8/26: 38mm 이동 시 2회로는 잔차 9.8px → 3회(7px 이하면 조기 종료)
                d_obs = [obs[i] - tgt[i] for i in range(3)]
                if station == "pick" and not cal.get("pick_use_depth", False):
                    d_obs[2] = 0.0     # 8/26: 랙 뎁스 불안정 → pick 은 Z 보정 생략(티칭 오프셋 신뢰)
                # ★부호(8/25 실증): 부품 +Δ ≡ 팔 -Δ → 팔 보정 = -Minv@Δobs
                if station == "pick":
                    # 8/26: pick 은 뎁스 미사용 → XY 2x2 역행렬만(3x3 Minv 는 뎁스축 결합으로 X 게인 폭주·발산)
                    M2 = [[M_obs[0][0], M_obs[0][1]], [M_obs[1][0], M_obs[1][1]]]
                    det = M2[0][0]*M2[1][1] - M2[0][1]*M2[1][0]
                    Mi2 = [[M2[1][1]/det, -M2[0][1]/det], [-M2[1][0]/det, M2[0][0]/det]]
                    d_mm = [-(Mi2[0][0]*d_obs[0] + Mi2[0][1]*d_obs[1]),
                            -(Mi2[1][0]*d_obs[0] + Mi2[1][1]*d_obs[1]), 0.0]
                else:
                    d_mm = [-sum(Minv[r][c] * d_obs[c] for c in range(3)) for r in range(3)]
                # 8/26 벽 길이 축척: Z 올림·거리 변화로 픽셀↔mm 가 변해도 양끝 간격(px)으로 실측 보정.
                #   factor = (이 벽이 관측 높이에서 가질 gap) / (지금 gap) = span_mm*calib_scale / cur_gap
                span_mm = ref.get("point_span_mm")
                cur_gap = getattr(self, "_last_end_gap", None)
                if station == "pick" and span_mm and cur_gap and lifted > 0:   # 8/26: Z 올렸을 때만 축척 재보정(관측 높이는 Minv 가 이미 정확)
                    gap_ref = span_mm * cal.get("calib_scale_px_per_mm", 3.125)
                    fac = gap_ref / cur_gap
                    d_mm = [v * fac for v in d_mm]
                    if it == 0:
                        rec("fr5", "dot_align", {}, f"축척보정 gap {cur_gap:.0f}px / 기준 {gap_ref:.0f}px → ×{fac:.3f}")
                if station == "pick" and not cal.get("pick_use_depth", False):
                    d_mm[2] = 0.0      # 8/26: 결합항으로 Z 가 17mm 밀린 사고 → pick 은 Z 보정 완전 0
                mag = max(abs(v) for v in d_mm)
                rec("fr5", "dot_align", {}, f"[{it+1}] Δobs={[round(v,1) for v in d_obs]} → Δmm={[round(v,1) for v in d_mm]}")
                if mag > 40:
                    raise RuntimeError(f"보정량 {mag:.0f}mm > 40mm — 오검출 의심, 중단")
                if mag < 0.5:
                    rec("fr5", "dot_align", {}, "보정 불필요(<0.5mm)")
                    res_px = ((obs[0]-tgt[0])**2 + (obs[1]-tgt[1])**2) ** 0.5
                    break
                cur = self._fk_tcp()
                for i in range(3): cur[i] += d_mm[i]
                self._cart_ref = list(cur)
                self._move_cart_line(cur)
                time.sleep(0.6)
                for i in range(3): d_tot[i] += d_mm[i]
                try:
                    obs, _ = read_obs()
                except Exception as e:
                    obs = None
                    rec("fr5", "dot_align", {}, f"[{it+1}] 재관측 실패({e}) — 이번 보정까지만 적용하고 종료")
                if obs is None:
                    # 8/27: 보정 이동 후 재관측이 안 되면(점이 화각 밖·순간 미검출) 여기서 종료.
                    #   종전에는 obs[0] 에서 'NoneType' object is not subscriptable 로 죽어 파지 전체가 중단됐다(3회 재발).
                    rec("fr5", "dot_align", {}, f"[{it+1}] 재관측 없음 — 적용한 보정 {[round(v,1) for v in d_tot]}mm 로 종료")
                    break
                res_px = ((obs[0]-tgt[0])**2 + (obs[1]-tgt[1])**2) ** 0.5
                if res_px < 7:            # ≈1mm
                    break
            # 8/30: 위 루프가 '재관측 없음/실패'로 끝나면 obs=None·res_px=None 인데
            #   여기서 round(obs[2]…) 하다 'NoneType __round__' 로 죽어 pick 전체가 중단됐다(파랑 실패).
            #   보정은 이미 적용됐으니 잔차 미상으로 정상 종료시킨다.
            result.update({"d_mm": [round(v, 2) for v in d_tot],
                           "residual_px": (None if res_px is None else round(res_px, 1)),
                           "residual_depth_mm": (None if obs is None else round(obs[2]-tgt[2], 1)),
                           "lift_mm": lifted if 'lifted' in dir() else lift_back})
            self.last_align = result
            _rp = "미상" if res_px is None else f"{res_px:.1f}px"
            _rd = "미상" if obs is None else f"{obs[2]-tgt[2]:+.1f}mm"
            rec("fr5", "dot_align", {}, f"보정 완료 잔차 {_rp} / {_rd} "
                                        f"yaw {result['yaw_deg']}° (잔차 {result['yaw_residual_deg']}°)")
        return self._start(fn, "dot_align")

    def place_success(self, body=None):
        """8/30 사용자 요청: 방금 삽입이 성공했으면, 이번 place 의 z 높이별
        '벽 점 ↔ 밑판 점' 직선거리·각도를 성공 기준으로 저장한다.
        (종전에는 8/28 수동 성공 1회를 찍은 기준뿐이라 오늘 성공해도 아무것도 안 남았다.)"""
        import json as _j, time as _t
        body = body or {}
        ck = body.get("kind")
        g = getattr(self, "_cp_geom", None)
        if not ck or not g:
            return "저장할 체크포인트 기하 없음(먼저 dot_place 실행)"
        CALP = "/home/ar/bf2_console/dot_calib.json"
        cal = _j.load(open(CALP))
        e = {"made": _t.strftime("%Y-%m-%d %H:%M"),
             "nudge_xy": body.get("nudge_xy"), "nudge_rz": body.get("nudge_rz"),
             "final_z": body.get("final_z"),
             "heights": [g[k] for k in sorted(g, reverse=True)]}
        cal["refs_place"][ck].setdefault("success_geom", []).append(e)
        cal["refs_place"][ck]["success_geom"] = cal["refs_place"][ck]["success_geom"][-10:]
        _j.dump(cal, open(CALP, "w"), ensure_ascii=False, indent=1)
        txt = " / ".join(f"z{h['z']:.0f}: " + ", ".join(f"{q['kind']} {q['dist_px']}px {q['ang_deg']}deg"
                                                       for q in h["pairs"]) for h in e["heights"])
        rec("fr5", "place_success", {}, f"[{ck}] 성공 기하 저장 — {txt}")
        return f"[{ck}] 성공 기하 {len(e['heights'])}개 높이 저장"

    def plugin(self, body=None):
        """9/1: 원시 SDK/플러그인 명령 1개 실행 — 진단·페이로드 등록용.
        조회(Get*/Is*)는 항상 허용, 그 외는 body.force=true 필요(오조작 방지).
        ⚠동작 중에는 거부(단일 클라이언트 원칙 + ServoJ 스트림 보호)."""
        body = body or {}
        cmd = str(body.get("cmd", "")).strip()
        if not cmd:
            raise RuntimeError("body.cmd 필요")
        readonly = cmd.startswith(("Get", "Is"))
        if not readonly and not body.get("force"):
            raise RuntimeError(f"쓰기 명령은 force=true 필요: {cmd}")
        if self.busy:
            raise Busy("fr5 동작 중 — 정지 후 재시도")
        self._ensure()
        return f"{cmd} = {str(self._svc()._call(cmd)).strip()}"

    def grip_read(self):
        """그리퍼 위치 1회 실측 — Recorder 캡처가 옛 명령값을 담던 문제(8/25).
        ⚠연속 폴링 금지(8/19 ServoJ 굶김) — 캡처 클릭 같은 단발 시점 전용.

        ★9/10: 여기서 읽은 값을 `_grip_real` 에도 넣는다. 사이클은 파지할 때마다 이 함수를 부르는데
        (`그리퍼 닫기 8 → 실측 15`) 그 신선한 값이 `gripper_real` 필드에 안 들어가서
        /cell/status 의 grip_real_age_s 가 47시간까지 낡아 있었다. 추가 통신 0 — 이미 읽은 값을 기록만 한다.
        (연속 폴링을 켜는 것이 아니므로 8/19 굶김과 무관하다.)"""
        self._ensure()
        res = str(self._svc()._call("GetGripperCurPosition()")).strip()   # '0,fault,pos'
        v = res.split(",")
        if v[0] == "0" and len(v) >= 3:
            self.grip_pos = int(v[2])
            self._grip_real = self.grip_pos
            self._grip_real_t = time.time()
        return self.grip_pos

    def vacuum(self, on):
        raise NotImplementedError("FR5 는 그리퍼만")

    def state(self):
        return {"connected": self.connected, "busy": self.busy,
                "stale": not self._have_joints,
                "joints": [round(x, 3) for x in self._joints_deg],
                "tcp": [round(v, 2) for v in getattr(self, "_tcp", [0.0]*6)],  # MoveIt FK (wrist3 플랜지 기준)
                "gripper": self.grip_pos,   # 실측 있으면 실측, 없으면 명령값
                "gripper_real": self._grip_real,
                "gripper_real_age": (round(time.time()-self._grip_real_t,1) if self._grip_real_t else None),
                "grip_watching": time.time() < self._grip_watch_until,
                "frozen": bool(getattr(self, "frozen", False)),
                "freeze_kind": getattr(self, "freeze_kind", None),
                "err": (lambda e: e if e and time.time() - e["t"] < 15 else None)(getattr(self, "last_err", None)),
                "manual": self.manual}


# ---------------- 인스턴스 ----------------
HOME = {"zk1": [0, 0, 0], "zk2": [0, 0, 0],
        "fr5": [0, -90, 90, -90, -90, 0]}   # mock 전용 — 실기 홈은 티칭 DB(poses.json)
if REAL:
    ROBOTS = {"zk1": ZKReal("zk1", os.environ.get("BF2_ZK1_PROFILE", "zkbot1")),
              "zk2": ZKReal("zk2", os.environ.get("BF2_ZK2_PROFILE", "zkbot2")),
              "fr5": FR5Real()}
else:
    ROBOTS = {k: MockRobot(k, v) for k, v in HOME.items()}

LOG = []
def rec(robot, action, body, result):
    LOG.append({"t": time.time(), "robot": robot, "action": action,
                "body": body, "result": result})
    print(f"[{robot}] {action} {body} -> {result}")

if REAL:
    def _poller():
        while True:
            for r in ROBOTS.values():
                try:
                    r.poll()
                except Exception:
                    pass
            time.sleep(1.0)
    threading.Thread(target=_poller, daemon=True).start()


# ---------------- 라우트 ----------------
@app.get("/homes")
def homes():
    """콘솔 고스트(미리보기)용 홈 자세(deg). 8/26: 콘솔에 fr5 홈이 하드코딩돼 실제(poses.json)와 달랐다."""
    import json as _j, math as _m
    out = {"zk1": [0.0, 0.0, 0.0], "zk2": [0.0, 0.0, 0.0], "fr5": None}
    try:
        db = _j.load(open("/home/ar/fr5_data/poses.json"))
        e = db["home"]; js = e["joints"] if isinstance(e, dict) else e
        out["fr5"] = [round(_m.degrees(float(v)), 3) for v in js]
    except Exception as ex:
        out["fr5_err"] = str(ex)
    return out


@app.get("/status")
def status():
    server = {"alive": False, "age": None, "state": ""}
    fr5 = ROBOTS.get("fr5")
    t = getattr(fr5, "_cell_t", 0.0)
    if t:
        age = time.time() - t
        server = {"alive": age < 3.0, "age": round(age, 1),
                  "state": getattr(fr5, "_cell_state", ""),
                  "managed": bool(CELL_PROC and CELL_PROC.poll() is None)}
    return {"mode": "real" if REAL else "mock", "server": server,
            "robots": {k: r.state() for k, r in ROBOTS.items()}}

# ---------------- 셀 실행기(cell_orchestrator) 관리 ----------------
CELL_PROC = None

@app.post("/cellctl/start")
async def cell_start(req: Request):
    """셀 실행기 기동. body: {mode: 'sim'|'real'} — real 은 ZK 노드 등 선행 필요."""
    global CELL_PROC
    body = await req.json()
    mode = body.get("mode", "sim")
    if mode not in ("sim", "real"):
        raise HTTPException(422, "mode must be sim|real")
    fr5 = ROBOTS.get("fr5")
    if getattr(fr5, "_cell_t", 0) and time.time() - fr5._cell_t < 3.0:
        raise HTTPException(409, "셀 실행기가 이미 떠 있음(하트비트 수신 중)")
    if CELL_PROC and CELL_PROC.poll() is None:
        raise HTTPException(409, "이미 브리지가 띄운 셀 실행기가 있음")
    CELL_PROC = subprocess.Popen(
        [sys.executable, "/home/ar/cell_orchestrator.py",
         "--ros-args", "-p", f"exec_mode:={mode}"],
        stdout=open("/tmp/cell_orchestrator.log", "a"),
        stderr=subprocess.STDOUT)
    rec("sys", "cell_start", body, f"pid={CELL_PROC.pid} mode={mode}")
    return {"result": f"started pid={CELL_PROC.pid} mode={mode}"}

@app.post("/cellctl/stop")
async def cell_stop():
    global CELL_PROC
    if not (CELL_PROC and CELL_PROC.poll() is None):
        fr5 = ROBOTS.get("fr5")
        if getattr(fr5, "_cell_t", 0) and time.time() - fr5._cell_t < 3.0:
            raise HTTPException(409, "떠 있는 셀 실행기는 브리지가 띄운 게 아님 — 해당 터미널에서 종료할 것")
        raise HTTPException(404, "브리지가 띄운 셀 실행기 없음")
    CELL_PROC.send_signal(signal.SIGINT)
    try:
        CELL_PROC.wait(timeout=6)
    except subprocess.TimeoutExpired:
        CELL_PROC.kill()
    rec("sys", "cell_stop", {}, "ok")
    CELL_PROC = None
    return {"result": "stopped"}


# 8/30 드래그 마찰보상 계수 J1~J6 (수동 모드에서만 켜짐 — 자동 전환 시 FrictionCompensationOnOff(0)).
#   전축 1.0 → J2 충돌알람(8/25), 손목 1.0/0.6/0.3 → 팔이 떠는 자려진동(8/30 실측).
#   0 이면 그 축은 보상 없음. J6 는 보상 없으면 손으로 안 돌아가므로(8/25) J6 만 켠다.
#   떨면 J6 를 0.25 로 내리고, 그래도 안 돌면 0.6 으로 올린다.
#   호출 시 body.friction_level=[6개] 로 그때만 다르게 줄 수 있고, body.friction=false 면 아예 끈다.
GRIP_MAX = 100       # ★9/4 사용자 지시: 그리퍼 최대 개도 100 으로 상향(8/30 의 50 가드 해제 요청)
J6_SWING_MAX = 180.0 # ★9/4 J6 대회전 봉인(지시서 B3/손목 스윙 봉인): 한 번에 J6 를 이보다 크게 돌리는 이동 거부.
#   J6 는 ±175° 라 ±180 을 못 넘는다 → 반대 자세로 갈 때 짧은 길(<40°)이 아니라 320° 로 돌아야 하고,
#   그 대회전이 컨트롤러 ServoJ 알람/앱 사망(controller_dead)을 낸다(9/4 실증: +161°↔−159° = 320°).
#   해법 = 중간에 J6=0 경유 자세. 강제 필요 시 body 에 allow_j6_swing:true.

FRICTION_LEVEL = [0, 0, 0, 0, 0, 0.4]

RESCUE_PROC = None

@app.post("/rescue/fr5")
async def rescue_fr5():
    """FR5 동결 원클릭 복구(8/25 확립) — fr5_rescue.sh 를 백그라운드 실행.
    동결 시에도 브리지는 살아있으므로 여기서 스택만 되살린다.
    ⚠라우트 순서: 반드시 범용 /{robot}/{action} 보다 앞에 선언(8/24 함정)."""
    global RESCUE_PROC
    if RESCUE_PROC and RESCUE_PROC.poll() is None:
        return {"result": "이미 복구 진행 중 — /tmp/fr5_rescue.log 확인"}
    RESCUE_PROC = subprocess.Popen(
        ["bash", "/home/ar/fr5_rescue.sh"],
        stdout=open("/tmp/fr5_rescue.log", "w"), stderr=subprocess.STDOUT)
    rec("fr5", "rescue", {}, f"fr5_rescue.sh 시작 pid={RESCUE_PROC.pid}")
    return {"result": "started",
            "note": "펜던트(192.168.58.2) 빨간 ⚠ Clear 는 사람이 먼저! 진행 ~2분, 로그 /tmp/fr5_rescue.log"}


@app.post("/{robot}/{action}")
async def command(robot: str, action: str, req: Request):
    if robot not in ROBOTS:
        raise HTTPException(404, "unknown robot")
    r = ROBOTS[robot]; body = await req.json(); dry = bool(body.get("dry_run", True))
    # 8/27 동결 가드: 동결 상태(joint_states 정지)면 이동류 명령 거부 — 죽은 컨트롤러에 명령 넣으면
    #   스테일 기준으로 엉뚱하게 움직이거나(8/25 cart_jog +103mm 사고) 악화. stop/복구/읽기만 허용.
    MOTION = {"jog", "move", "move_tcp", "cart_jog", "home", "lift", "dot_align", "dot_pick",
              "dot_pick_teach", "dot_grip_deeper", "dot_place", "dot_place_teach", "dot_place_scale", "dot_place_refs"}
    if action in MOTION and getattr(r, "frozen", False) and not dry:
        kind = getattr(r, "freeze_kind", "?")
        cure = ("전원 재투입(컨트롤러 앱 사망) — 콘솔 [🧊 동결 해제]" if kind == "controller_dead"
                else "스택 재기동/랜선 확인 — 콘솔 [🚑 복구]")
        raise HTTPException(409, f"🧊 FR5 동결({kind}) — 이동 거부. {cure} 후 재시도")
    # 서버측 안전 검사 — UI 검사와 별개로 항상 수행
    if action == "cart_jog" and robot != "fr5":
        raise HTTPException(422, "cart_jog 는 FR5 전용")
    if action == "move":
        need = 3 if robot.startswith("zk") else 6
        js = body.get("joints", [])
        if len(js) != need:
            raise HTTPException(422, f"{robot} joints must be length {need}")
        if robot.startswith("zk"):
            for i, (v, (lo, hi)) in enumerate(zip(js, ZK_LIMITS)):
                if not (lo <= float(v) <= hi):
                    raise HTTPException(422, f"A{i+1}={v} 소프트리밋({lo}~{hi}) 밖")
        if robot == "fr5" and not body.get("allow_j6_swing"):
            cj = getattr(r, "_joints_deg", None)
            if cj and len(cj) == 6 and any(cj):        # 현재관절 유효할 때만
                dj6 = abs(float(js[5]) - cj[5])
                if dj6 > J6_SWING_MAX:
                    raise HTTPException(422, f"J6 대회전 {dj6:.0f}° > {J6_SWING_MAX:.0f}° 봉인 — 손목이 ±180 못 넘어 크게 돌아 controller_dead 위험(9/4 실증). J6=0 경유 자세를 중간에 넣으세요(강제: allow_j6_swing:true)")
    if action == "speed" and not (1 <= body.get("value", 0) <= 100):
        raise HTTPException(422, "speed 1~100")
    if action == "lift":
        if not robot.startswith("zk"):
            raise HTTPException(422, "lift is ZK only")
        try:
            lift_mm = float(body.get("mm"))
        except (TypeError, ValueError):
            raise HTTPException(422, "lift mm required")
        if not (-50 <= lift_mm <= 50) or lift_mm == 0:
            raise HTTPException(422, "lift mm must be -50~50 and non-zero")
    # ★8/30 사용자 지시: 이 그리퍼는 최대치가 50 — 그 이상 절대 벌리지 말 것(교체 후 신품).
    if action == "gripper" and not (0 <= body.get("pos", -1) <= GRIP_MAX):
        raise HTTPException(422, f"gripper pos 0~{GRIP_MAX} (최대 개도 {GRIP_MAX} — 초과 금지)")
    if action in ("plugin", "grip_read", "grip_watch", "dot_align", "dot_pick_teach", "dot_pick", "dot_pick_continue", "dot_grip_deeper",
                  "dot_place_teach", "dot_place", "dot_place_continue", "dot_place_scale") and robot != "fr5":
        raise HTTPException(422, f"{action} 은 FR5 전용")
    # stop 과 "자동모드 복귀"(manual off)는 안전 복구 동작 — dry 여부와 무관하게 항상 실행
    # grip_read 는 읽기 전용이라 dry 에서도 실행(실기 연결돼 있으면 실측, 아니면 예외→캐시 유지)
    if dry and not (action == "stop" or action == "grip_read" or action == "grip_watch" or action == "dot_pick_continue" or action == "dot_place_continue" or action == "dot_grip_deeper"
                    or (action == "manual" and not body.get("on"))):
        rec(robot, action, body, "dry_run"); return {"result": "dry_run"}
    try:
        ret = None
        if action == "jog":        ret = r.jog(int(body["axis"]), float(body["delta"]))
        elif action == "move":     ret = r.move([float(x) for x in body["joints"]])
        elif action == "move_tcp": ret = r.move_tcp([float(x) for x in body["tcp"]])
        elif action == "cart_jog": ret = r.cart_jog(int(body["axis"]), float(body["delta"]))
        elif action == "home":     ret = r.go_home()
        elif action == "stop":     ret = r.stop()
        elif action == "speed":    ret = r.set_speed(int(body["value"]))
        elif action == "gripper":  ret = r.gripper(int(body["pos"]))
        elif action == "plugin":   ret = r.plugin(body)
        elif action == "grip_read": ret = r.grip_read()
        elif action == "grip_watch": ret = r.grip_watch(float(body.get("sec", 30.0)))
        elif action == "dot_align": ret = r.dot_align(body)
        elif action == "dot_pick_teach": ret = r.dot_pick_teach(body)
        elif action == "dot_pick": ret = r.dot_pick(body)
        elif action == "dot_pick_continue": r._pick_go.set(); ret = "하강 신호 전송"
        elif action == "dot_grip_deeper":
            dz = float(body.get("mm", 5.0))            # 파지 직전 정지 중 수직 하강(기본 5mm)
            t = r._fk_tcp(); t[2] -= abs(dz); r.speed = max(1, min(2, r.speed))
            r._cart_ref = list(t); r._move_cart_line(t); ret = f"{abs(dz):.0f}mm 더 하강 z{t[2]:.1f}"
        elif action == "dot_place_teach": ret = r.dot_place_teach(body)
        elif action == "dot_place": ret = r.dot_place(body)
        elif action == "dot_place_continue": r._place_go.set(); ret = "삽입 신호 전송"
        elif action == "dot_place_scale": ret = r.dot_place_scale(body)
        elif action == "dot_place_refs": ret = r.dot_place_refs(body)
        elif action == "place_success": ret = r.place_success(body)
        elif action == "vacuum":   ret = r.vacuum(bool(body["on"]))
        elif action == "lift":     ret = r.lift(float(body["mm"]))
        elif action == "manual":   ret = r.set_manual(bool(body["on"]), body)
        else: raise HTTPException(404, "unknown action")
    except Busy as e:
        rec(robot, action, body, f"BUSY {e}"); raise HTTPException(409, str(e))
    except NotImplementedError as e:
        rec(robot, action, body, f"NOT_IMPLEMENTED {e}"); raise HTTPException(501, str(e))
    except HTTPException:
        raise
    except Exception as e:
        rec(robot, action, body, f"ERR {e}"); raise HTTPException(500, str(e))
    rec(robot, action, body, ret or "ok")
    return {"result": ret or "ok"}

@app.get("/log")
def get_log():
    return LOG[-500:]

if __name__ == "__main__":
    print("BF2 bridge", "REAL" if REAL else "MOCK", "→ http://0.0.0.0:8765")
    if REAL:
        print("  ZK 프로파일:", {k: r.profile_name for k, r in ROBOTS.items()
                                 if isinstance(r, ZKReal)})
        print("  ★배치 이설 이력 주의 — 어느 개체가 어느 셀인지 눈으로 확인할 것")
        print("  ★브리지 사용 중 zk_*.py CLI 직접 실행 금지 (같은 시리얼)")
    uvicorn.run(app, host="0.0.0.0", port=8765)
