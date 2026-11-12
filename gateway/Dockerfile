# syntax=docker/dockerfile:1

# Frontend stage: build the React app of the viewer. Only `dist/` goes into the
# runtime image, so node stays in this stage.
FROM node:22-alpine AS frontend
WORKDIR /build
COPY gateway/frontend/package.json gateway/frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY gateway/frontend/ ./
# The app imports the two stylesheets of the viewer, which are the master
# copies and live beside the Python code. `src/index.css` names them
# `../../joshua_gateway/...`, one level above the workspace, which is `/` here.
COPY gateway/joshua_gateway/ui/joshua-ui.css /joshua_gateway/ui/
COPY gateway/joshua_gateway/viewer.css /joshua_gateway/
RUN npm run build

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-gateway

# Runtime stage: copy the venv and only the code this member needs.
FROM python:3.13-slim AS runtime

# The gateway installs a `package:` MCP server into its store volume at start,
# so the runtimes those packages need are in this image:
#
#   node and npm   an `npm:` package, and the executables it installs
#   uv             a `pypi:` package (uv tool install) and a `git+https:` project
#   git            cloning a `git+https:` package, and `joshua_shared.wikigit`,
#                  which commits a wiki write and a trash delete (see `wiki.git`)
#   curl           fetching the NodeSource setup script here; nothing at runtime
#
# The versions are pinned in this one RUN. Bump them here to upgrade. Debian
# trixie's apt "nodejs" is 20.x, so Node 22 comes from NodeSource, which bundles
# npm. The RUN ends with a version of each, so a broken install fails the build
# instead of every `package` entry at runtime.
#
# These runtimes make this image larger than the other two: about 600 MB, against
# the 700 MB limit CI checks (.github/workflows/ci.yml). Raise that limit on
# purpose, not by accident.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@11.18.0 \
    && node --version && npm --version && git --version
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
RUN uv --version

RUN useradd --uid 1000 --create-home app
# Own /data in the image, so docker initializes the named volume with this
# ownership. The app runs as uid 1000 and writes under /data.
RUN mkdir -p /data && chown 1000:1000 /data
# The MCP package store. Compose and the chart mount a volume here, so the image
# must own the path or the volume comes up root-owned and no install can write.
# It is derived data, never the data volume: nothing here is backed up.
RUN mkdir -p /opt/joshua-mcp && chown 1000:1000 /opt/joshua-mcp
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/shared /app/shared
COPY --from=builder --chown=1000:1000 /app/gateway /app/gateway
# The React app of the viewer. The viewer mounts this folder, and with no
# build in the image every page is a server-rendered one.
COPY --from=frontend --chown=1000:1000 /build/dist/ /app/gateway/joshua_gateway/static/
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_gateway"]
