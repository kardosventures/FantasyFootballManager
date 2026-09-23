#!/bin/zsh
set -euo pipefail
PROJECT_DIR="/Users/jimkardos/Documents/ChatGPT/Fantasy Football manager"
cd "$PROJECT_DIR"
exec /usr/bin/caffeinate -s /Applications/Docker.app/Contents/Resources/bin/docker compose up
