# The frontend of the viewer

The React app of the viewer. The gateway image builds it (see
`gateway/Dockerfile`) and copies `dist/` into
`joshua_gateway/static/`, which `joshua_gateway/viewer_spa.py` mounts at `/`.
With no build in the image, the viewer logs
`no frontend build; the React pages are off` and serves its server-rendered
pages only.

`package-lock.json` is not committed yet. Until it is, `npm ci` has no lock
file, so the frontend stage of the gateway image and the `frontend` job of CI
both fail. Make it with:

```
cd gateway/frontend
npm install --package-lock-only
```

and commit it.

## Work on it

```
cd gateway/frontend
npm ci
npm run dev        # the app on :5173, the JSON routes proxied to :8000
npm run typecheck
npm test
npm run build
```

`src/index.css` imports the two stylesheets of the viewer,
`joshua_gateway/ui/joshua-ui.css` and `joshua_gateway/viewer.css`. Those two
files are the master copies: change a style there, never here.

Every JSON route of the viewer is under `/api/v1/`. `src/api/client.ts` holds
the base and sends the credentials of the page.

`src/shell/sections.ts` is the navigation. A section has a `migrated` flag:
with the flag the section is a route of this app, and without it the section
is a plain link that the server renderer answers. Each task of the rewrite
migrates one section and turns its flag.
