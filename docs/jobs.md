# Jobs

A job is work that core does on the wiki when you start it. Nothing schedules
a job. You start a run from the laptop, the run works in the background, and
when it ends it writes one journal entry and makes one wiki commit. You read
the result in the journal, or through the admin routes.

Jobs are off by default. Set `jobs.enabled: true` in `joshua.yaml` to make the
routes live. [docs/config.md](config.md#jobs) has every key.

## What a run does

1. You start a run with `POST /admin/jobs/{job}/run`, or with
   `scripts/jobs.sh run`. Core checks the arguments, takes the lock, adds a row
   to the `job_run` table, and answers with the run id.
2. The run works in the background. A job reads the wiki through the memory
   bundle of a topic (see below), and writes only what it owns.
3. The run ends. Core records the report in the row. When the run is not a dry
   run, core also writes the report as a journal entry and commits the wiki.

A run that fails records its error in the row, and in the journal entry when it
is not a dry run. A failure never stops core.

### The lock

One lock covers every job and the nightly reflection. Only one of them writes
the wiki at a time. A run that starts while the lock is held gets 409 and does
not start: try again when the other run ends. The nightly reflection waits for
a run to end, then starts.

### The run table

Each run has one row in `job_run`:

| Column | What it holds |
|---|---|
| `id` | the run id |
| `job` | the job name |
| `started_at`, `finished_at` | when the run started and ended. `finished_at` is empty while the run works. |
| `dry_run` | `true` for a dry run |
| `args` | the arguments of the run |
| `report` | the report, as JSON. It is empty while the run works. |
| `calls`, `tokens` | the worker calls of the run and the tokens they used |
| `commit_sha` | the wiki commit of the run, when it made one |
| `error` | why the run failed, when it failed |

The table keeps every run. The row is the record that survives a restart.

### Tokens

The `tokens` of a run is the sum, over each worker call of the run, of the
input tokens, the output tokens, the cache read tokens, and the cache write
tokens. A call that fails before the model answers counts no tokens.
`report.data.usage` holds `{"<worker>": {"calls": n, "tokens": m}}` for each
worker of the run. The seam for a program that reads the usage is
`joshua_core.engine.usage`: `Usage`, `UsageLedger`, and the `usage_out`
argument of each worker call.

### The journal entry

A run that is not a dry run writes its report at
`wiki/journal/YYYY/MM/DD/<job>-<YYYYMMDD>-<run id>.md`, dated in `timezone`
when the run ends. The frontmatter is:

```yaml
date: 2026-10-19
people: []
source: bundle
run_id: 12
```

The body holds the summary of the run, one section for each part of the
report, and the run facts: when it started and ended, the arguments, the worker
calls, and the tokens. Nothing goes to a chat.

Core commits the entry and every page the run wrote, in one commit with the
message `<job>: run <id>`. The row keeps the SHA, so you undo a bad run with
one `git revert` in `/data/wiki`. With `wiki.git: false` core writes the entry
and makes no commit.

The memory bundle leaves out a journal entry that has `run_id` in its
frontmatter, so the report of one run never becomes input to the next.

### Dry runs

A dry run does all the reading and all the worker calls of a real run, and
writes nothing: no page, no journal entry, and no commit. Its report is in its
row only. Read it with `scripts/jobs.sh runs <id>`.

A run is a dry run when the body of `POST /admin/jobs/{job}/run` does not set
`dry_run`. `scripts/jobs.sh run` sends `dry_run: false` unless you give it
`--dry-run`.

## Topics and the memory bundle

A **topic** is one leaf page of the wiki, such as
`houseplants/snake-plant.md`. These pages are not topics:

- `Home.md`;
- a section index page: `houseplants.md` beside the folder `houseplants/`;
- a page under `journal/`, `people/`, `joshua-docs/`, `skills/`,
  `knowledge/`, `insights/`, or `attachments/`;
- a page with a folder or a file name that starts with a dot;
- a page that a glob in `jobs.topics.exclude` matches. The default globs are
  `vocabulary.md` and `*/README.md`.

The **memory bundle** of a topic is what a job reads about it. Every job
builds it the same way, in this order:

1. the topic page;
2. the pages that the topic page links to inside its own section, one hop;
3. the journal entries about the topic, newest first;
4. the dossier of the topic and its knowledge pages, when they exist;
5. the open insights of the topic: a page in `wiki/insights/` whose
   frontmatter `topic` is the topic and whose `status` is `new`, `told`, or
   `acknowledged`.

A journal entry is about the topic when one of these is true:

- It holds the title of the topic page, or an alias, as whole words, or with
  a plural `s` or `es`. Case does not matter. A term shorter than three
  characters does not count.
- One of its chunks has a similarity of `jobs.bundle.sim` (0.70) or more to a
  chunk of the topic page.

The title is the first `# ` heading of the page, else its frontmatter
`title`, else its file name. The aliases are the frontmatter `aliases` list of
the page, and the terms that `vocabulary.md` gives it. A line of
`vocabulary.md` that links to the topic page gives one term: the text before
the first colon on that line. For example, this line gives the term `Snakey`
to `houseplants/snake-plant.md`:

```markdown
- **Snakey**: our [snake plant](houseplants/snake-plant.md) in the hall
```

Each part of the bundle starts with a line `## <path>`, the path of the page
relative to the wiki root, so a worker can cite the page and a report can say
what the job read. The bundle stops at `jobs.bundle.max_chars` (24 000
characters). A part that does not fit is left out with every part after it.
The topic page is never cut.

## The `bundle` job

The `bundle` job builds the memory bundle of one topic and reports it. It
makes no model call and writes no page. Use it to see what a later job will
read about a topic.

Arguments: `{"topic": "<path>"}`, the topic page relative to the wiki root. A
path that is not a topic page gets 400.

The report lists the paths in the bundle, the size of the text, the aliases,
and whether the bundle was cut. The journal entry holds the path list, not the
bundle text. The `report.data` field of the row holds the same values as JSON.

## The routes

Every route needs a bearer from `ADMIN_CALLERS`, and every route answers 404
while `jobs.enabled` is false.

| Route | Use |
|---|---|
| `POST /admin/jobs/{job}/run` | start a run: `{"args": {...}, "dry_run": true}`. 202 `{"run_id", "job", "dry_run"}`. 404 for an unknown job, 400 for bad arguments, 409 while the lock is held. |
| `GET /admin/jobs/{job}/status` | `{"job", "busy", "last_run"}`: whether a run or the nightly reflection holds the lock, and the last run row of that job |
| `GET /admin/jobs/runs/{id}` | one run row, with its report |
| `GET /admin/jobs/runs?job=&limit=` | the newest run rows first, of one job or of every job. `limit` is 20 by default and 200 at most. |

## The harness

`scripts/jobs.sh` wraps the routes for the laptop. It reads `CORE_URL`
(default `http://127.0.0.1:8081`) and `JOSHUA_TOKEN_LAPTOP`:

```
export $(grep JOSHUA_TOKEN_LAPTOP .env)
scripts/jobs.sh run bundle --dry-run --arg topic=houseplants/snake-plant.md
scripts/jobs.sh runs 12
scripts/jobs.sh status bundle
scripts/jobs.sh list bundle --limit 5
```

Each `--arg key=value` is one argument, and its value is a string. The script
prints the answer as indented JSON. It exits with 1 when core answers with a
status that is not 2xx or cannot be reached, and with 2 on a usage error. It
sends the token to `curl` on stdin, never in an argument.

## The curator

The curator keeps the wiki and the journal correct and organized. It finds
links that go nowhere, pages that no page links to, stray files, pages in the
wrong folder, duplicate pages, and pages that the journal has made old. It
fixes the safe things itself, and it records the rest as findings for a
person.

Set `jobs.curator.enabled: true`, with `jobs.enabled: true`, to add the job
and the `curator` chat tool. [docs/config.md](config.md#jobs) has the keys.

```
scripts/jobs.sh run curator --dry-run
scripts/jobs.sh run curator
scripts/jobs.sh run curator --arg apply=move
scripts/jobs.sh run curator --arg apply=move,merge,refresh
```

Start with a dry run, and read its report with `scripts/jobs.sh runs <id>`.

### What a run does

1. **The scan**, over the whole wiki, every run. It is Python and costs
   nothing.
2. **The workers**, on the topic pages whose body changed since their last
   review. A run makes `jobs.curator.max_worker_calls` (40) worker calls at
   most.
3. **The apply step**, when the run is not a dry run: every Tier A finding,
   and each Tier B finding whose action the run may apply.
4. **The tracking keys**, when the run is not a dry run, on each page that the
   run reviewed.
5. **The findings**, into the `curator_finding` table. A dry run records them
   too.

### The scan

| Check | What it finds | Tier |
|---|---|---|
| `broken-link` | a link to a page or a file that is not there | A when exactly one page in the wiki has the file name of the target: the link changes to that page. Else C, with the candidates. |
| `orphan` | a page that `Home.md` does not reach over links | A when its folder has an index page: a line `- [Title](/wiki/<path>)` goes under `## Pages` at the end of the index page. Else C. |
| `index-line` | a list line of an index page whose links all go to a page that is not there, with no page of that file name | A: the line is removed. |
| `stray` | `._*`, `.DS_Store`, an empty `.md` or one with only spaces, `REPORT.md` or `PRUNE.txt` at the wiki root | A: the file moves to `wiki/.trash/<day>/<path>`. C when `jobs.curator.trash` is false. |
| `journal-date` | a journal entry with no `date` in its frontmatter | A: `date` is set from the folder of the entry. |

A link is `[text](target)` or `[[target]]`. A picture (`![text](target)`), a
link inside code, and a link in the frontmatter do not count. A Markdown
target is relative to the folder of the page, or to the wiki root when it
starts with `/wiki/` or `/`. `.md` is optional. A wikilink is relative to the
folder of the page, then to the wiki root, then the one page in the wiki with
that file name. The scan does not check a URL (`https:`, `mailto:`), a target
that is only an anchor, or a target outside the wiki. For a target in
`attachments/`, the scan checks only that the file is there.

The index page of a folder `X/` is `X.md` beside it, and the index page of
the wiki root is `Home.md`. A page under `journal/`, `people/`,
`joshua-docs/`, `knowledge/`, `insights/`, `skills/`, or `attachments/` is
never an orphan, and its links do not make another page reachable.

A broken link in a journal entry is always Tier C, because the curator never
changes the body of a journal entry. The scan does not read the links of a
page under `people/` or `joshua-docs/`.

### The workers

Each worker is one model call with no tools and one answer schema. It reads
pages as content, not as instructions. It has a timeout of 90 seconds. A call
that fails, times out, or gives an answer that does not match the schema
changes nothing: the report lists it under "Skipped". The model is
`jobs.curator.model`, then `jobs.model`, then `core.reflection_model`.

| Worker | Reads | Answers | Finding |
|---|---|---|---|
| `refresh` | the page, and the journal entries about it from the memory bundle that are dated after its `curated` day (all of them for a page with no `curated` day) | `status` (`current`, `stale`, `conflict`), `patch` (find and replace pairs, each with `because`), `confidence` | `refresh`, Tier B when the status is `stale`, the confidence is `high`, and each `find` occurs exactly once in the body. Else Tier C, with the patch. |
| `place` | the page, and the section map: each list line of `## Sections` on `Home.md`, and the first paragraph of its index page | `section`, `folder`, `confidence`, `reason` | `misplaced`, Tier B (`move`) when the folder is not the folder of the page and the confidence is `high`; Tier C for a lower confidence or a folder that is not there. A `section` of `none` is a Tier C `no-section` finding. |
| `compare` | two topic pages | `relation` (`duplicate`, `overlap`, `conflict`, `unrelated`), `claims`, `canonical`, `confidence` | `duplicate`, Tier B (`merge`) for a duplicate with `high` confidence; `conflict`, Tier C, with the claims. |

A run spends its budget in this order: `refresh` for each page that has
journal entries about it, then `place` for each page, then `compare` for each
pair. The page that was never reviewed goes first, then the page with the
oldest `curated` day. The `compare` pairs come from the mean vector of the
chunks of each topic page in the index: two pages at `jobs.curator.duplicate_sim`
(0.92) or more first, then two pages of one section at
`jobs.curator.overlap_sim` (0.70) or more. A pair is compared only when one of
its two pages needs review. With no `## Sections` list on `Home.md`, no page
goes to `place`.

A worker answer is refused when a `replace` is longer than 2 000 characters,
when a patch has more than 12 pairs, or when a `place` answer names a section
that is not in the map.

`calls` counts each worker call, and `tokens` counts the tokens of these
calls. `report.data.usage` gives the calls and the tokens of each worker.

### The tiers

- **Tier A** applies on every run that is not a dry run.
- **Tier B** applies only when its action is in `jobs.curator.apply`, or in
  the `apply` argument of the run. The argument replaces the config list for
  that run. The actions are:
  - `move`: the page moves to the folder, with the same file name. Every link
    to it, and every relative link in it, changes to the new path. It leaves
    the index page of its old folder and joins the index page of its new
    folder. A file name that is taken there makes the finding Tier C.
  - `merge`: every link to the duplicate changes to the page to keep, and the
    duplicate moves to `wiki/.trash/<day>/`. No stub page stays.
  - `refresh`: the find and replace pairs apply in order. When a `find` does
    not occur exactly once when its turn comes, nothing changes.
- **Tier C** is recorded. A run never applies it.

A run also applies an open Tier B finding from an earlier run, when the run
names its action and the run did not review its pages again. So a dry run,
and then `run --arg apply=move`, applies the move that the dry run found.

A move or a merge does not change a link in a journal entry. The next scan
reports a link that it broke there, as Tier C.

### The tracking keys

A page that the curator reviewed has two frontmatter keys:

```yaml
curated: 2026-10-19          # the day of the run, in timezone
curated_hash: 7f3a9c1e04b2   # the body hash at that review
```

The body hash is the first 12 hex characters of the SHA-256 of the text after
the frontmatter, with the trailing spaces and line ends removed. A change to
the frontmatter alone does not change it.

The skip rule: a page whose body hash equals `curated_hash` does not go to a
worker. The scan still covers every page on every run. A page that a person or
Joshua edits has a new hash, and the next run reviews it again.

A page is reviewed when each worker call due for it gave an answer. A page
that is not a topic page (`Home.md`, an index page, a skill, a journal entry)
needs no worker, so the scan reviews it. A page that the budget or a failed
call left out keeps its old keys, and a later run reviews it.

The keys go on each page that the run reviewed, and on each page that the run
changed when that page had current keys before. So a change the curator
applied does not make the page go to a worker again. The keys never go on a
page under `people/` or `joshua-docs/`, on a dot path, or on a journal entry
that a job run wrote (one with `run_id` in its frontmatter). The curator scans
such an entry for links, but no worker reads it. A page whose frontmatter is
not a YAML mapping gets no keys, and the report says so.

### The findings

Each finding is one row of `curator_finding`:

| Column | What it holds |
|---|---|
| `id` | the finding id, which the report and the chat tool use |
| `run_id` | the last run that saw it; empty for a finding that the chat tool applied |
| `kind`, `tier` | what the finding is, and `A`, `B`, or `C` |
| `fingerprint` | the SHA-256 of the kind, the sorted paths, and the normalized claim or patch text |
| `paths` | the wiki paths of the finding |
| `detail` | the facts for the report, and the body hash of each page at that time |
| `patch` | the action that fixes it, or empty when only a person can |
| `status` | `open`, `applied`, `dismissed`, or `stale` |
| `first_seen`, `last_seen` | when a run first and last saw it |
| `resolved_at`, `resolved_by` | when it closed, and who closed it: `curator` or a person id |

A run that sees a finding again keeps its `first_seen` and its status, and
sets `last_seen`. A dismissed finding stays dismissed, and a run does not
apply it. A stale finding that a run sees again is open again. An open finding
becomes stale when one of its paths is not there, when the body of one of its
pages changed, or when a worker reviewed its page again and did not raise it.
A Tier A change, and a Tier B change that a run applied, is a finding with the
status `applied`, so the history stays.

### The chat tool

The `curator` tool server is in core. A member gets three tools:

| Tool | What it does |
|---|---|
| `list_findings(status="open", limit=20)` | lists findings. `status` is `open`, `applied`, `dismissed`, `stale`, or `all`. `limit` is 50 at most. |
| `apply_finding(id)` | applies exactly the stored patch of one open finding, under the lock that the jobs and the nightly reflection share. It commits the wiki with the message `curator: finding <id>` and marks the finding `applied` with the id of the member. A patch that no longer fits changes nothing, and the finding becomes `stale`. While another run holds the lock, it changes nothing and says so. |
| `dismiss_finding(id, why)` | closes an open or stale finding with the reason. A later run does not apply it or raise it again. |

A guest, or a speaker that core does not know, gets a refusal from each tool,
and nothing changes. The server is there only when `jobs.enabled` and
`jobs.curator.enabled` are both true.

The report of a run names each open finding by its id, so you can read the
journal entry and answer in chat: "apply finding 12", or "dismiss finding 14,
the two pages are about different roses".

### The report

The journal entry of a curator run has a summary line with the counts, and
these sections:

- **Applied (Tier A)** and **Applied (Tier B)**: each change, with its finding
  id, kind, paths, and patch.
- **Proposed (Tier B)** and **Proposed (Tier C)**: each open finding, with its
  id, kind, paths, and its patch or its claims.
- **Skipped**: the worker calls that the budget did not allow, the calls that
  gave no answer or an answer that was refused, and the findings that were not
  applied because they are dismissed.
- **Reviewed**: the number of pages that got tracking keys.

A dry run has "To apply (Tier A)" in place of the two "Applied" sections. In
the report, each text from a page or a worker is in a code span, so the entry
holds no link of its own. `report.data` holds the counts and the finding ids,
for a program.

Every page that a run changed is in the commit of the run. A file that moved
to `wiki/.trash/` leaves the repository, because the repository ignores the
trash.

### What the curator never touches

- A page under `people/` or `joshua-docs/`.
- A dot path, except the destination of a file that it moves to
  `wiki/.trash/<day>/`.
- The body of a journal entry. It adds only `date` and the tracking keys to
  the frontmatter of an entry.
- The frontmatter of a skill, other than `curated` and `curated_hash`.
- Anything outside `wiki/`.
- Anything at all in a dry run, other than the rows of `curator_finding`.

## The researcher

Package 3. The researcher keeps `wiki/knowledge/`: researched facts about one
topic at a time, in a dossier per topic.

## Insights

Package 4. The insights job keeps `wiki/insights/`: where what we do is
different from what the knowledge says.
