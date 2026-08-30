#!/usr/bin/env bash
# Restore a Joshua backup into this stack.
# Usage: scripts/restore.sh <backup_dir> [--force]
#        scripts/restore.sh <db.dump> <data.tar.gz> [--force]
#
# The database must be empty, or you must pass --force. --force drops every
# table before the restore. The data volume is replaced in full.
#
# After the restore the script starts the stack and asks core to rebuild the
# search index from the restored files.
set -euo pipefail

cd "$(dirname "$0")/.."

force=0
args=()
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    *) args+=("$arg") ;;
  esac
done

case "${#args[@]}" in
  1) dump="${args[0]}/db.dump"; tarball="${args[0]}/data.tar.gz" ;;
  2) dump="${args[0]}"; tarball="${args[1]}" ;;
  *) echo "usage: scripts/restore.sh <backup_dir> [--force]" >&2; exit 2 ;;
esac
for f in "$dump" "$tarball"; do
  [ -f "$f" ] || { echo "restore: $f is not a file" >&2; exit 2; }
done
[ -f .env ] || { echo "restore: .env is missing. Run 'make init-env' and put the old secrets back." >&2; exit 2; }
[ -f joshua.yaml ] || { echo "restore: joshua.yaml is missing. Copy it from the backup." >&2; exit 2; }

compose() { docker compose "$@"; }

echo "restore: starting postgres"
compose up -d postgres >/dev/null
for _ in $(seq 1 30); do
  if compose exec -T postgres pg_isready -U joshua -d joshua >/dev/null 2>&1; then break; fi
  sleep 1
done

tables="$(compose exec -T postgres psql -U joshua -d joshua -tAc \
  "select count(*) from information_schema.tables where table_schema = 'public'")"
if [ "${tables:-0}" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "restore: the database already has $tables tables. Pass --force to drop them first." >&2
  exit 1
fi

echo "restore: stopping the app containers"
compose stop core channels gateway >/dev/null

if [ "$force" -eq 1 ]; then
  echo "restore: dropping the public schema"
  compose exec -T postgres psql -U joshua -d joshua -q -c \
    "drop schema public cascade; create schema public;"
fi

echo "restore: database <- $dump"
compose exec -T postgres pg_restore -U joshua -d joshua --no-owner --no-privileges < "$dump"

# The core container must exist so the volume name can be read from it. A
# fresh checkout has no container yet, so create it without starting it.
compose create core >/dev/null 2>&1 || true
core_id="$(compose ps -aq core | head -n1)"
data_volume="$(docker inspect "$core_id" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
[ -n "$data_volume" ] || { echo "restore: cannot find the /data volume" >&2; exit 1; }

echo "restore: data volume $data_volume <- $tarball"
tar_dir="$(cd "$(dirname "$tarball")" && pwd)"
tar_name="$(basename "$tarball")"
docker run --rm \
  -v "$data_volume:/data" \
  -v "$tar_dir:/backup:ro" \
  alpine:3.20 \
  sh -c "find /data -mindepth 1 -delete && tar xzf /backup/$tar_name -C /data && chown -R 1000:1000 /data"

echo "restore: starting the stack"
compose up -d >/dev/null

echo "restore: waiting for core"
for _ in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8081/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done

set -a; . ./.env; set +a
if [ -n "${JOSHUA_TOKEN_LAPTOP:-}" ]; then
  echo "restore: rebuilding the search index"
  curl -fs -X POST -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP" \
    -H "Content-Type: application/json" -d '{"full": true}' \
    http://127.0.0.1:8081/admin/kb/reindex >/dev/null \
    || echo "restore: the reindex request failed. Run it by hand: POST /admin/kb/reindex {\"full\": true}" >&2
fi

echo "restore: done"
