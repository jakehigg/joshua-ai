"""Structured JSON logging for joshua containers.

Each record is one JSON line with ``ts``, ``level``, ``service``, ``logger``,
``message`` and any extra fields. ``configure`` installs a stdout handler and
binds the service name to every record. A dict message flattens its keys into
the record; a plain string becomes the ``message``. Never log message bodies at
INFO and never log tokens; the formatter runs ``redact`` on every string value.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

_HEALTH_PATHS = frozenset({"/healthz", "/readyz"})

# Credential shapes that must never reach a log line. Also used by the
# config loader to refuse literal credentials in joshua.yaml.
CREDENTIAL_PREFIXES = ("sk-ant-", "glpat-", "xoxb-", "ak2_", "GOCSPX-")
_REDACTION = "[REDACTED]"
_BEARER_RE = re.compile(r"Bearer\s+\S+")
_PREFIX_RE = re.compile("(?:" + "|".join(re.escape(p) for p in CREDENTIAL_PREFIXES) + r")\S+")


def redact(text: str) -> str:
    """Mask bearer tokens and known credential shapes in ``text``."""
    text = _BEARER_RE.sub(_REDACTION, text)
    return _PREFIX_RE.sub(_REDACTION, text)


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class JSONFormatter(logging.Formatter):
    """Render a log record as one redacted JSON line."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        msg = record.msg
        fields: dict[str, Any] = {}
        if isinstance(msg, dict):
            fields = {key: value for key, value in msg.items() if key != "message"}
            message = msg.get("message", "")
        else:
            message = record.getMessage()

        data: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": _redact_value(message),
        }
        for key, value in fields.items():
            data.setdefault(key, _redact_value(value))
        if record.exc_info:
            data["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str)


def _parse_level(level: str) -> int:
    raw = (level or "").strip()
    if not raw:
        return logging.INFO
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw.upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure(level: str, service: str) -> logging.Logger:
    """Install a JSON stdout handler on the root logger and return the service logger."""
    root = logging.getLogger()
    root.setLevel(_parse_level(level))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service))
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


LEVEL_ENV = "LOG_LEVEL"


def configure_from_env(service: str) -> logging.Logger:
    """Configure ``service`` logging from ``LOG_LEVEL`` (default ``INFO``).

    Call this before ``uvicorn.run``, and pass ``log_config=None`` to uvicorn, so
    the uvicorn loggers propagate to the root handler and the process has one
    log format.
    """
    return configure(os.environ.get(LEVEL_ENV, "INFO"), service)


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name."""
    return logging.getLogger(name)


class HealthCheckFilter(logging.Filter):
    """Drop uvicorn access-log records for the ``/healthz`` and ``/readyz`` probes.

    uvicorn.access records carry ``args`` as a 5-tuple
    ``(client_addr, method, path, http_version, status)``. This reads the path
    (index 2, minus any query string) and drops the record when it targets a
    probe endpoint. Every other request logs normally and the request still
    succeeds — this only silences the log line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            if path in _HEALTH_PATHS:
                return False
        return True


def install_healthcheck_filter() -> None:
    """Attach the health-probe filter to ``uvicorn.access``. Idempotent."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthCheckFilter) for f in access.filters):
        access.addFilter(HealthCheckFilter())
