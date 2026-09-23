#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
FANTASY_ROOT=${FANTASY_ROOT:-${PROJECT_DIR:h}}
RUNTIME_DIR=${FANTASY_RUNTIME_HOST_DIR:-$FANTASY_ROOT/runtime}

[[ "$PROJECT_DIR" == "$FANTASY_ROOT/source" ]] || {
  print -u2 "Expected the checkout at $FANTASY_ROOT/source, found $PROJECT_DIR"
  exit 1
}

"$SCRIPT_DIR/mount-runtime.sh"
[[ -f "$RUNTIME_DIR/.env" ]] || {
  print -u2 "Runtime environment is missing: $RUNTIME_DIR/.env"
  exit 1
}

if [[ -e "$PROJECT_DIR/.env" || -L "$PROJECT_DIR/.env" ]]; then
  [[ "$PROJECT_DIR/.env" -ef "$RUNTIME_DIR/.env" ]] || {
    print -u2 "$PROJECT_DIR/.env does not point to the encrypted runtime environment."
    exit 1
  }
else
  ln -s ../runtime/.env "$PROJECT_DIR/.env"
fi

"$SCRIPT_DIR/rehydrate-docker.sh" --apply

print "Starting the Fantasy Football stack from the SSD checkout..."
docker compose \
  -f "$PROJECT_DIR/docker-compose.yml" \
  --env-file "$RUNTIME_DIR/.env" \
  up -d --force-recreate

print "Installing SSD-root host agents..."
FANTASY_RUNTIME_HOST_DIR="$RUNTIME_DIR" "$SCRIPT_DIR/install-launchd.sh"

print "Running recovery health checks..."
"$SCRIPT_DIR/doctor.sh"
print "Fantasy Football Manager rehydration completed."
