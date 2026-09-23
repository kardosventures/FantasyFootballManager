#!/bin/zsh
set -u

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
ENV_FILE="$PROJECT_DIR/.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

failures=0
check() {
  local label=$1
  shift
  if "$@" >/dev/null 2>&1; then
    print "PASS  $label"
  else
    print "FAIL  $label"
    failures=$((failures + 1))
  fi
}

runtime_free_gb() {
  local free_kb
  local storage_path=${FANTASY_STORAGE_HEALTH_PATH:-${FANTASY_ROOT:-$PROJECT_DIR}}
  free_kb=$(df -Pk "$storage_path" | awk 'NR==2 {print $4}')
  [[ $((free_kb / 1024 / 1024)) -ge ${MIN_FREE_DISK_GB:-40} ]]
}

private_dashboard_bind() {
  python3 - "${DASHBOARD_BIND_ADDRESS:-127.0.0.1}" <<'PY'
import ipaddress, sys
address = ipaddress.ip_address(sys.argv[1])
raise SystemExit(0 if address.is_private or address.is_loopback else 1)
PY
}

browser_ready() {
  local runtime_dir=${FANTASY_RUNTIME_HOST_DIR:-$HOME/Library/Application Support/JimAiFantasy}
  [[ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]
  [[ -d "$PROJECT_DIR/browser-agent/node_modules/playwright-core" || -d "$runtime_dir/browser-agent/node_modules/playwright-core" ]]
}

session_ready() {
  [[ "$(stat -f %Su /dev/console)" == "$USER" ]]
}

browser_profile_ready() {
  local profile_dir=${BROWSER_PROFILE_DIR:-$PROJECT_DIR/.browser-profile}
  [[ -d "$profile_dir" && -n "$(find "$profile_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]
}

slack_configured() {
  [[ "${SLACK_WEBHOOK_URL:-}" == https://hooks.slack.com/services/* ]]
}

secrets_configured() {
  local app_secret=${APP_SECRET_KEY:-}
  local shim_secret=${SHIM_SHARED_SECRET:-}
  [[ ${#app_secret} -ge 32 && $app_secret != replace-with-a-long-random-value && $app_secret != development-only-change-me ]] &&
    [[ ${#shim_secret} -ge 32 && $shim_secret != replace-with-a-different-long-random-value && $shim_secret != development-shim-secret ]]
}

codex_ready() {
  local executable=${CODEX_BIN:-/usr/local/bin/codex}
  [[ -x "$executable" ]] && [[ -s "${CODEX_HOME:-$HOME/.codex}/auth.json" ]]
}

check "40 GB free runtime storage" runtime_free_gb
check "Docker daemon" "${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}" info
check "Sleeper public API" curl --fail --silent --max-time 10 "https://api.sleeper.app/v1/league/${SLEEPER_LEAGUE_ID:-1395499060898586624}"
check "Private dashboard bind" private_dashboard_bind
check "Post-only Slack notification" slack_configured
check "Application and agent secrets" secrets_configured
check "Codex CLI and login" codex_ready
check "Logged-in console session" session_ready
check "Chrome and Playwright agent" browser_ready
check "Persistent Sleeper browser profile" browser_profile_ready

backup_dir=${BACKUP_DIR:-/Volumes/Extreme SSD/FantasyFootballManager/backups}
if [[ -d "${backup_dir:h}" && -w "${backup_dir:h}" ]]; then
  print "PASS  backup volume"
  newest_backup=$(find "$backup_dir/daily" -type f -name 'fantasy-*.dump.gz' -mtime -2 -print -quit 2>/dev/null)
  if [[ -n "$newest_backup" ]] && gzip -t "$newest_backup" >/dev/null 2>&1; then
    print "PASS  recent valid database backup"
  else
    print "FAIL  recent valid database backup"
    failures=$((failures + 1))
  fi
else
  print "FAIL  backup volume"
  failures=$((failures + 1))
fi

if (( failures > 0 )); then
  print "\nDoctor found $failures blocking issue(s)."
  exit 1
fi
print "\nAll host checks passed."
