"""The `usage_event` rows: what each model call used, and the sum by day.

A call site learns its tokens through the `usage_out` seam (see
`engine/usage.py`) and gives the list to `record_usage`, which writes one row
for each call. The job runner gives the ledger of a run to `record_job_usage`,
which writes one row for each worker of the run. Both functions never raise:
a row that cannot be written is a warning, and the turn, the reflection, or
the run continues.

A row holds counts and ids only. It never holds a message, a question, or a
token.

The groups of the usage page are the `name` of a row: `chat`, `nightly`,
`internet`, `describe`, and one group for each job by job name.
`summarize` folds the rows of `Repo.usage_rows` into the `UsageResponse`
shape, one day in the instance timezone for each row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from joshua_shared.contracts import USAGE_MAX_DAYS, UsageCounts, UsageGroup, UsageResponse
from joshua_shared.log import get_logger

from joshua_core.engine.usage import Usage, UsageLedger

logger = get_logger("store.usage")

USAGE_KINDS = ("chat", "nightly", "internet", "describe", "job")

_COUNT_FIELDS = ("calls", "input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class UsageEvent:
    """One row of `usage_event`. `at` None lets the database use `now()`."""

    kind: str
    name: str
    usage: Usage
    calls: int = 1
    worker: str | None = None
    model: str | None = None
    person_id: str | None = None
    conversation_id: str | None = None
    run_id: int | None = None
    ref: str | None = None
    at: datetime | None = None


class UsageEventStore(Protocol):
    """The one `Repo` method that the recording functions use."""

    async def add_usage_event(self, event: UsageEvent) -> None: ...


def _uuid_or_none(value: str | None) -> str | None:
    """`value` when it is a UUID, else None. The column is a UUID."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


async def _insert(store: Any, event: UsageEvent) -> bool:
    try:
        await store.add_usage_event(event)
    except Exception as exc:  # noqa: BLE001 — a usage row never fails its caller
        logger.warning(
            {
                "message": "usage row not written",
                "kind": event.kind,
                "name": event.name,
                "error": type(exc).__name__,
            }
        )
        return False
    return True


async def record_usage(
    store: Any,
    kind: str,
    usages: Iterable[Usage],
    *,
    model: str | None = None,
    person_id: str | None = None,
    conversation_id: str | None = None,
    ref: str | None = None,
) -> int:
    """Write one `kind` row for each usage in `usages`, and return how many
    rows were written.

    `kind` is one of `chat`, `nightly`, `internet`, or `describe`; the row
    name is the kind. `ref` names the source; with more than one usage, each
    row gets `<ref>:<n>`. With no `ref`, each row gets a new random ref.
    A row that cannot be written is a warning. Only a `kind` that is not a
    call kind raises, a `ValueError`, because that is a fault in the code.
    """
    if kind not in USAGE_KINDS or kind == "job":
        raise ValueError(f"not a call kind: {kind!r}")
    items = list(usages)
    written = 0
    for index, used in enumerate(items):
        if ref is None:
            row_ref = f"{kind}:{uuid.uuid4()}"
        else:
            row_ref = ref if len(items) == 1 else f"{ref}:{index}"
        event = UsageEvent(
            kind=kind,
            name=kind,
            usage=used,
            model=model,
            person_id=person_id,
            conversation_id=_uuid_or_none(conversation_id),
            ref=row_ref,
        )
        written += await _insert(store, event)
    return written


async def record_job_usage(
    store: Any, *, job: str, run_id: int, ledger: UsageLedger, at: datetime | None = None
) -> int:
    """Write one `job` row for each worker in `ledger`, with the ref
    `job_run:<run id>:<worker>`, and return how many rows were written.
    This never raises."""
    written = 0
    for worker, calls, used in ledger.workers():
        event = UsageEvent(
            kind="job",
            name=job,
            usage=used,
            calls=calls,
            worker=worker,
            run_id=run_id,
            ref=f"job_run:{run_id}:{worker}",
            at=at,
        )
        written += await _insert(store, event)
    return written


def clamp_days(days: int) -> int:
    """`days` held to `1..USAGE_MAX_DAYS`."""
    return max(1, min(int(days), USAGE_MAX_DAYS))


def window_start(now: datetime, timezone: str, days: int) -> datetime:
    """The local midnight that starts a window of `days` days, today included."""
    tz = ZoneInfo(timezone)
    first = now.astimezone(tz).date() - timedelta(days=clamp_days(days) - 1)
    return datetime.combine(first, time.min, tzinfo=tz)


def _counts(values: Mapping[str, Any]) -> dict[str, int]:
    out = {name: int(values.get(name) or 0) for name in _COUNT_FIELDS}
    out["total"] = out["input"] + out["output"] + out["cache_read"] + out["cache_write"]
    return out


def _add(into: dict[str, Any], counts: Mapping[str, int]) -> None:
    for name in (*_COUNT_FIELDS, "total"):
        into[name] = into.get(name, 0) + counts[name]


def _group(entry: dict[str, Any]) -> UsageGroup:
    workers = {name: UsageCounts(**c) for name, c in sorted(entry.get("workers", {}).items())}
    return UsageGroup(**{k: entry.get(k, 0) for k in (*_COUNT_FIELDS, "total")}, workers=workers)


def summarize(rows: Iterable[Mapping[str, Any]], *, timezone: str, days: int) -> UsageResponse:
    """Fold the rows of `Repo.usage_rows` into the response of `GET /v1/usage`.

    Each row has `day`, `name`, `worker`, and the five counts. A day with no
    row is not in the answer.
    """
    by_day: dict[str, dict[str, dict[str, Any]]] = {}
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = row["day"]
        key = day.isoformat() if isinstance(day, date) else str(day)
        name = str(row["name"])
        counts = _counts(row)
        for target in (
            by_day.setdefault(key, {}).setdefault(name, {}),
            totals.setdefault(name, {}),
        ):
            _add(target, counts)
            worker = row.get("worker")
            if worker:
                _add(target.setdefault("workers", {}).setdefault(str(worker), {}), counts)
    return UsageResponse(
        timezone=timezone,
        days=clamp_days(days),
        groups=sorted(totals),
        rows=[
            {"date": key, "groups": {n: _group(e) for n, e in sorted(groups.items())}}
            for key, groups in sorted(by_day.items(), reverse=True)
        ],
        totals={n: _group(e) for n, e in sorted(totals.items())},
    )
