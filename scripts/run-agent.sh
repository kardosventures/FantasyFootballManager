#!/bin/zsh
set -euo pipefail
PROJECT_DIR="/Users/jimkardos/Documents/ChatGPT/Fantasy Football manager"
[[ -f "$PROJECT_DIR/.env" ]] && set -a && source "$PROJECT_DIR/.env" && set +a
cd "$PROJECT_DIR/browser-agent"
exec /usr/bin/caffeinate -s /usr/local/bin/corepack pnpm start
