/**
 * The service worker of the installed app.
 *
 * It is written by hand and copied as it is: `public/` goes to the root of
 * the build, so the worker is served from `/sw.js` and its scope is `/`, the
 * scope of the app.
 *
 * It keeps one cache per version. The install fills it with the shell (the
 * entry page and the icons), the fetch of a hashed asset fills it further,
 * and the activate of a new version drops every older cache, so a new build
 * never meets an asset of an older one.
 *
 * An address under `/api/v1/` is never cached and never answered from the
 * cache: the worker does not take the request at all, so the chat stream, the
 * workspace routes, and every other JSON route of the viewer go to the
 * network exactly as they would with no worker. Nothing else is written to
 * the cache either: only a GET of this origin that answered 200.
 */

// Bump this when the shape of the cache changes; the activate drops the rest.
const CACHE = "joshua-v1";

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
