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

The nightly rollover skips workspace conversations. The nightly reflection
still reads them.

## Tools

The `workspace` builtin server has these tools.

| Tool | Where | Use |
|---|---|---|
| `add_to_workspace(kind, ref, title, url?, workspace?)` | every chat | Add an item. In a workspace, `workspace` defaults to this one. In a normal chat it is required: a slug or a title of one of the person's workspaces. An item added from another chat shows as new in the block. |
| `update_workspace_notes(notes)` | workspace | Replace the notes. Keep them short, under 4000 characters. |
| `send_to_normal_chat(text)` | workspace | Send `text` and a link to the workspace to the person's main chat. |

The main chat is the most recently active direct conversation that is not a
workspace and not a voice thread. Core sends to its last channel. The link is
`<workspaces.viewer_url>/chat/w/<slug>`. With no `viewer_url`, the message has
the path `/chat/w/<slug>`.

## Routes

The viewer calls `/v1/workspaces` on core. The caller names the person with the
`person` query parameter, and core trusts it, as for a search of documents.
Every route works on the workspaces of that person. A workspace of another
person answers 404. The callers are `workspaces.allowed_callers` (default
`viewer`). An empty list turns the routes off (403).

| Route | Use |
|---|---|
| `GET /v1/workspaces?status=` | List the workspaces. |
| `POST /v1/workspaces` | Create one: `title`, `goal`, `model`, `servers`, `notify`. |
| `GET`, `PATCH /v1/workspaces/{slug}` | Read or change: `title`, `goal`, `notes`, `servers`, `model`, `notify`, `status`. `{slug}` can also be the id. |
| `GET`, `POST /v1/workspaces/{slug}/items` | List or add items. |
| `PATCH`, `DELETE /v1/workspaces/{slug}/items/{id}` | Change `pinned` or `seen`, or remove an item. |
| `GET /v1/workspaces/{slug}/block` | The workspace block as text. |

`servers` must be servers that the person may use (422 otherwise). `model`
must be in `workspaces.models` when that list is not empty, and can be any text
when it is empty.

A chat turn in a workspace goes through the channels terminal routes.
`POST /v1/cli/turns/stream` and `GET /v1/cli/transcript` take an optional
`workspace` (the slug or the id). Core looks the workspace up for the person.

`GET /v1/usage?group=conversation` gives the totals of each conversation, with
the slug and the title of the workspace when the conversation belongs to one.
It shows what a workspace costs.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `workspaces.allowed_callers` | `viewer` | Fleet identities that may call `/v1/workspaces`. |
| `workspaces.models` | empty | The models a workspace may pick. Empty accepts any text. |
| `workspaces.context_cap` | `150000` | Input tokens over which a workspace turn ends its session. |
| `workspaces.tool_search_max_servers` | `3` | Servers up to which a workspace runs with tool search off. |
| `workspaces.viewer_url` | none | The public address of the viewer, for the link in `send_to_normal_chat`. |

See [config.md](config.md#workspaces).
