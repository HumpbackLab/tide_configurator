#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
venv=${TIDE_CONFIGURATOR_VENV:-$here/.venv}
dist=${TIDE_FRONTEND_DIST:-$here/frontend/dist}
[[ -x "$venv/bin/python" ]] || { echo "Python environment is missing: $venv" >&2; exit 1; }
[[ -f "$dist/index.html" ]] || { echo "Frontend build is missing: $dist/index.html" >&2; exit 1; }
set +u
if source "$here/../scripts/ros2-setup.sh" && tidesense_source_ros2; then
  cyclone="$here/../install/micro-ros/host-cyclone/opt/ros/$ROS_DISTRO"
  if [[ -f "$cyclone/lib/librmw_cyclonedds_cpp.so" ]]; then
    export AMENT_PREFIX_PATH="$cyclone${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}"
    export LD_LIBRARY_PATH="$cyclone/lib:$cyclone/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
  if [[ -f "/opt/ros/$ROS_DISTRO/lib/librmw_cyclonedds_cpp.so" || -f "$cyclone/lib/librmw_cyclonedds_cpp.so" ]]; then
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  fi
  case "${CYCLONEDDS_URI:-}" in
    */micro_ros/calibration-cyclone.xml) unset CYCLONEDDS_URI ;;
  esac
else
  unset ROS_VERSION
  echo 'ROS 2 unavailable: VIO trajectory view needs a ROS 2 environment.' >&2
fi
set -u
export PYTHONPATH="$here/backend:$here/calibration${PYTHONPATH:+:$PYTHONPATH}"
exec "$venv/bin/python" -m uvicorn tide_configurator.app:app --host 127.0.0.1 --port "${TIDE_PORT:-8765}" "$@"
