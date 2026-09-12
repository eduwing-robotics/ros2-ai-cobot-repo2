#!/usr/bin/env python3
"""
포크리프트 리프트 — 28BYJ-48 + ULN2003 을 라즈베리파이 GPIO로 직접 제어

XIAO 펌웨어와 명령 체계를 똑같이 맞춰서, 쓰던 방식 그대로 쓸 수 있음.

사용법:
    python3 forklift_pi.py --pins 17,18,27,22
    python3 forklift_pi.py --pins 17,18,27,22 --test     # 자동 테스트만 하고 종료

핀 번호는 BCM(GPIO) 번호. 물리 핀 자리번호가 아님.
설정은 ~/.forklift_pi.json 에 저장되고 다음 실행 때 자동으로 불러옴 (W 로 저장).
"""
import argparse, json, os, sys, time

CFG_PATH = os.path.expanduser('~/.forklift_pi.json')

try:
    import lgpio
except ImportError:
    sys.exit("lgpio 가 없어.  sudo apt install -y python3-lgpio  로 설치해줘.")

STEPS_PER_REV = 4096          # 하프스텝 기준 1회전

# 하프스텝 8단계. 홀수 인덱스(1,3,5,7)만 쓰면 풀스텝(2상 여자)이 된다.
SEQ = [
    (1, 0, 0, 0),
    (1, 1, 0, 0),   # 풀스텝
    (0, 1, 0, 0),
    (0, 1, 1, 0),   # 풀스텝
    (0, 0, 1, 0),
    (0, 0, 1, 1),   # 풀스텝
    (0, 0, 0, 1),
    (1, 0, 0, 1),   # 풀스텝
]


def precise_sleep(target_t):
    """리눅스는 sleep 정확도가 들쭉날쭉해서, 대부분 자고 마지막만 지켜본다"""
    while True:
        remain = target_t - time.perf_counter()
        if remain <= 0:
            return
        if remain > 0.0008:            # 0.8ms 넘게 남았으면 그냥 잔다
            time.sleep(remain - 0.0005)
        # 남은 자투리는 바쁘게 기다림(정확도 확보)


def find_chip(pins, want=None):
    """쓸 수 있는 gpiochip 을 찾는다.

    파이 모델마다 번호가 다르다. Pi 4는 보통 0, Pi 5는 RP1 칩이라 0 또는 4.
    지정값이 있으면 그것만 쓰고, 없으면 실제로 열리는 걸 찾는다.
    """
    candidates = [want] if want is not None else [0, 4, 1, 2, 3]
    errors = []
    for c in candidates:
        try:
            h = lgpio.gpiochip_open(c)
        except Exception as e:
            errors.append(f"  gpiochip{c}: 열 수 없음 ({e})")
            continue
        try:
            for p in pins:
                lgpio.gpio_claim_output(h, p, 0)
            for p in pins:                      # 확인용이므로 바로 반납
                lgpio.gpio_free(h, p)
            lgpio.gpiochip_close(h)
            return c
        except Exception as e:
            errors.append(f"  gpiochip{c}: 핀을 못 잡음 ({e})")
            try:
                lgpio.gpiochip_close(h)
            except Exception:
                pass
    msg = "쓸 수 있는 gpiochip 을 못 찾았어.\n" + "\n".join(errors)
    msg += ("\n\n확인해볼 것:\n"
            "  ls -l /dev/gpiochip*        (권한이 dialout 그룹인지)\n"
            "  groups                      (dialout 에 속해 있는지)\n"
            "  다른 프로그램이 그 핀을 쓰고 있지 않은지")
    raise SystemExit(msg)


class Lift:
    def __init__(self, pins, chip=0):
        self.pins = pins
        self.h = lgpio.gpiochip_open(chip)
        for p in pins:
            lgpio.gpio_claim_output(self.h, p, 0)

        self.position = 0          # 하프스텝 단위
        self.phase = 1             # 풀스텝과 호환되게 홀수에서 시작
        self.full_step = False

        self.min_delay = 600e-6    # 최고속 간격(초)
        self.start_delay = 2800e-6 # 출발 간격
        self.accel = 15e-6         # 스텝마다 줄이는 양
        self.hold = False          # 정지 시 코일 유지

        self.lead_mm = 8.0         # 나사 1회전당 이동 mm
        self.invert = False

        self.limit_on = False
        self.limit_min = 0
        self.limit_max = 0

        # 실제 타이밍이 얼마나 잘 지켜졌는지 기록
        self.last_expected = 0.0
        self.last_actual = 0.0

    # ── 저수준 ──
    def _apply(self, phase):
        for pin, val in zip(self.pins, SEQ[phase]):
            lgpio.gpio_write(self.h, pin, val)

    def coils_off(self):
        for p in self.pins:
            lgpio.gpio_write(self.h, p, 0)

    def close(self):
        self.coils_off()
        for p in self.pins:
            try:
                lgpio.gpio_free(self.h, p)
            except Exception:
                pass
        lgpio.gpiochip_close(self.h)

    # ── 단위 변환 ──
    def mm_to_steps(self, mm):
        return int(round(mm / self.lead_mm * STEPS_PER_REV))

    def steps_to_mm(self, st):
        return st / STEPS_PER_REV * self.lead_mm

    def dir_sign(self):
        return -1 if self.invert else 1

    def ramp_steps(self):
        if self.accel <= 0 or self.start_delay <= self.min_delay:
            return 0
        return int((self.start_delay - self.min_delay) / self.accel) + 1

    def clamp(self, t):
        if not self.limit_on:
            return t
        if t < self.limit_min:
            print(f"  ! 하한 걸림: {t} -> {self.limit_min}")
            return self.limit_min
        if t > self.limit_max:
            print(f"  ! 상한 걸림: {t} -> {self.limit_max}")
            return self.limit_max
        return t

    # ── 이동 ──
    def move_to(self, target):
        target = self.clamp(target)
        if target == self.position:
            return 0.0

        inc = 2 if self.full_step else 1
        direction = 1 if target > self.position else -1
        delay = self.start_delay
        ramp = self.ramp_steps()

        t_start = time.perf_counter()
        next_t = t_start
        expected = 0.0

        while self.position != target:
            next_t += delay
            expected += delay
            precise_sleep(next_t)

            self.phase = (self.phase + direction * inc) % 8
            self._apply(self.phase)
            self.position += direction * inc

            # 목표를 지나치지 않게 (풀스텝은 2칸씩이라 딱 안 떨어질 수 있음)
            if (direction > 0 and self.position >= target) or \
               (direction < 0 and self.position <= target):
                self.position = target

            remain = abs(target - self.position) // inc
            if remain <= ramp:
                delay = min(delay + self.accel, self.start_delay)     # 감속
            elif delay > self.min_delay:
                delay = max(delay - self.accel, self.min_delay)       # 가속

        actual = time.perf_counter() - t_start
        self.last_expected, self.last_actual = expected, actual

        if not self.hold:
            self.coils_off()
        return actual

    # ── 설정 저장 ──
    def save(self):
        cfg = {
            'min_delay': self.min_delay, 'start_delay': self.start_delay,
            'accel': self.accel, 'full_step': self.full_step,
            'lead_mm': self.lead_mm, 'invert': self.invert,
            'limit_on': self.limit_on,
            'limit_min': self.limit_min, 'limit_max': self.limit_max,
        }
        with open(CFG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)
        print(f"  [저장됨] {CFG_PATH}")

    def load(self, quiet=False):
        if not os.path.exists(CFG_PATH):
            if not quiet:
                print("  저장된 설정이 없어")
            return False
        try:
            with open(CFG_PATH) as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"  ! 설정 읽기 실패: {e}")
            return False
        for k, v in cfg.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if not quiet:
            print(f"  [불러옴] {CFG_PATH}")
        return True

    # ── 상태 ──
    def status(self):
        h = self.steps_to_mm(self.position) * self.dir_sign()
        lim = (f"{self.steps_to_mm(self.limit_min)*self.dir_sign():.1f}~"
               f"{self.steps_to_mm(self.limit_max)*self.dir_sign():.1f}mm"
               if self.limit_on else "없음")
        return (f"[상태] 위치={self.position}  높이={h:.2f}mm  "
                f"모드={'풀스텝' if self.full_step else '하프'}  "
                f"최고속={self.min_delay*1e6:.0f}us  가속={self.accel*1e6:.0f}  "
                f"리드={self.lead_mm}mm  리밋={lim}  "
                f"홀드={'ON' if self.hold else 'OFF'}")


def run_command(lift, cmd):
    """XIAO 펌웨어와 같은 명령 체계"""
    cmd = cmd.strip()
    if not cmd:
        return
    c, arg = cmd[0].upper(), cmd[1:].strip()

    def num(default=0.0):
        try:
            return float(arg)
        except ValueError:
            return default

    if c == 'F':
        t = lift.move_to(lift.position + int(num()))
        print(f"  정방향 {int(num())} 스텝 ({t:.2f}초)")
    elif c == 'B':
        t = lift.move_to(lift.position - int(num()))
        print(f"  역방향 {int(num())} 스텝 ({t:.2f}초)")
    elif c == 'T':
        t = lift.move_to(lift.position + int(num() * STEPS_PER_REV))
        print(f"  회전 {num():.3f} 바퀴 ({t:.2f}초)")
    elif c == 'G':
        t = lift.move_to(int(num()))
        print(f"  절대위치 {int(num())} ({t:.2f}초)")
    elif c == 'U':
        t = lift.move_to(lift.position + lift.dir_sign() * lift.mm_to_steps(num()))
        print(f"  위로 {num():.2f}mm ({t:.2f}초)")
    elif c == 'D':
        t = lift.move_to(lift.position - lift.dir_sign() * lift.mm_to_steps(num()))
        print(f"  아래로 {num():.2f}mm ({t:.2f}초)")
    elif c == 'P':
        t = lift.move_to(lift.dir_sign() * lift.mm_to_steps(num()))
        print(f"  높이 {num():.2f}mm 로 ({t:.2f}초)")
    elif c == 'K':
        if num() > 0:
            lift.lead_mm = num()
            print(f"  리드 = {lift.lead_mm}mm/회전, 1스텝 = "
                  f"{lift.steps_to_mm(1)*1000:.4f}um")
    elif c == 'R':
        lift.invert = (arg not in ('', '0'))
        print(f"  방향 반전 = {'ON' if lift.invert else 'OFF'}")
    elif c == 'Z':
        lift.position = 0
        print("  현재 위치를 0으로")
    elif c == 'V':
        v = max(num(600), 200)
        lift.min_delay = v * 1e-6
        if lift.start_delay < lift.min_delay:
            lift.start_delay = lift.min_delay
        print(f"  최고속 간격 = {v:.0f}us")
    elif c == 'A':
        lift.accel = num() * 1e-6
        print(f"  가속 = {num():.0f}us/스텝 (가속구간 {lift.ramp_steps()}스텝)")
    elif c == 'C':
        lift.start_delay = max(num(2800), lift.min_delay * 1e6) * 1e-6
        print(f"  출발 간격 = {lift.start_delay*1e6:.0f}us")
    elif c == 'M':
        lift.full_step = (arg not in ('', '0'))
        if lift.full_step and lift.phase % 2 == 0:
            lift.phase = (lift.phase + 1) % 8
        print(f"  모드 = {'풀스텝' if lift.full_step else '하프스텝'}")
    elif c == 'H':
        lift.hold = (arg not in ('', '0'))
        if not lift.hold:
            lift.coils_off()
        print(f"  코일유지 = {'ON (발열 주의)' if lift.hold else 'OFF'}")
    elif c == 'L':
        sub = arg[:1].upper()
        if arg == '':
            lift.limit_on = False
            print("  리밋 해제")
        elif sub == 'H':
            lift.limit_min = lift.position
            print(f"  하한 = {lift.limit_min}")
            if lift.limit_max > lift.limit_min:
                lift.limit_on = True
            else:
                print("  이제 위로 올려서 맨 위에서 LT")
        elif sub == 'T':
            if lift.position <= lift.limit_min:
                print("  ! 상한이 하한보다 위여야 해")
            else:
                lift.limit_max = lift.position
                lift.limit_on = True
                print(f"  상한 = {lift.limit_max}  "
                      f"(이동거리 {lift.steps_to_mm(lift.limit_max-lift.limit_min):.1f}mm)")
        elif ':' in arg:
            lo, hi = arg.split(':', 1)
            lo, hi = int(lo), int(hi)
            if hi > lo:
                lift.limit_min, lift.limit_max, lift.limit_on = lo, hi, True
                print(f"  리밋 {lo} ~ {hi}")
            else:
                print("  ! 상한이 하한보다 커야 해")
        else:
            print("  사용법: L0:5000 / LH / LT / L(해제)")
    elif c == 'W':
        lift.save()
    elif c == 'X':
        if lift.load():
            print("  " + lift.status())
    elif c == '?':
        print("  " + lift.status())
    elif c == '!':
        if lift.last_actual:
            err = (lift.last_actual - lift.last_expected) / lift.last_expected * 100
            print(f"  [타이밍] 예상 {lift.last_expected:.3f}초  "
                  f"실제 {lift.last_actual:.3f}초  오차 {err:+.1f}%")
            if err > 10:
                print("  ! 리눅스 스케줄링 때문에 밀리고 있어. 속도를 낮추는 게 좋아")
        else:
            print("  아직 이동한 적 없음")
    else:
        print(f"  ? 모르는 명령: {cmd}")


def self_test(lift):
    """자동 확인: 코일 순서대로 켜보기 -> 한 바퀴 왕복"""
    print("=" * 52)
    print(" 1단계: 코일 하나씩 켜기 (ULN2003 LED 4개를 봐줘)")
    print("=" * 52)
    for i, p in enumerate(lift.pins):
        print(f"  IN{i+1} (GPIO{p}) 켬 ... ", end='', flush=True)
        lgpio.gpio_write(lift.h, p, 1)
        time.sleep(1.0)
        lgpio.gpio_write(lift.h, p, 0)
        print("끔")
        time.sleep(0.3)

    print()
    print("=" * 52)
    print(" 2단계: 한 바퀴 정방향 -> 한 바퀴 역방향")
    print("=" * 52)
    for i in (1, 2):
        print(f"  {i}회차 정방향 ... ", end='', flush=True)
        t = lift.move_to(lift.position + STEPS_PER_REV)
        print(f"{t:.2f}초")
        time.sleep(1.0)
        print(f"  {i}회차 역방향 ... ", end='', flush=True)
        t = lift.move_to(lift.position - STEPS_PER_REV)
        print(f"{t:.2f}초")
        time.sleep(1.0)

    print()
    run_command(lift, '!')
    print("  " + lift.status())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pins', required=True,
                    help='IN1,IN2,IN3,IN4 의 BCM 번호. 예) 17,18,27,22')
    ap.add_argument('--chip', type=int, default=None,
                    help='gpiochip 번호. 안 주면 자동으로 찾음')
    ap.add_argument('--speed', type=int, default=600, help='최고속 간격 us (기본 600)')
    ap.add_argument('--test', action='store_true', help='자동 테스트만 하고 종료')
    args = ap.parse_args()

    pins = [int(x) for x in args.pins.split(',')]
    if len(pins) != 4:
        sys.exit("핀은 4개여야 해. 예) --pins 17,18,27,22")

    chip = find_chip(pins, args.chip)
    if args.chip is None:
        print(f"gpiochip{chip} 자동 선택")
    lift = Lift(pins, chip)
    restored = lift.load(quiet=True)
    if restored:
        print("저장된 설정을 불러왔어")
    # --speed 를 직접 준 경우엔 저장본보다 우선
    if args.speed != 600 or not restored:
        lift.min_delay = args.speed * 1e-6
    print(f"GPIO {pins} 로 열었어 (gpiochip{chip})")
    print("  " + lift.status())
    print()

    try:
        if args.test:
            self_test(lift)
        else:
            print("명령: U<mm> D<mm> P<mm> K<mm> R0/R1 | F B T G Z V A C M0/M1 L LH LT W X H0/H1 ? !")
            print("주의: 전원을 껐다 켜면 지금 높이를 모름. 맨아래 맞추고 Z 부터!")
            print("종료: q\n")
            while True:
                try:
                    cmd = input('리프트> ').strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if cmd.lower() in ('q', 'quit', 'exit'):
                    break
                run_command(lift, cmd)
    finally:
        lift.close()
        print("코일 끄고 정리 완료.")


if __name__ == '__main__':
    main()
