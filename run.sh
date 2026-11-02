#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$here/backend:$here/calibration${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m uvicorn tide_configurator.app:app --host 127.0.0.1 --port "${TIDE_PORT:-8765}" "$@"
