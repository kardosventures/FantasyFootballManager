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
runtime_pass=$(/usr/bin/osascript \
  -e 'with timeout of 900 seconds' \
  -e 'tell application "Finder"' \
  -e 'activate' \
  -e 'set dialogResult to display dialog "Unlock the encrypted Fantasy Football live runtime." default answer "" with hidden answer buttons {"Cancel", "Unlock"} default button "Unlock" with title "Fantasy Football Runtime"' \
  -e 'end tell' \
  -e 'end timeout' \
  -e 'return text returned of dialogResult')
[[ -n "$runtime_pass" ]] || {
  print -u2 "Runtime password cannot be empty."
  exit 1
}
printf '%s\n' "$runtime_pass" | hdiutil attach \
  -stdinpass \
  -nobrowse \
  -mountpoint "$RUNTIME_MOUNT" \
  "$RUNTIME_BUNDLE"
unset runtime_pass
ln -s "$RUNTIME_MOUNT" "$RUNTIME_LINK"
print "Runtime mounted at $RUNTIME_MOUNT and linked from $RUNTIME_LINK"
