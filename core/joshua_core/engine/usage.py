"""The tokens that one model call used, and the sum over many calls.

`Usage` holds the four counts of one call: input, output, cache read, and
cache write. `Usage.from_sdk` reads them from the `usage` field of an SDK
message, and a field that is not there counts as 0.

A caller learns the usage of a call through the `usage_out` argument of
`agent.run_structured`, `agent.run_worker_session`, `agent.run_oneshot`,
`ephemeral.run_worker`, and `internet.ask`: a list that the call appends one
`Usage` to when it ends. A structured call and a worker session read their
usage with `UsageMeter`, so a call that times out still appends the tokens
of the model messages that arrived before the timeout. A call that fails
before any model message appends nothing.

`UsageLedger` sums the calls of one job run by worker name. Its `as_data`
value goes into `RunReport.data["usage"]`, and its `tokens` into
`RunReport.tokens`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# The SDK names of the four counts, as the model API gives them.
_SDK_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def _count(value: Any) -> int:
    """A token count as an int: 0 for a value that is missing or not a count."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


@dataclass(frozen=True)
class Usage:
    """The tokens of one model call, or a sum of calls."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        """Input, output, cache read, and cache write, added."""
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )

    @classmethod
    def from_sdk(cls, usage: Any) -> Usage:
        """The usage in the `usage` dict of an SDK result message. `None`, or
        a value that is not a dict, gives a usage of 0."""
        if not isinstance(usage, dict):
            return cls()
        return cls(**{name: _count(usage.get(key)) for name, key in _SDK_FIELDS.items()})

    @classmethod
    def sum(cls, usages: Iterable[Usage]) -> Usage:
        total = cls()
        for usage in usages:
            total = total + usage
        return total

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "total": self.total,
        }


def note_result(msg: Any, usage_out: list[Usage] | None) -> None:
    """Append the usage of `msg` to `usage_out` when `msg` is the SDK result message."""
    if usage_out is not None and type(msg).__name__ == "ResultMessage":
        usage_out.append(Usage.from_sdk(getattr(msg, "usage", None)))


class UsageMeter:
    """The usage of one call, read from its messages as they arrive.

    The SDK result message holds the totals of the call. Each assistant
    message holds the usage of its model message, and the CLI can send one
    model message as several assistant messages with the same `message_id`,
    so the meter keeps one usage for each id. `usage` is the usage of the
    result message when it holds one, else the sum over the model messages.
    So a call that times out, and has no result message, still counts what
    it used, and a call that ends counts each token once.
    """

    def __init__(self) -> None:
        self._messages: dict[str, Usage] = {}
        self._result: Usage | None = None
        self._seen = False

    def note(self, msg: Any) -> None:
        """Read the usage of one SDK message, if it has one."""
        kind = type(msg).__name__
        raw = getattr(msg, "usage", None)
        if kind == "ResultMessage":
            self._seen = True
            if isinstance(raw, dict):
                self._result = Usage.from_sdk(raw)
        elif kind == "AssistantMessage" and isinstance(raw, dict):
            self._seen = True
            key = getattr(msg, "message_id", None) or f"#{len(self._messages)}"
            self._messages[str(key)] = Usage.from_sdk(raw)

    @property
    def seen(self) -> bool:
        """True when a result message, or a model message with a usage, arrived."""
        return self._seen

    @property
    def usage(self) -> Usage:
        if self._result is not None:
            return self._result
        return Usage.sum(self._messages.values())

    def flush(self, usage_out: list[Usage] | None) -> None:
        """Append `usage` to `usage_out`, when it is a list and a message arrived."""
        if usage_out is not None and self._seen:
            usage_out.append(self.usage)


@dataclass
class _WorkerUsage:
    calls: int = 0
    usage: Usage = field(default_factory=Usage)


class UsageLedger:
    """The calls and the tokens of one run, by worker name."""

    def __init__(self) -> None:
        self._by_worker: dict[str, _WorkerUsage] = {}

    def add(self, worker: str, usages: Iterable[Usage] = (), *, calls: int = 1) -> None:
        """Count `calls` calls of `worker` and add the tokens of `usages`."""
        entry = self._by_worker.setdefault(worker, _WorkerUsage())
        entry.calls += calls
        entry.usage = entry.usage + Usage.sum(usages)

    @property
    def calls(self) -> int:
        return sum(entry.calls for entry in self._by_worker.values())

    @property
    def usage(self) -> Usage:
        return Usage.sum(entry.usage for entry in self._by_worker.values())

    @property
    def tokens(self) -> int:
        return self.usage.total

    def as_data(self) -> dict[str, dict[str, int]]:
        """`{worker: {"calls": n, "tokens": m}}`, sorted by worker name."""
        return {
            name: {"calls": entry.calls, "tokens": entry.usage.total}
            for name, entry in sorted(self._by_worker.items())
        }
