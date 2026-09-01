# Changelog

Each entry names what changed for the person who runs Joshua. The releases,
with the images and the packaged chart, are at
<https://github.com/jakehigg/joshua-ai/releases>.

## Unreleased

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
- `/readyz` answers `503` when the container is not ready, and `200` when it
  is. Every answer was a `200` before, so a Kubernetes readiness probe could
  never fail and a broken container still took traffic. Read the note in
  `operations.md` before you add a second replica.
- `make restore` works on a fresh checkout. It stopped with `invalid container
  name or ID: value is empty`, because it read the volume name from a
  container that no image could make. It now pulls the image, finds the volume
  before it changes anything, and says what to do when it cannot.
- `make restore` refuses to restore into a second stack while `.env` holds a
  live `TELEGRAM_BOT_TOKEN`. Two stacks poll one bot, Telegram gives each
  message to whichever poller asks first, and the rehearsal stack takes the
  real messages. `KEEP_TOKEN=1` says you mean it. `operations.md` has the two
  rules for a safe rehearsal.

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
- Channels writes a `<file>.meta.json` sidecar next to a stored attachment. The
  sidecar holds the sender's filename, so the files tool reports the original
  name. Channels logs one line for each sidecar write, so the logs show whether
  the sidecar landed. A stored attachment with no sidecar still appears in the
  files list, without the original name. The retention sweep removes the sidecar
  with its file and counts attachments only.

- `/readyz` clears the Telegram channel after a transient poll error. A
  Conflict or a network blip that recovers no longer holds `ok: false` until
  the pod restarts. The next good poll reports `ok: true`, and the error count
  stays for the operator.
