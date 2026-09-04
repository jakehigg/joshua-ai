# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-core

# Runtime stage: copy the venv and only the code this member needs.
FROM python:3.13-slim AS runtime

# The SDK spawns the bundled Claude Code CLI, which needs Node. The CLI requires
# Node 22 or newer (its package.json sets engines.node ">=22"). Debian trixie's
# apt "nodejs" is 20.x, so install the Node 22 line from NodeSource instead. The
# NodeSource "nodejs" package bundles npm.
#
# npm 11 blocks a dependency's install scripts by default. The CLI's postinstall
# (install.cjs) downloads and links the platform "claude" binary, so allow that
# one package's scripts with --allow-scripts. Without it the "claude" command is
# a stub with no working binary, and every turn fails at runtime. "claude
# --version" runs the binary to confirm the postinstall linked it. Bump the
# versions here to upgrade Node, npm, or the CLI.
#
# libgomp1 is the OpenMP runtime that onnxruntime (fastembed, the memory
# embedder) links at import time. The slim image omits it, so without it the
# embedding model fails to load and retrieval degrades to "unavailable".
#
# git backs `joshua_shared.wikigit`: core keeps `/data/wiki` as a git
# repository and commits its own writes (see `wiki.git`).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libgomp1 \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@11.18.0 \
    && npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@2.1.201 \
    && claude --version

RUN useradd --uid 1000 --create-home app
# Own /data in the image, so docker initializes the named volume with this
# ownership. The app runs as uid 1000 and writes under /data.
RUN mkdir -p /data && chown 1000:1000 /data
# The SDK config dir. Compose mounts a named volume here, so the image must own
# the path or the volume comes up root-owned and no session can be saved.
RUN mkdir -p /home/app/.claude && chown 1000:1000 /home/app/.claude
# The embedding model cache. Compose and k8s mount a volume here, so the
# image must own the path or the volume comes up root-owned.
RUN mkdir -p /home/app/.cache/fastembed && chown 1000:1000 /home/app/.cache && chown 1000:1000 /home/app/.cache/fastembed
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/shared /app/shared
COPY --from=builder --chown=1000:1000 /app/core /app/core
# Core publishes these into wiki/joshua/ on the data volume at start.
COPY --from=builder --chown=1000:1000 /app/docs /app/docs
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_core"]
