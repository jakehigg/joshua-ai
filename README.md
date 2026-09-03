# Joshua

> **Pre-release.** The interfaces, the config, and the data layout can change
> between commits. Read `docs/CHANGELOG.md` before you update.

Joshua is a personal AI agent for one person or a small group of people who
share it: a family, friends, a team. It ships as three containers from one
repository and one `docker-compose.yml`. The three containers are
`channels` (the inbound trust boundary), `core` (the agent, memory, and
scheduling), and `gateway` (the outbound trust boundary that holds every
upstream credential). A shared package, `joshua_shared`, holds the code the
three containers use.

The same code runs on Kubernetes and on one Docker host with a Claude
subscription. One instance serves one group of people. The design keeps
the agent's power low: it has no file tools and no shell, and it reaches the
world only through `channels`, through `gateway`, and through the Claude Agent
SDK web tools.

## Architecture

![The three containers and what passes between them](docs/images/architecture.svg)

A message enters through `channels`, which authenticates the platform, applies
the allowlist and the rate limits, and stores any attachment. `channels` sends a
normalized turn to `core`. `core` runs the agent, keeps the session, and reaches
every tool through `gateway`. The reply goes back out through `channels`.

`core` holds the database and the data volume. `gateway` holds every upstream
credential, and applies the per-person tool policy. The agent has no file tools
and no shell: each capability is an MCP server behind `gateway`.

## Start here

[docs/quickstart.md](docs/quickstart.md) takes you from nothing to a
conversation with Joshua in about fifteen minutes. You need Docker and a Claude
subscription. You do not need Python, and you do not need a Telegram bot.

## Documentation

- [docs/quickstart.md](docs/quickstart.md): from nothing to a conversation.
- [docs/channels.md](docs/channels.md): the terminal, Telegram, iMessage, webhooks.
- [docs/config.md](docs/config.md): every setting in `joshua.yaml`.
- [docs/memory.md](docs/memory.md): notes, journal, profiles, retrieval.
- [docs/operations.md](docs/operations.md): health, logs, updates, backup, the viewer.
- [docs/architecture.md](docs/architecture.md): the three containers.
- [docs/security.md](docs/security.md): what the agent can and cannot reach.
- [docs/contracts.md](docs/contracts.md): every HTTP route.
- [docs/data-layout.md](docs/data-layout.md) and [docs/events.md](docs/events.md).
- [docs/enhancements.md](docs/enhancements.md): put your own frontend on the
  wiki, or run Otter Wiki.
- [docs/CHANGELOG.md](docs/CHANGELOG.md).

## Layout

- `shared/`: package `joshua_shared`
- `channels/`: package `joshua_channels`
- `core/`: package `joshua_core`
- `gateway/`: package `joshua_gateway`

Each of the three ships an image, built from its own `Dockerfile` by the
release workflow.

Each member has its own `pyproject.toml`, `Dockerfile`, and `tests/`. The repo
is a `uv` workspace. It uses Python 3.13, `ruff`, and `pytest`.

## Develop

```
make sync    # install the workspace with uv
make lint    # ruff check + format --check
make fmt     # ruff format + check --fix
make test    # pytest for the root and each member
```

## Run

There are two ways to run Joshua. Use the first one unless you change the code.

**Run a release.** `make up` starts the images that the release workflow
publishes to the GitHub container registry. Nothing is built.

```
make init-env    # create .env and mint the container tokens
make up          # start
make down        # stop
make logs        # follow the logs
```

`make init-env` writes `JOSHUA_VERSION` into your `.env`, so your installation
stays on one release. A `git pull` does not move it. To update, put the version
you want in `.env`, then `make pull && make up`. The releases are at
<https://github.com/jakehigg/joshua-ai/releases>.

**Run your own build.** `make up-dev` builds the three images from your
checkout and starts those instead. Use it when you change the code.

```
make up-dev      # build, then start
```

The two do not interfere. A build is tagged `joshua-ai-<component>:dev`, so it
never replaces a release on your machine, and `make up` still starts the
release. Both use the same containers and the same volumes, so your data stays
when you change from one to the other.

## Read your notes in a browser

Joshua writes Markdown to a volume, so a text editor is enough to read it. The
optional **viewer** is a small read-only web page for the same files: the
wiki, which holds the journal and every profile, and your attachments. A
person signs in with a password and reads; a member can also move a wiki page
to the trash, which is the only way to delete a page, because the agent has no
delete tool.

It is off by default, and it holds no credential of its own. To turn it on,
see [docs/operations.md](docs/operations.md#the-viewer).

```
docker compose --profile viewer up -d viewer   # then http://127.0.0.1:8082/
```

## Enhancements

The wiki is a folder of Markdown files, so any tool that reads such a folder
can be its frontend. Otter Wiki ships as one choice: a browser editor with
search and page history. `enhancements.yaml` turns it on. See
[docs/enhancements.md](docs/enhancements.md).

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) explains the fork and upstream setup, the
checks to run, and the writing standard. [CLAUDE.md](CLAUDE.md) holds the
conventions for people and agents who change the code.

## License

Joshua is free software under the GNU Affero General Public License, version 3
or later. See [LICENSE](LICENSE). You may run, change, and share it. If you
change it and let other people use it over a network, you must offer them the
source of your version.
