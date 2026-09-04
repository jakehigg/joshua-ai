# Enhancements

An enhancement is an optional service that runs beside Joshua's three
containers. It runs code from another project, not from Joshua, under that
project's license. Every enhancement is off by default. Joshua's docs say how
to turn one on and what it can reach. The other project's documentation is
the reference for its own settings.

When you turn one on, you run someone else's code. Read that project's
documentation and security notes first.

## The wiki is a folder

The wiki is a plug-in interface, and this is why a wiki frontend needs no
Joshua code at all. The contract:

- The wiki is `/data/wiki/**.md`: UTF-8 Markdown, subfolders allowed,
  optional front matter. `wiki/journal/` (Joshua's journal) and `wiki/people/`
  (the profiles) are part of the wiki, so a frontend shows them to every
  account it has, the same as any other page.
- `wiki/Home.md` is the front page. Core makes it once, from a template.
  You and Joshua both keep it.
- The agent writes pages through its `files` tool and never deletes a page.
  The viewer moves a page to `wiki/.trash/` instead.
- The memory index picks up any change within `memory.index_interval_s`
  (default 60 seconds). See [docs/memory.md](memory.md).
- `wiki/joshua-docs/` holds the repo docs. Core rewrites this folder at each
  start, so an edit there is lost.
- Two writers can change one file. The last write wins. There is no lock.
- Joshua ignores any file or directory whose name starts with a dot, at any
  depth. This applies to the agent's file tools, the viewer, and the index.
  A frontend may keep its own state there, such as `.git` or `.obsidian`.
  Joshua leaves alone anything that is not a `.md` file.

Any tool that reads a folder of Markdown files can be the wiki's frontend.
The built-in viewer stays as the read-only floor: it needs no setup and
holds no risk.

## Choose a frontend

Copy the example file and pick a choice for the `wiki` slot:

```
cp enhancements.example.yaml enhancements.yaml
```

```yaml
# enhancements.yaml
enhancements:
  wiki:
    use: otterwiki   # none | otterwiki | custom
```

Each key under `enhancements` is a slot: a role an enhancement fills. `use`
picks the choice for that slot. `make up` reads `enhancements.yaml` and adds
`enhancements/<slot>/<choice>/docker-compose.yml` for each slot whose `use`
is not `none`. `make down`, `make logs`, and `make ps` cover the same set.

Start it:

```
make up
```

This needs Docker Compose 2.26 or later.

## Otter Wiki

Repository: <https://github.com/redimp/otterwiki> (MIT license). Settings
reference: <https://otterwiki.com/Configuration>.

Otter Wiki shows every page and folder of the wiki in a browser, with
search, a Markdown editor, page history, and attachments.

What Joshua sets: the image `redimp/otterwiki:2.24.0-slim`, user 1000, only
the wiki mounted at `/app-data/repository`, and a separate volume for
`/app-data` (accounts, settings, and the search database). `SITE_NAME` comes
from `OTTERWIKI_SITE_NAME` in `.env` (default `Joshua wiki`). Joshua passes
no secret: Otter Wiki writes its own secret key into its settings on first
start. The port is `127.0.0.1:8083`. Put a reverse proxy with TLS in front
before you expose it.

The wiki is open by default, the same as Otter Wiki itself: anyone who
reaches the port reads and edits, and the port is loopback only. The wiki
holds Joshua's journal and the profiles. Before you expose the host, set
`OTTERWIKI_READ_ACCESS`, `OTTERWIKI_WRITE_ACCESS`, and
`OTTERWIKI_ATTACHMENT_ACCESS` to `REGISTERED` in `.env`, or the matching
`readAccess`, `writeAccess`, and `attachmentAccess` values in the chart, and
register an account.

First start: open `http://127.0.0.1:8083/` and register an account. The
first account becomes the admin, and it can turn registration off from the
settings page.

Things to know:

- Otter Wiki keeps its pages in a git repository, and so does Joshua (see
  `wiki.git` in [docs/config.md](config.md#wiki)): both run in `wiki/`, on
  the same `.git`. Joshua commits its own writes and syncs anything else
  that changed at start and at the nightly run, so the "not under version
  control" banner shows only for an edit made outside Joshua, and only
  until the next sync, never for a page the agent wrote.
- Its front page is `Home`, and `wiki/Home.md` is that page, so `/` opens
  on it.
- A delete in Otter Wiki is a real delete of the file. Otter Wiki keeps its
  own git history of the delete, but the file does not go to
  `wiki/.trash/`.

## Custom

`use: custom` means you supply
`enhancements/wiki/custom/docker-compose.yml`. The repo ships
`docker-compose.example.yml` next to it: copy that file and edit it. Joshua
already commits the wiki itself (`wiki.git`, see
[docs/config.md](config.md#wiki)), so the example runs a push sidecar
instead of a commit loop: `git push` to a remote you name, every
`WIKI_GIT_PUSH_SECONDS` seconds (default 900). Set `WIKI_GIT_REMOTE` in
`.env` to an `ssh` or `https` URL, with the credential in the URL or a
mounted key, that credential is yours, never Joshua's. The sidecar adds the
remote once, if it is not there yet, and makes no commit of its own.

Rules for a custom overlay:

- Mount the wiki alone, using the `data` volume with subpath `wiki`. Never
  mount all of `/data`.
- Publish a port on `127.0.0.1` only.
- Run as user 1000.
- Pass no Joshua secret and no fleet token.

Your copy of `docker-compose.yml` is gitignored, so it stays local.

A person who wants no web UI at all can also sync the `wiki/` folder to a
laptop and open it in any Markdown editor.

## Kubernetes (Helm chart)

In your values file:

```yaml
enhancements:
  wiki:
    use: otterwiki
    otterwiki:
      ingress:
        host: notes.example.com
        className: nginx
        tls:
          enabled: true
      persistence:
        existingClaim: ""   # set to reuse an existing PVC for /app-data
```

`enhancements.wiki.otterwiki.persistence.existingClaim` names an existing
claim for the `/app-data` volume. Leave it empty and the chart makes a 1Gi
claim for you. The pod mounts the data claim with subPath `wiki` at
`/app-data/repository`. No Secret is needed.

See [the chart README](../charts/joshua/README.md#enhancements) for the full
set of values.

The chart supports the choices this page lists. A custom container on
Kubernetes is your own manifest.

## Add a choice

A contributor who adds a choice to a slot adds all of these:

- A directory `enhancements/<slot>/<choice>/docker-compose.yml`.
- A value in the `use` comment of `enhancements.example.yaml`.
- For Kubernetes: a block under `enhancements.<slot>.<choice>` in the Helm
  chart, and a template.
- A section on this page, with the repository link and the license of the
  project.
- A test that the overlay carries no Joshua credential
  (`tests/test_enhancements.py`).
