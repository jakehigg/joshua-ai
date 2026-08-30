#!/usr/bin/env bash
# Back up a running Joshua stack: the database, the data volume, and the two
# config files. Usage: scripts/backup.sh [dest_dir]   (default ./backups)
#
# Writes into <dest>/joshua-<timestamp>/:
#   db.dump          pg_dump custom format (pg_restore reads it)
#   data.tar.gz      the /data volume, without inbox/ and .trash/
#   joshua.yaml      the config file
#   env              a copy of .env. It holds secrets. Keep the directory private.
#   manifest.json    when, from which commit, with which images
#
# Retention is yours. A cron line that keeps 14 days:
#   0 3 * * * cd /path/to/joshua && scripts/backup.sh /backups && find /backups -maxdepth 1 -name 'joshua-*' -mtime +14 -exec rm -r {} +
set -euo pipefail

cd "$(dirname "$0")/.."
dest_root="${1:-./backups}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$dest_root/joshua-$stamp"

compose() { docker compose "$@"; }

# The data volume's docker name. Compose prefixes it with the project name, so
# read it from the core container instead of guessing.
core_id="$(compose ps -aq core | head -n1)"
if [ -z "$core_id" ]; then
  echo "backup: the core container does not exist. Run 'make up' once first." >&2
  exit 1
fi
data_volume="$(docker inspect "$core_id" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
if [ -z "$data_volume" ]; then
  echo "backup: cannot find the /data volume on the core container." >&2
  exit 1
fi

if ! compose ps --status running postgres | grep -q postgres; then
  echo "backup: postgres is not running. Start the stack with 'make up'." >&2
  exit 1
fi

mkdir -p "$dest"
chmod 700 "$dest"

echo "backup: database -> $dest/db.dump"
compose exec -T postgres pg_dump -U joshua -d joshua -Fc > "$dest/db.dump"

echo "backup: data volume $data_volume -> $dest/data.tar.gz"
docker run --rm \
  -v "$data_volume:/data:ro" \
  -v "$(cd "$dest" && pwd):/backup" \
  alpine:3.20 \
  tar czf /backup/data.tar.gz -C /data \
    --exclude='./inbox' --exclude='./inbox/*' --exclude='*/.trash' --exclude='*/.trash/*' .

cp joshua.yaml "$dest/joshua.yaml"
if [ -f .env ]; then
  cp .env "$dest/env"
  chmod 600 "$dest/env"
  echo "backup: copied .env to $dest/env. It holds secrets. Keep this directory private."
fi

commit="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
images="$(compose images --format json 2>/dev/null || echo '[]')"
cat > "$dest/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "commit": "$commit",
  "data_volume": "$data_volume",
  "excluded": ["inbox/", "**/.trash/"],
  "images": $images
}
EOF

du -sh "$dest" | awk '{print "backup: done, " $1 " in " $2}'
