# Push notifications

A push notification reaches a person when Joshua has something to say and the
viewer is not open: a workspace finished, an assignment wants an answer, a
scheduled turn ran. The browser shows it, the person taps it, and the viewer
opens at the page the notification names.

Push is **off in every deployment** until the owner turns it on, and no
environment variable turns it on. The owner enters three keys under
Admin → Instance and ticks one checkbox, and that is the whole setup of the
instance. Each person then turns notifications on once per device, on their
Profile page, and on an iPhone or an iPad that device must be Joshua
installed to the home screen.

[settings.md](settings.md) has the store the keys live in, and
[contracts.md](contracts.md#the-push-routes) has the four routes.

## What push is

A browser that wants notifications subscribes at its own push service: Apple
for Safari, Google for Chrome, Mozilla for Firefox. The subscription is an
endpoint URL at that service and two keys of the browser. Joshua stores it,
and to send a notification it posts to that endpoint:

- the payload is encrypted to the keys of the browser (RFC 8291), so the push
  service carries a notification it cannot read;
- the request is signed with the VAPID private key of the instance (RFC 8292),
  so the push service knows which server sent it and whom to write to about
  it.

The payload holds four fields: `title`, `body`, `url`, and `tag`. The `tag` is
the workspace slug, so several notifications of one workspace collapse into
the newest instead of stacking up.

Nothing about a person's conversation goes to a push service in the clear.
Keep the `body` short anyway: a notification is a knock on the door, not the
message.

Two halves make one notification. Core holds the keys, the subscriptions and
the sender. The browser asks for the permission, subscribes, shows what
arrives, and opens the address of the payload when a person taps it. The
owner turns push on once, for the instance; each person then turns it on once
per device.

## Turning it on, as the owner

You need a VAPID key pair. It is yours, it is made once, and it belongs to the
instance and not to a person. `py_vapid` ships inside the core image, so one
command prints both keys in the base64url form the fields take:

```bash
docker compose exec core python -c "
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02, b64urlencode
key = Vapid02()
key.generate_keys()
raw = key.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
print('public: ', b64urlencode(raw))
print('private:', b64urlencode(key.private_key.private_numbers().private_value.to_bytes(32, 'big')))
"
```

It writes no file: copy the two lines, and nothing is left in the container.

Then, in the viewer, under **Admin → Instance → Notifications**, write:

| Field | What it holds |
|---|---|
| VAPID public key | the public key, base64url |
| VAPID private key | the private key, base64url. **Set the private key** opens the field. A secret: it is never shown again |
| VAPID subject | `mailto:you@example.com`, or `https://joshua.example` |
| Push notifications on | the checkbox. One tap writes it |

Save each key on its own; the checkbox needs no save. The line above the
fields says which keys are in and whether push is on, so it says what is
left. Keep the two keys together somewhere safe: a browser that subscribed
with one public key cannot be pushed to with another, so a new pair means
every device subscribes again.

**Each field is checked as it is written**, so a value that cannot work is
refused at the screen instead of failing in the middle of the first send:

| The field | What it takes | Why |
|---|---|---|
| VAPID subject | starts with `mailto:` or `https://` | `py_vapid` raises `VapidException` on anything else |
| VAPID public key | base64url of 65 bytes that start with `0x04` | one uncompressed P-256 point, what a browser subscribes with |
| VAPID private key | base64url of 32 bytes | the private value that signs a push |

A refusal is one line that says what the field takes, and it never shows the
value back: the private key is a secret. The command above prints both keys in
exactly these forms, the public one first.

The private key is a secret setting, so it needs `JOSHUA_SETTINGS_KEY` in the
environment of core, and it is encrypted at rest with it. No route ever
answers its value. [settings.md](settings.md#joshua_settings_key) has that
key, how to generate it, and why it is backed up with the database.

Turn push off again by clearing **Push notifications on**. The subscriptions
stay, so turning it on again needs no device to do anything.

## Turning it on, on your phone

Each device of yours subscribes once, and you do it yourself: a browser gives
no notification away without a tap.

1. **On an iPhone or an iPad, install Joshua first.** Open the viewer in
   Safari, tap Share, then **Add to Home Screen**, and open Joshua from that
   icon. iOS gives web push to an installed app only, on iOS 16.4 or later,
   and never to a tab. Android and a desktop browser need no install, but an
   installed app there keeps the notifications too.
2. Open your **Profile** page and look under "Notifications on this device".
3. Tap **Turn on notifications**. The browser asks, and you allow it. That one
   tap asks, subscribes at the push service of the browser, and stores the
   subscription.
4. Tap **Send a test**. A notification arrives, and a tap on it opens Joshua
   at the page it names.

A subscription is one person's and one device's, so the control is on the page
of the person who signed in and not on a page of a workspace. Each phone,
tablet and laptop of yours does these steps once. The Edit drawer of a
workspace keeps the two settings that *are* of the workspace, and a line there
points at the Profile page.

What the control says:

| It says | What to do |
|---|---|
| Notifications are on for this device. | Nothing. **Turn off** drops this device again. |
| This device gets no notifications yet. | Tap **Turn on notifications**. |
| This device blocks notifications. | Allow them for Joshua in the settings of the device. A page can only ask once; after a refusal only the operating system can undo it. |
| Add Joshua to the home screen. | Install the app, as in step 1, and open it from the icon. |
| This browser cannot show notifications. | Use another browser; this one has no web push. |
| notifications are not configured on the Joshua side | Ask the owner to turn push on, above. |
| a line of the server, such as `VapidException: Missing 'sub' from claims.` | A test send that could not be signed. Tell the owner: the subject or one of the keys under Admin → Instance is wrong. |

Per workspace, **Messages to your phone** says *when* Joshua writes: never,
on a question, or on a question and a finish. The control says whether this
device hears it. Both are needed: a workspace set to "Never" sends nothing to
a subscribed phone, and a workspace that would write reaches no device that
never subscribed.

**Turn off** drops the subscription in the browser and deletes the row on the
server, so Joshua stops pushing to that device at once. The viewer has no
sign-out button of its own yet. When it gets one, it must call `forget` in
`push.ts` **before** it posts to `POST /api/v1/session/sign-out`: the delete
needs the session, and a browser that signs out otherwise leaves a
subscription behind that Joshua keeps pushing to.

## When Joshua may reach you in a workspace

In a shared workspace Joshua can reach one person who is not reading the
thread, with the `notify_user` tool
(see [workspaces.md](workspaces.md#notifying-one-person)). Each person says
when it may reach them there, under Edit → **When Joshua may reach you here**:

| The setting | The mode | What Joshua does |
|---|---|---|
| Never | `off` | Nothing. The tool refuses. |
| When the thread needs me | `decide` | It notifies you when a question in the thread is for you, a decision waits on you, it wrote a document you need, it added an item you asked for, or somebody asked it to tell you. |
| Every message of another person | `all` | It notifies you on every turn another person writes. |

A person who sets nothing is asked for in a shared workspace (`decide`) and
never in a personal one (`off`), where nobody else writes. The setting is
yours alone: it says nothing about the other people of the workspace, and
nobody changes yours.

The notification goes to the devices you subscribed, as above, and to your
normal chat when you have no device. Three rules hold whatever the setting
is: Joshua never notifies the person who wrote the turn, it never notifies a
person who has the thread open and read to its newest row, and one person gets
one notification of one workspace in ten minutes. A line inside that time
folds into the notification you already have, which opens the thread.

**Messages to your phone** is a different setting, of the workspace and not of
one person: it says when Joshua writes to your normal chat about that
workspace. The two work together, as the table of each one says.

The mode is one row of the `notify_prefs` table, for that person and that
workspace, so it survives a restart. The same row holds the moment of the last
notification, which the ten-minute rule reads.

## Telling one person, from any chat

`notify_person` is the tool for the rest: Joshua decides somebody needs to
know, or a person asks him to tell somebody — "tell Lauren to look at the
recipe". It is in every chat of a member: a normal chat, a personal workspace,
and a shared one. A guest does not have it.

| The argument | What it is |
|---|---|
| `person` | an id or a name of the roster, with no regard to case |
| `text` | one short line, the knock on the door |
| `url` | a path inside Joshua the notification opens, `/` by default |

The person is one of the people of the instance, not of a workspace, so this
tool reaches anybody of this Joshua. A name that two people share is refused
with both of them named, and the call names an id instead. A `url` that is not
a path inside Joshua is refused: a notification of Joshua never sends a person
to another site.

It delivers on the same chain as `notify_user`: every push subscription of
that person, and their normal chat when no browser of theirs took it. So a
person with no device still gets the line, in the chat they already use.

One person gets one `notify_person` in ten minutes, and the tool keeps that
rule itself, whoever asked for it and from whatever chat. It is held in core's
memory and not in a table, so a restart of core forgets it; the worst that
costs is one notification more than the rule allows. **When Joshua may reach
you here** is of one workspace, so it governs `notify_user` and not this
tool. [workspaces.md](workspaces.md#notifying-one-person) has `notify_user`.

## What the browser does

The service worker of the installed app (`gateway/frontend/public/sw.js`) is
the one worker of the app, and web push is two of its handlers. There is one
worker per scope, so there is no second file.

- `push`: it shows the notification of the payload — the `title`, the `body`,
  and the `tag`. A payload it cannot read still shows a line that says Joshua
  has something, because iOS shows every push it delivers and allows no silent
  one.
- `notificationclick`: it brings the window to the `url` of the payload, which
  is a path of the app, such as `/chat/w/shared/kitchen`. A window that already
  shows that address takes the focus, an open window goes there, and with no
  window open it opens one. An address of another site is never followed: the
  tap opens Joshua.

The worker is served from `/sw.js`, so its scope is `/` and it covers the chat
at `/chat/…` with no `Service-Worker-Allowed` header. A worker served from
anywhere else would need one.

`gateway/frontend/src/push.ts` is the rest: the permission, the subscription,
and the test send. A subscription the browser holds and the server does not is
a notification nobody gets, so the two always go together: a refusal anywhere
unsubscribes the browser and deletes the row.

## Where the keys live

In the `settings` table of core's database, as four rows:

| Key | |
|---|---|
| `push.enabled` | `true` turns the routes and the sender on |
| `push.vapid_public_key` | what a browser subscribes with |
| `push.vapid_private_key` | signs each push. A secret setting |
| `push.vapid_subject` | the `mailto:` or `https:` the push service writes to |

They are not in `joshua.yaml`, not in `.env`, and not in the Helm values. A
container needs no restart: core reads a setting when it sends.

## While push is off

Every push route answers `503` and the error says what the owner has to do,
and the sender does nothing at all. That is the answer in each of three cases:

- `push.enabled` is not `true`;
- one of the public key, the private key, and the subject has no value;
- `JOSHUA_SETTINGS_KEY` is unset, so the private key cannot be read.

So an instance with no key is in a sound state, not a broken one: the viewer
shows that notifications are off, and nothing else notices.

## The subscriptions

One row of the `push_subscriptions` table per browser a person subscribed
with. One person has several rows, one per device, and the endpoint is unique:
a browser that subscribes again updates the row it already has.

The row keeps the two keys, the `User-Agent` the viewer saw, when it was made,
when a send last worked, and how many sends in a row were refused.

A push service's refusal is how Joshua learns a browser is gone:

- a `404` or a `410` deletes the subscription at once;
- a `429` is a rate limit, so that endpoint is left alone until its
  `Retry-After` has passed, and its failure count is untouched;
- any other failure raises the failure count, and the fifth one in a row drops
  the row.

A send never raises into the turn that caused it. A browser that cannot be
reached costs the person nothing but that notification.

## The sender is core

**Core sends.** The VAPID private key is a secret setting of core's database,
it is decrypted inside core, and it never leaves it, so the request to the
push service goes out of core. The gateway is not in this path.

That makes core the container that needs egress for push: on Kubernetes the
`joshua-core` pod reaches the push services, among them
`web.push.apple.com` and `fcm.googleapis.com`. A browser's endpoint names the
service of that browser, so the set follows the devices that subscribe. On
compose core already reaches the internet and there is nothing to allow.

There is no connectivity check and no pre-flight: with the keys written, the
first real send either works or reports the error of the push service.
**Admin → Instance** checks the form of each value, as above, and does not
test the pair.
`POST /api/v1/push/test` is the test: it sends the signed-in person a
notification through the same sender, and answers what each of their browsers
did with it.

A send is signed before it goes anywhere, and the VAPID header is built once,
for every subscription of that send. A header that cannot be built — a subject
`py_vapid` refuses, a private key that does not read — sends nothing, touches
no subscription, and answers the real message: the test route is `400` and its
`error` is the class and the text of the failure, which the card shows. So a
wrong setting costs no device: it is the instance that is wrong, not the
browser, and five tests in a row delete nothing. A browser that refuses the
push it was sent is the other case, and that one counts against that
subscription, as above.
