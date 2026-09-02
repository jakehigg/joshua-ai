#!/usr/bin/env bash
# Restore a Joshua backup into this stack.
# Usage: scripts/restore.sh <backup_dir> [--force] [--no-embed] [--keep-token]
#        scripts/restore.sh <db.dump> <data.tar.gz> [--force] [--no-embed]
#
# The database must be empty, or you must pass --force. --force drops every
# table before the restore. The data volume is replaced in full.
#
# A rehearsal restore into a second stack must not hold a live messaging
# token. Two pollers on one bot split the messages between them, so the
# throwaway stack takes real ones. The script refuses such a restore;
# --keep-token says you mean it.
#
# After the restore the script starts the stack and asks core to rebuild the
# search index. The request returns at once and the pass runs in core, which
# holds a CPU until it ends. --no-embed skips the request: the indexer picks
# up the restored files on its next pass, and the nightly run covers the
# rest.
set -euo pipefail

cd "$(dirname "$0")/.."

force=0
embed=1
keep_token=0
args=()
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    --no-embed) embed=0 ;;
    --keep-token) keep_token=1 ;;
    *) args+=("$arg") ;;
  esac
done

manifest=""
case "${#args[@]}" in
  1) dump="${args[0]}/db.dump"; tarball="${args[0]}/data.tar.gz"; manifest="${args[0]}/manifest.json" ;;
  2) dump="${args[0]}"; tarball="${args[1]}" ;;
  *) echo "usage: scripts/restore.sh <backup_dir> [--force] [--no-embed] [--keep-token]" >&2; exit 2 ;;
esac
for f in "$dump" "$tarball"; do
  [ -f "$f" ] || { echo "restore: $f is not a file" >&2; exit 2; }
done
[ -f .env ] || { echo "restore: .env is missing. Run 'make init-env' and put the old secrets back." >&2; exit 2; }
[ -f joshua.yaml ] || { echo "restore: joshua.yaml is missing. Copy it from the backup." >&2; exit 2; }

compose() { docker compose "$@"; }

# One value from .env, without sourcing the file. Sourcing it this early would
# put every secret into the environment of every command below.
env_value() {
  sed -n "s/^[[:space:]]*$1=//p" .env | tail -n1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//"
}

# The docker name of the /data volume. Compose prefixes it with the project
# name, so it is read from the core container rather than guessed. The
# container need not run; it need not even have run before. It does need an
# image, so a fresh checkout pulls one first, and says what to do when it
# cannot.
find_data_volume() {
  local id volume
  id="$(compose ps -aq core 2>/dev/null | head -n1)"
  if [ -z "$id" ]; then
    compose create core >/dev/null 2>&1 || true
    id="$(compose ps -aq core 2>/dev/null | head -n1)"
  fi
  if [ -z "$id" ]; then
    echo "restore: pulling the core image, to read the volume name from it" >&2
    compose pull core >/dev/null 2>&1 || true
    compose create core >/dev/null 2>&1 || true
    id="$(compose ps -aq core 2>/dev/null | head -n1)"
  fi
  if [ -z "$id" ]; then
    echo "restore: cannot make the core container, so the /data volume has no name yet." >&2
    echo "restore: 'docker compose create core' needs an image. Run 'make pull' (or" >&2
    echo "restore: 'make up-dev' to build from this checkout), then run this again." >&2
    exit 1
  fi
  volume="$(docker inspect "$id" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
  if [ -z "$volume" ]; then
    echo "restore: the core container has no /data mount. Check docker-compose.yml." >&2
    exit 1
  fi
  printf '%s' "$volume"
}

# The data volume, found before anything is changed. A restore that cannot
# find the volume must say so while the database is still whole.
data_volume="$(find_data_volume)"

# A second stack that holds a live messaging token steals the live messages.
# Telegram long polling has no lock: two pollers on one bot split the updates
# between them, and neither one reports an error. The original volume name is
# in the manifest, so a restore into a different stack is visible here.
original_volume=""
if [ -n "$manifest" ] && [ -f "$manifest" ]; then
  original_volume="$(sed -n 's/.*"data_volume"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$manifest" | head -n1)"
fi
telegram_token="$(env_value TELEGRAM_BOT_TOKEN)"
if [ -n "$telegram_token" ] && [ -n "$original_volume" ] && [ "$original_volume" != "$data_volume" ]; then
  if [ "$keep_token" -ne 1 ]; then
    echo "restore: refusing to restore into a second stack with a live TELEGRAM_BOT_TOKEN." >&2
    echo "restore: this stack is '$data_volume' and the backup came from '$original_volume'," >&2
    echo "restore: so this is a rehearsal. Telegram gives each message to whichever" >&2
    echo "restore: poller asks first, so this stack would take real messages from the" >&2
    echo "restore: live one. Blank TELEGRAM_BOT_TOKEN in .env, and any OAuth token," >&2
    echo "restore: then run this again. Pass --keep-token if you mean to keep it." >&2
    exit 1
  fi
  echo "restore: WARNING: --keep-token, and this stack is not the original one."
  echo "restore: this stack polls the same bot as the live one and takes real messages."
fi

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
if [ "$embed" -eq 0 ]; then
  echo "restore: the search index is not rebuilt, because of --no-embed."
  echo "restore: core indexes the restored files on its next pass, and the"
  echo "restore: nightly run covers the rest. To start a full pass by hand:"
  echo "restore:   POST /admin/kb/reindex {\"full\": true, \"background\": true}"
elif [ -n "${JOSHUA_TOKEN_LAPTOP:-}" ]; then
  echo "restore: starting a full rebuild of the search index"
  # `background` makes core answer at once and do the pass after. The token
  # goes to curl on stdin, as a config file, and never in an argument: every
  # user on the machine can read the argument list of every process, so
  # `-H "Authorization: Bearer $TOKEN"` shows the token in `ps`.
  if printf 'header = "Authorization: Bearer %s"\n' "$JOSHUA_TOKEN_LAPTOP" |
    curl -fs --max-time 30 --config - -X POST \
      -H "Content-Type: application/json" \
      -d '{"full": true, "background": true}' \
      http://127.0.0.1:8081/admin/kb/reindex >/dev/null
  then
    echo "restore: the rebuild runs in core now. It embeds every document, so"
    echo "restore: it holds a CPU until it ends. A large corpus takes minutes."
    echo "restore: Joshua answers while it runs, and a search gets better as it"
    echo "restore: goes. Watch it with GET /admin/kb/status. Next time, pass"
    echo "restore: --no-embed to leave the work to the nightly run."
  else
    echo "restore: the reindex request failed. Run it by hand: POST /admin/kb/reindex {\"full\": true, \"background\": true}" >&2
  fi
fi

echo "restore: done"
