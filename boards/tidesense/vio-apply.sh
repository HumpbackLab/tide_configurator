#!/bin/sh
set -eu
target=$1
staged=$2
expected=$3
directory=${target%/*}
name=${target##*/}
manifest=$directory/SHA256SUMS
manifest_tmp=$directory/.SHA256SUMS.$$.tmp
backup=$target.backup-$(date +%Y%m%d%H%M%S)-$$
[ "$name" = schurvins.yaml ] && [ "${staged%/*}" = "$directory" ] || {
  echo 'Unexpected VIO configuration path' >&2; exit 1;
}
[ -f "$target" ] && [ ! -L "$target" ] &&
  [ -f "$staged" ] && [ ! -L "$staged" ] &&
  [ -f "$manifest" ] && [ ! -L "$manifest" ] || {
    echo 'VIO installation or uploaded configuration is missing' >&2; exit 1;
  }
actual=$(sha256sum "$staged")
[ "${actual%% *}" = "$expected" ] || { echo 'Uploaded configuration checksum mismatch' >&2; exit 1; }
umask 077
cp -p "$target" "$backup"
rollback=1
cleanup() {
  if [ "$rollback" = 1 ]; then cp -p "$backup" "$target"; fi
  rm -f "$staged" "$manifest_tmp"
}
trap cleanup EXIT HUP INT TERM
chmod 644 "$staged"
mv "$staged" "$target"
(
  cd "$directory"
  awk -v name="$name" '$2 != name {print}' SHA256SUMS
  sha256sum "$name"
) >"$manifest_tmp"
(cd "$directory" && sha256sum -c "${manifest_tmp##*/}" >/dev/null)
mv "$manifest_tmp" "$manifest"
rollback=0
trap - EXIT HUP INT TERM
printf 'applied %s\nbackup %s\n' "$target" "$backup"
