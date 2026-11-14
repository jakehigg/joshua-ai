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

Use the token that `claude setup-token` gives. Joshua supports only this
token.

To see how much of the 5-hour and the weekly limit is used, ask Joshua, or
read core's [limits route](contracts.md#the-limits-route). It needs no token:

```
curl -s 127.0.0.1:8081/v1/limits
```

In a cluster, call `http://<core service>:8000/v1/limits` from a pod.

The numbers come from the rate-limit report of each model call on the
subscription. They show after the first model call since the start, or from
the last saved value. A job on another inference server (`inference.base_url`)
does not report them.

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

- **Web search stops.** On the Anthropic server the internet agent uses
  Anthropic's own `WebSearch`, which needs nothing. Through LiteLLM, Bedrock,
  or Ollama there is no `WebSearch`, so the internet agent has no search, and
  core writes a warning at start. To search again, point `internet.search` at
  a SearXNG instance: see "Web search off Anthropic: SearXNG". `WebFetch` runs
  in `core`, so it works on every server when `internet.fetch.enabled` is on.
- **A PDF can fail.** The `describe` job sends a PDF as a document block. A
  model that is not Claude can refuse it through LiteLLM. Keep `describe` on
  a Claude model.
- **The tool definitions are a large part of each call.** Without tool
  search, each model call sends the definition of every MCP tool. With many
  servers, that can be tens of thousands of tokens, and the cache of the
  prompt is written again on most turns. Tool search is on for `chat` by
  default: a call then sends the tool names only, and the model loads a
  definition when it needs the tool, with one more model call. It is off for
  `voice`, because a spoken reply must be fast. Set
  `inference.voice.tool_search: true` to turn it on there too. See
  [Tool search](config.md#tool-search).
- **The prompt cache lasts five minutes off the subscription.** The CLI
  writes the prompt cache for one hour on the subscription, and for five
  minutes on any other server. Core sets the one-hour cache for each job
  that is not on the subscription, so you do not set anything. See
  [config.md](config.md#inference).

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

### What was tested

These setups were tested. A setup that is not in this list can work, but
nobody has tested it.

| Setup | Result |
|---|---|
| The Claude subscription, no `inference` section | Works. |
| LiteLLM in front of Bedrock, Claude Opus, Sonnet, and Haiku 5.5 | Works. A conversation turn, a resumed session, a one-shot call, structured output, an image, a PDF, MCP tools, and `WebFetch` were tested with a test script. Then one instance ran on it for several days, with one LiteLLM key for each job: chat, voice, the nightly reflection, the description of attachments, and the internet agent. `POST /admin/inference/check` gave `ok` for each job. |
| Web search through LiteLLM | Does not work. LiteLLM does not send Anthropic's `WebSearch` to Bedrock. Use SearXNG (see the next section). |
| SearXNG for the internet agent, on Bedrock through LiteLLM | Works. The internet agent searched, read pages, and answered with sources. |
| Tool search through LiteLLM to Bedrock | Works. A test with 81 MCP tools used about 8000 tokens for one turn in place of about 106000, and the model called the correct tool. |
| The one-hour prompt cache through LiteLLM to Bedrock | Works. The usage of each call shows the cache write as a one-hour write. |
| A local model through Ollama or LiteLLM | Not tested. |
| A model that is not Claude | Not tested. Structured output, images, and PDFs can fail. |

**Cost off the subscription.** On the subscription, a model call has no
cost for each token. On another server, each call has a cost, and most of
the cost of a turn is the prompt: the system prompt, the tool definitions,
and the history of the session. Tool search and the one-hour prompt cache
make the prompt much smaller and much less expensive. The model of the
`chat` job is the largest part that is left: a smaller model, for example
Sonnet in place of Opus, costs about half as much for each token. The usage
page does not show a cost for a call to another server, because the SDK
does not know the price there. Read the cost in LiteLLM, or in the bill of
your provider.

## Web search off Anthropic: SearXNG

On the Anthropic server (the subscription or an Anthropic API key), the
internet agent uses Anthropic's own `WebSearch`. You set nothing, and
`internet.search` is not used.

When the `internet` job moves to another server, such as Bedrock through
LiteLLM or Ollama, web search stops. To search again, give the internet agent
a SearXNG instance. The gateway calls the JSON API of SearXNG, and the agent
reads the answer of the internet agent, never the raw results.

Do these steps:

1. Run a SearXNG instance that the gateway can reach, for example the
   container `searxng` on port 8080 of the compose network.
2. Turn on the `json` format of SearXNG. In its `settings.yml`, add `json` to
   `search.formats`:

   ```yaml
   search:
     formats:
       - html
       - json
   ```

   Restart SearXNG. Without `json`, SearXNG answers `403`, and the gateway
   gives the error "searxng refused the json format (403)".
3. Add the `search` block to the `internet` section of `joshua.yaml`:

   ```yaml
   internet:
     search:
       url: http://searxng:8080
   ```

4. If a reverse proxy in front of SearXNG checks a key, add
   `api_key: ${SEARXNG_API_KEY:-}` to the block. Put
   `SEARXNG_API_KEY=<the key>` in `.env.gateway` (or in `secrets.gateway` of
   the Helm chart). The gateway sends it as `Authorization: Bearer <the key>`.
   Do not give the key to `core`.
5. Restart the gateway and core.
6. Read the core log. The line `"message": "internet search"` must show
   `"search": "searxng"`.

[docs/config.md](config.md#web-search) explains each key, including
`categories`, `language`, and `max_results`.

To read a failure, find the gateway log line with `"search": true`:

| `error` | Do this |
|---|---|
| `searxng refused the json format (403)...` | Add `json` to `search.formats` of SearXNG, or make sure that the proxy accepts the key. |
| `searxng refused the key (401)` | Make sure that `SEARXNG_API_KEY` in `.env.gateway` is correct. |
| `searxng unreachable: ConnectError` | Make sure that the gateway can reach `search.url`. |
| `timeout` | Make sure that SearXNG answers in `search.timeout_seconds`. |
| `searxng answer is not json` | Make sure that `search.url` is the SearXNG instance, not a login page. |

The gateway log holds the length of each query, never the query or the key.

## Change the config

Every container mounts `joshua.yaml` read-only. After an edit:

```
make validate
make restart
```

`make validate` runs the core image once, with no network, and stops. It gets
the channel credentials from `.env`, so its summary names only the channels
that start. `make restart` restarts every container that runs. After a change
to `viewer.enabled` or `assignments.enabled`, run `make up` instead: it
starts or stops that container. A change to `mcp:` alone needs only
`POST /admin/reload` on the gateway and a restart of `core`.

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
`make ps`, `make restart`, `make backup`, and `make restore` work for either.
On your own build, use `make up-dev` where a page says `make up`, and
`make validate-dev` where it says `make validate`: each one checks or starts
the images of your checkout.

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
<https://github.com/jakehigg/joshua-ai/releases>. Read `CHANGELOG.md` before
an update. It names every change that a person who runs Joshua can see.

Run `make init-env` again after an update. It adds each secret that a newer
`.env.example` names and your `.env` does not have yet, and changes nothing
else.

The database schema is additive. A new version adds tables and columns and
never removes one, so an update needs no migration step. The documentation in
`wiki/joshua-docs/` is replaced at each start.

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

The viewer is the web interface of Joshua: the wiki, which holds the journal
and every profile, a person's own attachments, the documents, the
assignments, the usage, and the chat. It is one React single-page app, built
by the node stage of `gateway/Dockerfile` and carried in the gateway image,
served at the root of the host behind its own sign-in page (or
`viewer.anonymous`).
Everything it shows comes from a JSON API under `/api/v1/`, with the same
sign-in as the app. [auth.md](auth.md) opens with a guide to the sign-in:
what a person sees on each method, what the owner does under Admin → Users,
and what the sign-in does not do.

Everybody who signs in reads. A member also writes: they edit any wiki page,
make a new one, correct a journal entry, move a page to the trash, comment on
a document, and chat with Joshua. A guest reads and nothing more. The owner
also keeps the roster, under Admin → Users. A
signed-in person gets a profile link for a quick jump to their own page. It is
off by default. To turn it on:

1. Give one person `role: owner` in `joshua.yaml`, and turn the viewer on with
   the method the people of this instance sign in with: `pin` asks for a PIN,
   `password` asks for a person id and a password, and `anonymous` asks for
   nothing and leaves `viewer.anonymous` in charge.

   ```yaml
   people:
     - id: sam
       name: Sam
       role: owner
     - id: robin
       name: Robin
       role: member
   viewer:
     enabled: true
     auth:
       method: pin
   ```

2. Validate and start it:

   ```
   make validate
   make up
   ```

   `make up` starts the viewer because `viewer.enabled` is `true`, and
   `make down` stops it with the rest.

3. Give the first person their secret. A person with no secret is on the
   roster and cannot sign in, so the owner needs one from the console:

   ```
   docker compose exec core python -m joshua_core people auth-reset \
     --id sam --secret '<a long secret>'
   ```

   The command writes a bcrypt hash into core's `auth_secret` table and
   prints no secret. After that, the owner sets the secret of everybody else
   under Admin → Users, and each person changes their own.

The secrets live in core and nowhere else: one salted bcrypt hash per person,
reached only over core's `/auth/*` routes, which the viewer calls with its own
fleet token (`JOSHUA_TOKEN_VIEWER`). No route gives a hash back. A sign-in
answers with one signed session cookie: the browser drops it when it closes,
or keeps it for 30 days when the person checks "remember this device".
Changing a secret, a reset by the owner, and a removal all sign out every
browser that holds the old one. `JOSHUA_VIEWER_SESSION_SECRET` signs the
cookie, so a sign-in survives a restart; `make init-env` writes it into
`.env`. Core serves the roster to the viewer, because `viewer` is one of
core's `AUTH_CALLERS`, so Admin → Users needs no setting of its own.

The viewer listens on `127.0.0.1:8082`, beside the rest of the stack. Open
`http://127.0.0.1:8082/` and sign in. The address opens the wiki. The menu
holds Wiki, Journal, Search, Usage,
Assignments, Documents (members only) and Chat. At the bottom, "Profile" at
`/me` has the settings of the person who signed in (their secret, their
instructions to Joshua, and their journal consent), the profile link, and
their recent attachments. The owner also gets "Admin" under it. Every one of these is an address of the app's router, and the app
reads them from `/api/v1/`. The chat is a screen of the same app, at
`/chat/<person id>`, with a workspace at `/chat/w/<owner>/<slug>`; it reaches the
`channels` container through the viewer. An image with no build of the app
logs `no frontend build; the React pages are off` at start and serves the
JSON API alone, so every address answers 404 until the image carries a build.
Nothing off the host reaches it: put a reverse proxy in front, for TLS, before
you expose it.

A member sees a "move this page to trash" button on a wiki page. It moves the
page to `wiki/.trash/` and drops it from the search index at the next index run.
A guest reads the wiki but cannot delete. To restore a page, move it back from
`wiki/.trash/<timestamp>/` with a shell.

### Install the viewer on a phone

The viewer is an installable app, scoped to the whole viewer and not to the
chat alone. Open it over HTTPS and use "Add to Home Screen" (Safari) or
"Install app" (Chrome). The phone keeps a Joshua icon, the elephant on the
dark square. Tapping it opens the wiki with no address bar and no browser
chrome, on the dark background of the look, and every screen of the viewer is
in it: the wiki, the journal, the search, the usage, the assignments, the
documents and the chat.

The app signs in as the browser does, so set up the sign-in, and the TLS in
front of it, first: a browser installs no app from a plain `http://` address
other than `localhost`.

A service worker keeps the shell of the app and its assets, so the app opens
on a slow network and shows its frame at once. Nothing under `/api/v1/` is
kept: the chat, the wiki pages, the documents and the workspaces always come
from the instance, and no answer of a person sits in the cache of the phone.
An update arrives on the next visit with a network.

### Links in the menu

The menu has a "Links" group: links to the addons and other tools that the
people of an instance use beside Joshua, for example the pantry or the tasks
page. Each link opens in a new tab. On a phone, the menu is a drawer: the
hamburger opens it, and the links are at its foot.

A member keeps the list at `/links`, under "Edit links": one row per link,
with a title, a URL, and an icon from a small built-in set. A URL must start
with `http://` or `https://`. "Add a link" puts a row at the end, the arrows
move a row, "remove" drops it, and "Save links" writes the whole list. A guest
sees the links but cannot change them. The list holds at most 30 links.

The viewer keeps the list in `wiki/.viewer/links.yaml`. The wiki tree, the
search, and the index skip the file, because its directory name starts with a
dot. When `wiki.git` is on, each save is a commit, so the history of the list
is in the wiki's repository. You can also edit the file with a shell:

```yaml
links:
  - title: Pantry
    url: https://pantry.example.com/
    icon: box
```

The icons are `link`, `box`, `list`, `cart`, `chart`, `home`, `music`,
`calendar`, `book`, `tool`, `bolt`, `cloud`, `server`, `camera`, `chat`,
`heart`, `code`, `star`, `globe`, and `play`. An unknown name shows `link`. An
entry with a bad URL or no title does not show, and the others still do.

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
its fleet token in a Secret and an ingress of its own. See
[the chart README](../charts/joshua/README.md#the-viewer).

The viewer edits the wiki one page at a time, and it ships with Joshua. For a
browser editor with its own search and page history, see the Otter Wiki
enhancement in [docs/enhancements.md](enhancements.md). Unlike the viewer,
Otter Wiki runs code from another project.

### Opening the viewer with no sign-in

A group on a private network can skip the sign-in. Set `viewer.anonymous`
to `true` in `joshua.yaml`, and leave `viewer.auth.method` at `anonymous`:

```yaml
viewer:
  enabled: true
  anonymous: true
```

A request with no session cookie now gets full access: it reads and
also writes, the same as a signed-in member, with no secret. To serve such a
request as one person, set `anonymous` to the id of a member or of the owner,
such as `anonymous: sam`. The request is then served as that person, with
their profile, attachments, and assignments. A person who still wants to sign
in can, and Admin → Users stays the owner's. To ask for a secret instead, set
`viewer.auth.method` to `pin` or `password` and leave `anonymous` off.

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

`?days=N` sets the window, today included: default 30, at most 366. The
buttons above the chart give 7, 30, 90, and 366 days.

**Subscription limits.** Above the chart, the page shows one meter for each
limit window of the Claude subscription: `5-hour` and `Weekly`. Each meter
shows the percent used, a bar, the percent left, and the reset time in the
instance `timezone`, for example `resets 10:30 pm today` or
`resets Mon 9 Nov, 8:00 pm`. The bar is green below 75 percent, amber with
the word `high` from 75 percent, and red with the word `critical` from 90
percent. A window that is blocked is red with the word `blocked`.

A line under the heading, such as `as of 10:14 pm today`, gives the time of
the last model call that reported the numbers. When a window reset after that
call, its meter keeps the last bar, dimmed, and says
`reset at 10:30 pm today — updates on the next model call`.

The meters read core's `GET /v1/limits` (see
[contracts.md](contracts.md#the-limits-route)). That route has no bearer, so
the meters need only `CORE_URL`, also when `JOSHUA_TOKEN_VIEWER` is not set.
When core does not know the limits yet, the section gives the reason and no
meter. When core does not answer, answers `404` because `limits.enabled` is
`false`, or gives an unknown answer, the section says that the limits are not
available. The rest of the page does not change. The viewer asks core for the
limits and for the tokens at the same time, so the meters do not make the page
slower.

The tables and the chart need the same `CORE_URL` and `JOSHUA_TOKEN_VIEWER`
as the search by meaning, and `usage.allowed_callers` in `joshua.yaml` must name `viewer`,
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

### The assignments section

[assignments.md](assignments.md) opens with a guide: how a person uses an
assignment, step by step, and the words to say to Joshua.

`/assignments` in the viewer shows the assignments of the person who signed
in, and no other person's. The `Assignments` link in the menu opens it. To
change an assignment, tell Joshua, or leave a comment on a line of its
definition (see [comments on a definition](#comments-on-a-definition)), its
plan, or its report, or answer a decision of its report (see
[answers and comments on a report](#answers-and-comments-on-a-report)). The
viewer writes nothing else here.

Three pills at the top give the states: `Drafts`, `In progress`, and
`Completed`. With no pill chosen, the page shows the assignments in progress,
else the drafts when there is one, else the completed ones. When the
assignments container does not answer, no pill is selected. Each row gives
the title, the status, the time it started (or was made), the model, and the
total tokens (not for a draft). A
row with an open question has a `question` mark. A time such as `2h ago` gives
the full time in the instance `timezone` when you hold the pointer on it. The
newest assignment is first. Under the title, a row also gives the first
sentence of the report when there is one, else the next step of the plan, and
the number of outputs. The row of a draft also gives, in muted type, the words
to say: "Tell Joshua: start assignment N". The list of the manager gives
these, so the page makes one call.

Click a title to open the assignment as a document.

A draft has not run, so its page is its definition. The page gives the
title, the status, the saved assignment it came from (if any), the count of
open comments, the words to say under the title, "Tell Joshua: start
assignment N", and the line "Tell Joshua to start it, or leave comments
first." With the id in the words, Joshua starts that draft and not a copy of
it. Under it is the **Definition**, open, with a `+` on each field and
each block of the goal for a comment. A draft has no plan, no report, and no
progress.

While the assignment is in progress, or when it has no report, the plan is
the page:

1. The title, the status, the saved assignment it came from (if any), the
   time, and a row of numbers: the tokens, the working time, the turns, the
   model, and the outputs.
2. A notice when the assignment waits: the open question, or the stop at the
   subscription limits.
3. The progress now: a bar from the check boxes of the plan ("4 of 9
   steps"), the next step, and the last checkpoint with its age. A plan with
   no check boxes has no bar.
4. **Plan**: the plan of the worker, as a wiki page shows it, with each check
   box drawn as a box. Each paragraph, heading, and list item is a block of
   its own, with a `+` for a comment, as in the definition.
5. **Outputs so far**: the recorded outputs as cards.
6. **Progress**: the checkpoints, newest first.
7. **Report**, when there is one, and **Definition**, the spec as a
   document (see [comments on a definition](#comments-on-a-definition)).
   Click one to open it. The definition opens by itself when it has an open
   comment.
8. The error, when the assignment failed.

While the assignment is in progress, the page loads again every 20 seconds,
so you see the plan change as the worker ticks its steps.

When a completed assignment has a report, the report is the page. The report
is one Markdown text with fixed headings, so Joshua reads the same text. The
viewer finds each heading and shows each section in its own way:

1. The head: the title, the status, the first sentence under the title in
   large type, and a row of numbers: the tokens, the working time, the turns,
   the model, the outputs, and the decisions.
2. The lead band: "N decisions wait for you", a link to the decisions, and
   the outputs as cards, the documents first. Each card gives the title, the
   kind, the note from the line of the report with the same reference, and an
   icon for its kind (task, page, issue, merge request, journal, file, or
   other). An output whose reference is an `http` or `https` address, or a
   document, has an `Open` link. Any other reference shows as code. When
   each decision has an answer, the band says so.
3. **Highlights**: each bullet is a card, with its bold lead in large type.
4. **Decisions for you**: numbered cards, each with its question, its
   context, a text box, and `Send to Joshua`.
5. **What was asked**, as a quote.
6. **What was done**, as prose. A section with a heading that the viewer does
   not know shows here too.
7. **Follow-ups**, as a checklist.
8. **Cost**: the full numbers.
9. **Plan** with its bar, **Progress**, and **Definition**, folded. The plan
   opens by itself when it has an open comment.

Each card and each block of the report has a `+` for a comment. The pages of
the assignments section have no script.

A section that the report does not have, or that says "None.", does not
show. A report with none of the headings shows as Markdown, as a wiki page
does.

The `Saved` link beside the pills opens `/assignments/saved`: the saved
assignments, each with its name, its title, the number of runs, and the last
run. A saved assignment opens with its definition and its runs. A row with
open comments shows the count, such as `2 comments for Joshua`.

#### Answers and comments on a report

1. Open a completed assignment. The lead band says how many decisions wait
   for you. Click it to go to the decisions.
2. Write your answer in the text box of a decision, and click
   `Send to Joshua`. The page opens again at that decision.
3. The card shows your answer, `Sent to Joshua`, and the time. While it is
   open, a `Delete` button removes it, and the text box shows again.
4. Joshua gets the answer in your conversation within about two minutes,
   applies it, and resolves it. The card then shows the line from Joshua in
   place of `Sent to Joshua`.

A `+` on a block of the report or of the plan is a comment on that block, the
same as on a definition. A comment on the plan or the report of a completed
assignment goes to Joshua in the same way: the answers and comments of two
minutes go as one message. A comment on a draft, on a definition, or on an
assignment in progress does not: tell Joshua that you left it.

The message goes to `assignments.people.<id>.notify` through channels, so the
assignments container must be in `channels.webhooks.allowed_callers` (see
[assignments.md](assignments.md#the-events)).

#### Comments on a definition

You do not edit a definition in the viewer. You tell Joshua what to change,
part by part.

The definition shows as a document. At the top, each field (the title, the
model, the budget, the working time, the repositories, the tools, and the
others) is one row of a list of properties. Under it, the goal shows as
Markdown, as a wiki page shows it, cut into blocks: each paragraph, heading,
list item, quote, code block, and table is one block. A list item in a
nested list is a block too, so you can comment on one bullet. Each row and
each block has a `+` at its left. The `Source` fold at the end gives the
definition as YAML, with a number and a `+` on each line, for a comment on
one exact line.

1. Open the definition of a saved assignment, a draft, or a run.
2. Hold the pointer on a field or a block, and click its `+`. On a phone,
   the `+` always shows.
3. Write what to change, such as "delete this line" or "use opus here", and
   click `Comment`. The page opens again at that part.
4. Tell Joshua that you left comments. Joshua reads them with
   `assignment_comments`, saves the changed spec (gives it to the draft, or
   starts a new run with it), and resolves each comment with one line on what
   it did.

A comment names one line of the YAML: the first line of the field or the
block, or the line that you chose in `Source`. An open comment shows under
the field or the block that holds its line, with its age and a `Delete`
button. When its line is not the first line of the block, the comment also
shows that line. Only the open comments that you wrote have the button. When the line changed
after you wrote the comment, the comment has a `line changed` mark and shows
the line as it was. A resolved comment moves to the `resolved` group under
the definition, with the line from Joshua. The head of the definition gives
the count of open comments, such as `3 comments for Joshua`.

The viewer sends each comment to the assignments container as the person who
signed in. The form needs no script. A comment is text: the page escapes it.

The section needs `ASSIGNMENTS_URL` and `JOSHUA_TOKEN_VIEWER` on the viewer,
and the assignments container must hold the same `JOSHUA_TOKEN_VIEWER`. Compose
sets `ASSIGNMENTS_URL` to `http://assignments:8000`. The chart sets it when
`assignments.enabled` is true. Without the two values, the page says that
assignments are not configured. When the assignments container does not
answer, the page says so and shows nothing old. `GET /readyz` on the viewer
reports `assignments`.

### The documents section

[documents.md](documents.md) is the guide for documents and artifacts: what
goes where, comments, versions, and promote.

`/documents` in the viewer shows the documents of the member who signed in
and the shared documents. A document is a plan, a report, a spec, a note, or
an artifact that Joshua keeps in `/data/documents/<person>/` or
`/data/documents/shared/`. The `Documents` link in the menu opens it. A guest
has no `Documents` link, and each documents page is 404 for a guest. A member
never sees the folder of another member.

The pills select a status: `Active` (the default), `Drafts`, `Final`, and
`Archived`. The chips under them select a kind: `plan`, `report`, `spec`,
`artifact`, `note`, or each other kind that a document has. Each row gives
the title, the summary (or the first paragraph), the kind, the owner (`yours`
or `shared`), the time of the last change, the count of open comments, and
the status. With no `/data/documents` folder, the page says that there are no
documents yet, and the viewer does not create the folder.

A document opens at `/documents/<owner>/<slug>`:

- A Markdown document shows in blocks, as the goal of an assignment shows:
  one block for each paragraph, heading, list item, quote, code block, and
  table, and each block takes a comment on its first line.
- An HTML document shows in a frame with `sandbox` that loads the artifact
  address of the document, `/documents/<owner>/<slug>/artifact`. The frame
  cannot reach the viewer, its sign-in, or a form of the viewer. Scripts do
  not run, unless the frontmatter of the document has `scripts: true`: then
  the sandbox allows scripts and nothing more.
- The `Source` fold gives the frontmatter first, with no number and no `+`,
  then each line of the body with its number and a `+`.
- The `Versions` fold lists the commits of the file in the git repository of
  `/data/documents`, newest first. Click a version to read it at
  `?rev=<sha>`. An old version is read-only and has no comments.

`/documents/<owner>/<slug>.json` gives the same document as JSON, with four
fields only: `title`, `kind`, `status`, and `updated`. It reads no body, it
renders nothing, it reads no git history, and it answers with
`Cache-Control: no-store`, so the chat can read it again while its artifact
panel is open. It has the sign-in and the owner rule of the page, so a
document of another member and a document that is not there are both 404. A
document slug never ends in `.json`, so this address cannot name a file.

`/documents/<owner>/<slug>/artifact` gives the body of an HTML document
alone, for the frame of the page. `?rev=<sha>` gives an older version. It has
the sign-in and the owner rule of the page, so a document of another member,
a document that is not there, and a Markdown document, which has no frame,
are all 404. The address has a content policy of its own:
`default-src 'none'; img-src 'self'; style-src 'unsafe-inline';
form-action 'none'; frame-src 'none'; frame-ancestors 'self'`, then the
`sandbox` directive. So a `<style>` element and a `style` attribute of the
document render, and the document loads nothing from the network, submits
no form, and opens no frame. A document with no scripts gets
`sandbox allow-same-origin` and no script runs, so the viewer can read its
size and fit the frame. A document with `scripts: true` gets
`script-src 'unsafe-inline'` and `sandbox allow-scripts`, and never
`allow-same-origin`, so it has an opaque origin. The directive also holds
when a browser opens the address alone.

To comment on a document, use the `+` of a block, a card, a heading, or a line
of `Source`, as on a definition (see
[Comments on a definition](#comments-on-a-definition)). The `+` of the head
of the page is a comment on the document as a whole. The viewer appends the comment to the
comments file beside the document, `<slug>.comments.jsonl`, with you as its
`author`, and commits it with the author Joshua and the message
`viewer: comment documents/<owner>/<slug>.md`. A comment names one line of
the body, from 1 at the first line after the frontmatter, so a change of the
frontmatter never moves a comment. Line 0 is the document as a whole, and
its `line_text` is the `title:` line. `Delete` removes your own open
comment from the file, with one more commit. You cannot delete a comment of
another person or a resolved comment. Tell Joshua that you left comments:
Joshua reads them, changes the document, and resolves each one. The form has
the same CSRF token and `Origin` check as each other form of the viewer.

When `/data/documents` is not a git repository yet, the viewer stores the
comment and makes no commit.

On the page of an assignment, an output of kind `document` whose ref is a
documents path, such as `documents/alex/party-plan.md`, links to its page.

## Where the data is

| Docker volume | Holds | `make nuke` deletes it |
|---|---|---|
| `pgdata` | the database: people, conversations, transcripts, tasks, the search index | yes |
| `data` | `/data`: profiles, journals, notes, attachments, shared files | yes |
| `claude-config` | the agent SDK's session files | yes |
| `model-cache` | the embedding model | yes |
