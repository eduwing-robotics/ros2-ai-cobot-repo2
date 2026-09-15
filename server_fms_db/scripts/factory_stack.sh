#!/usr/bin/env bash
# Thin wrapper keeps the launcher usable from any working directory.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../.venv/bin/python" -m scripts.factory_stack "$@"
