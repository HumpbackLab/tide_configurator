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
if [ -f "$pidfile" ]; then
  read -r pid <"$pidfile" || true
  case "${pid:-}" in *[!0-9]*|'') rm -f "$pidfile" ;; *)
    if matches "$pid"; then printf 'running %s\n' "$pid"; exit 0; fi
    rm -f "$pidfile"
  ;; esac
fi
for process in /proc/[0-9]*; do
  pid=${process##*/}
  if matches "$pid"; then printf 'external %s\n' "$pid"; exit 0; fi
done
restore_console
printf 'stopped\n'
