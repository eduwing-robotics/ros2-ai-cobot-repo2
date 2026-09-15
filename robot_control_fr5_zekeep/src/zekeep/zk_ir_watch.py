#!/usr/bin/env python3
"""zk_ir_watch.py - 적외선 센서(X6) 장착 여부/동작 확인 도구.

    python3 ~/zk_ir_watch.py zkbot1        # Ctrl+C 로 종료

돌려놓고 로봇 주변(베이스 부근 센서 위치)에 손을 갖다 대 보세요.
값이 off <-> ON 으로 토글되면 센서가 장착·배선된 것.
아무리 가려도 변화가 없으면 미장착이거나 X6 배선이 비어 있는 것.
(NPN 로우액티브 — 감지 시 센서 자체 LED 도 켜집니다. 배선도 6p)
"""
import sys
import time

sys.path.insert(0, "/home/ar")
import zkfx                      # noqa: E402
import zk_profiles as ZPROF      # noqa: E402

prof = ZPROF.resolve_from_argv(announce=True)
zk = zkfx.ZK(prof.port)
last = None
print("X6 감시 중 — 센서 앞을 손으로 가려보세요 (Ctrl+C 종료)")
try:
    while True:
        on = zk.x()
        cur = None if on is None else (6 in on)
        if cur != last:
            t = time.strftime("%H:%M:%S")
            print(f"[{t}] 적외선(X6): {'★ON (감지)' if cur else ('off' if cur is not None else '읽기실패')}")
            last = cur
        time.sleep(0.15)
except KeyboardInterrupt:
    pass
finally:
    zk.close()
