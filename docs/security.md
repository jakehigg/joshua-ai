# Security model

This page states what Joshua can and cannot reach, and why. The first goal of
the design is security through reduced agency. The agent gets the smallest set
of abilities that still lets it help.

## The agent has no built-in tools

`core` runs the Claude Agent SDK with an empty tool list. The code asserts it.
The agent has no file tool, no shell, and no web fetch or web search. It cannot
read the container's environment, so it cannot read a token.

Every ability the agent has is an MCP server:

- Inside `core`: `search_memory`, the scheduling tools, and `add_user` and
  `list_users`.
- Behind `gateway`: the `files` server and every server you add in `mcp:`.

If a task seems to need a built-in tool, the task is wrong. Add an MCP server
instead.

## Two boundaries

`channels` is the inbound boundary. Every message passes its guard. A sender
that is not in `people`, or a group chat that is not in `groups`, gets no turn
and no reply. A stranger cannot make Joshua run. `channels` holds the platform
credentials and nothing else.

`gateway` is the outbound boundary. It holds every upstream credential, so a
compromised session in `core` cannot read one. It applies each person's tool
policy on every call, both when it lists the tools and when a tool runs. A
tool that a person may not use is not in their list, and a call to it fails.

`core` sits between them. It holds the Claude token and the database password.
It reaches the internet for the Claude API only.

## Who may call what

Every route has an allowlist of fleet identities. The list is in
`architecture.md`. Two rules matter most:

- `/admin/*` on every container checks `ADMIN_CALLERS` on every route, with
  no exception.
- `gateway` admits only `core` on its MCP routes, and trusts the
  `X-Joshua-Person` header only from `core`. No setting widens either one. Any
  other caller gets what an unknown person gets: the shared files, read-only.

`/healthz` and `/readyz` are open. They return booleans and counts, never a
name, a path, or a token.

## Per-person policy

Each MCP server in `joshua.yaml` has an `allow` list: `all`, or a list of
person ids. A person who is not on the list does not see the server. A group
turn, which runs with no person, sees only servers with `allow: all`.

A server can hold one credential per person with `identities`. The gateway
then runs one instance of the server per person, and a person's call never
uses another person's credential.

The `files` server confines every path to the speaker's roots:

| Root | Read | Write |
|---|---|---|
| `wiki/` | everyone | a member, `.md` only. A guest reads only. |
| `blog/` | the person | the person, `.md` only, create or append |
| `profile.md` | the person | nobody. Nightly reflection writes it. |
| `attachments/` | the person | nobody. Channels writes it. |
| `shared/` | everyone | nobody. Core and channels write it. |

The server refuses a path with `..`, an absolute path, and a symlink that
leaves a root.
The error names the root, never the real path. A request with no person, or
the `unknown` person, gets `wiki/` and `shared/` read-only and nothing else.

## Members and guests

A person is a `member` or a `guest`. The role is a trust tier inside one
instance. It is not a hosting concept.

| | Member | Guest |
|---|---|---|
| Own journal and profile | yes | yes |
| Read the wiki, through `files` and `search_memory` | yes | yes |
| Write the wiki | yes | no |
| The shared profile in the prompt | yes | no |
| Shared files through `files` and `search_memory` | yes | yes |
| Add a person | yes | no |
| Shapes the shared profile from group chats | yes | no |
| MCP servers | per `allow` | per `allow` |

A guest is a person you trust for a while: a visitor, a friend who tries
Joshua. A guest reads the whole wiki and `shared/`, and writes neither. Put
nothing in the wiki or in `shared/` that a guest must not see. Tool access is per person id, not per role. A guest
with `allow: all` on a server gets that server.

## Secrets

Secrets live in `.env` and nowhere else. `joshua.yaml` refers to them as
`${VAR}`. The loader refuses a literal credential in `joshua.yaml`: a value that
starts with `sk-ant-`, `glpat-`, `xoxb-`, `ak2_`, or `GOCSPX-` fails validation.
The log formatter replaces a bearer token and any value with those prefixes
with `[REDACTED]`.

`make init-env` mints the database password and the four fleet tokens. Each
token is 33 random bytes. Every container gets all four fleet tokens, because
each one verifies the callers it trusts. The three containers are one trust
domain.

Compose gives each container only the variables it needs. `core` gets the
Claude token and the database password. `channels` gets the platform
credentials. `gateway` gets the upstream credentials through `mcp:` entries.
This works because `joshua.yaml` references a secret as `${VAR:-}`: a
container without the variable expands it to the empty string and never holds
the credential.

## What a compromised session can reach

A prompt injection is the realistic attack: a message, a file, or a tool result
that tells the agent to do something. This is what it can do at most:

- Read the wiki, and write it when the speaker is a member.
- Read and write the speaker's own `blog/`.
- Read `shared/`.
- Call the MCP servers the speaker is allowed, with the speaker's identity.
- Reply on the channel the turn came from, and to any destination through a
  scheduled task.

It cannot read another person's journal. It cannot read a token. It cannot run a
command. It cannot fetch a URL, because no tool does that. A server that acts
on the world, such as home automation, is exactly as exposed as its `allow`
list makes it. Keep such servers on a short list.

## Network

- `channels` is on the host at port `8080` for BlueBubbles and webhook callers.
- `core` is on the host at `127.0.0.1:8081` only, for the operator.
- `gateway` and `postgres` have no host port.
- `core` reaches the Claude API. `gateway` reaches its upstreams. `channels`
  reaches Telegram and BlueBubbles.
- Telegram link previews are off, so Telegram's servers never fetch a URL that
  Joshua sends.

Compose does not enforce egress. A firewall around the host does.

## One instance, one group

One instance serves one group of people, with one database and one volume.
There is no multi-tenant mode. A second group gets a second instance that
shares nothing with the first. Do not host people who must not see each
other's shared files on one instance.

## Known limits

- A guest reads the whole wiki and every shared file. That is the design, not
  a gap. The wiki is one place for everyone.
- The webhook route trusts any caller with a listed fleet token. Give an
  external system its own token, and keep the list short.
- Only the path secret protects the iMessage webhook. Keep the secret
  long and rotate it when a log leaks.
- The `laptop` token can call every admin route. It is the operator's root
  credential. Keep `.env` private.
