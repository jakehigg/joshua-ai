# Memory

Joshua remembers each person through Markdown files on the data volume. The
in-core RAG indexes those files and injects the relevant parts into each turn.
This page covers how long-term memory is written, and how Joshua finds it again.

## How Joshua finds what it wrote

Joshua does not read every file on each turn. It searches them.

Core keeps an index of the Markdown on the data volume. Each file is cut into
chunks. Each chunk gets a vector from the embedding model. When a person sends a
message, core turns that message into a vector too, and finds the chunks that
are closest to it.

### What is in the index

| Path | Who can find it |
|---|---|
| `people/<id>/blog/**.md` | that person |
| `wiki/**.md` | everyone |
| `shared/**.md` | everyone |

A person finds their own journal, the wiki, and the shared files. They never
find another person's journal. A group chat finds the wiki and the shared
files, because no one person owns the turn. A guest follows the same rule as a member: their own files, plus
shared.

`.trash/` directories and `*.meta.json` files are not indexed.

The indexer re-reads the volume every `memory.index_interval_s` seconds (60 by
default). It compares each file against the index by content, so a file that did
not change is not embedded again. A new note is findable within one interval.

### Two ways a chunk reaches the agent

**The turn brings it.** Before each turn, core searches with the person's
message and puts what it finds in front of the agent. The agent does not ask for
this and does not spend a tool call on it.

**The agent asks for it.** The agent also has a `search_memory` tool. It uses
this when it wants something the turn did not bring, for example a second search
with different words.

### What the turn brings, and when

Core compares the best match against two thresholds:

| Decision | When | What the agent gets |
|---|---|---|
| `full` | best match ≥ `inject.full_sim` (0.72) | the text of the top `inject.top_k` chunks |
| `hint` | best match ≥ `inject.hint_sim` (0.60) | the names of up to three files to open |
| `none` | below `inject.hint_sim` | nothing |

A higher number means a closer match. `full` is the fast path: the agent has the
text already and answers with no tool call. `hint` names the files, and the agent
opens the ones it wants.

### Seeing what happened

Each turn writes one audit row. Read the recent rows with:

```
curl -s -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP" \
  127.0.0.1:8081/admin/kb/events
```

A row holds the question, the decision, and the best match. It also holds each
chunk core considered, with its file, its score, and whether the turn used it.
Read this when an answer is wrong. It says what Joshua read before it answered.

`GET /admin/kb/status` reports the state of each source and the chunk count.
`POST /admin/kb/reindex` re-indexes on demand.

### Tuning it

The knobs are the `memory` section of `joshua.yaml`. See
[config.md](config.md#memory) for each one.

Two rules of thumb:

- Answers miss things that are in your files: lower `inject.hint_sim`, or raise
  `inject.top_k`.
- Answers pull in text that is not relevant: raise `inject.full_sim`.

Change one value at a time, then read `/admin/kb/events` for a few real
questions before you change another. Core reads these values at start, so
restart core after an edit:

```
docker compose restart core
```

## Two ways a blog post is written

A person's blog (`people/<id>/blog/`) is the durable record of their life. Two
paths write it:

- **Nightly reflection** writes one digest per day (`blog/YYYY-MM-DD.md`) from
  that day's conversations. See the `memory` section of `CLAUDE.md`.
- **On-demand journaling** writes a post the moment a person shares a life
  update in chat. This is the primary way memory is created.

## On-demand journaling

When a person shares an event, a change, a milestone, or a photo with context,
the agent writes a short post with the files MCP `write_file` tool. The agent
passes a plain slug such as `blog/garden.md`. The files MCP stamps the date and
time onto the filename, so the post lands at
`blog/YYYY-MM-DD-HHMM-<slug>.md`.

Rules the agent follows (see `prompts/builtin/people.md`):

- One post per distinct update.
- First person, in the person's voice, not the agent's.
- Cite each attached file by its `attachments/…` path. In the frontmatter
  `attachments:` list and once in the body.
- Do not post for a question, chit-chat, or a request.

A post goes to the speaker's own blog. In a group chat it goes to the resolved
sender's blog, never to a shared file. A group turn with no resolved sender
writes no post.

### Per-person preference

Each person has a `journal` setting in `people[]`:

- `auto` (default). The agent writes the post on its own.
- `ask`: the agent offers first and writes only after the person agrees.
- `off`: the agent never posts on its own. It posts only when asked.

### Post format

```
---
date: 2026-08-27T14:32:00-04:00
person: alex
source: chat
attachments: [attachments/2026/08/2026-08-27-143210-IMG_4471.jpg]
---
Started using a new fertilizer on the garden today. It has grown a lot in the
last three months.
```

The indexer picks up the new post and chunks it into `kb_chunk`, scoped to the
person. A later turn that asks about the same topic gets the post back through
per-turn injection or the `search_memory` tool.

## Source adapters

The indexer reads through source adapters, not files directly. `files` is the
kernel adapter and always runs. Other adapters are optional. Each is one entry
under `memory.sources`. A source that fails to list is isolated. It never
blocks `files`, and its last error shows at `GET /admin/kb/status`. Remove a
source from `memory.sources` and its rows are purged on the next run.

### `memos`

The `memos` adapter scrapes a [Memos](https://usememos.com) server into the
index, read-only. Joshua never writes to Memos. The token needs read access
only. Give the Memos user no more visibility than you want indexed.

```yaml
memory:
  sources:
    memos:
      url: https://memos.example.net
      token: ${MEMOS_TOKEN}          # from .env, never inline
      schedule: nightly              # nightly | "every 6h" | seconds
      scope: shared                  # shared | person:<id>
      visibility:                    # PRIVATE memos are never indexed
        - PROTECTED
        - PUBLIC
      person_tag_prefix: "person/"   # a memo tagged person/alex lands under alex
```

- **Visibility**: only memos with a visibility in the list are indexed.
  `PRIVATE` is never indexed unless you list it.
- **Scope**: a memo lands under `scope`. `shared` puts it in the shared
  scope, which every person sees (a member, a guest, and a group session all
  read shared rows). For memos private to one member, use `scope: person:<id>`
  or a person tag. A memo tagged `person/<id>` (the `person_tag_prefix`) lands
  under that person and overrides `scope`.
- **State and tags**: an archived memo (state not `NORMAL`) and a memo tagged
  `deprecated` are skipped. A `deprecated` memo already in the index is purged.
- **Change tracking**: the memo `updateTime` is the change marker, so a re-run
  re-embeds only memos that changed.
- **Schedule**: `nightly` (the default) re-indexes once a day. `every 6h` or a
  plain number of seconds sets a different cadence.

## The embedding model

The index embeds text with `fastembed` and `BAAI/bge-small-en-v1.5` (384
dimensions). The model is about 65 MB. It is not in the image. Core downloads it
from `huggingface.co` on the first start.

`MEMORY_EMBED_CACHE_DIR` sets where the model is kept. The default is
`/home/app/.cache/fastembed`. Compose mounts the `model-cache` volume there, and
the k8s overlay mounts the `model-cache` subdirectory of the data volume. So core
downloads the model one time.

Do not put the cache under `/data/inbox`. Channels deletes each item there that
is more than one hour old.
