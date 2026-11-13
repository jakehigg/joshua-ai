# Changelog

Each entry names what changed for the person who runs Joshua. The releases,
with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-ai/releases>.

## Unreleased

- **The viewer has a sign-in page.** `viewer.auth.method` sets one method for
  the instance. `anonymous` is the default. It asks for nothing, and
  `viewer.anonymous` decides who a request is. `pin` shows the people who
  have a secret and asks the person for a PIN. `password` asks for a person
  id and a password. A PIN and a password are the same thing, one secret that
  can be any string. "Remember this device" keeps the session cookie for 30
  days. Without it, the browser drops the cookie when it closes. The files of
  the app load with no cookie, because the sign-in form is a page of the app.
  Every JSON route still needs the sign-in. See [auth.md](auth.md).
- **Core keeps the secrets.** Core stores one salted hash for each person in
  the new `auth_secret` table. The viewer calls six new routes of core:
  `POST /auth/verify`, `POST /auth/secret`, `POST /auth/secret/admin`,
  `DELETE /auth/secret/<id>`, `GET /auth/people`, and `GET /auth/people/<id>`.
  The viewer holds no password. HTTP basic is gone, and no answer carries
  `WWW-Authenticate`. A change of a secret, a reset, and a removal sign out
  every other browser of that person within a minute.
- **The owner keeps the roster.** `owner` is a new role. An owner is a member
  everywhere, including every MCP server behind the gateway. The owner also
  gets "Admin" in the navigation. Under Admin → Users the owner adds a person
  with a secret, sets a secret, promotes or demotes a person, and removes a
  person. Core serves three new roster routes: `POST /admin/people`,
  `POST /admin/people/<id>/role`, and `POST /admin/people/<id>/remove`.
  `make validate` warns when no person is the owner, and the file is still
  valid.
- **The roster always keeps an owner.** Core refuses, with `409`, a change
  that demotes or removes the only owner. The Users page turns off `Demote`
  and `Remove` on that row. Only three things make a person the owner:
  `role: owner` in `joshua.yaml`, another owner, or an operator with
  `python -m joshua_core people role --id <id> --role owner`.
- **The viewer reaches the roster routes as an auth caller.** Core admits the
  callers of `AUTH_CALLERS` (default `viewer`) on its four roster routes. The
  viewer gets no other admin route, and it serves the roster to the owner
  alone. You no longer add `viewer` to `ADMIN_CALLERS`.
- **A person needs no handle.** `Add a person`, `POST /admin/people`, and
  `people add` take the handle as optional, so you can add a person who uses
  only the viewer and the terminal.
- **Profile.** "Your page" is now "Profile", at `/me`. There a person changes
  their own PIN or password, their instructions to Joshua, and their journal
  consent. Core serves `GET` and `POST /people/<id>/profile`, and the viewer
  serves `GET` and `POST /api/v1/profile`. Core writes the instructions to
  `/data/people/<id>/prompt.md` and puts the path of that file in the
  `prompt` of the person. A person cannot change their own name, role, or
  handles.
- **The people file overlays fields.** An entry of `/data/people.yaml`
  changes only the keys that it holds on the same person in `joshua.yaml`.
  The handles of the two are the union. Before, the entry replaced the whole
  person, so one role change hid every later change of that person in
  `joshua.yaml`. A `role: removed` entry still removes the person.
- **A change of a person applies on the next turn.** Core reads the `prompt`
  and the `journal` of a person from the current config on each turn. Before,
  core read them one time, at start.
- **A text field shows one focus ring.** The wrapper of a field draws the
  ring, and the control inside it draws none.
- **`GET /readyz` of the viewer reports `auth`.** The field holds the sign-in
  method. The `users` count of the retired `viewer.users` is gone.
- **`make up` starts the viewer and the assignments container** when
  `joshua.yaml` sets `viewer.enabled: true` or `assignments.enabled: true`,
  and `make down` stops them. You do not name a compose profile.
- **`make init-env` writes `JOSHUA_VIEWER_SESSION_SECRET`**, so a restart of
  the viewer keeps everybody signed in.
- **`make validate` names only the channels that start.** It runs a new
  one-shot `validate` service with the channel credentials and no network, so
  a channel with an empty credential no longer shows as enabled.
  `make validate-dev` runs the same check on your own build.
- **`make restart` restarts every container that runs.** It keeps each
  image, so it works for a release and for your own build.
- **Upgrade notes for the sign-in.**
  - Give one person `role: owner` in `joshua.yaml`.
  - In docker compose, run `make init-env` again. It adds
    `JOSHUA_VIEWER_SESSION_SECRET` to `.env` and changes nothing else. In the
    chart, add `JOSHUA_VIEWER_SESSION_SECRET` to the viewer Secret. With no
    such key, the viewer makes a new key at each start, so a restart signs
    everybody out.
  - The first start of core after the upgrade imports the old viewer
    passwords one time. It reads `viewer.users` in `joshua.yaml`, then
    `VIEWER_PASSWORDS` and `VIEWER_PW_<ID>` in the environment of core. It
    writes a hash for each person who has no secret yet, and it overwrites
    nothing. The `auth_import_marker` table records that the import ran, so
    it never runs again. Docker compose gives core `VIEWER_PASSWORDS` from
    `.env`. In the chart, put it in the core Secret for that one start. Then
    delete `viewer.users` and `VIEWER_PASSWORDS`. `make validate` warns while
    either one is there.
  - A person with no secret is on the roster and cannot sign in. On a `pin`
    instance with no secret at all, the sign-in page shows the command that
    sets the first one:
    `python -m joshua_core people auth-reset --id <id> --secret '<secret>'`.
    Run it in the core container. After that, the owner sets the other
    secrets under Admin → Users.
  - The schema adds the tables `auth_secret` and `auth_import_marker`.
- **Workspaces.** A workspace is a chat thread for one large piece of work,
  such as a trip, a house project, or a piece of code. It has a title, a goal,
  a model, the MCP servers that the person picked, notes, and a list of items.
  An item is a document, an artifact, a wiki page, a pull request, an
  assignment, a developer task, a tasks project, a link, or a file. Core runs
  each workspace thread as the full Joshua and adds a workspace block to each
  message. The block holds the goal, the notes, the items, the jobs that the
  workspace started, and the next steps of a linked tasks project. A reply
  stays in the thread and does not go to the phone. The agent sends one
  message to the normal chat with `send_to_normal_chat`. A workspace belongs
  to the member who made it. See [workspaces.md](workspaces.md).
- **Workspace items are added for you.** A workspace session sends
  `X-Joshua-Workspace` to the gateway. The gateway puts
  `_meta["joshua/workspace"]` on each tool call. After a successful call, it
  adds items from a map of known tools and from each MCP `resource_link` in
  the result. It posts them to `POST /v1/workspaces/by-id/<id>/items`. A file
  that the person sends in a workspace thread is also an item. Channels
  `POST /v1/events` takes `workspace`, so an event runs in that thread. With
  `verbatim`, the event is only recorded there. The assignments container
  keeps the workspace of a start and reports to that thread.
- **Normal chat knows the workspaces.** The direct chat of a member lists the
  active workspaces, at most 10. A message that matches a workspace above
  `workspaces.hint_sim` (default 0.70) gets the line "May relate to workspace
  X". The new tools `send_to_workspace`, `tell_workspace`, `list_workspaces`,
  and `workspace_status` move work between the normal chat and a workspace.
- **The chat is a screen of the viewer.** `/chat/<person>` shows the normal
  chat, and `/chat/w/<slug>` shows a workspace. The screen has a rail with the
  workspaces, a dialog for a new workspace, a header with an edit drawer, and
  an items panel. A document, a page, or a photo opens in a panel next to the
  chat. A turn streams as it arrives. A message can carry one photo or one
  PDF, and the composer shows the upload progress. Events show as short lines
  that open to the full text. A silent reply (`[IGNORE]`) never shows. Voice
  is not part of this screen.
- **The chat works on a phone.** The rail and the panels open as sheets, one
  at a time, and a tap outside or Escape closes them. The app menu is a left
  drawer. The composer is one line when empty and grows to five lines. With a
  mouse or a trackpad, Enter sends and Shift+Enter adds a new line. On a touch
  screen, Enter adds a new line and the button sends. The composer stays above
  the on-screen keyboard. On iOS it keeps at least 44px free, and `?kbpad=<n>`
  sets another value. `?debug=vv` shows the viewport numbers on the screen.
- **Focus mode on a desktop.** Each side panel collapses, and focus mode
  collapses all of them. Ctrl+\ or Cmd+\ toggles focus mode. The browser keeps
  the choice.
- **Normal chat is one thread.** A turn from the viewer or the terminal runs in
  the main conversation of the person. That is the most recent direct
  conversation that is not a workspace, `cli`, or voice thread. Reminders and
  event replies still go to the phone. With no such conversation, the `cli:`
  conversation is used. Old `cli:` history stays in the database. A turn on a
  shared thread names its channel, so guidance for one channel does not apply
  to another. The `TurnEvent` has a new optional field, `thread`.
- **A turn survives the client that sent it.** Core runs a streamed turn in a
  task of its own. Channels and the viewer read the stream in their own tasks.
  A closed tab, a locked phone, or a dropped signal no longer stops the turn,
  and the reply goes into the transcript. The stream sends `accepted`,
  `status`, `delta`, `done`, and `error` events.
- **Every device sees a turn in progress.** `GET /v1/transcript` gives
  `pending`, with the state (`queued` or `running`), the turn id, the start
  time, and the text. The viewer shows "Joshua is working" and reloads the
  history until the answer arrives. A page that comes back after a sleep finds
  the turn again. The browser keeps an unsent message and its file, and the
  next visit offers to send them again.
- **Files stay with their message.** The transcript row of a message keeps its
  files in `meta.attachments`. The chat shows a photo as a thumbnail and a PDF
  as a chip, in a new message and in the history.
- **A long chat starts a new session at a size limit.** A turn whose input is
  over `core.context_cap` (default 150000 tokens) ends the SDK session. The
  next turn starts a new session and gets the last 10 rows of the thread. `0`
  turns the limit off. A workspace uses `workspaces.context_cap` (default
  150000).
- **The viewer is one React app.** The gateway renders no page in Python. It
  serves the app from `gateway/frontend` and a JSON API under `/api/v1/` for
  the wiki, the journal, search, usage, links, documents, assignments, the
  chat, and the workspaces. The report showcase and the saved assignments are
  React pages. Search has its own page, and the app bar has no search field.
  `/style.css` and `/ui/<file>` are removed. An image with no build of the app
  serves the JSON API only.
- **An HTML document fits the screen.** The frame is as tall as its content,
  and a wide document scales to the width of the panel. "Actual size" and "Fit
  width" switch the view, and the browser keeps the choice for each document.
  A document without scripts has `sandbox allow-same-origin` with no
  `allow-scripts`, so the page can read its size. A document with
  `scripts: true` keeps an opaque origin.
- **The viewer installs as an app.** Add to Home Screen opens the viewer with no
  browser chrome. The service worker keeps the shell for a slow network. It
  never handles a request under `/api/v1/`. Each build stamps its id into the
  service worker, so a deploy replaces the old cache. The app checks for a new
  build when it opens, on focus, when the network returns, and every 30
  minutes. A hidden app reloads. A visible app shows "Joshua was updated" with
  a Reload button. A reinstall is necessary one time, after the upgrade to this
  version.
- **Upgrade notes.**
  - The viewer calls channels at `CHANNELS_URL` with `JOSHUA_TOKEN_VIEWER`. The
    chart and docker compose set the URL. The channels container must also hold
    `JOSHUA_TOKEN_VIEWER`. Without both, the chat routes answer 503 and
    `/readyz` shows `"chat": false`.
  - The default of `channels.webhooks.allowed_callers` is now `laptop`, `ci`,
    and `viewer`. An instance that lists the callers in `joshua.yaml` must add
    `viewer`.
  - The default of `workspaces.allowed_callers` is `viewer` and `gateway`. An
    instance that lists the callers must add `gateway`.
  - The gateway reaches core at `CORE_URL`. The default is the core service of
    the platform.
  - The app now serves `/chat`. A deployment that routes `/chat` to the old
    separate chat app keeps that app until the route is removed.
  - The person id `w` is reserved.
  - The schema adds the tables `workspaces` and `workspace_items`. Nothing
    changes until a workspace exists.
- **The assignments in the viewer are React pages.** The list at
  `/assignments` and one assignment at `/assignments/<id>` are the app now.
  The state pills write the address (`?state=draft`), so a bookmark of a state
  opens the same rows. An assignment shows its definition as a document: each
  field of the specification is a property, the goal is blocks, and each part
  takes a comment on its first line, with the `Source` fold for one exact
  line. The plan shows its task boxes as a checklist with the steps that are
  done and the next step. An assignment in progress reads itself again every
  20 seconds and stops when it finishes. The report shows in blocks for now.
  The app reads and writes JSON addresses with the sign-in of a page:
  `GET /api/v1/assignments`, `GET /api/v1/assignments/<id>`,
  `POST /api/v1/assignments/<id>/comments` and
  `POST /api/v1/assignments/<id>/comments/<id>/delete`. The saved assignments
  at `/assignments/saved` are the same pages as before. With
  `ASSIGNMENTS_URL` or `JOSHUA_TOKEN_VIEWER` unset, both pages say that the
  assignments manager is not configured and select no state.
- **The documents in the viewer are React pages.** The list at `/documents`
  and one document at `/documents/<owner>/<slug>` are the app now. The status
  tabs and the kind chips write the address (`?status=draft&kind=plan`), so a
  bookmark of a list opens the same rows. A Markdown document shows in
  blocks, each block takes a comment on its first line, the `Source` fold
  takes a comment on one exact line, and the `Versions` fold opens an older
  version at `?rev=<sha>`, which is read-only. An HTML document still shows in
  its sandboxed frame. The app reads and writes JSON addresses with the
  sign-in of a page: `GET /api/v1/documents`,
  `GET /api/v1/documents/<owner>/<slug>`,
  `POST /api/v1/documents/<owner>/<slug>/comments`, and
  `POST /api/v1/documents/<owner>/<slug>/comments/<id>/delete`.
  `/documents/<owner>/<slug>.json` and `/documents/<owner>/<slug>/artifact`
  answer as before, because the chat calls both. In a frame, the app draws no
  sidebar, no app bar and no tabs, so the artifact panel of the chat shows the
  document alone. A document with the headings of an assignment report shows
  in blocks for now.
- **The links in the viewer are a React page.** `/links` is the app now, and
  the "Links" group of the sidebar shows on every page of the app. A member
  adds a link, changes it, moves it, and removes it under "Edit links", then
  saves the whole list with one commit on `wiki/.viewer/links.yaml`. A guest
  sees the list and no editor. The app reads and writes one JSON address with
  the sign-in of a page: `GET /api/v1/links` and `POST /api/v1/links`. That
  address is the one source of the list, so `GET /api/v1/session` no longer
  carries it.
- **The usage in the viewer is a React page.** `/usage` is the app now: the
  two subscription meters, the stacked bar chart of the tokens of each day,
  and the four tables. The day range writes the address (`?days=90`), so a
  bookmark of a range opens the same window. With `CORE_URL` or
  `JOSHUA_TOKEN_VIEWER` unset, or with a core that does not answer, the page
  says which half is missing and shows the other one. The app reads one JSON
  address with the sign-in of a page: `GET /api/v1/usage?days=<n>`, which
  makes both calls to core at the same time and derives every number of a
  meter.
- **The search in the viewer is a React page.** The field in the app bar now
  goes to `/search?q=<query>` inside the app, so no page loads, and a
  bookmark of a search opens the same hits. The page keeps both halves: the
  search by meaning through core first, then the search by words over the
  Markdown you can read, at most 50 hits. With `CORE_URL` or
  `JOSHUA_TOKEN_VIEWER` unset, or with a core that does not answer, the page
  shows the words half alone. The app reads one JSON address with the sign-in
  of a page: `GET /api/v1/search?q=<query>`.
- **The journal in the viewer is a React page.** The feed of one month at
  `/journal` and one day at `/journal/<year>/<month>/<day>` are the app now.
  The feed opens the newest month that holds an entry, newest day first. The
  month picker and the person filter write the address (`?month=2026-11`,
  `?person=alex`), so a bookmark of a month, of a person, or of both opens the
  same thing. A day page links the day before and the day after that hold an
  entry. A member still edits an entry from the entry, and lands back on the
  day at that entry. The app reads two JSON addresses with the sign-in of a
  page: `GET /api/v1/journal?month=&person=` and
  `GET /api/v1/journal/<year>/<month>/<day>`.
- **The writes on the wiki are React pages.** The editor of a page, the
  editor of a journal entry, the new page, and the trash are all the app now.
  Edit a page, save it, and the page comes back with your change committed;
  start a page in a folder from "new page in …"; the trash asks first and
  moves the page under `wiki/.trash/`. The app writes through four JSON
  addresses with the sign-in of a page: `GET /api/v1/edit?path=`,
  `GET /api/v1/new?folder=`, `POST /api/v1/save`, and
  `POST /api/v1/delete`. Each write keeps the checks it had: a member only, a
  token of the person and the page, an origin of the host, the 256 KB cap, and
  one commit of the wiki. A guest still reads.
- **The wiki in the viewer is a React page.** A page, a folder, the front
  page at `/wiki/`, a file under `/shared/` and one of your own attachments
  are all the app now. A page shows the crumbs, the frontmatter, the
  Markdown, and the other pages of its folder, and a member still gets the
  edit and the trash. The app reads one JSON address,
  `GET /api/v1/wiki?path=<path>`, which answers a page, a folder, or a file,
  and `GET /api/v1/files/<root>/<path>` delivers the bytes of a file: an
  image shows inline, anything else downloads with its name. Every address
  you have in a bookmark still opens the same thing, and a page you wrote
  before does not change.
- **Your own page in the viewer is a React page.** The viewer now ships a
  JavaScript app beside the pages it renders in Python, and `/me` is the
  first page of it: your name, your profile, the journal, your recent
  attachments, and the wiki tree, as before. The navigation reaches every
  other section, which the server still renders. The app reads JSON
  addresses under `/api/v1/`: `/api/v1/session`, `/api/v1/me`, and
  `/api/v1/wiki/tree`, each with the sign-in of a page. The content policy of
  the viewer now allows a script from the viewer itself (`script-src
  'self'`); no inline script runs. See
  [security.md](security.md#the-headers-of-the-viewer).
- **An HTML artifact shows with its own styles (#123).** The viewer serves
  the body of an HTML document from an address of its own,
  `/documents/<owner>/<slug>/artifact`, and the frame of the document page
  loads it. The address has a content policy of its own: the styles of the
  document render, an image comes from the viewer, and the document reaches
  no network, submits no form, and opens no frame. A script runs only with
  `scripts: true` in the frontmatter. The frame keeps its `sandbox` and never
  gets `allow-same-origin`, so the document still cannot reach the viewer or
  your sign-in. The address has the sign-in and the owner rule of the page. A
  Markdown document does not change. See
  [documents.md](documents.md#artifacts) and
  [security.md](security.md#the-headers-of-the-viewer).
- **A small JSON address for the head of a document.**
  `GET /documents/<owner>/<slug>.json` in the viewer gives the title, the
  kind, the status, and the time of the last change of one document, and no
  part of the body. The chat shows the title of an artifact card from it, and
  reads it again while the panel is open to see a change that no turn made.
  It has the same sign-in and the same owner rule as the document page, and
  the answer is never cached. A document slug now never ends in `.json`. See
  [documents.md](documents.md#the-documents-section-of-the-viewer).
- **A page of the viewer can frame a document page.** The content policy of
  the viewer now has `frame-ancestors 'self'`, and the viewer sends
  `X-Frame-Options: SAMEORIGIN`. So the chat shows a document in an artifact
  panel, which is the document page of the viewer itself. Only the same
  origin can frame the viewer: no other site can. An HTML document still
  shows in a sandboxed frame with no access to the viewer. See
  [security.md](security.md#the-headers-of-the-viewer).
- **A terminal turn can carry a file.** `POST /v1/cli/turns/stream` now also
  takes `multipart/form-data` with `person`, `text`, and one `file` part for
  each file. Channels runs the same attachment pipeline every other channel
  runs: it reads the type from the bytes, stores an image, a PDF, or a text
  file under `people/<id>/attachments/YYYY/MM/`, and skips any other type. The
  turn carries the stored paths. A JSON body works as before, so `make chat`
  does not change. See [channels.md](channels.md#the-terminal).
- **A Chat entry in the viewer navigation.** The sidebar and the phone tabs
  have a Chat item. It opens `/chat/<your id>`, the chat on the same host as
  the viewer. The viewer only links to the chat; it serves no chat page.
- **A chat client can read a conversation back (#54).** Core has a new route,
  `GET /v1/transcript`, for `channels` and `laptop`. It returns the messages
  of one conversation, oldest first, a page at a time. Channels has
  `GET /v1/cli/transcript?person=<id>` for the terminal conversation. Each
  message carries only safe `meta` keys, such as the turn id and the tools
  the turn used. An unknown conversation returns no messages.
- **The worker and Joshua write in Simplified Technical English (#140).**
  The worker prompt and the documents skill page have the same section, "How
  you write": one idea in each sentence, the present tense and the active
  voice, names instead of adjectives, no hedges, and "Not verified:" for what
  was not checked. The report shape and the plan shape do not change. See
  [documents.md](documents.md#versions).
- **A document states what is true now (#136).** The documents skill page,
  the assignments skill page, and the worker prompt tell Joshua and a worker
  to write a plan, a spec, or a definition in the present tense, as fact,
  with no "decision made" or "previously" section. The versions and the
  comment resolutions hold what changed. See
  [documents.md](documents.md#versions).

## 0.2.1 - 2026-11-09

Joshua now keeps documents for each member: plans, reports, specs, artifacts,
and notes, in a git repository of their own beside the wiki. Assignments write
their deliverables as documents, start as drafts instead of in a queue, can
be saved and started again by name, and take comments that Joshua applies.
The viewer has an Assignments section and a Documents section.

### Upgrading from 0.2.0

**Nothing needs a change.** Your `joshua.yaml` from 0.2.0 loads as it is. The
assignments store adds its new columns at start, and each `queued`
assignment becomes a `draft`. Then:

1. On Kubernetes, when the viewer and assignments are both on, add
   `JOSHUA_TOKEN_VIEWER` to the assignments Secret, with the value of the
   viewer Secret. Compose gives it to the container from `.env`.
2. A caller that filters assignments by state uses `draft` in place of
   `queued`, in `list_assignments` and in the read API.
3. To use documents, add `mcp.documents: {kind: builtin, allow: all}`. See
   [documents.md](documents.md).
4. `write_file` now refuses a wiki page whose frontmatter has `kind` `plan`,
   `report`, `spec`, `artifact`, or `draft`, or `status: draft`. Write such
   a page as a document.

### Assignments

- **The plan and the report of an assignment are in the store, not the
  wiki (#96, #98).** The worker writes its plan with `write_plan`, records
  each output with `record_output`, and writes a report with `write_report`
  before it calls `finish`. The runner adds the cost to the report. When an
  assignment stops in another status than `done` with no report, the
  runner writes one. `assignment_status` gives the title, the state, the
  plan, the report, and the outputs, and `list_assignments` filters by
  state. A spec can give a `title`. A session no longer gets the `files`
  server when `tools` leaves it out. The skill page tells Joshua to read the
  plan, the report, and the outputs with `assignment_status`. The plan pages
  that earlier assignments wrote in `wiki/assignments/` stay where they are.
  See [assignments.md](assignments.md#the-plan-the-outputs-and-the-report).
- **Saved assignments (#102).** Joshua can save the spec of a job that
  recurs under a short name with `save_assignment`, and start it again with
  `start_assignment(saved="<name>")`. A `spec` beside `saved` changes the
  fields that it gives for that run. `list_saved_assignments` gives the runs
  and the last run, and `delete_saved_assignment` deletes one.
  `assignment_status` and `list_assignments` give `saved`. A schedule is a
  scheduled task that starts the saved assignment. See
  [assignments.md](assignments.md#saved-assignments).
- **The assignments container has a read API for the viewer (#105).**
  `GET /v1/assignments`, `GET /v1/assignments/{id}`, `GET /v1/saved`, and
  `GET /v1/saved/{id}` give a person's assignments and saved assignments as
  JSON. They take the fleet token of a caller in the new key
  `assignments.read_callers` (default `viewer`). Compose gives the container
  `JOSHUA_TOKEN_VIEWER`. On Kubernetes, add `JOSHUA_TOKEN_VIEWER` to the
  assignments Secret, with the value of the viewer Secret; the chart lets
  the viewer pod reach the container. See
  [assignments.md](assignments.md#the-read-api).
- **A report that the runner writes at a stop puts each heading of the plan
  two levels down (#105).** A heading of the plan no longer looks like a
  section of the report.
- **Comments on a definition (#108).** In the viewer, click the `+` beside a
  line of the definition of a saved assignment or a run, and write what to
  change. Joshua reads the comments with the new tool
  `assignment_comments`, changes the spec, and resolves each one with
  `resolve_comment`. The definition shows the open comments under their
  lines, a `line changed` mark when a line changed, and the resolved
  comments in a group. The assignments container stores the comments in the
  new table `assignments.comment`, and the read API gets the comment routes.
  See [assignments.md](assignments.md#comments-on-a-definition).
- **Drafts instead of a queue (#110).** The assignments container holds no
  queue now. `start_assignment` starts an assignment at once, or it is
  refused with the reason `busy` and the id of your running assignment;
  nothing waits for a slot. `assignments.max_concurrent` stays the ceiling.
  The new tool `draft_assignment` stores an assignment as a `draft` that
  does not run. `update_assignment` changes the spec of a draft,
  `delete_assignment` removes a draft, and `start_assignment(id=...)` starts
  one. A draft takes comments on its lines in the viewer. The status
  `queued` is gone: at start, each `queued` row becomes a `draft`. The state
  filter of `list_assignments` and of the read API takes `draft` in place of
  `queued`, and `/readyz` counts `draft` in place of `queued`. See
  [assignments.md](assignments.md#drafts).
- **A worker can request a repository or a file (#115).** The worker tool
  `request_resource(kind, ref, reason)` stops an assignment as `waiting` with
  a request: `repo`, `file`, or `info` (the same as `ask`). Joshua gets an
  event that says "requests a repository" or "requests a file". The new tool
  `grant_repo(id, repo)` adds a repository that matches `assignments.repos`,
  and the git proxy admits it from then on. The new tool
  `provide_file(id, name, content)` writes a text file to `inbox/` in the
  scratch directory (2 MiB for one file, 20 MiB and 50 files for one
  assignment). `answer_assignment` can refuse a request. The store gets the
  columns `open_request`, `granted_repos`, and `inbox`, and the read API
  gives them. In the viewer, the notice band of an in-progress assignment
  shows the kind, the ref, and the reason of a request. See
  [assignments.md](assignments.md#requests-for-a-repository-or-a-file).
- **Assignments write deliverables as documents (#125).** The session of an
  assignment now always holds the `documents` server when the person may
  reach it, also when the spec narrows `tools`. The first message tells the
  worker to write each deliverable with `write_document` under
  `documents/<person>/` and to record it as an output of kind `document`,
  with the path as ref. The system prompt says that a long text with no
  named place is a document, and that a plan, a draft, or a report never
  goes into the wiki. The gateway sets `author: assignment:<id>` and
  `assignment: <id>` on a document that an assignment makes. The report that
  the runner generates names the viewer page `/documents/<owner>/<slug>` of
  each document. The skill page names a document as the default place for a
  plan, a spec, or a report that the person will read and refine. The plan
  and the report of the run stay in the assignments store. See
  [assignments.md](assignments.md#deliverables-are-documents).
- **Joshua starts exactly the work you mean (#127).** A refusal of
  `start_assignment` now has a `hint`, one sentence that Joshua tells you,
  and where it applies `retry_after_s`, `running` (the id and the title;
  another person's run has no id), and `drafts`. `busy` says "assignment N
  is still running; start this one when it ends, do not create another". A
  new spec is refused as `drafts_open` while you have drafts, unless the
  call passes `new: true`, so Joshua starts the draft by its id. A started
  draft keeps its id. `cancel_assignment` returns only when the slot is
  free, so a start right after it is not refused as `busy`.
  `delete_assignment` now only removes a draft from view: the store column
  `deleted_at` is set, the row and its comments stay, and the new tool
  `restore_assignment` brings it back. A deleted draft is in no list, not in
  the read API, and not in the viewer. The skill page has a section "Which
  assignment the person means". The page and the row of a draft in the
  viewer say "Tell Joshua: start assignment N". See
  [assignments.md](assignments.md#refusals-of-a-start).
- **Answer a decision on the report page, and Joshua hears it (#129).** The
  page of a completed assignment opens with a lead band: "N decisions wait
  for you", with a link to them, and the outputs as cards, the documents
  first, each with an `Open` link. The outputs section lower on the page is
  gone. Each decision card has a text box and `Send to Joshua` in place of
  the `Copy` button. The answer is a comment on the report, and the card
  shows it with `Sent to Joshua` and the time, then the resolution of
  Joshua. The plan and the report take a comment on each block with the
  `+`, as a definition does. The comment routes take `target`
  (`definition`, `plan`, or `report`), and `assignment_comments` takes
  `target` and gives the target of each comment. A comment on the plan or
  the report of a completed assignment goes to your conversation as one
  event, for the comments of two minutes, with a header that says that the
  text is from you. The store column `comment.sent_at` is new. The
  assignment pages of the viewer have no script now. See
  [assignments.md](assignments.md#comments-on-a-plan-or-a-report).
- **An answer changes the document, and a re-run keeps its lineage
  (#134).** The event for a comment on a report or a plan now tells Joshua
  to apply it to the document or the tasks, not to start an assignment for
  it, and to resolve it only after the change. `resolve_comment` takes
  `applied`: the document paths, tasks, or issue URLs that changed. A
  resolution with none is refused as `nothing_applied`. The viewer shows
  them under the resolution. `start_assignment(rerun=N, note)` starts a new
  run with the spec of the ended assignment N and the note of the person at
  the start of the goal. `assignment_status` and the read API give
  `parent_id` and `rerun_of`, and the viewer shows "run k of assignment N"
  and a fold with each run. The store columns `assignment.parent_id` and
  `comment.applied` are new. The manager and the gateway log each refused
  tool call with its reason, and never an argument. See
  [assignments.md](assignments.md#re-runs).

### Documents

- **Documents: a fourth memory surface (#121).** The new builtin `documents`
  MCP keeps what Joshua works on or makes for a member (a plan, a report, a
  spec, an artifact, a note) at `/data/documents/<person id>/` and
  `/data/documents/shared/`, out of the wiki. Turn it on with
  `mcp.documents: {kind: builtin, allow: all}`; there is nothing else to
  set. The folder is its own git repository, made on the first write, with
  one commit per write. Each document has YAML frontmatter; the server sets
  `author`, `created`, and `updated`. The tools are `list_documents`,
  `read_document`, `write_document`, `set_document_status`,
  `search_documents`, `promote_document`, `trash_document`,
  `document_comments`, and `resolve_document_comment`. Comments are in
  `<slug>.comments.jsonl` beside the document. A comment names a line of
  the body, from 1, so a change of the frontmatter never moves it; line 0 is
  the document as a whole. The viewer numbers the lines of a document the
  same way. Members only: a member never reaches the folder of another
  member, and a guest gets no tools. `write_file` now refuses a wiki page
  whose frontmatter has `kind` `plan`, `report`, `spec`, `artifact`, or
  `draft`, or `status: draft`. A member session that holds the server gets
  the skill page `prompts/builtin/skills/documents.md`. See
  [config.md](config.md#the-documents-mcp).

### Viewer

- **An Assignments section (#97).** The viewer has a fifth section,
  **Assignments**, at `/assignments`. A person reads their own assignments
  there: the state, the plan, the report and its outputs, the progress, and
  the definition. The saved assignments are at `/assignments/saved`. The
  viewer needs `ASSIGNMENTS_URL`: compose sets it, and the chart sets it
  when `assignments.enabled` is true.
- **The report of a completed assignment is the page (#103).** The page has
  a head with the first sentence and the numbers, the highlights and the
  decisions as cards, and the cost. An assignment in progress shows a bar
  from the check boxes of its plan, the next step, and the outputs so far.
  A list row shows the first sentence of the report or the next step.
- **An open viewer can belong to a member (#107).** `viewer.anonymous` now
  also takes a person id. A request with no password is then served as that
  member, as if they signed in: their profile, attachments, assignments,
  and saved assignments. The id must name a member in `people`; a guest or
  an unknown id stops the config load. `true` keeps the synthetic
  `anonymous` identity. See [config.md](config.md#viewer).
- **A definition reads as a document (#114).** The definition of a saved
  assignment, a draft, or a run shows its fields as a short list of
  properties and its goal as Markdown, as a wiki page shows it. Each field
  and each block of the goal (a paragraph, a heading, a list item, a quote,
  a code block, or a table) has its own `+` for a comment, and a comment
  shows under the block that holds its line. The `Source` fold under it
  gives each line of the YAML with its number and its `+`. A comment still
  names one line, so Joshua reads it as before. See
  [operations.md](operations.md#comments-on-a-definition).
- **A Documents section (#122).** `/documents` lists your documents and the
  shared ones by status (`Active`, `Drafts`, `Final`, `Archived`) and by
  kind. A Markdown document shows in blocks, and one with the headings of a
  report shows as the report of an assignment does. Each block and card has
  a `+` for a comment, which the viewer writes to the comments file of the
  document and commits. The `Versions` fold lists the commits of the file,
  and `?rev=<sha>` shows an old version, read-only. An HTML document shows
  in a sandboxed frame, with scripts only for `scripts: true`. An output of
  kind `document` on an assignment page links to its document. A guest has
  no Documents section. See
  [operations.md](operations.md#the-documents-section).

### Core and memory

- **The memory finds documents by title and summary (#120).** A new index
  source, `documents`, writes one row for each document in
  `documents/<owner>/`: the title, the kind, the status, and the `summary`
  (or the first paragraph), never the body. It skips an archived document
  and an `html` document, and it reads the volume again on
  `memory.index_interval_s`. Recall shows a document as
  `document: <title> (<path>, <kind>, <status>)`, and Joshua opens it with
  `read_document`. Only a conversation of the member who owns a document
  finds it, and every member finds the shared documents. A group chat and a
  guest find none. `POST /v1/memory/search` accepts `source: documents` with
  a `person`, and the search page of the viewer links a document to
  `/documents/<owner>/<slug>`. `memory.documents.enabled` (default `true`)
  turns it off. With no `documents/` folder the source does nothing. See
  [memory.md](memory.md#documents-in-the-index).

### Docs

- **A guide for assignments, and one for documents (#133).**
  [assignments.md](assignments.md) now opens with a guide for the person:
  what an assignment is, what to say to Joshua at each step, the flow from
  draft to completed, what the worker can and cannot do, and where each
  part is in the viewer. The reference follows. The new
  [documents.md](documents.md) is the guide for documents and artifacts.
  The README, the quickstart, memory.md, and operations.md link to both.

## 0.2.0 - 2026-11-08

Joshua can now run long jobs for a member in the background: the new
`assignments` container runs one Claude session for each assignment, stops
at the subscription limits that you set, asks when it is stuck, and reports
when it is done. Joshua also knows how much of the subscription limits is
left, and the usage page shows it. The viewer has the shared Joshua look, a
menu of links, and opens on the wiki.

### Upgrading from 0.1.1

**Nothing needs a change.** Your `joshua.yaml` from 0.1.1 loads as it is, and
the assignments container is off by default. Then:

1. Run `make init-env`. It keeps every value in your `.env` and mints only the
   new `JOSHUA_TOKEN_ASSIGNMENTS`. On Kubernetes, add it to the Secrets of
   core, channels, and the gateway before you turn assignments on.
2. The viewer address `/` now opens the wiki. The page that `/` showed
   before is at `/me`.
3. `memory.exclude_folders` defaults to `assignments`. If you keep your own
   pages in a top-level wiki folder `assignments/`, rename the folder or set
   `memory.exclude_folders`, or the index no longer holds those pages.
4. To use assignments, read [assignments.md](assignments.md): set
   `assignments.enabled: true`, add `assignments` to
   `channels.webhooks.allowed_callers`, and start the container with
   `docker compose --profile assignments up -d` or the chart value.

### Security

- **A session cannot read the assignments container.** A shell in an
  assignment session could read the environment of the container process
  in `/proc`, with the database URL and the bearers of its callers. The
  process now makes itself non-dumpable at start, so Linux refuses its
  environment and its memory to the session. The code in `/app` now belongs
  to root, so a session cannot change it. No setting is needed in compose or
  on Kubernetes. See
  [assignments.md](assignments.md#what-a-session-can-see).

### Added

- **Links in the viewer menu.** The viewer menu has a "Links" group with
  links to the tools that people use beside Joshua. Each link opens in a new
  tab. A member adds, changes, and removes the links at `/links`: a title, an
  http or https URL, and a built-in icon. The viewer keeps them in
  `wiki/.viewer/links.yaml`. See
  [operations.md](operations.md#links-in-the-menu).
- **`/` opens the wiki.** The viewer address now opens the wiki. The page
  that `/` showed before, with the profile link and the recent attachments,
  is now "Your page" at `/me`.
- **A stalled assignment stops and asks.** The system prompt of an
  assignment now tells the session to stop retrying when one action fails
  three times in a row with the same error, to write the error on the plan
  page, and to call `ask` with the error and what it tried. The message
  after a slice with no new checkpoint says "You made no progress last
  slice; if you are stuck, ask." After `assignments.stall_slices` slices in
  a row with no new checkpoint (default 3), the runner stops the assignment
  as `waiting`. Joshua then gets a question that names the last checkpoint
  and the last tool errors. `answer_assignment` resumes it. See
  [assignments.md](assignments.md#a-stalled-assignment).

- **Assignments ask, resume, and clone.** `answer_assignment` now reads the
  limits first: when a window is still at or over its ceiling, the
  assignment stays parked and the reply says when the window resets. A
  `waiting` assignment waits with no time limit. The answer goes to the
  model one time, so a restart does not send it again. At start, each
  `running` assignment continues its session with "Read your plan page
  before anything else; the pod restarted." `extend_assignment` also takes
  `max_turns`, and it continues a `budget_exceeded` or `time_exceeded`
  assignment. Their scratch now stays for `assignments.keep_failed_work`. A
  session clones the repositories of its spec over HTTPS through a
  listener on `127.0.0.1` in the manager, which puts the token of
  `assignments.platforms.<host>.token_env` on each clone, checks
  `assignments.repos` again, and refuses a push. The session never holds a
  git token. In compose, put the token in `.env.assignments`; on Kubernetes,
  add it to `secrets.assignments`. Follow-ups go to the tasks project of the
  spec only when the session holds the `tasks` server, and to the plan page
  in all other cases. The report gives the cost. See
  [assignments.md](assignments.md#questions-and-answers).

- **A skill page for assignments.** Core adds
  `prompts/builtin/skills/assignments.md` to the system prompt of a session
  that holds the `assignments` server: how to build a spec, how budget words
  become the `budget` block, when to answer a question alone, what to do at
  a limits stop, and to give the cost with each result. See
  [assignments.md](assignments.md#the-skill-page).

- **`rename_file` moves a page.** `new_name` can now be a path such as
  `wiki/projects/idea.md`, and the page moves to that folder, with one wiki
  commit. `rename_file` now refuses a file in, or a target in,
  `wiki/people/` and `wiki/joshua-docs/` too, not only `wiki/journal/`. See
  [config.md](config.md#the-files-mcp).

- **Assignments run a real session.** Each assignment now runs one Claude
  Agent SDK session in `/work/<id>/`, with the file tools, Bash, the MCP
  servers of the person through the gateway, and the tools `checkpoint`,
  `wait`, `ask`, and `finish`. It works in slices of
  `assignments.slice_turns` turns. Before each slice it reads core's
  `GET /v1/limits`, and at the ceiling of its spec it stops as
  `paused_limits` and asks Joshua through the question path. It writes a
  plan page in `wiki/assignments/` first and keeps it current. Each slice
  sends one usage row to core, so the usage page shows the tokens of the
  assignments. `assignment_status` gives the next step. With
  `limits.when_unknown: refuse`, `start_assignment` refuses a spec with no
  `budget.tokens` while core cannot say how much of the limits is used. The
  container now needs `CORE_URL` and `GATEWAY_URL`; compose and the chart set
  them. See [assignments.md](assignments.md#the-runner).

- **The assignments container.** A fourth container,
  `assignments`, stores long jobs that run for a member in the background.
  It is off by default: set `assignments.enabled: true` and start it with
  `docker compose --profile assignments up -d`, or set
  `assignments.enabled: true` in the chart. Joshua gets six tools through the
  gateway: `start_assignment`, `assignment_status`, `list_assignments`,
  `answer_assignment`, `cancel_assignment`, and `extend_assignment`. The
  container keeps its rows in the schema `assignments` of the same Postgres,
  holds no model credential, and sends the report to channels with its own
  fleet token, `JOSHUA_TOKEN_ASSIGNMENTS`.
  See
  [assignments.md](assignments.md) and
  [config.md](config.md#assignments).

- **Joshua knows how much of the subscription limits is left.** Ask "how
  much of my limit is left?", and the `check_limits` tool gives the percent
  used and left of the 5-hour window and the weekly window, and when each
  resets. Core's new `GET /v1/limits` gives the same numbers as JSON with no
  bearer, in the cluster and on the host at `127.0.0.1:8081`. The numbers
  come from the rate-limit report of each model call on the subscription, at
  no cost. They show after the first model call since the start, or from the
  last saved value. When the reset time of a window is in the past, the
  answer marks the window `reset`: the next model call updates its numbers.
  A job on another inference server does not report the limits. The token
  from `claude setup-token` is the supported token. The new `limits` section
  turns the route and the tool off, or the tool off for chat or for voice.
  See [config.md](config.md#limits).
- **The usage page shows the subscription limits.** Above the chart, the
  viewer's `/usage` page has a meter for the 5-hour window and the weekly
  window: the percent used, a bar, the percent left, and the reset time in
  the instance timezone. A line under the heading gives the time of the
  numbers. A window that reset shows its last bar dimmed and says that the
  next model call updates it. The bar is amber from 75 percent used and red
  from 90 percent. The meters need only `CORE_URL`. When core does not
  answer, the section says so, and the rest of the page does not change. See
  [operations.md](operations.md#the-usage-page).
- **Core can send the model calls of the assignments container.** The new
  route `/v1/model/{path}` takes a Claude API request with the token of an
  identity in `inference.forwarder.allowed_callers` (default `assignments`),
  puts the credential of the new inference job `assignment` on it, and
  streams it to the model server and back. The container holds no model
  credential, and the credential never leaves core. Only the paths of the
  Claude CLI go on. Every other token gets `401` and every other path gets
  `403`. `inference.assignment` can send these calls to another server, as
  for any job. See
  [contracts.md](contracts.md#the-model-forwarder).
- **Another container can report its model calls on the usage page.** The new
  route `POST /v1/usage` takes usage rows of kind `assignment` from an
  identity in `usage.writers` (default `assignments`). The usage page now
  always shows an `assignment` group. A row of any other kind gets `400`. See
  [contracts.md](contracts.md#the-usage-write-route).
- **A wiki folder can stay out of the memory.** `memory.exclude_folders`
  (default `assignments`) names top-level wiki folders that the index does not
  hold and that no search returns. The plan pages of the assignments, in
  `wiki/assignments/`, thus never come back in a turn. See
  [config.md](config.md#index).
- **The gateway trusts the person headers from the assignments container.**
  The new `gateway.person_header_callers` (default `core` and `assignments`)
  names the identities that may call the MCP routes and say which person a
  call is for. Every other identity gets `403` on an MCP route, as before.
  The snapshot and search routes still admit `core` only. See
  [config.md](config.md#gateway).
- **`make init-env` mints `JOSHUA_TOKEN_ASSIGNMENTS`.** Compose and the chart
  give it to core, channels, and the gateway, so each one can verify the
  assignments container. Run `make init-env` again to add it to an existing
  `.env`. To let the container post events, add `assignments` to
  `channels.webhooks.allowed_callers`.

### Changed

- **The viewer has the Joshua look.** The viewer now uses the shared Joshua
  stylesheet. It is dark only. On a wide screen, a sidebar holds the Wiki,
  Journal, Search, and Usage links. On a phone, an app bar and tabs hold them.
  The search field is in the app bar on each screen. The viewer serves the
  stylesheet, the logo, and the icons at `/ui/`, behind the same sign-in as a
  page. The routes, the forms, and what each page shows do not change.

### Fixed

- **A stopped tool call no longer breaks a store connection.** When a caller
  of the assignments store stopped during a write (a slice that ends while a
  `checkpoint` call runs, or a gateway call that closes), the manager logged
  `another command is already in progress`, and the pool closed that
  connection. Now each store operation completes and gives back a clean
  connection, also when its caller stops. See
  [assignments.md](assignments.md#the-store).

## 0.1.1 - 2026-11-07

A voice call that hangs up during a reply no longer makes the replies after it
late, and an idle session now closes after its deadline.

### Upgrading from 0.1.0

Nothing needs a change.

### Fixed

- **A turn that stops early no longer makes the next replies late.** When a
  client closed the stream of a turn before the reply completed (a voice call
  that hangs up, a closed page), the rest of that reply stayed on the session.
  The next turn of the conversation then gave the reply to the turn before it,
  and each later turn did the same. Now core gives up the client of a
  cancelled turn, and the next turn starts a new one. The log line is
  `turn cancelled`.

- **An idle session closes after its deadline.** Core now starts the reaper,
  so `core.session_idle_seconds` and `channels.voice.idle_ttl_s` take effect.
  The first turn after a session closes starts a new one, with the history of
  the conversation.

## 0.1.0 - 2026-11-07

Joshua can now send its model calls to a server other than the Claude
subscription: the Anthropic API, LiteLLM in front of Bedrock, or a local
model, with a server, a model, and a key for each job. Web search then works
through SearXNG. Tool search makes a conversation turn with many MCP servers
much smaller. The agent can read back the current conversation, snapshots put
the state of a live system in the memory, and a usage page shows the tokens
that Joshua used. The viewer and the joshua-mcp addon can search the memory by
meaning.

### Upgrading from 0.0.8

**Nothing needs a change.** Your `joshua.yaml` from 0.0.8 loads as it is. With
no `inference` section, every model call stays on the subscription token, as
before. Then:

1. Run `make init-env`. It keeps every value in your `.env` and mints only the
   two new tokens, `JOSHUA_TOKEN_VIEWER` and `JOSHUA_TOKEN_MCP`. Without them,
   the viewer searches by words, as before. On Kubernetes, add
   `JOSHUA_TOKEN_VIEWER` and `JOSHUA_TOKEN_MCP` to the core Secret, and
   `JOSHUA_TOKEN_VIEWER` to the viewer Secret. The chart marks both keys
   optional, so a Secret without them still starts.
2. Expect a longer first start on a large transcript. Core adds the index
   `idx_transcript_fts`, and fills the new `usage_event` table from the chat
   turns that it made before.
3. To send a reminder made by voice to a phone, set
   `channels.voice.deliver_via`. Without it, such a reply is dropped, and the
   log says why.

**Tool search is on for `chat`.** A chat turn now loads the definition of an
MCP tool when it needs one. A turn that uses a tool can take one more model
call. Set `inference.chat.tool_search: false` to turn it off.

**To move off the subscription,** read
[operations.md](operations.md#the-model-server) before you add an `inference`
section. When the `internet` job moves off Anthropic, web search stops unless
`internet.search` names a SearXNG instance.

### Added

- **An MCP server `allow` list can name a group chat.** A group chat reaches
  the gateway as person `unknown`, so a server scoped to person ids was closed
  to it. Add the group id from `groups` to `allow`, and the chat can use the
  server. An identity server takes an identity for the group id. Validation
  refuses a group with no `members`. Core sends the new `X-Joshua-Group`
  header, which the gateway trusts only from `core`. See
  [config.md](config.md).

- **An `inference` section names the server and the key for the model
  calls.** Set `inference.base_url` and `inference.api_key` to send every job
  to a server that speaks the Anthropic Messages API, such as LiteLLM, or set
  `api_key` alone to use the Anthropic API. A block for each job (`chat`,
  `voice`, `reflection`, `describe`, `internet`) can change the model, the
  server, the key, or all three. A job that sets `base_url` must also set `api_key`, so the
  global key never goes to another server. Core removes the provider
  variables from its own environment at start, gives each call the
  credential of its job only, and logs the server and the model of each job.
  `inference.timeout_seconds` (default 600) limits one chat or voice turn and
  one reflection call. A server change starts a new session for each
  conversation. A failed call gives the person the failure reply, never the
  error text. The usage rows name the model and the server of each call, and
  core keeps the cost of a turn only for a call to Anthropic. See
  [config.md](config.md#inference).

- **`POST /admin/inference/check` tests the server of each job.** Core sends
  one short prompt to each distinct server, model, and key of the
  `inference` jobs, and returns for each job the server, the model, `ok`,
  the seconds, and a short error. The error has no key and no token. Each
  call writes one usage row. `/readyz` on core gives `inference_overrides`,
  the number of job blocks that set `base_url` or `model`. The Helm chart
  (0.1.0) shows the `inference` keys in `secrets.core.keys` and the
  `inference` section with `${VAR:-}`. [operations.md](operations.md#the-model-server)
  has recipes for the subscription, LiteLLM in front of Bedrock, one LiteLLM
  key for each job, and a local model for one job.

- **A job block can set `api_key` alone.** The job then uses the global
  `base_url` with its own key, and never gets the global key, so LiteLLM can
  count the use of each job apart. With no global `base_url`, the job key is
  an Anthropic API key. An empty job key on the global server sends no key,
  and does not fall back to the global key. See [config.md](config.md#inference).

- **Web search off the Anthropic server, through SearXNG.** On the Anthropic
  server the internet agent uses Anthropic's own `WebSearch`, as before, and
  needs nothing. When the `internet` job moves to another server (Bedrock
  through LiteLLM, Ollama), web search stops, unless the new block
  `internet.search` names a SearXNG instance: `url`, an optional `api_key`,
  `max_results` (default 10), `timeout_seconds` (default 20), and the optional
  `categories` and `language`. The worker then holds one in-process tool,
  `web_search`, which calls the new gateway route `POST /v1/search` with the
  token of core. The gateway calls the SearXNG JSON API and keeps only `http`
  and `https` URLs. The optional key stays in the gateway (`.env.gateway`,
  `secrets.gateway` of the Helm chart), and the agent never sees a raw
  result. Each result URL may be fetched, and a URL in a snippet may not.
  SearXNG must allow the `json` format, and its `403` gives a clear error.
  With no block on another server, the worker has no search and says so.
  Core logs the search at start (`anthropic`, `searxng`, or `none`) and warns
  only for `none`. See
  [operations.md](operations.md#web-search-off-anthropic-searxng).

- **A one-hour prompt cache off the subscription.** On the subscription the
  CLI writes the prompt cache for one hour. On any other server it writes it
  for five minutes, so a reply more than five minutes after the last one
  wrote the full prompt to the cache again. In one measured day through
  LiteLLM to Bedrock, these writes were about 80% of the cost of `chat`.
  Each job that is not on the subscription now gets
  `ENABLE_PROMPT_CACHING_1H=1`, and the CLI asks for `"ttl": "1h"` on each
  cache marker. The other cache variables of the CLI are empty for each
  job. You do not set anything. See [config.md](config.md#inference).

- **Tool search for the conversation turns.** A new key,
  `inference.<job>.tool_search`, for `chat` (default `true`) and `voice`
  (default `false`). With it on, a model call sends only the names of the MCP
  tools, and the model loads the definition of a tool when it needs it. With
  many MCP servers, a call through LiteLLM sent all the definitions on each
  call (about 90000 tokens in one measured setup), and a turn with tool
  search used about 8000 tokens in place of about 106000. A turn that uses a
  tool can need one more model call, which is why it is off for `voice`. Core
  then gives the agent exactly one CLI tool, `ToolSearch`, and sets
  `ENABLE_TOOL_SEARCH` for the CLI; with it off, the agent has no tool and
  the variable is empty. `ToolSearch` gives no capability: it loads only the
  definition of a tool the agent already holds. The same rule applies on the
  subscription and on another server. The workers never get it, and a worker
  block that sets it fails the load. The start log gives `tool_search` for
  each job. See [config.md](config.md#tool-search).

- **The agent can read back the current conversation.** A new in-process
  server, `history`, has `search_history(query, limit)`, a word search with
  a snippet, newest first, and `read_history(start, end, cursor)`, the full
  messages of a time window in pages of at most 60 messages and 12000
  characters. Both cover only the conversation of the session: a direct
  message, a group, or a voice call. Neither tool takes a conversation, a
  channel, or a person. Failed turns, scheduled task prompts, event texts,
  and `[IGNORE]` replies are not shown. Core adds the index
  `idx_transcript_fts` at start, so the first start after the update can take
  longer on a large transcript. See [memory.md](memory.md#the-history-of-a-conversation).

- **`allow:` with no value on an `mcp` entry is the empty list.** No person
  reaches that server. Before, the value failed the config load.

- **Snapshots put the state of a live system in the memory index.** A new
  list, `memory.snapshots`, names fixed MCP tool calls: a server, a tool, the
  arguments, a cadence, and a scope. Core runs each one with no model, keeps
  the latest output in the new `snapshot_result` table, and indexes one row
  for each item. A JSON list, a run of JSON values, or a Markdown list gives
  one item for each element, and the key `fields` keeps only the named keys
  of each item. A turn whose words match an open item gets a short line with
  the time of the latest good fetch, and the agent checks the live tool
  before it changes anything. Core calls the new gateway route
  `POST /v1/snapshots/{name}` with an empty body, so the config is the
  allowlist. The gateway refuses a tool that its filter hides, that
  `tools.class` marks `write-local` or `act`, or that the server declares
  destructive. A snapshot that is stale or that failed three times leaves the
  index. `GET /admin/snapshots` shows the state of each one. `search_memory`
  now gives a snippet of 400 characters for each result, and takes
  `full: true` for the whole passage. See
  [memory.md](memory.md#snapshots-of-live-systems).

- **A usage page shows the tokens that Joshua used.** Core now writes one row
  to the new `usage_event` table for each model call: a chat turn, a nightly
  reflection call, a lookup with `use_internet`, and an attachment
  description. A row holds counts, the model, and ids only. The new
  `GET /v1/usage` (callers in the new `usage.allowed_callers`, default
  `viewer`) and `GET /admin/usage` give the tokens of each group for each day
  in the instance timezone, with `?days=N` (default 30, at most 366). The
  viewer has a new `/usage` page with a bar chart by day and a table of the
  totals and the models of each group. At each start, core adds the rows of
  the chat turns that it made before the table existed. See
  [operations.md](operations.md#the-usage-page).

- **Chart: system prompt snippets from your values file.** Set
  `core.userPrompts` to a map of file name to Markdown. The chart mounts the
  files as the prompts `user/` directory, so every session's prompt gets them,
  and you do not build an image. See
  [charts/joshua/README.md](../charts/joshua/README.md#system-prompt-snippets).

- **A voice device with no wake clip can default to a person.** Set
  `channels.voice.device_defaults` to map a device id to a person id. A device
  listed there speaks for that person on any turn that names no speaker, and
  also on a turn whose named speaker `min_confidence` does not trust, such as
  a mobile page whose own speaker identification always returns a weak guess.
  A trusted name still wins over the default.

- **Search by meaning, in the viewer and for an outside caller.** Core has a
  new route, `POST /v1/memory/search`. It searches the wiki, the journal, and
  the profiles by meaning, with the index the agent uses. The viewer's search
  page shows these results first and the word matches below them. The
  joshua-mcp addon can call the route too. The route returns no person's own
  attachments. `memory.search.allowed_callers` names who may call it (default
  `viewer`, `mcp`). Run `make init-env` to mint `JOSHUA_TOKEN_VIEWER` and
  `JOSHUA_TOKEN_MCP`. On Kubernetes, add `JOSHUA_TOKEN_VIEWER` and
  `JOSHUA_TOKEN_MCP` to the core Secret, and `JOSHUA_TOKEN_VIEWER` to the
  viewer Secret. Without the tokens, the viewer searches by words, as before.

- **The viewer can open with no sign-in, on a trusted network.** Set `viewer.anonymous: true`, and a browser with no password gets
  full access: it reads and writes the wiki, the same as a signed-in member.
  A member can still sign in with a password on the same viewer. Set
  `viewer.users` to add restrictions back. The network is then the only
  boundary, and the wiki holds the journal and every profile, so keep this off
  on an open network, and keep TLS at your reverse proxy.

- **A reminder made by voice can now reach you.** Set
  `channels.voice.deliver_via` to `imessage` or `telegram`, and a scheduled
  task or an event that would have spoken into a voice conversation arrives
  as a message on that channel instead. With `deliver_via` unset, such a
  reply is dropped, with one line in the log naming why.

### Fixed

- **The journal holds what people said, and what changed.** The nightly
  reflection no longer reads the instruction of a scheduled task or an event
  as words of a person. Joshua's reply to them stays, labelled Joshua. The
  nightly page leaves out a concern that only Joshua raised and no person took
  up, records a change and not a standing state, and keeps a deferred or
  declined suggestion as one short line. A profile takes a preference only
  from what the person said. A scheduled task that writes a journal entry on
  each run records only what changed since the last run.

- **A tool server that restarts no longer takes its tools away until the
  session ends.** An `http` upstream drops its sessions when it restarts or
  when a session is idle. The next call failed. The gateway answered core with
  `400` and `503` while it reconnected, and the agent's client in core then
  marked the server failed and stopped calling it for the life of the warm
  session, which can be hours. Now the gateway connects again and sends a
  refused call once more, and a request during the reconnect waits for it. A
  call that still fails is a tool error that says to try again. After a turn,
  core reads the status of each gateway server. When one is failed, the next
  turn starts a new session. `core.mcp_replace_seconds` (default `300`) allows
  one such replacement per session in that time. `GET /admin/inventory` shows
  `reconnects` and `last_reconnect_at` for each connection.

- **A voice reply speaks only the answer.** The stream sent all the text that
  the model wrote, also the text before a tool call. A voice device spoke it,
  so "add chips to the shopping list" got "Not on there. Adding chips." before
  the answer. Core now sends only the text that the turn returns. The answer
  starts after the model finishes the message, not word by word.

- **An announcement on a speaker says dates and years correctly.** The rules
  for words that will be spoken loaded only on a voice call, so a scheduled
  announcement that gave its text to a speaker tool got none of them. The
  voice then said "30th" and "1960" wrong. The rules are now in `speech.md`,
  which every profile loads. They apply only to text that is heard, and they
  now also tell the agent to write a date and a year the way a person says
  them: "September thirtieth", "nineteen sixty".

- **The links on a folder page work.** A folder page, such as
  `/wiki/recipes/`, shows the text of its index page, `recipes.md`. That file
  is one folder up, so a relative link in it, such as
  `recipes/pollo-saltado.md`, went to the wrong path and got 404. The viewer
  now resolves each relative link and image from the folder of the index page,
  the same as on the page itself. A link to `/attachments/` on a folder page
  now goes to the wiki attachment, the same as on a page.

## 0.0.8 - 2026-09-26

Joshua can look something up on the open web now, through the internet agent:
a worker that holds one question and nothing else. A person can have more than
one handle on a channel, and iMessage works on a BlueBubbles server that names
its chats `any;`.

### Upgrading from 0.0.7

**Action needed: to let the internet agent read a page, turn fetch on in your
`joshua.yaml`.** After the update, Joshua searches the open web, but it reads
no page, not even a link a person sends. Reading a page opens a connection
from your machine, so it is off until you turn it on and name what it must
never reach. Your `joshua.yaml` from 0.0.7 has no `internet:` section, so it
gets that default. Do these steps:

1. Copy the `internet:` section from `joshua.example.yaml` into your
   `joshua.yaml`. It sets `fetch.enabled: true` and holds a block list to
   start from.
2. Read the block list. It refuses every private network, the loopback, the
   zero address, carrier-grade NAT (where a Tailscale address lives), and the
   cloud metadata address. Add the names of your own network, such as
   `example.net` for your home domain.
3. Run `make validate`, then restart `core`. On Kubernetes, put the section in
   the `config` value of the chart.

The smallest section that turns fetch on is this. The block list in
`joshua.example.yaml` is the one to start from; this one only shows the shape:

```yaml
internet:
  fetch:
    enabled: true
    blocked:
      - 0.0.0.0/8
      - 10.0.0.0/8
      - 100.64.0.0/10
      - 127.0.0.0/8
      - 169.254.0.0/16
      - 172.16.0.0/12
      - 192.168.0.0/16
      - "::/128"
      - "::1/128"
      - "fc00::/7"
      - "fe80::/10"
      - localhost
      - .local
      - .internal
      - .svc
      - example.net           # your own domain
```

Leave fetch off if you want Joshua to search only. It then makes no
connection from your machine for a question.

**Search is on by default, and it costs model turns.** Each question uses the
model in `internet.model`, and `internet.max_turns` is the ceiling for one
question. Set `internet.enabled: false` to turn off the whole thing.

**A group lists platform handles in `members`, not person ids.** The old
example showed person ids, and a group written that way was a guest chat. If
you copied it, put each member's handle there instead.

**On BlueBubbles, check the direct-message GUIDs.** If the GUIDs in
`/admin/guard/recent` start with `any;`, set `channels.imessage.dm_service:
any`. If you do not, a reminder to a direct message is lost.

Nothing else needs a change. A handle written as one string still works. A
list of handles is new, and you do not need it.

### Added

- **A person can have more than one handle on a channel.** Most people have an
  email address and a phone number on iMessage, and they write from either.
  `people[].handles.<channel>` now takes one handle or a list. Every handle in
  the list names the person, in a direct message and in a group chat, and the
  first one is the handle Joshua uses to address them. `add_user` and
  `people add` keep the handles a person already has when they gain another.
  `GET /admin/people` and `people list` show every handle.
- **`channels.imessage.dm_service`.** A BlueBubbles server names the service
  in a direct-message GUID `iMessage` on most installs and `any` on some. Joshua
  used to build `iMessage;-;<handle>` for every reply it starts on its own, and
  on an `any` server that chat does not exist, so a reminder to a person's DM
  was lost. Set `dm_service: any` when the GUIDs in `/admin/guard/recent` start
  with `any;`. The default is unchanged.

- **Joshua can look something up.** Ask for a recipe, a price, an opening time,
  or anything that changed after the model was trained, and Joshua searches the
  open web and answers with its sources. Before this it answered from memory,
  confidently, and sometimes wrongly.

  The agent still has no web tool and never will. It asks the internet agent,
  a worker that holds one question and nothing else: no wiki, no journal, no
  profile, and no message of the conversation. So a page that tells it to look
  up something private has nothing to look up. It has no file tool, no shell,
  and no MCP server, so nothing it reads can make it act. What comes back
  reaches Joshua marked as content from the open web, never as an instruction.

  A search runs on Anthropic's side and makes no connection from your machine.
  Reading a page does make one, from your machine, so **reading a page is off
  unless you turn it on**. With no `internet:` section in `joshua.yaml`, Joshua
  searches and never fetches. `joshua.example.yaml` turns fetch on, with a
  block list a careful person would start from: every private network, the
  loopback, the zero address, carrier-grade NAT (where a Tailscale address
  lives), and the address that carries cloud credentials. The list is yours:
  nothing is blocked in the code, an empty list blocks nothing, and every line
  can go, because letting Joshua read a page on your own network is a choice
  you are allowed to make.

  ```yaml
  internet:
    fetch:
      enabled: true
      blocked:
        - 10.0.0.0/8
        - 192.168.0.0/16
        - example.net         # the domain, and every host under it
        - "*.example.net"     # the same, written as a pattern
  ```

  A URL that the check and the fetch could read as two different hosts is
  always refused, whatever the list says: a backslash, a user name, a `%` in
  the host. A host written as a number (`2130706433`, `0x7f.1`, `127.1`) is
  checked as the address it is, and an IPv6 address that carries an IPv4
  address is checked against the IPv4 entries.

  **Send Joshua a link** and it reads that page: "save this recipe for me". Ask
  about a source it cited and it goes back to it. The worker reads no other
  page than one it was given and one its own search returned. A URL on a page
  it read, in the text of a file somebody sent, or written into the question
  is refused at the fetch. Those words are content, and a link in them is the
  shortest way to send something out of your instance. Joshua asks you for the
  link instead.

  `internet.enabled: false` removes the whole thing, and Joshua then says it
  cannot look something up rather than answering as though it had.
  `docs/security.md` says what the block list does not stop.

### Fixed

- `joshua.example.yaml` and `docs/channels.md` listed person ids under
  `groups[].members`. The guard and the group role compare platform handles,
  so a group written that way was a guest chat. The examples now show handles.

## 0.0.7 - 2026-09-19

### Upgrading from 0.0.6

**Joshua now looks at each picture and each scanned PDF that somebody sends.**
A small model reads it, names the file for what it holds, and keeps the words
it can read. This is on by default. It costs about a fifth of a cent for each
new file and adds a few seconds to the first turn that carries one. The same
file is never read twice. Turn it off with:

```yaml
attachments:
  describe:
    enabled: false
```

**The wiki keeps its own files now, in `wiki/attachments/`.** Nothing is moved
without you asking: a file a person sends stays in the record of the chat, and
Joshua copies one into the wiki when a page uses it. Set
`attachments.auto_save: true` if you would rather every file a member sends go
straight to the wiki and never be deleted.

At the first start, every picture a wiki page already points at is copied into
`wiki/attachments/` and the link in the page is rewritten, one time. The file
the person sent is left where it is. `wiki/.gitignore` gains `/attachments/`,
so the files stay out of the wiki repository and your volume backup is what
holds them.

**A taught skill is matched by its words now, not by meaning.** A page whose
trigger is a phrase people really say keeps working. A page that relied on a
similar-sounding turn does not, and that is the point: the old match fired on
a plain greeting. Read `docs/memory.md` and check your `wiki/skills/` pages.
Add `match: semantic` to a page that genuinely needs the old behaviour.

**An MCP server can install itself from a package.** If you want that, give
the entry a `package:` field and add the store: the `mcp-store` volume on
compose, or `gateway.mcpStore` in the chart. An existing `mcp:` section needs
no change.

### Added
- **The gateway installs an MCP server for you, so you no longer build an
  image.** Give a `stdio` entry a `package:` field and the gateway installs it
  at start: `npm:<name>@1.2.3`, `pypi:<name>==1.2.3`,
  `git+https://<url>@<commit>`, or an `https://` file with its `sha256`. Every
  kind must pin an exact version, and an unpinned spec fails the config load
  with a message that says how to pin. The gateway image now carries Node, npm,
  `uv`, and git for this.

  Installs go to `/opt/joshua-mcp`, on their own volume: the `mcp-store` volume
  on compose, and `gateway.mcpStore` in the chart. An entry whose store already
  holds its pinned spec is not installed again, so a restart with no network
  starts the server as it is. A version bump that cannot install leaves the
  version you are running in place, and the other entries keep serving while
  one installs.

  `docs/mcp-servers.md` is new: the steps to add a server, and a tested entry
  for the weather and for Home Assistant, each with a pinned version.
  `docs/config.md` has the field, the store, and the rules.
- **A pre-flight for a package server.**
  `docker compose exec gateway python -m joshua_gateway mcp check` says which
  entries the store holds, and `mcp install` installs the rest and prints what
  it resolved. On a running gateway, `POST /admin/mcp/install` installs one
  entry and reconnects it.
- **`/readyz` separates an install from a fault.** It now counts `installing`
  and `disabled` beside `connected` and `errored`, so an entry that is still
  installing does not read as broken. `GET /admin/inventory` adds `package`,
  `resolved`, and `installed_at` for a package entry.
- **Upstream credentials have their own file on compose.** Put each variable an
  `mcp:` entry names in `.env.gateway`, one `NAME=value` per line. The gateway
  service reads it, no other service does, and it is optional. Before this there
  was no way to get an upstream credential into the gateway container without
  editing `docker-compose.yml`.
- **Joshua looks at a picture before it answers, so a taught skill fires on the
  right one.** A photo with no caption used to carry one fixed sentence, so
  every such photo matched the same skill, whatever it showed. A small model
  (`claude-haiku-4-5`) now looks at each new picture, and at a PDF with no text
  layer, and gives the turn real words: what the file is, a short subject, and
  a name for the file. The file is then renamed after what it holds, so
  `2026-09-16-140509-IMG_4471.jpg` becomes `grocery-receipt-d7e122.jpg`. The
  folder is already `YYYY/MM`, so the name carries no date; the six characters
  at the end are the digest of the bytes, which keeps two files apart and makes
  the same file land on one name however often it is filed.

  The words in a file are kept too: the total on a receipt, the account number
  on a bill, the pages of a PDF from school. Ask about any of it months later
  and Joshua answers from what it read at the time. It reads a file one time:
  the answer is stored beside the file and keyed by the digest of the bytes.

  The worker has no tool, sees no message of the conversation, and answers with
  a fixed set of fields. So an instruction written on a picture reaches nothing.
  `docs/security.md` says how that is held.

  New `attachments:` settings: `describe.enabled`, `describe.model`,
  `describe.timeout_seconds` (30 seconds, because reading a receipt off a
  picture measured 15), `extract.max_chars`, and `extract.embed`. A failure or
  a timeout leaves the turn as it was.
- **A picture in a page stays in the page.** A page used to point at the record
  of a chat, which is deleted after a year, so the page kept the link and lost
  the picture. The wiki keeps its own files now, in `wiki/attachments/`, and
  the retention sweep never touches them. Joshua puts one there with the new
  `save_attachment` tool, which copies the file and leaves what the person sent
  where it is. At the next start, every picture a page already points at is
  copied into the wiki and the link is rewritten, one time.

  Set `attachments.auto_save: true` and every file a member sends goes to the
  wiki as it arrives. A guest's file never moves, because a guest does not
  write the wiki. The wiki repository ignores the folder, so these files stay
  out of git and the volume backup holds them.
- **The wiki browser lists the files a folder holds.** A folder page showed
  its pages only, so `wiki/attachments/` looked empty even when it held the
  picture a page shows. Each file is now listed under the pages, with its size,
  and the metadata file beside it stays out of the list.
- **A search reaches what a document says.** The text of an attachment is in
  the index, so "what number do I call if I am late for pickup" finds the page
  of the school PDF that holds it. The text came from outside, so the row is
  marked external. `attachments.extract.embed: false` turns it off.

### Changed
- **A taught skill is a note to the agent, not a trigger.** A skill that
  matches is put in front of Joshua with its instructions and one line saying
  how the phrase sat in the turn: the person said it and nothing else, opened
  with it, or only mentioned it. Joshua reads the turn and decides. If you
  asked, it acts. If you used the words while describing or planning something,
  it answers you and offers, rather than acting.

  So "I really need to settle in after this week" now gets you an answer and an
  offer, where before it either dimmed the lights or ignored the skill
  entirely. And "on Saturday I am going to light the fire with friends" no
  longer starts the music.

  Before this, a match was an instruction to run the skill, so the match had to
  be certain, so it needed a new rule for every way a person can say a phrase
  without asking for it: a question, a memory, a plan, a negation. Each rule
  fitted one household's way of speaking more tightly than the last. Deciding
  what a person meant is the agent's job, and it reads the whole turn.

  `memory.skills.top_k` rises from 1 to 3: a note is not an instruction, so
  seeing two neighbouring skills is better than being handed the winner of a
  tie.
- **A taught skill fires only when a person asks for it.** A skill is now
  matched by phrase and not by meaning: the turn must open with the trigger,
  every word that carries the request must appear in order, and at most
  `memory.skills.max_extra_words` (2) extra words may sit inside the match. An
  article the person left out is still fine, and so is one inserted object
  ("add *milk* to the list").

  Before this the match was a similarity score with a floor of 0.62. Similarity
  over short phrases has a high floor — two unrelated triggers measure about
  0.62 with the default model — so a skill fired on turns that only shared a
  register with it. A plain greeting measured 0.9090 against a trigger that
  shared one word with it, and fired a skill that switches a device on.

  Set `match: semantic` on a page to keep the old behaviour for an intent that
  is genuinely said many ways. `memory.skills.min_sim` now applies to those
  pages only, and its default rises from 0.62 to 0.80.
- **A skill can belong to one person.** `for:` in the frontmatter takes
  `everyone` (the default), `members`, `guests`, a person id, or a list of ids.
  A skill the audience does not name never fires for that person, so two people
  can teach the same words and each get their own result. `for` is a match
  rule, not a permission: the wiki page itself stays readable.
- **A page of guidance is no longer a command.** `kind: convention` marks a
  page that holds standing guidance for a domain instead of a behaviour. It
  takes no triggers and never fires from a phrase; it is indexed as an ordinary
  wiki page and comes back through recall. Before this the only way to ship
  such a page was to invent trigger phrases for it, which fired it on turns
  nobody meant.
- **A trigger that cannot work is refused when the file is read.** A phrase of
  fewer than two words, or one that ends with an article, a preposition, or an
  auxiliary, is dropped with a warning that names the file and the reason.
  `announce` fires on every turn that mentions it, and `turn on the` is the
  front half of a sentence.
- **When two skills match, the more specific one wins.** The winner is the one
  whose trigger matched more words, so a page for one room is no longer beaten
  by a page for the whole house.
- **An entry with an empty credential is turned off, not retried.** A
  `${VAR:-}` reference in a container without the variable expands to nothing.
  Such an entry now reports `disabled` with one log line that names the key,
  and starts on the reload after you set the value. Before this an empty
  `Authorization: "Bearer ${TOKEN:-}"` header sent `Bearer ` with no token,
  which is an illegal HTTP header, and the connection failed and retried
  forever.
- **A stdio server's environment is explicit.** It gets its entry's own `env`
  block plus `PATH`, `HOME`, and `LANG`, and nothing else: no fleet token, no
  other entry's credential, no database URL. Its stderr now reaches the gateway
  log, one record per line, with the entry name.

### Fixed
- **An attachment turn no longer costs a failed tool call.** Channels named a
  file `attachments/YYYY/MM/x.jpg` and the files MCP refused that path, so
  Joshua had to guess again with `people/<id>/attachments/...`. Every container
  uses the second form now. A reply that carries a person's file no longer
  builds a path of `people/<id>/people/<id>/...`.
- **A picture sent in a group chat is a picture.** `read_file` returned
  metadata for it, and not the image.
- **A file that arrives through a webhook keeps the name the sender gave it.**

## 0.0.6 - 2026-09-07

### Upgrading from 0.0.5

The viewer is no longer read-only. A member who signs in can now edit any wiki
page, make a new one, and correct a journal entry. A guest still only reads,
and the agent still never reaches the viewer. Nothing in your config changes,
so check `viewer.users`: everybody listed there who is a member gains the
ability to write.

Nothing else needs an action. The voice channel is off until you add a
`channels.voice` section.

### Added

- **The viewer reads the journal as a feed, and a member writes in it.** The
  wiki tree keeps the journal as `YYYY/MM/DD` folders, which is the wrong shape
  for reading what happened, so the viewer has a `/journal` route: the newest
  month first, one section per day, the nightly page and then the entries, each
  with the people it names and whether the nightly run or the agent wrote it. A
  month link and a person filter narrow it, and a day has a page of its own with
  the nearest older and newer day.

  A member edits any wiki page through one editor, makes a new page from a
  folder, and corrects the text and the people of a journal entry. Frontmatter
  is kept: a journal entry keeps its day, its source and its file name, and any
  other page keeps its frontmatter byte for byte. The journal has one writer, so
  a new page under it is refused. Every write runs the same member check, CSRF
  token, origin check, size cap, atomic write and git commit as the delete. A
  guest sees no edit link and gets 403 on the routes.
- **The wiki is navigable in the viewer.** The home page was one flat list of
  every file, which stops working past a few dozen pages. It is now a tree of
  collapsible folders with a count on each, pages listed by title instead of
  path, and a page of its own for every folder at `/wiki/<folder>/`. A page
  beside a folder of the same name becomes that folder's index, so
  `/wiki/recipes/` shows the text of `recipes.md` and then what is under it. A
  page gets crumbs, its title in the browser tab, its frontmatter as one muted
  line, and the other pages of its folder below it. A small folder starts open;
  the shipped docs and a large folder start closed.
- **A voice channel.** Joshua answers a spoken turn on
  `POST /v1/voice/chat/completions`, which speaks the OpenAI chat-completions
  protocol. Any voice front end that can point an OpenAI client at a base URL
  works: a Pipecat bot, a phone app, a smart speaker, `curl`. Turn it on with a
  `channels.voice` block and a `voice` handle for each person. With no block
  there is no route.

  The front end says who is speaking and how sure it is. Joshua matches the name
  against `people[].handles.voice`, and it never adds a person from a voice
  turn. A name with a low score falls back to the identity the device already
  holds, so a short "yes" stays with the person who was already talking. A
  speaker Joshua cannot name reaches no agent and lands in
  `GET /admin/guard/recent`, so you can enroll them.

  With `thread: person` a member keeps one conversation as they move between
  devices, and the room reaches the agent as framing on each turn. A guest stays
  on their device.

  A spoken turn takes the session profile it would take anyway, and adds two
  more prompt files: `voice.md` (short replies, write speech and not a page,
  the `[DONE]` and `[IGNORE]` call markers) and
  `voice-speech.md` (punctuate for the ear, and write a number, a time or a
  symbol the way a person says it). Both ship in the core image, so none of it
  is yours to write. Nothing is taken away either: a guest on a call keeps the
  guest prompt. A voice session also runs on `channels.voice.model` and closes
  after `channels.voice.idle_ttl_s`. `docs/channels.md` and `docs/contracts.md`
  have the detail.

  Joshua cannot start a call yet. A reminder scheduled inside a voice
  conversation has no device to reach.

### Changed

- A session closes on the deadline its profile names, and on
  `core.session_idle_seconds` when the profile names none. Only voice names one
  today, so nothing else changes.

### Fixed

- The viewer renders a Markdown table. The preset it used has no table rule, so
  a table in a wiki page or a journal entry showed as pipe characters. Tables
  and strikethrough are on for both renderers, with no new dependency, and a
  table scrolls inside its own box on a narrow screen.

## 0.0.5 - 2026-09-05

### Upgrading from 0.0.4

Core moves your memory into one wiki the first time it starts, and then
re-embeds all of it. The journal moves to `wiki/journal/`, each profile to
`wiki/people/<id>.md`, and the shared profile to `wiki/people/everyone.md`.
Every path changes, so every document is indexed again. On a large corpus that
first start takes minutes and holds a CPU while it runs. Nothing is lost: the
move never writes over a file it finds already there, and running it twice
changes nothing.

Watch it with `GET /admin/kb/status`, which reports the pass, and
`GET /admin/journal/status`, which is new in this release.

### Added

- Enhancements: optional services that run beside the three containers, off
  by default. The `enhancements.yaml` file picks a choice for each slot.
  Otter Wiki is the first wiki frontend, and a custom slot lets you plug in
  your own container. See `docs/enhancements.md`.
- Joshua keeps `/data/wiki` as a git repository: it makes the first commit at
  launch, commits every write it makes, and commits anything else that
  changed at each start and at the nightly run, so a wiki frontend such as
  Otter Wiki never shows a page as not under version control. Set
  `wiki.git: false` to leave git to a frontend or a sync tool. The custom wiki
  enhancement example is a container that pushes to a remote.
- A gateway tool, `write_journal_entry`, lets the agent write a dated entry
  to Joshua's own journal in the wiki.
- `GET /admin/journal/status` reports today's journal: the entries Joshua
  wrote during the day, whether today has a nightly page, and the summary of
  the last nightly run. `docs/operations.md` shows how to read it.
- A push to any branch, `main` included, builds the three images and tags
  them with the commit SHA and `branch-<name>`, so `branch-main` always names
  the newest commit on `main`. An ArgoCD Application can follow `main` or a
  branch and run each push. A weekly job removes the old SHA-tagged builds and
  untagged layers from the registry.

### Changed

- Joshua keeps its whole memory in one wiki. The journal, every profile and
  the shared profile all live under `wiki/`, so whatever frontend you point at
  the wiki shows all of it, and one search covers the lot. See "Upgrading"
  above for what the first start does.
- The journal is Joshua's episodic memory, and only that. There is one journal
  and it belongs to Joshua: a person is named in an entry, never the owner of
  one. It holds what happened, it is selective, and it reaches a turn only
  through search, either the confidence-gated injection or the `search_memory`
  tool. No turn gets a journal block in the system prompt any more, so
  `memory.recent_posts` and `memory.recent_max_chars` still load and nothing
  reads them. The nightly page leaves out anything that is somebody operating
  Joshua: a request to do a thing is not a life update, however it is worded,
  and that holds when the tool keeps a record of its own. Ask Joshua to write
  in a journal, a blog, or notes of your own on another service, and that
  entry lives there rather than being copied here. A durable trait stays in
  your profile.
- Joshua has one test for which store a thing goes in. If keeping it current
  means rewriting a page, it is the wiki; if it means adding another dated
  line, it is the journal, one entry each time. Joshua no longer invents a
  wiki page that grows dated rows, so a reading, a dose, a score, a meal or a
  mood lands in the journal where it belongs. Asking Joshua to keep track of
  something states a preference, which the profile holds; it does not make a
  page. Ask for the page and Joshua makes it: a history you would rather read
  in one place, a changelog for example, is yours to ask for. See
  `docs/memory.md`.
- `joshua.yaml` starts the roster; Joshua keeps it. The file is where you name
  the first person, so somebody can talk to Joshua at all. Everyone after that
  is enrolled through Joshua, and the people file at `/data/people.yaml` holds
  them. An edit to an enrolled person in `joshua.yaml` therefore changes
  nothing, which `docs/config.md` now says plainly, along with how to hand a
  person back to the file.
- `people[].journal` (`auto`, `ask`, `off`) is unchanged in behaviour, and is
  now documented for what it does: whether Joshua's journal may record that
  person. It never gave a person a journal of their own.
- `wiki/Home.md` is the wiki's front page. Core makes it once, from a
  template, and never rewrites it. Core also removes the old `wiki/README.md`
  and `shared/README.md`, which `Home.md` replaces.
- The shipped docs folder in the wiki is `wiki/joshua-docs/`, so its name says
  what it is. Core renames an existing `wiki/joshua/` at start.
- The agent's file tools, the viewer and the index ignore any dot-file or
  dot-directory at any depth, so a wiki frontend can keep its own state in the
  folder.
- The prompts Joshua ships describe only what this repository builds, and
  assume nothing about what any person keeps. Identity no longer offers voice
  as a way to know who is speaking, because there is no voice channel; the
  guest prompt describes what a guest reaches in terms of the tool list rather
  than naming systems that are not here; and the rule that decides where a
  thing is stored names the shape of the thing, not its subject.

### Fixed

- A turn slower than 15 seconds no longer dies part way through. One timeout
  covered both an ordinary request and a streamed turn, and a streamed turn
  holds its body open for as long as the turn runs, so the number was a cap on
  how long the agent could think. `make chat` failed on a cold first turn,
  which is the first thing anybody runs after `make up`. A request keeps its
  deadline; a stream reads without one and keeps the connect timeout, so an
  unreachable container still fails in seconds.
- The chart README told you to install from an OCI registry that holds no
  chart, so the command answered 403. The chart ships as a package attached to
  each release; the README now takes it from there, or from a checkout.
- The journal has one writer. `write_file` and `rename_file` refuse a path
  under `wiki/journal/`, so an entry cannot land outside its day folder
  without the frontmatter the index reads, and the nightly page cannot be
  overwritten or renamed by the agent. Use `write_journal_entry`.
- A turn that wrote a journal entry reported writing nothing. The turn log
  names the files a turn wrote, and it built that list from each call's `path`
  argument, which `write_journal_entry` does not have: the tool takes a slug
  and the gateway places the file. The entry landed correctly, and only the
  record of it was missing.
- `docs/architecture.md`, `docs/quickstart.md` and `docs/config.md` described
  the layout from before the journal moved into the wiki. They now describe
  the wiki.

## 0.0.4 - 2026-09-01

### Fixed

- A journal post goes to the person the turn belongs to. The agent was told
  to write `people/<id>/blog/`, and the only name it held was the display
  name, so on every installation where the two differ it made a second
  directory beside the real one and the journal split in two. The files MCP
  now refuses a person segment that names nobody, and the prompt carries the
  person id.
- A turn with no person, such as a group chat, writes no journal post. Such a
  turn reached a person's directory before. It still reads and writes the
  wiki.
- One document that the index cannot hold no longer fails on every pass. A
  directory below `/data/people/` that names no person made the same failure
  every 60 seconds, about 2,880 times a day, and the search index never
  settled. The indexer now walks only the people on the roster, and holds a
  document that fails for a fault of its own until its content changes.
  `GET /admin/kb/status` reports such a document by path, under
  `unindexable`.
- `/readyz` no longer reports a data volume as not writable when it is. The
  check read the mode bits, and on an NFS export the server decides, so the
  bits lie. It now writes a probe file and removes it.
- A document that the index cannot hold anything for is no longer re-processed
  every 60 seconds. A zero-byte journal post produced no chunk, so no row, and
  the diff reads a missing row as a changed document: the pass reported
  `indexed: 1, failed: 0` forever and wrote nothing. Such a document is now
  held until its content changes, and `GET /admin/kb/status` names it under
  `empty`.
- One Telegram poll error no longer makes `/readyz` report the channel as down
  until somebody sends a message. The recovery flag was set from a delivered
  update alone, so an idle bot stayed false after a transient fault. A poll
  error now stands for 90 seconds and a continuing fault keeps renewing it, so
  the report follows the fault.
- `core` answers `/readyz` with `503` when it is not ready, and `200` when it
  is. Every answer was a `200` before, so a Kubernetes readiness probe could
  never fail and a core with no database still took traffic. `channels` and
  `gateway` keep the `200`: their `ok` reports an upstream, not whether the
  container can serve, and a failing poller or a failed MCP connection must
  not take the pod out of its Service.
- `make restore` works on a fresh checkout. It stopped with `invalid container
  name or ID: value is empty`, because it read the volume name from a
  container that no image could make. It now pulls the image, finds the volume
  before it changes anything, and says what to do when it cannot.
- `make restore` refuses to restore into a second stack while `.env` holds a
  live `TELEGRAM_BOT_TOKEN`. Two stacks poll one bot, Telegram gives each
  message to whichever poller asks first, and the rehearsal stack takes the
  real messages. `KEEP_TOKEN=1` says you mean it. `operations.md` has the two
  rules for a safe rehearsal.
- `docker compose --profile viewer up` starts. The viewer and `core` both
  published host port 8081, so the viewer could not bind. The viewer now
  listens on `127.0.0.1:8082`, on the loopback address like `core`, and no
  longer on every interface.

### Added

- The Helm chart can run the viewer. It is off by default; `viewer.enabled`
  puts the pod in the cluster, and `viewer.enabled` inside your `config` lets
  the process start. It gets its own Service, its own optional ingress, and
  one Secret that holds `VIEWER_PASSWORDS`. It holds no fleet token and no
  upstream credential.
- The viewer is named in `README.md` and in `docs/quickstart.md`. It shipped
  with neither, so nobody knew it was there.

### Changed

- The viewer takes every password from one variable, `VIEWER_PASSWORDS` in
  `.env`, as a comma-separated list of `<person-id>=<value>` pairs. The
  compose file passed `VIEWER_PW_ALEX`, which names the person in the example
  config, so a real installation had to edit a tracked file to sign in. Put
  your passwords in `VIEWER_PASSWORDS`, and leave the values in
  `viewer.users` empty. A `viewer.users` entry that carries a reference still
  works.

## 0.0.3 - 2026-08-30

### Added

- An image for arm64, next to the one for amd64. A `docker pull` on an Apple
  Silicon Mac, a Raspberry Pi, or an ARM server failed before this, so Joshua
  did not install on such a machine at all.
- `make init-env` writes `JOSHUA_VERSION` into `.env`, so an installation
  stays on one release. A `git pull` does not move it. To update, put the
  version you want in `.env`, then `make pull && make up`.
- `make up-dev` builds the three images from your checkout and starts those,
  for a person who changes the code. A build is tagged
  `joshua-ai-<component>:dev`, so it never replaces a release on your machine.
- `POST /admin/kb/reindex` takes `{"background": true}`. It answers 202 at
  once and runs the pass after.
- `make restore FROM=… NO_EMBED=1` restores without a rebuild of the search
  index. The indexer takes the restored files on its next pass, and the
  nightly run covers the rest.

### Changed

- `make up` starts the released images from the GitHub container registry.
  Nothing is built, so a first start takes minutes and not tens of minutes.
- A restore no longer waits for the search index. It starts the rebuild and
  returns. The pass makes an embedding for every document and holds a CPU
  until it ends, and the restore says so.

### Fixed

- The restore sent the fleet token to `curl` as an argument, where every user
  on the machine reads it in `ps`. It goes on stdin now.
- A build from a checkout was tagged with the release reference, so it
  replaced the release on your machine and `make up` then ran the build.

## 0.0.1 - 2026-08-30

The first release.

### Added

- `GET /admin/chats/unconfigured` on `channels` lists the group chats that
  `joshua.yaml` does not hold, so you can read a group's chat id without
  stopping Joshua. It holds no message text and no sender handle.
- A Helm chart at `charts/joshua`, to run the three containers on Kubernetes.
  One values file holds the images, the whole `joshua.yaml`, the names of the
  Secrets each container reads, the storage, and the optional Ingress. The
  chart makes no Secret.
- The chart can bind storage you made yourself, for a site that keeps volumes
  outside the deployment tool. `persistence.existingClaim` binds the `/data`
  claim and `postgres.storage.existingClaim` binds the database claim.
- A release workflow. A `v*` tag builds the three images to the GitHub
  container registry, packages the chart, and attaches both to a release.
- Three containers, `channels`, `core`, and `gateway`, from one repository and
  one `docker-compose.yml`. `make up` starts them.
- Channels: Telegram, iMessage through BlueBubbles, a webhook route for
  external events, and the terminal (`make chat`).
- An inbound guard: only the people and groups in `joshua.yaml` get a reply. A
  stranger gets nothing.
- Attachments: files arrive with a timestamped name.
- Memory: notes and a journal per person on one volume, a shared profile, a
  retrieval index, and a nightly reflection that writes the day's post and
  updates each profile.
- On-demand journal posts during a conversation, with a per-person `journal`
  setting (`auto`, `ask`, `off`).
- Taught skills: a member teaches a "when I say X, do Y" behavior by writing
  `wiki/skills/<slug>.md` with trigger phrases. A matching message fires the
  instructions. Tune it under `memory.skills`.
- An optional read-only Memos source for the index.
- Reminders and scheduled tasks.
- A gateway that holds every upstream credential and applies a per-person tool
  policy. The `files` MCP runs inside it.
- `joshua chat`: the terminal is a channel, with an outbox for replies that
  arrive while the person is away.
- The documentation ships into `wiki/joshua/` at each start, so a person
  can ask Joshua how to use it.
- A read-only web viewer for the wiki and a person's own files. A person signs
  in with a password and reads the wiki, their profile, their journal, their
  attachments, and the shared profile. A member can delete a wiki page, which
  moves it to `wiki/.trash/`. Off by default; turn it on under `viewer` and
  start it with `docker compose --profile viewer up`. See
  [operations.md](operations.md#the-viewer).

### Changed

- **The corpus is shared.** The wiki, every journal, and every attachment are
  one body of knowledge that backs Joshua's memory. A member reads and writes
  it; a guest reads it. A person's journal is no longer private to them, and
  `people/<id>/` is now provenance, not a boundary. Something that must stay
  private lives outside Joshua, behind an MCP server.
- A group chat can write the wiki. It writes when every handle in the group's
  `members` names a member; one guest in the chat holds it to read-only for
  everybody. Core logs a `group role` line at start so you can see why.
- **Breaking, for a taught skill.** The file paths changed: `blog/x.md` is now
  `people/<id>/blog/x.md`, `profile.md` is `people/<id>/profile.md`, and
  `attachments/…` is `people/<id>/attachments/…`. A skill in `wiki/skills/`
  that names an old path must be edited. The old names return an error that
  says the new one.
- Only the container that owns a secret gets it. `joshua.yaml` references the
  channel credentials as `${VAR:-}`, so Compose no longer passes them to
  `core` and `gateway`. `make init-env` sets `.env` to mode 600.

- `make validate` runs in the core image. The host needs Docker and nothing
  else, which matches the quickstart.

- One wiki. `/data/wiki/` is the wiki for everyone who uses Joshua. A member
  writes it, a guest reads it. The per-person `people/<id>/wiki/` is gone; at
  the next start, core moves its pages to `wiki/<id>/`. The documentation
  ships to `wiki/joshua/` instead of `shared/repo-docs/`. `shared/` is
  read-only through the files tool, and the `shared_write` option is gone.

- `joshua.yaml` has no `household` section. `name`, `timezone`, `people`, and
  `groups` are top-level keys.
- The shared profile is `shared/profile.md`.
- The Kubernetes manifests left this repository. The public deployment path is
  Docker Compose.

### Fixed

- The `written` field of a `turn done` log line now names the files a turn
  actually wrote. A write the gateway refused was reported as a write, so a
  chat that saved nothing could log three file names. The line also carries
  `write_failed` when a write did not land.
- One turn now fires one taught skill. A second skill with a similar trigger no
  longer rides in beside the skill you asked for. Set `memory.skills.top_k` in
  `joshua.yaml` to fire more than one.
- Memory: a model cache directory that core cannot write no longer stops every
  embedding. Core logs a warning that names the directory and uses a temporary
  one, so the memory and the taught skills keep working while you correct the
  mount.
- `GET /readyz` and `GET /admin/kb/status` now report the embedding model as
  `embed`. A lost model does not hold `readyz` down, because core still answers.
  It is now visible instead of silent.
- A lost embedding model writes one log line for each index pass, and not one
  for each document.
- Channels writes a `<file>.meta.json` metadata file next to a stored
  attachment. It holds the sender's filename, so the files tool reports the
  original name. Channels logs one line for each write, so the logs show
  whether the file landed. A stored attachment with no metadata file still
  appears in the files list, without the original name. The retention sweep
  removes the metadata file
  with its file and counts attachments only.

- `/readyz` clears the Telegram channel after a transient poll error. A
  Conflict or a network blip that recovers no longer holds `ok: false` until
  the pod restarts. The next good poll reports `ok: true`, and the error count
  stays for the operator.
