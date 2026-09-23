#!/bin/sh
set -eu

BACKUP_TARGET=/backups
mkdir -p "$BACKUP_TARGET/daily" "$BACKUP_TARGET/weekly"
verify_db=""
cleanup() {
  if [ -n "$verify_db" ]; then
    dropdb -h db -U "${POSTGRES_USER:-fantasy}" --if-exists "$verify_db" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  daily="$BACKUP_TARGET/daily/fantasy-$stamp.dump.gz"
  partial="$daily.partial"
  rm -f "$partial"
  pg_dump -h db -U "${POSTGRES_USER:-fantasy}" -d "${POSTGRES_DB:-fantasy}" -Fc | gzip -9 > "$partial"
  gzip -t "$partial"
  verify_db="fantasy_backup_verify_$(date -u +%Y%m%d%H%M%S)_$$"
  createdb -h db -U "${POSTGRES_USER:-fantasy}" "$verify_db"
  gzip -dc "$partial" | pg_restore -h db -U "${POSTGRES_USER:-fantasy}" -d "$verify_db" --no-owner --no-privileges
  table_count=$(psql -h db -U "${POSTGRES_USER:-fantasy}" -d "$verify_db" -Atc "select count(*) from information_schema.tables where table_schema='public'")
  [ "$table_count" -ge 10 ]
  dropdb -h db -U "${POSTGRES_USER:-fantasy}" "$verify_db"
  verify_db=""
  mv "$partial" "$daily"
  daily_dir=$(dirname "$daily")
  daily_name=$(basename "$daily")
  (cd "$daily_dir" && sha256sum "$daily_name" > "$daily_name.sha256")
  byte_count=$(wc -c < "$daily" | tr -d ' ')
  digest=$(cut -d ' ' -f 1 "$daily.sha256")
  printf '{"backup":"%s","bytes":%s,"restored_table_count":%s,"sha256":"%s","verified":true}\n' \
    "$daily_name" "$byte_count" "$table_count" "$digest" > "$daily.manifest.json"
  find "$BACKUP_TARGET/daily" -type f -name 'fantasy-*.dump.gz' -mtime +13 -delete
  find "$BACKUP_TARGET/daily" -type f \( -name 'fantasy-*.dump.gz.sha256' -o -name 'fantasy-*.dump.gz.manifest.json' \) -mtime +13 -delete
  if [ "$(date +%u)" = 7 ]; then
    cp -p "$daily" "$BACKUP_TARGET/weekly/$(basename "$daily")"
    cp -p "$daily.sha256" "$BACKUP_TARGET/weekly/$(basename "$daily").sha256"
    cp -p "$daily.manifest.json" "$BACKUP_TARGET/weekly/$(basename "$daily").manifest.json"
  fi
  weekly_files=$(find "$BACKUP_TARGET/weekly" -type f -name 'fantasy-*.dump.gz' | sort -r)
  printf '%s\n' "$weekly_files" | sed -n '9,$p' | while IFS= read -r old; do
    [ -n "$old" ] || continue
    rm -f "$old" "$old.sha256" "$old.manifest.json"
  done
  sleep 86400
done
