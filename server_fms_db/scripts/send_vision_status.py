#!/usr/bin/env python3
"""Send one local development global_vision_status UDP packet to the FMS."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

# ``python scripts/send_vision_status.py`` puts scripts/ on sys.path, not the
# project root. Add it explicitly so this tool works exactly as documented.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.schemas.vision import VisionStatus


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one mock AI Perception global vision status packet over UDP."
    )
    parser.add_argument("--host", default="127.0.0.1", help="FMS UDP receiver host")
    parser.add_argument("--port", type=int, default=20050, help="FMS UDP receiver port")
    parser.add_argument("--camera-id", default="global_camera", help="Camera identifier")
    parser.add_argument("--status", choices=[status.value for status in VisionStatus], default="ACTIVE")
    parser.add_argument("--frame-seq", type=int, default=123)
    parser.add_argument("--last-frame-age-sec", type=float, default=0.032)
    parser.add_argument("--inference-error-count", type=int, default=0)
    parser.add_argument("--last-error", default=None)
    parser.add_argument("--status-stamp-sec", type=int, default=0)
    parser.add_argument("--status-stamp-nanosec", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = {
        "schema_version": "0.1",
        "message_type": "global_vision_status",
        "camera_id": args.camera_id,
        "status": args.status,
        "frame_seq": args.frame_seq,
        "last_frame_age_sec": args.last_frame_age_sec,
        "inference_error_count": args.inference_error_count,
        "last_error": args.last_error,
        "status_stamp_sec": args.status_stamp_sec,
        "status_stamp_nanosec": args.status_stamp_nanosec,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.sendto(encoded, (args.host, args.port))
    print(f"Sent global_vision_status to {args.host}:{args.port} ({len(encoded)} bytes)")


if __name__ == "__main__":
    main()
