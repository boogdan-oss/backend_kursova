#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${BACKUP_ENV_FILE:-$root/.env}"
backup_dir="${BACKUP_DIR:-$HOME/backups/cryptoparser}"
keep_days="${BACKUP_KEEP_DAYS:-7}"
network="${BACKUP_DB_NETWORK:-crypto-infra_default}"

read_env() { grep -E "^$1=" "$env_file" | head -1 | cut -d= -f2- | tr -d '\r'; }

db_user="$(read_env POSTGRES_USER)"
db_password="$(read_env POSTGRES_PASSWORD)"
db_name="$(read_env POSTGRES_DB)"
if [ -z "$db_user" ] || [ -z "$db_name" ]; then
  echo "У $env_file немає POSTGRES_USER або POSTGRES_DB" >&2
  exit 1
fi

container="${BACKUP_DB_CONTAINER:-}"
if [ -z "$container" ]; then
  container="$(docker ps --filter "network=$network" --format '{{.Names}} {{.Image}}' \
    | awk '/timescale|postgres/ {print $1; exit}')"
fi
if [ -z "$container" ]; then
  echo "Не знайдено контейнер PostgreSQL у мережі $network; задай BACKUP_DB_CONTAINER" >&2
  exit 1
fi

pg() {
  if [ "$container" = "local" ]; then
    PGPASSWORD="$db_password" "$@"
  else
    docker exec -i -e PGPASSWORD="$db_password" "$container" "$@"
  fi
}

ids="$(pg psql -U "$db_user" -d "$db_name" -AtX -F ' ' -c \
  "SELECT id, COALESCE(compressed_hypertable_id, 0) FROM _timescaledb_catalog.hypertable WHERE table_name = 'offer_snapshots'")"
read -r raw_id compressed_id <<<"$ids"
if [ -z "${raw_id:-}" ]; then
  echo "Не знайдено hypertable offer_snapshots" >&2
  exit 1
fi

mkdir -p "$backup_dir"
target="$backup_dir/cryptoparser-$(date -u +%Y%m%d-%H%M).dump"
if ! pg pg_dump -U "$db_user" -d "$db_name" -Fc \
  --exclude-table-data="_timescaledb_internal._hyper_${raw_id}_*" \
  --exclude-table-data="_timescaledb_internal.compress_hyper_${compressed_id}_*_chunk" \
  2>"$target.log" >"$target.partial"; then
  cat "$target.log" >&2
  rm -f "$target.partial" "$target.log"
  exit 1
fi

if [ ! -s "$target.partial" ]; then
  echo "Дамп порожній" >&2
  rm -f "$target.partial" "$target.log"
  exit 1
fi
mv "$target.partial" "$target"
rm -f "$target.log"
find "$backup_dir" -name 'cryptoparser-*.dump' -mtime +"$keep_days" -delete
echo "$(date -u +%FT%TZ) $target $(du -h "$target" | cut -f1)"
