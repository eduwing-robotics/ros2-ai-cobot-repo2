#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../.venv/bin/python" "$SCRIPT_DIR/setup_pre_roof_rehearsal_db.py" "$@"
