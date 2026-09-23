#!/bin/zsh
set -euo pipefail

SSD_VOLUME=${FANTASY_SSD_VOLUME:-/Volumes/Extreme SSD}
FANTASY_ROOT=${FANTASY_ROOT:-$SSD_VOLUME/FantasyFootballManager}
RUNTIME_BUNDLE=${FANTASY_RUNTIME_BUNDLE:-$FANTASY_ROOT/.runtime.sparsebundle}
RUNTIME_LINK=${FANTASY_RUNTIME_HOST_DIR:-$FANTASY_ROOT/runtime}
RUNTIME_MOUNT=${FANTASY_RUNTIME_VOLUME_MOUNT:-/Volumes/FantasyFootballRuntime}

[[ -d "$SSD_VOLUME" ]] || {
  print -u2 "SSD is not mounted at $SSD_VOLUME"
  exit 1
}
[[ -d "$RUNTIME_BUNDLE" ]] || {
  print -u2 "Encrypted runtime image is missing: $RUNTIME_BUNDLE"
  exit 1
}

if mount | grep -F " on $RUNTIME_MOUNT " >/dev/null 2>&1; then
  if [[ ! -L "$RUNTIME_LINK" ]]; then
    [[ ! -e "$RUNTIME_LINK" ]] || {
      print -u2 "Runtime link path is occupied: $RUNTIME_LINK"
      exit 1
    }
    ln -s "$RUNTIME_MOUNT" "$RUNTIME_LINK"
  fi
  print "Runtime is already mounted at $RUNTIME_MOUNT and linked from $RUNTIME_LINK"
  exit 0
fi

if [[ -L "$RUNTIME_LINK" ]]; then
  rm "$RUNTIME_LINK"
elif [[ -e "$RUNTIME_LINK" ]]; then
  print -u2 "Runtime link path is occupied: $RUNTIME_LINK"
  exit 1
fi
print "Unlocking the encrypted Fantasy Football runtime..."
hdiutil attach -nobrowse -mountpoint "$RUNTIME_MOUNT" "$RUNTIME_BUNDLE"
ln -s "$RUNTIME_MOUNT" "$RUNTIME_LINK"
print "Runtime mounted at $RUNTIME_MOUNT and linked from $RUNTIME_LINK"
