# Channels

A channel is a way to talk to Joshua. `channels` is the container that owns
every channel. It receives a message, checks the sender, stores any file, and
posts one turn to `core`. It also sends every reply.

Four channels exist: the terminal, Telegram, iMessage, and webhooks. The
terminal is always on. Telegram and iMessage are on when their credential is
set. A channel with an empty credential is off, and the stack starts without it.

## Who gets a reply

Every channel runs the same guard before a message reaches `core`:

1. The guard looks up the sender's handle in `people`. A known handle gives the
   person.
2. The guard admits an unknown sender in a group chat when the chat is in
   `groups` and the group either lists no `members` or lists this handle. The turn then
   runs as the group, with no person.
3. The guard refuses any other unknown sender with the reason `unknown_sender`. A
   direct message never falls back to a group.
4. Each handle gets `channels.limits.per_handle_per_minute` messages per
   minute. Over that, the guard refuses with `rate_limited`.

A refused message gets no reply by default. Set `unknown_sender: reply` on
Telegram or iMessage to answer a stranger once with "Sorry, I do not know you,
so I cannot help here." A rate-limited sender never gets a reply.

`GET /admin/guard/stats` and `GET /admin/guard/recent` show the refusals. See
`operations.md`.

## The terminal

The terminal needs no credential. A person who can run a command on the host
is already trusted, so `people[].handles` needs no `cli` entry. The person id
is the handle.

```
make chat AS=<id>              open a session
make chat AS=<id> MSG="hello"  send one message and stop
```

`make chat` runs `python -m joshua_core chat` inside the `core` container. The
client posts each line to `channels` at `POST /v1/cli/turns/stream` with the
`laptop` token. The guard runs on it like on any other channel. The guard refuses an id
that is not in `people`.

A reply that arrives while nobody is in a session, for example a reminder,
goes to `people/<id>/cli/outbox.jsonl`. The next session shows each line first,
as `[joshua earlier, <time>] <text>`, and marks it read.

## Telegram

### Make a bot

1. In Telegram, open `@BotFather`.
2. Send `/newbot`. Give the bot a name and a username.
3. Copy the token that BotFather shows.
4. Put the token in `.env` on the `TELEGRAM_BOT_TOKEN` line.
5. Run `make up` again.

The `telegram` block in `joshua.yaml` reads the token with
`${TELEGRAM_BOT_TOKEN}`. Keep the block. Change nothing else in it to start.

### Add your handle

Joshua answers only the people in `joshua.yaml`. Telegram identifies a person by
a numeric user id, not by a username.

1. Send any message to the bot.
2. Run `curl -s -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP"
   localhost:8080/admin/guard/recent`. The refusal shows your id in `address`.
3. Put the id in `joshua.yaml`:

```yaml
people:
  - id: sam
    name: Sam
    role: member
    handles:
      telegram: "123456789"
```

4. Run `docker compose restart core channels`.

### Adding someone from chat

You do not need the terminal, and you do not need their handle. When Joshua
turns someone away, a member can enroll them by name:

```
you>    Mia just messaged you. Add her as a member.
joshua> One handle was turned away recently: telegram 123456789.
        Confirm that is Mia and I will add her.
you>    Go ahead and add her.
joshua> Mia is added as a member on telegram 123456789.
```

Mia reaches Joshua on her next message. Nothing restarts.

Joshua never reads a refused message. The message stops at the guard. Joshua
reads only the handle, the channel, and the reason. It asks you to confirm the
handle before it writes anything.

Only a member can do this. Joshua tells a guest that the tool is not available
to them.

If you already know the handle, name it and skip the question: "add my friend
Ana, her Telegram id is 987654321".

### What each person sees

| | What they see |
|---|---|
| The unknown sender | Nothing. `unknown_sender: drop` sends no reply. |
| A member in chat | Ask Joshua who was turned away, and it names the handle. |
| An operator | One `inbound refused` line in the log, with the channel, the reason, and the handle. |

### When a restart is needed

| Change | Takes effect |
|---|---|
| Joshua adds a person with `add_user` | The next message. Nothing restarts. |
| `people remove --id <id>` | The next message. Nothing restarts. |
| You edit `people` in `joshua.yaml` | After `docker compose restart core channels`. |

A change through chat writes `/data/people.yaml`, and every container re-reads
that file when it changes. `joshua.yaml` is read once at start, so an edit there
needs the restart above.

### Group chats

To let Joshua take part in a group chat, add the group to `joshua.yaml`:

```yaml
groups:
  - id: everyone
    channel: telegram
    chat_id: "-1001234567890"
    members:
      - sam
      - ana
```

`chat_id` is the numeric id of the group. Add the bot to the group, send a
message, and read the id from `/admin/guard/recent` in the same way as above. A
group with no `members` list admits every sender in that chat.

In a group, Joshua answers as the group. It reads the shared profile and the
shared files, and it does not read anyone's private notes.

### Privacy mode

By default, a Telegram bot receives only the group messages that mention it by
name, reply to it, or start with a command. Every other group message never
reaches the bot. Telegram controls this with the bot's **privacy mode**.

If you want Joshua to see every message in a group:

1. In Telegram, open `@BotFather`.
2. Send `/setprivacy`.
3. Select the bot.
4. Select **Disable**.
5. Remove the bot from the group, then add it again. Telegram applies the
   setting to a group only when the bot joins it.

Privacy mode is a Telegram account setting. It is not a value in `joshua.yaml`,
and Joshua cannot change it.

Privacy mode changes what the bot *receives*. It does not change what Joshua
*acts on*. The guard still refuses every group that is not in `groups`, and
every sender that the group does not admit. The two controls are independent.
A stranger who adds the bot to their own group gets nothing, with privacy mode
on or off.

### Link previews

Joshua sends every Telegram message with link previews off. A preview makes
Telegram's servers fetch the URL. Joshua never triggers that fetch.

### Files

A photo or a document from Telegram goes through the attachment pipeline. See
"Attachments" below.

## iMessage

iMessage needs a Mac that runs [BlueBubbles](https://bluebubbles.app). Joshua
talks to the BlueBubbles server over HTTP. BlueBubbles calls Joshua back on a
webhook.

### Set up

1. Install the BlueBubbles server on the Mac and note its URL and password.
2. Put the password in `.env` on the `BLUEBUBBLES_PASSWORD` line.
3. Make a long random string and put it in `.env` on the
   `IMESSAGE_WEBHOOK_SECRET` line. For example: `openssl rand -hex 32`.
4. In `joshua.yaml`, set `channels.imessage.bluebubbles_url` to the server URL.
5. Run `make up` again.
6. In BlueBubbles, add a webhook that points at
   `http://<joshua-host>:8080/webhook/imessage/<the secret>` for the event
   `new-message`.

The webhook has no bearer token, because BlueBubbles cannot send one. The
secret in the path is the credential. A request with a wrong secret gets a 404,
not a 401, so a prober cannot confirm that the route exists.

### Handles and groups

An iMessage handle is a phone number in E.164 form (`+15551234567`) or a
lower-case email address. A group chat id is the BlueBubbles chat GUID, for
example `iMessage;+;chat100000000000000001`. Read both from
`/admin/guard/recent` after a first message, in the same way as for Telegram.

```yaml
people:
  - id: sam
    name: Sam
    handles:
      imessage: "+15551234567"
groups:
  - id: everyone
    channel: imessage
    chat_id: "iMessage;+;chat100000000000000001"
```

### Knobs

| Setting | Default | What it does |
|---|---|---|
| `coalesce_window_s` | `2.0` | Merges a burst of messages from one sender into one turn. `0` sends each message alone. |
| `stale_max_age_s` | `900.0` | Drops a webhook older than this. It stops a replay. `0` accepts any age. |
| `reconcile_interval_s` | `60` | Polls BlueBubbles this often for a message the webhook missed. `0` turns the poll off. |
| `reconcile_lookback_s` | `300` | How far back the first poll looks at start. |
| `send_chunk_chars` | `4000` | Splits a long reply into messages of this size. |
| `unknown_sender` | `drop` | `reply` answers a stranger once. |

`/readyz` reports whether BlueBubbles answers a ping. A Mac that sleeps makes
that `false` for a while. Joshua does not stop for it, and the poll picks up
the messages when the Mac wakes.

## Webhooks

An external system can send Joshua an event. `channels` accepts it at
`POST /v1/events` from the identities in `channels.webhooks.allowed_callers`
(default `laptop` and `ci`). Each caller needs its own `JOSHUA_TOKEN_<NAME>`.

Two shapes exist:

- A **named event** carries `event_type` and an optional `payload`. Core runs
  the skill file `skills/<event_type>.md`. See `events.md`.
- A **message** carries `destination` and `text`. Core runs the text as one
  turn and delivers the reply to the destination. With `verbatim: true`,
  channels sends the text as it is and no model runs.

`destination` is a logical name from `channels.destinations`, such as
`everyone`, or a direct reference such as `telegram:dm:sam`. `image_urls` may
list `http` or `https` images. Channels downloads them into the attachment
pipeline. Channels refuses the `file:` and `data:` schemes.

Example:

```
curl -X POST localhost:8080/v1/events \
  -H "Authorization: Bearer $JOSHUA_TOKEN_LAPTOP" \
  -H "Content-Type: application/json" \
  -d '{"destination": "everyone", "text": "The garage door is open.", "verbatim": true}'
```

## Destinations

`channels.destinations` maps a logical name to a channel reference:

```yaml
channels:
  destinations:
    everyone: imessage:group:everyone
    sam: telegram:dm:sam
```

A reference is `<channel>:group:<group id>`, `<channel>:dm:<person id>`, or
`<channel>:<platform chat id>`. Skills, events, and scheduled tasks use the
logical name, so a move from one platform to another changes one line.

## Attachments

Every file that arrives goes through one pipeline:

1. Channels reads the file type from the bytes. It does not trust the file
   name or the platform.
2. It accepts images, PDF, and plain text. It skips any other type.
3. A file over `channels.limits.max_attachment_bytes` (default 25 MiB) is
   skipped. The message still goes through.
4. It converts an image to JPEG when needed and scales it to at most 1568 px
   on the long edge and about 600 KB.
5. It stores the file as `YYYY-MM-DD-HHMMSS-<name>.<ext>` in the configured
   timezone. A file from a direct message goes to
   `people/<id>/attachments/YYYY/MM/`. A file from a group goes to
   `shared/attachments/<group>/YYYY/MM/`.

The agent reads a stored file with the `files` tool. It can cite a file in a
journal post by its `people/<id>/attachments/…` path.

`channels.limits.attachment_retention_days` (default `365`) deletes stored
files older than that. `0` keeps every file. `keep_originals: true` also
stores the untouched original next to the stored file.

## Limits

| Setting | Default | What it does |
|---|---|---|
| `max_text_chars` | `8000` | A longer message is cut to this length. |
| `max_attachment_bytes` | `26214400` | A larger file is skipped. |
| `per_handle_per_minute` | `20` | Messages one handle may send per minute. |
| `attachment_retention_days` | `365` | Age at which a stored file is deleted. |
| `keep_originals` | `false` | Also keep the original bytes of an image. |
