#!/usr/bin/env python3
"""
리프트 조그 데몬 — UDP 명령대로 계속 움직이고, 명령이 끊기면 스스로 멈춘다.

  python3 fk_jogd.py --pins 26,19,13,6 --port 5005

핵심 성질
  - 이동이 별도 스레드라 명령 수신이 블로킹되지 않음 (주행 노드와 같이 돌릴 수 있음)
  - 데드맨: 0.35초 동안 명령이 안 오면 자동 정지 (클라이언트가 죽어도 안 폭주)
  - 정지 시 코일 끔 → 발열 없음 (리드 스크류 셀프 락킹으로 높이 유지)

명령 (UDP 텍스트, 응답은 상태 한 줄)
  U / D      위/아래로 계속 (누르고 있는 동안 반복 전송할 것)
  S          정지
  Z          지금 위치를 0으로
  L / LH / LT / L<lo>:<hi>      리밋 해제 / 하한 / 상한 / 직접지정
  V<us> A<us> C<us> K<mm> M0/M1 H0/H1 R0/R1
  G<steps>   절대위치로 (리밋 안에서)
  P<mm>      높이 mm 로
  Y<steps>   지금 위치가 실제로 몇 스텝인지 알려주기 (기준 복구용)
  E1~E4      지금 높이를 그 번호에 기억
  J1~J4      기억해 둔 그 높이로 이동
  W / X      설정 저장 / 불러오기
  ?          상태
"""
import argparse, json, os, socket, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forklift_pi import Lift, find_chip, precise_sleep, STEPS_PER_REV, CFG_PATH

DEADMAN = 0.50          # 초. 이 시간 동안 명령 없으면 정지
COAST   = 0.20          # 초. 이 안에 재개하면 속도(램프)를 유지한다
HOLD_MS = 0.25          # 초. 멈춘 직후 이만큼은 코일을 물고 있는다 (미끄럼 방지)


class Jogger:
    def __init__(self, pins, chip):
        self.lift = Lift(pins, chip)
        self.lift.load(quiet=True)
        self.lock = threading.Lock()
        self.dir = 0                 # -1 아래 / 0 정지 / +1 위
        self.last_cmd = 0.0
        self.alive = True
        self.note = ''               # 마지막 사건 (리밋 걸림 등)
        self.seq = ''                # 마지막 명령의 순번 (응답 짝맞춤용)
        # 상승은 중력을 거스르므로 하강보다 느려야 한다 (스테퍼는 느릴수록 토크가 큼).
        # 저장본에 값이 있으면 그걸 쓰고, 없으면 상승만 기본을 넉넉히 잡는다.
        cfg = {}
        try:
            with open(CFG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            pass
        self.up_delay = cfg.get('up_delay', max(self.lift.min_delay, 1500e-6))
        self.down_delay = cfg.get('down_delay', self.lift.min_delay)
        # 기억 높이 4칸. 값은 하프스텝 단위 절대위치, 안 정했으면 None
        mem = cfg.get('mem') or [None] * 4
        self.mem = (list(mem) + [None] * 4)[:4]
        self.goal = None             # G/P 명령용 목표 (None 이면 연속 조그)
        threading.Thread(target=self._loop, daemon=True).start()

    # ── 이동 스레드 ──
    def _loop(self):
        lf = self.lift
        delay = lf.start_delay
        next_t = None
        stopped_at = None
        while self.alive:
            now = time.time()
            with self.lock:
                d = self.dir
                goal = self.goal
                stale = (d != 0 and goal is None
                         and now - self.last_cmd > DEADMAN)
                if stale:
                    self.dir = 0
                    d = 0

            if d == 0:
                if stopped_at is None:
                    stopped_at = now
                idle = now - stopped_at
                # 멈춘 직후 잠깐은 코일을 물어 둔다 — 놓치면 짐 무게로 미끄러진다
                if idle > HOLD_MS and not lf.hold:
                    lf.coils_off()
                # 충분히 쉬었을 때만 가속 램프를 처음으로 되돌린다.
                # 그 전에 재개되면 속도를 유지해서 끊김 없이 이어진다.
                if idle > COAST:
                    delay = lf.start_delay
                next_t = None
                time.sleep(0.003)
                continue
            stopped_at = None

            # 리밋 검사
            inc = 2 if lf.full_step else 1
            nxt = lf.position + d * inc
            if lf.limit_on and (nxt > lf.limit_max or nxt < lf.limit_min):
                with self.lock:
                    self.dir = 0
                    self.goal = None
                    self.note = '리밋 끝'
                continue

            # 목표 도달 검사 (G/P)
            if goal is not None and ((d > 0 and lf.position >= goal) or
                                     (d < 0 and lf.position <= goal)):
                with self.lock:
                    self.dir = 0
                    self.goal = None
                    self.note = '도착'
                continue

            if next_t is None:
                next_t = time.perf_counter()
            next_t += delay
            precise_sleep(next_t)

            lf.phase = (lf.phase + d * inc) % 8
            lf._apply(lf.phase)
            lf.position += d * inc

            target = self.up_delay if d > 0 else self.down_delay
            if delay > target:
                delay = max(delay - lf.accel, target)
            elif delay < target:
                delay = target

    # ── 명령 ──
    def cmd(self, s):
        s = s.strip()
        # "<순번>|<명령>" 형태면 순번을 떼어 응답에 그대로 실어준다.
        # UDP 응답이 한 칸씩 밀려 엉뚱한 상태를 읽는 걸 막기 위한 것.
        self.seq = ''
        if '|' in s[:12]:
            self.seq, s = s.split('|', 1)
            s = s.strip()
        if not s:
            return self.status()
        c, arg = s[0].upper(), s[1:].strip()
        lf = self.lift

        def num(dflt=0.0):
            try:
                return float(arg)
            except ValueError:
                return dflt

        if c == 'U':
            with self.lock:
                self.dir, self.goal, self.last_cmd = 1, None, time.time()
                self.note = ''
        elif c == 'D':
            with self.lock:
                self.dir, self.goal, self.last_cmd = -1, None, time.time()
                self.note = ''
        elif c == 'S':
            with self.lock:
                self.dir, self.goal = 0, None
        elif c in ('G', 'P'):
            tgt = int(num()) if c == 'G' else lf.dir_sign() * lf.mm_to_steps(num())
            tgt = lf.clamp(tgt)
            with self.lock:
                self.goal = tgt
                self.dir = 1 if tgt > lf.position else (-1 if tgt < lf.position else 0)
                self.note = ''
        elif c == 'E':                       # E1~E4 : 지금 높이를 그 번호에 기억
            try:
                n = int(arg)
            except ValueError:
                n = 0
            if 1 <= n <= 4:
                self.mem[n - 1] = lf.position
                self.note = f'{n}번에 {lf.steps_to_mm(lf.position)*lf.dir_sign():.1f}mm 기억'
            else:
                self.note = '! E1~E4 만 됨'
        elif c == 'J':                       # J1~J4 : 그 번호 높이로 이동
            try:
                n = int(arg)
            except ValueError:
                n = 0
            if not (1 <= n <= 4):
                self.note = '! J1~J4 만 됨'
            elif self.mem[n - 1] is None:
                self.note = f'! {n}번은 아직 안 정해짐'
            else:
                tgt = lf.clamp(int(self.mem[n - 1]))
                with self.lock:
                    self.goal = tgt
                    self.dir = 1 if tgt > lf.position else (-1 if tgt < lf.position else 0)
                self.note = f'{n}번으로 이동'
        elif c == 'Y':                       # Y<steps> : 지금 위치가 실제로 어디인지 알려준다
            try:
                v = int(float(arg))
            except ValueError:
                self.note = '! Y<스텝수> 형식'
            else:
                with self.lock:
                    lf.position = v
                self.note = f'현재 위치를 {v} 로 맞춤'
        elif c == 'Z':
            with self.lock:
                lf.position = 0
            self.note = '0 재설정'
        elif c == 'V':
            sub = arg[:1].upper()
            if sub in ('U', 'D'):
                try:
                    v = max(float(arg[1:]), 200) * 1e-6
                except ValueError:
                    v = None
                if v:
                    if sub == 'U':
                        self.up_delay = v
                    else:
                        self.down_delay = v
                    self.note = f"{'상승' if sub == 'U' else '하강'} {v*1e6:.0f}us"
            else:
                v = max(num(600), 200) * 1e-6
                lf.min_delay = self.up_delay = self.down_delay = v
                lf.start_delay = max(lf.start_delay, v)
        elif c == 'A':
            lf.accel = max(num(15), 0.5) * 1e-6
        elif c == 'C':
            lf.start_delay = max(num(2800) * 1e-6, lf.min_delay)
        elif c == 'K':
            if num() > 0:
                lf.lead_mm = num()
        elif c == 'M':
            lf.full_step = (arg not in ('', '0'))
            if lf.full_step and lf.phase % 2 == 0:
                lf.phase = (lf.phase + 1) % 8
        elif c == 'H':
            lf.hold = (arg not in ('', '0'))
            if not lf.hold:
                lf.coils_off()
        elif c == 'R':
            lf.invert = (arg not in ('', '0'))
        elif c == 'L':
            sub = arg[:1].upper()
            if arg == '':
                lf.limit_on = False
                self.note = '리밋 해제'
            elif sub == 'H':
                lf.limit_min = lf.position
                if lf.limit_max > lf.limit_min:
                    lf.limit_on = True
                self.note = f'하한 {lf.limit_min}'
            elif sub == 'T':
                if lf.position > lf.limit_min:
                    lf.limit_max = lf.position
                    lf.limit_on = True
                    self.note = f'상한 {lf.limit_max}'
                else:
                    self.note = '! 상한이 하한보다 위여야 함'
            elif ':' in arg:
                lo, hi = (int(v) for v in arg.split(':', 1))
                if hi > lo:
                    lf.limit_min, lf.limit_max, lf.limit_on = lo, hi, True
                    self.note = f'리밋 {lo}~{hi}'
        elif c == 'W':
            lf.save()
            try:
                with open(CFG_PATH) as f:
                    cfg = json.load(f)
                cfg['up_delay'] = self.up_delay
                cfg['down_delay'] = self.down_delay
                cfg['mem'] = self.mem
                with open(CFG_PATH, 'w') as f:
                    json.dump(cfg, f, indent=2)
            except Exception as e:
                self.note = f'저장 실패 {e}'
            else:
                self.note = '저장됨'
        elif c == 'X':
            lf.load(quiet=True)
            try:
                with open(CFG_PATH) as f:
                    cfg = json.load(f)
                self.up_delay = cfg.get('up_delay', self.up_delay)
                self.down_delay = cfg.get('down_delay', self.down_delay)
                if cfg.get('mem'):
                    self.mem = (list(cfg['mem']) + [None] * 4)[:4]
            except Exception:
                pass
            self.note = '불러옴'
        return self.status()

    def status(self):
        lf = self.lift
        with self.lock:
            d = self.dir
        return json.dumps({
            'seq': getattr(self, 'seq', ''),
            'pos': lf.position,
            'mm': round(lf.steps_to_mm(lf.position) * lf.dir_sign(), 2),
            'dir': d,
            'us': int(round(self.up_delay * 1e6)),
            'up_us': int(round(self.up_delay * 1e6)),
            'down_us': int(round(self.down_delay * 1e6)),
            'accel': int(lf.accel * 1e6),
            'lead': lf.lead_mm,
            'full': lf.full_step,
            'limit_on': lf.limit_on,
            'lo': lf.limit_min,
            'hi': lf.limit_max,
            'hold': lf.hold,
            'mem': self.mem,
            'mem_mm': [None if m is None else round(lf.steps_to_mm(m) * lf.dir_sign(), 1)
                       for m in self.mem],
            'note': self.note,
        }, ensure_ascii=False)

    def close(self):
        self.alive = False
        time.sleep(0.1)
        self.lift.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pins', default='26,19,13,6')
    ap.add_argument('--chip', type=int, default=None)
    ap.add_argument('--port', type=int, default=5005)
    a = ap.parse_args()

    pins = [int(x) for x in a.pins.split(',')]
    jog = Jogger(pins, find_chip(pins, a.chip))

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', a.port))
    print(f"리프트 데몬 시작 — 핀 {pins}, UDP {a.port}, 데드맨 {DEADMAN}s", flush=True)
    print(f"설정: {CFG_PATH}", flush=True)
    print(jog.status(), flush=True)

    try:
        while True:
            data, addr = srv.recvfrom(256)
            try:
                reply = jog.cmd(data.decode('utf-8', 'replace'))
            except Exception as e:
                reply = json.dumps({'err': str(e)}, ensure_ascii=False)
            srv.sendto(reply.encode(), addr)
    except KeyboardInterrupt:
        pass
    finally:
        jog.close()
        print("코일 끄고 종료", flush=True)


if __name__ == '__main__':
    main()
