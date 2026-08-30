# Architecture

Joshua is three containers and one database. The three containers ship from one
repository and one `docker-compose.yml`. The same code runs on one Docker host
and on Kubernetes.

![Joshua v4 high-level architecture](images/architecture.svg)

```
                inbound trust boundary                 outbound trust boundary
Telegram ─┐                                                       ┌─ files MCP (wiki/blog/attachments)
iMessage ─┤   ┌──────────┐   POST /v1/turns   ┌──────────┐  MCP   │  weather, user-added MCPs …
Terminal ─┤──►│ channels │ ────────────────► │   core   │ ──────►│ gateway ├─ per-person upstreams
Webhooks ─┘   └──────────┘ ◄──────────────── └────┬─────┘        └─────────┘
                             POST /v1/deliver     │ Postgres + /data volume
```

## The three goals

Every design choice serves these goals, in this order:

1. **Security through reduced agency.** The agent has no file tools, no shell,
   and no direct web access. It reaches the world in two ways only: a reply
   through `channels`, and an MCP tool call through `gateway`.
2. **Each task in the right container.** `channels` is the inbound trust
   boundary. `core` is the agent, the memory, and the scheduler. `gateway` is
   the outbound trust boundary and holds every upstream credential.
3. **Usable by one person, by a group, and by self-hosters.** One
   `joshua.yaml`, one compose file, `make up`.

## channels

`channels` talks to the messaging platforms. It receives a message from
Telegram, iMessage, the terminal, or a webhook. It checks the sender against the
`people` and `groups` lists, applies the rate limit, and stores any attachment.
Then it posts one normalized turn to `core`. A reply comes back the same way:
`core` calls `channels`, and `channels` sends the text to the platform.

`channels` holds the platform credentials: the Telegram bot token and the
BlueBubbles password. It holds no upstream tool credential.

## core

`core` runs the agent. It keeps one session per conversation, composes the
system prompt, and calls the Claude Agent SDK. The SDK gets an empty tool list.
Every capability the agent has is an MCP server. Some run inside `core`
(memory search, reminders, adding a person). The rest sit behind `gateway`.

`core` owns the database and the data volume. It indexes the volume for
retrieval, writes the nightly reflection, and runs the scheduler.

`core` holds the Claude token and the database password.

## gateway

`gateway` is one MCP endpoint for `core`. It maps each request to the speaker,
applies the speaker's tool policy, and proxies the call to
the upstream MCP server. It holds every upstream credential, so a compromised
session in `core` cannot read one.

The `files` MCP runs inside `gateway`. It is the only path that writes the
wiki and a person's journal, and it confines every path to its root. There is
one wiki. Everyone reads it, a member writes it, a guest reads it only.

## Who may call what

Every container loads the fleet tokens (`JOSHUA_TOKEN_<NAME>`). A caller sends
its own token as a bearer. The callee matches it and gets the caller's identity.
A missing or wrong token gets 401. An identity that is not on the route's list
gets 403.

| Route | Allowed callers |
|---|---|
| core `POST /v1/turns`, `POST /v1/turns/stream` | `channels` (and `laptop` for a `cli` handle) |
| channels `POST /v1/deliver`, `GET /v1/channels/resolve` | `core` |
| channels `POST /v1/events` | `channels.webhooks.allowed_callers` (default `laptop`, `ci`) |
| channels `POST /v1/cli/turns/stream`, `GET /v1/cli/outbox` | `channels.webhooks.allowed_callers` |
| channels `POST /webhook/imessage/{secret}` | no bearer; the path secret is the credential |
| gateway MCP routes | `core` only |
| `/admin/*` on every container | `ADMIN_CALLERS` (default `laptop`, `ci`) |
| `/healthz`, `/readyz` on every container | open, no secrets in the answer |

`contracts.md` has the request and response shapes.

## The data volume

One volume mounts at `/data` in all three containers. Each container writes its
own part of it:

| Path | Writer |
|---|---|
| `people/<id>/attachments/`, `inbox/` | channels |
| `people/<id>/blog/YYYY-MM-DD.md`, `people/<id>/profile.md`, `shared/profile.md` | core |
| `wiki/` | gateway, for a member |
| `people/<id>/blog/<dated post>` | gateway, for the person |
| `wiki/joshua/` | core, at each start |

`data-layout.md` describes the tree. `memory.md` describes what `core` does with
it.

## One instance, one group of people

One instance serves one group of people. There is no multi-tenant mode. A
second group gets a second instance: the same three containers, a second
database, a second volume, and its own `joshua.yaml`. The two instances share
nothing.

Inside one instance, a person is a `member` or a `guest`. The role is a trust
tier. A guest has their own notes and journal, but does not see the shared
profile in the prompt and cannot add people. `security.md` states what each
role can reach.
