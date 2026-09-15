#!/usr/bin/env python3
"""
zk_playback.py - 출하 래더 자동재생으로 포즈 시퀀스 실행 (2026-08-12 해독·실증)

    python3 zk_playback.py --robot zkbot1 --poses above,pick,place,above --dry
    python3 zk_playback.py --poses above,pick,place,above --pump 2:on,3:off   # 실전(펌프)
    python3 zk_playback.py --restore     # 원본 데모 테이블 복원

★해독 결과 (교재와 다른 출하 래더의 실체):
  · 모드 = D90 (0정지/1수동/2자동) → M200/201/202 코일이 따라옴. D0/D2 아님!
  · 기동 = 자동 모드에서 컨트롤박스 물리버튼(X3, 짧게=단순환/길게=연순환)
    또는 ★프로그램: M301+M21 강제 (수동 모드에선 완전 무시됨)
  · ★DI '有等待' 모드면 원점복귀 후 X6 센서를 영원히 기다림(M175 점등).
    이 실물은 X6 미장착 → HMI [I/O]→[DI-Setting] 을 "No wait" 로 (8/12 전환 완료)
  · ★D50(float 총속도%)가 자동 속도에 곱해짐 — 조그 스크립트가 저속으로 남겨두면
    자동재생이 기어간다. 재생 전 100.0 설정 필수.
  · 사이클 시작마다 강제 원점복귀 (원점에 이미 있으면 수 초 내 완료)
  · ★실행 패턴(하드코딩): 포즈마다 "팔 접기(원점높이) → A1 회전 → 하강" 2페이즈.
    '먼저 들고 돌기'가 래더 차원에서 보장됨. 단 경유점 체인(직선보간)은 불가 —
    직선 하강은 zk_zline.py(PC측 IK)가 담당.
  · ★주입 레시피: 포즈 m(1..K) → A1열 idx(2m-1)=idx(2m)=a1_m /
    A2·A3열 idx(m)=a23_m / 속도·지연·펌프 idx 는 A1 과 동일 규칙 / D70=2K.
    idx0 은 원점 취급이라 미사용. (실증: 4포즈 24.7초, 최종오차 3축 0.000°)
  · 펌프: D150+2n (1=ON) — 하강 페이즈(짝수 idx=2m)에 걸어야 자세 도착 후 절환.
  · 완료 감지 = M21·M301 자동 소등. 페이즈 표시 D40.

⚠️ 사용 후 티칭 테이블에 우리 값이 남는다 — HMI 데모가 필요하면 --restore.
   원본 백업: ~/zkbot_data/teach_raw_zkbot1_0812_pretest.json
"""
import json
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402

PPD = 177.78
BACKUP = "/home/ar/zkbot_data/teach_raw_zkbot1_0812_pretest.json"


def w32(zk, a, v):
    v &= 0xFFFFFFFF
    ok = zk.set_d(a, v & 0xFFFF) and zk.set_d(a + 1, (v >> 16) & 0xFFFF)
    if not ok:
        raise RuntimeError(f"D{a} 쓰기 실패")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    prof = P.apply_profile(ZPROF.resolve_from_argv())
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")
    try:
        if "--restore" in sys.argv:
            raw = json.load(open(BACKUP))
            bad = sum(0 if zk.set_d(int(k), v) else 1
                      for k, v in raw.items() if not k.startswith("D"))
            zk.set_d(70, raw["D70"])
            print(f"원본 테이블 복원 {'완료' if bad == 0 else f'실패 {bad}워드'} (D70={zk.d(70)})")
            return

        names = (arg("--poses") or "above,pick,place,above").split(",")
        db = json.load(open(prof.pose_file))
        alias = {"above": "base_above", "pick": "base_pick",
                 "lift": "base_lift", "place": "base_place"}
        seq = []
        for n in names:
            key = alias.get(n.strip(), n.strip())
            if key not in db:
                sys.exit(f"■ 자세 '{key}' 없음")
            p = db[key]
            seq.append((p["A1"], p["A2"], p["A3"]))
        pump = {}                      # 포즈번호(1기준) → 1/0
        if arg("--pump"):
            for part in arg("--pump").split(","):
                k, v = part.split(":")
                pump[int(k)] = 1 if v.strip() in ("on", "1") else 0
        if "--dry" in sys.argv:
            pump = {}
        speed = int(arg("--speed", "60"))
        dwell = int(arg("--dwell", "3"))        # 100ms 단위

        K = len(seq)
        for n in range(2 * K + 2):
            m = (n + 1) // 2
            a1 = seq[m - 1][0] if 1 <= n <= 2 * K else 0
            w32(zk, 100 + 2 * n, round(a1 * PPD))
            a23 = seq[n - 1] if 1 <= n <= K else (0, 0, 0)
            w32(zk, 200 + 2 * n, round(a23[1] * PPD))
            w32(zk, 300 + 2 * n, round(a23[2] * PPD))
            zk.set_d(400 + 2 * n, speed)
            zk.set_d(450 + 2 * n, dwell)
            # 펌프는 하강 페이즈(idx=2m)에서 절환, 이후 유지
            state = 0
            for pm in range(1, m + 1):
                if pm in pump and (n >= 2 * pm):
                    state = pump[pm]
            zk.set_d(150 + 2 * n, state)
        zk.set_d(70, 2 * K)
        print(f"주입: 포즈 {K}개(페이즈 {2*K}) 속도 {speed}% 펌프 {pump or '없음'}", flush=True)

        P.set_speed(zk, 100.0)                  # ★총속도 — 저속 잔류 방지
        zk.set_d(90, 2)
        time.sleep(0.8)
        if 202 not in (zk.m(32) or set()):
            sys.exit("■ 자동 모드 진입 실패 (M202 소등)")
        zk.m_on(301)
        zk.m_on(21)
        t0 = time.time()
        print("[t=0.0] 재생 시작", flush=True)
        last = None
        moved = False
        still = 0
        while time.time() - t0 < 600:
            a = P.angles(zk)
            cur = (round(a["A1"], 1), round(a["A2"], 1), round(a["A3"], 1))
            if cur != last:
                print(f"[t={time.time()-t0:6.1f}] A1={cur[0]:8.1f} A2={cur[1]:7.1f} "
                      f"A3={cur[2]:7.1f}  페이즈{zk.d(40)}", flush=True)
                if last is not None:
                    moved = True
                    still = 0
                last = cur
            elif moved:
                still += 1
                if still > 40:
                    print("  ⚠ 40틱 정지 — 감시 종료(사이클 상태 확인 요망)", flush=True)
                    break
            ms = zk.m(64) or set()
            if moved and 301 not in ms and 21 not in ms and time.time() - t0 > 5:
                print(f"[t={time.time()-t0:6.1f}] 사이클 종료", flush=True)
                break
            time.sleep(0.25)
        a = P.angles(zk)
        tgt = seq[-1]
        errs = [a["A1"] - tgt[0], a["A2"] - tgt[1], a["A3"] - tgt[2]]
        print(f"최종 {P.fmt(a)}  마지막 포즈 대비 "
              + "  ".join(f"{j} {e:+.3f}°" for j, e in zip(("A1", "A2", "A3"), errs)),
              flush=True)
    finally:
        zk.m_off(301)
        zk.m_off(21)
        zk.set_d(90, 1)
        zk.close()


if __name__ == "__main__":
    main()
