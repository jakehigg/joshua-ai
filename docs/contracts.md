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
| `message_id` | string | `null` | idempotency key, with `channel` and `chat.id`, kept for 15 minutes |
| `event_type` | string | `null` | names a skill (events only) |
| `payload` | object | `null` | event data (events only) |
| `options` | object | `null` | `verbatim: true` records the text with no model call |
| `framing` | string | `null` | replaces the default event framing |
| `held_take` | string | `null` | the ticket of a hold take. Core redeems it once and adds the files of the taken messages to `attachments`. |

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
| `POST /v1/events` | `channels.webhooks.allowed_callers` | see `channels.md` | `202 {"accepted": true}`, `200 {"ignored": true}`, `400` (also `invalid_workspace`), `404 unknown_channel`, `404 unknown_workspace` (core's reason, for a `workspace` core does not have), `502`, `503 core_unavailable`. The optional `workspace` (a UUID) with `text` sends the event to the thread of that workspace and to no adapter; with `verbatim` core records the text there. |
| `POST /webhook/imessage/{secret}` | none, the path secret | BlueBubbles payload | always `200`. A wrong secret gets `404`. |
| `POST /v1/cli/turns/stream` | `channels.webhooks.allowed_callers` | `{"person": id, "text": str}`, or `multipart/form-data` (below) | SSE, or `400`, `403 {"reason": <guard reason>}`, `503 core_unavailable`, `503 pipeline_unavailable` |
| `GET /v1/cli/outbox?person=&ack=true` | `channels.webhooks.allowed_callers` | | `{"messages": [{"ts", "text", "attachments"}]}`, `403 unknown_person` |
| `GET /v1/cli/transcript?person=&limit=&before=` | `channels.webhooks.allowed_callers` | | core's `GET /v1/transcript` reply and status for `cli:<person>`, `400 bad_request`, `403 unknown_person`, `503 core_unavailable` |
| `GET /v1/cli/held?person=&workspace=` | `channels.webhooks.allowed_callers` | | core's `GET /v1/holds` reply for `cli:<person>`, or for the workspace thread, `403 unknown_person`, `503 core_unavailable` |
| `POST /v1/cli/held` | `channels.webhooks.allowed_callers` | `{"person", "text", "workspace"?}`, or `multipart/form-data` with one `file` part (below) | core's `POST /v1/holds` reply, `400 bad_request` for an empty text with no file, `403 unknown_person`, `503 core_unavailable`, `503 pipeline_unavailable` |
| `POST /v1/cli/held/take` | `channels.webhooks.allowed_callers` | `{"person", "workspace"?}` | core's `POST /v1/holds/take` reply, `403 unknown_person`, `503 core_unavailable` |
| `DELETE /v1/cli/held/{hold_id}?person=&workspace=` | `channels.webhooks.allowed_callers` | | core's `DELETE /v1/holds/{hold_id}` reply, `403 unknown_person`, `503 core_unavailable` |
| `GET /v1/cli/draft?person=&workspace=` | `channels.webhooks.allowed_callers` | | core's `GET /v1/drafts` reply for `cli:<person>`, or for the workspace thread, `403 unknown_person`, `503 core_unavailable` |
| `POST /v1/cli/draft` | `channels.webhooks.allowed_callers` | `{"person", "text", "workspace"?}` | core's `POST /v1/drafts` reply, `403 unknown_person`, `503 core_unavailable` |
| `POST /v1/voice/chat/completions` | `channels.voice.allowed_callers` | OpenAI chat completions, plus `speaker` and `speaker_confidence` | SSE of `chat.completion.chunk`, or one `chat.completion` for `stream: false`. `400 bad_request`, `404 not_found` with no voice section, `503 core_unavailable` |
| `GET /v1/models` | `channels.voice.allowed_callers` | | `{"object": "list", "data": [{"id": "joshua", …}]}`, `404 not_found` with no voice section |
| `GET /admin/guard/stats` | `ADMIN_CALLERS` | | counters: `unknown_sender`, `rate_limited`, `truncated`, `refused_total` |
| `GET /admin/guard/recent` | `ADMIN_CALLERS` | | `{"recent": [{"channel_type", "address", "chat_id", "reason", "at"}]}` |
| `GET /admin/chats/unconfigured` | `ADMIN_CALLERS` | | `{"groups": [{"channel_type", "chat_id", "chat_title", "at"}]}` |

An error reply from `channels` is `{"reason": "<code>"}`, with a `detail` field
when there is more to say.

### A terminal turn with a file

`POST /v1/cli/turns/stream` also takes `multipart/form-data`, so a client can
send a file with the message:

| Field | Times | Meaning |
|---|---|---|
| `person` | one | the person id, as in the JSON body |
| `text` | one | the message. It can be empty when the turn carries a file. |
| `file` | zero or more | one file part, with its filename |

Channels writes every part into `/data/inbox/cli-<uuid>/` and gives the
directory to the attachment pipeline. The pipeline sniffs the type from the
bytes, keeps an image, a PDF, or a text file, and stores it under
`people/<person>/attachments/YYYY/MM/`. The turn carries one `Attachment` per
stored file, so `core` gets paths and never bytes.

The pipeline skips a part that is not an image, a PDF, or a text file, and the
turn still goes through. It skips a part over
`channels.limits.max_attachment_bytes` as well. A turn with a file and no text
carries the same placeholder text as a file with no caption on any other
channel.

With no attachment pipeline, a turn with a file gets
`503 pipeline_unavailable`. The JSON body takes no file.

## core

Port 8000 in the container. Compose publishes it on the host as
`127.0.0.1:8081`, so only this host reaches it.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok": bool, "checks": {"db": bool, "layout": bool, "embed": bool}, "inference_overrides": int}`. `200` when ready, `503` when not; `embed` does not hold `ok` down. `inference_overrides` counts the `inference` job blocks that set `base_url` or `model`. |
| `POST /v1/turns` | `channels` | TurnEvent | `202 {"accepted": true, "turn_id": id}`, `202 {"accepted": true, "folded": true}` when a turn of the conversation runs and the message joins the next one, `200 {"accepted": false, "reason": "duplicate"}`, `403 unknown_sender`, `429 overloaded` |
| `POST /v1/turns/stream` | `channels`, and `laptop` for a `cli` handle | TurnEvent | SSE (below), `403 unknown_sender`, `400 {"reason": "unknown_hold_take"}` for a `held_take` that is unknown, used, or of another thread |
| `GET /v1/transcript?channel=&person=&limit=&before=` | `channels`, `laptop` | | `{"conversation_id", "messages", "next_before"}` (below), `400 bad_request` with no `channel`, `422` for a bad `limit` or `before` |
| `GET /v1/holds?channel=&person=` | `channels`, `laptop` | | `{"held": [{"id", "text", "at", "attachments"?}], "dropped": [...]}`, oldest first. An `attachments` entry is `{"name", "mime"}`, never a path. |
| `POST /v1/holds` | `channels`, `laptop` | `{"channel", "person", "text", "attachments"?}` | `{"held": [...], "dropped": [...]}`, `400` for an empty text, `429 {"reason": "hold_full"}` past 20 messages |
| `POST /v1/holds/take` | `channels`, `laptop` | `{"channel", "person"}` | `{"text", "held": [...], "dropped": [...]}`, plus `take_ticket` when a taken message carried a file. It empties the hold and joins the messages, in order, one per line. |
| `DELETE /v1/holds/{hold_id}?channel=&person=` | `channels`, `laptop` | | `{"held": [...], "dropped": [...]}`, `404 {"reason": "unknown_hold"}` |
| `GET /v1/drafts?channel=&person=` | `channels`, `laptop` | | `{"text", "at"}`, the unsent text of that person's composer in that thread, or `{"text": "", "at": null}` |
| `POST /v1/drafts` | `channels`, `laptop` | `{"channel", "person", "text"}` | `{"text", "at"}`, or `{"text": "", "at": null}` for an empty text, which clears the draft |
| `POST /v1/memory/search` | `memory.search.allowed_callers` (default `viewer`, `mcp`) | `{"query", "limit"?, "kinds"?, "source"?, "person"?}` | `{"results": [{"path", "title", "heading", "text", "kind", "score", "date"}]}`, `422` for a bad body, `503` when the embedding model is not loaded |
| `GET /v1/usage?days=` | `usage.allowed_callers` (default `viewer`) | | `{"timezone", "days", "groups", "rows", "totals"}` (below), `422` when `days` is not an integer |
| `GET /v1/usage?group=conversation&person=` | `usage.allowed_callers` (default `viewer`) | | `{"days", "conversations": [{"conversation_id", "workspace": {"slug", "title"} or null, "calls", "input", "output", "cache_read", "cache_write"}]}`, largest first. Only the conversations of `person`. A row with no conversation is not listed. `422` for another `group` or no `person`; `403` for a person who is not a member. |
| `/v1/workspaces` | `workspaces.allowed_callers` (default `viewer`, `gateway`, `assignments`) | `person` query parameter | See [workspaces.md](workspaces.md#routes). |
| `POST /v1/workspaces/{slug}/read` | `workspaces.allowed_callers` | `person` query parameter, `{"turn_id"}` | `{"conversation", "last_read_turn_id", "last_read_at", "unread"}`. It marks the thread of that workspace read, for that person alone, up to the id of a transcript row. The mark never moves back. `404` for a workspace the person does not reach, `422` for a missing or negative `turn_id`. `GET /v1/workspaces` carries the `unread` count of each workspace for that person: the rows after their mark that somebody else wrote, Joshua or another person of the thread. A row of their own never counts. |
| `GET`, `POST /v1/workspaces/{slug}/notify` | `workspaces.allowed_callers` | `person` query parameter, `{"mode"}` on the `POST` | `{"mode", "default"}`, the notify mode of that person in that workspace and the default of the workspace. `mode` is `off`, `decide`, or `all`. `404` for a workspace the person does not reach, `422` for another mode, a missing one, or an unknown field. |
| `POST /v1/usage` | `usage.writers` (default `assignments`, `developer`) | `{"rows": [...]}` (below) | `{"accepted": n}`. `400` for a bad body or a row that is not of kind `assignment` or `developer`, `413` over 1 MiB, `503` when the database fails. |
| `POST /v1/history/search` | `assignments` | `{"person", "workspace"?, "query", "limit"?}` | `{"messages": [{"at", "direction", "speaker", "text", "cut"}]}`, newest first. The chat history of a worker (below). `400` for a bad body. |
| `POST /v1/history/read` | `assignments` | `{"person", "workspace"?, "start", "end"?, "cursor"?}` | `{"messages": [...], "cursor"}`, oldest first; `cursor` is the next page or `null`. `400` for a bad body, an `end` at or before the `start`, or a cursor that is not valid. |
| `ANY /v1/model/{path}` | `inference.forwarder.allowed_callers` (default `assignments`) | a Claude API request | the answer of the model server, streamed (below). `401` for any other token, `403` for a path off the list, `404` when the list is empty, `502` when the server does not answer, `503` with no credential. `GET` or `HEAD` `/v1/model/api/hello` is open and answers `{}`. |
| `GET /v1/limits` | open | | the subscription limits (below), `404` when `limits.enabled` is `false` |
| `GET /v1/push/key` | `AUTH_CALLERS` (default `viewer`) | `person` query parameter | `{"key"}`, the public VAPID key a browser subscribes with. `503 {"error": …}` while push is not configured (below). |
| `POST /v1/push/subscribe` | `AUTH_CALLERS` | `person` query parameter, `{"endpoint", "keys": {"p256dh", "auth"}, "user_agent"?}` | `{"subscribed": true}`. The endpoint is unique, so a browser that subscribes again updates its row and starts from no failure. `422` for an `endpoint` that is not an `https` URL, a missing key, or an unknown field. `503` while push is not configured. |
| `DELETE /v1/push/subscribe` | `AUTH_CALLERS` | `person` query parameter, `{"endpoint"}` | `{"removed": true}`. The delete is that person's alone: `404 {"error": "this browser is not subscribed"}` for an endpoint they do not have. `503` while push is not configured. |
| `POST /v1/push/test` | `AUTH_CALLERS` | `person` query parameter, empty body | `{"sent", "failed", "dropped", "held", "error"}`, the counts of that person's browsers. `404` when they have no subscription, `503` while push is not configured. |
| `POST /auth/verify` | `AUTH_CALLERS` (default `viewer`) | `{"person", "secret"}` | `{"ok": bool}`. `ok` is `false` for a wrong secret, for a person with no secret, and for a person who is not on the roster; the answer never says which of the three. |
| `POST /auth/secret` | `AUTH_CALLERS` | `{"person", "secret", "new_secret"}` | `{"ok": true, "person"}`. A person changes their own secret and sends the current one: `403 {"error": "wrong secret"}` for a wrong one and for a person who has none yet, `404` for a person who is not on the roster, `400 {"error": "an auth secret cannot be empty"}` for a `new_secret` of whitespace alone. |
| `POST /auth/secret/admin` | `AUTH_CALLERS` | `{"person", "new_secret"}` | `{"ok": true, "person"}`. The secret of a person without the current one, for a first sign-in or a reset. `404` for a person who is not on the roster, `400 {"error": "an auth secret cannot be empty"}` for a `new_secret` of whitespace alone. |
| `DELETE /auth/secret/{person_id}` | `AUTH_CALLERS` | | `{"ok": true, "person", "deleted": bool}`. `deleted` is `false` when that person had no secret. `404` for a person who is not on the roster. |
| `GET /auth/people` | `AUTH_CALLERS` | | `{"people": […]}`, the ids of the people who have a secret. A person with no secret is absent, and no row carries a time. |
| `GET /auth/people/{person_id}` | `AUTH_CALLERS` | | `{"person", "updated_at"}`, when that person's secret last changed. `404 {"error": "no secret"}` for a person who has none, `404` for one who is not on the roster. |
| `GET /people/{person_id}/profile` | `AUTH_CALLERS` | | `{"prompt", "journal", "in_config"}`. `prompt` is the text of the personal instructions of that person to Joshua, or `null` for none. Core reads it from the snippet file at the path the roster holds. `journal` is `auto`, `ask`, or `off`. `in_config` is `true` when `joshua.yaml` also names the person, so an edit overrides the config file. `404` for a person who is not on the roster. |
| `POST /people/{person_id}/profile` | `AUTH_CALLERS` | `{"prompt"?, "journal"?}` | The new profile, in the shape of the `GET`. A person edits only their own entry, and only these two fields. `prompt` is text in and text out. Core writes the text to `<data_dir>/people/<id>/prompt.md` and sets `prompt` in the people file to that path. An empty string or `null` clears `prompt`, writes `""` as the path, and removes the file, and a `prompt` over 4000 characters is `400`. A `journal` outside the three values is `400`, as is an unknown field or an empty body. `404` for a person who is not on the roster. |
| `GET /admin/people` | `ADMIN_CALLERS`, `AUTH_CALLERS` | | `{"people": […]}` |
| `POST /admin/people` | `ADMIN_CALLERS`, `AUTH_CALLERS` | `{"id", "name", "handle"?, "role"?}` | `201 {"person": …}`, `400 {"error": …}`, `409 {"error": "the id <id> is taken"}` for an id the roster holds, and `409` when it takes the role of the only owner. The route adds a person and never overwrites one; the id of a removed person is free again. `handle` is `<type>:<value>`; with none, the person has no handle. |
| `POST /admin/people/{id}/role` | `ADMIN_CALLERS`, `AUTH_CALLERS` | `{"role"}` (`member`, `guest`, or `owner`) | `{"person": …}` with only the role changed, `400 {"error": …}` for another role, `404` for a person who is not on the roster, `409` for the only owner |
| `POST /admin/people/{id}/remove` | `ADMIN_CALLERS`, `AUTH_CALLERS` | | `{"removed": true, "id"}`. The handles and the sign-in secret go; the data dirs and the transcripts stay. `404` for a person who is not on the roster, `409` for the only owner. |
| `GET /admin/settings?prefix=` | `ADMIN_CALLERS`, `AUTH_CALLERS` | | `{"groups": [{"prefix", "label", "settings": [{"key", "label", "hint", "is_secret", "is_set", "value", "updated_at", "updated_by"}]}]}`. `prefix` names one group; with none, every group comes back. `value` is `null` for a secret, set or not, and `is_set` says whether one is stored. `404 {"error": …}` for a prefix no group declares. |
| `POST /admin/settings` | `ADMIN_CALLERS`, `AUTH_CALLERS` | `{"key", "value", "person"?}` | `{"setting": …}`, one field in the shape of the `GET`, so no answer carries a secret value. `400 {"error": …}` for a key no group declares, a `value` that is not a string or is over 4096 characters, an empty value for a secret, or an unknown field. `503 {"error": …}` for a write of a secret with no `JOSHUA_SETTINGS_KEY`; the error names the variable. |
| `GET /admin/update` | `ADMIN_CALLERS`, `AUTH_CALLERS` | | `{"current", "latest", "url", "checked_at", "error"}`. `current` is the release of the running image, `dev` for an image built with no tag. `latest` and `url` are the tag and the page of the latest release of the repository in `admin.update_repo`, read unauthenticated from the GitHub releases API with a five second timeout. Core keeps one answer for six hours, so a load of the page makes no call to GitHub. A check that did not reach GitHub answers a null `latest` and `url`, `error` is `The release check did not reach GitHub`, and `checked_at` is the last check that did reach it, or `null` for none. |
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
same `channel`, `chat.id` and `message_id` inside 15 minutes is a duplicate.
Core keeps these keys in the table `turn_message_seen`, so the rule holds
across a restart of core. Core refuses new turns with
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
describer), `assignment` (the model calls of the assignments container), and
`developer` (the model calls of the developer addon).
`days` is the window, today included: default 30, held to 1 to
366.

```json
{"timezone": "America/New_York", "days": 30,
 "groups": ["chat", "reflection", "internet", "describe", "assignment",
            "developer"],
 "rows": [{"date": "2026-10-20",
           "groups": {"chat": {"calls": 12, "input": 900, "output": 400,
                               "cache_read": 52000, "cache_write": 3100,
                               "total": 56400, "models": ["claude-opus-5"],
                               "workers": {}}}}],
 "totals": {"chat": {"calls": 12, "...": "..."},
            "reflection": {"calls": 0, "...": "...", "models": [], "workers": {}}}}
```

`groups` always holds every feature, also one with no usage: `chat`,
`reflection`, `internet`, `describe`, `assignment`, and `developer`, in that
order. After them come the
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

The group `assignment` holds the model calls of the assignments container,
and the group `developer` those of the developer addon. Core does not make
these calls itself, so each one reports them with `POST /v1/usage`.

### The usage write route

`POST /v1/usage` writes usage rows that another container reports for its
own model calls. The callers are `usage.writers` (default `assignments` and
`developer`). An
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
| `kind` | `assignment` or `developer` only. A row of `chat`, `reflection`, `internet`, `describe`, or any other kind gets `400`. Only core writes those. |
| `name` | optional, the group on the usage page: `assignment` or `developer`. With none, the group is the `kind`. |
| `worker` | optional, at most 64 characters. The usage page shows it as a worker of the group, for example one assignment or one developer task. |
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

### The auth routes

`/auth/*` holds the sign-in secret of a person. Core owns the roster and the
database, so a secret lives here and nowhere else: one bcrypt hash per person
in the `auth_secret` table. The viewer calls these routes to check a secret at
sign-in, and to set, change, or drop one.

`AUTH_CALLERS` (env, a comma list, default `viewer`) names the fleet
identities that may call. It is checked on every route with no exception, the
way `ADMIN_CALLERS` is checked on every admin route: a missing or invalid
bearer is `401`, an identity outside the list is `403`, and an empty list
turns the routes off, so every caller gets `403`.

`person` is a person id of 1 to 64 characters, `secret` and `new_secret` are
at most 256 characters, and an unknown field is `422`. A `new_secret` that is
empty, or whitespace alone, is `400`: such a secret matches no input, so no
route stores one. No answer and no log line ever carries a secret or a hash. `GET /auth/people` carries no time, because
the viewer renders that list on a page with no session, and when each person
last changed their secret stays out of it;
`GET /auth/people/{person_id}` carries the time of one person, because the
viewer stamps its session cookie with it, so a changed secret signs out every
cookie that carries the old stamp.

The profile routes are no admin action. A person edits their own `prompt` and
`journal`, and the viewer calls them for the person who signed in, with the
same `AUTH_CALLERS` check as the routes above. Core writes only the `prompt` and
`journal` keys to the entry of that person in `people.yaml`. That file overlays
the configured entry field by field, so the name, the role, and the handles
stay as they are.

The roster routes are the admin ones: `POST /admin/people` adds a person,
`POST /admin/people/{id}/role` changes a role to `member`, `guest`, or
`owner`, and `POST /admin/people/{id}/remove` takes a person off the roster
and drops their handles and their secret with them. The viewer's Admin → Users
section calls those three and `POST /auth/secret/admin` for the owner, as the
identity `viewer`. The roster routes admit the callers of `AUTH_CALLERS` as
well as those of `ADMIN_CALLERS`, so the viewer needs no admin setting, and it
gets no other admin route. A change that would leave the roster with no owner
is `409`, and so is an add of an id the roster already holds.

### The settings routes

`/admin/settings` holds the settings of the instance: one row of the
`settings` table per setting, named by a dotted prefix such as
`push.vapid_public_key`. Core owns the database, so a setting lives here and
nowhere else. The viewer's Admin → Instance section calls the two routes as
the identity `viewer`, over `GET /api/v1/admin/settings` and
`POST /api/v1/admin/settings`, and serves them to the owner alone. Like the
roster routes, they admit the callers of `AUTH_CALLERS` as well as those of
`ADMIN_CALLERS`.

The write takes a key that a settings group declares, and nothing else: core
holds the groups, and a group says the kind of each of its fields (`text`,
`bool` or `secret`), so a caller can neither write an unknown key nor turn a
secret into a plain value. A field of the answer carries its `kind` beside
`is_secret`. `person` names the writer, for the `updated_by` of the row.

**A value the feature cannot use is `400` here, not an error later.** The
refusal is one line that says what the field takes, and it never carries the
value:

| The key | The refusal |
|---|---|
| `push.vapid_subject` | it must start with `mailto:` or `https://` |
| `push.vapid_public_key` | it must be base64url of 65 bytes that start with `0x04` |
| `push.vapid_private_key` | it must be base64url of 32 bytes |

A write of an unknown key, a value over 4096 characters, and an empty value
for a secret are `400` as before.

**No answer carries a secret value.** A secret is encrypted at rest, a field
answers `is_secret` and `is_set`, and `value` is `null` for it, whether one is
stored or not. The owner replaces a secret by writing a new one; nothing reads
the old one back. [settings.md](settings.md) has the store, the encryption,
and `JOSHUA_SETTINGS_KEY`.

### The release check

`GET /admin/update` says which release the instance runs and which one is the
latest. The viewer proxies it as `GET /api/v1/admin/update`, as the identity
`viewer`, and serves it to the owner alone, beside the settings of the
Instance section. It reports a newer release and nothing else: it offers no
upgrade and runs none.

The running release is in the image: the build writes the release tag into
`JOSHUA_RELEASE`, and core reads it at start. An image built with no tag
reports `dev`.

The latest release comes from the GitHub releases API of the repository in the
setting `admin.update_repo`, `jakehigg/joshua-ai` with none. The call carries
no credential, so it gets the anonymous rate of sixty calls an hour, and it
times out after five seconds. Core keeps the answer, good or failed, for six
hours: a second load inside that window makes no call to GitHub, which keeps
the real rate at a few calls a day and keeps a failed check from retrying. A
value of the setting that is not `owner/name` is refused at the write, and the
default stands.

### The push routes

`/v1/push` holds the web push subscription of a browser and sends one
notification to it. Core owns the database and the VAPID private key, so the
subscriptions live here and core is what posts to a push service. The viewer
proxies the four routes as the identity `viewer`, under
`GET /api/v1/push/key`, `POST /api/v1/push/subscribe`,
`DELETE /api/v1/push/subscribe`, and `POST /api/v1/push/test`, and serves each
one to the person who signed in. They take the same `AUTH_CALLERS` check as
the auth routes.

**The person is the `person` query parameter and never a body field**, so one
person can neither read the key of another nor remove another's subscription.
The viewer adds the `User-Agent` of the request to a subscription, so the
owner can tell two devices apart, and it checks the `Origin` of every write
(`403 {"error": …}`) as the workspace writes do. With no `CORE_URL`, no viewer
token, or a core that does not answer, the viewer's own routes are `503`.

**Every route is `503` while push is not configured**: `push.enabled` is not
`true`, one of the three VAPID settings is missing, or `JOSHUA_SETTINGS_KEY`
is unset, which is what the private key needs to be read. The `error` is the
sentence that says what the owner has to do, so the page can show it.

`POST /v1/push/test` sends the notification through the same sender as every
other push, so its answer says what each browser did: `sent`, `failed`,
`dropped` (the subscription is gone), and `held` (a push service is rate
limiting that endpoint). A send never raises into the turn that caused it.

**A send that could not be signed is `400`, and the `error` is the real
message**, such as `VapidException: Missing 'sub' from claims.` The VAPID
header is built once, before the subscriptions, so a bad subject or an
unreadable private key sends nothing, touches no subscription, and counts
against no browser. A browser that refused the push is still a `200` with
`failed`: that is one device's answer, not a failure of the instance.
[notifications.md](notifications.md) has the sender, the payload, and what the
owner does to turn push on.

### The workspace notify mode

`GET` and `POST /v1/workspaces/{slug}/notify` hold when Joshua may reach one
person of one workspace with the `notify_user` tool: one row of the
`notify_prefs` table per person and workspace, with the `mode` and the moment
that person was last notified of that workspace. A person with no row has the
default of the workspace: `decide` when it is shared, and `off` when it is
personal, where nobody else writes. So the table stays empty until somebody
changes a mode.

**The mode is one person's own.** Every workspace route is scoped by the
`person` query parameter, so a caller reads and writes the mode of that person
and of nobody else. The viewer proxies the two routes as the identity `viewer`,
under `GET` and `POST /api/v1/workspaces/{slug}/notify`, and serves them to the
person who signed in. [notifications.md](notifications.md#when-joshua-may-reach-you-in-a-workspace)
has the setting, and [workspaces.md](workspaces.md#notifying-one-person) has
the tool and the rules it keeps on its own.

### The stream

`POST /v1/turns/stream` and `POST /v1/cli/turns/stream` answer with
`text/event-stream`. Each frame is `event: <name>` and one `data:` line of JSON.

| Event | Data |
|---|---|
| `accepted` | `{"turn_id"}`, sent first: core has the turn and will finish it |
| `status` | `{"state": "queued"}` while another turn of the conversation runs, then `{"state": "running"}` |
| `delta` | `{"text": "<chunk>"}` |
| `done` | `{"turn_id", "text", "tools"}`, the full text and the tools used |
| `error` | `{"message": "<what failed>"}` |

A closed stream does not stop the turn. Core runs the turn in a task of its
own, and `channels` and the viewer read the stream in a task of their own, so
a phone that locks or a tab that closes leaves the turn to finish. The reply
lands in the transcript as the reply of any other turn, and the same delivery
rules apply: a viewer, terminal, or workspace turn is only pulled, so nothing
is pushed to another channel. Only a shutdown of core cancels a turn. The
inference timeout still ends a turn that runs too long.

The `delta` frames carry only the answer, and together they are the text of
`done`. The text that the model writes before it calls a tool, such as "Not
on the list, adding it", is not sent. Core holds the text of each model
message until the message ends, so the answer arrives one message at a time.

### The transcript route

`GET /v1/transcript` reads one conversation back, a page at a time. `channel`
is the channel id, for example `cli:alex`. `person` is the person id. Leave
it out for a group conversation.

The route finds the conversation for that pair and never creates one. An
unknown conversation answers `200` with `conversation_id` null and no
messages.

```json
{
  "conversation_id": "<uuid>",
  "messages": [
    {"id": 41, "direction": "in", "text": "<the message>", "status": "ok",
     "at": "2026-03-04T12:00:01+00:00", "meta": {"turn_id": "t-…"}}
  ],
  "next_before": 41,
  "pending": null
}
```

`pending` is null when no turn of the conversation is in flight. Otherwise it
is `{"turn_id", "state", "since", "text"}`: `state` is `queued` while the turn
waits for another turn of the conversation, and `running` after that. `since`
is when core took the turn, and `text` is the message of the person (empty for
a turn that is not from a person). A client that sees it knows that a reply is
coming, whichever device sent the message. Core keeps the list in memory, so
a restart clears it.

The messages are oldest first. `direction` is the stored value: `in` for a
person's message, `out` for Joshua's reply, `task` for a scheduled task, and
`inject` for an event. `status` is `ok` or `failed`. `at` is the row time in
UTC.

`limit` is 1 to 200, default 50. The page holds the newest `limit` rows with
an `id` less than `before`. `next_before` is the smallest `id` in the page
when older rows exist, else null. Send it as `before` to get the page before
this one.

`meta` holds only these keys: `turn_id`, `tools` (the tool names the turn
used), `call_count`, `duration_ms`, `first_output_ms`, `attachments` (on
the row of a message that came with files: a list of `{path, mime, name,
size}`, where `path` is the filed path under the data volume and `name` is
the name the sender gave), and `speaker` (on the row of a thread several
people write in, a group chat or a shared workspace: the name of the person
who wrote it). Core drops every
other key, for example the token usage, the cost, and the paths
the turn wrote. Core never logs a message.

`GET /v1/cli/transcript?person=` on channels asks core for `cli:<person>`
with the channels token, and returns core's body and status as they are.

### The chat history of a worker

`POST /v1/history/search` and `POST /v1/history/read` give the chat history to
a worker of the assignments container, which has no database of its own. Both
are read-only, create no conversation, and take the fleet token of
`assignments` and no other.

The body names who started the run: `person`, and `workspace` (an id, a slug,
or `<space>/<slug>`) when the run started in a workspace thread. It never
names a conversation. Core turns the two into the conversations of the read,
and the store query takes those ids only:

- a run that a person started reads the conversations of that person, the
  threads of their personal workspaces included;
- a run that a shared workspace started reads the thread of that workspace and
  nothing else. A shared thread belongs to several people and to none of them,
  so it is never in a person's scope;
- an unknown person, or a workspace that the person does not reach, has an
  empty scope, and the answer holds no message.

`query` is the word search of the transcript, `limit` is 1 to 50 (default 10).
A read takes `start` and `end` (default now) and pages with `cursor`. A
message is `{"at", "direction", "speaker", "text", "cut"}`, the row as it is,
so a report may quote a turn verbatim. Core never logs a query or a message.
[assignments.md](assignments.md#the-chat-history) has the rest.

### The hold of a chat thread

A person in the web chat can write while a turn of that thread still runs.
The message is not a second turn: it goes to the **hold** of the thread,
which lives in core (`/v1/holds`), not in the page. A reload or a closed tab
keeps it, and a second screen sees it.

A hold is named by its channel and the person who writes it, so the normal
chat (`cli:<person>`) and each workspace thread (`workspace:<slug>`) hold
their own. `POST` adds one message (up to 20 per thread), `GET` lists them
oldest first, `DELETE` takes one back before it goes, and `POST
/v1/holds/take` empties the hold in one step and answers with `text`: every
held message in order, one per line. The caller then sends that text as one
turn, so the waiting messages are one turn and never several. Because `take`
empties the hold as it reads it, two screens never send the same message
twice. The hold is in memory, as the turns in flight are, so a restart of
core clears it.

A message waits at most `core.hold_max_age_s` (default 900 seconds). Core
drops a message that waited longer, so a page that was away all night never
sends it as if it had just been written. Every hold answer carries `dropped`,
the messages the limit took since the last call, each in the shape of a held
one. A reader sees each dropped message once, and the viewer tells the person
that it was not sent. `0` turns the limit off.

Channels passes these through as `/v1/cli/held` for the person of the body
or the query, and the viewer as `/api/v1/chat/<person>/held`.

### A held message with a file

A held message carries a photo or a PDF, as a turn does. `POST /v1/cli/held`
on channels and `POST /api/v1/chat/<person>/held` on the viewer take
`multipart/form-data` with one `file` part beside `person`, `text`, and the
optional `workspace`. Channels lands the part with the pipeline a turn uses,
so the hold keeps a stored path and never bytes, and a hold with a file and no
text carries the placeholder text of a file with no caption. With no
attachment pipeline, a multipart hold gets `503 pipeline_unavailable`. The
viewer caps the text and the file together at the size a turn may carry, and
answers `413` over it and `422` for a bad form, as its turn route does. A held
message names its files to the browser as `{"name", "mime"}`; a stored path
stays on the server.

The take releases the files against a **ticket**. `POST /v1/holds/take`
answers with `take_ticket` when a taken message carried a file, and the caller
sends that ticket back as the `held_take` of the turn it then sends (the
viewer as `heldTake`). Core keeps the ticket in memory for
`core.hold_max_age_s`, with the channel, the person, and the released files,
and puts the files on the turn when the ticket matches. A ticket is good for
one turn: an unknown, a used, or a foreign ticket is refused with `400
{"reason": "unknown_hold_take"}`, and that turn does not run. The browser
sends a ticket and never a path, and core reads the paths of its own hold
only, so a caller of the chat cannot name an attachment of its own.

The hold serves the web chat, which holds a message it has already sent. On
every other channel core folds instead: a message that arrives while a turn
of that conversation runs joins the next turn, one message per line, and
answers `202 {"accepted": true, "folded": true}`. The fold is per
conversation and per sender, so a group thread never mixes two people. A
folded message older than `core.hold_max_age_s` is dropped, and core says so
on the channel. `channels.imessage.coalesce_window_s` still merges a burst
that arrives before a turn starts.

### The draft of a composer

Text that a person wrote and did not send is a **draft**. It lives in core
(`/v1/drafts`), so a person who leaves a thread, reloads the page, or opens
the chat on another device finds the text again.

A draft is named by its thread and the person who wrote it, as a hold is, so
the normal chat and each workspace hold their own, and in a shared workspace
one person's draft never reaches another person's composer. `GET` reads the
draft of one thread and person, `POST` keeps it, and a `POST` with an empty
text clears it, which a send does too. A draft is at most 8000 characters,
the length of a message.

A draft that was not touched for `core.draft_max_age_s` (default 604800
seconds, seven days) is dropped, so old drafts do not grow without limit. The
store is in memory, as the hold is, so a restart of core clears it.

Channels passes these through as `/v1/cli/draft`, and the viewer as
`/api/v1/chat/<person>/draft`. The viewer's composer saves a short time after
the person stops typing, not on every keypress.

A draft waits for the person; a held message waits for the running turn.

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
| `X-Joshua-Role` | `member`, `owner`, or `guest`. Any other value is a 400. |
| `X-Joshua-Group` | the group id, only for a group chat in `groups` |
| `X-Joshua-Workspace` | the workspace id (a UUID), only for a workspace thread. The gateway sends it upstream as `_meta["joshua/workspace"]` of each tool call and records the items of the call. |
| `X-Joshua-Workspace-People` | the people of a shared workspace, the owner first, comma separated; only with `X-Joshua-Workspace`. Each must be in `people`, else 400. The gateway sends them upstream as `_meta["joshua/workspace_people"]`. |

The gateway trusts these headers only from the identities in
`gateway.person_header_callers` (default `core` and `assignments`). Every other
identity gets `403` on an MCP route. A group id that is not in `groups` is
400.

## viewer

The viewer runs from the gateway image, as a process of its own, on port 8000
in the container. Compose publishes it on `127.0.0.1:8082`. Its caller is a
browser, never a container: the credential is the signed session cookie
`joshua_session`, not a fleet token, and no route here takes a bearer. A
request with no cookie gets `401 {"error": "sign in to read this"}` unless
`viewer.anonymous` serves it. Every write also checks the `Origin` header,
and a request from another site is `403`. Every answer is
`application/json` and is never cached.

The viewer holds no secret and no database. It calls core with its own fleet
token, `JOSHUA_TOKEN_VIEWER`, for everything below (`/auth/*`,
`/admin/people`, `/admin/settings`), so a core that does not answer is `503`
here.

A viewer route that proxies another container stands with the route it calls:
the settings routes, the release check, the push routes, and the workspace
notify mode with core's, and the chat routes with those of channels. The
routes of the pages that read the data volume — the wiki, the journal, the
search, the documents, the files, and the editor — serve the React app alone;
`gateway/joshua_gateway/viewer_api/` holds them.

### The session routes

| Route | Session | Request | Reply |
|---|---|---|---|
| `GET /api/v1/session` | needed | | `{"person": {"id", "name", "role"}, "anonymous": bool, "sections": [{"key", "label", "path"}], "chat_url", "timezone"}`. The sections are the navigation of that person: a guest gets no Documents, Chat is left out unless the roster knows the person, and Assignments unless the assignments container runs. |
| `POST /api/v1/session/sign-in` | no | `{"person", "secret", "remember"?}` | `{"ok": true, "person": {…}}`, with the session cookie: a cookie of the browser session, or of thirty days with `remember`. `401 {"error": "That sign-in did not work."}` for a wrong secret, a person who is not on the roster, and a person with no secret alike. `403` for a body that is not a JSON object or a cross-site origin. `503 {"error": "Joshua is still starting. Try again in a moment."}` when core could not check the secret: nothing was refused. |
| `POST /api/v1/session/sign-out` | no | empty | `{"ok": true}`, and the cookie is cleared. It needs no session, so a cookie the viewer no longer accepts can still be thrown away. `403` for a cross-site origin. |
| `POST /api/v1/session/secret` | needed | `{"secret", "new_secret"}` | `{"ok": true}`, with a new cookie, because the change moves the stamp every outstanding cookie of that person carries: this browser stays signed in and every other one signs out. `400` when either field is empty. `403 {"error": "That current PIN did not match."}`, `password` in place of `PIN` on that method, when core refused the current secret. `503` for every other failure. |
| `GET /api/v1/auth-method` | no | | `{"method": "anonymous" \| "pin" \| "password", "anonymous": bool}`: what the sign-in page draws, and whether a request with no cookie is served. The page reads it before there is a session. |
| `GET /api/v1/sign-in/people` | no | | `{"people": [{"id", "name", "role"}]}`: the people core says can sign in, named as the roster names them. A person core names who is not on the roster is left out. `404` on any method but `pin`, so no other instance renders a roster to a stranger. `503` when core did not answer. |

### The Users routes

The Users section of Admin. Every route is the owner's: a person whose role is
not `owner` gets `403 {"error": "the roster is the owner's"}` before any call
to core. Core's status and body pass through as they are, so a change that
would leave the roster with no owner is `409`, and so is an add of an id the
roster already holds.

| Route | Request | Reply |
|---|---|---|
| `GET /api/v1/admin/users` | | `{"users": [{"id", "name", "role", "has_secret"}]}`: the roster of `joshua.yaml`, with `has_secret` from core's `GET /auth/people`. `503` when core's answer holds no list of people, so the page never says that nobody can sign in because core said nothing. |
| `POST /api/v1/admin/users` | `{"id", "name", "handle"?, "role"?, "secret"?}` | `201` and core's answer plus `secret_set`, which says whether the secret was set: the route calls `POST /admin/people` and then `POST /auth/secret/admin`. With no `secret` the person is added and `secret_set` is `false`. A secret core refused answers its status, with `secret_set` `false` and an `error`, and the person stays added. `422` for a body that is not a JSON object. The role of an add that names none is `member`. |
| `POST /api/v1/admin/users/{id}/secret` | `{"secret"}` | core's answer to `POST /auth/secret/admin`: the first secret of a person, or a reset, with no current one. `400 {"error": "a secret is needed"}` for an empty secret, `422` for a body that is not a JSON object. |
| `POST /api/v1/admin/users/{id}/role` | `{"role"}` | core's answer to `POST /admin/people/{id}/role`. `422` for a body that is not a JSON object. |
| `POST /api/v1/admin/users/{id}/remove` | empty | core's answer to `POST /admin/people/{id}/remove`: the person is off the roster, and their handles and their secret go with them. |

## assignments

Port 8000 in the container. Compose does not publish it. The gateway calls
`/mcp` for a person. The viewer calls the read API. The container is off unless `assignments.enabled` is
true and its profile runs. [assignments.md](assignments.md) has the tools.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok", "database", "channels", "people", "draft", "running", "waiting", "paused_limits"}`: booleans and counts only. `503` when the database does not answer. |
| `ANY /mcp` | a bearer in `ASSIGNMENTS_TOKENS` or `ADDON_TOKEN` | MCP over streamable HTTP | the seventeen tools. `start_assignment` takes a slot at once or is refused with the reason `busy`; there is no queue. A refusal of `start_assignment` has `reason`, `message`, and `hint`, and where it applies `retry_after_s`, `running` (`{"id", "title"}`; another person's run has a null id), or `drafts`. A spec alone is refused as `drafts_open` while the person has drafts, unless `new` is true. `start_assignment(rerun=N, note)` starts a new run with the spec of the ended assignment N, with `parent_id` N; a draft is `is_a_draft`. `resolve_comment` takes `applied`, the references of what changed, and is refused as `nothing_applied` without one. `cancel_assignment` returns when the slot is free. `delete_assignment` hides a draft, and `restore_assignment` brings it back. `grant_repo` and `provide_file` answer a request of a worker for a repository or a file. `401` for a missing or wrong bearer, and for a fleet token. |
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
| channels `POST /v1/events` | the report, the question, or the request of an assignment, and the comments of the person on the plan or the report of a completed one | `{"destination": str, "text": str}`, and `"workspace": <id>` for an assignment that a workspace started (the `destination` is then optional). Channels accepts it only when `assignments` is in `channels.webhooks.allowed_callers`. |
| core `GET /v1/limits` | before each slice, and at `start_assignment` when `when_unknown` is `refuse` and the spec has no `budget.tokens` | none |
| core `POST /v1/usage` | after each slice that used tokens | one row: kind and name `assignment`, `worker` the model name of the spec, `run_id` the id, `person_id`, `model`, `calls`, `usage`, and `ref` `<id>:slice:<n>` |
| core `POST /v1/history/search` | a `search_history` call of a session | `{"person", "workspace", "query", "limit"}`: who started the run, and the words. The scope of the read is core's. |
| core `POST /v1/history/read` | a `read_history` call of a session | `{"person", "workspace", "start", "end", "cursor"}`, the same scope |
| core `ANY /v1/model/{path}` | each model call of a session, from the Claude CLI | a Claude API request |
| gateway MCP routes | each MCP call of a session | MCP, with `X-Joshua-Person` the person, `X-Joshua-Role` `member`, and `X-Joshua-Conversation` `assignment:<id>` |
| a git host, `https://<host>/<repo>.git/...` | a clone or a fetch of a session, through the git proxy | the git request, with HTTP Basic: the token of `assignments.platforms.<host>.token_env`. Never a push. |

A usage row that fails with a connection error, a timeout, or a `5xx` is sent
again, 3 attempts in all. Core keeps one row for each `ref`, so a row that
arrives two times counts one time. A history read is one attempt: a read that
fails tells the worker that the history cannot be read, and the slice goes on.

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
