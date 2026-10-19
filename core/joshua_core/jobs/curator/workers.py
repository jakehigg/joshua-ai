"""The curator workers: `place`, `compare`, and `refresh`.

Each worker is one `run_worker` call with no tools and one answer schema, and
each has a 90-second timeout. `ask_worker` runs one and says what happened:
`answered`, `no-answer` (the call failed or gave nothing), or `refused` (the
answer did not match the schema or a check of this module). Only an answered
call can change the wiki.

`Budget` counts the calls of one run against `jobs.curator.max_worker_calls`,
and its `usage` ledger sums the tokens of each worker. A worker function takes
`usage_out`, a list that the call appends its tokens to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from joshua_shared.config import JoshuaConfig
from pydantic import BaseModel, Field

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.sanitize import CleanModel
from joshua_core.engine.usage import Usage, UsageLedger
from joshua_core.jobs.curator.prompts import COMPARE_PROMPT, PLACE_PROMPT, REFRESH_PROMPT
from joshua_core.jobs.curator.scan import Section, render_section_map

TIMEOUT_S = 90.0
MAX_PATCH_PAIRS = 12
MAX_REPLACE_CHARS = 2000
NO_SECTION = "none"

Confidence = Literal["low", "medium", "high"]
Outcome = Literal["answered", "no-answer", "refused"]


def worker_model(settings: JoshuaConfig) -> str | None:
    """`jobs.curator.model`, else `jobs.model`, else `core.reflection_model`."""
    jobs = settings.jobs
    return jobs.curator.model or jobs.model or settings.core.reflection_model


class Budget:
    """The worker calls one run may still make, and the tokens they used."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.calls = 0
        self.usage = UsageLedger()

    @property
    def left(self) -> int:
        return self.limit - self.calls

    def spend(self) -> bool:
        """Count one call and return True, or return False when none is left."""
        if self.calls >= self.limit:
            return False
        self.calls += 1
        return True


# -- the answers ------------------------------------------------------------


class PlaceAnswer(CleanModel):
    """The `place` answer. `reason` is cleaned of leaked markup. `section` and
    `folder` stay as they are, so the check refuses a value that is not a name."""

    raw_fields = frozenset({"section", "folder"})

    section: str = Field(max_length=200)
    folder: str = Field(max_length=300)
    confidence: Confidence
    reason: str = Field(default="", max_length=1000)


class Claim(CleanModel):
    """One claim of each page. Both are cleaned of leaked markup."""

    a_says: str = Field(max_length=2000)
    b_says: str = Field(max_length=2000)


class CompareAnswer(BaseModel):
    relation: Literal["duplicate", "overlap", "conflict", "unrelated"]
    claims: list[Claim] = Field(default_factory=list, max_length=MAX_PATCH_PAIRS)
    canonical: Literal["a", "b", ""] = ""
    confidence: Confidence


class PatchPair(CleanModel):
    """One edit of a page. `find` and `replace` stay byte for byte, because
    `find` must match the page. `because` is cleaned of leaked markup."""

    raw_fields = frozenset({"find", "replace"})

    find: str = Field(min_length=1, max_length=MAX_REPLACE_CHARS)
    replace: str = Field(max_length=MAX_REPLACE_CHARS)
    because: str = Field(default="", max_length=1000)


class RefreshAnswer(BaseModel):
    status: Literal["current", "stale", "conflict"]
    patch: list[PatchPair] = Field(default_factory=list, max_length=MAX_PATCH_PAIRS)
    confidence: Confidence


@dataclass(frozen=True)
class WorkerResult[T: BaseModel]:
    """What one worker call gave: the answer, or None, and why."""

    answer: T | None
    outcome: Outcome


def _default_runner() -> Runner:
    from joshua_core.engine.agent import run_structured

    return run_structured


async def ask_worker[T: BaseModel](
    name: str,
    *,
    system_prompt: str,
    user_text: str,
    answer: type[T],
    model: str | None,
    runner: Runner | None = None,
    check: Callable[[T], bool] | None = None,
    usage_out: list[Usage] | None = None,
) -> WorkerResult[T]:
    """Run one tool-less worker. `check` refuses an answer that the schema allows.
    `usage_out` gets the tokens of the call."""
    call = runner or _default_runner()
    seen: dict[str, Any] = {}

    async def recording(**kwargs: Any) -> dict[str, Any] | None:
        raw = await call(**kwargs)
        seen["raw"] = raw
        return raw

    got = await run_worker(
        name=name,
        system_prompt=system_prompt,
        user_text=user_text,
        answer=answer,
        model=model,
        timeout_s=TIMEOUT_S,
        runner=recording,
        usage_out=usage_out,
    )
    if got is None:
        return WorkerResult(None, "refused" if seen.get("raw") is not None else "no-answer")
    if check is not None and not check(got):
        return WorkerResult(None, "refused")
    return WorkerResult(got, "answered")


def _page_block(tag: str, path: str, text: str) -> str:
    return f'<{tag} path="{path}">\n{text.strip()}\n</{tag}>'


def _safe_folder(folder: str) -> bool:
    if folder in ("", "."):
        return True
    if folder.startswith("/") or "\\" in folder:
        return False
    return all(
        part and part not in (".", "..") and not part.startswith(".") for part in folder.split("/")
    )


def normalize_folder(folder: str) -> str:
    """A folder from a worker answer, without the slashes at its ends."""
    cleaned = folder.strip().strip("/")
    return "" if cleaned in (".", "(wiki root)") else cleaned


async def place(
    path: str,
    page_text: str,
    sections: list[Section],
    *,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> WorkerResult[PlaceAnswer]:
    """Ask where one page belongs. The section must be one of `sections`, or `none`."""
    names = {s.name for s in sections} | {NO_SECTION}
    current = PurePosixPath(path).parent.as_posix()
    user_text = (
        "<section_map>\n"
        + render_section_map(sections)
        + "\n</section_map>\n\n"
        + f"The page is in the folder {current if current != '.' else '(wiki root)'}.\n\n"
        + _page_block("page", path, page_text)
    )

    def check(answer: PlaceAnswer) -> bool:
        return answer.section in names and _safe_folder(normalize_folder(answer.folder))

    return await ask_worker(
        "curator-place",
        system_prompt=PLACE_PROMPT,
        user_text=user_text,
        answer=PlaceAnswer,
        model=model,
        runner=runner,
        check=check,
        usage_out=usage_out,
    )


async def compare(
    a: tuple[str, str],
    b: tuple[str, str],
    *,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> WorkerResult[CompareAnswer]:
    """Compare two pages, each `(path, text)`. Page `a` is the first."""
    user_text = _page_block("page_a", a[0], a[1]) + "\n\n" + _page_block("page_b", b[0], b[1])
    return await ask_worker(
        "curator-compare",
        system_prompt=COMPARE_PROMPT,
        user_text=user_text,
        answer=CompareAnswer,
        model=model,
        runner=runner,
        usage_out=usage_out,
    )


async def refresh(
    path: str,
    page_text: str,
    entries: list[tuple[str, str]],
    *,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> WorkerResult[RefreshAnswer]:
    """Check a page against its journal entries, each `(path, text)`."""
    blocks = "\n\n".join(_page_block("entry", p, t) for p, t in entries)
    user_text = _page_block("page", path, page_text) + "\n\n<journal>\n" + blocks + "\n</journal>"
    return await ask_worker(
        "curator-refresh",
        system_prompt=REFRESH_PROMPT,
        user_text=user_text,
        answer=RefreshAnswer,
        model=model,
        runner=runner,
        usage_out=usage_out,
    )
