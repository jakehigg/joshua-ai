# Operations

Day-to-day care of a running instance: health, logs, updates, backup, and the
admin routes.

## Health

Each container answers two open routes on its port:

- `GET /healthz` returns `{"ok": true}` when the process runs.
- `GET /readyz` returns `{"ok": bool, ...}` with the checks that matter for
  that container. `core` answers `503` when it is not ready, so a Kubernetes
  readiness probe can act on it. `channels` and `gateway` always answer `200`:
  their checks report an upstream, not whether the container can serve. See
  the table.

| Container | `/readyz` checks |
|---|---|
| `channels` | one entry per channel: `{"ok": bool}`. Telegram reports `ok: false` while a poll error stands, and `ok: true` again after the next good poll. iMessage reports whether BlueBubbles answers a ping. The code stays `200`: channels serves `core` and the webhook while a poller is failing, and a `503` would stop the webhook that works because of an upstream that does not. |
| `core` | `db` (the database answers) and `layout` (the data volume is complete and writable). Either one false is a `503`. `embed` reports the model and does not hold `ok` down: a lost model costs the memory and not the turn. `inference_overrides` is the number of `inference` job blocks that set `base_url` or `model`. It names no server and no model. |
| `gateway` | `connected`, `errored`, `total`: the MCP upstreams. The code stays `200`: the `files` builtin answers whatever an upstream is doing, and a `503` would take away every tool because one failed to connect. |

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
| `GET /admin/journal/status` | today's journal: entries written, whether the nightly page landed, and the last nightly run |
| `POST /admin/turn` | run one turn with no channel: `{"channel", "text", "person"}`. The reply comes back in the response and is not delivered. |
| `POST /admin/inference/check` | send one short prompt to the server and the model of each `inference` job. See "Check the model server". |

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

## The model server

The [`inference`](config.md#inference) section of `joshua.yaml` sets the
server, the key, and the model of each model call. Each recipe here is a
complete `inference` section. Put each key in `.env` (or in `secrets.core` of
the Helm chart), never in `joshua.yaml`, and write it as `${VAR:-}`.

### The Claude subscription

Do not write an `inference` section. Set `CLAUDE_CODE_OAUTH_TOKEN` in `.env`.
Every job uses the subscription. This is the default.

### LiteLLM in front of Bedrock

LiteLLM speaks the Anthropic Messages API and sends each call to the model
that its config names, for example a model on Bedrock. Joshua sends every job
to LiteLLM:

```yaml
inference:
  base_url: http://litellm:4000
  api_key: ${INFERENCE_API_KEY:-}
```

`INFERENCE_API_KEY` in `.env` is a LiteLLM key. `docker-compose.yml` gives it
to `core`.

Joshua sends the model setting of each job as it is, for example
`claude-opus-5`. Each name must be a `model_name` in the LiteLLM config.
Thus do one of these:

- Give LiteLLM a `model_name` for each model that a job asks for.
- Set `model` in each job block to a LiteLLM name:

```yaml
inference:
  base_url: http://litellm:4000
  api_key: ${INFERENCE_API_KEY:-}
  chat:
    model: bedrock-claude-sonnet
  voice:
    model: bedrock-claude-haiku
  reflection:
    model: bedrock-claude-sonnet
  describe:
    model: bedrock-claude-haiku
  internet:
    model: bedrock-claude-haiku
```

The LiteLLM config then names each alias:

```yaml
model_list:
  - model_name: bedrock-claude-sonnet
    litellm_params:
      model: bedrock/<the Bedrock model id>
  - model_name: bedrock-claude-haiku
    litellm_params:
      model: bedrock/<the Bedrock model id>
```

Know these limits:

- **Web search does not work.** The internet agent searches with `WebSearch`,
  a tool that runs on Anthropic's side. Through LiteLLM or Bedrock the search
  fails, and core writes a warning at start. `WebFetch` runs in `core`, so it
  works on every server when `internet.fetch.enabled` is on.
- **A PDF can fail.** The `describe` job sends a PDF as a document block. A
  model that is not Claude can refuse it through LiteLLM. Keep `describe` on
  a Claude model.

### One LiteLLM key for each job

LiteLLM counts the use of each key. To see the use of each job in LiteLLM,
give each job its own key and keep the one server. A job block that sets only
`api_key` uses the global `base_url` with that key, and never gets the global
key:

```yaml
inference:
  base_url: http://litellm:4000
  chat:
    api_key: ${INFERENCE_KEY_CHAT:-}
  voice:
    api_key: ${INFERENCE_KEY_VOICE:-}
  reflection:
    api_key: ${INFERENCE_KEY_REFLECTION:-}
  describe:
    api_key: ${INFERENCE_KEY_DESCRIBE:-}
  internet:
    api_key: ${INFERENCE_KEY_INTERNET:-}
```

Do these steps:

1. Make one key in LiteLLM for each job.
2. Put each key in `.env`, for example `INFERENCE_KEY_CHAT=...`.
3. Give each variable to `core`. `docker-compose.yml` gives `core` only
   `INFERENCE_BASE_URL` and `INFERENCE_API_KEY`, so add the job keys to the
   `environment` of `core` in `docker-compose.yml`:

```yaml
services:
  core:
    environment:
      INFERENCE_KEY_CHAT: ${INFERENCE_KEY_CHAT:-}
      INFERENCE_KEY_VOICE: ${INFERENCE_KEY_VOICE:-}
      INFERENCE_KEY_REFLECTION: ${INFERENCE_KEY_REFLECTION:-}
      INFERENCE_KEY_DESCRIBE: ${INFERENCE_KEY_DESCRIBE:-}
      INFERENCE_KEY_INTERNET: ${INFERENCE_KEY_INTERNET:-}
```

   With the Helm chart, add each variable to `secrets.core.keys`.
4. Restart `core`, then run the check (see "Check the model server").

A job key that `core` does not get is empty. The job then sends no key, and
the check shows `http_401` for that job. Jobs with different keys get
different check calls.

### A local model for one job

Ollama and LiteLLM can serve a local model through the Anthropic Messages
API. Send one job there, and keep the other jobs on the global setting. A
job that sets `base_url` must also set `api_key`. Write `api_key: ""` for a
server that needs no key:

```yaml
inference:
  reflection:
    base_url: http://ollama:11434
    api_key: ""
    model: llama3.1
```

The other jobs use the subscription. The nightly reflection uses the local
model. A small model can write a weak journal page. Read the next page that
it writes.

### Check the model server

`POST /admin/inference/check` on `core` sends the prompt "Reply with the word
ok." to each job. Jobs with the same server, model, and key share one call.
Each call has a limit of 30 seconds, or `inference.timeout_seconds` when that
is less. The calls run at the same time. Each call that ends writes one usage
row, in the usage group of its first job.

Run it after a change to the `inference` section:

```
curl -s -H "$auth" -X POST 127.0.0.1:8081/admin/inference/check
```

The answer has one row for each job. This example shows two of the five:

```json
{
  "ok": false,
  "jobs": [
    {"job": "chat", "endpoint": "litellm:4000", "model": "bedrock-claude-sonnet",
     "ok": true, "seconds": 1.42},
    {"job": "reflection", "endpoint": "ollama:11434", "model": "llama3.1",
     "ok": false, "seconds": 0.08, "error": "http_404: model 'llama3.1' not found"}
  ]
}
```

- `ok` at the top is `true` only when every job is `ok`.
- `endpoint` is the host and the port of the server, or `anthropic`.
- `error` is a short label, such as `http_401`, `http_404`, `timeout`, or the
  name of an exception, then the start of the error text. Core removes every
  key and token from the text and cuts it to 200 characters. The log gets the
  label and the server, never the text.

To read a failure:

| `error` starts with | Do this |
|---|---|
| `http_401`, `authentication_failed` | Make sure that the key in `.env` is correct and that `core` gets it. |
| `http_404`, `http_400` | Make sure that the server has a model with the name in `model`. |
| `timeout` | Make sure that `core` can reach the server, and that the model loads in time. |
| `CLIConnectionError`, another exception name | Read the `core` log. |

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
an update needs no migration step. The documentation in `wiki/joshua-docs/`
is replaced at each start.

Read `CHANGELOG.md` before an update. It names every change that a person who
runs Joshua can see.

### Run the newest main

Set `JOSHUA_VERSION=branch-main` in `.env`, then:

```
make pull
make up
```

This tracks every merge to `main`, not a tested release. The images are amd64
only, so on an Apple Silicon host, run `make up-dev` from a checkout of `main`
instead: it builds the images locally. A release is the tested, multi-arch,
versioned build, and the default. To go back to one, put its version back in
`JOSHUA_VERSION` and run `make pull && make up` again.

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

### The wiki is a git repository

`wiki.git` (default `true`) keeps `/data/wiki` under git, committed at start,
at each write, and at the nightly run — see
[docs/data-layout.md](data-layout.md#the-wiki). The backup archive holds it
like any other file, so a restore brings the history back too.

To keep a history outside the volume, add a remote and push from the gateway
container, which mounts the wiki at `/data/wiki`:

```
docker compose exec gateway git -C /data/wiki remote add origin <url>
docker compose exec gateway git -C /data/wiki push -u origin main
```

To look at the history without pushing anywhere:

```
docker compose exec gateway git -C /data/wiki log --oneline | head
```

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
day's transcripts, writes one journal page per person for that day at
`wiki/journal/YYYY/MM/DD/YYYY-MM-DD.md`, updates each profile page and the
shared profile, and re-indexes. A day with nothing worth keeping gets no
journal page. With `memory.daily_rollover: true` core then closes every
session, so the next message starts a fresh one with the profile block as
context. `memory.md` explains the rules.

To run the reflection by hand, or to re-run one day, use `POST /admin/reflect`.

To tell that the journal is alive without reading the logs, use
`GET /admin/journal/status`:

```
curl -s -H "$auth" 127.0.0.1:8081/admin/journal/status
```

It answers with today's date, `entries_today` (the entries Joshua wrote during
the day), `day_page_today` (whether today already has a nightly page), and
`last_run`, the summary of the last reflection this core ran. Two things to
know when you read it. The nightly page is dated for the day it covers, so the
03:30 run writes yesterday's page and `day_page_today` stays false until
tomorrow morning. And `last_run` is None after a restart, because it is held in
the process; the day folders on the volume are the record that survives.

## The viewer

The viewer is a web page for the wiki, which holds the journal and every
profile, and a person's own attachments. Everybody who signs in reads. A
member also writes: they edit any wiki page, make a new one, correct a journal
entry, and move a page to the trash. A guest reads and nothing more. A
signed-in person gets a profile link for a quick jump to their own page. It is
off by default. To turn it on:

1. Put every password in `VIEWER_PASSWORDS` in `.env`, as a comma-separated
   list of `<person-id>=<value>` pairs. One variable carries all of them, so
   no tracked file names a person.

   ```
   VIEWER_PASSWORDS=alex=a-long-random-password,mia=$2b$12$...
   ```

   A value that starts with `$2` is a bcrypt hash. Any other value is a
   literal password, which cannot hold a comma. Make a hash with:

   ```
   docker compose run --rm --no-deps -T --entrypoint python core -c \
     "import bcrypt,getpass;print(bcrypt.hashpw(getpass.getpass().encode(),bcrypt.gensalt()).decode())"
   ```

2. Turn the viewer on in `joshua.yaml` and list each person who may sign in.
   Leave the value empty; the password comes from `.env`.

   ```yaml
   viewer:
     enabled: true
     users:
       alex: ""
   ```

   This list is what says who may sign in. A person who is not a key here
   cannot sign in, whatever `VIEWER_PASSWORDS` holds.

3. Validate and start it:

   ```
   make validate
   docker compose --profile viewer up -d viewer
   ```

The viewer listens on `127.0.0.1:8082`, beside the rest of the stack. Open
`http://127.0.0.1:8082/` and sign in with the person id and the password.
Nothing off the host reaches it: put a reverse proxy in front, for TLS, before
you expose it.

A member sees a "move this page to trash" button on a wiki page. It moves the
page to `wiki/.trash/` and drops it from the search index at the next index run.
A guest reads the wiki but cannot delete. To restore a page, move it back from
`wiki/.trash/<timestamp>/` with a shell.

The viewer holds one fleet token, its own, and no upstream credential. It
reads the volume through the same resolver as the agent's `files` tool, so the
two never drift on who reads what.

The search page finds pages by words, and by meaning when the viewer can reach
core. For the search by meaning, the viewer needs `CORE_URL` and
`JOSHUA_TOKEN_VIEWER`, and core needs the same `JOSHUA_TOKEN_VIEWER`. Compose
sets both, and `make init-env` mints the token. On Kubernetes, put the same
value under `JOSHUA_TOKEN_VIEWER` in the core Secret and in the viewer Secret.
Without the token, the page finds pages by words only. `GET /readyz` on the
viewer reports `core_search`.

On Kubernetes the chart runs it as its own deployment, off by default, with
the passwords in a Secret and an ingress of its own. See
[the chart README](../charts/joshua/README.md#the-viewer).

The viewer is read-only, and it ships with Joshua. For a browser editor that
can also change the wiki, see the Otter Wiki enhancement in
[docs/enhancements.md](enhancements.md). Unlike the viewer, Otter Wiki runs
code from another project.

### Opening the viewer with no sign-in

A household on a private network can skip the password. Set `viewer.anonymous`
to `true` in `joshua.yaml`:

```yaml
viewer:
  enabled: true
  anonymous: true
```

A request with no `Authorization` header now gets full access: it reads and
also writes, the same as a signed-in member, with no password. A member who
still wants to sign in by password can, exactly as before. To add
restrictions back, set `viewer.users` and list who may sign in; a household
that wants read-only for everyone else keeps `anonymous` off and gives each
person their own password.

**The network is now the only boundary.** Anyone who can reach the viewer
reads and edits the whole wiki: the journal and every profile. Use this only
on a network you trust, put TLS at the ingress or reverse proxy as always,
and never expose the viewer to the open internet with `anonymous` set.

### The usage page

`/usage` in the viewer shows the tokens that Joshua used, for each calendar
day in the instance `timezone`. The `usage` link in the menu opens it. Each
signed-in person can open it. It shows counts only: no person, no message,
and no question.

The page has a bar chart of the total tokens of each day, stacked by group, a
table of the days by group with a total row and a total column, and a table
of the totals of each group. Each group has six counts: calls, input, output,
cache read, cache write, and total. The groups are:

| Group | The model calls |
|---|---|
| `chat` | the turns of the agent in a conversation |
| `reflection` | the nightly reflection |
| `internet` | a lookup that the agent makes with `use_internet` in a conversation |
| `describe` | the attachment describer |

The table of days and the table of totals show each of these groups, also a
group with no usage in the window. The `Models` column of the totals table
lists the models of the calls of each group. A dash means that no call of the
group named its model, for example an old row from before the table kept the
model. A group whose rows name a worker gets a table of its workers under the
totals table. The "Each day" sections show only the groups with usage on that
day.

`?days=N` sets the window, today included: default 30, at most 366. The links
at the top give 7, 30, 90, and 366 days.

The page needs the same `CORE_URL` and `JOSHUA_TOKEN_VIEWER` as the search by
meaning, and `usage.allowed_callers` in `joshua.yaml` must name `viewer`,
which it does by default. Without them, or when core does not answer, the page
says so.

Core keeps one row for each call in the `usage_event` table. At each start,
core also adds the rows of the chat turns that it made before the table
existed, from the transcript. The nightly reflection, the lookups, and the
describer kept no count before, so the page has no old rows for them. An
operator reads the same numbers with an admin token:

```
curl -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP" \
  "http://127.0.0.1:8081/admin/usage?days=7"
```

## Where the data is

| Docker volume | Holds | `make nuke` deletes it |
|---|---|---|
| `pgdata` | the database: people, conversations, transcripts, tasks, the search index | yes |
| `data` | `/data`: profiles, journals, notes, attachments, shared files | yes |
| `claude-config` | the agent SDK's session files | yes |
| `model-cache` | the embedding model | yes |
