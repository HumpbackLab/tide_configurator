#!/bin/sh
set -eu
archive=$1
target=$2
stage=$3
format=$4
name=${target##*/}
extract=$stage/extracted
backup=$target.backup-${stage##*-}
mkdir "$extract"
trap 'rm -rf "$stage"' EXIT HUP INT TERM
if [ "$format" = gz ]; then
  tar -xzf "$archive" -C "$extract"
else
  tar -xf "$archive" -C "$extract"
fi
candidate=$extract/$name
[ -x "$candidate/run.sh" ] && [ -x "$candidate/bin/schurvins_stereo" ] &&
  [ -f "$candidate/schurvins.yaml" ] && [ -f "$candidate/SHA256SUMS" ] || {
    echo 'SchurVINS package is incomplete' >&2; exit 1;
  }
(cd "$candidate" && sha256sum -c SHA256SUMS >/dev/null)
chown -R root:root "$candidate"
# Preserve the board's calibration, including migration from the old micro-ROS package.
calibration_source=$target
if [ ! -d "$calibration_source" ] && [ "$name" = schurvins-gcc11-native-dds ]; then
  calibration_source=/mnt/data/schurvins-gcc11-static-micro-ros
fi
if [ -d "$calibration_source" ]; then
  for config in schurvins.yaml tidesense.yaml; do
    if [ -f "$calibration_source/$config" ] && [ ! -L "$calibration_source/$config" ]; then
      cp -p "$calibration_source/$config" "$candidate/$config"
    fi
  done
fi
# A preserved calibration changes schurvins.yaml; keep the installed manifest valid.
(cd "$candidate" &&
  awk '$2 != "schurvins.yaml" {print}' SHA256SUMS >SHA256SUMS.new &&
  sha256sum schurvins.yaml >>SHA256SUMS.new &&
  mv SHA256SUMS.new SHA256SUMS &&
  sha256sum -c SHA256SUMS >/dev/null)
"$candidate/run.sh" --help >/dev/null
if [ -e "$target" ]; then mv "$target" "$backup"; else backup=; fi
if ! mv "$candidate" "$target"; then
  if [ -n "$backup" ]; then mv "$backup" "$target"; fi
  echo 'SchurVINS installation failed; previous version restored' >&2
  exit 1
fi
printf 'installed %s\n' "$target"
[ -z "$backup" ] || printf 'backup %s\n' "$backup"
