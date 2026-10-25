# Jobs

A job is work that core does on the wiki when you start it. Nothing schedules
a job. You start a run from the laptop, the run works in the background, and
when it ends it writes one report page and makes one wiki commit. You read
the result in `wiki/job-runs/`, or through the admin routes.

Jobs are off by default. Set `jobs.enabled: true` in `joshua.yaml` to make the
routes live. [docs/config.md](config.md#jobs) has every key.

## What a run does

1. You start a run with `POST /admin/jobs/{job}/run`, or with
   `scripts/jobs.sh run`. Core checks the arguments, takes the lock, adds a row
   to the `job_run` table, and answers with the run id.
2. The run works in the background. A job reads the wiki through the memory
   bundle of a topic (see below), and writes only what it owns.
3. The run ends. Core records the report in the row. When the run is not a dry
   run, core also writes the report as a page in `wiki/job-runs/` and commits
   the wiki.

A run that fails records its error in the row, and in the report page when it
is not a dry run. A failure never stops core.

Core cleans the text fields of each worker answer before it uses them: a tag
that the model put into a field, such as `</answer>` or
`<parameter name="sources">`, is removed with the text after it, and the
researcher's editor gets an empty field back from a copy that it put into
`answer`. The log line `worker answer cleaned` names the fields. The `find`
and `replace` of a curator patch are never changed.

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
tokens. A call that times out counts the tokens of each model message that
arrived before the timeout. A call that fails before the model answers counts
no tokens.
`report.data.usage` holds `{"<worker>": {"calls", "tokens", "input",
"output", "cache_read", "cache_write", "model"}}` for each worker of the run.
`tokens` is the total of the four counts. `model` is the model of the worker,
or `null` when the job did not name one. The "Usage" section of the report
page shows the model beside each worker. The seam for a program that reads the usage
is `joshua_core.engine.usage`: `Usage`, `UsageLedger`, and the `usage_out`
argument of each worker call.

### Which model runs which worker

Each worker takes the first model that is set in its chain. So you can give
the workers that judge a stronger model and keep the others on a cheaper one.

| Job | Worker | What it does | Its key | Then |
|---|---|---|---|---|
| research | `raise` | adds the questions of a topic | `jobs.research.models.raise` | `jobs.research.model`, `jobs.model`, `core.reflection_model` |
| research | `apply` | answers a question from the knowledge we hold | `jobs.research.models.apply` | `jobs.research.model`, `jobs.model`, `core.reflection_model` |
| research | `editor` | writes the page from the two lookups | `jobs.research.models.editor` | `jobs.research.model`, `jobs.model`, `core.reflection_model` |
| research | `lookup` | the researcher and the challenger, through the internet agent | `jobs.research.models.lookup` | `jobs.research.model`, `jobs.model`, `internet.model` |
| curator | `place` | finds the section of a stray page | `jobs.curator.models.place` | `jobs.curator.model`, `jobs.model`, `core.reflection_model` |
| curator | `compare` | reads two pages that overlap | `jobs.curator.models.compare` | `jobs.curator.model`, `jobs.model`, `core.reflection_model` |
| curator | `refresh` | writes a stale page again | `jobs.curator.models.refresh` | `jobs.curator.model`, `jobs.model`, `core.reflection_model` |
| insights | `insight` | finds a defect in our own setup | `jobs.insights.model` | `jobs.model`, `core.reflection_model` |
| insights | `rank` | orders the open insights when there are more than `jobs.insights.max_open` | `jobs.insights.model` | `jobs.model`, `core.reflection_model` |
| digest | `digest` | writes one entry for a run of near-duplicate journal entries | `jobs.digest.model` | `jobs.model`, `core.reflection_model` |

The `use_internet` chat tool always uses `internet.model`. In the usage of a
run, the lookups are the `internet` worker.

### What a run cost

When a run ends, also when it fails and also for a dry run, the runner writes
one row for each worker of the run to the `usage_event` table. A job that
raises before it returns its report keeps its counts too: they go into the
`job_run` row and into those rows. The usage page of the viewer, `/usage`,
shows the tokens of each job by day, beside the chat. An operator reads the
same numbers at `GET /admin/usage?days=N`. See
[operations.md](operations.md#the-usage-page).

### The run report

A run that is not a dry run writes its report at
`wiki/job-runs/YYYY/MM/<job>-<YYYYMMDD>-<run id>.md`, dated in `timezone`
when the run ends. The frontmatter is:

```yaml
date: 2026-10-19
job: bundle
run_id: 12
index: false
```

The body holds the summary of the run, one section for each part of the
report, and the run facts: when it started and ended, the arguments, the worker
calls, and the tokens. Nothing goes to a chat.

Core commits the report and every page the run wrote, in one commit with the
message `<job>: run <id>`. The row keeps the SHA, so you undo a bad run with
one `git revert` in `/data/wiki`. With `wiki.git: false` core writes the
report and makes no commit.

A run report is not memory, and it is not a journal entry. The memory index
leaves out the whole `wiki/job-runs/` folder, so a search and the per-turn
injection do not return a report, and the memory bundle never reads the
folder. Thus the report of one run never becomes input to the next, and a
report that repeats a question does not take the place of the page that
answers it. No topic is in `job-runs/`, the curator does not read or change a
page there, and the files tool of the agent cannot write there. To read a
report, use `scripts/jobs.sh runs <id>`, the viewer, or the file in
`/data/wiki`.

A report from before this folder is a journal entry with `run_id` in its
frontmatter. The index and the bundle leave such an entry out too.

A journal entry that a digest covers holds `digest` in its frontmatter, and
the index and the bundle leave it out too. See [The digest](#the-digest).

The dossiers and the `README.md` lists that the jobs write hold
`index: false` for the same reason: they list questions, and Joshua answers
from the pages they link to. The knowledge pages, the question pages, and the
insight pages are indexed, but a chat turn gets them only in the ways that
[Retrieval in chat](#retrieval-in-chat) gives. See
[What is in the index](memory.md#what-is-in-the-index).

### Dry runs

A dry run does all the reading and all the worker calls of a real run, and
writes nothing: no page, no report page, and no commit. Its report is in its
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
  `knowledge/`, `insights/`, `questions/`, `job-runs/`, or `attachments/`;
- a page with a folder or a file name that starts with a dot;
- a page that a glob in `jobs.topics.exclude` matches. The default globs are
  `vocabulary.md`, `*/README.md`, and `coach/profiles/*`;
- a **person page**: a page about one person on the roster. Its file name,
  without `.md`, is the id of a person, or its title, or a name from its
  title or its file name, is the id or the display name of a person. The
  comparison uses the whole name, in any case. So `coach/alex.md` and a page
  with the title "Alex" are not topics. A recipe with the title "Alex's caldo
  verde" is a topic.

The **memory bundle** of a topic is what a job reads about it. Every job
builds it the same way, in this order:

1. the topic page;
2. the pages that the topic page links to inside its own section, one hop;
3. the snapshot block: the items of live systems that are close to the topic
   page (see [Snapshots in the bundle](#snapshots-in-the-bundle));
4. the journal entries about the topic, newest first;
5. the dossier of the topic and its knowledge pages, when they exist;
6. the answered questions of the topic: a page in `wiki/questions/` whose
   frontmatter `topic` is the topic and whose `status` is `answered`. The
   bundle holds the question and the answer only. An open or a dismissed
   question is not in the bundle;
7. the open insights of the topic: a page in `wiki/insights/` whose
   frontmatter `topic` is the topic and whose `status` is `new`, `told`, or
   `acknowledged`.

A journal entry is about the topic when one of these is true:

- It holds a name of the topic (see below). This is a `title` match or an
  `alias` match.
- One of its chunks has a similarity of `jobs.bundle.sim` (0.78) or more to a
  chunk of the topic page. This is a `vector` match. The bundle takes
  `jobs.bundle.max_vector_entries` (6) of these entries at most, the closest
  first. An entry that names the topic is not in this count.

These journal entries are never in a bundle:

- the report of a job run: an entry with `run_id` in its frontmatter, or with
  a `source` of `curator`, `research`, or `insights`;
- an entry whose file name, without `.md`, a glob in
  `jobs.bundle.exclude_slugs` matches. The globs are `fnmatch` patterns, and
  case counts. The list is empty by default, because the names belong to one
  instance. For example, `garden-weather-log-*` keeps a daily weather log out
  of every bundle;
- an entry dated after the day of the run.

The names of a topic are these:

- the title: the first `# ` heading of the page, else its frontmatter
  `title`, else its file name;
- the title without a part in parentheses at its end. "Snake Plants (Living
  Room Window)" gives "Snake Plants";
- each alias;
- the first two, three, and four words of the file name, with hyphens as
  spaces. `snake-plants-living-room-window.md` gives "snake plants",
  "snake plants living", and "snake plants living room". A file name of one
  word is a name too.

A text holds a name when it holds the words of the name as whole words, in
any case, with any spaces between them. The name can also have a plural `s`
or `es`, or lose the `s` or `es` at its end. So "Snake Plants" matches "snake
plant" and "snake plants", but not "the plants". A name shorter than three
characters does not count.

The id or the display name of a person on the roster is never a name of a
topic, and never an alias. The name with `s` or `'s` after it ("alexs", "alex
s", "Alex's") is not a name either, and a name that starts with such a form is
not a name. A phrase of the file name that starts with the name of a person,
with or without an `s` after it, is not a name. So a journal entry that only
names a person is not about a topic. It does not come into a bundle, and it
does not count for the selection.

A possessive title names the thing, not the person who owns it. A possessive
is the name of a person with `'s` or `s'` after it, or with `'` alone after a
name that ends in `s`. The generic name is the title without the possessive:
"Alex's Caldo Verde" gives "Caldo Verde", and "Pancakes, Alex's way" gives
"Pancakes". The generic name is a name of the topic, and the full title is
not. So for `recipes/alex-s-caldo-verde.md` the only name is "Caldo Verde",
and "Alex watered the plants" is not about the recipe. The researcher's gate
also puts the generic name in place of the title in a `web` question, before
the route rules. See
[The questions and the route](#the-questions-and-the-route).

The aliases are the frontmatter `aliases` list of the page, and the terms
that `vocabulary.md` gives it. A line of
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
The topic page is never cut. The snapshot block comes before the journal
entries, so the bundle cuts a journal entry before it cuts a snapshot.

### Snapshots in the bundle

A job worker has no tools, so it cannot call a live system. A snapshot
(`memory.snapshots`, see [docs/memory.md](memory.md)) gives it the live state:
the bundle holds the few snapshot items that are close to the topic, with
the time of the fetch. A worker can then see that a page or a journal entry
is out of date.

Which snapshots a job sees:

- a snapshot with `scope: shared`;
- a snapshot of one person only when its entry sets `jobs: true`. The
  default is false, because a bundle feeds wiki pages that every reader of
  the wiki can open.

How the bundle selects the items:

1. The chunks of the topic page are the query. The bundle embeds them once,
   for the journal match and for the snapshots.
2. An item is a candidate when it has a similarity of
   `jobs.bundle.snapshot_sim` (0.55) or more to a chunk of the topic page.
   The items are short, so the floor is lower than `jobs.bundle.sim`.
3. A candidate comes in only when it names the topic, by the title and alias
   rule of the journal match, or when its similarity is
   `jobs.bundle.snapshot_strong_sim` (0.75) or more. A short item of live
   state is close to many pages by meaning alone, so the floor by itself
   lets in items about other things.
4. The closest item goes first. The block holds
   `jobs.bundle.snapshot_items` (12) items at most, and it stops at the
   first item that does not fit in `jobs.bundle.snapshot_chars` (3 000)
   characters. The bundle never holds a whole snapshot or every snapshot.
5. A snapshot with no good fetch is left out.

The block has one part for each snapshot, at the path `snapshots/<name>`.
The first line gives the server, the tool, and the local time of the latest
good fetch. Then each item is one line, with its line breaks folded, at most
300 characters:

```markdown
## snapshots/open-tasks

Live snapshot of tasks list_open_tasks as of 2026-03-14 11:00 EDT.

- title: Change the filter; due: 2026-03-20
- title: Check the backup; due: 2026-03-16
```

The workers read a `snapshots/` part as what a live system held at its time.
When it disagrees with a page or an older journal entry, the snapshot is the
current state, and the page is out of date. A snapshot line is not the words
of a person, so it never shows that a person cares about something.

With no embedding model, the bundle has no snapshot block, and the report
says so. `Bundle.snapshots` records each part, its items, and the similarity
of each item. The report of each job says `Snapshots: <name> (<n> items)`.

The digest job never reads a snapshot.

## The `bundle` job

The `bundle` job builds the memory bundle of one topic and reports it. It
makes no model call and writes no page. Use it to see what a later job will
read about a topic.

Arguments: `{"topic": "<path>"}`, the topic page relative to the wiki root. A
path that is not a topic page gets 400.

The report lists the paths in the bundle, the size of the text, the aliases,
and whether the bundle was cut. Under "Journal entries" it gives, for each
journal entry, how the entry got in: `title`, `alias`, or `vector <sim>`.
Under "Snapshots" it gives the snapshot parts and each item with its
similarity. The report page holds the path list, not the bundle text. The `report.data` field of the row holds the same values as JSON.

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
scripts/jobs.sh run curator --dry-run --arg paths=houseplants/,garden/ --arg max_worker_calls=20
```

Start with a dry run, and read its report with `scripts/jobs.sh runs <id>`.

The arguments are optional:

| Argument | Value | Effect |
|---|---|---|
| `apply` | actions, as a list or with commas: `move,merge,refresh` | It replaces `jobs.curator.apply` for the run. |
| `max_worker_calls` | a whole number, 0 or more | It replaces `jobs.curator.max_worker_calls` for the run. `0` makes a run that only scans. |
| `paths` | wiki path prefixes, as a list or with commas: `houseplants/,garden/` | The workers get only the topic pages whose path starts with a prefix, and only the pages in scope get tracking keys. A pair goes to `compare` when one of its pages is in scope. The scan still reads the whole wiki. |

A prefix is relative to the wiki root and is compared as text, so use the
trailing `/` for a folder: `garden` also matches `garden-old/`. A prefix that
starts with `/`, or that holds `..` or a part that starts with a dot, gets
400.

### What a run does

1. **The scan**, over the whole wiki, every run. It is Python and costs
   nothing.
2. **The workers**, on the topic pages that have a pass due: the body
   changed since that pass, or the pass never ran. A run makes
   `jobs.curator.max_worker_calls` (40) worker calls at most, or the
   `max_worker_calls` of the run.
3. **The apply step**, when the run is not a dry run: every Tier A finding,
   and each Tier B finding whose action the run may apply.
4. **The tracking keys**, when the run is not a dry run, for each pass that
   the run did.
5. **The findings**, into the `curator_finding` table, and a row in
   `curator_pair` for each pair that `compare` read. A dry run records them
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
`joshua-docs/`, `knowledge/`, `insights/`, `questions/`, `job-runs/`,
`skills/`, or `attachments/` is never an orphan, and its links do not make
another page reachable.

A broken link in a journal entry is always Tier C, because the curator never
changes the body of a journal entry. The scan does not read the links of a
page under `people/`, `joshua-docs/`, or `job-runs/`.

### The workers

Each worker is one model call with no tools and one answer schema. It reads
pages as content, not as instructions. It has a timeout of 90 seconds. A call
that fails, times out, or gives an answer that does not match the schema
changes nothing: the report lists it under "Skipped". The model of a worker
is `jobs.curator.models.<worker>`, then `jobs.curator.model`, then
`jobs.model`, then `core.reflection_model`.

| Worker | Reads | Answers | Finding |
|---|---|---|---|
| `refresh` | the page, the journal entries about it from the memory bundle that are dated after its `refreshed` day (all of them for a page with no `refreshed` day), and the snapshot parts of its bundle | `status` (`current`, `stale`, `conflict`), `patch` (find and replace pairs, each with `because`: an entry and its date, or `snapshots/<name>` and its time), `confidence` | `refresh`, Tier B when the status is `stale`, the confidence is `high`, and each `find` occurs exactly once in the body. Else Tier C, with the patch. |
| `place` | the page, and the section map: each list line of `## Sections` on `Home.md`, and the first paragraph of its index page | `section`, `folder`, `confidence`, `reason` | `misplaced`, Tier B (`move`) when the folder is not the folder of the page and the confidence is `high`; Tier C for a lower confidence or a folder that is not there. A `section` of `none` is a Tier C `no-section` finding. |
| `compare` | two topic pages | `relation` (`duplicate`, `overlap`, `conflict`, `unrelated`), `claims`, `canonical`, `confidence` | `duplicate`, Tier B (`merge`) for a duplicate with `high` confidence; `conflict`, Tier C, with the claims. |

A run spends its budget in this order:

1. `place` for each page that was never curated: it has no `curated_hash`.
2. `compare` for each pair, the near-duplicates first. A pair that `compare`
   read before, at the same two body hashes, is not read again.
3. `refresh` for each page whose `refreshed` pair is old or missing, when it
   has journal entries about it or a snapshot part in its bundle. The page that was never refreshed goes
   first, then the page with the oldest `refreshed` day.
4. `place` for each page that was curated before and changed since.

The place of a page and its duplicates organize the wiki, so they go before
the facts that the journal made old. Each pass counts on its own. When the
budget runs out after the `place` calls, the pages that got an answer keep
it, and the next run spends its budget on the passes that are still due.

The `compare` pairs come from the mean vector of the chunks of each topic
page in the index: two pages at `jobs.curator.duplicate_sim` (0.92) or more
first, then two pages of one section at `jobs.curator.overlap_sim` (0.70) or
more. A pair is compared only when one of its two pages has a pass due. With
no `## Sections` list on `Home.md`, no page goes to `place`.

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
names its action and the worker that raised it was not due to read its pages
again. So a dry run, and then `run --arg apply=move`, applies the move that
the dry run found.

A move or a merge does not change a link in a journal entry. The next scan
reports a link that it broke there, as Tier C.

### The tracking keys

The curator records each pass on a page with a pair of frontmatter keys:

```yaml
curated: 2026-10-19            # the day of the run that placed the page, in timezone
curated_hash: 7f3a9c1e04b2     # the body hash at that pass
refreshed: 2026-10-19          # the day of the run that refreshed the page
refreshed_hash: 7f3a9c1e04b2   # the body hash at that pass
```

The body hash is the first 12 hex characters of the SHA-256 of the text after
the frontmatter, with the trailing spaces and line ends removed. A change to
the frontmatter alone does not change it.

- The `curated` pair goes on a page when `place` answered for it. A page that
  is not a topic page (`Home.md`, an index page, a skill, a journal entry)
  needs no `place`, so the scan gives it the `curated` pair.
- The `refreshed` pair goes on a topic page when `refresh` answered for it,
  or when the page has no journal entry to read after its `refreshed` day.

The skip rule: a page whose body hash equals `curated_hash` does not go to
`place`, and a page whose body hash equals `refreshed_hash` does not go to
`refresh`. The scan still covers every page on every run. A page that a person
or Joshua edits has a new hash, so both pairs are old, and the next run does
both passes again. A pass that the budget or a failed call left out keeps its
pair old, and a later run does that pass only.

`compare` reads two pages, so its record is not on a page. After `compare`
answers for a pair, the run writes a row in `curator_pair`:

| Column | What it holds |
|---|---|
| `fingerprint` | the SHA-256 of the two sorted paths of the pair |
| `hash_a`, `hash_b` | the body hash of each page, in the sorted order of the paths |
| `checked_at` | the time of the run |

A run does not send the pair to `compare` again while both hashes are the
same. The hashes are the ones after the apply step, so a change that the
curator applied does not send the pair again.

The pairs go on each page for the passes that the run did, and again on each
page that the run changed when that page had current pairs before. So a
change the curator applied does not make the page go to a worker again. The
keys never go on a page under `people/`, `joshua-docs/`, or `job-runs/`, on a
dot path, or
on a journal entry that a job run wrote (one with `run_id` in its
frontmatter). The curator scans such an entry for links, but no worker reads
it. A page whose frontmatter is not a YAML mapping gets no keys, and the
report says so.

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
pages changed, or when the worker that raised it read its page again and did
not raise it.
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
report page and answer in chat: "apply finding 12", or "dismiss finding 14,
the two pages are about different roses".

### The report

The report page of a curator run has a summary line with the counts, and
these sections:

- **Applied (Tier A)** and **Applied (Tier B)**: each change, with its finding
  id, kind, paths, and patch.
- **Proposed (Tier B)** and **Proposed (Tier C)**: each open finding, with its
  id, kind, paths, and its patch or its claims.
- **Skipped**: the worker calls that the budget did not allow, the calls that
  gave no answer or an answer that was refused, and the findings that were not
  applied because they are dismissed. The calls that gave no usable answer
  are in one line for each worker, with a count, and the pages or pairs are
  in a list under it.
- **Snapshots**: for each page whose `refresh` read a snapshot part, the line
  `Snapshots: <name> (<n> items)`. The section is not there when no page did.
- **Reviewed**: the number of pages that got the `curated` pair, and the
  number of pages that got the `refreshed` pair.

A dry run has "To apply (Tier A)" in place of the two "Applied" sections. In
the report, each text from a page or a worker is in a code span, so the page
holds no link of its own. A find and replace pair of a `refresh` patch is in a
fenced `text` block, with a `find:` line, a `replace:` line, and a `because:`
line, byte for byte as the patch holds it. The fence is longer than each run
of backticks in the pair. The scan reads no link in a fenced block. `report.data` holds the counts and the finding ids,
for a program.

Every page that a run changed is in the commit of the run. A file that moved
to `wiki/.trash/` leaves the repository, because the repository ignores the
trash.

### What the curator never touches

- A page under `people/`, `joshua-docs/`, or `job-runs/`.
- A dot path, except the destination of a file that it moves to
  `wiki/.trash/<day>/`.
- The body of a journal entry. It adds only `date` and the tracking keys to
  the frontmatter of an entry.
- The frontmatter of a skill, other than the tracking keys.
- Anything outside `wiki/`.
- Anything at all in a dry run, other than the rows of `curator_finding`.

## The researcher

The researcher keeps `wiki/knowledge/`: facts about one topic at a time, each
looked up on the web, challenged, and cited. It works on one topic in each
run. It asks each question in two parts: a general question, which goes to
the web, and our local context, which stays inside the instance. Before a
lookup, it looks for the answer in the knowledge that it already holds. It
also keeps `wiki/questions/`: the questions that only a person
can answer.

A job runs at night, when nobody is in a conversation. So Joshua never sends
a question to a person. The researcher keeps each question for a person as a
page, and a person answers it later, in chat. See
[Questions for a person](#questions-for-a-person).

Set `jobs.research.enabled: true`, with `jobs.enabled: true`, to add the job
and the `research` chat tool. A `run` also needs `internet.enabled: true`.
[docs/config.md](config.md#jobs) has the keys.

```
scripts/jobs.sh run research --arg mode=plan
scripts/jobs.sh run research --arg mode=plan --arg topic=houseplants/snake-plant.md
scripts/jobs.sh run research --dry-run --arg mode=run --arg topic=houseplants/snake-plant.md
scripts/jobs.sh run research --arg mode=run --arg topic=houseplants/snake-plant.md \
    --arg questions=winter-watering,repotting
scripts/jobs.sh run research --arg mode=run --arg max_questions=1
```

Start with a `plan`, then a dry `run` on one question, and read each report
with `scripts/jobs.sh runs <id>`.

### The arguments

| Argument | What it does |
|---|---|
| `mode` | Required. `plan` or `run`. |
| `topic` | A topic page, relative to the wiki root. When it is not there, the run selects a topic. A path that is not a topic page gets 400. |
| `questions` | Slugs from the dossier, with commas between them. Only with `mode=run` and a `topic`. A slug that is not in the dossier gets 400. |
| `max_questions` | A lower limit than `jobs.research.questions_per_run`, for this run. |
| `then_insights` | `true` or `false`. Only with `mode=run`. It replaces `jobs.insights.after_research` for this run. See [the research hook](#the-research-hook). |

### The modes

Each mode first selects the topic, builds its memory bundle, and loads its
dossier, or starts a new one. When the dossier has fewer open questions than
`questions_per_run`, and the run names no `questions`, the `raise` worker
proposes new questions, and the gate decides which questions the dossier
takes.

- **`plan`** stops there. It makes no lookup and writes no knowledge page.
  When it is not a dry run, it writes the dossier, `knowledge/README.md`, and
  each new question for a person with `questions/README.md`, so that a later
  run can name the new slugs in `questions`. Its report is described in
  [The report](#the-report).
- **`run`** then researches the questions, up to the limit: the ones that
  `questions` names, or else the questions in the order that
  [The kept lookups](#the-kept-lookups) gives. It
  writes a page for each published answer, and the new state of the dossier
  and of `knowledge/README.md`. A dry run does all the worker calls and writes
  nothing. Its report holds the full text of each page that it would write.

### The topic

A topic is one leaf page of the wiki (see
[Topics and the memory bundle](#topics-and-the-memory-bundle)). The rules
below select one topic. The first rule that gives a topic is the one that
applies:

1. **`person`**: a topic whose dossier has an open question that a person
   asked.
2. **`mentioned`**: of the topics that the messages of people of the last
   `jobs.bundle.recent_days` days name, the one with the lowest coverage. A
   topic with no dossier has a coverage of 0. A message names a topic when it
   holds a name of the topic, the same text rule as the bundle. When two
   topics have the same coverage, the topic with more messages is first.
3. **`coverage`**: of all the topics, the one with the lowest coverage below
   `jobs.research.coverage_target` (0.6).
4. **`refresh`**: the dossier with the oldest `refresh_after` that is before
   the day of the run.

Only what people say counts for the `mentioned` rule. A message is a turn
that a person of the roster sent, a member or a guest. A reply of Joshua, a
scheduled task, an event, and the journal do not count, because the words of
Joshua do not show what a person cares about.

The name of a person is never a name of a topic, so a message that only names
a person counts for no topic. The run logs the number of messages and topics,
and never the text.

A **person section** is a wiki section, the first part of the path, that
`jobs.research.person_sections` names. The default list is `coach` and
`people`. The pages of a person section are about people. Each rule takes a
topic in a person section only when no topic outside one qualifies under the
same rule. The reason of the selection then says so, and the `Topic` section
of the report has a line `Person section: <section>`.

Each tie that is left goes to the **seeded pick**: a shuffle of the sorted
topic paths with the seed `<day of the run>:<sha256 of the sorted paths>`.
The same wiki on the same day gives the same topic. On a different day, a
different topic can win a tie. The report names the rule, the reason, and the
runner-up: the second topic of that rule, or else the first topic of a later
rule.

### The dossier

Each topic that the researcher works on has one dossier, at the path of the
topic page under `wiki/knowledge/`:

```
wiki/knowledge/README.md                                   the table of every dossier
wiki/knowledge/houseplants/snake-plant.md                  the dossier
wiki/knowledge/houseplants/snake-plant/winter-watering.md  one question, one page
```

The frontmatter of the dossier is the state of the topic:

```yaml
topic: houseplants/snake-plant.md
title: Snake plant
researched_at: 2026-10-19        # the last run that researched this topic
refresh_after: 2027-04-17        # researched_at + jobs.research.refresh_days
runs: 2
coverage: 0.4                    # answered / all questions; 0.0 with no questions
questions:
  - slug: winter-watering
    question: How often should a snake plant in a gritty mix be watered in winter?
    local_context: Ours is in the hall in a gritty soil mix, watered to runoff, at 65 to 68 F.
    subject: snake plant         # the generic name that the general question holds
    shared: false                # true when a page of another topic answers it
    status: answered             # open, answered, held, or dropped
    page: houseplants/snake-plant/winter-watering.md
    grounded_in: ["gritty soil mix", "water to runoff"]
    asked_by: research           # research, or the id of a person
    holds: 0
    hold_reason: ""
    raised_at: 2026-10-19
  - slug: winter-light
    question: How much light does the snake plant need in a hall in winter?
    status: held
    holds: 1
    hold_reason: no answer from the editor
    draft:                       # the answer of the researcher; see The kept lookups
      answer: Bright, indirect light ...
      sources: [{url: "https://extension.university.example/a", title: ""}]
      confidence: medium
      disagreements: ""
    challenge: {answer: ..., sources: [...], confidence: high, disagreements: ""}
    looked_up_at: 2026-10-19
person_open: 1                   # the open questions of the topic in wiki/questions/
person_answered: 0               # the answered questions of the topic in wiki/questions/
source: research
index: false                     # the memory index leaves the dossier out
```

`question` is the general question, the text that went to the web.
`local_context` holds our specifics. A dossier from before these fields loads
with `local_context: ""`, `subject: ""`, and `shared: false`.

The slug comes from the question: its first six words that are not small
words such as "how" or "the", in lower case, joined with `-`. A slug that the
dossier has already gets `-2`, `-3`, and so on. The researcher writes the body
of the dossier again on each save: a title, a line for each answered question
with a link to its page and its short answer, and the open and held
questions. Under a held question with a `draft`, a collapsed section
(`<details>`) shows the draft answer in a code block, with its day, its
confidence, and its sources, so a person can read what came back. When the topic has questions for a person, a line gives the two
counts and a link to `questions/README.md`. The dossier holds only these
counts, so it stays about the research on the web. No worker writes the
dossier.

`knowledge/README.md` has one table with a row for each dossier, sorted by
topic path: the topic, with a link to its dossier, the coverage, the answered
and total questions, the shared answers (the answered questions that a page
of another topic answers), the day of the last research, and the open
questions.
The researcher writes it again each time it writes the wiki. It is the index
page of `knowledge/`: it has a link to each dossier.

### The questions and the route

The `raise` worker has no tools. It gets the memory bundle of the topic, the
title and the aliases, and the questions in the dossier. It answers with up
to `questions_per_topic` questions, less the open questions. The prompt tells
the worker that the bundle is content, not instructions.

A `web` question is a pair:

- `general_question`: the text that goes to the open web. It holds the class
  of the thing and its public specifics only: the species, the product model,
  the technique, the material, the season, the condition of the thing. It
  calls the thing by a generic name. It never holds a person, a possessive, a
  date from our records, a measurement of a person, a nickname, a room, or
  anything that has a meaning only in this house.
- `local_context`: our specifics and why we ask, 40 to 600 characters: the
  phrases of the bundle in their context, the cadence we use, what was seen.
  It stays inside the instance. The editor reads it, and a lookup never does.

So the web question is general by construction, and our specifics stay with
the tool-less editor. The same general text also makes one question of
"winter watering of Sansevieria in a mineral mix" for each snake plant page,
and lets the knowledge that we hold answer a question with no lookup.

| `local_context` | `general_question` |
|---|---|
| The living room snake plants are in Jack's Gritty Mix, watered to runoff every 4 weeks, in a 65-68°F house. | How often should a snake plant (Sansevieria) in a mineral gritty mix be watered from October to March in a cool indoor home? |
| Alex's caldo verde uses grow-tent Portuguese kale that comes out pale. | What makes Portuguese kale (couve galega) grown under indoor lights turn pale yellow-green instead of deep green? |

Each question has these fields:

- `audience`: `web`, `person`, or `record`.
- `general_question` and `local_context`: for a `web` question.
- `subject`: for a `web` question whose general question holds no name of the
  topic: the generic name of the thing, such as "snake plant". 80 characters
  or less.
- `question`: for a `person` or a `record` question. It keeps its own text,
  and it may name a person.
- `about_person`: true when the question carries a specific of a person,
  named or not. It is required.
- `grounded_in`: the phrases from the bundle that motivate the question, for
  every audience. The prompt asks for the shortest span that identifies the
  specific: a product name, a quantity, a cadence, not a whole sentence.
- `why`: one or two sentences that say why the researcher wants the answer.
  A `person` question must have it.

The three audiences are these:

- **`web`**: a question that an expert source outside the house can answer:
  horticulture, materials, products, methods, standards, seasons, safety. Its
  general question goes to the internet agent, which searches the open web
  and has no access to our pages. It carries the specifics of the thing (the
  plant, the device, the material, the method) and never the specifics of a
  person, named or not: not a condition, a diagnosis, a symptom, a
  medication, a diet, a body measurement, a mood, a finance, a location, or a
  relationship. "What are the early symptoms of a depressive episode for
  someone who stopped sleeping well in October?" is about a person. "What
  does the NHS say about sleep hygiene for adults?" is about a thing.
- **`person`**: a question that only somebody here can answer: what was done,
  used, intended, or preferred, or a record that is missing. "What product and
  dose went on the snake plants at the October 4 feed?" is one.
- **`record`**: a question that our own pages and journal already answer.

#### The gate on a `web` question

The gate is Python. It checks a `web` question by these rules, in this order,
and rejects it with a reason at the first rule that it fails:

1. The general question gets the generic name of the topic in place of the
   possessive title, in any case: for "Alex's Caldo Verde", "How should
   lacinato kale be blanched for a soup like Alex's Caldo Verde?" becomes
   "... for a soup like Caldo Verde?". The dossier and the pages keep the new
   text, and the Questions section of the report marks the question
   `(generic name)`.
2. The general question has 20 to 300 characters and ends with `?`.
3. It holds a name of the topic (see
   [Topics and the memory bundle](#topics-and-the-memory-bundle)), or the
   `subject` that the worker gave. The question holds the subject when it
   holds the subject as a phrase, or when it holds at least 2 of the content
   words of the subject and at least 60 percent of them. The content words
   are the words of the subject in lower case, split at each hyphen, with no
   small word such as "a", "the", "of", "for", "in", or "inside". A word
   matches when a word of the question starts with its stem: the word with no
   `ing`, `ed`, `es`, or `s` at its end, when 3 letters or more stay. So
   "Buildah image build inside a container" matches "building container
   images inside a container". A `subject` that the question does not hold is
   a rejection, and the reason names the missing words. With no `subject`,
   the dossier keeps the name of the topic that the question holds as its
   `subject`.
4. `local_context` has 40 to 600 characters.
5. `grounded_in` has two phrases or more. Each phrase has 3 to 160 characters
   and occurs word for word in the bundle text, a `snapshots/<name>` part
   too. Case and line breaks do not count. There is no stemming and no fuzzy
   match.
6. The floor rules below. A general question that fails one is rejected, and
   the reason starts with the name of the rule. It does not go to a person,
   because the worker was told not to write such a question, and a question
   for a person has its own text.
7. It is not a duplicate. See [Duplicates and shared answers](#duplicates-and-shared-answers).

| Floor rule | Name |
|---|---|
| The question names a person on the roster: a display name or an id from `people`, as whole words, in any case. | `names a person` |
| The question holds a date in ISO form (`2026-10-04`) or in month and day form (`October 4`, `Oct 4`). | `names a record date` |
| The question starts with "what did", "why did", "when did", "will", "should we", "did we", or "do we". | `asks what we did or will do` |
| The question holds "we", "our", or "us" as a word. "US" in capitals is a country and does not count. | `asks about us` |

A `web` question that passes goes to the dossier as an `open` question, with
two exceptions, which send it to `person` with the general question as its
text:

| Rule | `route_reason` |
|---|---|
| The topic is in a person section. | `person section` |
| The worker tags the question `about_person: true`. | `about a person` |

The worker judges which question is about a person, and Python enforces the
floor: when the worker tags a question `false` but the general question names
a person, the gate rejects it. A question for a person that one of these rules
sends there needs no `why`: its page gets a fixed sentence. The prompt also
tells the worker not to raise a question about a person that no person here
can answer.

#### The gate on a `person` or a `record` question

A `person` or a `record` question keeps its own `question` text, and the pair
rules do not apply to it. It must have the length of rule 2, pass rule 5, and
not be a duplicate. It need not name the topic, because its page stores the
topic and the context. A `person` question from the worker has a
`why`. When the question fails a floor rule, it goes to `person`, and its
`route_reason` is the name of the rule. A question of a person section that is
not `record` goes to `person` (`person section`), and so does a question with
`about_person: true` (`about a person`). A `person` question that no rule
sends there has the `route_reason` `the raise worker asks a person`. Then:

- **`record`**: the gate searches the index with the question, over the kinds
  `wiki`, `journal`, and `knowledge`, and over the items of the snapshots that
  a job sees (see [Snapshots in the bundle](#snapshots-in-the-bundle)). A hit
  counts when it is a topic page, a journal entry that no job wrote, a
  knowledge page with a `question`, or a snapshot item with a good fetch.
  When the best hit is at `jobs.research.record_sim` (0.80) or more, the
  report closes the question with `answered_by: <path>`, or
  `answered_by: snapshots/<name>` for a snapshot item, and the run writes
  nothing for it. A question that a snapshot answers never goes to
  `wiki/questions/`. Below that, nothing holds the answer, so the question goes to
  `person` with the `route_reason` `no record found`. The record search stays
  inside the instance, so a `record` question of a person section, or one
  about a person, still goes to it.
- **`person`**: the question becomes a page under `wiki/questions/`. It never
  goes to a lookup and never goes into the dossier.

On a topic in a person section, a `run` researches only the questions of the
dossier that a person asked, and the report has a note for each other
question.

#### Duplicates and shared answers

The duplicate check uses the general question only. A question is a
duplicate when its text is the text of a question of the dossier, or its
vector is at `jobs.research.dup_sim` (0.85) or more to a question of the
dossier or to a chunk of `kind: knowledge` in the index, of any topic. The
report gives `answered_by: <path>` for a question that a knowledge page of
the same topic answers.

A match on a published knowledge page of **another** topic is not a
rejection. The question goes into the dossier as `answered`, with
`page: <that page>` and `shared: true`, and no run researches it. The
coverage counts it, the dossier body marks it `(shared with another topic)`,
and the `Shared` column of `knowledge/README.md` counts it.

So "How often should I water a snake plant?" fails, because no phrase of it
comes from our pages. "What dose was used on 2026-10-04?" fails as a `web`
question, because it holds a date of our records, and goes to a person as a
`person` question. "How often should a snake plant in a gritty soil mix be
watered in winter?" goes to the web when "gritty soil mix" and "water to
runoff" are in the topic page, and its local context says that ours is in the
hall at 65 to 68 F.

### Questions for a person

A question for a person is one page at
`wiki/questions/<section>/<slug>.md`. The section is the first folder of the
topic path, or `general` for a page at the wiki root. The slug comes from the
question as the slug of a dossier question does, and is unique in its
section.

```yaml
question: What product and dose went on the snake plants at the October 4 feed?
topic: houseplants/snake-plants-living-room-window.md
section: houseplants
why: The researcher wants to know whether an early-October feed builds up salt in a gritty mix.
for: everyone                  # everyone, or the id of the person who would know
context: [houseplants/snake-plants-living-room-window.md, journal/2026/10/04/snake-plants-fed.md]
grounded_in: ["fed at the October watering", "Liquid rather than granular"]
route_reason: names a record date
status: open                   # open, answered, or dismissed
asked_by: research
raised_at: 2026-10-19
answered_at: null
answered_by: null
answer: null
source: research
run_id: 6
```

`context` lists the bundle pages that the `grounded_in` phrases come from, or
the topic page. `for` is the id of a person only when the sentences of the
`grounded_in` phrases name that one person, by a display name or an id from
`people`. Else it is `everyone`. When the worker gave no `why`, the
researcher writes a fixed sentence. The body has these headings, in this
order: the question as the title, **Why Joshua wants to know**, **Where it
came from** (a link to each `context` page), and **Answer**, which is empty
until a person answers.

`questions/README.md` is the list of every question. The researcher and the
`questions` chat tool write it again on each change. Its first paragraph says
that these are questions that the researcher could not answer from the web or
from our records, that Joshua does not send them, and that a person answers
them in chat. A table of the open questions follows, the oldest first, with
the question, the topic, `for`, and the day it was raised. A second table
holds the answered and the dismissed questions, the newest first.

A run does not write the same question two times. When the text of a new
`person` question, or its vector at `jobs.research.dup_sim` or more, matches
an open or answered question page of the same topic, the run writes no page,
and the report says `already asked`. When it matches a dismissed question,
the report says `dismissed before`.

The dossier of the topic holds only the counts, `person_open` and
`person_answered`, and a line that links to `questions/README.md`.

A person answers a question in chat with the `questions` chat tool, which
the chat tool section below describes. The answer goes onto the page, and the
memory bundle of the topic holds each answered question from then on, as the
question and the answer only. So the next `raise` call and the editor read
what the person said. An open or a dismissed question is not in the bundle.
A dry run writes no question page.

### The crew

For each question, the crew first looks at the knowledge we hold (see
[Knowledge first](#knowledge-first)). Then three workers run in this order:

1. The **researcher** is the internet agent. It gets the general question, or
   the narrowed question of a `partial` verdict, and a brief
   that asks for the primary source, two independent sources or more, the
   numbers, and the disagreements. The brief tells it to read at most four
   pages, and to stop when it has a primary source and one independent second
   source. It gets nothing from the wiki and never the local context.
2. The **challenger** is the internet agent again. It gets the same question and
   the draft answer, and it looks for the strongest evidence that the draft
   is wrong, old, or not complete. It says so when it finds nothing. The
   brief tells it to read at most three pages.
3. The **editor** has no tools and a timeout of
   `jobs.research.editor_timeout_seconds` (240) seconds. It gets the general
   question, the local context, each `grounded_in` phrase with its sentence
   from the bundle, the draft and the challenge with their sources, the short
   answers of the pages that a `partial` verdict cites, and the trust rubric
   with the two domain lists. It writes the page: the answer, a short answer of 160
   characters or less, where our situation is different, the evidence, the
   disagreements, each source with its trust (`primary`, `secondary`, or
   `weak`), the confidence, and the verdict: `publish` or `hold`.

The editor answers in one structured call of up to 6 turns. The CLI checks
the answer against a flat schema, and when the answer does not match, the
model tries again in the next turn. The log line
`structured answer refused by the validator` gives the start of the message
of the validator for each refused try. The timeout includes each try.

The schema puts the short required fields first, in this order: `verdict`,
`confidence`, `answer_short`, `sources`, and `answer`. The fields that are
not required come after them: `local_notes`, `evidence`, `disagreements`, and
`reason`. So an answer that is cut off still has the fields that a page
needs. The prompt tells the editor to give the fields in this order, and to
keep the whole answer under 2500 characters.

Each field has a limit:

| Field | Limit |
|---|---|
| `answer_short` | 160 characters |
| `answer` | 2500 characters |
| `local_notes` | 800 characters |
| `evidence` | 8 items, each 300 characters |
| `disagreements` | 800 characters |
| `reason` | 300 characters |
| `sources` | 8 items |
| `title` of a source | 200 characters |
| `url` of a source | 500 characters |

The schema gives each limit as a `description`, not as `maxLength` or
`maxItems`, because the validator refuses a value over a hard limit, and each
refused try costs a turn. Python then cuts a longer value to its limit: a
string ends with `…`, and a list keeps its first items. A long field never
causes the editor to lose the answer.

When a lookup gives an answer with no source, but its fetches read pages, the
pages that the fetch gate let through become its sources, with no title, up
to 12. The log line `sources backfilled from fetched pages` gives the count.
With `internet.fetch.enabled` off, there is no fetch, and the list stays
empty.

The two lookups have their own limits, because the brief needs more searches
and fetches than a chat question. Each lookup may take
`jobs.research.lookup_timeout_seconds` (300) seconds and
`jobs.research.lookup_max_turns` (14) turns, in place of
`internet.timeout_seconds` and `internet.max_turns`. The `internet` chat tool
keeps the limits of the `internet` section.

Before a lookup, the general question must have 20 to 300 characters, end
with `?`, and pass the floor rules. A question that fails, such as a question
that a person queued with a name in it, is held with the reason
`not sent: <why>`, and no worker runs for it. The Researched section of the
report marks it `not sent`, and the report data lists it in `not_sent`.

### Knowledge first

Before the lookups of a question, the crew searches the `kind: knowledge`
chunks of the index with the vector of the general question. It keeps the
pages at `jobs.research.apply_sim` (0.70) or more that have
`status: published`, of any topic, the closest first, at most
`jobs.research.apply_pages` (5). With no such page, the full crew runs.

With one page or more, the **apply** worker runs. It has no tools, the model
`jobs.research.models.apply` (see
[Which model runs which worker](#which-model-runs-which-worker)), and a
timeout of 120 seconds. It gets the general question,
the local context, and the whole text of each page. Its answer is cleaned of
leaked markup like the answer of the editor, and it gives a verdict:

- **`answered`**: the pages answer the whole question. The page is written
  from the answer of the apply worker, with no lookup. Its sources are the
  sources of the cited pages, each once, with their trust. Its confidence is
  the lowest confidence of the cited pages, and a `low` confidence is a hold.
  Its frontmatter has `applies`, and the body has a line `Applied from:` under
  **Sources**. The Researched section marks it `applied, no lookup`.
- **`partial`**: the pages answer a part of the question. The two lookups ask
  the `narrowed_question` of the worker, which asks only for the missing
  part, and the editor also gets the short answer of each cited page. The
  frontmatter has `applies`. The Researched section marks it `narrowed`. A
  narrowed question that is empty or fails the floor rules is not sent: the
  lookups ask the general question, and the Notes section says so.
- **`none`**: the full crew runs.

A cited path that is not one of the pages that the worker got, and so not a
published knowledge page, is dropped. An `answered` or `partial` verdict with
no cited page left, or no answer, counts as `none`. A held question with kept
lookups goes to the editor alone, and the crew does not search for it. The
apply call counts on the usage line of the question, as `apply`.

### The hold rules

Python checks the answer of the editor. Each of these is a hold, with a
reason:

- A source URL that neither lookup returned. The URL is removed, and the
  count below is done again without it.
- Fewer than two sources that are not `weak`, from two different registrable
  domains. The registrable domain is the host without `www.`, cut to its last
  two labels, or to its last three when the label before a two-letter country
  code is `co`, `ac`, `gov`, `edu`, `org`, or `com` (`example.co.uk`).
- `verdict: hold`.
- `confidence: low`.

A source on `distrusted_domains` is always `weak`. A source on
`trusted_domains` is never `weak`: a `weak` grade there becomes `secondary`.
A worker that gives no answer at any step is a hold with the reason
`no answer`, and the hold counts. A lookup that passed its timeout is a hold
with the reason `lookup timed out after 300 s (6 searches, 4 fetches)`, with
the counts of that lookup, so the report shows how far it got. For the
challenger, the reason starts with `the lookup of the challenger`. A held
question gets one more `holds` and the reason in `hold_reason`. At
`max_holds` (2) its status becomes `dropped`, and no run researches it again.

### The kept lookups

A question that is held or dropped after its two lookups ran keeps their
answers in the dossier: `draft` from the researcher, `challenge` from the
challenger, and `looked_up_at`, the day of the lookups. Each has its
`answer`, `sources`, `confidence`, and `disagreements`. When the researcher
gave no answer, the question keeps nothing.

The next run that takes a held question with a `draft` and a `challenge`
sends it to the editor alone, and does no lookup. When `looked_up_at` is
more than `jobs.research.lookup_max_age_days` (7) days old, the run does the
two lookups again and keeps the new answers. The Researched section of the
report marks such a question `editor only`, and each other question
`full crew`. The report data lists the editor-only questions in
`editor_only`. This question counts toward `questions_per_run` and
`max_questions` the same as a question with lookups. When the editor
publishes the question, the dossier removes the three fields, because the
page holds what it needs.

A run that names no `questions` takes the questions in this order, up to
the limit:

1. The held questions with a kept `draft` and `challenge` that are not more
   than `lookup_max_age_days` old. These need only the editor, which costs
   much less than two lookups. The oldest `looked_up_at` goes first.
2. The open questions that a person asked.
3. The other open questions, in dossier order.
4. The held questions with no kept lookups, or with lookups that are too
   old, in dossier order.

A run that names `questions` takes exactly those questions.

### The page

A published answer is one page at
`wiki/knowledge/<topic path without .md>/<slug>.md`:

```yaml
question: How often should a snake plant in a gritty mix be watered in winter?
answer_short: About every 4 weeks in winter, when the mix is dry.
topic: houseplants/snake-plant.md
local_context: Ours is in the hall in a gritty soil mix, watered to runoff, at 65 to 68 F.
context: [houseplants/snake-plant.md, journal/2026/10/15/snake-plant-leaf-flop.md]
grounded_in: ["gritty soil mix", "water to runoff"]
researched_at: 2026-10-19
refresh_after: 2027-04-17
confidence: high
status: published
sources:
  - url: https://extension.university.example/snake-plant
    title: Snake plant care
    trust: primary
    retrieved: 2026-10-19
applies: [knowledge/houseplants/zz-plant/winter-watering.md]  # only with knowledge we hold
source: research
run_id: 7
```

`context` lists the bundle pages that the `grounded_in` phrases come from, or
the topic page for a question with no phrases. The body has these headings,
in this order: the question as the title, **Answer**, **Where this differs
for us** (only when there is a local note, which the editor writes from the
local context), **Evidence**, **Disagreements**
(only when there are some), and **Sources** (the title, the URL, and the
trust of each source). A page with `applies` ends with the line
`Applied from:` and a link to each page that it uses. A source of such a page
keeps the `retrieved` day of the page it came from. A link in the text of a worker stays only when its URL
is a source of the page. The researcher removes each other link, picture, and
wikilink, and keeps its text. A page is 64 KB or less, and a longer page is a
hold.

### The report

The report of a research run is the report page of the run, and
`scripts/jobs.sh runs <id>` shows it. It has these sections, in this order:

| Section | What it holds |
|---|---|
| Topic | the topic, the rule that selected it and why, the runner-up, and for a topic in a person section, a line that says so |
| Bundle | each path of the bundle, its size, if it was cut, and the line `Snapshots: <name> (<n> items)` |
| Questions | each question of the dossier, with its slug and status, as `general: <the general question>; local: <the local context>`. A new question that got the generic name of the topic has `(generic name)` after its general question. A shared answer has `shared` in its status and the page that answers it |
| For a person | each new question page, with its id, `for`, `route_reason`, and `why`, and each question that was `already asked` or `dismissed before` |
| Answered by our records | each `record` question with its `answered_by` path, which can be `snapshots/<name>` |
| Rejected | each rejected question with the reason |
| Researched | `run` only: the result of each question that the run researched, marked `applied, no lookup`, `narrowed`, `full crew`, `editor only`, or `not sent` |
| Pages | a dry `run` only: the full text of each page it would write |
| Notes | what did not run, such as the vector check with no embedding model |
| Insights | the insight pass, when one ran |
| Usage | the calls and the tokens of each worker, and for each researched question a line `<slug>: apply A tokens, researcher N tokens, challenger M tokens, editor K tokens` |

The report data has `person`, the ids of the new question pages, and
`record`, the `answered_by` paths, beside `added` and `rejected`. `shared`
lists the new questions that a page of another topic answers, and `applied`,
`narrowed`, and `not_sent` list the researched questions of each kind.
`usage_by_question` holds
`{"<slug>": {"apply": a, "researcher": n, "challenger": m, "editor": k}}` for
each question that the run researched. A step that did not run has 0.

### The chat tool

The `research` tool server is in core. A member gets four tools:

| Tool | What it does |
|---|---|
| `plan_research(topic=None)` | starts a `plan` run that is not a dry run, and answers with the run id |
| `run_research(topic, questions=None, max_questions=None)` | starts a `run` run that is not a dry run, and answers with the run id |
| `queue_question(topic, question)` | adds an open question with `asked_by: <person id>` to the dossier, writes the dossier and `knowledge/README.md`, and commits the wiki with the message `research: question from <person id>`. The question must have 20 to 300 characters, end with `?`, and name the topic. The dossier keeps it as given, with `local_context: ""` and the generic name of the topic as its `subject`, and the crew gets it as given. The floor rules apply before a lookup: a question that fails one is held, not sent, and the reply says so. |
| `research_status()` | gives the summary of the last research run |

A guest, or a speaker that core does not know, gets a refusal from each tool,
and nothing changes. The server is there only when `jobs.enabled` and
`jobs.research.enabled` are both true. A question that a person queues makes
rule 1 select its topic in the next run that does not name a topic.

The agent prompt tells Joshua to search memory first for a factual question
about the world, to use and name a `knowledge/` page and its confidence when
one answers it, and else to say that the answer is from inference and offer
to queue the question.

The `questions` tool server is in core too, with the same two switches. A
member gets three tools. A tool names a question by `<section>/<slug>`:

| Tool | What it does |
|---|---|
| `list_questions(status="open", topic=None, for_person=None, limit=20)` | lists the questions, the oldest open question first, each with its id, the question, `why`, `for`, and the topic. `for_person` keeps the questions for that person and for `everyone`. `status` is `open`, `answered`, `dismissed`, or `all`. |
| `answer_question(slug, answer)` | writes the answer of a person onto an open question: `answer`, `answered_at`, `answered_by: <person id>`, `status: answered`, and the **Answer** section. It writes `questions/README.md` and the counts of the dossier again, and commits the wiki with the message `research: question <slug> answered by <person id>`, under the job lock. |
| `dismiss_question(slug, why)` | closes an open question that does not apply: `status: dismissed`, `answered_by`, and `answer: <why>`, with the same writes and the commit message `research: question <slug> dismissed by <person id>`. |

An answer is text from a person. The page shows it under **Answer** as
content. A link in it stays only when it points to a page of the wiki that
exists. The tool removes each other link and picture, and keeps its text. A
guest, or a speaker that core does not know, gets a refusal from each tool,
and nothing changes.

No tool and no prompt rule sends a question to anybody. The agent prompt says
that the questions are under `questions/`, that Joshua lists the open
questions with `list_questions` when a person asks if it needs anything from
them, and that it never raises one on its own.

### What the researcher never does

- It never writes outside `wiki/knowledge/` and `wiki/questions/`. Each path
  comes from `layout`, and a write outside these folders fails.
- It never sends a question to a person. It keeps the question as a page,
  and a person answers it in chat.
- It never gives a question for a person to a lookup.
- It never takes a person page as a topic. It never gives a lookup a
  question that the worker tags `about_person: true`, or a question of a
  topic in a person section that no person asked.
- It never gives the bundle, the local context, or any page of the wiki, to
  a lookup. The internet agent gets the general question (or the narrowed
  question) and the brief. The challenger also gets the draft answer.
- It never sends a question that fails a floor rule to the web.
- It never cites a URL that a lookup did not return.
- It writes nothing in a dry run.

## Insights

The insights job keeps `wiki/insights/`. An insight is a defect in our own
setup that we probably cannot see now. It is one of these kinds:

| Kind | What it is | Example |
|---|---|---|
| Contradiction | Two things that we do or wrote disagree, or a number or a method that we use disagrees with a knowledge page. | Our page sets a feed at half strength, and the knowledge page says quarter strength. |
| Unused part | We measure, collect, or built something that nothing uses. | A sensor value is calculated, and no rule reads it. |
| Silent failure | A thing that we set up or changed can stop, and nothing would tell us. A step for the alert that the wiki marks "to be added", "to come", or "pending" means that there is no alert today. | A freezer is on a circuit with no alarm, and the alarm is "to be added". |
| Wrong assumption | A rule or a formula of ours rests on an assumption that is not true. | A heating rule assumes that calm air means no loss of heat. |
| Stale state | A page says one thing, and a snapshot or a newer journal entry shows that the state changed. | A page lists a device that a snapshot no longer shows. |

These are not insights: a best practice that we do not follow when nothing
of ours is broken or at risk ("experts advise a fence", "repot every two
years"), a thing that we have not planned, the age of a thing that works,
and a fact that shows no defect of ours. The person knows these things, or
chose them. The one exception is a risk of harm or loss now that the person
cannot see. The test of the prompt is: the person who set this up would say
"I did not know that, and it matters". A seasonal or a dated insight ends
with its window, so its `valid_until` is the last day of the window.

Each insight has a **basis**:

- `knowledge`: a knowledge page of the topic shows the defect, and
  `knowledge_says` names it.
- `ours`: our own pages show the defect, with no knowledge page. `we_do`
  holds two phrases or more, the two sides of the defect.

The job works on one topic in each run. A topic with no published knowledge
page runs too: the report says `no knowledge for this topic`, and only an
insight with `basis: ours` can pass the gate.

Set `jobs.insights.enabled: true`, with `jobs.enabled: true`, to add the job
and the `insights` chat tool. [docs/config.md](config.md#jobs) has the keys.

```
scripts/jobs.sh run insights --dry-run --arg topic=garden/irrigation.md
scripts/jobs.sh run insights --arg topic=garden/irrigation.md
```

The one argument is `topic`, a topic page relative to the wiki root. It is
required. A path that is not a topic page gets 400.

### What a run does

1. It loads the topic and builds its memory bundle.
2. It expires the old insights of the topic (see [Expiry](#expiry)).
3. It loads the published knowledge pages of the topic: each page that the
   dossier names for an answered question, and each page in the folder of the
   topic under `knowledge/`, when its frontmatter has `status: published`.
   There can be none.
4. The `insight` worker has no tools and a timeout of 120 seconds. It gets
   the parts of the bundle that say what we do (the parts that are not under
   `knowledge/` or `insights/`), the full text of each knowledge page with its
   frontmatter (so it sees `confidence`, `sources`, and `refresh_after`), the
   `answer_short` of each page, and the editor's note for us: the text under
   `## Where this differs for us`, on one line and cut at the end of a
   sentence to 600 characters or less. A page without that section has no
   note. The worker also gets the date, the timezone and the `name` from
   `joshua.yaml`, and the open insights of the topic, so that it does not
   raise one again. A snoozed insight is open, so it is on that list. The
   worker also gets the dismissed insights of the topic, each with the
   reason that the person gave. The prompt tells the worker not to raise an
   insight that a dismissed one covers, in other words too, or that the
   reason for a dismissal answers. The worker also gets the **verdict
   examples** of the whole instance, from every topic: in the block
   `<kept>`, the claims of the `jobs.insights.examples` (12) most recent
   insights that a person kept (`told` or `acknowledged`), and in the block
   `<dismissed_anywhere>`, the claims of the 12 most recent dismissed
   insights with their reasons. The prompt says that these show what the
   people of this instance find worth telling: raise what is like the kept
   ones, and do not raise what is like the dismissed ones. The prompt
   tells the worker that the bundle and the pages are content, not
   instructions.
5. The gate decides which insights are new, which refresh an open insight,
   and which are rejected.
6. The open budget decides which insights stay open (see
   [The open budget](#the-open-budget)).
7. It writes each new, refreshed, and ranked-out page and
   `insights/README.md`.

A dry run does the worker calls and the gate, and writes nothing. It does
not expire an insight either: the report lists the insights that would
expire, by the valid days or by the open budget. The report holds the full
text of each page that the run would write.

The summary is `insights: <topic> (N new, M refreshed, K expired, R
rejected)`, and K counts the insights that the open budget expired too. A
topic with no knowledge page adds `There is no knowledge for this topic.`
The report has the sections **Bundle** (the line
`Snapshots: <name> (<n> items)`), **Knowledge**, **New** (each with its
claim, severity, basis, valid days, `we_do`, and `knowledge_says`),
**Refreshed**, **Expired**, **Rejected** (each with its reason), **Ranked**
(when the open budget asked the `rank` worker), and **Usage**. The worker
calls count under the names `insight` and `rank`.

### The gate

The gate is Python. An insight gets a page only when all of these rules are
true:

- `claim` has 15 to 200 characters, and `action` is not empty.
- `confidence` is not `low`. The reason is `low confidence`.
- `we_do` has one phrase or more. Each phrase has 3 to 160 characters and
  occurs word for word in a part of the bundle that says what we do: a part
  that is not under `knowledge/` or `insights/`. A `snapshots/<name>` part
  is one of them, so `we_do_paths` can name it. Case and line breaks do not
  count.
- Each path in `we_do_paths` is one of those parts, and holds at least one of
  the phrases. When `we_do_paths` is empty, it becomes the parts that hold the
  phrases.
- The basis rule. With `basis: knowledge`, the default for an item with no
  `basis`, `knowledge_says` has one path or more, each a published knowledge
  page of this topic. A topic with no such page rejects it with the reason
  `basis knowledge needs a published knowledge page, and the topic has
  none`. With `basis: ours`, `we_do` has two phrases or more, and
  `knowledge_says` can be empty. Each path that it names is still a
  published knowledge page of this topic. A path is relative to
  `wiki/knowledge/`, with or without `knowledge/` in front.
- `valid_from` and `valid_until` are ISO days. `valid_until` is after
  `valid_from`, and not before today. When `valid_from` is missing, it is
  today. When `valid_until` is missing, it is today plus
  `jobs.insights.default_valid_days` (60). `valid_until` is at most today
  plus `jobs.insights.max_valid_days` (45): a later day becomes that day.

A claim that has the same text as an open insight of the topic, or a vector
at `jobs.insights.dup_sim` (0.88) or more to the claim of one, is a
duplicate. A duplicate of an open insight refreshes it: `seen_at` becomes
today, and `we_do`, `we_do_paths`, `knowledge_says`, and the evidence come
from the new answer. `valid_until` becomes the later of its current day and
the new day, which the cap keeps at most today plus `max_valid_days`. So a
refresh never moves it to an earlier day. The status, `snoozed_until`, and `snoozes` do not
change, and the report lists it as refreshed. A duplicate of a `dismissed` insight is dropped with the reason
`dismissed before`.

A run writes at most `jobs.insights.max_per_run` (5) new insights: the
highest severity first (`urgent`, `act`, `note`), then the highest
confidence. The rest are rejected with a reason. An item of the worker answer
that does not match the schema is a rejection too, and it changes nothing.

### The open budget

A person can act on only a few insights at a time. `jobs.insights.max_open`
(5) is the most open insights of the whole instance: the insights with the
status `new`, `told`, or `acknowledged`, of every topic. A snoozed insight
counts. After the gate, when the open insights and the new ones of the run
are more than `max_open`, the `rank` worker orders them.

The `rank` worker has no tools, a timeout of 60 seconds, and the model of
the `insight` worker. It gets each item with its id, topic, claim, why it
matters, and status, the open insights first and the new ones after them,
and the verdict examples. It answers with the ids in order of importance to
these people. Then:

- The first `max_open` ids stay open.
- A new insight below the line is rejected with the reason
  `below the open budget`.
- An open insight below the line becomes `expired`, with `resolved_at`
  today and `resolved_by: rank`. A later run can raise it again.
- An id that the worker leaves out goes to the end, in the order that the
  worker got.
- When the worker gives no answer, each open insight stays, and each new
  insight over the budget is rejected with `rank worker gave no answer`.

The section **Ranked** of the report gives the order, and for each id,
whether it stays open, is rejected, or expires. A dry run asks the worker
and writes and expires nothing. `max_per_run` still applies first.

### The page

An insight is one page at `wiki/insights/<section>/<slug>.md`. The section is
the first folder of the topic path, or `general` for a page at the wiki root.
The slug comes from the claim, in the same way as a question slug: its first
six words that are not small words, joined with `-`, and `-2`, `-3`, and so
on when the section has the slug already. A tool names an insight by
`<section>/<slug>`.

```yaml
topic: garden/irrigation.md
section: garden
claim: The garden is still on the 20-minute summer program in late October.
status: new                    # new, told, acknowledged, dismissed, or expired
severity: act                  # note, act, or urgent
basis: knowledge               # knowledge, or ours; a page with no basis is knowledge
valid_from: 2026-10-01
valid_until: 2026-11-30
we_do: ["every zone on the 20-minute summer program"]
we_do_paths: [garden/irrigation.md, journal/2026/10/15/garden-weather-log.md]
knowledge_says: [garden/irrigation/autumn-watering.md]
confidence: high
found_at: 2026-10-19
seen_at: 2026-10-19            # the last run that raised it
source: insights
run_id: 9
```

`told_at`, `resolved_at`, `resolved_by`, `resolution_note`, and
`snoozed_until` are added when they get a value. `snoozes` counts the snoozes
of the insight, and starts at 0. The body has these headings, in this order: the claim as
the title, **Why it matters**, **What to do**, **What we do** (each `we_do`
phrase in quotes, with a link to its page), **What is known** (the
`answer_short` of each `knowledge_says` page, with a link, or a line that
says there is no knowledge page), and **Evidence**
(the reasoning of the worker). The job removes each link from the text of the
worker, and keeps its text. The one exception is a link in the evidence to a
path in `we_do_paths` or `knowledge_says`. A heading line from the worker
becomes plain text.

`insights/README.md` has one table with a row for each insight: the status,
the severity, the topic with a link, the claim with a link, the valid-until
day, and the day it was found. The status cell of a snoozed insight also
says `snoozed until <day>`. The open insights (`new`, `told`, and
`acknowledged`) come first, then the rest, and the newest come first in each
group. The job and the chat tool write it again on each change.

### Expiry

At the start of each run, and in the insight pass of a research run, each
open insight of the topic whose `valid_until` is before today becomes
`expired`, with `resolved_at` today and `resolved_by: expiry`. The open
budget can also expire an insight, with `resolved_by: rank` (see
[The open budget](#the-open-budget)). Then
`insights/README.md` is written again. This is Python, with no worker call.
A dry run does not expire an insight.

### Status, snooze, and due

The status of an insight is one of these:

| Status | Meaning |
|---|---|
| `new` | The job raised it. |
| `told` | Joshua told a person about it. |
| `acknowledged` | A person knows about it. |
| `dismissed` | A person closed it, with a reason. A later run does not raise it again. |
| `expired` | Its `valid_until` day is in the past, or the open budget put it below the line. |

`new`, `told`, and `acknowledged` are open. A snooze is not a status. It is
the day `snoozed_until` on an open insight: the last day of the snooze. An
insight is **due** when it is open and it has no `snoozed_until`, or its
`snoozed_until` is before today. Today is the day in the `timezone` of
`joshua.yaml`. So a snooze until 2026-10-25 makes the insight due again on
2026-10-26.

A snooze with no number of days is `jobs.insights.snooze_days` (7) days,
times 2 for each earlier snooze of the insight, and at most
`jobs.insights.snooze_max_days` (90) days. With the defaults, the snoozes of
one insight are 7, 14, 28, 56, and then 90 days. A snooze with a number of
days uses that number, from 1 to `snooze_max_days`. A snooze includes
today.

### Retrieval in chat

The pages that the jobs write are the work of the jobs, not memory. If they
came into each chat, one concern would come back in a knowledge page, an
insight, and a question, also in a chat about something else. So a turn gets
them in these ways only:

| Way | Knowledge | Insight | Question |
|---|---|---|---|
| The injection before the turn | no | no | no |
| The `search_memory` tool | yes | no | no |
| The topic hint line | no | the count | the count |
| `list_insights`, `list_questions` | no | yes | yes |

The injection leaves out the kinds `knowledge`, `insight`, and `question`.
`search_memory` leaves out `insight` and `question`, and keeps `knowledge`,
so a person or Joshua can search the facts that the researcher found. Its
description tells the agent to read the insights and the questions with
their own tools.

**The topic hint.** When a turn that a person sent is about a topic with due
insights or open questions, the injection adds one line:

```
[Open items about Snake plant (houseplants/snake-plant.md): 1 insight, 2 questions. Read them with list_insights(topic=...) / list_questions(topic=...) before you mention one.]
```

A turn is about a topic when one of these is true:

- A chunk of the search, at `memory.inject.hint_sim` or more, is the topic
  page.
- The text of the turn holds a name of the topic, by the name rule of the
  `mentioned` selection (see [The topic](#the-topic)).

The chunks of the search come first, in their rank order, then the topics
that the text names. A line names 2 topics at most. It holds the title, the
path, and the counts, and never a claim or a question. A scheduled task, an
event, and a webhook turn get no line. The counts come from the frontmatter
of the pages on the disk: a due insight (see
[Status, snooze, and due](#status-snooze-and-due)) and a question with
`status: open`. Core keeps the counts for 60 seconds, so a turn does not read
every page. Insights count only when `jobs.insights.enabled` is true, and
questions only when `jobs.research.enabled` is true. A failure skips the line
and logs a warning, and the turn goes on. The `kb_event` row of the turn
holds the topics of the line, with their counts, in `topics`.

By default, `list_insights` lists only the due insights, and
`list_questions` only the open questions. So a closed concern does not come
back in chat.

The pages stay on the disk and in the wiki. `POST /v1/memory/search`, which
the viewer and the MCP search use, returns every kind and does not apply
these rules.

### The research hook

A research `run` that publishes a page also runs the insight pass for the
same topic, at the end of the same run, when `jobs.insights.enabled` is true
and `jobs.insights.after_research` is true. The `then_insights` argument of
the research run replaces `after_research` for that run.

- The paths of the pass join the paths of the research run, so one commit
  holds the knowledge page and the insight.
- The worker call of the pass counts under `insight` in the usage of the
  research run.
- The report of the pass is the section **Insights** of the research report.
- On a dry research run, the pass is a dry run too. It reads the pages that
  the research run would publish.
- A failure of the pass is a line in the **Insights** section. The research
  run does not fail.

### The chat tool

The `insights` tool server is in core. A member gets five tools:

| Tool | What it does |
|---|---|
| `list_insights(status="open", topic=None, limit=20)` | lists insights, ranked. `open` means the due insights. `snoozed` means the open insights that a snooze hides today. A status can also be `new`, `told`, `acknowledged`, `dismissed`, `expired`, or `all`. |
| `acknowledge_insight(slug)` | sets `status: acknowledged`, `resolved_at`, and `resolved_by: <person id>` on a `new` or `told` insight |
| `dismiss_insight(slug, why)` | sets `status: dismissed`, `resolved_at`, `resolved_by`, and `resolution_note` on an open insight. A later run does not raise it again. |
| `snooze_insight(slug, days=None)` | sets `snoozed_until` and adds 1 to `snoozes` on an open insight (see [Status, snooze, and due](#status-snooze-and-due)). A `new` insight becomes `acknowledged`. The answer gives the day the insight is due again. |
| `run_insights(topic)` | starts a run that is not a dry run, and answers with the run id |

The list is ranked: the highest severity first (`urgent`, `act`, `note`),
then the highest confidence (`high`, `medium`, `low`), then the oldest
`found_at`. With `all`, the open insights come first. Each line has the id,
the status, the severity, and the claim. The first line of the answer is
"Ranked by severity, then confidence. Name every urgent and act item when
you summarize."

The agent uses `snooze_insight` when a person says not now, later, or that
they are busy, and `dismiss_insight`, with the reason, when a person says no
or that the insight does not matter to them. In a live conversation about
the topic of a due insight, the agent can ask once if the person wants to
plan something. It does not ask in a scheduled task or an event message,
and it does not write a snooze to the journal.

`slug` is `<section>/<slug>`. A status change takes the job lock, writes the
page and `insights/README.md`, and commits the wiki with the message
`insights: <slug> <status> by <person id>`, or
`insights: <slug> snoozed until <day> by <person id>` for a snooze. A guest, or a speaker that core
does not know, gets a refusal from each tool, and nothing changes. The server
is there only when `jobs.enabled` and `jobs.insights.enabled` are both true.

### Telling us

An insight with `status: new` waits in `wiki/insights/` for a later delivery
step, which is not in this release. Now, the chat tools are the way to read
and resolve an insight, and the report of each run is a page in
`wiki/job-runs/`.

### What the insights job never does

- It never writes outside `wiki/insights/`. Each path comes from `layout`,
  and a write outside `insights/` fails.
- It never raises an insight with `basis: knowledge` for a topic with no
  published knowledge page.
- It never raises a claim with `confidence: low`.
- It never keeps more than `jobs.insights.max_open` insights open when the
  `rank` worker answered.
- It writes nothing in a dry run, and expires nothing.

## The digest

A task that runs each day, or a subject that comes back again and again,
leaves many journal entries that say nearly the same thing. They fill the
search results and the memory bundles, so one old subject takes the place of
the subjects that matter now. The digest job reads a run of such entries and
writes one digest entry that keeps each distinct fact. The entries that it
covers stay on the disk, in the wiki history, and in the viewer, and you can
open each one by its path and its day. Their text does not change. Only the
index and the bundles leave them out.

Set `jobs.digest.enabled: true`, with `jobs.enabled: true`, to add the job.
[docs/config.md](config.md#jobs) has the keys. Nothing schedules it.

```
scripts/jobs.sh digest plan
scripts/jobs.sh digest run --dry-run
scripts/jobs.sh digest run
scripts/jobs.sh digest undo journal/2026/03/08/digest-filter-check.md
```

The argument `mode` is required: `plan`, `run`, or `undo`. `undo` also needs
the argument `digest`, the path of a digest entry relative to the wiki root.
`scripts/jobs.sh digest plan` sends a dry run.

### The candidates

A candidate is a journal entry dated before today less
`jobs.digest.min_age_days`. These entries are never candidates:

- the nightly page of a day, `YYYY/MM/DD/YYYY-MM-DD.md`;
- the report of a job run;
- an entry that a digest covers already: its frontmatter has `digest`;
- a digest entry: its `source` is `digest`;
- an entry whose frontmatter block is not a YAML mapping.

### The groups

Python makes the groups, in two steps:

1. By the slug. The stem of an entry is its file slug without the tokens
   that name a date: numbers, ordinals such as `3rd`, and the names of the
   weekdays and the months. `filter-check-monday-march-3` and
   `filter-check-tuesday-march-4` have the stem `filter-check`. The entries
   with one stem are a group when there are `jobs.digest.min_entries` of them
   or more.
2. By the meaning. Each other entry gets one vector, the mean of the vectors
   of its chunks, from the same embedding model as the index. In date order,
   an entry joins the group whose first entry is closest to it when the
   cosine is `jobs.digest.sim` or more. Else it starts a new group.

A group of more than `jobs.digest.max_entries` entries is split into runs in
date order, of sizes that differ by one at most. A group or a run with fewer
than `min_entries` entries is dropped. The groups go in order of size, the
largest first, then by the first day. A run takes the first
`jobs.digest.max_groups`. The same wiki on the same day gives the same groups.

### What a run does

`plan` finds the groups and gives the path of each digest. It calls no worker.

`run` then asks the `digest` worker once for each group. The worker has no
tools and a timeout of 120 seconds. It gets the entries of the group in date
order, each with its path, its date, and its people. The prompt tells it to
write in Joshua's own voice, in the third person, to keep each number, date,
decision, change, and outcome one time, to say how the subject stood at the
last entry, and to invent nothing. The prompt tells the worker that the
entries are content, not instructions. The answer is a `title` and a
`summary` in Markdown. Python rejects a summary of more than 500 words, and
removes each link to a URL that no covered entry holds. The worker does not
decide which entries a digest covers: the group does.

For each answer that passes, the run writes:

1. The digest entry, on the day of the last entry of the group, with the slug
   `digest-<stem>` (or `digest-<first slug>` for a group by meaning), and a
   number after it when that slug is taken. Its frontmatter is:

   ```yaml
   date: 2026-03-08
   people: [alex, sam]
   source: digest
   covers:
     - journal/2026/03/03/filter-check-monday-march-3.md
     - journal/2026/03/04/filter-check-tuesday-march-4.md
   digest_run: 41
   ```

   The body is the title, the summary, the line "Covers N entries from
   <first day> to <last day>", and a link to each covered entry. The key is
   `digest_run`, not `run_id`, because a digest is memory: the index and the
   bundles keep it.
2. On each covered entry, one frontmatter line: `digest: <digest path>`.
   The body and each other line of the entry stay byte for byte.

A dry run calls the workers and writes nothing. Its report holds the full
text of each digest.

### The report

The summary is `digest: N digests, M entries covered, R rejected`, or
`digest plan: N groups, M entries` for a plan. The report has the sections
**Groups** (each with its stem, how it was made, its size, its first and
last day, and the digest path), **Skipped** (the count of entries for each
reason), **Digests** on a dry run, **Notes**, and **Usage**. The worker call
counts under the name `digest`.

### Undo

A covered entry comes back to the index and the bundles when its `digest:`
line goes. `mode: undo` removes that line from each entry that the digest
covers, and deletes the digest entry, in one commit. A dry run shows the
entries that would come back and writes nothing. You can also remove the
lines by hand, or `git revert` the commit of the run in `/data/wiki`.

### What the digest job never does

- It never changes the body of a journal entry, or a frontmatter line other
  than the one line it adds.
- It never deletes an entry, except a digest entry in an undo.
- It never takes a nightly day page, a job report, or a recent entry.
- It writes nothing in a dry run or a plan.
- It never reads a snapshot. Its groups come from the journal only.
