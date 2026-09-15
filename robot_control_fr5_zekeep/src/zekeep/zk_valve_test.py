#!/usr/bin/env python3
"""흡착 중 밸브 ON 가설 시험 — A → B → A 비교판

매뉴얼상 Y15 가 '常开电磁阀'(상시개방형). 통전 안 할 때 실제로 열려 있다면
흡착 중 그 구멍으로 공기가 새어 진공이 안 잡힌다.
밸브 통전은 교재 권고대로 매회 10초 미만.
"""
import sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK, PUMP, VALVE, names, YN
import zk_profiles as ZPROF

zk = ZK(ZPROF.resolve_from_argv().port)
if not zk.link():
    sys.exit("링크 실패")

def phase(tag, valve, sec, msg):
    print("\n" + "=" * 58, flush=True)
    print(f"[{tag}] {msg}  — {sec}초", flush=True)
    print("=" * 58, flush=True)
    zk.y_on(PUMP)
    if valve:
        time.sleep(0.2); zk.y_on(VALVE)
    else:
        zk.y_off(VALVE)
    time.sleep(0.5)
    print(f"    출력: {names(zk.y() or set(), YN)}", flush=True)
    time.sleep(sec - 0.5)
    zk.y_off(VALVE); time.sleep(0.2); zk.y_off(PUMP)
    print(f"[{tag}] 끝", flush=True)

try:
    zk.y_off(PUMP); zk.y_off(VALVE); time.sleep(0.3)
    print("15초 뒤 시작합니다 — 파레트를 흡착판 밑에 대고 준비하세요", flush=True)
    time.sleep(15)

    phase("A1", False, 12, "펌프만 ON        ← 지금까지 해온 방식")
    time.sleep(4)
    phase("B",  True,   9, "펌프 + 밸브 ON   ← 가설")
    time.sleep(4)
    phase("A2", False, 10, "다시 펌프만 ON   ← A 와 B 를 되짚어 비교")
finally:
    zk.y_off(VALVE); time.sleep(0.2); zk.y_off(PUMP)
    zk.y_off(VALVE); zk.y_off(PUMP)
    print(f"\n정리 완료 — 출력 {names(zk.y() or set(), YN) or '✅ 전부 OFF'}", flush=True)
    print(zk.report(), flush=True)
    zk.close()
