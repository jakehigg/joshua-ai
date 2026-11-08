# syntax=docker/dockerfile:1

# Build stage: resolve and install the member and its dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --package joshua-assignments \
    && /app/.venv/bin/python -m compileall -q /app/shared /app/assignments \
    && chmod -R a+rX,go-w /app/.venv /app/shared /app/assignments

# Runtime stage: the runtime of the core image, plus git for the checkouts.
FROM python:3.13-slim AS runtime

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

RUN useradd --uid 1000 --create-home app
# Own /data in the image, so docker initializes the named volume with this
# ownership. This container reads /data (the people file) and writes nothing
# there.
RUN mkdir -p /data && chown 1000:1000 /data
# The scratch directory of each assignment, /work/<id>/. Compose and the chart
# mount a volume here, so the image must own the path.
RUN mkdir -p /work && chown 1000:1000 /work
# The code and the venv belong to root and are read-only for uid 1000. A
# session runs as uid 1000 and has a shell, so it must not change the code
# that the manager runs at its next start. The builder compiled the bytecode,
# so nothing writes to /app at run time.
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/assignments /app/assignments
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_assignments"]
