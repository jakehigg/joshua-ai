# Documents

A document is what Joshua works on or makes for a person: a plan, a draft, a
report, a spec, a meal plan, a party plan, a list in progress, an artifact.
It belongs to a member, it changes many times, and each change is a version.
You read it and comment on it in the viewer, and Joshua changes it when you
ask.

This page is the guide. [config.md](config.md#the-documents-mcp) has the
reference for the tools and the files, and
[memory.md](memory.md#documents-in-the-index) has how the index holds them.

## Four places for text

Joshua keeps four kinds of text, and each one has its own place:

| Place | What it holds | Example |
|---|---|---|
| wiki | what Joshua knows: reference that is still true next month | a recipe, a runbook, how the network is set up |
| journal | what happened | "Alex and Joshua planned the party on Saturday." |
| knowledge | what a person gave Joshua to read | a manual, a book, a folder of notes, through an MCP server that the operator adds |
| documents | what Joshua works on or has made | the party plan, this week's meal plan, the findings of an audit |

The rule: a text that you will change again, or that has a status, is a
document. A text that stays true as reference goes to the wiki. When in
doubt, it is a document.

The wiki refuses a plan or a draft. When Joshua tries to write a page with
`kind: plan`, `report`, `spec`, `artifact`, or `draft`, or with
`status: draft`, into the wiki, the write fails with "a plan or a draft is a
document: use write_document".

Joshua does not get the body of a document in each turn. The index holds
the title and the summary only, so a long plan does not come back each time
you talk about its subject. Joshua reads the document when it needs it.

## What a document is

A document is one file on the data volume, with YAML frontmatter at the top:

```
documents/<owner>/<slug>.md       a Markdown document
documents/<owner>/<slug>.html     an HTML document, an artifact
```

`<owner>` is the id of a member, or `shared` for a document of every member.
A member reads and writes their own folder and `shared/`, and never the
folder of another member. A guest has no documents. A group chat reaches
`shared/` only.

### Kinds

| Kind | Use it for |
|---|---|
| `plan` | the steps of something to do: a party, a migration, a clean-up |
| `report` | what was found or done: the findings of an audit |
| `spec` | what something must be: a feature, a purchase, an assignment |
| `artifact` | a thing to look at: a page, a table, a mock-up |
| `note` | anything else (the default) |

### Statuses

| Status | Means |
|---|---|
| `draft` | it still changes (the default) |
| `active` | it is in use: the meal plan of this week |
| `final` | it is done |
| `archived` | it is kept, but not in use. The index drops it. |

Joshua keeps the status true as the document changes.

### Formats

A document is Markdown (`.md`) or HTML (`.html`). The name of the file gives
the format. One slug holds one document, so `party.md` and `party.html`
cannot both exist. A slug never ends in `.comments` or in `.json`: the
comments file and the frontmatter route of the viewer own those names.

### The frontmatter

```yaml
title: Party plan            # required
kind: plan
status: active
format: markdown             # from the file name
tags: [party, june]
summary: The garden party on 14 June, for 30 people.
author: alex                 # set by the server
assignment: 12               # set by the server, when an assignment wrote it
created: 2026-06-01T09:12:00+00:00   # set by the server
updated: 2026-06-03T18:40:00+00:00   # set by the server
promoted_to: wiki/recipes/lemon-cake.md   # set when part of it went to the wiki
```

Joshua gives the title, the kind, the status, the tags, and the summary.
The server sets `author`, `assignment`, `created`, `updated`, and
`promoted_to`, and nobody can write them by hand. Without a `summary`, the
first paragraph of the body is the summary.

## Artifacts

An artifact is a document of kind `artifact`: a thing to look at more than
to read. It is Markdown or HTML.

- A Markdown artifact shows in the viewer as a Markdown document does.
- An HTML artifact shows in a frame, apart from the viewer. The frame cannot
  reach the viewer, your sign-in, or a form of the viewer. Scripts do not
  run unless the frontmatter has `scripts: true`.

The frame loads the artifact from an address of its own,
`/documents/<owner>/<slug>/artifact`, which has a content policy of its own.
So a `<style>` element and a `style` attribute of the artifact render, and an
image comes from the viewer. The artifact reaches no network: it loads no
font, no stylesheet, and no script from a file, and it sends no request. It
submits no form and opens no frame. Write the styles in the file.

## Comments

You comment on a document in the viewer, part by part:

1. Open the document.
2. Click the `+` beside a paragraph, a heading, or a list item.
   The `+` at the head of the page is a comment on the whole document. The `Source` fold has a `+` on each line,
   for one exact line.
3. Write what to change, such as "make this two days" or "remove the
   oysters", and click `Comment`.
4. Tell Joshua: "I left comments on the party plan."

A comment on a document does not reach Joshua alone: tell Joshua. Joshua
then reads the open comments, changes the document, and resolves each
comment with one line that says what it did. The resolved comment moves to
the `resolved` fold of the page, with that line.

A comment names the line where you put it, and keeps the text of that line.
When the line changes after your comment, the comment shows a
`line changed` mark and the line as it was, and Joshua compares the two
first. You can delete your own open comment. A resolved comment stays.

The comments of `<slug>.md` are in `<slug>.comments.jsonl` beside it, in the
same history as the document.

## Versions

Each write of a document is one version. `/data/documents` is a git
repository of its own, and each write is one commit by Joshua. A comment is
a commit too.

In the viewer, the `Versions` fold of a document lists its versions, the
newest first. Click one to read the document as it was. An old version is
read-only and takes no comment.

So a document states what is true now, as fact, and does not tell how it
got there: its versions and the resolutions of its comments hold what
changed.

Joshua writes a document in Simplified Technical English: one idea in each
sentence, the present tense, names and numbers instead of adjectives, and a
line that starts with "Not verified:" for what it did not check.

## Promote to the wiki

A finished document can hold something that stays true: a recipe in a meal
plan, a runbook in a clean-up plan. Say "promote the recipe part to the
wiki". Joshua writes that part as a reference page of the wiki, then
archives the document and records the page in `promoted_to`. The wiki page
follows the rules of the wiki, so it holds the reference and not the plan.
Joshua also notes in its journal that it happened.

## The Documents section of the viewer

The `Documents` link in the menu of the viewer opens `/documents`. A member
sees their own documents and the shared ones. A guest has no `Documents`
link.

- The pills select a status: `Active` (the default), `Drafts`, `Final`, and
  `Archived`. The chips under them select a kind.
- Each row has the title, the summary, the kind, the owner (`yours` or
  `shared`), the time of the last change, the count of open comments, and
  the status.
- A document opens at `/documents/<owner>/<slug>`. A Markdown document shows
  in blocks: one block for each paragraph, heading, list item, quote, code
  block, and table. An HTML document shows in its frame (see
  [artifacts](#artifacts)).
- `Source` gives the frontmatter and each line of the body. `Versions` gives
  the history.
- `/documents/<owner>/<slug>.json` gives the title, the kind, the status, and
  the time of the last change, as JSON, and no part of the body. The chat
  shows the title of an artifact card from it, and reads it again while the
  panel is open to see a change that no turn made. It needs the same sign-in
  as the page, a document of another member is 404, and the answer is never
  cached. A slug never ends in `.json`, so this address always names the
  frontmatter of `<slug>`.

The viewer writes only your comments. Joshua makes every other change.
[operations.md](operations.md#the-documents-section) has the details, and
how to turn on the viewer.

## Documents from an assignment

An [assignment](assignments.md) writes its deliverables as documents of the
person who asked for it: a plan for you, a spec, findings, a draft. The
server sets `author: assignment:<id>` and `assignment: <id>` on each one, so
you can see which assignment wrote it. The worker records each one as an
output of kind `document`.

On the page of the assignment, the output card of a document has an `Open`
link to its page. There you read it, comment on it, and tell Joshua what to
change. When the goal of the assignment tells the worker to read that
document first, a re-run of the assignment works from the document as you
changed it. See [use an assignment](assignments.md#use-an-assignment).

The plan and the report of the assignment itself are not documents. They
stay in the assignments store and show on the page of the assignment.

## The tools of Joshua

The `documents` server gives Joshua these tools, for a member only:

| Tool | What it does |
|---|---|
| `list_documents` | lists your documents and the shared ones, with a filter on the owner, the kind, the status, or a tag |
| `read_document` | gives the frontmatter, the body, and the count of open comments |
| `write_document` | makes a document, or changes one. Each write is one version. |
| `set_document_status` | sets `draft`, `active`, `final`, or `archived` |
| `search_documents` | finds the documents that hold some words |
| `promote_document` | writes a wiki page from a document, and archives the document |
| `trash_document` | moves a document and its comments to `documents/.trash/` |
| `document_comments` | gives the comments on a document |
| `resolve_document_comment` | resolves one comment, with one line on what was done |

There is no tool to read a trashed document. To get one back, the operator
moves it back from `documents/.trash/` with a shell.

## Say it to Joshua

| You say | Joshua does |
|---|---|
| "Make that a document." | writes it as a document with a title and a kind, and tells you its path |
| "Plan the party as a document for all of us." | writes it in `documents/shared/` |
| "What plans do I have?" | lists your documents of kind `plan` |
| "Find the document about the garden." | searches the titles, the tags, the summaries, and the bodies |
| "I left comments on my plan." | reads the comments, changes the document, and resolves each one |
| "The meal plan is final." "Archive the party plan." | sets the status |
| "Promote the recipe part to the wiki." | writes that part as a wiki page, and archives the document |
| "Make me a page that shows the schedule." | writes an artifact |
| "Throw away the old draft." | moves it to the trash |

## Turn it on

The operator adds the builtin server to `joshua.yaml`:

```yaml
mcp:
  documents:
    kind: builtin
    allow: all
```

The server makes `/data/documents/` and its git repository on the first
write. To read the documents in a browser, turn on the viewer: see
[operations.md](operations.md#the-viewer).
