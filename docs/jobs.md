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

Package 2. The curator keeps the wiki and the journal correct and organized.

## The researcher

Package 3. The researcher keeps `wiki/knowledge/`: researched facts about one
topic at a time, in a dossier per topic.

## Insights

Package 4. The insights job keeps `wiki/insights/`: where what we do is
different from what the knowledge says.
