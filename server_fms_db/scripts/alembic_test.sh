#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${POSTGRES_TEST_DATABASE_URL:-}" ]]; then
  echo "POSTGRES_TEST_DATABASE_URL is required." >&2
  exit 2
fi

export ALEMBIC_ENV=test
export ALEMBIC_DATABASE_URL="$POSTGRES_TEST_DATABASE_URL"

exec "$(dirname "$0")/../.venv/bin/alembic" "$@"
