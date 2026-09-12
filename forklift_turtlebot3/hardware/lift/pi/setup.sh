#!/usr/bin/env bash
# 새 라즈베리파이에서 포크리프트 모터 제어 준비하기
# 사용법:  bash setup.sh

set -u

echo "=================================================="
echo " 포크리프트 스테퍼 — 파이 셋업"
echo "=================================================="
echo

# ── 1. 이 파이가 뭔지 ──
echo "[1/5] 파이 정보"
if [ -f /proc/device-tree/model ]; then
    echo "  모델: $(tr -d '\0' < /proc/device-tree/model)"
else
    echo "  모델: 확인 불가 (라즈베리파이가 아닐 수 있음)"
fi
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "  OS  : ${PRETTY_NAME:-?}"
fi
echo "  계정: $(whoami)"
echo

# ── 2. lgpio 설치 ──
echo "[2/5] lgpio 확인"
if python3 -c 'import lgpio' 2>/dev/null; then
    echo "  이미 설치돼 있음"
else
    echo "  설치를 시작할게 (sudo 비밀번호가 필요할 수 있어)"
    if sudo apt-get update -qq && sudo apt-get install -y python3-lgpio; then
        echo "  설치 완료"
    else
        echo "  ! apt 설치 실패. 이렇게 해볼 것:"
        echo "      pip3 install lgpio --break-system-packages"
        exit 1
    fi
fi
python3 -c 'import lgpio; print("  import 확인 OK")' || exit 1
echo

# ── 3. GPIO 권한 ──
echo "[3/5] GPIO 권한"
ls -l /dev/gpiochip* 2>/dev/null | sed 's/^/  /'
echo "  내 그룹: $(id -nG)"
if id -nG | tr ' ' '\n' | grep -qxE 'dialout|gpio'; then
    echo "  dialout/gpio 그룹에 속해 있음 → sudo 없이 될 가능성 높음"
else
    echo "  ! dialout 그룹에 없음. 안 되면 아래를 실행하고 재로그인:"
    echo "      sudo usermod -aG dialout $(whoami)"
fi
echo

# ── 4. 어떤 gpiochip 을 쓸 수 있나 ──
echo "[4/5] gpiochip 확인 (핀 6,13,19,26 기준)"
python3 - <<'PYEOF'
import lgpio
pins = [6, 13, 19, 26]
found = []
for c in range(6):
    try:
        h = lgpio.gpiochip_open(c)
    except Exception:
        continue
    try:
        for p in pins:
            lgpio.gpio_claim_output(h, p, 0)
        for p in pins:
            lgpio.gpio_free(h, p)
        found.append(c)
    except Exception:
        pass
    finally:
        try:
            lgpio.gpiochip_close(h)
        except Exception:
            pass
if found:
    print(f"  쓸 수 있는 gpiochip: {found}  (프로그램이 자동으로 고름)")
else:
    print("  ! 쓸 수 있는 gpiochip 을 못 찾음")
    print("    권한 문제이거나 다른 프로그램이 핀을 쓰는 중일 수 있어")
PYEOF
echo

# ── 5. 안내 ──
echo "[5/5] 준비 끝"
echo
echo "  배선 확인 (모터 안 돌리고 LED만):"
echo "     python3 forklift_pi.py --pins 26,19,13,6 --test"
echo
echo "  모터 돌리기:"
echo "     python3 forklift_pi.py --pins 26,19,13,6"
echo
echo "  핀을 다르게 꽂았으면 --pins 뒤 숫자를 바꾸면 돼 (BCM 번호)"
echo "=================================================="
