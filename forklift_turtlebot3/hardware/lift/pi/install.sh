#!/usr/bin/env bash
# 파이에 설치 — 이 폴더의 pi/ 안에서 실행:  bash install.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/forklift"

echo "[1/3] 파일 복사 → $DEST"
mkdir -p "$DEST"
for src in fk_jogd.py forklift_pi.py jogd_start jogd_stop setup.sh; do
  if [ "$HERE/$src" != "$DEST/$src" ]; then
    cp -v "$HERE/$src" "$DEST/$src"
  fi
done
chmod +x "$DEST"/jogd_start "$DEST"/jogd_stop "$DEST"/setup.sh

echo
echo "[2/3] lgpio 확인"
if python3 -c 'import lgpio' 2>/dev/null; then
  echo "  이미 설치돼 있음"
else
  sudo apt-get update -qq && sudo apt-get install -y python3-lgpio \
    || { echo "  ! 실패. pip3 install lgpio --break-system-packages 로 시도"; exit 1; }
fi

echo
echo "[3/3] GPIO 권한"
ls -l /dev/gpiochip0
if getfacl /dev/gpiochip0 2>/dev/null | grep -q "user:$(whoami):rw"; then
  echo "  ACL 로 열려 있음 — sudo 없이 동작"
elif id -nG | tr ' ' '\n' | grep -qx dialout; then
  echo "  dialout 그룹 소속 — 동작할 것"
else
  echo "  ! 권한 없음. 아래 실행 후 재로그인:"
  echo "      sudo usermod -aG dialout $(whoami)"
fi

echo
echo "설치 끝. 실행:"
echo "   ~/forklift/jogd_start          # 리프트 데몬"
if [ -f "$HOME/.forklift_pi.json" ]; then
  echo "기존 리프트 캘리브레이션을 유지했습니다: $HOME/.forklift_pi.json"
else
  echo "주의: 아직 리프트 캘리브레이션이 없습니다. PC의 fk_key.py로 J2/J3와 리밋을 설정하세요."
fi
