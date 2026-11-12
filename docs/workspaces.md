# Workspaces

A workspace is a place for one large piece of work: code, a meal plan, a trip,
a house project. It has its own chat thread. It holds the MCP servers that the
person picked, a model, a goal, some notes, and links to what was built or
collected for it. Normal chat stays small, because project talk goes into the
workspaces.

A workspace chat is the full Joshua. It has the same prompt, the same builtin
tools, the same memory, and the same RAG from everything outside the
workspace. Only these things differ: the thread, the model, the servers, one
prompt file (`workspace.md`), and a small workspace block in each message.

Nothing changes until a workspace exists. The tables are empty and no turn
reads them.

## What a workspace holds

| Field | Meaning |
|---|---|
| `slug`, `title` | The slug comes from the title. It is unique for each owner. |
| `owner` | One person. Another person sees the workspace as not found. |
| `goal` | A short paragraph. |
| `notes` | Standing facts and rules. The agent keeps them with `update_workspace_notes`. |
| `servers` | The MCP servers that the workspace uses. Only servers that the person may use. |
| `model` | The model of the workspace. Empty keeps the model of the chat job. |
| `notify` | `none` (default), `questions`, or `questions+finishes`. |
| `status` | `active`, `paused`, or `archived`. |

An item is a link: a `kind` (`document`, `artifact`, `plan`, `wiki`,
`tasks_project`, `task`, `assignment`, `developer_task`, `pr`, `link`, or
`file`), a `ref`, a `title`, and an optional `url`. It also has `added_by`,
`pinned`, and `seen`. Adding an item with the same kind and ref again updates
the row.

## The thread

A new workspace gets the channel `workspace:<id>` (type `workspace`) and one
conversation for the owner. A turn on this channel runs only when the
workspace exists and its owner is the person of the turn. Core never creates a
`workspace:` channel from a turn.

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

## What a session gets

- **Model.** The model of the workspace replaces the model of the chat job.
  The server and the key of the job stay.
- **Servers.** The servers that the workspace picked, among the servers that
  the person may use. The builtin tools stay as in a normal chat, with the
  `workspace` tools added.
- **Prompt.** The `dm` profile and `workspace.md`.
- **Tool search.** Off when the workspace picks `workspaces.tool_search_max_servers`
  servers or fewer (default 3). On when it picks more. Core does not know how
  many tools a server has, so the rule counts servers.
- **Changes.** A change of `servers` or `model` applies on the next turn. Core
  builds a new session and resumes the same conversation.

## The workspace block

Each turn of a workspace starts with the workspace block. It holds:

- the title, the goal, the `notify` setting, and the notes;
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
  rows of the thread, cut short.

Because the block is always in context, a reset loses no project state.
`GET /v1/workspaces/<slug>/block` returns the block as text.

RAG skips the documents and the wiki pages that are items of the workspace,
because the block holds them already. It keeps everything else.

## The context cap

When the input of a workspace turn is over `workspaces.context_cap` (default
150000 tokens), core forgets the SDK session after the turn. The next turn
starts new, from the block and the last 10 rows. The input is the size of the
last model call of the turn.

Every other conversation has the same cap in `core.context_cap`. See
[config.md](config.md#core). A workspace keeps its own setting, so each can
change on its own.

The nightly rollover skips workspace conversations. The nightly reflection
still reads them.

## Tools

The `workspace` builtin server has these tools.

| Tool | Where | Use |
|---|---|---|
| `add_to_workspace(kind, ref, title, url?, workspace?)` | every chat | Add an item. In a workspace, `workspace` defaults to this one. In a normal chat it is required: a slug or a title of one of the person's workspaces. An item added from another chat shows as new in the block. |
| `update_workspace_notes(notes)` | workspace | Replace the notes. Keep them short, under 4000 characters. |
| `send_to_normal_chat(text)` | workspace | Send `text` and a link to the workspace to the person's main chat. |

A normal chat (a direct chat that is not a workspace) has four more tools.
`workspace` takes a slug or a title, matched with no regard to case among the
workspaces of the person. A name that matches none gets the slugs of the active
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
`<workspaces.viewer_url>/chat/w/<slug>`. With no `viewer_url`, the message has
the path `/chat/w/<slug>`.

## Normal chat knows its workspaces

A direct chat of a member that is not a workspace gets two things on each turn.
A person with no active workspace gets neither. A guest, a group chat, and a
workspace thread get neither.

- **The list.** "Your workspaces:" and one line for each active workspace, at
  most 10: the title (60 characters) and the goal (90). A last line says to
  record what belongs to a workspace with `send_to_workspace` and to say so in
  one line.
- **The hint.** Core indexes one record for each active workspace in the
  knowledge index, as the source `workspaces`. The record holds the title, the
  goal, the notes, and the item titles. The indexing cycle writes it again when
  it changes (`memory.index_interval_s`). The record is for the owner only, and
  it is never shown as text: recall and the RAG note never return it. When the
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
Every route works on the workspaces of that person. A workspace of another
person answers 404. The callers are `workspaces.allowed_callers` (default
`viewer` and `gateway`). An empty list turns the routes off (403).

| Route | Use |
|---|---|
| `GET /v1/workspaces?status=` | List the workspaces. |
| `POST /v1/workspaces` | Create one: `title`, `goal`, `model`, `servers`, `notify`. |
| `GET`, `PATCH /v1/workspaces/{slug}` | Read or change: `title`, `goal`, `notes`, `servers`, `model`, `notify`, `status`. `{slug}` can also be the id. |
| `GET`, `POST /v1/workspaces/{slug}/items` | List or add items. |
| `PATCH`, `DELETE /v1/workspaces/{slug}/items/{id}` | Change `pinned` or `seen`, or remove an item. |
| `GET /v1/workspaces/{slug}/block` | The workspace block as text. |
| `POST /v1/workspaces/by-id/{id}/items` | Add an item for a tool call. The gateway calls it (see Items from tools). It takes no `person`. |

`servers` must be servers that the person may use (422 otherwise). `model`
must be in `workspaces.models` when that list is not empty, and can be any text
when it is empty.

A chat turn in a workspace goes through the channels terminal routes.
`POST /v1/cli/turns/stream` and `GET /v1/cli/transcript` take an optional
`workspace` (the slug or the id). Core looks the workspace up for the person.

`GET /v1/usage?group=conversation&person=<id>` gives the totals of each conversation, with
the slug and the title of the workspace when the conversation belongs to one.
It shows what a workspace costs.

## Items from tools

Joshua adds items without being asked, from the tool calls of a workspace
thread. The gateway does this. It adds no container.

A session of a workspace sends `X-Joshua-Workspace: <workspace id>` to the
gateway on every MCP server, and the gateway trusts the header only from
`gateway.person_header_callers`, as it does the conversation header. The header
is fixed for the session.

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
trusted: it is in `workspaces.allowed_callers` (default `viewer` and `gateway`).
The body is `kind`, `ref`, `title`, `url`, and `added_by` (`tool:<server>`). An
item with the same kind and ref updates the row, and an empty `title` keeps the
title that the item has.

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

- `/chat/<person>` is the normal chat. `/chat/w/<slug>` is a workspace of the
  person who signed in. The id `w` is reserved, so no person can have it.
- The left rail shows Chat, the active workspaces, the paused workspaces (folded),
  and "+ New workspace". On a phone it is a drawer.
- The new workspace dialog asks for a title, a goal (optional), a model, and the
  servers. It creates the workspace and opens it.
- A workspace view has the chat in the centre, an item panel at the right, and
  a header with the title and the goal. The edit drawer changes the goal, the
  notes, the servers, the model, `notify`, and the status (pause, resume,
  archive). The item panel groups items by kind. A document or an artifact opens
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

`GET /v1/workspaces/options` returns `servers` (the servers that the person may
use, each with a `name` and a one-line `description`) and `models` (the list
`workspaces.models`, empty when any model is allowed). The description is the
optional `description` of the server in `mcp:`. Without it, the name is used.
The slug `options` is reserved.

The chat routes take `workspace` (the slug or the id): `POST
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
| `workspaces.allowed_callers` | `viewer`, `gateway` | Fleet identities that may call `/v1/workspaces`. |
| `workspaces.models` | empty | The models a workspace may pick. Empty accepts any text. |
| `workspaces.context_cap` | `150000` | Input tokens over which a workspace turn ends its session. |
| `workspaces.tool_search_max_servers` | `3` | Servers up to which a workspace runs with tool search off. |
| `workspaces.viewer_url` | none | The public address of the viewer, for the link in `send_to_normal_chat`. |
| `workspaces.hint_sim` | `0.70` | The similarity over which normal chat gets the line "May relate to workspace X". |
| `workspaces.tasks_url` | none | The address of the tasks addon, for "Next steps" in the block. Without it, the section is off. Kubernetes: `http://tasks:8000` in this cluster. The docker compose file has no tasks service, so it sets nothing. |

Each `mcp:` server can have a `description`, one line (200 characters at most)
that the new workspace dialog shows beside the name.

See [config.md](config.md#workspaces).
