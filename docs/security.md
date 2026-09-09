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

| Path | Read | Write |
|---|---|---|
| `wiki/` | everyone | a member, `.md` only. A guest reads only. |
| `wiki/journal/` | everyone | a member, through `write_journal_entry` only. The nightly page is core alone. |
| `wiki/people/<id>.md` | everyone | that person, when a member. Nightly reflection also rewrites it. |
| `wiki/people/everyone.md` | everyone | nobody through the agent. Nightly reflection writes it. |
| `people/<id>/attachments/` | everyone | nobody. Channels writes it. |
| `shared/` | everyone | nobody. Core and channels write it. |

A journal entry names at least one person in its `people` front matter. The
gateway refuses a name that is not on the roster. Writing the journal follows
the same role rule as the rest of the wiki: a guest never writes one, and a
group chat writes one only when its derived role is member.

The server refuses a path with `..`, an absolute path, and a symlink that
leaves a root.
The error names the root, never the real path. A request with no person, or
the `unknown` person, gets `wiki/` and `shared/` read-only and nothing else.

## One corpus, and two axes

Joshua holds one corpus, and it is the memory of one entity. The wiki is what
Joshua knows. The journal, inside the wiki at `wiki/journal/`, is when
something happened. An attachment is the artifact. None of the three belongs
to one person: the `people` front matter on a journal entry says whose episode
it records, and it is provenance, not a wall.

Two axes carry the whole model:

| Axis | What it holds | What decides |
|---|---|---|
| The corpus of Joshua | wiki, journal, attachments | the **role** |
| An upstream MCP server | Gmail, a calendar, your own store | the **person** |

Something that must stay private to one person lives outside Joshua, behind an
MCP server. The gateway applies a tool policy for each person, so that store
stays theirs. A group turn reaches the corpus and reaches no upstream of a
person, so a family chat writes the family wiki and touches nobody's private
store.

## Members and guests

A person is a `member` or a `guest`. The role is a trust tier inside one
instance. It is not a hosting concept.

| | Member | Guest |
|---|---|---|
| Read the corpus, through `files` and `search_memory` | yes | yes |
| Write the wiki and a journal post | yes | no |
| The shared profile in the prompt | yes | no |
| Add a person | yes | no |
| Shapes the shared profile from group chats | yes | no |
| MCP servers | per `allow` | per `allow` |

**Write is the only real difference.** A guest is a person you trust for a
while: a visitor, a friend who tries Joshua. A guest reads the whole corpus and
writes none of it. Put nothing in the corpus that a guest must not see. Tool
access is per person id, not per role: a guest with `allow: all` on a server
gets that server.

An `owner` role arrives later, and it will be the only role that adds a person.

### What decides a write

The gateway acts on the `X-Joshua-Role` header, which only `core` may assert.
`X-Joshua-Person` rides along for the audit trail and decides nothing about a
file.

A conversation carries one role. A direct message carries the role of its
person. A group carries a derived role: `member` only when `members` is not
empty and every handle in it names a member. An empty list, or a handle that
names nobody, is a guest chat.

So one guest in a family chat stops Joshua writing for the members too. That is
the safe direction, and it is not visible from the chat, so `core` logs a
`group role` line at start that names the group, the role, and the handle that
lowered it.

The role belongs to the conversation and not to the turn, because the SDK
writes the gateway headers onto the command line of the CLI subprocess when a
session starts. A per-turn role could not reach the gateway without rebuilding
the session for every message.

### Two rules that look alike

The files MCP keeps one rule and dropped another:

- **The person boundary** decided which person could reach `people/<id>/`. It is
  gone. The corpus is shared.
- **The write domain** decides which container owns a path: `channels` alone
  writes an attachment, `core` writes every profile nightly, and the agent
  writes the wiki: a page, a journal entry, and a member's own profile page.
  It stays. A member reads an attachment and does not write one, because
  `channels` owns that path.

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
credentials. `gateway` gets the upstream credentials, from its own
`.env.gateway` file, which no other service reads. This works because
`joshua.yaml` references a secret as `${VAR:-}`: a container without the
variable expands it to the empty string and never holds the credential.

An entry whose credential expanded to nothing is turned off, not started with an
empty credential and not retried. `/readyz` counts it in `disabled` and the log
names the empty key.

## Package MCP servers

A `stdio` entry with a `package` field is another author's code, installed by
the gateway and run in the gateway's container. Read what that means before you
add one.

**What you accept.** You trust the author of that package and everyone who can
publish to it. The pin is what limits this: an exact version, commit, or file
hash means the code cannot change under you, and a version bump is a change you
made and can review.

**What the gateway does to limit it.**

- The spec must pin. `latest`, a range, and a branch fail the config load.
- `npm install` runs with `--ignore-scripts`, so a `postinstall` does not run
  unless the entry sets `allow_scripts: true`. Set that only for a package you
  have read.
- The install command gets a minimal environment: no fleet token, no other
  entry's credential, and no database URL.
- The server process gets its entry's own `env` block and a minimal base
  (`PATH`, `HOME`, `LANG`) and nothing else. It cannot read another entry's
  credential, a fleet token, or the Claude token. A test starts a server that
  prints its environment and proves this.
- It runs with its working directory in its own store directory, as uid 1000,
  and the gateway's tool filter and `allow` list apply to it exactly as to an
  `http` server.

**What the gateway does not do.** A stdio child is not sandboxed from the
gateway process. It runs as the same user in the same container, so it can read
the data volume the gateway mounts and reach the network the gateway reaches.
A package you would not run on your own machine does not belong here. Sandboxing
a stdio child is later work.

## What a compromised session can reach

A prompt injection is the realistic attack: a message, a file, or a tool result
that tells the agent to do something. This is what it can do at most:

- Read the wiki, and write it when the speaker is a member: a page, a journal
  entry through `write_journal_entry`, or the speaker's own profile page.
- Read `shared/`.
- Call the MCP servers the speaker is allowed, with the speaker's identity.
- Reply on the channel the turn came from, and to any destination through a
  scheduled task.

It cannot read a token. It cannot run a command. It cannot fetch a URL, because no tool does that. A server that acts
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
- A `package` MCP server runs in the gateway's container with no sandbox. See
  [Package MCP servers](#package-mcp-servers).
