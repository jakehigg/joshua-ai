# Data volume layout

One volume mounts at `/data` in all three containers. `joshua_shared.layout`
builds every path, so channels, core, and gateway agree on where a file lives.

Tests override the root with the `JOSHUA_DATA_DIR` environment variable.
Production uses the fixed `/data`.

Joshua keeps its memory in one folder, the wiki. A wiki frontend, such as
Otter Wiki, reads and writes it directly. The journal and the profiles live
inside it now.

An older layout kept them under `people/<id>/`. At each start, core moves
what it finds there onto the new paths, and removes the old ones. A page
already at the new path stays where it is. See [Migration](#migration).

## Tree

```
/data/wiki/Home.md                               front page; core makes it once, Joshua and people keep it
/data/wiki/joshua-docs/*.md                      docs from the repo, rewritten each start
/data/wiki/journal/YYYY/MM/DD/YYYY-MM-DD.md      the nightly page for that day
/data/wiki/journal/YYYY/MM/DD/<slug>.md          an entry Joshua wrote that day
/data/wiki/people/<person_id>.md                 Joshua's profile of a person
/data/wiki/people/everyone.md                    the shared profile
/data/wiki/**.md                                 the rest of the wiki, incl. wiki/skills/*.md
/data/wiki/.trash/<UTC timestamp>/**             pages the viewer deleted; the index skips it
/data/wiki/.git/                                 the wiki's git repository; core makes it
/data/wiki/.gitignore                            excludes .trash/; core writes it once
/data/people/<person_id>/attachments/YYYY/MM/<file>
/data/people/<person_id>/cli/outbox.jsonl        (the terminal channel's outbox)
/data/shared/attachments/<group>/YYYY/MM/<file>  (files from a group chat)
/data/inbox/                                     (channels scratch, short-lived)
```

`person_id` is a slug that matches `^[a-z0-9][a-z0-9-]{0,31}$`.
`layout.safe_segment` rejects any other value, so a person id cannot escape
the volume.

No person may use `everyone` as an id. It names the shared profile.

## Ownership

All three containers run as uid 1000. All three mount the volume read-write.
The code decides who writes what.

`people/<id>/` groups the files that belong to one person: attachments and
the CLI outbox. It is provenance, not a boundary. The corpus belongs to
everyone, and any member reads any journal entry or profile. What a person
may write comes from their role, never from the path. See `security.md`.

- channels writes `people/<id>/attachments/`, `shared/attachments/`,
  `people/<id>/cli/`, and `inbox/`.
- core writes `wiki/journal/`, `wiki/people/`, `wiki/joshua-docs/`, and
  `inbox/sessions/`.
- gateway (files MCP) writes the rest of `wiki/` for a member, and
  `wiki/people/<id>.md` for that person. The `<id>` must be a person on the
  roster, so a write makes no page beside a real one.

Nobody else writes. `/data/wiki/**` and `/data/shared/**` are readable by
everyone.

## The wiki

There is one wiki, at `/data/wiki/`. Every person who uses Joshua reads it.
A member writes it. A guest only reads it. `Home.md` is the front page.

`layout.bootstrap_wiki` writes `Home.md` once, from a template, and never
touches it again. An edit a person makes to the page stays.

The agent files reference pages in the wiki: recipes, how things work,
decisions, plans. `wiki/joshua-docs/` is different. The repo owns it.

Core copies the documentation from the image at each start. Core replaces an
edit there, and an upgrade brings the new text. The index holds these pages
at shared scope, so any person can ask about them. `JOSHUA_DOCS_DIR` sets the
source directory. The default is `/app/docs`.

`wiki/skills/<slug>.md` is a taught skill: a "when I say X, do Y" behavior.
The frontmatter holds a `name` and a `triggers` list. The body holds the
instructions.

```
---
name: movie time
triggers: ["movie time", "let's watch a movie"]
---
Dim the living room lights to 30 percent and turn on the TV.
```

A member teaches one. [memory.md](memory.md#taught-skills) explains the match.

`wiki/.trash/` holds pages the viewer deleted. A delete moves the page to
`wiki/.trash/<UTC timestamp>/<the path under wiki>`. It is never a hard
delete. The timestamp directory keeps the original tree, so you restore a
page with a shell. The index skips any path with a `.trash` part, so a
deleted page leaves the search index at the next index run.

The file tools, the viewer, and the index ignore any file or directory whose
name starts with a dot, at any depth under `wiki/`. This is what lets a wiki
frontend, such as Otter Wiki, keep its own state next to the pages: a `.git`
directory is a common example. See [docs/enhancements.md](enhancements.md).

`wiki/` is also Joshua's own git repository, so a frontend never shows a page
as not under version control. Core makes it at the first start
(`git init`), with `wiki/.gitignore` excluding `wiki/.trash/`. Core commits
everything at each start, gateway commits each write the agent makes
(`write_file`, `rename_file`, `write_journal_entry`) and each delete the
viewer makes, and core commits the day page and the profiles at the nightly
run, then anything else that changed. `wiki.git` turns this off; see
[docs/config.md](config.md#wiki).

## Journal

`wiki/journal/YYYY/MM/DD/` is the day-by-day memory of Joshua, not of a
person. It holds two kinds of page:

- `YYYY-MM-DD.md`: the nightly page. Core reflection writes it.
- `<slug>.md`: an entry Joshua wrote during the day. The slug matches
  `^[a-z0-9][a-z0-9-]{0,63}$` and never equals the name of the nightly page.

The index holds both kinds. This is how Joshua remembers that a person did
something: as an event in its own journal, in its own voice, not as a post
that belongs to the person.

## Profiles

`wiki/people/<id>.md` is the profile Joshua keeps for a person: short,
standing, and always in the system prompt. Nightly reflection rewrites it.
The sections never change:

```
## About
## Preferences
## Ongoing
## Notes for Joshua
```

`wiki/people/everyone.md` is the shared profile: who is who, shared facts,
and standing shared preferences. It has the same shape. Nightly reflection
writes it. Every member reads it in their own system prompt.

## Attachments

An attachment file has the name `YYYY-MM-DD-HHMMSS-<stem>.<ext>`. A
`.meta.json` metadata file next to it holds the original name and the source
metadata.

## Bootstrap

`layout.bootstrap_person(pid, display_name)` creates `attachments/` under
`people/<pid>/`, then writes the profile at `wiki/people/<pid>.md` from a
template if it is absent. `layout.bootstrap_wiki()` creates `wiki/` with
`skills/`, `journal/`, `people/`, and `Home.md`, and removes a stale
`wiki/README.md` left by an older layout. `layout.bootstrap_shared()` creates
`shared/attachments/`. `layout.bootstrap_shared_profile(name)` writes the
shared profile at `wiki/people/everyone.md` from a template if it is absent.
All four are idempotent. A second call changes nothing.

Bootstrap runs at core startup for every configured person, and again when
`add_user` or the people CLI adds a new person.

## Migration

`layout.migrate_to_one_wiki()` runs at every core start, before the shared
profile is bootstrapped. For each person, it moves `blog/*.md` onto
`wiki/journal/` and `profile.md` onto `wiki/people/<id>.md`. It also moves
`shared/profile.md` onto `wiki/people/everyone.md`, and removes
`shared/README.md` and `wiki/README.md`, which `Home.md` replaces. An
existing `wiki/joshua/` is renamed to `wiki/joshua-docs/`; if both are
present, `wiki/joshua/` is removed instead, since core owns and rewrites it.

An old per-person digest, `blog/YYYY-MM-DD.md`, lands as `<id>.md` in the
day's journal folder: `YYYY-MM-DD.md` itself is reserved for the new
instance-wide nightly page. A colliding journal entry, two posts by
one person on one day with the same slug, gets the old post's time appended to its
name. A moved journal page gets `people` and `date` added to its
frontmatter, so the index can still scope it to the person who wrote it.
When a target already exists and it is not still the unedited shipped
template, migration does not overwrite it, and the source stays where it
was, counted as skipped. The function does nothing on a volume that never
had the old layout. It logs one line with what it moved.

## Readiness

`layout.validate_layout()` returns a list of problems. A problem is a
missing data root, a root that is not writable, a missing `wiki/` or
`shared/`, or a person directory without `attachments/` or a profile page at
`wiki/people/<id>.md`. Core reports `layout: false` from `/readyz` when the
list is not empty.
