# The frontend of the viewer

The React app of the viewer. It is the whole interface: every screen is a
route of its router, and it reads one JSON API under `/api/v1/`. The gateway
image builds it (see `gateway/Dockerfile`) and copies `dist/` into
`joshua_gateway/static/`, which `joshua_gateway/viewer_spa.py` mounts at `/`.
With no build in the image, the viewer logs
`no frontend build; the React pages are off` and serves the JSON API alone.

## Work on it

```
cd gateway/frontend
npm ci
npm run dev        # the app on :5173, the JSON routes proxied to :8000
npm run typecheck
npm test
npm run build
```

`src/index.css` imports the two stylesheets of the viewer:
`joshua_gateway/ui/joshua-ui.css`, the master copy of the shared Joshua look,
which is never edited in a copy, and `src/viewer.css`, which holds what only
the viewer needs. The images of the look are in `public/`.

Every JSON route of the viewer is under `/api/v1/`. `src/api/client.ts` holds
the base and sends the credentials of the page.

## The installed app

The viewer is a progressive web app, scoped to `/`: the whole viewer
installs, never the chat alone. `public/manifest.webmanifest` names it,
`public/sw.js` is its service worker, and `src/pwa.ts` registers the worker
in a build.

Add to Home Screen, from the address of the instance, puts a Joshua icon, the
elephant on the dark square, on the home screen. It opens at the wiki, the
front door of the viewer, with no address bar and no browser chrome, on the
same dark background (`#080d17`), and it signs in as the browser does. Every
screen of the viewer is in it, the chat among them, because its scope is the
whole app. On a slow network the shell comes from the cache and the page is
there at once; the data behind it still comes from the network, because no
answer under `/api/v1/` is ever cached.

The manifest declares `logo-192.png` and `logo-512.png`, both `purpose:
"any"`. It declares no maskable icon: the mark fills the square to its edges,
so a circular mask would cut the ear and the tusk.

`src/shell/sections.ts` is the one source of the navigation.
`src/shell/AppNav.tsx` draws it as a drawer on a phone, and
`src/shell/panels.ts` keeps the folds of the desktop. A section carries a
`migrated` flag, which every section now has: the item is a router link, not
a plain `<a>`.
