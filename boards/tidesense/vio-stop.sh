#!/bin/sh
set -eu
run=$1
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
matches() {
  [ -r "/proc/$1/cmdline" ] || return 1
  command_line=$(tr '\000' ' ' <"/proc/$1/cmdline")
  case "$command_line" in *"$binary --vio "*) return 0 ;; *) return 1 ;; esac
}
pid=
if [ -f "$pidfile" ]; then
  read -r candidate <"$pidfile" || true
  case "${candidate:-}" in *[!0-9]*|'') ;; *) if matches "$candidate"; then pid=$candidate; fi ;; esac
fi
if [ -z "$pid" ]; then
  for process in /proc/[0-9]*; do
    candidate=${process##*/}
    if matches "$candidate"; then
      [ -z "$pid" ] || { echo 'Multiple VIO processes found; stop them manually' >&2; exit 1; }
      pid=$candidate
    fi
  done
fi
if [ -z "$pid" ]; then rm -f "$pidfile"; restore_console; echo stopped; exit 0; fi
kill -INT "$pid" 2>/dev/null || true
count=0
while matches "$pid" && [ "$count" -lt 100 ]; do sleep 0.1; count=$((count+1)); done
if matches "$pid"; then
  kill -TERM "$pid"
  count=0
  while matches "$pid" && [ "$count" -lt 50 ]; do sleep 0.1; count=$((count+1)); done
fi
if matches "$pid"; then echo "VIO did not stop: $pid" >&2; exit 1; fi
rm -f "$pidfile"
restore_console
echo stopped
