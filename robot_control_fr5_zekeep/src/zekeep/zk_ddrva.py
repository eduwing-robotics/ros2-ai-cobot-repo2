#!/usr/bin/env python3
"""
zk_ddrva.py - PLC 위치결정(DDRVA) 경로 검증  [2순위 과제, 2026-08-21 미검증]

★ 왜: 조그 방식은 통신지연 150ms 가 0.5° 오버슛을 만든다(구조적, 8/10 규명).
  DDRVA 는 PLC 가 직접 위치결정하므로 지연 무관, 펄스 단위(0.0056°) 정밀.
  8/10 해독한 경로: DDRVA D100 D2024 Y000 Y004 (A1) / M21 기동 / M155~157 완료.

🔴 위험 (그래서 이 스크립트가 조심스러운 것):
  M21 이 출하 래더에서 "단순환" = 티칭 테이블(D70 스텝) 전체 재생일 수 있다.
  현재 D70=11 (출하 데모). 검증 없이 M21 을 켜면 11스텝이 전부 돈다.

🔴🔴 2026-08-21 실기 검증 결과 — **M301+M21 경로는 봉인한다**
  1차: M21 단독(D90=0, D50=0)          → 완전 무동작 (안전)
  2차: D90=2 + D50=20% + M301+M21      → 🔴 3축이 동시에 같은 속도로 음(-)방향 연속 구동.
       A1 목표(D100=-180.67°)를 무시하고 반대 방향으로 감. A2/A3 도 구동(목표 레지스터를
       쓰지도 않았는데). 가드가 3° 초과에서 킬 — 관성+통신지연으로 -4.6° 에서 정지,
       A1 이 소프트리밋 -190° 에서 2.7° 앞까지 갔다.
  ⇒ 이 조합은 위치결정이 아니라 정체불명의 연속 구동이다. **가드 없이 켜면 리밋까지 민다.**
  ⇒ DDRVA 실사용은 래더(.gxw) 원문에서 실제 트리거 조건을 확인하기 전에는 진행 금지.

절차 (반드시 이 순서, 사람이 지켜보며):
  1) dump    : 관련 레지스터 읽기만. 아무 때나 안전
  2) prep    : 티칭 스텝0 목표 = "현재 위치 + A1 만 +2°" 로 만들고 D70=1 로 줄임
               → M21 이 재생이더라도 "한 스텝, 2° 회전"이 전부가 되게 만든다
  3) fire    : M21 펄스. 감시 루프가 붙는다 —
               · 어느 축이든 3° 이상 이탈 → 즉시 M21 OFF + 정지
               · X5 → 즉시 중단 / 5초 타임아웃
  4) restore : D70·스텝0 원복 (prep 이 백업해 둔 값으로)

  fire 는 --i-am-watching 플래그 없이는 절대 안 돈다.
  성공 판정: A1 이 정확히 +2.0° 이동 + M155 완료 비트 + 다른 축 부동.
"""
import json, struct, sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D
from zk_safety import HOME_FLAG, JOINT_D
import zk_pose as P
import zk_profiles as ZPROF

PPD = 177.78                       # 펄스/도
BAK = "/home/ar/zkbot_data/ddrva_backup.json"
M21, M120 = 21, 120
DONE_BITS = {"A1": 155, "A2": 156, "A3": 157}


def arg(n, d=None):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d


def d32(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<i", b)[0]


def w32(zk, d, v):
    return zk.write(A_D + d * 2, int(v).to_bytes(4, "little", signed=True))


def d16(zk, d):
    b = zk.read(A_D + d * 2, 2)
    return None if b is None else struct.unpack("<h", b)[0]


def w16(zk, d, v):
    return zk.write(A_D + d * 2, int(v).to_bytes(2, "little", signed=True))


def cmd_dump(zk):
    print("  ── 티칭/위치결정 레지스터 ──")
    for lab, d, w in (("D30 현재스텝", 30, 16), ("D70 총스텝", 70, 16),
                      ("D90 모드", 90, 16), ("D50 속도%", 50, 16),
                      ("D52 기준Hz", 52, 16), ("D2002 조그Hz", 2002, 16),
                      ("D2024 위치결정Hz?", 2024, 16),
                      ("D100 스텝0 A1펄스", 100, 32)):
        v = d16(zk, d) if w == 16 else d32(zk, d)
        deg = f"  (={v / PPD:+.2f}°)" if d == 100 and v is not None else ""
        print(f"   {lab:20} = {v}{deg}")
    ms = zk.m(200) or set()
    print(f"   M21={'ON' if M21 in ms else 'off'}  "
          f"완료 M155/156/157: " + "/".join("ON" if b in ms else "-" for b in DONE_BITS.values()))


def cmd_prep(zk):
    a = P.angles(zk)
    cur_p = {j: int(round(a[j] * PPD)) for j in ("A1", "A2", "A3")}
    bak = {"D70": d16(zk, 70), "D100": d32(zk, 100),
           "D400": d16(zk, 400), "D450": d16(zk, 450), "D150": d16(zk, 150),
           "ts": time.strftime("%Y-%m-%d %H:%M")}
    json.dump(bak, open(BAK, "w"))
    print(f"  백업 → {BAK}  {bak}")
    tgt = cur_p["A1"] + int(round(2.0 * PPD))          # A1 +2° 만
    ok = (w32(zk, 100, tgt) and w16(zk, 400, 5)        # 속도 5%
          and w16(zk, 450, 0) and w16(zk, 150, 0)      # 지연 0, 펌프 0
          and w16(zk, 70, 1))                          # ★총 1스텝
    print(f"  스텝0: A1 {a['A1']:+.2f}° → {tgt / PPD:+.2f}° (+2.0°), 속도5%, D70=1  "
          f"{'✅' if ok else '🔴 쓰기 실패'}")
    print("  ⚠ A2/A3 스텝 레지스터 주소는 미확정(8/21) — A1 단독 검증만 한다.")
    print("  다음: 사람이 지켜보며  python3 zk_ddrva.py fire --robot ... --i-am-watching")


def cmd_fire(zk):
    if "--i-am-watching" not in sys.argv:
        sys.exit("■ 거부 — 이 시험은 사람이 로봇을 직접 보고 있어야 합니다.\n"
                 "  비상정지에 손 올리고:  fire --i-am-watching")
    if d16(zk, 70) != 1:
        sys.exit("■ 거부 — D70 이 1 이 아님. prep 을 먼저 (재생 폭주 방지)")
    a0 = P.angles(zk)
    print(f"  시작 {P.fmt(a0)}")
    print("  M21 ON …", flush=True)
    zk.m_on(M21)
    t0 = time.time()
    verdict = "타임아웃(5s)"
    try:
        while time.time() - t0 < 5.0:
            if 5 in (zk.x() or set()):
                verdict = "X5 급정지"; break
            a = P.angles(zk)
            if a and all(v is not None for v in a.values()):
                d1 = a["A1"] - a0["A1"]
                if abs(a["A2"] - a0["A2"]) > 3 or abs(a["A3"] - a0["A3"]) > 3:
                    verdict = f"🔴 다른 축이 움직임 (A2 {a['A2']-a0['A2']:+.1f} A3 {a['A3']-a0['A3']:+.1f}) — 재생 의심"
                    break
                if abs(d1) > 3.0:
                    verdict = f"🔴 A1 과주행 {d1:+.1f}°"; break
                ms = zk.m(200) or set()
                if DONE_BITS["A1"] in ms or abs(d1 - 2.0) < 0.15:
                    verdict = f"✅ A1 {d1:+.2f}° 이동, 완료비트 {'ON' if DONE_BITS['A1'] in ms else '미확인'}"
                    break
            time.sleep(0.05)
    finally:
        for _ in range(3):
            zk.m_off(M21)
        P.all_off(zk)
    aN = P.angles(zk)
    print(f"  종료 {P.fmt(aN)}")
    print(f"  판정: {verdict}")
    print(f"  ΔA1 {aN['A1']-a0['A1']:+.3f}°  ΔA2 {aN['A2']-a0['A2']:+.3f}°  ΔA3 {aN['A3']-a0['A3']:+.3f}°")
    print("  마무리:  python3 zk_ddrva.py restore --robot ...")


def cmd_restore(zk):
    bak = json.load(open(BAK))
    ok = (w16(zk, 70, bak["D70"]) and w32(zk, 100, bak["D100"])
          and w16(zk, 400, bak["D400"]) and w16(zk, 450, bak["D450"])
          and w16(zk, 150, bak["D150"]))
    print(f"  원복 {bak}  {'✅' if ok else '🔴 일부 실패 — dump 로 확인'}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "dump"
    prof = P.apply_profile(ZPROF.resolve(argv=sys.argv))
    print(f"[{prof.name}] {prof.port}", flush=True)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit("링크 실패")
    try:
        ms = zk.m(32) or set()
        if cmd in ("prep", "fire") and any(b not in ms for b in HOME_FLAG.values()):
            sys.exit("■ 원점 미확보 — zk_startup.py 먼저")
        {"dump": cmd_dump, "prep": cmd_prep, "fire": cmd_fire,
         "restore": cmd_restore}.get(cmd, lambda z: sys.exit(__doc__))(zk)
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
