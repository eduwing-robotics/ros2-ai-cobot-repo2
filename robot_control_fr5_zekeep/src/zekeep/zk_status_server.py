#!/usr/bin/env python3
"""
zk_status_server.py - 로봇 상태를 서버로 내보낸다

출력 방식을 골라 쓴다 (동시 지정 가능)
    --stdout          한 줄 JSON 으로 표준출력 (파이프로 아무 데나 연결)
    --file  PATH      최신 상태를 JSON 파일로 계속 덮어씀 (대시보드가 읽기 편함)
    --post  URL       HTTP POST 로 전송
    --udp   HOST:PORT UDP 로 전송 (가장 가볍다)

    --hz N            전송 주기 (기본 2.0)
    --robot NAME      로봇 이름 (여러 대일 때 구분)
    --port DEV        시리얼 포트 (기본 /dev/ttyUSB0)

★ 대역폭 주의
  상태 읽기와 제어 명령이 **같은 시리얼 링크를 공유**한다.
  3축 각도 한 바퀴에 약 150ms(6.6Hz)가 걸리므로, 상태를 6.6Hz로 뽑으면
  제어에 쓸 여유가 없다. 실용적으로는 **2~3Hz 로 상태를 보내고 나머지를
  제어에 남기는** 것이 맞다. 모니터링 용도로는 2Hz면 충분하다.

예)
    python3 zk_status_server.py --udp 192.168.0.10:9000 --hz 2 --robot zkbot1
    python3 zk_status_server.py --file /tmp/zkbot1.json --stdout
"""
import argparse
import json
import socket
import struct
import sys
import time
import urllib.request

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D, PUMP, VALVE                   # noqa: E402
import zk_profiles as ZPROF                             # noqa: E402
from zk_safety import JOINT_D, HOME_FLAG, JOINT_LIMITS  # noqa: E402

XN = {0: "a1_limit", 1: "a2_limit", 2: "a3_limit",
      3: "start_btn", 4: "origin_btn", 5: "estop", 6: "infrared"}
MODE = {0: "STOP", 1: "MANUAL", 2: "AUTO"}


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else round(struct.unpack("<f", b)[0], 3)


def snapshot(zk, robot):
    xs = zk.x()
    ys = zk.y()
    ms = zk.m(32)
    joints = {j: ang(zk, d) for j, d in JOINT_D.items()}

    ok = xs is not None and ms is not None
    homed = ({j: (b in ms) for j, b in HOME_FLAG.items()} if ms else None)
    in_range = {}
    for j, v in joints.items():
        lo, hi = JOINT_LIMITS[j]
        in_range[j] = None if v is None else (lo <= v <= hi)

    # ★ 통계는 마지막 읽기까지 끝난 뒤에 계산해야 한다.
    #   dict 리터럴 안에서 zk.d(...) 를 더 호출하면 분자만 늘어 100%를 넘는다.
    mode = MODE.get(zk.d(90), "?")
    step, total, creep = zk.d(40), zk.d(70), zk.d(76)
    st = dict(zk.stats)
    tot = st["ok"] + st["csum"] + st["noresp"]
    return {
        "robot": robot,
        "ts": time.time(),
        "online": ok,
        "joints_deg": joints,
        "joints_in_range": in_range,
        "homed": homed,
        "ready": bool(homed and all(homed.values()) and xs is not None and 5 not in xs),
        "inputs": ({XN[i]: (i in xs) for i in XN} if xs is not None else None),
        "estop": (5 in xs) if xs is not None else None,
        "pump": (PUMP in ys) if ys is not None else None,
        "valve": (VALVE in ys) if ys is not None else None,
        "mode": mode,
        "step": step,
        "total_steps": total,
        "creep_speed": creep,
        "link_quality_pct": (st["ok"] * 100 // tot) if tot else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", default=ZPROF.DEFAULT_ROBOT)
    ap.add_argument("--port", default=None)
    ap.add_argument("--robot", default="zkbot1")
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--post")
    ap.add_argument("--udp")
    a = ap.parse_args()
    if not (a.stdout or a.file or a.post or a.udp):
        a.stdout = True

    sock = None
    dest = None
    if a.udp:
        host, p = a.udp.rsplit(":", 1)
        dest = (host, int(p))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    zk = ZK(ZPROF.resolve(robot=a.robot, port=a.port).port)
    if not zk.link():
        sys.exit("링크 실패")
    print(f"# {a.robot} 상태 송출 시작 ({a.hz}Hz)", file=sys.stderr, flush=True)

    period = 1.0 / a.hz
    try:
        while True:
            t0 = time.time()
            try:
                s = snapshot(zk, a.robot)
            except Exception as e:
                s = {"robot": a.robot, "ts": time.time(),
                     "online": False, "error": str(e)}
            line = json.dumps(s, ensure_ascii=False)

            if a.stdout:
                print(line, flush=True)
            if a.file:
                tmp = a.file + ".tmp"
                with open(tmp, "w") as f:
                    f.write(line)
                import os
                os.replace(tmp, a.file)        # 원자적 교체 — 읽는 쪽이 깨진 파일을 안 본다
            if sock:
                sock.sendto(line.encode(), dest)
            if a.post:
                try:
                    req = urllib.request.Request(
                        a.post, data=line.encode(),
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=1.0)
                except Exception as e:
                    print(f"# POST 실패: {e}", file=sys.stderr, flush=True)

            time.sleep(max(0, period - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\n# 종료", file=sys.stderr)
    finally:
        zk.close()


if __name__ == "__main__":
    main()
