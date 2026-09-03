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
  optional front matter.
- The agent writes pages through its `files` tool and never deletes a page.
  The viewer moves a page to `wiki/.trash/` instead.
- The memory index picks up any change within `memory.index_interval_s`
  (default 60 seconds). See [docs/memory.md](memory.md).
- `wiki/joshua/` holds the repo docs. Core rewrites this folder at each
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

First start: open `http://127.0.0.1:8083/` and register an account. The
first account becomes the admin, and it can turn registration off from the
settings page.

Things to know:

- Otter Wiki keeps its pages in a git repository. It runs `git init` in
  `wiki/`, so a `.git` directory appears there. It commits only its own
  edits, so a page the agent wrote shows a "not under version control"
  notice until a person commits it from the edit page.
- Its front page is `Home`. The wiki has a `README.md`, not a `Home` page,
  so `/` reports not found at first. Open the page index from the menu, or
  create a `Home` page.
- A delete in Otter Wiki is a real delete of the file. Otter Wiki keeps its
  own git history of the delete, but the file does not go to
  `wiki/.trash/`.

## Custom

`use: custom` means you supply
`enhancements/wiki/custom/docker-compose.yml`. The repo ships
`docker-compose.example.yml` next to it: copy that file and edit it. The
example runs a small git auto-commit sidecar (`alpine/git`) that commits the
wiki every five minutes.

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
