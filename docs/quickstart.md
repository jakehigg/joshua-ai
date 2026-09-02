# Quickstart

This page takes you from nothing to a conversation with Joshua in a terminal.
It takes about fifteen minutes.

After the last step, ask Joshua what to do next. Joshua holds its own
documentation and can search it. It can tell you how to connect Telegram, how to
add a person, and how it remembers.

## What you need

- Docker, with the Compose plugin.
- A Claude subscription, for the agent.
- Network access to `huggingface.co` on the first start. Core downloads its
  embedding model one time. The model is about 65 MB.

You do not need Python. Every command below runs in Docker.

You do not need a Telegram bot. The terminal is a channel of its own.

## 1. Get the code

```
git clone https://github.com/jakehigg/joshua-ai.git
cd joshua-ai
```

## 2. Make the secrets file

```
make init-env
```

This command copies `.env.example` to `.env`. It writes a database password,
one token for each container, and the version of Joshua you run. Keep this
file. Do not commit it.

Your installation stays on that version until you change `JOSHUA_VERSION` in
`.env`. A later `git pull` does not move it.

## 3. Add your Claude token

On a machine that has the `claude` command and your subscription, run:

```
claude setup-token
```

Copy the token. Open `.env` and put it on the `CLAUDE_CODE_OAUTH_TOKEN` line:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-...
```

If you want to try the stack with no token, put `AGENT_BACKEND=stub` in `.env`
instead. Joshua then answers with fixed text. Everything else works.

## 4. Describe yourself

```
cp joshua.example.yaml joshua.yaml
```

Open `joshua.yaml`. Set the name, the timezone, and the people. One person is
enough:

```yaml
name: Home
timezone: America/New_York
people:
  - id: sam
    name: Sam
    role: member
```

`id` is the name you use when you talk to Joshua. Use lower-case letters,
numbers, and hyphens only. `role` is `member` or `guest`. A member writes
the wiki and can add people. A guest reads the wiki and cannot write it.

Leave the `channels` section as it is. A channel with no credential is off.
Telegram and iMessage stay off until you add their tokens.

Check the file:

```
make validate
```

The command prints `ok: joshua.yaml` and the number of people it found. The
first run pulls the core image, so it takes a few minutes. `make up` in the
next step uses the same image. If you skip this step, `make up` runs the same
check and stops on the same error.

## 5. Start Joshua

```
make up
```

Compose starts four containers: `postgres`, `gateway`, `core`, and `channels`.
The images come from the GitHub container registry, so nothing is built. The
first start takes a few minutes, because it pulls the images and downloads the
embedding model.

Make sure that everything runs:

```
make ps
curl localhost:8080/healthz
```

`curl` prints `{"ok": true}` when Joshua is ready.

## 6. Talk to Joshua

```
make chat AS=sam
```

Use your own `id` from `joshua.yaml`. Type a message and press Enter. Type
`/exit` to stop.

To send one message and stop:

```
make chat AS=sam MSG="hello"
```

The terminal is a real channel. Your message passes the same checks that a
Telegram message passes. Joshua refuses an id that is not in `joshua.yaml`.

When Joshua sent you something while you were away, for example a reminder, the
session shows it first. Each line starts with `[joshua earlier, <time>]`.

## 7. Add the other people

Joshua answers only the people it knows. Anyone else is turned away in silence.

You do not need to edit a file. Ask the person to message your bot once, then
tell Joshua:

```
Mia just messaged you. Add her as a member.
```

Joshua names the handle it turned away and asks you to confirm. After that, her
next message reaches Joshua, and nothing restarts.

`member` sees everything and can add people. `guest` sees less. See
[channels.md](channels.md#adding-someone-from-chat).

## 8. Ask Joshua what to do next

Joshua holds its own documentation and can search it. Ask it:

```
make chat AS=sam
you> how do I connect Telegram?
you> how do I add another person?
you> how do I add a tool?
you> where do you keep my notes?
you> how do you decide what to remember?
you> how do I update you?
```

This is the fastest way to learn the rest. Joshua reads the same pages that you
find under `docs/`:

- [channels.md](channels.md): Telegram, iMessage, the terminal, and webhooks.
- [config.md](config.md): every setting in `joshua.yaml`, and how to add a
  person or a tool.
- [memory.md](memory.md): notes, journal, profiles, and retrieval.
- [operations.md](operations.md): logs, health, updates, backup, and the
  viewer, which is a read-only web page for your notes.
- [architecture.md](architecture.md) and [security.md](security.md): how the
  three containers divide the work, and what the agent cannot do.

## Day-to-day commands

```
make chat AS=<id>   talk to Joshua
make logs           follow the logs
make ps             show the container state
make down           stop, and keep the data
make nuke           stop, and delete the data (it asks first)
```

`make down` keeps your database and your files. `make nuke` deletes them.

To read your notes in a browser instead of the terminal, turn on the viewer.
It is a read-only web page for the wiki, your profile, your journal, and your
attachments. It is off until you turn it on, and it listens on the loopback
address only. See [operations.md](operations.md#the-viewer).

## If something is wrong

| What you see | What to do |
|---|---|
| `make up` stops on a config error | The error names the line. Fix `joshua.yaml`, then run `make validate`. |
| `Joshua does not know "<id>"` | The id is not in `joshua.yaml`. Add the person, then run `docker compose restart core`. |
| `channels refused the message (rate_limited)` | Wait one minute. The limit is `channels.limits.per_handle_per_minute`. |
| A container restarts again and again | `make logs` prints the reason on the first line. |
| An answer is wrong or out of date | Read what Joshua found: `curl -s -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP" 127.0.0.1:8081/admin/kb/events` |

## Where your data lives

Everything Joshua writes is Markdown on one volume:

```
/data/people/<id>/profile.md      who you are
/data/people/<id>/blog/           what happened, one file per post
/data/wiki/                       the wiki: reference pages for everyone
/data/wiki/joshua/                Joshua's own documentation
/data/shared/                     the shared profile and group-chat files
```

Read them with `docker compose exec core ls /data/wiki`.
[data-layout.md](data-layout.md) describes the whole tree.
