# Operations

Day-to-day care of a running instance: health, logs, updates, backup, and the
admin routes.

## Health

Each container answers two open routes on its port:

- `GET /healthz` returns `{"ok": true}` when the process runs.
- `GET /readyz` returns `{"ok": bool, ...}` with the checks that matter for that
  container.

| Container | `/readyz` checks |
|---|---|
| `channels` | one entry per channel: `{"ok": bool}`. Telegram reports `ok: false` after a poll error. iMessage reports whether BlueBubbles answers a ping. |
| `core` | `db` (the database answers) and `layout` (the data volume is complete and writable) |
| `gateway` | `connected`, `errored`, `total`: the MCP upstreams |

From the host:

```
curl localhost:8080/readyz          # channels
curl 127.0.0.1:8081/readyz          # core
docker compose exec core python -c "import urllib.request; print(urllib.request.urlopen('http://gateway:8000/readyz').read().decode())"
```

Compose runs the same `/healthz` check every 10 seconds. `make ps` shows the
result. A container that fails five checks in a row shows `unhealthy`.

## Logs

Every container writes one JSON line per event to stdout:

```
make logs                       # all containers
docker compose logs -f core     # one container
```

Each line has `ts`, `level`, `service`, `logger`, and `message`, plus the
fields of that event. A bearer token or a credential never appears. The
formatter replaces it with `[REDACTED]`. Health-check requests are not logged.

`LOG_LEVEL` in `.env` sets the level. The default is `INFO`. `DEBUG` shows each
tool call and each retrieval decision.

Message bodies are not logged at `INFO`. To read what a person said, use the
transcript route below.

## The admin routes

Every `/admin/*` route needs a bearer from `ADMIN_CALLERS`. The default is
`laptop` and `ci`. Use the `laptop` token from `.env`:

```
export $(grep JOSHUA_TOKEN_LAPTOP .env)
auth="Authorization: Bearer $JOSHUA_TOKEN_LAPTOP"
```

### channels, on `localhost:8080`

| Route | Use |
|---|---|
| `GET /admin/guard/stats` | how many messages the guard refused, by reason |
| `GET /admin/guard/recent` | the last 200 refusals, with the sender's handle. Use it to find a new person's Telegram id. |

### core, on `127.0.0.1:8081`

| Route | Use |
|---|---|
| `GET /admin/people` | the roster |
| `POST /admin/people` | add a person: `{"id", "name", "handle", "role"}` |
| `GET /admin/pool` | the open agent sessions |
| `POST /admin/sessions/flush` | close every session. The next message starts a fresh one. |
| `GET /admin/transcript/<conversation>?limit=40` | the last rows of one conversation |
| `GET /admin/kb/status` | the index: per source, when it ran, how many documents and chunks |
| `POST /admin/kb/reindex` | reconcile the index. `{"full": true}` re-embeds everything. `{"person": "sam"}` limits it to one person. |
| `GET /admin/kb/events?limit=40` | what retrieval found for recent turns, and what it injected |
| `POST /admin/reflect` | run the nightly reflection now. `{"date": "2026-08-27", "person": "sam"}` re-runs one day for one person. |
| `POST /admin/turn` | run one turn with no channel: `{"channel", "text", "person"}`. The reply comes back in the response and is not delivered. |

Example:

```
curl -s -H "$auth" 127.0.0.1:8081/admin/kb/status
curl -s -H "$auth" -X POST -H "Content-Type: application/json" \
  -d '{"full": true}' 127.0.0.1:8081/admin/kb/reindex
```

### gateway

The gateway has no host port. Reach it from inside the `core` container, or
add a port to a compose override for a debugging session.

| Route | Use |
|---|---|
| `GET /admin/inventory` | every MCP server, its status, its tools, and who may use it |
| `GET /admin/calls?identity=&tool=&limit=200` | the last tool calls: who, which tool, how long, success or denied |
| `POST /admin/reload` | reconnect the upstreams after a change to `mcp:` in `joshua.yaml`. `{"server": "name"}` reloads one. |

## Change the config

Every container mounts `joshua.yaml` read-only. After an edit:

```
make validate
docker compose restart core channels gateway
```

`make validate` runs in the core image. Compose passes the secrets from
`.env`, because the file refers to them by name.
A change to `mcp:` alone needs only `POST /admin/reload` on the gateway and a
restart of `core`.

Every refusal writes one line, so a dropped message is not silent to you:

```json
{"level": "WARNING", "logger": "channels.guard", "message": "inbound refused",
 "channel_type": "telegram", "reason": "unknown_sender",
 "address": "123456789", "chat_id": "123456789"}
```

`address` is the handle to add to the roster. The line never holds the message
text: the guard is given a length, never the words. `GET /admin/guard/recent`
holds the same fields.

A person that Joshua adds with `add_user` goes to `/data/people.yaml`, not to
`joshua.yaml`. The loader merges that file over `people` at each load, so an
added person survives a restart and a config edit.

## Update Joshua

```
git pull
make up
```

`make up` rebuilds the images and restarts the containers. The database schema
is additive. A new version adds tables and columns and never removes one, so
an update needs no migration step. The documentation in `wiki/joshua/` is
replaced at each start.

Read `CHANGELOG.md` before an update. It names every change that a person who
runs Joshua can see.

## Back up and restore

A backup is three things: the database, the `/data` volume, and the two config
files.

```
make backup                   # writes ./backups/joshua-<timestamp>/
make backup DEST=/mnt/nas     # writes there instead
```

The backup directory holds `db.dump` (`pg_dump` custom format), `data.tar.gz`
(the volume, without `inbox/` and `.trash/`), `joshua.yaml`, `env` (a copy of
`.env`, which holds secrets), and `manifest.json` (when, from which commit,
with which images). The stack must run for the backup. Keep the directory
private. It holds every secret.

Retention is yours. A cron line that keeps 14 days:

```
0 3 * * * cd /path/to/joshua && scripts/backup.sh /backups && find /backups -maxdepth 1 -name 'joshua-*' -mtime +14 -exec rm -r {} +
```

To restore on a new machine:

1. Clone the repository.
2. Copy `env` from the backup to `.env`, and `joshua.yaml` to the repository
   root.
3. Run `make up` once, then `make down`. This creates the volumes.
4. Run `make restore FROM=<backup directory>`.

The restore refuses a database that already has tables. `make restore FROM=…
FORCE=1` drops them first. The restore replaces the `/data` volume in full,
starts the stack, and asks `core` to rebuild the search index from the restored
files.

`make nuke` deletes the volumes. It asks first.

## The embedding model

On the first start, `core` downloads its embedding model, about 65 MB, from
`huggingface.co`. It goes to the `model-cache` volume, so a restart does not
download it again. `make nuke` deletes this volume with the others, so the
next start downloads the model again.

A host with no access to `huggingface.co` cannot complete the first start. After
the first start, the stack runs with no access.

## The scheduler and the nightly reflection

`core` runs the scheduler every `core.scheduler_tick_seconds` seconds (default
15). It fires due reminders and scheduled tasks, and delivers each reply to its
destination.

At `memory.nightly_at` local time (default `03:30`), `core` reads the previous
day's transcripts, writes each person's post for that day to
`blog/YYYY-MM-DD.md`, updates each `profile.md` and `shared/profile.md`, and
re-indexes. With `memory.daily_rollover: true` it then closes every session, so
the next message starts a fresh one with the profile and the recent posts as
context. `memory.md` explains the rules.

To run the reflection by hand, or to re-run one day, use `POST /admin/reflect`.

## Where the data is

| Docker volume | Holds | `make nuke` deletes it |
|---|---|---|
| `pgdata` | the database: people, conversations, transcripts, tasks, the search index | yes |
| `data` | `/data`: profiles, journals, notes, attachments, shared files | yes |
| `claude-config` | the agent SDK's session files | yes |
| `model-cache` | the embedding model | yes |
