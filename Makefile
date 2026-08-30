.PHONY: sync lint fmt test up up-dev pull down nuke logs ps shell-core psql init-env e2e smoke chat validate backup restore

MEMBERS := shared channels core gateway

# The developer file pair: docker-compose.yml names the released images, and
# docker-compose.dev.yml replaces them with a build of this checkout.
DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

sync:
	uv sync --all-packages

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run --with pyyaml python scripts/check_chart_version.py

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest tests
	@for m in $(MEMBERS); do \
		echo "==> pytest $$m"; \
		uv run pytest $$m/tests || exit 1; \
	done

# Copy the example config on first run, then start the stack.
joshua.yaml:
	cp joshua.example.yaml joshua.yaml

# There are two ways to run Joshua, and they do not interfere:
#
#   make up       the released images from the GitHub container registry.
#                 Nothing is built. This is the way to run Joshua.
#   make up-dev   the images built from this checkout, tagged :dev. This is
#                 the way to try a change to the code.
#
# Both use the same containers and the same volumes, so your data stays when
# you change from one to the other. `make down`, `make logs`, and `make ps`
# work for both.

# Run Joshua. Set JOSHUA_VERSION in .env to take another release.
up: joshua.yaml
	docker compose up -d
	$(MAKE) --no-print-directory ps

# Get a newer release of the images. `make up` then restarts on them.
pull:
	docker compose pull

# Run your build of this checkout. Rebuilds what changed.
up-dev: joshua.yaml
	$(DEV) up -d --build
	$(MAKE) --no-print-directory ps

down:
	docker compose down

# Drop the volumes too. Asks first, because this deletes the database and data.
nuke:
	@printf 'This deletes the pgdata, data, and claude-config volumes. Continue? [y/N] '; \
	read ans; [ "$$ans" = "y" ] || { echo aborted; exit 1; }; \
	docker compose down -v

# Check joshua.yaml. The secrets in .env must be in the environment, because the
# config file refers to them.
# Runs in the core image, so the host needs no Python. The first run pulls the
# image; `make up` reuses it. Compose passes the secrets from .env.
validate: joshua.yaml
	docker compose run --rm --no-deps -T --entrypoint joshua-config core validate /etc/joshua/joshua.yaml

# Talk to Joshua. Set AS to a person id from joshua.yaml.
#   make chat AS=alex              open a session
#   make chat AS=alex MSG="hello"  send one message and stop
# This runs inside the core container, so it needs no Python on the host.
chat:
	@test -n "$(AS)" || { echo 'set AS to a person id, e.g. make chat AS=alex'; exit 1; }
	@docker compose exec $(if $(MSG),-T,) core python -m joshua_core chat --as $(AS) $(if $(MSG),"$(MSG)",)

logs:
	docker compose logs -f

ps:
	docker compose ps

shell-core:
	docker compose exec core /bin/sh

psql:
	docker compose exec postgres psql -U joshua -d joshua

# Create .env, pin the version, then mint each secret that .env does not
# already set. Running it again changes nothing.
init-env:
	@test -f .env || cp .env.example .env
	@chmod 600 .env
	@if ! grep -q "^JOSHUA_VERSION=." .env; then \
		ver=$$(grep -E '^JOSHUA_VERSION=' .env.example | head -1 | cut -d= -f2); \
		echo "JOSHUA_VERSION=$$ver" >> .env; \
		echo "pinned JOSHUA_VERSION=$$ver"; \
	fi
	@if ! grep -q "^POSTGRES_PASSWORD=." .env; then \
		pw=$$(openssl rand -base64 33 | tr '+/' '-_' | tr -d '='); \
		sed -i.bak '/^POSTGRES_PASSWORD=/d' .env && rm -f .env.bak; \
		echo "POSTGRES_PASSWORD=$$pw" >> .env; \
		echo "minted POSTGRES_PASSWORD"; \
	fi
	@for name in CHANNELS CORE GATEWAY LAPTOP; do \
		if ! grep -q "^JOSHUA_TOKEN_$$name=." .env; then \
			token=$$(openssl rand -base64 33 | tr '+/' '-_' | tr -d '='); \
			sed -i.bak "/^JOSHUA_TOKEN_$$name=/d" .env && rm -f .env.bak; \
			echo "JOSHUA_TOKEN_$$name=$$token" >> .env; \
			echo "minted JOSHUA_TOKEN_$$name"; \
		fi; \
	done

e2e:
	uv run pytest tests/e2e -m integration

# Run the core smoke suite against the compose Postgres. Starts (only) the
# postgres service with a host port, then runs the integration smoke suite from
# the dev venv. Set SMOKE_PG_PORT to change the host port.
smoke: joshua.yaml init-env
	docker compose -f docker-compose.yml -f tests/e2e/compose.smoke.override.yml up -d postgres --wait
	set -a; . ./.env; set +a; \
	DATABASE_URL="postgresql://joshua:$$POSTGRES_PASSWORD@127.0.0.1:$${SMOKE_PG_PORT:-5432}/joshua" \
	  uv run --package joshua-core pytest core/tests/test_smoke_integration.py -m integration

# Back up the database, the data volume, and the config files.
#   make backup                 writes ./backups/joshua-<timestamp>/
#   make backup DEST=/mnt/nas   writes there instead
backup:
	@scripts/backup.sh $(DEST)

# Restore one backup directory. The database must be empty, or pass FORCE=1.
#   make restore FROM=backups/joshua-20260828T031500Z
#   make restore FROM=backups/joshua-20260828T031500Z FORCE=1
#
# The restore starts a full rebuild of the search index, which holds a CPU
# until it ends. NO_EMBED=1 skips it and leaves the work to the nightly run.
#   make restore FROM=... NO_EMBED=1
restore:
	@test -n "$(FROM)" || { echo 'set FROM to a backup directory, e.g. make restore FROM=backups/joshua-...'; exit 1; }
	@scripts/restore.sh "$(FROM)" $(if $(FORCE),--force,) $(if $(NO_EMBED),--no-embed,)
