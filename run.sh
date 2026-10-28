#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
venv=${TIDE_CONFIGURATOR_VENV:-$here/.venv}
dist=${TIDE_FRONTEND_DIST:-$here/frontend/dist}
[[ -x "$venv/bin/python" ]] || { echo "Python environment is missing: $venv" >&2; exit 1; }
[[ -f "$dist/index.html" ]] || { echo "Frontend build is missing: $dist/index.html" >&2; exit 1; }
export PYTHONPATH="$here/backend:$here/calibration${PYTHONPATH:+:$PYTHONPATH}"
exec "$venv/bin/python" -m uvicorn tide_configurator.app:app --host 127.0.0.1 --port "${TIDE_PORT:-8765}" "$@"
