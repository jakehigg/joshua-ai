# joshua.yaml

One file configures one instance. All three containers read it. The schema, the
loader, and the validator live in `joshua_shared.config`.

Copy `joshua.example.yaml`, edit it, and mount it at `/etc/joshua/joshua.yaml`.
Secrets never live in this file. Put secrets in `.env` and reference them with
`${VAR}`.

## Load rules

- The loader reads the path in `JOSHUA_CONFIG`, or `/etc/joshua/joshua.yaml` when
  that variable is not set.
- `${VAR}` and `${VAR:-default}` expand from the environment on the raw text
  before the parse. An unset variable with no default is an error. The error
  names the variable.
- Unknown keys are errors. A typo fails the load.
- A literal credential in the file is an error. Credentials belong in `.env`. A
  value that comes from `${VAR}` is allowed.
- The config is cached per process. Call `reload()` to read the file again.

## CLI

```
python -m joshua_shared.config validate <file>   # summary, or the first error
python -m joshua_shared.config schema            # JSON schema
```

`validate` exits with code 1 on the first error. The error names the YAML path.

## Person options

Each `people[]` entry takes:

- `id`: the person slug (`^[a-z0-9][a-z0-9-]{0,31}$`).
- `name`: the display name.
- `role`: `member` (default) or `guest`.
- `handles`: a map of channel to handle id.
- `prompt`: a path to an extra prompt snippet for this person.
- `journal`: consent for what Joshua's journal says about this person's own
  life updates: `auto` (default, write an entry on its own), `ask` (offer
  first), or `off` (write an entry only when asked). See [memory.md](memory.md).

## Helpers

The loaded model has these lookups:

- `person(id)`: the person, or None.
- `people_by_handle(type, id)`: the person who owns a handle, or None.
- `groups_by_chat(channel, chat_id)`: the group, or None.
- `destination(name)`: the channel address for a destination alias.

## Runtime people (the people.yaml sidecar)

`joshua.yaml` is the source of truth for the roster at boot. The app can also
enroll people at runtime with no edit to `joshua.yaml`. A runtime change is written
to two places:

- the database roster cache, and
- a sidecar file `<data_dir>/people.yaml` (default `/data/people.yaml`), in the
  same schema as `people`.

The loader reads the sidecar and merges it over `people` at load time. A
sidecar entry with the same id as a `joshua.yaml` person replaces that person. A
new id is appended. An entry with `role: removed` drops that id from the merged
roster. This is how a removed person's handle stops resolving. So `joshua.yaml`
stays human-owned while the app enrolls people.

The data dir comes from `JOSHUA_DATA_DIR` (default `/data`), the same variable all
three containers read.

Three paths write the sidecar:

- The agent's `registration` tool (`add_user`, `list_users`). Only a
  member can add people. A guest or an unidentified caller gets "not available".
- The CLI `python -m joshua_core people {add,list,remove}`. `add` takes
  `--id --name --handle <type>:<value> --role member|guest`. It runs with
  `DATABASE_URL` set.
- The core admin API `GET /admin/people` and `POST /admin/people` (gated by
  `ADMIN_CALLERS`). `POST` takes the same fields as the CLI `add`.

`remove` marks the person `removed` and drops their handles. It keeps the data dirs
and the transcripts, so removal of data stays a human action.

Example `people.yaml`:

```yaml
people:
  - id: sam
    name: Sam
    role: guest
    handles:
      telegram: "555"
```

## Channels inbound guard

Every channel adapter runs one guard before it builds a turn. The guard checks
the allowlist, the per-handle rate, and the message size. An unknown sender never
reaches core.

The guard reads the current roster on each message, so a runtime enrollment
through `people.yaml` takes effect on the next message with no restart. A person
with `role: removed` is off the roster, so their handle no longer resolves.

### Limits

The `channels.limits` block sets the caps. The defaults are:

| Knob | Default | Effect |
|---|---|---|
| `per_handle_per_minute` | 20 | Token bucket per handle. The burst equals the rate. A handle over the rate is refused with `rate_limited`. |
| `max_text_chars` | 8000 | Text over the cap is truncated to the cap. The message is still allowed. |
| `max_attachment_bytes` | 26214400 | An attachment over the cap is skipped. The message is still allowed. |

### Unknown senders

The default on every channel is silent drop. Joshua says nothing to a sender it
does not know. A reply would confirm that the address is live and worth an attack.
An unknown sender's message never reaches core.

Each channel sets its own reply policy with `unknown_sender`:

- `drop` (the default on every channel) says nothing.
- `reply` sends a fixed refusal to the sender. A self-hoster opts into this per
  channel in `joshua.yaml`. It is not the default.

Both policies log the refusal at INFO with the `channel_type`, the `address`, and
the `chat_id`. The `address` is the handle an operator enrolls.

### Group attribution

A group in `groups` with no `members` treats anyone in that chat as the
group. Every message from that chat is attributed to the group, not to a person.
List `members` (the platform handles) to admit only those senders. A known person
in the chat is always allowed as that person, whatever the `members` list holds.

`members` also sets what the chat may write. A group writes the wiki only when
`members` is not empty and every handle in it names a person whose role is
`member`. An empty list admits anybody in that chat, so it writes nothing. A
handle that names no person carries no role, so it holds the chat to read-only
as well.

One chat carries one role, so **one guest in a family chat stops Joshua writing
for the members too**. That is deliberate: a session cannot change its role for
one turn. Core logs a `group role` line at start that names each group, its
derived role, and the handle that lowered it, so you can see why a chat is
read-only.

A group chat that `joshua.yaml` does not list appears on
`GET /admin/chats/unconfigured`, with the chat id to add here.

### Guard audit

The guard counts every refusal and keeps a bounded ring of the recent refusals.
An operator reads them through two admin routes, both gated by `ADMIN_CALLERS`:

- `GET /admin/guard/stats` returns the refusal and truncation counters.
- `GET /admin/guard/recent` returns the recent refusals. Each entry carries the
  `address` to enroll.

### Adding a channel adapter

Every channel adapter must call `Guard.check` before it builds a `TurnEvent`. No
adapter ships without the guard. This rule holds for every future adapter, for
example a voice or Siri channel.

The guard is the one boundary that keeps an unknown sender out of core. An adapter
that skips it lets a stranger reach the agent. When you add an adapter:

- Call `Guard.check` for every inbound message. Build the turn only on an allowed
  verdict.
- Apply the channel's `unknown_sender` policy to a refusal. The default is `drop`.
- Never create a person for an unknown handle. Enrollment is a member action
  through the `add_user` tool, the people CLI, or the admin API. An unknown sender
  gets no person, no tools, and no conversation with memory.

## memory

The `memory` section controls what Joshua remembers and how it finds it again.
[memory.md](memory.md) explains the mechanism. This is the reference.

Core reads these values at start. Restart core after an edit:

```
docker compose restart core
```

### Prompt tiers

Every turn carries the person's profile page in the system prompt, and the
shared profile too for a member or a group session. This is always present,
whatever the search finds. The journal carries no prompt tier: it reaches a
turn only through retrieval. See
[memory.md](memory.md#the-journal-reaches-a-turn-only-through-retrieval).

| Key | Default | What it does |
|---|---|---|
| `shared_max_chars` | `2000` | cap for the shared profile section |
| `recent_posts` | `3` | accepted for a config from an earlier release; no longer read |
| `recent_max_chars` | `6000` | accepted for a config from an earlier release; no longer read |

### Retrieval

Before each turn, core searches the index with the person's message. The result
is one of three decisions. See [memory.md](memory.md#what-the-turn-brings-and-when).

| Key | Default | What it does |
|---|---|---|
| `inject.enabled` | `true` | `false` turns off the per-turn search. The `search_memory` tool still works |
| `inject.full_sim` | `0.72` | at or above this, the chunk text goes in the prompt |
| `inject.hint_sim` | `0.60` | at or above this, the file names go in the prompt |
| `inject.top_k` | `2` | chunks used on a `full` decision |
| `inject.max_chars` | `1200` | cap per chunk, in the prompt and in the audit row |

Raise `full_sim` if answers use text that is not relevant. Lower `hint_sim` if
answers miss things that are in your files. Change one value at a time, and read
`GET /admin/kb/events` for a few real questions before the next change.

### Taught skills

A taught skill is a `wiki/skills/<slug>.md` file: a "when I say X, do Y"
behavior a member teaches in chat. See [memory.md](memory.md#taught-skills).

| Key | Default | What it does |
|---|---|---|
| `skills.enabled` | `true` | `false` runs no taught-skill match. No turn gets a skill block |
| `skills.min_sim` | `0.62` | the similarity floor for a trigger match |
| `skills.top_k` | `1` | most skills one turn can fire |

### Index

| Key | Default | What it does |
|---|---|---|
| `index_interval_s` | `60` | how often the `files` source re-reads the volume |
| `chunk_chars` | `1600` | target size of one chunk |
| `min_sim` | `0.55` | floor for a search result, below the two decision thresholds |
| `per_doc_cap` | `3` | most chunks one file may contribute to one search |
| `recency_bonus` | `0.08` | score added to a recent document |
| `recency_half_life_days` | `14.0` | how fast that bonus decays |
| `embed_model` | `BAAI/bge-small-en-v1.5` | the embedding model, 384 dimensions |

Do not change `embed_model` on a running instance. The `kb_chunk` column holds
384 numbers per chunk. A model with a different size needs a new index.

### Sources

`sources` lists what the index reads. `files` is the kernel source and always
runs. Other sources are optional, and each takes its own options.

```yaml
memory:
  sources:
    files:
```

[memory.md](memory.md#source-adapters) documents the `memos` source.

### Writing

| Key | Default | What it does |
|---|---|---|
| `nightly_at` | `"03:30"` | local time of the nightly reflection |
| `daily_rollover` | `true` | start a fresh agent session after the nightly run |
| `min_chars_for_post` | `200` | skip a person's post below this much conversation |

A person below `min_chars_for_post` is skipped, and core logs the reason with
the count. `POST /admin/reflect` runs or re-runs one day.

## MCP servers

The `mcp:` section lists the servers the gateway hosts. Add a server by adding one
entry. The entry name is the gateway route. Two entries can name the same upstream
under different names and tool filters (the "view" pattern below).

Each name must match `^[a-z][a-z0-9-]{0,31}$`. An entry is a builtin
(`kind: builtin`, run in-process) or an external upstream (`type: stdio|http|sse`).
A `stdio` entry needs `command`. An `http` or `sse` entry needs `url`.

The shipped gateway image runs only the builtins and any `type: http` or `type:
sse` server it can reach over the network. It has no `uv` or node, so a `type:
stdio` server that starts with `uvx` or `npx` does not start. The gateway retries
it forever and `/readyz` reports it errored. To run such a server, build its runtime
into your own gateway image, or run it as its own container and reach it with `type:
http`. The `stdio` examples below show the config shape, not servers the shipped
image can run.

`allow` is `all` or a list of person ids. Person ids must exist in
`people`. `all` means every person, including a request with no person.
A list scopes the server to those people: the gateway serves a request only when
its person is on the list, and denies a request with no person or `unknown`. The
person comes from the `X-Joshua-Person` header, which the gateway trusts only from
core.

`tools` filters the tool list. `allow` and `deny` are `fnmatch` globs matched
against the tool name. `deny` beats `allow`. An absent `tools` block means every
tool. A user server without a filter exposes every tool, so set `allow`.

Secrets never live in this file. Put a token in `.env` and reference it with
`${VAR:-}`. The loader expands the reference and never logs the value. The
`:-` default matters: only the container that owns a secret defines the
variable. In every other container the reference expands to the empty string,
and an empty credential turns the feature off. A plain `${VAR}` reference
fails the load in a container without the variable, so use it only for a value
that every container defines.

### The three shapes

```yaml
mcp:
  files:                                  # builtin, run in-process
    kind: builtin
    allow: all
  weather:                                # external, stdio
    type: stdio
    command: uvx
    args:
      - mcp-weather
    env:
      WEATHER_KEY: ${WEATHER_KEY}
    allow: all
    tools:
      allow:
        - "get_*"
      deny:
        - "*_admin"
  spotify:                                # external, http, per-person credential
    type: http
    url: https://spotify-mcp.example/mcp
    allow:
      - alex
      - mia
    identities:
      alex:
        headers:
          Authorization: "Bearer ${SPOTIFY_TOKEN_ALEX}"
      mia:
        headers:
          Authorization: "Bearer ${SPOTIFY_TOKEN_MIA}"
```

### The files MCP

`files` is a builtin. It is the agent's only file interface. It lists, reads,
searches, writes, and renames files. Every path is confined to a root.

The corpus is one corpus. The wiki is what Joshua knows, a journal records when
something happened, and an attachment is the artifact. No root is keyed on a
person: the role of the request decides the write, and `people/<id>/` records
whose episode a journal entry holds. The role comes from the request, never from
a tool argument.

Three roots under `/data`:

| Root | Path | Read | Write |
|---|---|---|---|
| `wiki/` | `wiki/` | everyone | a member, `.md` only |
| `people/` | `people/` | everyone | a member, `people/<id>/blog/` only, `.md` only, create or append |
| `shared/` | `shared/` | everyone | no |

A member writes the wiki and a journal post. A guest reads and writes nothing.
A request with no role is a guest.

The write rule below `people/` is the write domain of a container, and not a
wall between people: `channels` owns `people/<id>/attachments/` and `core` owns
`people/<id>/profile.md`, so the agent reads both and writes neither.

`wiki/joshua-docs/` holds the documentation that the repo ships. Core
replaces it at each start.

Channels stores an inbound attachment under `people/<id>/attachments/YYYY/MM/` with the name
`YYYY-MM-DD-HHMMSS-<stem>.<ext>`, where `<stem>` is the sanitized original stem.
The timestamp is the arrival time in `timezone`, the person's wall
clock, so the name reads naturally. The message frontmatter and the index keep
UTC. A second file with the same name in the same second gets `-2`, `-3`, and so
on before the extension.

A journal write is create or append only. Overwrite is refused. The server
names the post: `write_file` to `people/<id>/blog/<slug>.md` lands as
`people/<id>/blog/YYYY-MM-DD-HHMM-<slug>.md`, stamped with the gateway clock in
`timezone`. A name that already carries a valid `YYYY-MM-DD-HHMM-`
prefix is kept. A second write with the same slug in the same minute gets `-2`,
`-3`, and so on. The digest name `people/<id>/blog/YYYY-MM-DD.md` (no time part) is reserved
for core and is refused. `write_file` injects frontmatter (`date`, `person`,
`source: chat`, `attachments: []`) when the post has none. When the post lists
`attachments`, the server checks each path names a stored attachment under
`people/<id>/attachments/`.

Tools: `list_files`, `read_file`, `write_file`, `rename_file`, and
`search_files`. `read_file` returns an image block for a `.jpg`, `.jpeg`, `.png`,
`.gif`, or `.webp` under `people/<id>/attachments/`. A `.pdf` returns its extracted text (the
first 20 pages, capped at 256 KB). Another attachment returns text when it is
UTF-8 and 256 KB or less, else metadata only. `write_file` writes `.md` only, at
most 256 KB, and `mode: create` fails when the file exists. `rename_file` renames
one file in place under `wiki/`, a journal, or an attachments directory.
`new_name` is a bare
filename and the extension must not change. An attachment keeps its date-time
prefix, so the person renames the descriptive part only. `search_files` is a
substring or regex search over markdown text, not semantic search. `list_files`
adds `original_name` for an attachment when a `<file>.meta.json` sidecar records
the sender's filename.

There is no `delete_file`. Deletion is a human action through the viewer or a
shell.

### Per-person identities

`identities` runs one upstream connection per person, each started with that
person's own credential. A request from person P is routed to P's connection, so
P's tool calls run as P and core never sees the credential. The `spotify` entry
above shows the shape: one block per person under `identities`, each with connect
overrides that merge onto the base connect config.

- Use `env` (and `command`, `args`) for a stdio server. Use `headers` (and `url`)
  for an http or sse server. A cross-transport override fails validation.
- Every person in `allow` needs an identity. A person in `allow` with no identity
  fails validation. An identity server needs an explicit `allow` list, not `all`.
- A request with no person gets 403 `person_required`. A person the server does
  not admit gets 403.
- The connections are isolated: one person's expired credential does not affect
  another person's connection. `/readyz` counts every connection and
  `/admin/inventory` lists them per server.

### The view pattern

Two entries can share one upstream. Each entry is independent. The name is the
route. Give each a different filter to expose a different view of the same server.

```yaml
mcp:
  tracker-readonly:
    type: stdio
    command: mcp-tracker
    env:
      TRACKER_TOKEN: ${TRACKER_TOKEN_RO}
    allow:
      - alex
    tools:
      allow:
        - "get_*"
        - "list_*"
        - "search_*"
  tracker-full:
    type: stdio
    command: mcp-tracker
    env:
      TRACKER_TOKEN: ${TRACKER_TOKEN}
    allow:
      - alex
```

### Reserved policy fields

These fields are recorded now and enforced later. `/admin/inventory` shows
them. The proxy does not act on them yet.

- `url_args`: `none` or `grant-required` (default `grant-required`).
- `tools.class`: a map of tool or pattern to `read`, `write-local`, or `act`.
  Anything not listed is `act`.

```yaml
mcp:
  weather:
    type: stdio
    command: uvx
    args:
      - mcp-weather
    allow: all
    url_args: none
    tools:
      allow:
        - "get_*"
      class:
        "get_*": read
```

### Reload

`POST /admin/reload` on the gateway re-reads this file and diffs the catalog. The
diff runs per connection. A new entry starts. A removed entry stops. An entry with
a connect or filter change restarts. An unchanged entry keeps its session. For an
identity server, a person added to `identities` starts one connection and a person
removed stops one. The other people's connections are untouched. A body
`{"server": "<name>"}` restarts one entry.

## modules

`modules` is a reserved list of module names. It is the hook for optional
prompt and tool modules. Today the kernel ships every capability and the list
has no effect. The loader accepts it and validates its shape.

## viewer

`viewer` runs a read-only web viewer for the wiki and a person's own files. A
person opens a browser, signs in with a password, and reads the wiki, their own
profile, their own journal, their own attachments, and the shared profile. A
member can also delete a wiki page, which moves it to `wiki/.trash/`. A guest
cannot. The agent never reaches the viewer, and the viewer never calls core.

The viewer is off by default. Start it with `docker compose --profile viewer
up`; it listens on `127.0.0.1:8082`, beside the rest of the stack. Put a
reverse proxy in front for TLS.

- `viewer.enabled`: `false` by default. The viewer refuses to start when it is
  false.
- `viewer.users`: a map of person id to a password reference. A key must name a
  person in `people`, or the config fails to load. A person with no entry, or
  an empty value, cannot sign in.

Set each password through the environment, never a literal in `joshua.yaml`:

```yaml
viewer:
  enabled: true
  users:
    alex: ""
```

The password comes from `VIEWER_PASSWORDS` in `.env`, one variable that holds
every one of them as `<person-id>=<value>` pairs, comma separated. A value
that starts with `$2` is a bcrypt hash and is checked with bcrypt. Any other
value is a literal password, which cannot hold a comma. See `.env.example`.

The list in `viewer.users` is the authorization: a person who is not a key
there cannot sign in, whatever the environment holds. An entry with a value
still works, so an installation that puts the reference in `joshua.yaml`
keeps working.

The role, member or guest, comes from `people`. It is never set in `viewer`.

## Agent backend

`core` reads the `AGENT_BACKEND` environment variable to pick the session backend.
It is an env var, not a `joshua.yaml` key. `docker-compose.yml` defaults it to
`sdk`. An unset value also selects `sdk`.

| Value | Runs | Needs |
|---|---|---|
| `sdk` (default) | Real Claude through the Claude Agent SDK | `CLAUDE_CODE_OAUTH_TOKEN` and the bundled `claude` CLI (in the core image) |
| `stub` | A canned reply, for plumbing tests | Nothing: no SDK, token, or network |

Get a token with `claude setup-token`. For `stub`, set `AGENT_BACKEND=stub` in
`.env` and leave `CLAUDE_CODE_OAUTH_TOKEN` empty. Any other value fails at boot.

## System prompt snippets

The core image ships its system prompt as Markdown files in two directories under
`core/joshua_core/prompts/`:

- `builtin/` holds the kernel files that Joshua ships (`base.md`, `shared/profile.md`,
  `chat.md`, `group.md`, `guest.md`, `event.md`, `scheduled.md`). A profile
  composes its prompt from these files. Do not edit them. An upstream pull can
  overwrite them.
- `user/` holds your own snippets. It ships empty, except for a `README.md` and an
  example file.

The composer loads every `.md` file in `user/`. It sorts the files by name and
appends them to each session's prompt, after the kernel files and before the
identity block. The snippets apply to every profile (dm, group, guest, event,
scheduled). You do not edit any Python code or `Profile.prompt_files`.

Use `user/` for house rules, guidance for a custom MCP server you added to the
`mcp:` section, or any other instruction.

To add a snippet:

1. Clone the repo.
2. Add a file such as `core/joshua_core/prompts/user/house-rules.md`.
3. Commit it, then build and deploy the core image.

Only `.md` files load. `README.md` and any dotfile are ignored. A name prefix such
as `10-`, `20-` sets the order.

### `core.prompts_dir`

The default is unset. The composer then reads the prompts that ship in the image,
at `core/joshua_core/prompts/`.

Set it only when you mount your own prompts directory into the container. The
directory must hold a `builtin/` subdirectory and can hold a `user/` one. It must
hold every file the profiles name.

A wrong path gives the agent no system prompt. The composer logs one
`prompt file missing` warning per file and uses an empty string, so the agent
answers as a generic assistant. The warning appears when a turn composes a
prompt, not at start. To check a change, send a turn first, then read the log.
