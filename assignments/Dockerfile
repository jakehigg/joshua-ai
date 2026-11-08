# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-assignments

# The same, plus the pinned dev group of the workspace, for the
# `session-test` target only.
FROM builder AS builder-test
RUN uv export --frozen --only-group dev --no-hashes --no-emit-project -o /tmp/dev.txt \
    && uv pip install --python /app/.venv/bin/python -r /tmp/dev.txt

# The common stage: the runtime of the core image, plus git for the checkouts.
FROM python:3.13-slim AS base

# The same Node, npm, and Claude Code CLI as core/Dockerfile, pinned the same
# way. The CLI needs Node 22. npm 11 blocks a dependency's install scripts, and
# the CLI's postinstall links the platform binary, so the install allows that
# one package's scripts, and `claude --version` fails the build when the binary
# is missing. git is for the repositories in `assignments.repos`. Bump the
# versions here and in core/Dockerfile together.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@11.18.0 \
    && npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@2.1.201 \
    && claude --version && git --version

# Two users. `assignments` (uid 1000) runs the manager. `session` (uid 1001)
# runs each session, in the container `assignments-session`, so a shell in a
# session cannot read the environment of the manager. Both are in the group
# `work` (gid 1001). `session` has no login shell and no home outside /work.
RUN groupadd --gid 1000 assignments \
    && groupadd --gid 1001 work \
    && useradd --uid 1000 --gid 1000 --groups work --create-home assignments \
    && useradd --uid 1001 --gid work --home-dir /work --no-create-home \
        --shell /usr/sbin/nologin session
# Own /data in the image, so docker initializes the named volume with this
# ownership. This container reads /data (the people file) and writes nothing
# there.
RUN mkdir -p /data && chown 1000:1000 /data
# The scratch directory of each assignment, /work/<id>/. Compose and the chart
# mount a volume here, so the image sets the owner and the mode: the manager
# owns it, and the group `work` can write it. The setgid bit gives each new
# directory the group `work`.
RUN mkdir -p /work && chown 1000:1001 /work && chmod 2770 /work
# The socket of the session launcher. Both containers mount a volume here.
RUN mkdir -p /run/joshua-session && chown 1001:1001 /run/joshua-session \
    && chmod 2770 /run/joshua-session
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/shared /app/shared
COPY --from=builder --chown=1000:1000 /app/assignments /app/assignments
# The SDK runs `<cli_path> -v` from the working directory of the manager
# before each session. The launcher refuses a directory outside /work, so the
# check gives nothing; the variable turns it off.
ENV PATH="/app/.venv/bin:$PATH" \
    CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1

# The image of scripts/test-session-user.sh: the runtime, plus pytest.
FROM base AS session-test
COPY --from=builder-test --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder-test /app/pyproject.toml /app/pyproject.toml
USER 1000
ENTRYPOINT ["python", "-m", "pytest"]

# The runtime: the manager. The `assignments-session` container runs the same
# image as uid 1001, with the entrypoint `python -m joshua_assignments.launcher`.
# This stage is the last one, so a build with no target gives it.
FROM base AS runtime
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_assignments"]
