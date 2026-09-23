#!/bin/zsh
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  print -u2 "Usage: CONFIRM_RESTORE=restore-fantasy ./scripts/restore.sh /absolute/path/to/backup.dump.gz"
  exit 2
fi
if [[ ${CONFIRM_RESTORE:-} != restore-fantasy ]]; then
  print -u2 "Restore replaces the fantasy database. Set CONFIRM_RESTORE=restore-fantasy to continue."
  exit 3
fi
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
backup=${1:A}
gzip -t "$backup"
cd "$PROJECT_DIR"
docker compose stop api scheduler worker
docker compose exec -T db dropdb -U fantasy --if-exists fantasy
docker compose exec -T db createdb -U fantasy fantasy
gzip -dc "$backup" | docker compose exec -T db pg_restore -U fantasy -d fantasy --no-owner --no-privileges
docker compose up -d api scheduler worker
print "Restore completed from $backup"
