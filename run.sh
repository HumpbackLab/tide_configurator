#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -x "$here/.venv/bin/python" ]] || { echo 'Create tide_configurator/.venv and install requirements.txt first.' >&2; exit 1; }
[[ -f "$here/frontend/dist/index.html" ]] || { echo 'Build frontend first: cd tide_configurator/frontend && npm ci && npm run build' >&2; exit 1; }
export PYTHONPATH="$here/backend:$here/calibration${PYTHONPATH:+:$PYTHONPATH}"
exec "$here/.venv/bin/python" -m uvicorn tide_configurator.app:app --host 127.0.0.1 --port "${TIDE_PORT:-8765}" "$@"
