# Workspaces

A workspace is a place for one large piece of work: code, a meal plan, a trip,
a house project. It has its own chat thread. It holds the MCP servers that the
person picked, a model, a goal, some notes, and links to what was built or
collected for it. Normal chat stays small, because project talk goes into the
workspaces.

A workspace chat is the full Joshua. It has the same prompt, the same builtin
tools, the same memory, and the same RAG from everything outside the
workspace. Only these things differ: the thread, the model, the servers, the
prompt file `workspace.md` (and `workspace_shared.md` in a shared workspace),
and a small workspace block in each message.

Nothing changes until a workspace exists. The tables are empty and no turn
reads them.

## What a workspace holds

| Field | Meaning |
|---|---|
| `slug`, `title` | The slug comes from the title. It is unique for each owner among the workspaces that are not closed, and the slug of a shared workspace is unique over everybody. A closed workspace keeps its slug and frees the name; its `id` is the permanent name of the row. |
| `owner` | The person who made it. |
| `shared`, `members` | `shared` is true for a workspace that several people write in, and `members` names the other people. A person who is neither the owner nor a member sees the workspace as not found. |
| `goal` | A short paragraph. |
| `notes` | Standing facts and rules. The agent keeps them with `update_workspace_notes`. |
| `servers` | The MCP servers that the workspace uses. Only servers that every person of it may use. |
| `model` | The model of the workspace. Empty keeps the model of the chat job. |
| `notify` | `none` (default), `questions`, or `questions+finishes`. |
| `status` | `active`, `paused`, or `archived`. `archived` means closed: it frees the name for a new workspace, and the closed row keeps its own slug. |

An item is a link: a `kind` (`document`, `artifact`, `plan`, `wiki`,
`tasks_project`, `task`, `assignment`, `developer_task`, `pr`, `link`, or
`file`), a `ref`, a `title`, and an optional `url`. It also has `added_by`,
`pinned`, and `seen`. Adding an item with the same kind and ref again updates
the row.

## The thread

A new workspace gets the channel `workspace:<id>` (type `workspace`) and one
conversation. The conversation belongs to the owner, and to nobody for a
shared workspace, where it is the one thread of everybody. A turn on this
channel runs only when the workspace exists and the person of the turn reaches
it: the owner, or a member of a shared one. Core never creates a `workspace:`
channel from a turn.

Replies stay in the thread. Core writes each reply to the transcript, and the
viewer reads it there. Core delivers nothing to a phone for a workspace: not a
reply, not the result of a scheduled task, and not the reply to an event. The
agent sends one message to the normal chat with `send_to_normal_chat`. It does
so when the person asked ("text me when done") or when `notify` allows it.

A scheduled task belongs to the conversation that made it, so a task that was
made in a workspace runs in that workspace.

A scheduled or event turn in a workspace starts with this line: "This is a
scheduled task in a workspace. Nothing you write here reaches the person. If
the person should see it, call send_to_normal_chat." The description of
`schedule_task` says the same inside a workspace.

## A shared workspace

A workspace can hold several people. The person who makes it picks the others
from the roster, and they are the `members`. The people of a workspace are set
at the create and never change: no route and no tool adds or removes one. For
other people, make another workspace.

A shared workspace is one thread. The owner and every member write in it, read
every row, and see the same goal, notes, and items. Everything written there is
read by everybody in it.

**The address.** A link to a workspace has two segments: `<owner>/<slug>` for a
personal one and `shared/<slug>` for a shared one. The viewer address is
`/chat/w/<owner>/<slug>` or `/chat/w/shared/<slug>`, so everybody in a shared
workspace opens the same address. `shared` is the namespace of a shared
workspace, so no person may hold the id `shared`. A create that names one
answers 422.

**The slug.** The slug of a personal workspace is unique for its owner, and a
taken one gets a number (`solar-install-2`). The slug of a shared workspace is
unique over all people, because its address names no owner. A title whose
shared slug is taken answers 409 and gives the slug, and the person picks
another title. A personal workspace of any person may hold the same slug.

Closing a workspace (`status` `archived`) frees its name at once: a new
workspace of the same title takes the plain slug (`test`), personal or shared.
The uniqueness of a slug holds among the workspaces that are not archived, in
the database (two partial unique indexes with `status <> 'archived'`), so
nothing renames a row: the closed workspace keeps the slug it was created
with, for ever. A bare slug names the live workspace that holds it, so
`/chat/w/<owner>/test` is the new one; a slug reaches a closed workspace only
when no live one holds it, and then the one made last. The permanent address
of a workspace, closed or not, is its id: `/chat/w/<id>`. That is the address
the chat screen links to.

**The ref.** A route, a tool, and a turn name a workspace with a ref. It has
three forms:

- `shared/<slug>`: the shared workspace of that slug;
- `<owner>/<slug>`: the workspace of that person with that slug;
- `<slug>` or `<id>`: one segment. Core takes the person's own workspace of
  that slug first, then the workspace of that id.

The person must be one of the people of the workspace. Any other ref is not
found. The one-segment form keeps every older link working: the owner of a
shared workspace still reaches it by its slug, and a member by its id. A ref
with a slash reaches a route in the path
(`/v1/workspaces/shared/thanksgiving`) and URL-encoded
(`shared%2Fthanksgiving`) alike.

The ref is the human-readable form of the name, for an address a person types
and a name a person says to Joshua. The viewer never sends one once it has the
workspace: the screen resolves the address it was opened at once, and after
that names the workspace by its id, in every workspace route and as the
`workspace` of a turn, of the thread, of the history and of the hold
(`gateway/frontend/src/api/workspaces.ts`). A ref may move to another
workspace, because a closed one frees its name; an id names one row for ever,
so the closed workspace stays the one that is read and written.

**The turn.** A turn of a shared workspace runs as a group turn. The message
carries the name of the person who wrote it ("Mia: who brings the pie?"), and
the transcript row keeps that name in `meta.speaker`, so the viewer shows it on
the row. Core runs the turns of the one conversation one at a time. A message
written while a turn of the thread runs waits in the hold of the person who
wrote it, and goes as their own turn after.

**Who acts.** The person who wrote the turn acts. The session names that
person to the gateway, so a server with a bearer for each person (the
assignments container, the developer addon) acts as that person. When the
next turn is from another person, core builds the session again before the
turn runs, and the thread carries on. A turn with no person, a job event or a
scheduled task, acts as the owner.

**The screen.** The chat screen of a shared workspace loads the newest rows
every 4 seconds while the tab shows, and every 30 seconds while it is hidden,
so a message of another person shows with no reload. The thread reads like a
group messaging app: the messages of the person who is signed in stand at the
right, every other person's and Joshua's at the left, and each writer keeps
one bubble colour. A bubble at the left is named, once for a run of messages
from the same writer: the person's name, or Joshua. The colour comes from the
name `meta.speaker` holds, so every device agrees on it and nothing is
stored; two people with the same display name share a colour, and a renamed
person takes a new one. The screen of a personal workspace is unchanged.

**What it writes.** A document that Joshua writes in a shared workspace goes to
`documents/shared/<slug>.md`, where every member reads it. RAG skips the
documents of the workspace under that path, as it does for a personal one.

**One message, one person.** `send_to_normal_chat` sends to the main chat of
the person who wrote the turn, and never to everybody. On a turn with no
person, a job event or a scheduled task, it sends to the owner. To tell
everybody something, the agent writes in the thread.

## What a session gets

- **Model.** The model of the workspace replaces the model of the chat job.
  The server and the key of the job stay.
- **Servers.** The servers that the workspace picked, among the servers that
  the person may use. A shared workspace keeps the servers of its create,
  which are the servers that every person of it may use. The builtin tools
  stay as in a normal chat, with the `workspace` tools added.
- **Prompt.** The `dm` profile and `workspace.md`. A shared workspace takes
  the `group` profile, `workspace.md`, and `workspace_shared.md`, which tells
  the agent that several people read the thread, that each message names its
  writer, that `send_to_normal_chat` reaches one person, and when to use
  `notify_user`.
- **Role.** A workspace thread carries the role of its people, as a group chat
  does: `member` only when every person of it is a member.
- **Tool search.** Off when the workspace picks `workspaces.tool_search_max_servers`
  servers or fewer (default 3). On when it picks more. Core does not know how
  many tools a server has, so the rule counts servers.
- **Changes.** A change of `servers` or `model` applies on the next turn. Core
  builds a new session and resumes the same conversation.

## The workspace block

Each turn of a workspace starts with the workspace block. It holds:

- the title, the goal, the `notify` setting, and the notes;
- the "People:" line of a shared workspace, under the goal: the owner, marked
  `(owner)`, then the members. A personal workspace has no such line;
- the "Notify each person:" line of a shared workspace, under `Notify`: the
  notify mode of each person, as `alex decide, mia all`. It is what
  `notify_user` may do for them, and a personal workspace has no such line;
- the items as kind and title, with the ref in brackets: pinned items first,
  then the newest, at most 30;
- the items that came from normal chat and that this workspace has not seen.
  Core marks them seen after the turn;
- the messages that normal chat sent with `send_to_workspace`, under "From
  normal chat, since your last turn". Core marks them seen after the turn, so
  each shows once;
- "Next steps", when the workspace has a `tasks_project` item and
  `workspaces.tasks_url` is set: up to 3 steps (number and title) for each of the
  first 3 projects, from `GET <tasks_url>/api/next-steps?project=<ref>&limit=3`.
  The call waits 2 seconds. Any error skips the section;
- "Jobs started from this workspace": the `developer_task` and `assignment`
  items, newest first, at most 10, each with the date it was added. The block
  makes no network call, and the thread does not tie a status line to an item,
  so the block shows no job status. A job reports in the thread as an event, and
  the last rows of the thread show it;
- when the session has no history of its own (the first turn, a start after
  the context cap, a reset, or a restart that could not resume), the last 10
  rows of the thread, cut short. Each row of a shared workspace names the
  person who wrote it.

Because the block is always in context, a reset loses no project state.
`GET /v1/workspaces/<slug>/block` returns the block as text.

RAG skips the documents and the wiki pages that are items of the workspace,
because the block holds them already. It keeps everything else.

## The context cap

When the input of a workspace turn is over `workspaces.context_cap` (default
150000 tokens), core forgets the SDK session after the turn. The input is the
size of the last model call of the turn.

Before the old session ends, core asks it for a handoff, after the reply has
reached the person. The handoff names the open questions in the words Joshua
used, what waits on the person, what waits on Joshua, the decisions with
their reasons, the running work, and the last request. Core keeps it on the
conversation. The handoff is not a row of the thread.

The next turn waits for the handoff, then starts new, from the block, the
handoff, and the last 10 rows. A row of the person is shown whole up to 2000
characters, and a row of Joshua is cut at 300. A line tells the session to use
`search_history` or `read_history` for the exact words of an earlier message.
When the handoff fails, the session starts from the block and the rows.

Every other conversation has the same cap in `core.context_cap`. See
[config.md](config.md#context-cap). A workspace keeps its own setting, so each can
change on its own.

The nightly rollover skips workspace conversations. The nightly reflection
still reads them.

## Tools

The `workspace` builtin server has these tools.

| Tool | Where | Use |
|---|---|---|
| `add_to_workspace(kind, ref, title, url?, workspace?)` | every chat | Add an item. In a workspace, `workspace` defaults to this one. In a normal chat it is required: a slug or a title of a workspace the person reaches. An item added from another chat shows as new in the block. |
| `update_workspace_notes(notes)` | workspace | Replace the notes. Keep them short, under 4000 characters. |
| `send_to_normal_chat(text)` | workspace | Send `text` and a link to the workspace to the main chat of the person who wrote the turn. A turn with no person, a job event or a scheduled task, sends to the owner. One recipient, never the whole workspace. |
| `notify_user(person, text, url?)` | shared workspace | Reach one person of this workspace: a notification on their devices, or a line and a link in their normal chat when they have none. `url` is the path the tap opens, this thread by default. A person who is not in the workspace is refused. See [Notifying one person](#notifying-one-person). |

A normal chat (a direct chat that is not a workspace) has four more tools.
`workspace` takes a slug or a title, matched with no regard to case among the
workspaces the person reaches, their own and the shared ones they are in. A
name that matches none gets the slugs of the active
workspaces in the answer, and a title that two workspaces share is refused with
their slugs.

| Tool | Use |
|---|---|
| `send_to_workspace(workspace, text, items?)` | Write `text` into the thread as a row from normal chat, and add the items (`kind`, `ref`, `title`, `url`) as new. No turn runs. The block shows both on the next turn of the workspace, once. Refused for an archived workspace. |
| `tell_workspace(workspace, text)` | Run one turn in the thread with `text`, as an event turn that starts "[From normal chat]". The tool returns at once. The reply stays in the thread, and core delivers nothing. Refused unless the workspace is active. |
| `list_workspaces()` | The title, slug, status, and goal of each workspace. |
| `workspace_status(workspace)` | The workspace block as text. It never shows the thread, and it marks nothing seen. |

A group chat gets no `workspace` tools, because other people read it. In a
direct chat, a name that matches no workspace gets the slugs of the person's
active workspaces in the error.

An item `url` must be an `http` or `https` address, or a path that starts with a
single `/`. The routes answer 422 and the tool refuses any other value.

The main chat is the most recently active direct conversation that is not a
workspace, a terminal or a voice thread. Core sends to its last channel. The link is
`<workspaces.viewer_url>/chat/w/<owner>/<slug>`, and `shared` stands for the
owner of a shared workspace. With no `viewer_url`, the message has the path
`/chat/w/<owner>/<slug>`.

## Notifying one person

In a shared workspace, `notify_user(person, text, url?)` reaches one person who
is not reading the thread. It goes to the push subscriptions of that person
(see [notifications.md](notifications.md)), with the title of the workspace,
`text` as one line of at most 160 characters, the address `url` names, and
the slug of the workspace as the tag, so the notifications of one workspace
collapse into the newest one. A person with no subscription gets a line and a
link in their normal chat instead. A personal workspace does not get the tool:
nobody else writes there.

**The url is where the tap lands**, so a notification opens the thing to read:
a document (`/documents/<owner>/<slug>`), a wiki page (`/wiki/...`), or the
thread of the workspace, which is what a call with no `url` opens. It is a path
inside Joshua and nothing else: an address of another site is refused, as the
service worker refuses one on a tap.

**When Joshua notifies.** The prompt (`workspace_shared.md`) says to notify a
person when a question in the thread is addressed to them, a decision waits on
them, Joshua wrote a document they need, it added an item they asked for,
somebody asked it to tell them, somebody asks it to check something with them
("see if this works for Jake"), or a plan or a document of the workspace is
settled and waits on them. The last two carry the `url` of the thing to read.
More conversation on a subject they already know about notifies nobody.

**Three rules are the tool's own**, so no prompt has to hold them:

- it never notifies the person who wrote the turn;
- it never notifies a person whose read mark is the newest row of the thread,
  because the thread is in front of them;
- one person gets one notification of one workspace in ten minutes. A line
  inside that time folds into the notification they already have, which opens
  where that one pointed, so nothing is sent twice and nothing is lost.

The mode of the person comes first: `off` sends nothing, `decide` leaves the
rules above, and `all` asks for a notification on every turn another person
writes. Each person sets their own mode on the workspace page, and the block
shows every mode on its "Notify each person:" line. See
[notifications.md](notifications.md#when-joshua-may-reach-you-in-a-workspace).

Each call writes one decision line in core's log: the person, the workspace,
whether a notification went, and the guard that stopped it (`writer`,
`reading`, `fold`, `mode:off`, or `unreached` for a person a send reached
neither way). A notification that did not arrive has a reason, and that is
where it is read.

## Normal chat knows its workspaces

A direct chat of a member that is not a workspace gets two things on each turn.
A person with no active workspace gets neither. A guest, a group chat, and a
workspace thread get neither.

- **The list.** "Your workspaces:" and one line for each active workspace the
  person reaches, their own and the shared ones they are in, at
  most 10: the title (60 characters) and the goal (90). A last line says to
  record what belongs to a workspace with `send_to_workspace` and to say so in
  one line.
- **The hint.** Core indexes one record for each active workspace in the
  knowledge index, as the source `workspaces`. The record holds the title, the
  goal, the notes, and the item titles. The indexing cycle writes it again when
  it changes (`memory.index_interval_s`). There is one record for each person
  of the workspace, under the uri `workspaces/<id>/<person>`, so each of them
  finds it. A record is never shown as text: recall and the RAG note never
  return it. When the
  message matches a record with a similarity of `workspaces.hint_sim` or more
  (default 0.70, just under `memory.inject.full_sim`), the turn gets one line:
  "May relate to workspace *<title>*."

The rows that `send_to_workspace` writes are rows of the thread of the
workspace with `direction` `inject` and `meta` `{"source": "normal_chat",
"seen": false}`. Core sets `seen` to true after the workspace turn that showed
them. There is no schema change.

## Routes

The viewer calls `/v1/workspaces` on core. The caller names the person with the
`person` query parameter, and core trusts it, as for a search of documents.
Every route works on the workspaces that person reaches, their own and the
shared ones they are in. Any other workspace answers 404. The callers are
`workspaces.allowed_callers` (default `viewer`, `gateway` and `assignments`).
An empty list turns the routes off (403).

Every `{slug}` below is a ref: a slug, an id, or two segments
(`shared/<slug>`, `<owner>/<slug>`), in the path or URL-encoded.

| Route | Use |
|---|---|
| `GET /v1/workspaces?status=` | List the workspaces the person reaches. |
| `POST /v1/workspaces` | Create one: `title`, `goal`, `model`, `servers`, `notify`, `members`. `members` makes it shared. A taken shared slug answers 409 with the slug. |
| `GET`, `PATCH /v1/workspaces/{slug}` | Read or change: `title`, `goal`, `notes`, `servers`, `model`, `notify`, `status`. A `status` of `archived` closes it, which frees its name for a new workspace and keeps its own slug. No route changes the people, and no route changes a slug. |
| `GET`, `POST /v1/workspaces/{slug}/items` | List or add items. The assignments container adds the outputs of a run that started in a workspace here, as the person of the run. |
| `PATCH`, `DELETE /v1/workspaces/{slug}/items/{id}` | Change `pinned` or `seen`, or remove an item. |
| `GET /v1/workspaces/{slug}/block` | The workspace block as text. |
| `POST /v1/workspaces/{slug}/read` | Mark the thread read up to `turn_id`, for that person alone. |
| `GET`, `POST /v1/workspaces/{slug}/notify` | Read or write the notify mode of that person in that workspace: `off`, `decide`, or `all`. |
| `POST /v1/workspaces/by-id/{id}/items` | Add an item for a tool call. The gateway calls it (see Items from tools). It takes no `person`. |

`GET /v1/workspaces` gives each workspace an `unread` count: the rows of its
thread after the `last_read_turn_id` of that person that somebody else wrote,
Joshua or another person of the thread, and every such row when they have read
nothing of it. A row of their own never counts, so sending a message and
leaving the workspace at once leaves the count at zero.
`POST /v1/workspaces/{slug}/read` takes
`{"turn_id"}`, the id of a transcript row (0 or more), and answers
`{"conversation", "last_read_turn_id", "last_read_at", "unread"}`. The mark is
one row of `conversation_reads`, for that person and that conversation, and it
never moves back. In a shared workspace every person holds a mark of their own,
so one of them reading the thread leaves the counts of the others whole.

`GET /v1/workspaces/{slug}/notify` answers `{"mode", "default"}`: the mode of
that person and the default of the workspace, which a person with no row of
their own has. `POST` takes `{"mode"}`, one of `off`, `decide` and `all` (422
otherwise), writes the row, and answers the same two keys. The mode is that
person's alone: no route sets the mode of another person.

`servers` must be servers that every person of the workspace may use (422
otherwise). Every name in `members` must be a member of the roster, and never
the reserved id `shared` (422 otherwise). `model` must be in
`workspaces.models` when that list is not empty, and can be any text when it
is empty.

A chat turn in a workspace goes through the channels terminal routes.
`POST /v1/cli/turns/stream` and `GET /v1/cli/transcript` take an optional
`workspace` (a ref). Core looks the workspace up for the person. The viewer
sends the id there, as it does everywhere else; the ref form stays for the
terminal and anything else that names a workspace the way a person does.

`GET /v1/usage?group=conversation&person=<id>` gives the totals of each conversation, with
the slug and the title of the workspace when the conversation belongs to one.
It shows what a workspace costs.

## Items from tools

Joshua adds items without being asked, from the tool calls of a workspace
thread. The gateway does this. It adds no container.

A session of a workspace sends `X-Joshua-Workspace: <workspace id>` to the
gateway on every MCP server, and the gateway trusts the header only from
`gateway.person_header_callers`, as it does the conversation header. The header
is fixed for the session. A session of a shared workspace also sends
`X-Joshua-Workspace-People`: the owner and the members, comma separated. The
gateway refuses an id that is not in `people` (400), and sends the list
upstream as `_meta["joshua/workspace_people"]`.

After a tool call from a workspace succeeds, the gateway records items. It does
this in the background and never fails the call: a failed post is a log line.

- **The per-tool map.** The map is in code
  (`gateway/joshua_gateway/workspace_items.py`). The key is the server name and
  the tool name, as the gateway sees them.

  | Server | Tool | Item |
  |---|---|---|
  | `documents` | `write_document` | `document`, or `artifact` for an `.html` file. `ref` is the path, and `url` is `/documents/<owner>/<slug>`. |
  | `joshua` or `wiki` | `write_page` | `wiki`. `ref` is the path, and `url` is `/wiki/<path>`. |
  | `tasks` | `create_project` | `tasks_project`. `ref` is the slug. |
  | `developer` | `develop`, `rework` | `developer_task`. `ref` is the `task_id`. |
  | `assignments` | `start_assignment` | `assignment`. `ref` is the id, and `url` is `/assignments/<id>`. |

  A refusal (`status: rejected`), an error, and a tool that is not in the map
  add nothing from the map.
- **Links.** Every MCP `resource_link` block in the result of any tool becomes a
  `link` item: `ref` and `url` are the `uri`, and `title` is the `name`. The
  `url` is kept only when it is `http(s)` or a path that starts with `/`. A link
  that an item of the map already covers is not added again.

The gateway posts to core at `POST /v1/workspaces/by-id/<id>/items`, with its own
fleet token. The route takes the id of the workspace and no person. The caller is
trusted: it is in `workspaces.allowed_callers`.
The body is `kind`, `ref`, `title`, `url`, and `added_by` (`tool:<server>`). An
item with the same kind and ref updates the row, and an empty `title` keeps the
title that the item has.

**Items from an assignment.** A run of assignments that started in a workspace
thread keeps the id of that workspace in its spec, and each output it records
with `record_output` becomes an item of that workspace at that moment
(`assignments/joshua_assignments/workspace_items.py`). An output that names a
document is a `document` item, an `.html` one an `artifact`, and `url` is the
page of the document, so the item opens in the chat. An output whose ref is an
`http(s)` address keeps it as the `url`, and an output of a kind core does not
know is a `link`. The container posts to
`POST /v1/workspaces/<workspace>/items` with `person`, the person the run
belongs to, so a run of a person who is not one of the people of that
workspace adds nothing (404). An item with the same kind and ref updates the
one row it has. A refused or failed post is a log line and never fails the
tool call. `assignments` must be in `workspaces.allowed_callers`.

The gateway needs `JOSHUA_TOKEN_GATEWAY`, which it holds already, and the
address of core. `CORE_URL` names it. Without it, the address is
`http://joshua-core:8000` on Kubernetes and `http://core:8000` on docker
compose. Without the token, the gateway records nothing and says so once.

## The stamp and the return

Each tool call from a workspace thread reaches the upstream server with
`params._meta["joshua/workspace"] = "<workspace id>"`. A call from any other chat
has no such key. The gateway sets it, and nothing else can: the header that
carries the id is trusted only from `core` and `assignments`.

A job that a workspace started goes back to the workspace. The addon that runs
the job keeps the id with the job, and when the job ends or asks a question, it
sends the event with the id:

```
POST /v1/events
{"workspace": "<id>", "text": "...", "verbatim": false}
```

- `workspace` is the id from the stamp. Without it, the event goes to normal chat
  as it did before.
- Channels accepts `workspace` with `text` and no `destination`. A `destination`
  that comes with it is not used. An id that is not a UUID gets 400
  `invalid_workspace`.
- Core runs the text as a turn in the thread of the workspace, with the owner as
  the person. An unknown id gets 404 `unknown_workspace`. The turn starts with the
  line for a scheduled task in a workspace, and the reply stays in the transcript.
  The agent uses `send_to_normal_chat` when `notify` allows it or the person
  asked.
- With `"verbatim": true`, core records the text in the thread and runs no model.
  Channels delivers nothing to an adapter.

## For addon authors

1. **Read the stamp.** In a FastMCP tool, `ctx.request_context.meta` holds
   `_meta`. The workspace id is `meta.get("joshua/workspace")`. The value is
   `None` outside a workspace, and `ctx.request_context.meta` itself can be
   `None`.

   ```python
   def origin_of(ctx) -> str | None:
       meta = ctx.request_context.meta
       value = meta.get("joshua/workspace") if meta else None
       return value if isinstance(value, str) and value else None
   ```

2. **Keep it with a long job.** Store the id with the job. A job that was not
   started from a workspace has none.
3. **Send it back.** Add `"workspace": <id>` to every completion, failure, and
   question event that you post to channels `/v1/events`, when the job has one.
4. **Return links.** Add an MCP `resource_link` block to the result of a tool for
   anything a person may want to open (a pull request, a report, a cart):
   `{"type": "resource_link", "uri": ..., "name": ..., "description"?, "mimeType"?}`.
   Use an `http(s)` address, or a path that starts with `/` for a page of the
   viewer. The gateway makes a `link` item of it for a call from a workspace.
   You can add the block to every result: it costs nothing outside a workspace.

The gateway test `gateway/tests/test_workspace_stamp.py` runs a real FastMCP
server behind the gateway and reads the stamp the way step 1 does.

## The viewer

The chat screen of the viewer is the workspace UI. It works the same on docker
compose and on Kubernetes, and it adds no container.

- `/chat/<person>` is the normal chat. `/chat/w/<owner>/<slug>` is a
  workspace, and `shared` stands for the owner of a shared one, so everyone in
  it opens the same address. Every link the screen builds — the rail, the
  header, a new workspace — is `/chat/w/<id>`, the permanent address: a closed
  workspace frees its name, so a later workspace may hold its slug, and the id
  never moves. A link of one segment that is not an id still works: it finds
  the workspace and sends the browser to `/chat/w/<id>`. The id `w` is
  reserved, so no person can have it.
- The left rail shows Chat, the active workspaces, the paused workspaces (folded),
  and "+ New workspace". On a phone it is a drawer. A shared workspace carries
  a small two-person icon beside its name, in the rail and in the header of the
  workspace; the icon is the only mark, and no row says "Shared". A workspace
  whose thread holds rows the person has not read shows the count after the
  title. The chat marks the thread read while its newest row is on screen, and
  not while the tab is hidden, and the rail asks for the counts again after
  each mark.
- The new workspace dialog asks for a title, a goal (optional), a model, and
  the servers. "Shared" offers the other people of the roster as checkboxes
  and shows the address the workspace will take; Create waits until at least
  one person is picked. A title whose shared slug is taken says so. The dialog
  creates the workspace and opens it.
- A workspace view has the chat in the centre, an item panel at the right, and
  a header with the title and the goal. The edit drawer changes the goal, the
  notes, the servers, the model, `notify`, and the status (pause, resume,
  archive). In a shared workspace it also holds "When Joshua may reach you
  here", the notify mode of the person who is signed in, which saves as it
  changes. The item panel groups items by kind, with one row for each thing
  the items point at: the same document recorded twice, as two kinds or by two
  writers, keeps the row that has a title and opens it, and a row with no
  title of its own asks the viewer for the title of its document, so no row is
  labelled with a bare path. A document or an artifact opens
  in the document panel. Another item opens in a new tab, but only when its
  address is http(s) or a path that starts with `/`. Any other address is shown
  as text. Each item can be pinned or removed, and a form adds a link. The panel
  loads again after each finished turn and when the tab gets focus.

### Viewer routes

Every route is under `/api/v1/workspaces`, behind the sign-in. Each one proxies
the route of the same name on core, with `CORE_URL` and `JOSHUA_TOKEN_VIEWER`,
and names the signed-in person. A guest gets 403. Every write checks the
`Origin` header. Core's status and body come back as they are.

| Route | Core route |
|---|---|
| `GET`, `POST /api/v1/workspaces` | `GET`, `POST /v1/workspaces` |
| `GET /api/v1/workspaces/options` | `GET /v1/workspaces/options` |
| `GET`, `PATCH /api/v1/workspaces/{slug}` | `GET`, `PATCH /v1/workspaces/{slug}` |
| `GET`, `POST /api/v1/workspaces/{slug}/items` | `GET`, `POST /v1/workspaces/{slug}/items` |
| `PATCH`, `DELETE /api/v1/workspaces/{slug}/items/{id}` | the same on core |
| `POST /api/v1/workspaces/{slug}/read` | `POST /v1/workspaces/{slug}/read` |
| `GET`, `POST /api/v1/workspaces/{slug}/notify` | `GET`, `POST /v1/workspaces/{slug}/notify` |

`GET /v1/workspaces/options` returns `servers` (the servers that the person may
use, each with a `name` and a one-line `description`), `models` (the list
`workspaces.models`, empty when any model is allowed), and `people` (every
member of the roster but the caller, each with an `id` and a `name`, to share a
workspace with). The description is the optional `description` of the server in
`mcp:`. Without it, the name is used. The slug `options` is reserved.

The chat routes take `workspace` (a ref, two segments for a workspace on
screen): `POST
/api/v1/chat/<person>/turns` in the JSON body or the form, and `GET
/api/v1/chat/<person>/history` in the query. The documents list takes
`tag`: `GET /api/v1/documents?tag=ws:<slug>` keeps the documents with that tag.

## The main thread

Normal chat is one thread. A viewer turn, or a turn from the terminal, with no
`workspace` runs in the main conversation of the person: the most recently
active conversation of the person that is a direct chat and is not a
`workspace`, `cli` or `voice` channel. The fallback is the `cli:<person>`
conversation. Channels sends `thread: "main"` in the turn, and the transcript
route reads the same conversation.

The viewer only pulls. A viewer turn does not change `last_channel_id` of the
main conversation, so a reminder or the reply to an event still goes to the
phone. Core runs the turns of one conversation one at a time, whatever the
channel. Old `cli:<person>` history stays in the database and is not shown.

A session is built for the channel of its first turn. When a turn arrives from
another channel, it starts with a line that names its channel, so the guidance
for that channel applies (Markdown in the viewer, plain text on iMessage). The
line is added when the channel changes, and on each turn off the channel of the
session.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `workspaces.allowed_callers` | `viewer`, `gateway`, `assignments` | Fleet identities that may call `/v1/workspaces`. |
| `workspaces.models` | empty | The models a workspace may pick. Empty accepts any text. |
| `workspaces.context_cap` | `150000` | Input tokens over which a workspace turn ends its session. |
| `workspaces.max_turns` | `50` | The least number of agent turns that a workspace turn gets. |
| `workspaces.tool_search_max_servers` | `3` | Servers up to which a workspace runs with tool search off. |
| `workspaces.viewer_url` | none | The public address of the viewer, for the link in `send_to_normal_chat`. |
| `workspaces.hint_sim` | `0.70` | The similarity over which normal chat gets the line "May relate to workspace X". |
| `workspaces.tasks_url` | none | The address of the tasks addon, for "Next steps" in the block. Without it, the section is off. Kubernetes: `http://tasks:8000`. The docker compose file has no tasks service, so it sets nothing. |

Each `mcp:` server can have a `description`, one line (200 characters at most)
that the new workspace dialog shows beside the name.

See [config.md](config.md#workspaces).
