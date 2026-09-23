#!/bin/zsh
set -euo pipefail
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
TARGET="$HOME/Library/LaunchAgents"
RUNTIME_DIR="$HOME/Library/Application Support/JimAiFantasy"
mkdir -p "$TARGET" "$RUNTIME_DIR" "$RUNTIME_DIR/codex-decisions"
mkdir -p "$RUNTIME_DIR/scripts"
chmod 700 "$RUNTIME_DIR/codex-decisions"
ditto "$PROJECT_DIR/browser-agent" "$RUNTIME_DIR/browser-agent"
ditto "$PROJECT_DIR/codex-agent" "$RUNTIME_DIR/codex-agent"
cp "$PROJECT_DIR/.env" "$RUNTIME_DIR/.env"
chmod 600 "$PROJECT_DIR/.env" "$RUNTIME_DIR/.env"
cp "$PROJECT_DIR/scripts/operational-health.sh" "$RUNTIME_DIR/scripts/operational-health.sh"
cp "$PROJECT_DIR/scripts/codex-watchdog.sh" "$RUNTIME_DIR/scripts/codex-watchdog.sh"
chmod 700 "$RUNTIME_DIR/scripts/operational-health.sh" "$RUNTIME_DIR/scripts/codex-watchdog.sh"
if [[ -d "$PROJECT_DIR/.browser-profile" && ! -d "$RUNTIME_DIR/browser-profile" ]]; then
  ditto "$PROJECT_DIR/.browser-profile" "$RUNTIME_DIR/browser-profile"
fi
cp "$PROJECT_DIR/infra/launchd/com.jimai.fantasy-browser-agent.plist" "$TARGET/"
cp "$PROJECT_DIR/infra/launchd/com.jimai.fantasy-codex-agent.plist" "$TARGET/"
cp "$PROJECT_DIR/infra/launchd/com.jimai.fantasy-codex-watchdog.plist" "$TARGET/"
cp "$PROJECT_DIR/infra/launchd/com.jimai.fantasy-health.plist" "$TARGET/"
launchctl bootout "gui/$UID/com.jimai.fantasy-stack" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-mac-agent" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-browser-agent" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-codex-agent" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-codex-watchdog" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-backup" 2>/dev/null || true
launchctl bootout "gui/$UID/com.jimai.fantasy-health" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID" "$TARGET/com.jimai.fantasy-browser-agent.plist"
launchctl bootstrap "gui/$UID" "$TARGET/com.jimai.fantasy-codex-agent.plist"
launchctl bootstrap "gui/$UID" "$TARGET/com.jimai.fantasy-codex-watchdog.plist"
launchctl bootstrap "gui/$UID" "$TARGET/com.jimai.fantasy-health.plist"
print "Installed and started the Playwright browser agent, ChatGPT-authenticated Codex worker, and worker watchdog."
print "Docker containers use restart policies and start when Docker Desktop starts."
