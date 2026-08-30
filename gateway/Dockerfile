# syntax=docker/dockerfile:1

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
RUN useradd --uid 1000 --create-home app
# Own /data in the image, so docker initializes the named volume with this
# ownership. The app runs as uid 1000 and writes under /data.
RUN mkdir -p /data && chown 1000:1000 /data
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
COPY --from=builder --chown=1000:1000 /app/shared /app/shared
COPY --from=builder --chown=1000:1000 /app/gateway /app/gateway
ENV PATH="/app/.venv/bin:$PATH"
USER 1000
EXPOSE 8000
ENTRYPOINT ["python", "-m", "joshua_gateway"]
