/**
 * The service worker of the installed app.
 *
 * It is written by hand and copied as it is: `public/` goes to the root of
 * the build, so the worker is served from `/sw.js` and its scope is `/`, the
 * scope of the app.
 *
 * It keeps one cache per build. The install fills it with the shell (the
 * entry page and the icons), the fetch of a hashed asset fills it further,
 * and the activate of a new version drops every older cache, so a new build
 * never meets an asset of an older one.
 *
 * An address under `/api/v1/` is never cached and never answered from the
 * cache: the worker does not take the request at all, so the chat stream, the
 * workspace routes, and every other JSON route of the viewer go to the
 * network exactly as they would with no worker. Nothing else is written to
 * the cache either: only a GET of this origin that answered 200.
 *
 * It is also the half of web push that runs in the browser: a `push` event
 * shows the notification its payload names, and a tap on that notification
 * (`notificationclick`) brings the window to the address of the payload, or
 * opens one there. There is one worker per scope, so these handlers live
 * beside the cache and not in a second file.
 */

// The build writes its id over the placeholder (see `buildStamp.ts`), so every
// build has a worker that differs from the last one. The browser then installs
// it, and its activate drops the cache of every older build. The shell and
// the hashed assets of an old build never outlive it.
const CACHE = "joshua-__BUILD_ID__";

// The JSON space of the viewer. No answer under it is ever cached.
const API = "/api/v1/";

// The hashed assets of the build. The name carries the hash, so a file never
// changes under its name and the cache may answer first.
const ASSETS = "/assets/";

// The shell: the entry page of the app and the files the installed app shows
// before any route has loaded.
const SHELL = "/";
const PRECACHE = [
  SHELL,
  "/manifest.webmanifest",
  "/favicon-32.png",
  "/logo-192.png",
  "/logo-512.png",
  "/apple-touch-icon.png",
];

// How long a navigation waits for the network before the cached shell
// answers. The app then loads its data over the same slow network, but the
// person sees the shell at once.
const SLOW_MS = 2500;

// True for an answer that may be kept: this origin, and a plain 200. A 401
// of the sign-in, a redirect, and an opaque answer are never cached.
function keepable(response) {
  return response.ok && response.type === "basic";
}

async function put(request, response) {
  if (!keepable(response)) return;
  const cache = await caches.open(CACHE);
  await cache.put(request, response.clone());
}

// A hashed asset or an icon: the cache first, the network once.
async function cacheFirst(request) {
  const hit = await caches.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  await put(request, response);
  return response;
}

// A page of the app: the network, so a new build arrives, and the cached
// shell when the network fails or is slow.
async function navigate(request) {
  const network = fetch(request).then(async (response) => {
    await put(SHELL, response);
    return response;
  });
  // A failure after the cached shell has answered is not an error here.
  network.catch(() => undefined);
  const cached = await caches.match(SHELL);
  if (!cached) return network;
  let timer;
  const slow = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("slow")), SLOW_MS);
  });
  try {
    return await Promise.race([network, slow]);
  } catch {
    return cached;
  } finally {
    clearTimeout(timer);
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // One file that does not arrive must not fail the install.
      await Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => undefined)));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The JSON API, the chat stream and the workspace routes among it: the
  // worker stands aside, so no answer of it is read from or written to the
  // cache.
  if (url.pathname.startsWith(API)) return;
  if (request.mode === "navigate") {
    event.respondWith(navigate(request));
    return;
  }
  if (url.pathname.startsWith(ASSETS) || PRECACHE.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
});

// Web push: the notification, and the tap on it.
//
// The payload of a push holds `title`, `body`, `url` and `tag` (core's
// `Notification`). iOS shows every push it delivers and allows no silent one,
// so a payload that cannot be read still leaves a line a person can tap.

// What a notification says when the payload says nothing.
const FALLBACK_TITLE = "Joshua";
const FALLBACK_TAG = "joshua";

// Where a tap goes when the payload names no address: the start of the app.
const FALLBACK_URL = "/";

// The app icon of a notification, and the small badge a phone draws beside it.
const NOTIFICATION_ICON = "/logo-192.png";
const NOTIFICATION_BADGE = "/favicon-32.png";

// The address of a payload, which is a path of this app, such as
// `/chat/w/shared/<slug>`. Anything else — another origin, a scheme, a
// protocol-relative address — is not followed, and the tap opens the app.
function pathOf(value) {
  if (!value.startsWith("/") || value.startsWith("//")) return FALLBACK_URL;
  return value;
}

// The four fields of the payload, each of them a string.
function payloadOf(event) {
  let body = null;
  try {
    body = event.data ? event.data.json() : null;
  } catch {
    // The payload is not JSON: the notification below says Joshua has
    // something, and the tap opens the app.
  }
  if (!body || typeof body !== "object") body = {};
  const text = (value) => (typeof value === "string" ? value : "");
  return {
    title: text(body.title) || FALLBACK_TITLE,
    body: text(body.body),
    // The tag of a workspace, so its notifications collapse into the newest.
    tag: text(body.tag) || FALLBACK_TAG,
    url: pathOf(text(body.url)),
  };
}

self.addEventListener("push", (event) => {
  const payload = payloadOf(event);
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: payload.tag,
      icon: NOTIFICATION_ICON,
      badge: NOTIFICATION_BADGE,
      // The tap reads the address back from here.
      data: { url: payload.url },
    }),
  );
});

// The window the tap belongs in: the one that already shows that address, else
// the open window, sent there, else a new one. The installed app has one
// window, so the common path is the second one.
async function showAt(url) {
  const target = new URL(url, self.location.origin);
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const here = windows.find((client) => new URL(client.url).pathname === target.pathname);
  if (here) {
    await here.focus();
    return;
  }
  const first = windows[0];
  if (first && typeof first.navigate === "function") {
    // A browser that refuses to send a window elsewhere gets a window of its
    // own below.
    const moved = await first.navigate(target.href).catch(() => null);
    if (moved) {
      await moved.focus();
      return;
    }
  }
  await self.clients.openWindow(target.href);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data;
  event.waitUntil(showAt(data && typeof data.url === "string" ? data.url : FALLBACK_URL));
});
