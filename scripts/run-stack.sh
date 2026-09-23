#!/bin/zsh
set -euo pipefail
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${FANTASY_PROJECT_DIR:-${SCRIPT_DIR:h}}
cd "$PROJECT_DIR"
exec /usr/bin/caffeinate -s /Applications/Docker.app/Contents/Resources/bin/docker compose up
