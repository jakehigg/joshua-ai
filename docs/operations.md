# Operations

Day-to-day care of a running instance: health, logs, updates, backup, and the
admin routes.

## Health

Each container answers two open routes on its port:

- `GET /healthz` returns `{"ok": true}` when the process runs.
- `GET /readyz` returns `{"ok": bool, ...}` with the checks that matter for
  that container. It answers `200` when the container is ready and `503` when
  it is not, so a Kubernetes readiness probe can act on it.

| Container | `/readyz` checks |
|---|---|
| `channels` | one entry per channel: `{"ok": bool}`. Telegram reports `ok: false` while a poll error stands, and `ok: true` again after the next good poll. iMessage reports whether BlueBubbles answers a ping. |
| `core` | `db` (the database answers) and `layout` (the data volume is complete and writable). `embed` reports the model and does not hold `ok` down: a lost model costs the memory and not the turn. |
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
| `GET /admin/chats/unconfigured` | the group chats `joshua.yaml` does not list. Use it to find a group's chat id. |

### Add a group chat

Joshua answers a group only when `joshua.yaml` lists it. To add one:

1. Add Joshua to the chat and send one message there.
2. Read the chat id:

```
curl -sH "$auth" localhost:8080/admin/chats/unconfigured
```

3. Put the `chat_id` in `groups` in `joshua.yaml`, with an `id` and the
   `channel`. Add `members` to admit only some senders.
4. Reload the config (see "Change the config").

The route lists a chat whether the sender is on the roster or not, because a
group of enrolled people refuses nobody and so writes no refusal. It holds no
message text and no sender handle.

### core, on `127.0.0.1:8081`

| Route | Use |
|---|---|
| `GET /admin/people` | the roster |
| `POST /admin/people` | add a person: `{"id", "name", "handle", "role"}` |
| `GET /admin/pool` | the open agent sessions |
| `POST /admin/sessions/flush` | close every session. The next message starts a fresh one. |
| `GET /admin/transcript/<conversation>?limit=40` | the last rows of one conversation |
| `GET /admin/kb/status` | the index: per source, when it ran, how many documents and chunks |
| `POST /admin/kb/reindex` | reconcile the index. `{"full": true}` re-embeds everything. `{"person": "sam"}` limits it to one person. `{"background": true}` answers 202 at once and runs the pass after. |
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

## The two ways to run Joshua

`make up` starts the images of a release, from the GitHub container registry.
Nothing is built. This is the way to run Joshua.

`make up-dev` builds the three images from your checkout and starts those. Use
it only when you change the code. A build is tagged
`joshua-ai-<component>:dev`, so it never replaces a release on your machine.

Both use the same containers and the same volumes. `make down`, `make logs`,
`make ps`, `make backup`, and `make restore` work for either.

## The version you run

`make init-env` writes `JOSHUA_VERSION` into `.env`, so an installation stays
on one release. Nothing moves it: not `git pull`, and not a new release. Read
it at any time:

```
grep JOSHUA_VERSION .env
```

There is no `latest` tag on the registry. Every image reference names one
version, so a start always gets the same three images.

## Update Joshua

Put the release you want in `.env`, then:

```
make pull
make up
```

`make pull` gets the images of that release and `make up` restarts the
containers. The releases are at
<https://github.com/jakehigg/joshua-ai/releases>. Read `CHANGELOG.md` first.
The database schema is additive. A new version adds tables and columns and never removes one, so
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
3. Run `make pull`. The restore reads the volume name from the `core` image.
4. Run `make restore FROM=<backup directory>`.

The restore refuses a database that already has tables. `make restore FROM=…
FORCE=1` drops them first. The restore replaces the `/data` volume in full,
starts the stack, and asks `core` to rebuild the search index from the restored
files.

### To rehearse a restore

Rehearse a restore before you need one. Two rules make a rehearsal safe.

**Blank every live credential first.** Set `TELEGRAM_BOT_TOKEN=` in the `.env`
of the rehearsal stack, and blank each OAuth token. Telegram long polling has
no lock: two stacks that hold one bot token both poll it, Telegram gives each
message to whichever poller asks first, and neither stack reports an error.
Your live messages then land in the throwaway stack. The same is true of an
OAuth token to an upstream. The restore refuses to go on when the `.env` holds
a token and the stack is not the one the backup came from; `--keep-token` says
you mean it.

**Restore into a directory with a different name.** The directory name becomes
the compose project name, and the project name keys the volumes, so a
different directory gives the rehearsal its own database and its own `/data`.
The live stack is untouched.

```
git clone <the repository> joshua-rehearsal
cd joshua-rehearsal
cp <backup>/joshua.yaml .
cp <backup>/env .env
# blank TELEGRAM_BOT_TOKEN and every OAuth token in .env now
make pull
make restore FROM=<backup directory>
```

Delete the directory and its volumes (`make nuke`) when you are done.

### The rebuild of the search index

The request returns at once and the pass runs in `core`. It makes an embedding
for every document, so **it holds a CPU until it ends**. A large corpus takes
minutes. Joshua answers while it runs, and a search gets better as the pass
goes. Watch it:

```
curl -s -H "$auth" 127.0.0.1:8081/admin/kb/status
```

`running` is true while the pass runs. `last_error` names a failure.

To leave the work for later:

```
make restore FROM=<backup directory> NO_EMBED=1
```

`core` then indexes the restored files on its next pass, which is every
`memory.index_interval_s` seconds, and the nightly reflection covers the rest.
This costs nothing at the time of the restore, and a search is weaker until the
index catches up. Start a full pass at any time:

```
curl -s -X POST -H "$auth" -H 'Content-Type: application/json' \
  -d '{"full": true, "background": true}' \
  127.0.0.1:8081/admin/kb/reindex
```

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

## The viewer

The viewer is a read-only web page for the wiki and a person's own files. It is
off by default. To turn it on:

1. Set a password for each person in `.env`. Name it `VIEWER_PW_<ID>` in upper
   case, for example `VIEWER_PW_ALEX`.

   ```
   VIEWER_PW_ALEX=a-long-random-password
   ```

   For a bcrypt hash instead of a literal, set the value to a `$2` string.

2. Turn the viewer on in `joshua.yaml` and list each person:

   ```yaml
   viewer:
     enabled: true
     users:
       alex: ${VIEWER_PW_ALEX:-}
   ```

3. Validate and start it:

   ```
   make validate
   docker compose --profile viewer up -d viewer
   ```

The viewer listens on host port 8081. Open `http://localhost:8081/` and sign in
with the person id and the password. Put a reverse proxy in front for TLS
before you expose it off the host.

A member sees a "move this page to trash" button on a wiki page. It moves the
page to `wiki/.trash/` and drops it from the search index at the next index run.
A guest reads the wiki but cannot delete. To restore a page, move it back from
`wiki/.trash/<timestamp>/` with a shell.

## Where the data is

| Docker volume | Holds | `make nuke` deletes it |
|---|---|---|
| `pgdata` | the database: people, conversations, transcripts, tasks, the search index | yes |
| `data` | `/data`: profiles, journals, notes, attachments, shared files | yes |
| `claude-config` | the agent SDK's session files | yes |
| `model-cache` | the embedding model | yes |
