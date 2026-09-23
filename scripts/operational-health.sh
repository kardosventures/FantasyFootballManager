#!/bin/zsh
set -u

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${FANTASY_PROJECT_DIR:-${SCRIPT_DIR:h}}
ENV_FILE=${FANTASY_ENV_FILE:-$PROJECT_DIR/.env}
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
BACKUP_TARGET=${BACKUP_DIR:-/Volumes/Extreme SSD/FantasyFootballManager/backups}
minimum_gb=${MIN_FREE_DISK_GB:-40}
storage_path=${FANTASY_RUNTIME_HOST_DIR:-$PROJECT_DIR}
free_kb=$(df -Pk "$storage_path" | awk 'NR==2 {print $4}')
free_gb=$((free_kb / 1024 / 1024))
newest_backup=$(find "$BACKUP_TARGET/daily" -type f -name 'fantasy-*.dump.gz' -mtime -2 -print -quit 2>/dev/null)
backup_status=delegated
if [[ -n "$newest_backup" ]] && gzip -t "$newest_backup" >/dev/null 2>&1; then
  backup_status=healthy
fi
disk_status=healthy
[[ "$free_gb" -lt 50 ]] && disk_status=warning
[[ "$free_gb" -lt "$minimum_gb" ]] && disk_status=critical
running_services=$(docker ps --filter label=com.docker.compose.project=fantasy-operations --filter status=running --format '{{.ID}}' 2>/dev/null | wc -l | tr -d ' ')
service_status=healthy
[[ "$running_services" -lt 7 ]] && service_status=critical
overall=healthy
[[ "$disk_status" != healthy || "$service_status" != healthy ]] && overall=degraded
HOST_HEALTH_PATH=${FANTASY_HOST_HEALTH_PATH:-$PROJECT_DIR/reports/host-health.json}
temporary="$HOST_HEALTH_PATH.tmp-$$"
python3 - "$temporary" "$overall" "$free_gb" "$minimum_gb" "$disk_status" "$backup_status" "$newest_backup" "$service_status" "$running_services" <<'PY'
from datetime import datetime, timezone
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "status": sys.argv[2],
    "checks": {
        "disk": {
            "status": sys.argv[5],
            "free_gb": int(sys.argv[3]),
            "minimum_free_gb": int(sys.argv[4]),
        },
        "backup": {
            "status": sys.argv[6],
            "newest_path": sys.argv[7] or None,
        },
        "services": {
            "status": sys.argv[8],
            "running_count": int(sys.argv[9]),
            "expected_count": 7,
        },
    },
}
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
mv "$temporary" "$HOST_HEALTH_PATH"
[[ "$overall" == healthy ]]
