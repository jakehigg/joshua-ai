# Data volume layout

One volume mounts at `/data` in all three containers. `joshua_shared.layout`
builds every path, so channels, core, and gateway agree on where a file lives.

Tests override the root with the `JOSHUA_DATA_DIR` environment variable.
Production uses the fixed `/data`.

## Tree

```
/data/people/<person_id>/profile.md
/data/people/<person_id>/blog/YYYY-MM-DD.md
/data/people/<person_id>/blog/YYYY-MM-DD-HHMM-<slug>.md
/data/people/<person_id>/attachments/YYYY/MM/<file>
/data/people/<person_id>/cli/outbox.jsonl        (the terminal channel's outbox)
/data/wiki/README.md
/data/wiki/**.md                                 (the one wiki, incl. wiki/skills/*.md)
/data/wiki/joshua/*.md                           (docs from the repo, rewritten each start)
/data/wiki/.trash/<UTC timestamp>/**             (pages the viewer deleted; the index skips it)
/data/shared/README.md
/data/shared/profile.md
/data/shared/attachments/<group>/YYYY/MM/<file>  (files from a group chat)
/data/inbox/                                     (channels scratch, short-lived)
```

`person_id` is a slug that matches `^[a-z0-9][a-z0-9-]{0,31}$`.
`layout.safe_segment` rejects any other value, so a person id cannot escape the
volume.

## Ownership

All three containers run as uid 1000. All three mount the volume read-write.
The code decides who writes what:

`people/<id>/` groups a person's files. It is provenance and not a boundary:
the corpus is shared, and any member reads any journal. What a person may write
comes from their role, never from the path. See `security.md`.

- channels writes `people/<id>/attachments/`, `shared/attachments/`,
  `people/<id>/cli/`, and `inbox/`.
- core writes `people/<id>/blog/`, `profile.md`, `shared/profile.md`, `wiki/joshua/`, and
  `inbox/sessions/`.
- gateway (files MCP) writes `wiki/` and `people/<id>/blog/` for a member.

Nobody else writes. `/data/wiki/**` and `/data/shared/**` are readable by
everyone.

## The wiki

There is one wiki, at `/data/wiki/`. Every person who uses Joshua reads it. A
member writes it. A guest reads it only. The agent files reference pages there:
recipes, how things work, decisions, notes about people, plans.

`wiki/joshua/` is different. The repo owns it. Core copies the documentation
from the image at each start. Core replaces an edit there, and an upgrade
brings the new text. Core touches no other directory. The index holds these
pages at shared scope, so any person can ask about them. `JOSHUA_DOCS_DIR` sets
the source directory. The default is `/app/docs`.

`wiki/skills/<slug>.md` is a taught skill: a "when I say X, do Y" behavior. The
frontmatter holds a `name` and a `triggers` list; the body is the instructions.

```
---
name: movie time
triggers: ["movie time", "let's watch a movie"]
---
Dim the living room lights to 30 percent and turn on the TV.
```

A member teaches one. [memory.md](memory.md#taught-skills) explains the match.

`wiki/.trash/` holds pages the viewer deleted. A delete moves the page to
`wiki/.trash/<UTC timestamp>/<the path under wiki>`; it is never a hard delete.
The timestamp directory keeps the original tree, so you restore a page with a
shell. The index skips any path with a `.trash` part, so a deleted page leaves
the search index at the next index run.

An older layout kept a wiki per person under `people/<id>/wiki/`. At start,
core moves those pages to `wiki/<id>/` and removes the empty directory. A page
that already exists at the target stays where it was, and the log names the
count.

## Profiles

`profile.md` is the person's short, standing profile. The system prompt always
holds it. Nightly reflection rewrites it. The sections are fixed:

```
## About
## Preferences
## Ongoing
## Notes for Joshua
```

`shared/profile.md` is the shared profile: who is who, shared facts, and
standing shared preferences. It has the same shape. Nightly reflection writes
it. Every member's prompt reads it.

## Blog

The `blog/` directory holds two kinds of post:

- `YYYY-MM-DD.md`: the nightly digest. Core reflection writes it.
- `YYYY-MM-DD-HHMM-<slug>.md`: a post the agent writes during a conversation.

The index holds both kinds.

## Attachments

An attachment file is named `YYYY-MM-DD-HHMMSS-<stem>.<ext>`. A `.meta.json`
sidecar next to it holds the original name and the source metadata.

## Bootstrap

`layout.bootstrap_person(pid, display_name)` creates `blog/` and
`attachments/`, then writes `profile.md` from the template if it is absent.
`layout.bootstrap_wiki()` creates `wiki/` with its `README.md` and `skills/`,
and moves the pages of the older per-person layout. `layout.bootstrap_shared(name)`
creates `shared/` with its `README.md` and `shared/profile.md`. All three are
idempotent. A second call changes nothing.

Bootstrap runs at core startup for every configured person, and again when
`add_user` or the people CLI adds a new person.

## Readiness

`layout.validate_layout()` returns a list of problems. A problem is a missing
data root, a root that is not writable, a missing `wiki/` or `shared/`, or a person
directory without a subdirectory or `profile.md`. Core's `/readyz` reports
`layout: false` when the list is not empty.
