# Changelog

Joshua v4 has no numbered release yet. This file starts with the state of
`main` on 2026-08-28. From here on, each entry names what changed for the
person who runs Joshua.

## Unreleased

### Added

- `GET /admin/chats/unconfigured` on `channels` lists the group chats that
  `joshua.yaml` does not hold, so you can read a group's chat id without
  stopping Joshua. It holds no message text and no sender handle.
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
