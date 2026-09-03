# CLAUDE.md

Conventions for people and agents who change this repository. `docs/` explains
the product. This file explains how to work on it.

## What Joshua is

Joshua is a personal AI agent for one person or a small group of people who
share it. Three containers ship from this one repository and one
`docker-compose.yml`:

- `channels` is the inbound trust boundary: Telegram, iMessage, the terminal,
  webhooks. It checks the sender, stores files, and posts one turn to `core`.
- `core` is the agent, the memory, and the scheduler. It owns the database and
  the data volume.
- `gateway` is the outbound trust boundary. It holds every upstream credential
  and applies the per-person tool policy. Every capability is an MCP server
  behind it or inside `core`.

`docs/architecture.md` and `docs/security.md` say why. Read them before a
change that touches a boundary.

## The three goals, in order

1. **Security through reduced agency.** The agent has no file tools, no shell,
   and no direct web access. It reaches the world through a reply on
   `channels` and a tool call through `gateway`. Nothing else.
2. **Each task in the right container.** Inbound checks live in `channels`.
   Credentials and tool policy live in `gateway`. The agent lives in `core`.
3. **Usable by one person, by a group, and by self-hosters.** One
   `joshua.yaml`, one compose file, `make up`.

A change that serves a lower goal at the cost of a higher one is wrong.

## Vocabulary

Use one name for one thing, in code and in prose:

- **person**, **people**: the humans in `joshua.yaml`. A person is a
  **member** or a **guest**. The role is a trust tier, not a hosting concept.
- **the wiki**: `/data/wiki/`, one wiki for everyone. There is no per-person
  wiki. A member writes it, a guest reads it.
- **journal**: Joshua's own journal, at `wiki/journal/`. It is not a person's
  blog. **profile**: `wiki/people/<id>.md`. **shared profile**:
  `wiki/people/everyone.md`.
- **instance**: one deployment, one group of people, one database, one volume.
  There is no multi-tenant mode.
- **channel**: a way to talk to Joshua. **destination**: a logical name that
  resolves to a channel.

Do not use "household", "family", or "tenant" in identifiers, config keys,
paths, prompts, or docs. Examples of who can share Joshua ("a family, friends,
a team") are fine in a sentence.

## Repo layout

```
pyproject.toml           uv workspace root: members shared, channels, core, gateway
uv.lock                  committed; a pin change is a reviewed change
joshua.example.yaml      the one config file, documented inline
docker-compose.yml       postgres (pgvector), gateway, core, channels
.env.example             secrets and tokens only; never in joshua.yaml
Makefile                 up, down, chat, validate, lint, test, backup, restore
shared/                  package joshua_shared: config, layout, contracts, fleet_auth, log
channels/                package joshua_channels
core/                    package joshua_core
gateway/                 package joshua_gateway
docs/                    the product documentation; core ships it into the wiki
scripts/                 backup, restore, chat, check_test_policy, ci-local
tests/e2e/               compose-level tests
.claude/skills/          the ste-writing skill (the documentation standard)
.github/workflows/       CI
```

Each member has its own `pyproject.toml` (it depends on `joshua-shared` through
the workspace), its own `Dockerfile`, and its own `tests/`. Python 3.13. `uv`
for everything (`uv sync --frozen` in the Dockerfiles). `ruff` for format and
lint. `pytest`. Pydantic v2 for config and API models.

## Identity and auth between containers

Every container loads every `JOSHUA_TOKEN_<NAME>` variable into a map. A caller
sends `Authorization: Bearer <its own token>`. The callee compares with
`hmac.compare_digest` against the whole map, and the matched key, in lower
case, is the caller's identity. A missing or wrong token gets 401. An identity
that is not on the route's allowlist gets 403. There is no other bearer path.

Identities: `channels`, `core`, `gateway`, plus `laptop` and `ci` for
operators. `make init-env` mints the tokens into `.env`. Each container gets all
of them, because each one verifies the callers it trusts. The three containers
are one trust domain. The agent cannot read the environment, because it has no
file or shell tool.

Route allowlists, by default:

- core `POST /v1/turns`, `POST /v1/turns/stream`: `channels`. The stream route
  also accepts `laptop` for a `cli` handle.
- channels `POST /v1/deliver`, `GET /v1/channels/resolve`: `core`.
- channels `POST /v1/events`, `POST /v1/cli/*`, `GET /v1/cli/*`:
  `channels.webhooks.allowed_callers` (default `laptop`, `ci`).
- channels `POST /webhook/imessage/{secret}`: no bearer. The path secret is the
  credential. A wrong secret gets 404, never 401. The handler always answers
  200 fast, because BlueBubbles never retries.
- gateway MCP routes: `core` only. `X-Joshua-Person` is trusted only from
  `core`.
- `/admin/*` on every container: `ADMIN_CALLERS` (default `laptop,ci`), checked
  on every admin route with no exception.
- `/healthz`: open, `{"ok": true}`. `/readyz`: open, booleans and counts, no
  names and no secrets.

`docs/contracts.md` has every route and body.

## The data volume

One volume mounts at `/data` in all three containers:

```
/data/wiki/journal/YYYY/MM/DD/YYYY-MM-DD.md      the nightly page; core writes it
/data/wiki/journal/YYYY/MM/DD/<slug>.md          a journal entry; gateway writes it
/data/wiki/people/<id>.md                        a profile; core writes it (nightly), gateway for that person
/data/wiki/people/everyone.md                    the shared profile; core writes it (nightly)
/data/wiki/**.md                                 the rest of the wiki; gateway writes it for a member
/data/wiki/joshua/*.md                           the repo docs; core rewrites them each start
/data/people/<id>/attachments/YYYY/MM/<file>     channels writes it
/data/people/<id>/cli/outbox.jsonl               channels writes it
/data/shared/attachments/<group>/YYYY/MM/        channels writes it
/data/inbox/                                     channels scratch, short-lived
```

Nobody else writes anything. `people/` and `shared/` are read-only through the
files MCP. A person id is a slug `^[a-z0-9][a-z0-9-]{0,31}$`.
`joshua_shared.layout` builds every path and bootstraps the tree.
`docs/data-layout.md` is the reference.

## Memory

`core/joshua_core/memory/` indexes the volume into the `kb_chunk` pgvector
table. The `files` source is always shared scope now: the wiki holds the
journal and every profile, so `person_id` is NULL for every row it writes. The
optional `memos` source can still scope a row to one person. A search is
`person_id = %s OR person_id IS NULL`, so a person finds their own scoped rows,
if any, plus everything shared, and a group turn finds the shared scope only.

- Source adapters implement one protocol (`memory/sources/__init__.py`).
  `files` is the kernel adapter and always runs. `memos` is optional. The
  indexer diffs `(person_id, path, sha256)` and re-embeds only changed
  documents. Embeddings come from fastembed (`bge-small-en-v1.5`, 384 dims).
- The agent searches with the in-process `recall` server (`search_memory`).
  Each turn also gets a confidence-gated injection (`engine/injection.py`):
  `full` above `inject.full_sim`, `hint` above `inject.hint_sim`, else
  nothing. It never breaks a turn. Every decision writes one `kb_event` row.
- The recency tier is separate (`memory/prompt.py::build_memory_block`): the
  person's profile page and, for a member or a group session, the shared
  profile. A guest gets no shared profile section. The journal reaches a turn
  only through retrieval, never through this block.
- Nightly reflection (`memory/nightly.py`) writes the corpus at
  `memory.nightly_at`: one selective page for the whole day at
  `wiki/journal/YYYY/MM/DD/YYYY-MM-DD.md`, or none when nothing is worth
  keeping, a new profile page at
  `wiki/people/<id>.md` when something durable changed, and
  `wiki/people/everyone.md` from the group chats. The daily rollover then
  closes every session.
- On-demand journaling: `prompts/builtin/people.md` tells the agent to write a
  short journal entry, in Joshua's own voice, when something a person shares
  is worth keeping. `people[].journal` (`auto`, `ask`, `off`) is consent for
  what a person says about themselves. The agent writes an entry with the
  gateway tool `write_journal_entry(slug, markdown, people)`. The `stub`
  backend does the same on a `[[journal]]` marker, so the flow is testable
  without the SDK.

`docs/memory.md` documents every knob. The admin routes are
`POST /admin/kb/reindex`, `GET /admin/kb/status`, `GET /admin/kb/events`, and
`POST /admin/reflect`.

## Runtime images

`core/Dockerfile` bundles the Claude Code CLI, which the SDK starts as a Node
subprocess. The CLI needs Node 22, so the runtime stage installs it from
NodeSource. Node, npm, and the CLI version are pinned in one `RUN`. npm 11
blocks a dependency's install scripts by default, and the CLI's `postinstall`
links the platform binary, so the install passes
`--allow-scripts=@anthropic-ai/claude-code` and the `RUN` ends with
`claude --version`, which fails the build when the binary is missing.

## Agent tools

Never add `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, or
`WebFetch` to the agent's tool list. `engine/agent.py` passes `tools=[]` and
asserts it. Every capability is an MCP server: `recall`, `scheduling`, and
`registration` inside `core`, and `files` plus every configured upstream behind
`gateway`. If a task seems to need a built-in tool, the task is wrong. Open an
issue.

## Coding rules

- Type hints everywhere. `ruff` clean. Tests offline by default: no network,
  no SDK, no Docker. Mark a test that needs Postgres `@pytest.mark.integration`.
- Structured JSON logs through `joshua_shared.log`. Never log a message body at
  INFO. Never log a token. The formatter redacts a bearer and a credential
  prefix, but do not rely on it.
- No behavior behind an undocumented environment variable. Every knob is in
  `joshua.yaml` or documented in `.env.example`.
- Additive schema only: `CREATE TABLE IF NOT EXISTS` and
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in one `SCHEMA_SQL` string, applied
  at start. A release never removes a table or a column.
- Path safety: every path from the agent or a person goes through
  `joshua_shared.layout` or `files_mcp.paths.resolve`. Never build a data
  volume path by hand.
- Security-relevant behavior gets a test that proves the negative: a denied
  path, a 403, a missing tool. If a doc says "cannot", a test tries.

## Documentation rules

- **Simplified Technical English.** Docs, READMEs, docstrings, comments, PR
  text, commit messages, error messages, and release notes follow ASD-STE100.
  The `ste-writing` skill in `.claude/skills/ste-writing/` holds the rules and
  a linter. Use STE-flavored mode for prose and strict mode for procedures and
  error messages. Score a page with
  `python3 .claude/skills/ste-writing/scripts/ste-lint.py docs/<page>.md`.
  Aim for less than 2.5 violations per 100 words on prose and near 0 on a
  procedure. The rule does not apply to code, identifiers, or command syntax.
- **Write the minimum the reader needs, and only the current behavior.** A
  docstring or a comment says what the code does now. Never narrate history in
  code ("this used to", "changed from"). That belongs in the commit message.
- **Docs are shipped.** Core copies `docs/*.md` into the wiki at each start and
  the index holds them, so a person can ask Joshua how to do something. A page
  must be true. When a change alters behavior, the same PR changes the page.
- `docs/CHANGELOG.md` gets one entry per change a person who runs Joshua can
  see.

## Tests

- Every PR adds or changes tests for the code it touches. A new module without
  a test that imports it fails `scripts/check_test_policy.py`.
- Coverage floor: 80 percent per member (`--cov-fail-under=80`).
- No `@pytest.mark.skip` or `xfail` without a reason that names an issue.
- The `stub` agent backend (`AGENT_BACKEND=stub`) makes `core` testable without
  the SDK. Extend the stub instead of mocking around it.
- Run before you push:

```
make lint
make test
uv run python scripts/check_test_policy.py
```

`make smoke` boots Postgres in compose and runs the integration suite against
it. `make e2e` runs the compose-level tests.

## Workflow

- One branch per issue, named `<issue>-<slug>`. One PR per issue. Squash merge.
  CI must be green.
- The PR description says what changed and why, in STE, and ends with
  `Closes #<issue>`.
- A commit message has a short subject, a body that says why, and the
  `Co-Authored-By:` trailer when an agent wrote the change.
- An issue has: Goal · Why (which of the three goals) · Spec · Acceptance
  criteria · Tests · Out of scope · Depends on.

`CONTRIBUTING.md` has the fork and upstream setup.

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

- `lint`: `uv sync --frozen --all-packages`, `uv lock --check`,
  `ruff check .`, `ruff format --check .`, and `scripts/check_test_policy.py`.
- `test`: one job per member with `--cov-fail-under=80`. The `core` job also
  runs the `-m integration` suite against a `pgvector/pgvector:pg16` service.
- `build`: on `main` only, `docker compose build` for the three images.

`scripts/ci-local.sh` runs the lint and test jobs with the same commands.

## Deployment

Two deployment paths ship here: `docker-compose.yml` for one host, and the Helm
chart in `charts/joshua` for Kubernetes. Both take released images from the
GitHub container registry, and neither builds. `docker-compose.dev.yml` adds a
build for a developer.

Never add the detail of one installation to this repository: no hostname, IP
address, roster, credential, or manifest for a particular deployment. Every
such value belongs in a values file or an environment file that the person who
runs Joshua keeps.
