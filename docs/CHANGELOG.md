# Changelog

Each entry names what changed for the person who runs Joshua. The releases,
with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-ai/releases>.

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
