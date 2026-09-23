#!/bin/zsh
set -u

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${FANTASY_PROJECT_DIR:-${SCRIPT_DIR:h}}
ENV_FILE=${FANTASY_ENV_FILE:-$PROJECT_DIR/.env}
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

RUNTIME_ROOT=${FANTASY_RUNTIME_DIR:-${CODEX_DECISIONS_HOST_DIR:h}}
HEARTBEAT_PATH=${FANTASY_CODEX_HEARTBEAT_PATH:-$RUNTIME_ROOT/codex-runtime/codex-worker-heartbeat.json}
WATCHDOG_PATH=${FANTASY_CODEX_WATCHDOG_PATH:-$RUNTIME_ROOT/codex-watchdog.json}
STALE_SECONDS=${CODEX_WORKER_STALE_SECONDS:-300}
LAUNCHCTL_BIN=${FANTASY_LAUNCHCTL_BIN:-/bin/launchctl}
AGENT_LABEL=${FANTASY_CODEX_AGENT_LABEL:-com.jimai.fantasy-codex-agent}

if [[ ! "$STALE_SECONDS" =~ '^[0-9]+$' ]] || [[ "$STALE_SECONDS" -lt 60 ]]; then
  print -u2 "CODEX_WORKER_STALE_SECONDS must be an integer of at least 60"
  exit 2
fi

now_epoch=$(date +%s)
heartbeat_epoch=0
if [[ -f "$HEARTBEAT_PATH" ]]; then
  heartbeat_epoch=$(/usr/bin/stat -f %m "$HEARTBEAT_PATH" 2>/dev/null || print 0)
fi
heartbeat_age=-1
if [[ "$heartbeat_epoch" -gt 0 ]]; then
  heartbeat_age=$((now_epoch - heartbeat_epoch))
  [[ "$heartbeat_age" -lt 0 ]] && heartbeat_age=0
fi

watchdog_status=healthy
reason="Worker heartbeat is current"
restart_attempted=false
if [[ "$heartbeat_age" -lt 0 || "$heartbeat_age" -gt "$STALE_SECONDS" ]]; then
  restart_attempted=true
  if "$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$AGENT_LABEL"; then
    watchdog_status=restarted
    reason="Worker heartbeat was stale or missing; LaunchAgent restarted"
  else
    watchdog_status=critical
    reason="Worker heartbeat was stale or missing; LaunchAgent restart failed"
  fi
fi

watchdog_dir=${WATCHDOG_PATH:h}
mkdir -p "$watchdog_dir"
temporary="$WATCHDOG_PATH.tmp-$$"
python3 - "$temporary" "$watchdog_status" "$reason" "$HEARTBEAT_PATH" "$heartbeat_age" "$STALE_SECONDS" "$restart_attempted" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

target = Path(sys.argv[1])
heartbeat_path = Path(sys.argv[4])
worker = None
try:
    worker = json.loads(heartbeat_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    pass
payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "status": sys.argv[2],
    "reason": sys.argv[3],
    "heartbeat_path": str(heartbeat_path),
    "heartbeat_age_seconds": int(sys.argv[5]),
    "stale_after_seconds": int(sys.argv[6]),
    "restart_attempted": sys.argv[7] == "true",
    "worker": worker,
}
target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
mv "$temporary" "$WATCHDOG_PATH"
[[ "$watchdog_status" != critical ]]
