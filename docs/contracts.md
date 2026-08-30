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
and `ci` are for operators. `make init-env` writes the first four into `.env`.

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
| `GET /readyz` | open | | `{"ok": bool, "checks": {<channel>: {"ok": bool, …}}}` |
| `POST /v1/deliver` | `core` | DeliverRequest | `200 {"delivered": true, "parts": n}`, `400 bad_request` or `invalid_attachment`, `404 unknown_channel`, `502 send_failed` |
| `GET /v1/channels/resolve?ref=` | `core` | | `200` ResolveResponse, `404 unknown_channel` |
| `GET /v1/channels/refusals?limit=` | `core` | | `200 {"refusals": [{"channel_type", "address", "chat_id", "reason", "at"}]}`. The handle and the channel, never a message body. |
| `POST /v1/events` | `channels.webhooks.allowed_callers` | see `channels.md` | `202 {"accepted": true}`, `200 {"ignored": true}`, `400`, `404 unknown_channel`, `502`, `503 core_unavailable` |
| `POST /webhook/imessage/{secret}` | none, the path secret | BlueBubbles payload | always `200`. A wrong secret gets `404`. |
| `POST /v1/cli/turns/stream` | `channels.webhooks.allowed_callers` | `{"person": id, "text": str}` | SSE, or `400`, `403 {"reason": <guard reason>}`, `503 core_unavailable` |
| `GET /v1/cli/outbox?person=&ack=true` | `channels.webhooks.allowed_callers` | | `{"messages": [{"ts", "text", "attachments"}]}`, `403 unknown_person` |
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
| `GET /readyz` | open | | `{"ok": bool, "checks": {"db": bool, "layout": bool}}` |
| `POST /v1/turns` | `channels` | TurnEvent | `202 {"accepted": true, "turn_id": id}`, `200 {"accepted": false, "reason": "duplicate"}`, `403 unknown_sender`, `429 overloaded` |
| `POST /v1/turns/stream` | `channels`, and `laptop` for a `cli` handle | TurnEvent | SSE (below), `403 unknown_sender` |
| `GET /admin/people` | `ADMIN_CALLERS` | | `{"people": […]}` |
| `POST /admin/people` | `ADMIN_CALLERS` | `{"id", "name", "handle", "role"?}` | `201 {"person": …}`, `400 {"error": …}` |
| `GET /admin/pool` | `ADMIN_CALLERS` | | the session pool |
| `POST /admin/sessions/flush` | `ADMIN_CALLERS` | | `{"flushed": n}` |
| `GET /admin/transcript/{conversation}?limit=` | `ADMIN_CALLERS` | | `{"conversation_id", "rows": […]}` |
| `POST /admin/kb/reindex` | `ADMIN_CALLERS` | `{"source"?, "person"?, "full"?, "background"?}` | `{"results": …}`, or 202 `{"started": true, …}` for `background` |
| `GET /admin/kb/status` | `ADMIN_CALLERS` | | `{"sources": {…}, "stats": […]}` |
| `GET /admin/kb/events?limit=` | `ADMIN_CALLERS` | | `{"events": […]}` |
| `POST /admin/reflect` | `ADMIN_CALLERS` | `{"person"?, "date"?}` | the reflection summary |
| `POST /admin/turn` | `ADMIN_CALLERS` | `{"channel", "text", "person"?, "framing"?}` | `{"conversation_id", "text"}`. The reply is not delivered. |

`POST /v1/turns` answers `202` at once and runs the turn in the background. The
reply reaches the person through `POST /v1/deliver`. A second turn with the
same `message_id` inside 15 minutes is a duplicate. Core refuses new turns with
`429` when more than `2 × core.pool_max` turns run at once.

### The stream

`POST /v1/turns/stream` and `POST /v1/cli/turns/stream` answer with
`text/event-stream`. Each frame is `event: <name>` and one `data:` line of JSON.

| Event | Data |
|---|---|
| `delta` | `{"text": "<chunk>"}` |
| `done` | `{"turn_id", "text", "tools"}`, the full text and the tools used |
| `error` | `{"message": "<what failed>"}` |

## gateway

Port 8000 in the container. Compose does not publish it. Only `core` reaches
it.

| Route | Callers | Request | Reply |
|---|---|---|---|
| `GET /healthz` | open | | `{"ok": true}` |
| `GET /readyz` | open | | `{"ok": bool, "connected", "errored", "total"}`, counts only |
| `ANY /<server>` and `/<server>/…` | `core` | MCP over streamable HTTP | the upstream's answer. `403` when the person may not use this server. `409` when a live session changes person. `503` when the upstream is down. |
| `POST /admin/reload` | `ADMIN_CALLERS` | `{"server": name}` or empty | `{"reloaded": {…}, "failed": {…}}`, `502` when one failed |
| `GET /admin/calls?identity=&tool=&limit=` | `ADMIN_CALLERS` | | `{"calls": […]}`, newest first |
| `GET /admin/inventory` | `ADMIN_CALLERS` | | `{"servers": {…}, "identities": {…}}` |

`core` sends two headers with every MCP request:

| Header | Meaning |
|---|---|
| `X-Joshua-Person` | the person id, or `unknown` |
| `X-Joshua-Conversation` | the conversation id |

The gateway trusts `X-Joshua-Person` only from `core`. From any other caller it
ignores the header, and the request gets only what an unknown person gets.

## Where `laptop` may go

The `laptop` identity is for the operator on the host. It may call
`POST /v1/events`, the two `/v1/cli/*` routes, every `/admin/*` route (by
default), and `POST /v1/turns/stream` with a `cli` handle.

It may not call the gateway MCP routes. Only `core` reaches a person's tools and
files, and only `core` says which person a request belongs to. It may not call
`/v1/turns`, `/v1/deliver`, or `/v1/channels/resolve` either. Those belong to
the containers.
