#!/bin/zsh
set -euo pipefail

SSD_VOLUME=${FANTASY_SSD_VOLUME:-/Volumes/Extreme SSD}
DOCKER_DATA_DIR=${FANTASY_DOCKER_DATA_DIR:-$SSD_VOLUME/DockerDesktop}
DOCKER_RAW="$DOCKER_DATA_DIR/Docker.raw"
RECOVERY_DIR=${FANTASY_RECOVERY_DIR:-$SSD_VOLUME/FantasyFootballRecovery/docker}
SAVED_SETTINGS=${FANTASY_DOCKER_SETTINGS_BACKUP:-$RECOVERY_DIR/settings-store.json}
SETTINGS_DIR="$HOME/Library/Group Containers/group.com.docker"
SETTINGS_FILE="$SETTINGS_DIR/settings-store.json"
DOCKER_APP=${DOCKER_APP:-/Applications/Docker.app}
DOCKER_BIN=${DOCKER_BIN:-$DOCKER_APP/Contents/Resources/bin/docker}

usage() {
  print "Usage: $0 [--check|--apply]"
  print ""
  print "  --check  Verify the SSD Docker data and current Desktop setting (default)."
  print "  --apply  Stop Docker Desktop, restore the SSD data-folder setting, and start Docker."
}

mode=${1:---check}
case "$mode" in
  --check|--apply) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac

[[ "$(uname -s)" == Darwin ]] || {
  print -u2 "This recovery script supports macOS only."
  exit 1
}
[[ -d "$SSD_VOLUME" ]] || {
  print -u2 "SSD is not mounted at $SSD_VOLUME"
  exit 1
}
[[ -f "$DOCKER_RAW" ]] || {
  print -u2 "Docker data disk is missing: $DOCKER_RAW"
  exit 1
}
[[ -d "$DOCKER_APP" ]] || {
  print -u2 "Install Docker Desktop at $DOCKER_APP before running this script."
  exit 1
}
[[ -f "$SAVED_SETTINGS" ]] || {
  print -u2 "Saved Docker settings are missing: $SAVED_SETTINGS"
  exit 1
}

saved_data_folder=$(plutil -extract DataFolder raw -o - "$SAVED_SETTINGS")
[[ "$saved_data_folder" == "$DOCKER_DATA_DIR" ]] || {
  print -u2 "Saved settings point to '$saved_data_folder', not '$DOCKER_DATA_DIR'."
  exit 1
}

print "SSD Docker data: $DOCKER_RAW"
print "Saved Docker setting: $SAVED_SETTINGS"
print "Requested data folder: $DOCKER_DATA_DIR"

if [[ -f "$SETTINGS_FILE" ]]; then
  current_data_folder=$(plutil -extract DataFolder raw -o - "$SETTINGS_FILE" 2>/dev/null || true)
  print "Current Docker data folder: ${current_data_folder:-not configured}"
else
  current_data_folder=
  print "Current Docker data folder: not configured"
fi

if [[ "$mode" == --check ]]; then
  [[ "$current_data_folder" == "$DOCKER_DATA_DIR" ]] || {
    print -u2 "Docker Desktop is not configured for the SSD. Run: $0 --apply"
    exit 2
  }
  print "Docker Desktop is configured to use the existing SSD data disk."
  exit 0
fi

print "Stopping Docker Desktop before changing its data-folder setting..."
if [[ -x "$DOCKER_BIN" ]]; then
  "$DOCKER_BIN" desktop stop >/dev/null 2>&1 || true
fi
/usr/bin/osascript -e 'tell application "Docker" to quit' >/dev/null 2>&1 || true

for _ in {1..60}; do
  pgrep -f '/Applications/Docker.app/Contents/MacOS/com.docker.backend' >/dev/null 2>&1 || break
  sleep 1
done
if pgrep -f '/Applications/Docker.app/Contents/MacOS/com.docker.backend' >/dev/null 2>&1; then
  print -u2 "Docker Desktop did not stop. Quit it manually and rerun this script."
  exit 1
fi

mkdir -p "$SETTINGS_DIR"
if [[ -f "$SETTINGS_FILE" ]]; then
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  cp -p "$SETTINGS_FILE" "$SETTINGS_FILE.pre-rehydrate-$timestamp"
  plutil -replace DataFolder -string "$DOCKER_DATA_DIR" "$SETTINGS_FILE"
else
  cp -p "$SAVED_SETTINGS" "$SETTINGS_FILE"
  plutil -replace DataFolder -string "$DOCKER_DATA_DIR" "$SETTINGS_FILE"
fi
chmod 600 "$SETTINGS_FILE"

configured_data_folder=$(plutil -extract DataFolder raw -o - "$SETTINGS_FILE")
[[ "$configured_data_folder" == "$DOCKER_DATA_DIR" ]] || {
  print -u2 "Failed to configure Docker Desktop for the SSD."
  exit 1
}

print "Starting Docker Desktop with the existing SSD data disk..."
open "$DOCKER_APP"
for _ in {1..180}; do
  if "$DOCKER_BIN" info >/dev/null 2>&1; then
    print "Docker Desktop is running from the SSD data folder."
    "$DOCKER_BIN" volume ls
    exit 0
  fi
  sleep 1
done

print -u2 "Docker Desktop did not become ready within three minutes."
print -u2 "Complete any first-launch prompts, then rerun: $0 --check"
exit 1
