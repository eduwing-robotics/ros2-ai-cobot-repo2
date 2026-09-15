"""house_cycle 모의 검증 — 브리지·카메라 없이. 기존 10 + 9/6 보강 시나리오(안착 실패·하강 게이트·승격 history·run4·slot_both).
  HOUSE_MOCK_STATE=<dir> 로 상태 디렉터리 지정(기본: 이 파일 옆 hs_mock). 로봇·브리지에 아무것도 보내지 않는다."""
import os, sys, json, threading, time, shutil
SD = os.path.dirname(os.path.abspath(__file__)); ST = os.environ.get("HOUSE_MOCK_STATE") or os.path.join(SD, "hs_mock")
shutil.rmtree(ST, ignore_errors=True)
os.environ["HOUSE_STATE"] = ST
sys.path.insert(0, "/home/ar/bf2_console/tools")
import house_cycle as HC
PC, HA, HG = HC.PC, HC.HA, HC.HG
HC.seed_state()
state = {"tcp": [200.0, -330.0, 650.0, 180.0, 0.0, 180.0], "grip": 30}
LOG = []
def st(): return {"tcp": list(state["tcp"]), "gripper": state["grip"], "busy": False, "frozen": False}
def post(a, b):
    LOG.append(("post", a))
    if a == "move_tcp":
        if b.get("dry_run"): return {"result": "dry_run"}
        state["tcp"] = list(b["tcp"]); return {"result": "started"}
    if a == "grip_read": return {"result": str(state["grip"])}
    return {"result": "ok"}
def gripper(pos): state["grip"] = SC["grip_real"](pos); LOG.append(("grip", pos, state["grip"])); return str(state["grip"])
PC.st = st; PC.post = post; PC.speed = lambda v: LOG.append(("speed", v)); PC.gripper = gripper; PC.grip_read = lambda: str(state["grip"])
PC.wait_idle = lambda t=30: None
HA.st = st; HA.post = lambda a, b: "ok"
def move_rel(dx, dy, drz, tol=0.3, timeout=40):
    c = state["tcp"]; state["tcp"] = [c[0]+dx, c[1]+dy, c[2], c[3], c[4], HG.wrap_deg(c[5]+drz)]; LOG.append(("rel", dx, dy, drz)); return state["tcp"]
HC._ha_move_rel = move_rel
# 검출기 모의
BASE_PX = [(300.0, 200.0, 0), (800.0, 200.0, 1), (800.0, 520.0, 2), (300.0, 520.0, 3)]
PC.find_base_4pts = lambda holding=False: ([(x+SC["base_shift_px"][0], y+SC["base_shift_px"][1], i) for x, y, i in BASE_PX], (0.0, 0.0))
HC.BT.markers = lambda: (None, "모의: 마커 없음")
PC.rack_ends = lambda color, n=4, x_hint=None: SC.get("rack_by", {}).get(color, SC["rack"])
PC.held_wall_dots = lambda color: SC.get("held", [(900.0, 400.0, 800), (1100.0, 400.0, 800)])
_base0 = PC.find_base_4pts
NBASE = [0]
def _find_base(holding=False): NBASE[0] += 1; return _base0(holding)
PC.find_base_4pts = _find_base
PC._grip_ok = lambda gr, g_close, color: (SC["grip_ok"], "모의")
PC.grasp_measure = lambda color, rack_dang=None: (object(), {"across_mm": SC["dev"][0], "along_mm": SC["dev"][1], "dang": SC["dev"][2], "how": "모의"})
PC.load_grasp_ref = lambda color: {"x": 1} if SC["sig"] else None
def align(color, dry=False, tol_mm=0.3, tol_deg=0.15):
    if SC["align"] == "diverge": raise RuntimeError("호버 정렬 발산(모의)")
    move_rel(0.6, -1.2, -0.3); LOG.append(("align", color))
HA.align = align; HA.restore_expo = lambda: None; HA.load_ref = lambda color, src=None: ({"z": 440} if SC["hover"] else None); HA.promote_ref = lambda color, note="": LOG.append(("promote", color))
PC.descend_monitored = lambda color, x, y, rot, zs, g_close: LOG.append(("descend", round(x, 2), round(y, 2), round(rot[2], 2), zs))
import types, numpy as _np
def _verify():
    st_ = SC.get("seat", "seated")
    return {"state": st_ + "(모의)", "why": "모의", "_img": _np.zeros((2, 2)), "_above_pts": []}   # '_' 키는 JSON 불가 → house_cycle 이 걸러야
sys.modules["seat_verify"] = types.SimpleNamespace(verify=_verify)

def slot_ref(shift=(0, 0), dyaw=0.0):
    json.dump({"blue": {"seat_tcp": [203.0, -409.0, 354.0, 180.0, 0.0, 180.0], "base": {"x": 0.0, "y": 0.0, "yaw": -90.0}, "made": "모의"}}, open(HC.F["slot"], "w"))
    # 기준 베이스 = 지금 모의 픽셀에서 나오는 값으로 맞춘다
    B = HC.stage_base(); d = json.load(open(HC.F["slot"])); d["blue"]["base"] = {"x": B["x"], "y": B["y"], "yaw": B["yaw"]}; json.dump(d, open(HC.F["slot"], "w"))

def run(name, teach=False, buttons=()):
    global SC
    print(f"\n=== {name}"); LOG.clear(); HC.ABORT.clear(); HC.S.update(err=None, wait=None)
    def presser():
        for delay, op in buttons:
            if op == "abort": time.sleep(delay); HC.handle_cmd({"op": ["abort"]}); continue
            t0 = time.time()
            while HC.S.get("wait") is None and time.time() - t0 < 20: time.sleep(0.05)   # 실제 대기 상태가 된 뒤 버튼
            time.sleep(0.1)
            if op == "rack_offset": state["tcp"][0] += 2.0; state["tcp"][1] -= 1.0; HC.teach_rack_offset("blue"); HC.RESUME.set()
            elif op == "hover_ref": SC["hover"] = True; HC.RESUME.set()
            elif op == "sig": SC["sig"] = True; HC.RESUME.set()
    threading.Thread(target=presser, daemon=True).start()
    try:
        HC.run_cycle("blue", teach); stop = None
    except HC.Abort: stop = "중단"
    except HC.Gate as g: stop = f"게이트: {g}"
    except Exception as e: stop = f"예외: {e}"
    moves = [l for l in LOG if l[0] == "post" and l[1] == "move_tcp"]
    print("  단계:", HC.S["stage"], "| 정지:", stop)
    print("  이동수", len(moves), "| grip:", [l for l in LOG if l[0] == "grip"], "| align/promote:", [l for l in LOG if l[0] in ("align", "promote", "descend")])
    return stop

SC = {"base_shift_px": (0, 0), "rack": {"mid": (569.3, 390.7), "len_px": 567.8, "ang": 1.39, "n_dots": 5, "p1": (569.3, 106.8), "p2": (569.3, 674.6)},
      "grip_ok": True, "dev": (0.1, -0.4, 0.05), "sig": True, "hover": True, "align": "ok", "grip_real": lambda p: 13 if p < 20 else 30}
state["tcp"] = [200.0, -330.0, 650.0, 180.0, 0.0, 180.0]

# 0. 슬롯 기준 없음 → 픽 전에 정지
s = run("슬롯 기준 없음 → 픽 전 정지"); assert s and "슬롯 기준 없음" in s and not any(l[0] == "grip" for l in LOG)
slot_ref()
# 1. 정상: 베이스가 화면에서 +24px(≈10mm) 이동 + 정렬 → WAIT DESCEND → 하강 → DONE
SC["base_shift_px"] = (24.0, 0.0)
s = run("정상 풀사이클(베이스 +24px 이동)"); assert s is None and HC.S["stage"] == "WAIT DESCEND"
T = HC.S["target"]; print("  목표:", {k: round(v, 2) if isinstance(v, float) else v for k, v in T.items() if k in ("x", "y", "rz", "dyaw")})
assert abs((T["x"] - 203.0) - (-24 * 0.4135)) < 0.3 or abs((T["x"] - 203.0) + (-24 * 0.4135)) < 0.3, "베이스 이동이 목표에 반영돼야"
HC.stage_descend("blue"); d = [l for l in LOG if l[0] == "descend"][0]; a = HC.S["align"]
assert abs(d[1] - a["x"]) < 0.01 and abs(d[2] - a["y"]) < 0.01, "정렬된 TCP 로 하강해야"; print("  하강 TCP = 정렬 TCP ✓", d, "| 단계:", HC.S["stage"])
# 2. 랙 길이 불일치 → 파지 금지
SC["base_shift_px"] = (0, 0); SC["rack"] = dict(SC["rack"], len_px=479.0)
s = run("랙 길이 −16% → 파지 금지"); assert s and "길이 불일치" in s and not any(l[0] == "grip" and l[1] < 20 for l in LOG)
SC["rack"] = dict(SC["rack"], len_px=567.8)
# 3. 파지 편차 게이트
SC["dev"] = (0.1, 2.3, 0.0); s = run("파지 편차 +2.3mm → 정지"); assert s and "게이트 초과" in s
SC["dev"] = (0.1, -0.4, 0.05)
# 4. 랙 파지 티칭: 대기 → 조그(+2,−1) → 랙 보정 저장 → 계속 → 정상 완료, offset 이 벽 축으로 저장됨
s = run("랙 파지 티칭 흐름", teach=True, buttons=[(0.5, "rack_offset")]); assert s is None
off = json.load(open(HC.F["rack"]))["blue"]["offset"]; print("  저장 offset:", off); assert abs(abs(off["along"]) - 1.0) < 0.05 and abs(abs(off["across"]) - 2.0) < 0.05
# 5. 서명 없음 → 대기 → 서명 저장 → 계속
SC["sig"] = False; s = run("좋은 파지 서명 없음 → 버튼 대기 → 계속", buttons=[(0.5, "sig")]); assert s is None
# 6. z440 기준 없음 → 대기 → 저장 → 계속 → 정렬
SC["hover"] = False; s = run("z440 기준 없음 → 버튼 대기 → 계속", buttons=[(0.5, "hover_ref")]); assert s is None and any(l[0] == "align" for l in LOG)
# 7. 정렬 발산 → 정지(하강 없음)
SC["align"] = "diverge"; s = run("호버 정렬 발산 → 정지"); assert s and "발산" in s; SC["align"] = "ok"
# 8. 사용자 중단(운반 중)
s = run("운반 중 ⛔ 중단", buttons=[(0.05, "abort")]); assert s == "중단" and ("post", "stop") in LOG
# 9. 빈 파지
SC["grip_ok"] = False; s = run("빈 파지 → 정지"); assert s and "빈 파지" in s
print("\n기존 모의 10 시나리오 통과")

# ====================================================================== 9/6 보강 시나리오
N = [10]
def case(name):
    N[0] += 1; print(f"\n=== [{N[0]}] {name}")
def grips(): return [l for l in LOG if l[0] == "grip"]
def promotes(): return [l for l in LOG if l[0] == "promote"]
def press_later(op, delay=0.3, **kw):
    """대기 상태가 된 뒤 버튼."""
    def t():
        t0 = time.time()
        while HC.S.get("wait") is None and time.time() - t0 < 20: time.sleep(0.05)
        time.sleep(delay)
        if op == "abort": HC.handle_cmd({"op": ["abort"]})
        elif op == "resume": HC.handle_cmd({"op": ["resume"]})
        elif op == "descend": HC.handle_cmd({"op": ["descend"], "color": ["blue"]})
        elif op == "open_then_resume": state["grip"] = 30; HC.handle_cmd({"op": ["resume"]})
    threading.Thread(target=t, daemon=True).start()
def try_descend(color="blue"):
    try: return HC.stage_descend(color), None
    except HC.Abort: return None, "중단"
    except HC.Gate as g: return None, f"게이트: {g}"
    except Exception as e: return None, f"예외: {e}"
SC.update(grip_ok=True, dev=(0.1, -0.4, 0.05), sig=True, hover=True, align="ok", seat="seated"); SC["base_shift_px"] = (0, 0)

# 11. 안착 실패 → 그리퍼 유지·SEAT FAIL 대기 → ⛔중단: 그리퍼 안 열림·승격 없음
case("안착 실패(perched) → 그리퍼 유지 → ⛔중단")
s = run("사이클"); assert s is None
SC["seat"] = "perched"; LOG.clear(); HC.ABORT.clear(); slot_before = json.load(open(HC.F["slot"]))["blue"]
press_later("abort", 0.3); ok, stop = try_descend()
print("  단계:", HC.S["stage"], "| 정지:", stop, "| grip:", grips(), "| promote:", promotes(), "| tcp z:", round(state["tcp"][2], 1))
assert stop == "중단" and not grips() and not promotes(), "안착 실패면 그리퍼를 열지도 승격하지도 않아야"
assert json.load(open(HC.F["slot"]))["blue"] == slot_before, "슬롯 기준 변경 없어야"
assert any(l[0] == "post" and l[1] == "stop" for l in LOG)

# 12. 안착 실패 → ▶계속(사용자 육안 확인) → 개방·상승, 승격은 없음
case("안착 실패 → ▶계속 → 개방·상승(승격 없음)")
SC["seat"] = "seated"; s = run("사이클"); assert s is None
SC["seat"] = "perched"; LOG.clear(); HC.ABORT.clear(); slot_before = json.load(open(HC.F["slot"]))["blue"]
press_later("resume", 0.3); ok, stop = try_descend()
print("  단계:", HC.S["stage"], "| 정지:", stop, "| grip:", grips(), "| promote:", promotes(), "| seat:", HC.S["seat"])
assert stop is None and ok is False and grips() and grips()[0][1] == 30 and not promotes()
assert json.load(open(HC.F["slot"]))["blue"] == slot_before and HC.S["seat"].get("user_override") and state["tcp"][2] == 650.0
assert "_img" not in HC.S["seat"]; json.dumps(HC.snapshot_S() if hasattr(HC, "snapshot_S") else {k: v for k, v in HC.S.items() if k != "log"})   # JSON 직렬화 가능

# 13. 하강 게이트: 정렬 후 TCP 가 XY 0.6mm / rz 0.4° 움직임 → 거부, 0.3mm 는 통과
case("하강 게이트: 정렬 TCP 와 불일치 → 거부")
SC["seat"] = "seated"; s = run("사이클"); assert s is None; LOG.clear()
state["tcp"][0] += 0.6; ok, stop = try_descend(); print("  XY +0.6:", stop); assert stop and "불일치" in stop and not grips()
state["tcp"][0] -= 0.6; state["tcp"][5] = HG.wrap_deg(state["tcp"][5] + 0.4); ok, stop = try_descend(); print("  rz +0.4:", stop); assert stop and "불일치" in stop
state["tcp"][5] = HG.wrap_deg(state["tcp"][5] - 0.4); state["tcp"][2] += 4.0; ok, stop = try_descend(); print("  z +4:", stop); assert stop and "≠" in stop
state["tcp"][2] -= 4.0; ok, stop = try_descend("yellow"); print("  색 불일치:", stop); assert stop and "색 불일치" in stop
state["tcp"][0] += 0.3; LOG.clear(); ok, stop = try_descend(); print("  XY +0.3:", stop, HC.S["stage"]); assert stop is None and ok

# 14. 정렬 후 15분 경과 → 거부
case("정렬 후 15분 경과 → 하강 거부")
s = run("사이클"); assert s is None; LOG.clear()
HC.S["align"]["made_t"] -= 15 * 60 + 1; ok, stop = try_descend(); print("  ", stop); assert stop and "경과" in stop and not grips()
HC.S["align"]["made_t"] += 2; ok, stop = try_descend(); print("  14분59초:", stop); assert stop is None

# 15. 승격 history: 성공마다 이전 값이 history 로, 최대 5건, seat z 는 티칭값 유지
case("슬롯 기준 승격 history(최대 5)")
hist_len = len(json.load(open(HC.F["slot"]))["blue"].get("history", []))
for k in range(6):
    SC["base_shift_px"] = (3.0 * k, -2.0 * k); s = run(f"사이클 #{k}"); assert s is None
    prev = json.load(open(HC.F["slot"]))["blue"]; ok, stop = try_descend(); assert stop is None and ok
    d = json.load(open(HC.F["slot"]))["blue"]
    assert d["history"][0]["seat_tcp"] == prev["seat_tcp"] and d["history"][0]["base"] == prev["base"] and "history" not in d["history"][0]
    assert len(d["history"]) == min(hist_len + k + 1, 5), (len(d["history"]), hist_len, k)
    assert d["seat_tcp"][2] == prev["seat_tcp"][2] == 354.0 and abs(d["seat_tcp"][0] - HC.S["align"]["x"]) < 1e-3 and d["base"]["x"] == HC.S["base"]["x"]
print("  history:", len(d["history"]), "건 | 최신 seat:", [round(v, 2) for v in d["seat_tcp"][:3]], "| 최신 base:", {k: round(v, 2) for k, v in d["base"].items()})
SC["base_shift_px"] = (0, 0)

# 16. run4: 2벽(blue→yellow) 성공 — 벽마다 빈손 베이스 재측정, 하강은 버튼(descend op = 계속)
case("run4 2벽 성공(벽마다 베이스 재측정·하강 버튼)")
rr = json.load(open(HC.F["rack"])); assert "yellow" in rr, "seed 에 yellow 랙 기준이 있어야"
SC["rack_by"] = {"yellow": {"mid": tuple(rr["yellow"]["Pc0"]), "len_px": rr["yellow"]["len0_px"], "ang": rr["yellow"]["ang0"], "n_dots": 4,
                            "p1": (rr["yellow"]["Pc0"][0], rr["yellow"]["Pc0"][1] - rr["yellow"]["len0_px"] / 2), "p2": (rr["yellow"]["Pc0"][0], rr["yellow"]["Pc0"][1] + rr["yellow"]["len0_px"] / 2)}}
d = json.load(open(HC.F["slot"])); d["yellow"] = dict(d["blue"], seat_tcp=[250.0, -400.0, 354.0, 180.0, 0.0, 90.0]); json.dump(d, open(HC.F["slot"], "w"))
def run4(order, presses):
    LOG.clear(); HC.ABORT.clear(); HC.S.update(err=None, wait=None); NBASE[0] = 0
    def presser():
        for op in presses:
            t0 = time.time()
            while HC.S.get("wait") is None and time.time() - t0 < 30: time.sleep(0.05)
            time.sleep(0.2)
            if op == "descend": r = HC.handle_cmd({"op": ["descend"], "color": ["blue"]}); assert r.get("note"), r
            elif op == "abort": HC.handle_cmd({"op": ["abort"]})
            elif op == "resume": HC.handle_cmd({"op": ["resume"]})
    threading.Thread(target=presser, daemon=True).start()
    try: HC.run_multi(order); return None
    except HC.Abort: return "중단"
    except HC.Gate as g: return f"게이트: {g}"
    except Exception as e: return f"예외: {e}"
stop = run4(["blue", "yellow"], ["descend", "descend"])
print("  단계:", HC.S["stage"], "| 정지:", stop, "| 베이스 측정:", NBASE[0], "| run4:", HC.S["run4"], "| descend:", [l for l in LOG if l[0] == "descend"])
assert stop is None and HC.S["stage"] == "RUN4 DONE" and HC.S["run4"]["done"] == ["blue", "yellow"] and NBASE[0] == 2 and not HC.S["run4"]["active"]
assert len([l for l in LOG if l[0] == "descend"]) == 2 and len(promotes()) == 2

# 17. run4 중단 전파: yellow 정렬 발산 → 전체 중단(red 진행 없음); 안착 실패 → 계속 → run4 중단
case("run4 중단 전파(2번째 벽 실패 → 전체 정지)")
SC["align_by_color"] = {"yellow": "diverge"}
_align0 = HA.align
def align2(color, dry=False, tol_mm=0.3, tol_deg=0.15):
    if SC.get("align_by_color", {}).get(color) == "diverge": raise RuntimeError("호버 정렬 발산(모의)")
    return _align0(color, dry, tol_mm, tol_deg)
HA.align = align2
stop = run4(["blue", "yellow", "red"], ["descend"])
print("  정지:", stop, "| run4:", HC.S["run4"], "| 베이스 측정:", NBASE[0])
assert stop and "발산" in stop and HC.S["run4"]["done"] == ["blue"] and HC.S["run4"]["idx"] == 1 and NBASE[0] == 2 and not HC.S["run4"]["active"]
SC["align_by_color"] = {}
SC["seat"] = "perched"
stop = run4(["blue", "yellow"], ["descend", "resume"])
print("  안착실패→계속:", stop, "| run4:", HC.S["run4"])
assert stop and "run4 중단" in stop and HC.S["run4"]["done"] == [] and NBASE[0] == 1
SC["seat"] = "seated"
stop = run4(["blue", "yellow"], ["abort"])
print("  첫 벽 하강 대기 중 ⛔:", stop, "| run4:", HC.S["run4"]); assert stop == "중단" and HC.S["run4"]["done"] == []
# 하강 버튼은 run4 대기 중이 아니면 큐로(여기선 워커 없음 → note 없음)
r = HC.handle_cmd({"op": ["descend"], "color": ["blue"]}); assert r["ok"] and not r.get("note")

# 18. slot_both: 빈손 거부(그리퍼 ≤ 닫힘값 / 벽 점 0) · 정상 흐름(1/2 → 대기 → 계속 → 2/2)
case("slot_both 빈손 거부 + 정상 흐름")
gc = json.load(open(HC.F["rack"]))["blue"]["grip_close"]
def try_both():
    try: HC.teach_slot_both("blue"); return None
    except HC.Abort: return "중단"
    except HC.Gate as g: return f"게이트: {g}"
    except Exception as e: return f"예외: {e}"
HC._pending_seat.clear(); HC.ABORT.clear(); HC.S.update(wait=None)
state["grip"] = gc; SC["held"] = [(900.0, 400.0, 800)]; stop = try_both(); print("  그리퍼=닫힘값:", stop); assert stop and "빈손" in stop and "blue" not in HC._pending_seat
state["grip"] = gc + 6; SC["held"] = []; stop = try_both(); print("  벽 점 0:", stop); assert stop and "빈손" in stop and "blue" not in HC._pending_seat
SC["held"] = [(900.0, 400.0, 800)]; state["tcp"] = [210.0, -405.0, 355.0, 180.0, 0.0, 180.0]
press_later("resume", 0.3); stop = try_both(); print("  계속 눌렀지만 그리퍼 아직 닫힘:", stop); assert stop and "아직 닫힘" in stop and HC._pending_seat.get("blue")
state["tcp"] = [210.0, -405.0, 355.0, 180.0, 0.0, 180.0]
press_later("open_then_resume", 0.3); stop = try_both(); d = json.load(open(HC.F["slot"]))["blue"]
print("  정상:", stop, "| 단계:", HC.S["stage"], "| seat_tcp:", d["seat_tcp"][:3], "| tcp:", [round(v) for v in state["tcp"][:3]])
assert stop is None and d["seat_tcp"][:3] == [210.0, -405.0, 355.0] and "blue" not in HC._pending_seat and state["tcp"][2] == 650.0
SC["held"] = None; del SC["held"]

# 19. 하강 버튼 게이트(스냅샷): WAIT DESCEND 자체는 '대기'지만 하강 버튼을 막으면 안 된다
case("스냅샷 게이트: WAIT DESCEND 에서 하강 버튼 활성")
s = run("사이클"); assert s is None
snap = HC.snapshot(); print("  gates:", snap["gates"]); assert all(snap["gates"].values()) and snap["stage"] == "WAIT DESCEND"
json.dumps(snap, ensure_ascii=False)

print(f"\n모의 전부 통과 ({N[0]} 시나리오)")
