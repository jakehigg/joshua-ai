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

`src/shell/sections.ts` is the one source of the navigation.
`src/shell/AppNav.tsx` draws it as a drawer on a phone, and
`src/shell/panels.ts` keeps the folds of the desktop. A section carries a
`migrated` flag, which every section now has: the item is a router link, not
a plain `<a>`.
