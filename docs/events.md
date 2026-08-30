# Events and event-skills

An external system triggers Joshua through the channels webhook adapter. Channels
verifies the caller, stores any files, and posts a `TurnEvent` with `kind: event`
to core `POST /v1/turns`. Core routes the event. It never downloads files and
never checks webhook auth. Channels owns both.

Two event shapes arrive.

## Named event → skill

An event skill is not a taught skill. An event skill lives under
`<prompts_dir>/skills/`, ships with the image, and fires on a webhook event. A
taught skill lives under `wiki/skills/`, a person teaches it in chat, and a
phrase in a message fires it — see [memory.md](memory.md#taught-skills).

A named event carries an `event_type`. Core looks it up in the skill registry.
A skill is one Markdown file:

```
<prompts_dir>/skills/<event_type>.md
```

The file has YAML frontmatter and a body:

```markdown
---
destination: everyone
framing: optional override text
---
The instruction the model runs when this event fires.
```

- `destination` (required) is the channel the reply goes to. Channels resolves the
  name, so use a logical name such as `everyone`, not a platform id.
- `framing` (optional) replaces the default skill framing.

When a matching file exists, core:

1. Reads the body and appends the event `payload` as a fenced JSON block.
2. Asks channels to resolve `destination` to a channel.
3. Runs one turn on that channel in the background.
4. Delivers the reply to `destination`. A reply of `[IGNORE]` delivers nothing.

An unknown `event_type` returns `200 {"ignored": true}` and runs no turn.

`event_type` must be a slug (`^[a-z0-9][a-z0-9_-]{0,63}$`), so a caller cannot
reach outside the skills directory.

### Add a skill

Put a file in `skills/`. You need no code change, no restart, and no config
reload. Core reads the file when the event fires. The kernel ships zero skills.

Example `skills/front_door.md`:

```markdown
---
destination: everyone
---
The front door opened. If it is late, send a short heads-up to everyone. Stay
silent otherwise.
```

## Channel-addressed event

A channel-addressed event carries a `channel` and `text` and no `event_type`.

- With `options.verbatim: true`, core writes one `inject` transcript row and
  returns. No model runs and core does not deliver, because channels already
  delivered `text` to the channel.
- Without `verbatim`, core runs `text` as one turn framed as an injection (see
  `prompts/builtin/event.md`), then delivers the reply.

## Request fields

`kind: event` turns use these `TurnEvent` fields:

| field | use |
| --- | --- |
| `event_type` | names a skill (named event) |
| `payload` | event data, appended to the skill prompt as JSON |
| `channel` | the destination channel (channel-addressed event) |
| `text` | the message (channel-addressed event) |
| `options.verbatim` | record `text` with no model call; channels delivered it |
| `attachments` | files that channels already stored |
