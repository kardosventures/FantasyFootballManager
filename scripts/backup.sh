#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${FANTASY_PROJECT_DIR:-${SCRIPT_DIR:h}}
ENV_FILE=${FANTASY_ENV_FILE:-$PROJECT_DIR/.env}
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
BACKUP_TARGET=${BACKUP_DIR:-/Volumes/Extreme SSD/FantasyFootballBackups}
DB_CONTAINER=${FANTASY_DB_CONTAINER:-fantasy-operations-db-1}
[[ "$DB_CONTAINER" == fantasy-operations-db-<-> ]] || { print -u2 "Unsafe database container name"; exit 2; }
case "$BACKUP_TARGET" in
  /Volumes/Extreme\ SSD/*) ;;
  *) print -u2 "BACKUP_DIR must be beneath /Volumes/Extreme SSD"; exit 2 ;;
esac
mkdir -p "$BACKUP_TARGET/daily" "$BACKUP_TARGET/weekly"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
daily="$BACKUP_TARGET/daily/fantasy-$stamp.dump.gz"
partial="$daily.partial"
trap 'rm -f "$partial"' EXIT
docker exec -i "$DB_CONTAINER" pg_dump -U fantasy -d fantasy -Fc | gzip -9 > "$partial"
mv "$partial" "$daily"
gzip -t "$daily"
shasum -a 256 "$daily" > "$daily.sha256"
python3 - "$daily" "$daily.manifest.json" <<'PY'
import hashlib, json, pathlib, sys

backup = pathlib.Path(sys.argv[1])
manifest = pathlib.Path(sys.argv[2])
digest = hashlib.sha256(backup.read_bytes()).hexdigest()
manifest.write_text(json.dumps({
    "backup": backup.name,
    "bytes": backup.stat().st_size,
    "sha256": digest,
}, sort_keys=True) + "\n", encoding="utf-8")
PY
find "$BACKUP_TARGET/daily" -type f -name 'fantasy-*.dump.gz' -mtime +13 -delete
find "$BACKUP_TARGET/daily" -type f \( -name 'fantasy-*.dump.gz.sha256' -o -name 'fantasy-*.dump.gz.manifest.json' \) -mtime +13 -delete
if [[ $(date +%u) == 7 ]]; then
  cp -p "$daily" "$BACKUP_TARGET/weekly/${daily:t}"
  cp -p "$daily.sha256" "$BACKUP_TARGET/weekly/${daily:t}.sha256"
  cp -p "$daily.manifest.json" "$BACKUP_TARGET/weekly/${daily:t}.manifest.json"
fi
weekly_files=("$BACKUP_TARGET"/weekly/fantasy-*.dump.gz(N.om))
if (( ${#weekly_files} > 8 )); then
  for old in "${weekly_files[@]:8}"; do rm -f "$old"; done
fi
print "$daily"
