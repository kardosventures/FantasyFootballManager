#!/bin/zsh
set -euo pipefail
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${FANTASY_PROJECT_DIR:-${SCRIPT_DIR:h}}
[[ -f "$PROJECT_DIR/.env" ]] && set -a && source "$PROJECT_DIR/.env" && set +a
AGENT_DIR=${FANTASY_BROWSER_AGENT_DIR:-$PROJECT_DIR/browser-agent}
cd "$AGENT_DIR"
exec /usr/bin/caffeinate -s /usr/local/bin/corepack pnpm start
