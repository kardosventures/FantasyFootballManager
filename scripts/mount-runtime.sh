#!/bin/zsh
set -euo pipefail

SSD_VOLUME=${FANTASY_SSD_VOLUME:-/Volumes/Extreme SSD}
FANTASY_ROOT=${FANTASY_ROOT:-$SSD_VOLUME/FantasyFootballManager}
RUNTIME_BUNDLE=${FANTASY_RUNTIME_BUNDLE:-$FANTASY_ROOT/.runtime.sparsebundle}
RUNTIME_MOUNT=${FANTASY_RUNTIME_HOST_DIR:-$FANTASY_ROOT/runtime}

[[ -d "$SSD_VOLUME" ]] || {
  print -u2 "SSD is not mounted at $SSD_VOLUME"
  exit 1
}
[[ -d "$RUNTIME_BUNDLE" ]] || {
  print -u2 "Encrypted runtime image is missing: $RUNTIME_BUNDLE"
  exit 1
}

if mount | grep -F " on $RUNTIME_MOUNT " >/dev/null 2>&1; then
  print "Runtime is already mounted at $RUNTIME_MOUNT"
  exit 0
fi

mkdir -p "$RUNTIME_MOUNT"
print "Unlocking the encrypted Fantasy Football runtime..."
hdiutil attach -nobrowse -mountpoint "$RUNTIME_MOUNT" "$RUNTIME_BUNDLE"
print "Runtime mounted at $RUNTIME_MOUNT"
