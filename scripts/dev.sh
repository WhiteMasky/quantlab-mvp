#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$ROOT/.tools/node/bin:$PATH"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
MODE="${1:-api}"
if [[ "$MODE" == "api" ]]; then
  exec "$ROOT/.venv/bin/python" -m uvicorn quantlab.service.app:app --host 127.0.0.1 --port 18765
elif [[ "$MODE" == "web" ]]; then
  exec npm --prefix "$ROOT/frontend" run dev
else
  printf 'Usage: bash scripts/dev.sh api|web\n' >&2
  exit 2
fi
