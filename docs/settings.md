# The settings of the instance

A setting of the instance lives in the database, and the owner writes it in
the viewer under Admin → Instance. It is not a line of `joshua.yaml` and not
an environment variable, so it needs no restart of a container and no edit of
a file on the host. A setting that holds a credential is a secret: it is
encrypted at rest, and no route ever answers its value.

This page is the guide to the store: what it holds, how a key is named, how a
secret is encrypted, how to generate the one key the encryption needs, and
what happens when that key is missing.

[contracts.md](contracts.md#the-settings-routes) has the two routes, and
[config.md](config.md) has `joshua.yaml`, which this store does not replace.

## What the store is

Core owns the database, so it owns the settings: one row of the `settings`
table per setting.

| Column | What it holds |
|---|---|
| `key` | the name of the setting, such as `push.vapid_public_key` |
| `value` | the text of a plain setting, or the ciphertext of a secret one |
| `is_secret` | whether `value` is encrypted |
| `updated_at` | when the row last changed |
| `updated_by` | the person who wrote it, or empty for server code |

The table is created with the rest of the schema at start, and a setting that
nobody has written has no row: a read of it answers the empty value. Core
reads a setting when it needs one, so a written setting takes effect on the
next use and survives a restart of the container.

`joshua.yaml` keeps what an operator sets up once: the people, the channels,
the inference jobs, the tool policy. The settings store keeps what the owner
changes while Joshua runs, and what is a credential of a feature rather than
of a container.

## How a key is named

A key is namespaced by a dotted prefix. The part before the first dot is the
group, and the rest names the setting in it:

```
push.vapid_public_key
push.vapid_private_key
```

A group is one section of the Instance page, with its own label, and each of
its fields has a label, a hint, and a flag that says whether it is a secret.
Core declares every group in one place, `core/joshua_core/store/settings.py`,
so a later feature adds a group there and the page renders it with nothing of
its own. A key that no group declares cannot be written: the route answers
`400`.

The first group is push notifications: whether they are on, the VAPID pair of
the instance, the public key the browser subscribes with and the private key
that signs each push, and the subject a push service writes to.
[notifications.md](notifications.md) has that feature. The store came with
push and is not of it; any feature uses it.

## Admin → Instance

The Instance section of the Admin area is the editor. It shows one section
per group, and in it one field per setting, each with its own save: a save
writes that setting and nothing else.

A plain setting shows its value in the field, and the line under it says when
it last changed and who changed it.

A secret setting shows **set** or **not set**, and no field at all. "Replace"
(or "Set", for one that is not stored) opens an empty field, and a save writes
what the owner typed. The stored secret is never shown, never filled into a
field, and never sent to the browser, so the owner can replace a secret and
nobody, the owner included, can read one back.

The section is the owner's. A member and a guest get no page and no route: the
viewer refuses both routes with `403`.

## How a secret is encrypted

A secret value is encrypted before it reaches the database, with AES-GCM: a
random 12-byte nonce, the ciphertext beside it, and the key of the setting as
the associated data, so a ciphertext cannot be moved from one setting to
another. The stored text is `v1:` and the base64 of the nonce and the
ciphertext, so a secret is unreadable in the table and in a backup of it.

Core decrypts a secret only for the server code that uses it. No route
decrypts one: a route reads whether a row is there, and the answer carries
`is_set` and a null value.

## `JOSHUA_SETTINGS_KEY`

The encryption key comes from the environment, `JOSHUA_SETTINGS_KEY`, a base64
32-byte key. Generate one:

```bash
openssl rand -base64 32
```

Put it in `.env` beside the other secrets of the instance:

```
JOSHUA_SETTINGS_KEY=<the line openssl printed>
```

On Kubernetes it is a key of the `joshua-core` secret of the chart, as
`values.yaml` shows.

**Keep that key for the life of the instance, and back it up with the
database.** A key that changes makes every stored secret unreadable: the rows
stay, and nothing can decrypt them. There is no way to recover a secret
written with a key that is gone; the owner writes each one again. For the same
reason Joshua never mints a key at startup, and never falls back to storing a
secret in plain text.

Restore a backup to a pod that has the same `JOSHUA_SETTINGS_KEY` as the one
it was taken from. [operations.md](operations.md) has the backup.

## With the key missing

Joshua starts and runs with no `JOSHUA_SETTINGS_KEY`. The variable is needed
for a secret, and for nothing else:

- a read or a write of a plain setting works;
- the Instance page lists every group, and a secret shows as set or not set,
  because that is a read of the row and not of the value;
- a write of a secret is refused with `503`, and the error names the variable:
  `JOSHUA_SETTINGS_KEY is not set: a secret setting cannot be read or written
  without it.` The page shows that line;
- server code that needs a stored secret gets the same error, so the feature
  that needs it says so instead of running without it.

The same error, with "another key" in it, means that the variable holds a key
that is not the one a stored secret was written with.
