"""2호기 전용 경로와 포트 설정.

이 폴더의 제어 스크립트만 이 값을 사용한다. 1호기 설정 파일이나
다운로드 원본 키트는 건드리지 않는다.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# USB 물리 포트 기준 별칭이다. ttyUSB 번호가 재삽입 때 바뀌는 문제를 피한다.
DEFAULT_PORT = os.environ.get(
    "ZKBOT2_PORT",
    "/dev/serial/by-path/pci-0000:00:14.0-usb-0:7:1.0-port0",
)
