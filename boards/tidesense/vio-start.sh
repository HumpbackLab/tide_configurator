#!/bin/sh
set -eu
run=$1
config=$2
output=$3
camera_settings=$4
binary=${run%/*}/bin/schurvins_stereo
pidfile=/run/tide-configurator-vio.pid
console_state=/run/tide-configurator-vio.console
restore_console() {
  [ -f "$console_state" ] || return 0
  read -r token previous <"$console_state" || true
  current=$(awk 'NR == 1 {print $1}' /proc/sys/kernel/printk)
  if [ "$current" = 3 ]; then printf '%s\n' "$previous" >/proc/sys/kernel/printk; fi
  rm -f "$console_state"
}
[ -x "$run" ] || { echo "SchurVINS is not installed: $run" >&2; exit 1; }
[ -f "$config" ] || { echo "VIO calibration is missing: $config" >&2; exit 1; }
if [ -f "$camera_settings" ] && ! "$run" --help 2>&1 | grep -q TIDE_CAM_CONFIG_PATH; then
  echo 'SchurVINS is too old to read camera settings; deploy an updated package' >&2
  exit 1
fi
for process in /proc/[0-9]*; do
  pid=${process##*/}
  [ -r "$process/cmdline" ] || continue
  command_line=$(tr '\000' ' ' <"$process/cmdline")
  case "$command_line" in *"$binary --vio "*) echo "VIO is already running: $pid" >&2; exit 1 ;; esac
done
restore_console
mkdir -p "$output"
# The board boots with all ISP warnings mirrored to a 115200 baud serial console.
# That synchronous output delays SC7U22 SPI reads beyond their 5 ms deadline.
# Keep messages in dmesg while VIO runs, then restore the console setting.
previous=$(awk 'NR == 1 {print $1}' /proc/sys/kernel/printk)
if [ "$previous" -gt 3 ]; then
  umask 077
  token=$$
  printf '%s %s\n' "$token" "$previous" >"$console_state"
  if ! printf '3\n' >/proc/sys/kernel/printk; then
    rm -f "$console_state"
    echo 'Cannot quiet the serial kernel console for VIO' >&2
    exit 1
  fi
fi
SCHUR_MICRO_ROS=0; export SCHUR_MICRO_ROS
TIDE_CAM_CONFIG_PATH=$camera_settings; export TIDE_CAM_CONFIG_PATH
nohup setsid "$run" --vio "$config" auto "$output/poses.csv" 0 >"$output/vio.log" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$pidfile"
sleep 1
if [ ! -r "/proc/$pid/cmdline" ]; then
  rm -f "$pidfile"
  restore_console
  tail -n 15 "$output/vio.log" >&2 || true
  echo 'VIO exited during startup' >&2
  exit 1
fi
command_line=$(tr '\000' ' ' <"/proc/$pid/cmdline")
case "$command_line" in
  *"$binary --vio "*) printf 'running %s\n' "$pid" ;;
  *) rm -f "$pidfile"; restore_console; tail -n 15 "$output/vio.log" >&2 || true; echo 'VIO did not start' >&2; exit 1 ;;
esac
# Also restore the console if VIO exits while the browser is closed.
nohup setsid sh -c '
  pid=$1; binary=$2; state=$3; token=$4
  while [ -r "/proc/$pid/cmdline" ]; do
    command_line=$(tr "\000" " " <"/proc/$pid/cmdline")
    case "$command_line" in *"$binary --vio "*) sleep 1 ;; *) break ;; esac
  done
  if [ -f "$state" ]; then
    read -r active_token previous <"$state" || true
    [ "$active_token" = "$token" ] || exit 0
    current=$(awk "NR == 1 {print \$1}" /proc/sys/kernel/printk)
    if [ "$current" = 3 ]; then printf "%s\n" "$previous" >/proc/sys/kernel/printk; fi
    rm -f "$state"
  fi
' watch "$pid" "$binary" "$console_state" "${token:-}" >/dev/null 2>&1 </dev/null &
