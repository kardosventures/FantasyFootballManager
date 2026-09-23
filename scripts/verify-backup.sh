#!/bin/zsh
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  print -u2 "Usage: ./scripts/verify-backup.sh /absolute/path/to/fantasy-*.dump.gz"
  exit 2
fi
SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}
backup=${1:A}
case "$backup" in
  /Volumes/Extreme\ SSD/FantasyFootballBackups/*) ;;
  *) print -u2 "Backup must be beneath the configured FantasyFootballBackups directory"; exit 3 ;;
esac
gzip -t "$backup"
if [[ -f "$backup.sha256" ]]; then
  (cd "${backup:h}" && shasum -a 256 -c "${backup:t}.sha256")
fi
stamp=$(date -u +%Y%m%d%H%M%S)
verify_db="fantasy_verify_${stamp}_$$"
[[ "$verify_db" == fantasy_verify_<->_<-> ]] || { print -u2 "Unsafe verification database name"; exit 4; }
cd "$PROJECT_DIR"
cleanup() {
  docker compose exec -T db dropdb -U fantasy --if-exists "$verify_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker compose exec -T db createdb -U fantasy "$verify_db"
gzip -dc "$backup" | docker compose exec -T db pg_restore -U fantasy -d "$verify_db" --no-owner --no-privileges
table_count=$(docker compose exec -T db psql -U fantasy -d "$verify_db" -Atc "select count(*) from information_schema.tables where table_schema='public'")
[[ "$table_count" -ge 10 ]] || { print -u2 "Restored database contains only $table_count public tables"; exit 5; }
print "Verified $backup by restoring $table_count tables into an isolated temporary database."
