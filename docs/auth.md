# Signing in

The viewer is the web app of Joshua: the wiki, the journal, your
attachments, your documents, your assignments, the usage, and the chat. This
page is the guide to the sign-in of the viewer: what a person sees, where the
secret lives, what the owner does, and what an operator sets up.

[config.md](config.md#viewer) has the settings,
[operations.md](operations.md#the-viewer) has how to turn the viewer on,
[security.md](security.md#the-sign-in-of-the-viewer) has the boundary, and
[contracts.md](contracts.md#the-auth-routes) has each route.

## What a sign-in looks like

`viewer.auth.method` in `joshua.yaml` picks one method for the whole
instance. It is not a setting of one person.

| Method | What the page asks for |
|---|---|
| `anonymous` (the default) | nothing. There is no sign-in. |
| `pin` | who you are, from a list, then your PIN |
| `password` | your id, which you type, then your password |

### anonymous

The page asks for nothing, and `viewer.anonymous` decides who a request with
no cookie is. Both forms of it keep working:

- `anonymous: true` serves every request as a member who is not a person. The
  home page has no profile and no attachments, and the assignments section is
  empty.
- `anonymous: sam` serves every request as Sam: Sam's profile, Sam's
  attachments, Sam's assignments, and each write as Sam.

`viewer.anonymous` and `viewer.auth.method` are two settings. With
`method: anonymous` the app shows no form, and `/sign-in` says that this
Joshua asks nobody to sign in. With `pin` or `password` beside
`viewer.anonymous`, a request with no cookie is still served as the open
identity, and a person who signs in is served as themselves. See
[opening the viewer with no sign-in](operations.md#opening-the-viewer-with-no-sign-in).

### pin

1. The page shows the people who can sign in, under "Who are you?": Sam,
   Robin, Theo. The list comes from core, and it holds only the people who
   have a secret.
   While nobody has a secret, the list is empty. The page then says that
   nobody can sign in yet, and shows the `auth-reset` command that sets the
   first PIN.
2. Theo taps his name. The id field is filled in with `theo`. Theo can still
   change it, for example when a password manager writes its own username
   into the field.
3. Theo types his PIN and taps `Sign in`.
4. "Not you?" goes back to the list and clears the field.

### password

The page shows the same form at once, with the id field empty. Iris types
`iris`, then her password, then taps `Sign in`.

One form serves both methods. Only one word changes: the field says "your
PIN" or "your password". There is no keypad. A PIN is typed in the same
field as a password, so a PIN of letters and digits works.

A sign-in that does not work says one line: "That PIN didn't match. Try
again.", or "That password didn't match. Try again." on the other method. It
says the same line for a wrong secret, for a person who is not on the roster,
and for a person with no secret, so the page tells a stranger nothing about
who is there.

## The one secret

A PIN and a password are the same thing. A secret is any string, from a
four-digit PIN to a long passphrase. Core keeps one salted hash of it, and
the method only changes what the page calls it. So there is one way to set a
secret, one way to check it, and one "change my secret" action, whatever the
instance asks for.

## Remember this device

The sign-in form has one check box.

| The box | What the browser keeps |
|---|---|
| unchecked | a cookie of the browser session. It has no `Max-Age`, so the browser drops it when it closes. |
| checked | a cookie of thirty days. |

The signature of a cookie of the browser session expires one day after the
sign-in, whatever the browser kept.

The cookie is `joshua_session`, and it is the whole session: the viewer reads
the person out of it and asks core nothing for an ordinary page. A changed
secret, a reset by the owner, and a removal from the roster sign out every
browser that holds the old cookie, within the minute.

## Who holds what

| What | Where |
|---|---|
| the roster: who the people are, and their roles | `joshua.yaml`, with `/data/people.yaml` merged over it |
| the secret of each person | one salted hash in core's auth table, keyed by the person id |
| the session | one signed cookie in the browser |

A sign-in reads the roster first. So a hash row for somebody who is no longer
on the roster is inert: Nova, who left, cannot sign in with it, and the pin
list leaves her out. A removal also drops her row.

No route gives a hash back. `GET /auth/people` answers with the ids of the
people who have a secret, and nothing more.

The viewer has no connection to the database. It calls core's `/auth/*`
routes with its own fleet token, `JOSHUA_TOKEN_VIEWER`, and core does every
read and write of a secret. There is no HTTP Basic path and no password in
the viewer.

## The owner

`role: owner` in `joshua.yaml` names the person who keeps the roster:

```yaml
people:
  - id: sam
    name: Sam
    role: owner
  - id: robin
    name: Robin
    role: member
  - id: kit
    name: Kit
    role: guest
```

An owner is a member everywhere else. Sam writes the wiki, corrects a journal
entry, writes documents, and runs assignments, exactly as Robin does. The
owner alone gets the admin interface.

Nothing promotes a person to owner. The first person on the roster is not the
owner, a member who adds people from chat is not the owner, and no number of
people makes one. An owner is the role in the file, or a role that another
owner sets in the viewer.

`make validate` warns when nobody holds it:

```
ok: /etc/joshua/joshua.yaml
  warning: no person has role 'owner'
```

The file is still valid and the instance runs. What is missing is the person
who keeps the roster from the viewer, not a capability.

## The Users section

Sam opens Admin, the link under Profile at the bottom of the navigation, or
`/admin/users`. Robin and Kit do not see the link, and every route behind it
refuses them.

The table holds one row for each person: the name and the id, the role, and
whether that person has a secret. The secret column says `Set`, or
`Not set — can't sign in`. A row with no secret leads with `Set secret`.

| What Sam does | What happens |
|---|---|
| `Add a person` | one dialog: the name, the id, a handle, the role, and the secret. The secret is set in the same step, so Iris is added and can sign in at once. The handle is optional: without one, Iris uses Joshua from the viewer's chat. |
| `Set secret` | sets the secret of that person, with no current secret. It is a first secret and a reset in one. |
| `Promote` or `Demote` | a guest becomes a member, a member becomes an owner, an owner becomes a member. |
| `Remove` | takes the person off the roster. Their handles and their secret go. What they wrote stays. |

The secret of the dialog can stay empty. Then Iris is on the roster and
cannot sign in, her row says so, and Sam sets one later.

The roster always keeps an owner. On the row of the only owner, `Demote` and
`Remove` are off, and core refuses both with `409`. To hand the roster to
Robin, Sam promotes Robin to owner first, and then steps down.

Two other paths add a person, and neither one sets a secret:

- the agent's `add_user` tool, when a member adds somebody from a chat;
- the CLI, `python -m joshua_core people add --id iris --name Iris`, with
  `--handle telegram:998877` and `--role` when you want them.

So a person who arrives that way is on the roster, reaches Joshua through
their channel, and cannot sign in to the viewer until an owner gives them a
secret. The pin list shows only the people who have one, so such a person is
not on it.

## Your Profile

"Profile" is at the bottom of the navigation, at `/me`. Every person who
signs in changes three things about themselves there:

- **Your PIN or password.** Type the current secret, then the new one two
  times. The viewer sends it to `POST /api/v1/session/secret`. The change
  moves the stamp of that person, so this browser stays signed in and every
  other browser of that person signs out.
- **Your instructions to Joshua.** Joshua reads this text in every
  conversation with you. It is the `prompt` of your roster entry.
- **Journal entries about you.** `auto`, `ask`, or `off`: the `journal`
  consent of your roster entry.

A person cannot change their own name, role, or handles. The owner changes a
role under Admin → Users.

The instructions and the consent go to `POST /api/v1/profile`. Core writes
the instructions to `/data/people/<id>/prompt.md` and puts two fields in your
entry of `/data/people.yaml`: `prompt`, the path of that file, and `journal`.
An entry of the people file overlays only the fields it holds on your entry
in `joshua.yaml`, so your name and your handles still come from
`joshua.yaml`. A role that the owner sets under Admin → Users also goes into
the people file, and from then on it wins over the role in `joshua.yaml`.
Joshua reads the new instructions and the new consent on the next turn, with
no restart.

When somebody is locked out, and an owner cannot help, an operator runs one
command in the core container:

```
docker compose exec core python -m joshua_core people auth-reset \
  --id theo --secret '<a long secret>'
```

It writes the hash straight to the database, so it needs no token and no
running viewer, and it prints no secret. A person who is not on the roster
gets nothing: a secret without a person is a way in that no roster shows.

When nobody is the owner, an operator names one the same way:

```
docker compose exec core python -m joshua_core people role --id sam --role owner
```

## Deploy it

1. Give one person `role: owner` in `joshua.yaml`, and turn the viewer on
   with a method.

   ```yaml
   viewer:
     enabled: true
     auth:
       method: pin
   ```

2. Run `make validate`, then `make up`. `make up` starts the viewer because
   `viewer.enabled` is `true`.

3. Give the owner the first secret with `auth-reset` above. After that, the
   owner sets the secret of everybody else under Admin → Users.

Nothing else needs a setting. `make init-env` writes
`JOSHUA_VIEWER_SESSION_SECRET`, which signs the cookies, so a restart keeps
everybody signed in. Core serves the roster to the viewer because `viewer` is
one of core's `AUTH_CALLERS`, the callers of `/auth/*`.

[operations.md](operations.md#the-viewer) has the whole procedure, and
[the chart README](../charts/joshua/README.md#the-viewer) has the Kubernetes
form.

### If you signed in with the viewer passwords before

An instance that signed people in with `viewer.users` and `VIEWER_PASSWORDS`
needs nobody to sign in again. At its first start with the new sign-in, core
reads those two and `VIEWER_PW_<ID>`, writes a hash for each person who has
no secret yet, and marks the import done in a table of its own. It overwrites
nothing, and the marker, not the state of the secrets, is what stops it: a
restart, a restored database, and a roster whose secrets were all dropped
never run it a second time.

Compose gives `VIEWER_PASSWORDS` from `.env` to core. Delete `viewer.users`
and `VIEWER_PASSWORDS` after that first start. Nothing reads them again, and
`make validate` warns while either one is there.

## What this sign-in does not do

- **There is no lockout.** A wrong secret costs nothing but the answer.
  Nothing counts the tries of a person, and nothing locks an account.
- **There is no rate limiting.** The viewer does not slow a caller down, and
  it does not refuse one that calls the sign-in again and again.
- **There is no recovery.** A person who forgets their secret does not get it
  back, and the viewer sends no mail. The owner sets a new one under
  Admin → Users, or an operator runs `auth-reset`.

The viewer serves plain HTTP. Terminate TLS at a reverse proxy, and keep the
address on an internal network that you trust. This is the interface of one
instance, for the people of that instance. Do not expose it on the public
internet.
