# Contracts

The three containers talk to each other over HTTP. This page lists every route,
who may call it, and the shape of the request and the reply. The models live in
`joshua_shared.contracts`. Every model rejects an unknown field.

## Authentication

Every container reads the `JOSHUA_TOKEN_<NAME>` variables. A caller sends its
own token:

```
Authorization: Bearer <token>
```

The callee matches the token against all of them and gets the caller's
identity. The identity is the name after `JOSHUA_TOKEN_`, in lower case. A
missing or wrong token gets 401. An identity that is not on the route's list
gets 403.

The identities are `channels`, `core`, `gateway`, `laptop`, and `ci`. `laptop`
and `ci` are for operators. `viewer` and `mcp` call core's search by meaning:
the web viewer and the joshua-mcp addon. `assignments` is the assignments
container: it calls core's model forwarder and usage write, the gateway MCP
routes, and channels events. `make init-env` writes all of them except `ci`
into `.env`.

## Models

### TurnEvent

One message or event, from `channels` to `core`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `channel` | string | required | `<type>:<platform chat id>`, for example `telegram:123` |
| `chat` | Chat | required | `kind` (`dm` or `group`), `id`, `title` |
| `handle` | Handle | `null` | `type` (`telegram`, `imessage`, `webhook`, `voice`, `cli`) and `id` |
| `kind` | `message` or `event` | `message` | |
| `text` | string | `""` | |
| `attachments` | list of Attachment | `[]` | `path`, `mime`, `name`, `original_name` |
| `reply_to` | string | `null` | the platform message this replies to |
| `message_id` | string | `null` | idempotency key, kept for 15 minutes |
| `event_type` | string | `null` | names a skill (events only) |
| `payload` | object | `null` | event data (events only) |
| `options` | object | `null` | `verbatim: true` records the text with no model call |
| `framing` | string | `null` | replaces the default event framing |

`Attachment.path` is relative to `/data`: `people/<id>/attachments/YYYY/MM/<file>`
or `shared/…`.

### DeliverRequest

One reply, from `core` to `channels`.

| Field | Type | Default |
|---|---|---|
| `channel` | string | required |
| `text` | string | `""` |
| `attachments` | list of string | `[]` |

### ResolveResponse

The answer to `GET /v1/channels/resolve`.

| Field | Type |
|---|---|
| `channel` | string, `<type>:<platform chat id>` |
| `chat` | Chat |
| `channel_id` | string |
| `chat_kind` | `dm` or `group` |

## channels

Port 8000 in the container. Compose publishes it on the host as `8080`.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok": bool, "checks": {<channel>: {"ok": bool, …}}}`. Always `200`; `ok` reports the pollers, not readiness. The `voice` entry reports `thread` and a count of held identities. |
| `POST /v1/deliver` | `core` | DeliverRequest | `200 {"delivered": true, "parts": n}`, `400 bad_request` or `invalid_attachment`, `404 unknown_channel`, `502 send_failed` |
| `GET /v1/channels/resolve?ref=` | `core` | | `200` ResolveResponse, `404 unknown_channel` |
| `GET /v1/channels/refusals?limit=` | `core` | | `200 {"refusals": [{"channel_type", "address", "chat_id", "reason", "at"}]}`. The handle and the channel, never a message body. |
| `POST /v1/events` | `channels.webhooks.allowed_callers` | see `channels.md` | `202 {"accepted": true}`, `200 {"ignored": true}`, `400`, `404 unknown_channel`, `502`, `503 core_unavailable` |
| `POST /webhook/imessage/{secret}` | none, the path secret | BlueBubbles payload | always `200`. A wrong secret gets `404`. |
| `POST /v1/cli/turns/stream` | `channels.webhooks.allowed_callers` | `{"person": id, "text": str}` | SSE, or `400`, `403 {"reason": <guard reason>}`, `503 core_unavailable` |
| `GET /v1/cli/outbox?person=&ack=true` | `channels.webhooks.allowed_callers` | | `{"messages": [{"ts", "text", "attachments"}]}`, `403 unknown_person` |
| `POST /v1/voice/chat/completions` | `channels.voice.allowed_callers` | OpenAI chat completions, plus `speaker` and `speaker_confidence` | SSE of `chat.completion.chunk`, or one `chat.completion` for `stream: false`. `400 bad_request`, `404 not_found` with no voice section, `503 core_unavailable` |
| `GET /v1/models` | `channels.voice.allowed_callers` | | `{"object": "list", "data": [{"id": "joshua", …}]}`, `404 not_found` with no voice section |
| `GET /admin/guard/stats` | `ADMIN_CALLERS` | | counters: `unknown_sender`, `rate_limited`, `truncated`, `refused_total` |
| `GET /admin/guard/recent` | `ADMIN_CALLERS` | | `{"recent": [{"channel_type", "address", "chat_id", "reason", "at"}]}` |
| `GET /admin/chats/unconfigured` | `ADMIN_CALLERS` | | `{"groups": [{"channel_type", "chat_id", "chat_title", "at"}]}` |

An error reply from `channels` is `{"reason": "<code>"}`, with a `detail` field
when there is more to say.

## core

Port 8000 in the container. Compose publishes it on the host as
`127.0.0.1:8081`, so only this host reaches it.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok": bool, "checks": {"db": bool, "layout": bool, "embed": bool}, "inference_overrides": int}`. `200` when ready, `503` when not; `embed` does not hold `ok` down. `inference_overrides` counts the `inference` job blocks that set `base_url` or `model`. |
| `POST /v1/turns` | `channels` | TurnEvent | `202 {"accepted": true, "turn_id": id}`, `200 {"accepted": false, "reason": "duplicate"}`, `403 unknown_sender`, `429 overloaded` |
| `POST /v1/turns/stream` | `channels`, and `laptop` for a `cli` handle | TurnEvent | SSE (below), `403 unknown_sender` |
| `POST /v1/memory/search` | `memory.search.allowed_callers` (default `viewer`, `mcp`) | `{"query", "limit"?, "kinds"?, "source"?, "person"?}` | `{"results": [{"path", "title", "heading", "text", "kind", "score", "date"}]}`, `422` for a bad body, `503` when the embedding model is not loaded |
| `GET /v1/usage?days=` | `usage.allowed_callers` (default `viewer`) | | `{"timezone", "days", "groups", "rows", "totals"}` (below), `422` when `days` is not an integer |
| `POST /v1/usage` | `usage.writers` (default `assignments`) | `{"rows": [...]}` (below) | `{"accepted": n}`. `400` for a bad body or a row that is not of kind `assignment`, `413` over 1 MiB, `503` when the database fails. |
| `ANY /v1/model/{path}` | `inference.forwarder.allowed_callers` (default `assignments`) | a Claude API request | the answer of the model server, streamed (below). `401` for any other token, `403` for a path off the list, `404` when the list is empty, `502` when the server does not answer, `503` with no credential. `GET` or `HEAD` `/v1/model/api/hello` is open and answers `{}`. |
| `GET /v1/limits` | open | | the subscription limits (below), `404` when `limits.enabled` is `false` |
| `GET /admin/people` | `ADMIN_CALLERS` | | `{"people": […]}` |
| `POST /admin/people` | `ADMIN_CALLERS` | `{"id", "name", "handle", "role"?}` | `201 {"person": …}`, `400 {"error": …}` |
| `GET /admin/pool` | `ADMIN_CALLERS` | | the session pool |
| `POST /admin/sessions/flush` | `ADMIN_CALLERS` | | `{"flushed": n}` |
| `GET /admin/transcript/{conversation}?limit=` | `ADMIN_CALLERS` | | `{"conversation_id", "rows": […]}` |
| `POST /admin/kb/reindex` | `ADMIN_CALLERS` | `{"source"?, "person"?, "full"?, "background"?}` | `{"results": …}`, or 202 `{"started": true, …}` for `background` |
| `GET /admin/kb/status` | `ADMIN_CALLERS` | | `{"sources": {…, "unindexable": {"count", "paths"}}, "stats": […]}` |
| `GET /admin/kb/events?limit=` | `ADMIN_CALLERS` | | `{"events": […]}` |
| `POST /admin/reflect` | `ADMIN_CALLERS` | `{"person"?, "date"?}` | the reflection summary |
| `POST /admin/turn` | `ADMIN_CALLERS` | `{"channel", "text", "person"?, "framing"?}` | `{"conversation_id", "text"}`. The reply is not delivered. |
| `POST /admin/inference/check` | `ADMIN_CALLERS` | none | `{"ok": bool, "jobs": [{"job", "endpoint", "model", "ok", "seconds", "error"?}]}`, one row for each `inference` job. Jobs with the same server, model, and key share one call. `error` has no key and no token, and is 200 characters at most. |
| `GET /admin/usage?days=` | `ADMIN_CALLERS` | | the same answer as `GET /v1/usage` |
| `GET /admin/snapshots` | `ADMIN_CALLERS` | | `{"snapshots": [{"name", "server", "tool", "scope", "every", "fetched_at", "ok", "failures", "last_error", "items", "listed"}]}`, no text |

`POST /v1/turns` answers `202` at once and runs the turn in the background. The
reply reaches the person through `POST /v1/deliver`. A second turn with the
same `message_id` inside 15 minutes is a duplicate. Core refuses new turns with
`429` when more than `2 × core.pool_max` turns run at once.

### The search route

`POST /v1/memory/search` searches the index by meaning. `query` is 1 to 1000
characters. `limit` is 1 to 25, default 10. `kinds` is a list of `wiki`,
`journal`, and `profile`; empty or absent means all three. Any other kind gets
`422`.

The search is always in the shared scope, and the body names no person. It
reads the `files` source only, and returns only a page under `wiki/`: a wiki
page, a journal entry or day page, or a profile. It never returns a person's
own attachments, a row from another source, or a taught skill. `path` is
relative to the data volume. `date` is `YYYY-MM-DD` or null.

`source` is `files` (the default) or `documents`. Any other value gets `422`.
A search of `documents` must name `person`, a person id; without it the
route answers `400`. It reads the `documents` source only, and returns the
documents of that person and the documents of `documents/shared/`, never the
documents of another person. A hit has `kind: document`, the path
`documents/<owner>/<slug>.md`, the kind and the status of the document in
`heading` (`plan, active`), and the summary in `text`, never the body. When
`person` is not a member, or `memory.documents.enabled` is `false`, the
results are empty. `kinds` does not apply. The caller vouches for the person:
the viewer sends the member who signed in. An empty list in
`allowed_callers` turns the route off: every caller gets `403`. Core never
logs the query.

### The usage route

`GET /v1/usage` gives the tokens of the model calls, for each calendar day in
the instance `timezone` and for each group. The groups are `chat` (the turns
of the agent), `reflection` (the nightly reflection), `internet` (a lookup
that the agent makes with `use_internet`), `describe` (the attachment
describer), and `assignment` (the model calls of the assignments container).
`days` is the window, today included: default 30, held to 1 to
366.

```json
{"timezone": "America/New_York", "days": 30,
 "groups": ["chat", "reflection", "internet", "describe", "assignment"],
 "rows": [{"date": "2026-10-20",
           "groups": {"chat": {"calls": 12, "input": 900, "output": 400,
                               "cache_read": 52000, "cache_write": 3100,
                               "total": 56400, "models": ["claude-opus-5"],
                               "workers": {}}}}],
 "totals": {"chat": {"calls": 12, "...": "..."},
            "reflection": {"calls": 0, "...": "...", "models": [], "workers": {}}}}
```

`groups` always holds every feature, also one with no usage: `chat`,
`reflection`, `internet`, `describe`, and `assignment`, in that order. After them come the
other names of the rows in the window, sorted. `totals` holds one entry for
each name of `groups`, with zero counts for a group with no usage. `rows` is
newest first and holds only a day with usage, and a day holds only the groups
with usage that day. `total` is input, output, cache read, and cache write,
added. `models` is the sorted list of the distinct models of the calls of a
group, and `[]` when no call named one. `workers` gives the same counts and
`models` for each worker of a group whose rows name a worker, and is empty for
the other groups. The answer holds no person, no message, and no question. An
empty list in `usage.allowed_callers` turns the route off: every caller gets
`403`. `GET /admin/usage` gives the same answer to an admin caller.

The group `assignment` holds the model calls of the assignments container.
Core does not make these calls itself, so the container reports them with
`POST /v1/usage`.

### The usage write route

`POST /v1/usage` writes usage rows that another container reports for its
own model calls. The callers are `usage.writers` (default `assignments`). An
empty list turns the route off: every caller gets `403`. Core checks the
bearer before it reads the body.

```json
{"rows": [{"kind": "assignment", "name": "assignment",
           "worker": "assignment-12", "run_id": 12, "person_id": "alex",
           "model": "claude-sonnet-5", "endpoint": "anthropic", "calls": 3,
           "usage": {"input": 100, "output": 20, "cache_read": 1000,
                     "cache_write": 50},
           "ref": "12:slice:1"}]}
```

| Field | Rule |
|---|---|
| `kind` | `assignment` only. A row of `chat`, `reflection`, `internet`, `describe`, or any other kind gets `400`. Only core writes those. |
| `name` | `assignment`, the default. It is the group on the usage page. |
| `worker` | optional, at most 64 characters. The usage page shows it as a worker of the group, for example one assignment. |
| `run_id` | optional, a positive integer, for example the id of the assignment. |
| `person_id` | optional, a person id. |
| `model`, `endpoint` | optional labels: the model and the server label of the calls. |
| `calls` | default 1, 0 or more. |
| `usage` | the four counts, each 0 to 2147483647, default 0. |
| `ref` | required, 1 to 200 characters. The id of the row for its writer. |

The body holds at most 500 rows and 1 MiB. A larger body gets `413`. When one
row is not valid, core writes no row and answers `400`. Core stores the `ref`
as `<writer>:<ref>`, for example `assignments:12:slice:1`. Thus a writer
cannot change the rows of core or of another writer, and a row that the
writer sends again is not written again. The answer `{"accepted": n}` counts
the rows of the body, also a row that core already had. When the database
fails, the answer is `503` and the writer can send the same rows again. Core
logs the writer and the number of rows, never a row.

### The model forwarder

`ANY /v1/model/{path}` sends a Claude API request on to the model server, with
the credential of the inference job `assignment`. The assignments container
holds no model credential. Its Claude CLI sets:

```
ANTHROPIC_BASE_URL=http://core:8000/v1/model
CLAUDE_CODE_OAUTH_TOKEN=<JOSHUA_TOKEN_ASSIGNMENTS>
```

The CLI sends its fleet token as a Claude token. Core compares it with the
tokens of `inference.forwarder.allowed_callers` only. A missing token, a
wrong token, and the token of any other identity, also `core`, get `401`. An
empty list turns the route off: every request gets `404`.

Core sends a request on only for these paths:

- `v1/messages`
- `v1/messages/count_tokens`
- `v1/models`
- `v1/models/<id>`, one more segment with no dot segment

Any other path gets `403`, and core sends nothing. Core answers
`GET` or `HEAD` `/v1/model/api/hello` itself with `{}` and no bearer: it is the
connectivity probe of the CLI.

The server is the server of the `assignment` job: `inference.assignment`,
else the global `inference` settings, else the Anthropic API with the
subscription token. Core removes `Authorization`, `x-api-key`, `Host`, and
the hop-by-hop headers. Then it adds the credential of the job:

| The job uses | Core sends |
|---|---|
| the subscription | `Authorization: Bearer <subscription token>` |
| an API key with no `base_url` | `x-api-key: <key>` |
| a `base_url` and a key | `Authorization: Bearer <key>` |
| a `base_url` with `api_key: ""` | no credential |

With a credential that is not the subscription token, core also removes the
`oauth-*` flags from `anthropic-beta`, because the CLI sets them for its
stand-in token. Core passes the other headers and the query on, and does not
change the body. Thus the model of a call is the model that the caller asks
for. The body streams in both directions, so a server-sent event stream
arrives at the caller as the server sends it. The status and the headers of
the server come back as they are.

The forwarder writes no usage row: the container reports its own usage with
`POST /v1/usage`. Core never logs a token, a header value, or a body. When the
server does not answer, the reply is `502 {"error": "model server
unreachable"}`, and the log names the error type and the server label, never
the error text, because the error text can name a host. With no subscription
token for a job on the subscription, the reply is `503`.

### The limits route

`GET /v1/limits` tells how much of the Claude subscription limits is used. It
has no bearer, as `/healthz`. Core is not exposed outside the cluster
(`http://<core service>:8000/v1/limits`), and on compose it is on the host at
`127.0.0.1:8081` only (`http://127.0.0.1:8081/v1/limits`). The answer holds
percentages and reset times only: no person, no message, and no token.

```json
{"available": true, "reason": null,
 "observed_at": "2026-11-07T20:00:00-05:00",
 "five_hour": {"used_pct": 31.0, "left_pct": 69.0,
               "resets_at": "2026-11-07T22:30:00-05:00", "status": "allowed",
               "reset": false},
 "seven_day": {"used_pct": 47.0, "left_pct": 53.0,
               "resets_at": "2026-11-09T20:00:00-05:00", "status": "allowed_warning",
               "reset": false}}
```

The numbers come from the rate-limit report of each model call on the
subscription. Core keeps the last report. It makes no request to get the
numbers, and no parameter changes the answer. A call on another inference
server does not report the limits.

- `available` is `false` when core knows nothing yet. `reason` then says why.
- `observed_at` is the time of the model call that gave the numbers.
- `used_pct` and `left_pct` are 0 to 100. `resets_at` and `observed_at` are
  ISO 8601 with the offset of the instance `timezone`, or `null`.
- `status` is the word that the model call gave: `allowed`,
  `allowed_warning`, or `rejected`, or `null`.
- `reset` is `true` when `resets_at` is in the past at the time of the
  request. The window reset after the last model call, so its numbers are old
  until the next call.

Core keeps the last snapshot in the `limits_snapshot` table, so a restart
keeps it.

### The stream

`POST /v1/turns/stream` and `POST /v1/cli/turns/stream` answer with
`text/event-stream`. Each frame is `event: <name>` and one `data:` line of JSON.

| Event | Data |
|---|---|
| `delta` | `{"text": "<chunk>"}` |
| `done` | `{"turn_id", "text", "tools"}`, the full text and the tools used |
| `error` | `{"message": "<what failed>"}` |

The `delta` frames carry only the answer, and together they are the text of
`done`. The text that the model writes before it calls a tool, such as "Not
on the list, adding it", is not sent. Core holds the text of each model
message until the message ends, so the answer arrives one message at a time.

### The voice route

`POST /v1/voice/chat/completions` speaks the OpenAI chat-completions protocol,
so a voice front end reaches Joshua with a base URL and a bearer token. Point an
OpenAI client at `<channels>/v1/voice` and it works.

The request is an ordinary OpenAI body. Joshua reads four fields and ignores the
rest (`model`, `temperature`, `max_tokens`, and so on):

| Field | Meaning |
|---|---|
| `messages` | The last `user` message is the spoken turn. Joshua holds its own history, so the earlier messages are not read. |
| `user` | The device: `voice:<device>` or the bare `<device>`. It becomes the room in the turn's framing. Default `default`. |
| `speaker` | Who the front end thinks is speaking: `person:<name>` or `<name>`. Joshua matches the name against `people[].handles.voice`. |
| `speaker_confidence` | The score for that name, 0 to 1. Below `channels.voice.min_confidence` the name is not trusted. |
| `stream` | `true` for SSE chunks, `false` for one completion object. |

The reply is OpenAI-shaped. In stream mode the first chunk carries
`delta.role`, each following chunk carries `delta.content`, the last carries
`finish_reason: "stop"`, and the body ends with `data: [DONE]`. Core's `delta`
frames become content chunks. Core's `done` frame ends the reply, and it sends
that frame's text only when no delta arrived. Core's `error` frame becomes one
short spoken sentence. The cause goes to the log and never to the speaker.

Two markers ride inside the reply **text**, and the front end removes them
before it speaks:

| Marker | The front end does |
|---|---|
| `[DONE]` | End the call. The conversation is over. |
| `[IGNORE]` | Speak nothing, stay open. The words were not addressed to Joshua. |

They are not the SSE terminator `data: [DONE]`, which ends the HTTP body of
every streamed reply.

A speaker Joshua cannot identify never reaches core. `channels.voice.unknown_sender`
picks what they hear: `drop` answers `[IGNORE]`, `reply` answers one sentence
that ends the call. Either way the refusal is in `GET /admin/guard/recent` with
the name the front end sent, so an operator can add that person.

## gateway

Port 8000 in the container. Compose does not publish it. Only the containers
reach it.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok": bool, "connected", "errored", "installing", "disabled", "total"}`, counts only. Always `200`; `ok` reports the upstreams, not readiness. `installing` is a package install still running; `disabled` is an entry whose credential is empty. Neither is an error. |
| `ANY /<server>` and `/<server>/…` | `gateway.person_header_callers` (default `core`, `assignments`) | MCP over streamable HTTP | the upstream's answer. `403` when the person may not use this server. `409` when a live session changes person. `503` when the upstream is down and not reconnecting. See below for a lost upstream session. |
| `POST /admin/reload` | `ADMIN_CALLERS` | `{"server": name}` or empty | `{"reloaded": {…}, "failed": {…}}`, `502` when one failed |
| `GET /admin/calls?identity=&tool=&limit=` | `ADMIN_CALLERS` | | `{"calls": […]}`, newest first |
| `GET /admin/inventory` | `ADMIN_CALLERS` | | `{"servers": {…}, "identities": {…}}`. Each connection has `reconnects`, `last_reconnect_at`, and `last_reconnect_reason`. A `package` entry adds `package`, `resolved`, `installed_at`; a disabled entry adds `disabled_reason`. |
| `POST /v1/snapshots/{name}` | `core` | empty | `{"ok", "text", "is_error", "fetched_at"}` (below). `404` for a name that is not a gateway snapshot, `403` for a refused tool, `503` when the upstream is down, `504` after 30 seconds. |
| `POST /v1/search` | `core` | `{"query": str}`, 1 to 500 characters | `{"results": [{"title", "url", "snippet"}]}` (below). `400` for a bad body, `404` when `internet.search` is not set or the internet agent is off, `502` when SearXNG refuses, fails, or does not answer JSON, `504` after `internet.search.timeout_seconds`. |
| `POST /admin/mcp/install` | `ADMIN_CALLERS` | `{"server": name, "reinstall": bool}` | `{"server", "package", "resolved", "installed_at", "reconnected": […]}`. `404` for a server with no `package`, `502` with a one-line `error` when the install failed. |

When an upstream loses its session, core's session to the gateway continues.
The gateway connects to the upstream again:

- A call that the upstream refused with "Session not found" did not run. The
  gateway sends it once more, and core gets only the second answer.
- A call that was in flight when the transport closed can have run. The
  gateway sends a list or a read once more, but not a `tools/call`.
- A request that arrives during the reconnect waits for up to 10 seconds.
- A `tools/call` that fails is a tool result with `isError: true`, never a
  JSON-RPC error. The text says what to do, for example
  `upstream health is reconnecting; try again`.
- A list or read that fails is a JSON-RPC error with code `-32603`
  (`INTERNAL_ERROR`). The gateway never passes on the upstream's own code: a
  code such as `-32600` would tell core that its request was bad, and HTTP
  then answers `400`.

### The snapshot route

`POST /v1/snapshots/{name}` runs one snapshot of `memory.snapshots` for core.
The body is empty. The gateway reads the server, the tool, the arguments, and
the scope from its own copy of `joshua.yaml`, so the config is the allowlist.
It refuses with `403`, and writes a `denied` row to the call log, in these
cases:

- The person of the scope may not use the server (`person_not_allowed`).
- The tool filter hides the tool, or the upstream does not declare it
  (`tool_not_exposed`).
- The `class` map marks the tool `write-local` or `act`
  (`tool_class_write-local`, `tool_class_act`).
- The upstream declares the tool with `destructiveHint: true` and not
  `readOnlyHint: true` (`tool_destructive`).

Otherwise the gateway calls the tool one time. It uses the upstream instance
of the person of the scope, as the MCP route does.

`text` joins the text parts of the result with a newline. `is_error` is the
flag of the tool. `ok` is true when the call ran and `is_error` is false.
`fetched_at` is UTC, in ISO 8601. The call log holds the call with the
snapshot name and no content.

### The web search route

`POST /v1/search` runs the SearXNG search of `internet.search` for the
internet agent in core. The body holds the query and nothing else. The gateway
calls `GET <url>/search` with `q=<query>`, `format=json`, and `categories` and
`language` when they are set. It sends `Authorization: Bearer <api_key>` only
when `api_key` is set. The URL, the parameters, and the key come from the
config of the gateway, never from the caller.

The gateway reads `results` of the SearXNG answer, and takes `url`, `title`,
and `content` (as `snippet`) of each result. A result has a `title` of at
most 300 characters, an `http` or `https` `url`, and a `snippet` of at most
1000 characters. One URL is kept once, and the list has at most
`internet.search.max_results` results.

An error is a short fixed text, never the text of SearXNG:

| `error` | Status | Cause |
|---|---|---|
| `searxng refused the json format (403): ...` | `502` | SearXNG answered `403`: `json` is not in `search.formats` of its `settings.yml`, or a proxy refused the key |
| `searxng refused the key (401)` | `502` | a proxy refused the key |
| `searxng answered <status>` | `502` | another status that is not `200` |
| `searxng answer is not json`, `searxng answer has no results list` | `502` | the answer is not a SearXNG JSON answer |
| `searxng unreachable: <exception>` | `502` | the connection failed |
| `timeout` | `504` | no answer in `timeout_seconds` |

The call log holds the length of the query and the count of the results,
never the query and never the key.

`core` sends these headers with every MCP request. The assignments container
sends them for the person who started the assignment:

| Header | Meaning |
|---|---|
| `X-Joshua-Person` | the person id, or `unknown` |
| `X-Joshua-Conversation` | the conversation id |
| `X-Joshua-Role` | `member` or `guest` |
| `X-Joshua-Group` | the group id, only for a group chat in `groups` |

The gateway trusts these headers only from the identities in
`gateway.person_header_callers` (default `core` and `assignments`). Every other
identity gets `403` on an MCP route. A group id that is not in `groups` is
400.

## assignments

Port 8000 in the container. Compose does not publish it. The gateway calls
`/mcp` for a person. The viewer calls the read API. The container is off unless `assignments.enabled` is
true and its profile runs. [assignments.md](assignments.md) has the tools.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok", "database", "channels", "people", "draft", "running", "waiting", "paused_limits"}`: booleans and counts only. `503` when the database does not answer. |
| `ANY /mcp` | a bearer in `ASSIGNMENTS_TOKENS` or `ADDON_TOKEN` | MCP over streamable HTTP | the seventeen tools. `start_assignment` takes a slot at once or is refused with the reason `busy`; there is no queue. A refusal of `start_assignment` has `reason`, `message`, and `hint`, and where it applies `retry_after_s`, `running` (`{"id", "title"}`; another person's run has a null id), or `drafts`. A spec alone is refused as `drafts_open` while the person has drafts, unless `new` is true. `cancel_assignment` returns when the slot is free. `delete_assignment` hides a draft, and `restore_assignment` brings it back. `grant_repo` and `provide_file` answer a request of a worker for a repository or a file. `401` for a missing or wrong bearer, and for a fleet token. |
| `GET /v1/assignments?person=&state=&limit=` | `assignments.read_callers` (default `viewer`) | none | `{"items": [item, ...]}`, the newest first. Each item has the counts, the state, `lede`, `next_step`, `open_question`, and `open_request` (`{"kind", "ref", "reason"}` or null). `state` is `draft`, `in_progress`, or `completed`. `limit` 1 to 200, default 50. |
| `GET /v1/assignments/{id}?person=` | the same | none | one assignment: item plus spec, definition, comments, open_comments, plan, report, outputs, checkpoints (newest first, at most 200), summary, follow-ups, answer, error, granted_repos, inbox |
| `GET /v1/saved?person=` | the same | none | `{"items": [saved_item, ...]}`, the newest first, each with `open_comments` |
| `GET /v1/saved/{id}?person=` | the same | none | one saved assignment plus `definition`, `comments`, and `runs_list` (newest first, at most 50; a draft is not a run) |
| `GET /v1/assignments/{id}/comments?person=`, `GET /v1/saved/{id}/comments?person=` | the same | none | `{"items": [comment, ...]}`, the oldest first. Each comment has `target`, `line_now`, and `stale`, computed against the text of its target. |
| `POST /v1/assignments/{id}/comments?person=`, `POST /v1/saved/{id}/comments?person=` | the same | `{"line_no", "line_text", "body"}`, and `target` (`definition`, the default, `plan`, or `report`; a saved assignment takes `definition` only) | `201` and the comment, written as `person`. `400` for a bad value or target, `409` past 200 open comments. A comment on the plan or the report of a completed assignment goes to channels `POST /v1/events` as one event for the comments of two minutes. |
| `DELETE /v1/assignments/{id}/comments/{comment_id}?person=`, `DELETE /v1/saved/{id}/comments/{comment_id}?person=` | the same | none | `204`. Only an open comment of `person`: `404` for another person's, `409` for a resolved one. |

The read API takes only the fleet token of an identity in
`assignments.read_callers`, compared with its `JOSHUA_TOKEN_<NAME>`. A
missing or wrong bearer is `401`. `person` is required (`400`). A row of
another person is `404`, and so is a comment of another person. A deleted
draft is `404`, and no list holds it.
[assignments.md](assignments.md#the-read-api) has the shapes.

The git proxy is internal. It listens on `127.0.0.1` on a free port, in the
manager process, and no other container can reach it. Only the git of a
session calls it, through the `.gitconfig` that the runner writes in the
`HOME` of the session.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /<host>/<repo>/info/refs?service=git-upload-pack` | `Authorization: Bearer <secret of the session>` | none | the reply of `https://<host>/<repo>.git/info/refs`, with the token of the host on the request |
| `POST /<host>/<repo>/git-upload-pack` | the same | the git request | the reply of the host |

Every other request gets `403`: a missing or wrong secret, a repository that
is not in the spec or the granted repositories (`grant_repo`), or not in
`assignments.repos`, a push
(`git-receive-pack`), a missing token, and any other path. A `401` or a
redirect from the host is `403` or `502`, so git never asks for a password.

These bearers are not fleet tokens. `ASSIGNMENTS_TOKENS` holds
`<person-id>=<token>` pairs, and a bearer names one person. The gateway sends
it from the `identities` of the `assignments` entry under `mcp:`. The
container compares each bearer with `hmac.compare_digest`. A tool call with an
`ADDON_TOKEN` bearer gets `403 forbidden: this tool needs a person` from every
tool except `assignment_status` and `list_assignments`. A person who is not a
member gets `403 forbidden: assignments are for members only`. Another
person's assignment is `404 not found: no assignment <id>`.

The container calls these routes of the other containers, each with
`Authorization: Bearer <JOSHUA_TOKEN_ASSIGNMENTS>` (the limits route has no
bearer):

| Route | When | Body |
|---|---|---|
| channels `POST /v1/events` | the report, the question, or the request of an assignment, and the comments of the person on the plan or the report of a completed one | `{"destination": str, "text": str}`. Channels accepts it only when `assignments` is in `channels.webhooks.allowed_callers`. |
| core `GET /v1/limits` | before each slice, and at `start_assignment` when `when_unknown` is `refuse` and the spec has no `budget.tokens` | none |
| core `POST /v1/usage` | after each slice that used tokens | one row: kind and name `assignment`, `worker` the model name of the spec, `run_id` the id, `person_id`, `model`, `calls`, `usage`, and `ref` `<id>:slice:<n>` |
| core `ANY /v1/model/{path}` | each model call of a session, from the Claude CLI | a Claude API request |
| gateway MCP routes | each MCP call of a session | MCP, with `X-Joshua-Person` the person, `X-Joshua-Role` `member`, and `X-Joshua-Conversation` `assignment:<id>` |
| a git host, `https://<host>/<repo>.git/...` | a clone or a fetch of a session, through the git proxy | the git request, with HTTP Basic: the token of `assignments.platforms.<host>.token_env`. Never a push. |

A usage row that fails with a connection error, a timeout, or a `5xx` is sent
again, 3 attempts in all. Core keeps one row for each `ref`, so a row that
arrives two times counts one time.

## Where `laptop` may go

The `laptop` identity is for the operator on the host. It may call
`POST /v1/events`, the two `/v1/cli/*` routes, every `/admin/*` route (by
default), and `POST /v1/turns/stream` with a `cli` handle.

It may not call the gateway MCP routes. Only the identities in
`gateway.person_header_callers` (default `core` and `assignments`) reach a
person's tools and files, and only they say which person a request belongs
to. It may not call
`/v1/turns`, `/v1/deliver`, or `/v1/channels/resolve` either. Those belong to
the containers.
