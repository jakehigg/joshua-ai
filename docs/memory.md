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
| `wiki/**.md`, including `wiki/journal/` and `wiki/people/` | everyone |
| `shared/**.md` | everyone |

Joshua keeps its memory in one folder, the wiki. `wiki/journal/` is Joshua's
own journal: what happened, in Joshua's voice, third person, names attached.
`wiki/people/<id>.md` and `wiki/people/everyone.md` are the profiles. Every
page in the wiki is shared scope, so a search reaches the whole corpus for
every person. A question about last Tuesday reaches the journal entry that
names the person it happened to, whoever asks.

A group chat searches the same corpus as a direct message. A guest searches it
too: reading is not what separates a guest from a member. Writing is.

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

Each source also reports `empty`: the documents that hold nothing to index,
such as a zero-byte file. They are not a fault, and they are named so that a
pass which reports no work is explainable.

Each source also reports `unindexable`: the documents the index cannot hold,
with their paths. A document gets there when the fault is its own, such as a
`memos` entry tagged for a person who is not on the roster. It is passed over
until its content changes, a full reindex asks for it, or core restarts, so
one bad document does not make the same failure on every pass. When the count
is not zero, look at the paths and fix the source, or add the person.

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

## Taught skills

A person can teach Joshua a skill: "when I say X, do Y". A taught skill is one
Markdown file at `wiki/skills/<slug>.md`. The frontmatter says what the page
is, who it is for, and how it is matched; the body is the instructions.

```
---
name: movie time
kind: command
for: everyone
match: phrase
triggers: ["movie time", "start movie night"]
---
Dim the living room lights to 30 percent and turn on the TV.
```

| key | value | default |
| --- | --- | --- |
| `name` | the display name | the slug |
| `kind` | `command` or `convention` | `command` |
| `for` | `everyone`, `members`, `guests`, a person id, or a list | `everyone` |
| `match` | `phrase` or `semantic` | `phrase` |
| `triggers` | the phrases that fire it | none |

### Two kinds of page

A **command** page is a behaviour a person asks for by name. It holds triggers
and it fires when one matches.

A **convention** page is standing guidance for a domain: how to choose a device
entity, how the task lists are laid out. It holds no triggers and it never
fires from a phrase. It is indexed as an ordinary wiki page, so a turn reaches
it through recall when the subject comes up. Give a page of guidance
`kind: convention`: a trigger invented for it fires it on turns nobody meant.

### How a phrase is matched

A command page is matched by phrase, not by meaning. Three rules make the match
near-exact:

1. **The turn opens with the trigger.** Only address and politeness may come
   first, so "can you start movie night please" fires and "what does movie
   time do" does not. A turn that says something else first is talking about
   the behaviour, not asking for it.
2. **Every word that carries the request appears, in order.** An article the
   person left out does not break the match.
3. **Up to `memory.skills.max_extra_words` extra words** that carry meaning may
   sit inside the match. Two is enough for one inserted object ("add *milk* to
   the list") and few enough that a sentence which merely holds the words does
   not fire.

Set `match: semantic` for an intent that is genuinely said many ways, and the
page is matched by meaning instead, above `memory.skills.min_sim`. Choose it by
the cost of a wrong fire: a page that only shapes an answer can be loose, a
page that turns on a device must not be. Similarity over short phrases has a
high floor — two unrelated triggers measure about 0.62 with the default model —
so `min_sim` is 0.80 and a lower value means very little.

One turn fires one skill. Raise `memory.skills.top_k` to fire more. When two
skills match, the one whose trigger matched more words wins: a page for one
room beats a page for the whole house.

### What makes a usable trigger

A trigger is refused, with a warning that names the file and the reason, when
it holds fewer than two words, or when it ends with an article, a preposition,
or an auxiliary. `announce` fires on every turn that mentions it, and
`turn on the` is the front half of a sentence. Write the phrase the way a
person says it, and keep the verb.

### Who can teach, who can fire

A member writes `wiki/skills/<slug>.md` through the files MCP. The wiki is
read-only for a guest, so a guest cannot teach a skill or change one.

`for` says who a skill fires for. It is a match rule, not a permission: a guest
can still read any page of the wiki. Use it when a skill holds one person's own
choices, so that two people can teach the same words and each get their own
result.

A trigger row is an instruction, not a note. It never appears in the per-turn
injection note and never comes back from `search_memory`.

### The index delay

A skill starts to work after the next index pass
(`memory.index_interval_s`, 60 seconds by default). A skill never fires on the
turn that taught it. To stop a skill, a member sets `triggers: []` or deletes
the file. A deleted file stops firing on the next pass.

## The journal is Joshua's, not a person's

`wiki/journal/YYYY/MM/DD/` is Joshua's own journal: what happened, in
Joshua's voice, third person, names attached. It is episodic memory. There is
one journal and it belongs to Joshua; a person is named in a page, never the
owner of one. Two kinds of page live in one day folder:

- `YYYY-MM-DD.md`: the nightly page. Nightly reflection writes it.
- `<slug>.md`: an entry Joshua wrote during the day.

Every page carries front matter: `date`, `people` (the ids it names), and
`source` (`nightly` or `agent`). The journal is selective. It holds what would
matter in a month: a fact about someone's life, a decision, a plan, a visit, a
change. It never holds a tool call, a routine automation, the weather, or
small talk. A day with nothing worth keeping gets no page.

The journal also leaves out somebody operating Joshua. A request to do a thing
is not a life update, whatever words it uses. This holds when the tool keeps a
record of its own.

A person who asks Joshua to add an entry to a journal, a blog, or notes on
another service gets that entry there, and the journal here does not copy it.
What a person says in the conversation itself is still journal material.

The split with the profile runs both ways. A durable trait belongs in the
profile: what a person is reliably like, what they prefer, what they avoid.
Transient state belongs in the journal: a timer, a plan for the day, a
one-off reminder. Each prompt carries the rule that points to the other.

### The test for the wiki against the journal

One test decides which store a thing goes in, and Joshua applies it rather
than working from a list of subjects:

- If keeping it current means **rewriting the page**, it is the wiki. How
  something works, a decision, a reference, what is set up where.
- If it means **adding another dated line**, it is the journal, one entry each
  time. Anything that carries a time is episodic, whatever its subject: a
  measurement, an observation, one occurrence of a thing that recurs.

So Joshua does not invent a wiki page that grows dated rows. A person who asks
Joshua to keep track of something has stated a preference about how Joshua
works, which the profile holds; each occurrence is its own journal entry.

A person can still ask for the page. Some histories are easier to read in one
place than to search for, a changelog for example. Ask for that page and
Joshua makes it and keeps it current. The request decides, and Joshua goes on
writing an entry for anything that matters on its own.

### The journal reaches a turn only through retrieval

Nobody gets a journal block in the system prompt. The only way a journal entry
reaches a turn is the same as any other wiki page: the confidence-gated
per-turn injection, or the agent's own `search_memory` call. The old knobs
`memory.recent_posts` and `memory.recent_max_chars` still load and validate,
but nothing reads them.

For example, Alex tells Joshua "Sam is coming for dinner tonight." Joshua
writes a journal entry that names Sam. On Wednesday, Sam, a guest,
messages Joshua. Retrieval surfaces Tuesday's dinner entry, and Joshua can ask
how it went.

### Entries written during the day

A person can ask Joshua to keep something ("note that I had oatmeal for
breakfast"), and Joshua also decides on its own when an update is worth an
entry. The agent writes an entry with the gateway tool
`write_journal_entry(slug, markdown, people)`.

`people[].journal` (`auto`, `ask`, `off`) says whether Joshua's journal may
record a person's own life updates. It does not give the person a journal:

- `auto` (default). Joshua writes the entry on its own.
- `ask`: Joshua offers first and writes only after the person agrees.
- `off`: Joshua never writes an entry on its own. It writes one only when
  asked.

A member telling Joshua about a guest is enough for an entry that names the
guest. The setting governs what a person shares about themselves, not what a
member may tell Joshua about someone else.

The journal has one writer: `write_journal_entry`. `write_file` and
`rename_file` refuse a path under `wiki/journal/`, so every entry lands in the
right day folder with the front matter the index reads, and the nightly page
cannot be overwritten by a free write.

The indexer picks up a new or changed journal page like any other wiki page,
and chunks it into `kb_chunk` at shared scope. A later turn that asks about the
same topic gets the entry back through per-turn injection or `search_memory`.

Every write in this section also lands in the wiki's own git history: the
gateway commits an entry when the agent writes it, and the nightly reflection
commits the day page and each profile it wrote, then anything else that
changed. See `wiki.git` in [docs/config.md](config.md#wiki).

## Source adapters

The indexer reads through source adapters, not files directly. `files` is the
kernel adapter and always runs. Other adapters are optional. Each is one entry
under `memory.sources`. A source that fails to list is isolated. It never
blocks `files`, and its last error shows at `GET /admin/kb/status`. Remove a
source from `memory.sources` and its rows are purged on the next run.

The `files` adapter walks `wiki/` and `shared/`. Every document it finds is
shared scope, so it needs no roster to walk the wiki.

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

`GET /readyz` reports the model as `checks.embed`, and `GET /admin/kb/status`
reports it as `embed`. A lost model does not make core unready, because core
still answers a turn. Use the field to see the loss, because the container stays
healthy with no memory.

The directory must be writable for the user that core runs as, uid 1000. A
bind mount, an NFS export, a k8s subPath, or a rootless host can give a
directory that root owns. Core tests the directory at the first embedding. When
it cannot write, it logs `embed cache dir not writable, using a temporary
directory` with both paths and keeps the memory. The model is then downloaded
again on each start, so correct the mount:

```
chown 1000:1000 <the cache directory>
```

Do not put the cache under `/data/inbox`. Channels deletes each item there that
is more than one hour old.
