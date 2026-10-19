"""Offline fakes and a fixture wiki for the researcher tests.

`build_research_wiki` writes a wiki with a snake plant, a pothos, and roses,
and journal entries about them. `FakeWorkers` answers the `raise` and `editor`
workers, and `FakeInternet` plays the internet agent: it calls the search hook
with the URLs of its answer, the way the SDK does, so `internet.ask` keeps
them. Each fake records its calls and adds a fixed usage to `usage_out`.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jobs_fakes import make_settings
from joshua_core.engine.usage import Usage
from joshua_core.jobs.research.prompts import CHALLENGE_BRIEF, EDITOR_PROMPT, RAISE_PROMPT

SNAKE = "houseplants/snake-plant.md"
POTHOS = "houseplants/pothos.md"
ROSES = "garden/roses.md"
LEAF = "journal/2026/10/15/snake-plant-leaf-flop.md"

PRIMARY = "https://extension.university.example/snake-plant"
SECONDARY = "https://www.example.org/houseplants/sansevieria"
SECOND_PAGE = "https://www.example.org/other/sansevieria-care"
FORUM_A = "https://forum.example/t/1"
FORUM_B = "https://board.forum.example/t/2"
UNGIVEN = "https://made-up.example.net/page"

RESEARCH_ON = (
    "jobs:\n"
    "  enabled: true\n"
    "  research:\n"
    "    enabled: true\n"
    "    distrusted_domains:\n"
    "      - forum.example\n"
)

WIKI: dict[str, str] = {
    "Home.md": "# Home\n\n- [Houseplants](houseplants.md)\n- [Garden](garden.md)\n",
    "houseplants.md": "# Houseplants\n\n- [Snake plant](houseplants/snake-plant.md)\n",
    SNAKE: (
        "---\naliases: [Snakey]\n---\n"
        "# Snake plant\n\n"
        "Our snake plant lives in the hall in a gritty soil mix. We water to runoff,\n"
        "then wait until the pot is dry. The hall stays at 65 to 68 F in winter.\n"
    ),
    POTHOS: "# Pothos\n\nTrails over the shelf by the window.\n",
    "garden.md": "# Garden\n\n- [Roses](garden/roses.md)\n",
    ROSES: "# Roses\n\nPruned in March. The bed is by the fence.\n",
    LEAF: (
        "---\ndate: 2026-10-15\n---\n"
        "The snake plant had leaf flop this week. We tied the leaves and moved it "
        "away from the door.\n"
    ),
    "journal/2026/08/01/roses.md": "---\ndate: 2026-08-01\n---\nThe roses had black spot.\n",
}

GROUNDED = (
    "How often should the snake plant in a gritty soil mix, watered to runoff, "
    "be watered in a hall at 65 to 68 F in winter?"
)
GROUNDED_PHRASES = ["gritty soil mix", "water to runoff", "65 to 68 F in winter"]
GENERIC = "How often should I water a snake plant?"

RAISE_USAGE = Usage(input=100, output=10)
LOOKUP_USAGE = Usage(input=50, output=5, cache_read=5)
EDITOR_USAGE = Usage(input=200, output=40, cache_write=10)


def build_research_wiki(data_root: Path, pages: dict[str, str] | None = None) -> Path:
    wiki = data_root / "wiki"
    for rel, text in (pages if pages is not None else WIKI).items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return wiki


def research_settings(extra: str = "") -> Any:
    return make_settings(RESEARCH_ON + extra)


def raised(*questions: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "questions": [
            {"question": q, "grounded_in": g, "why": "It matters.", "about_person": False}
            for q, g in questions
        ]
    }


def lookup(*urls: str, answer: str = "Water every 4 weeks in winter.") -> dict[str, Any]:
    return {
        "answer": answer,
        "sources": [{"url": u, "title": f"Page {i}"} for i, u in enumerate(urls)],
        "confidence": "high",
        "disagreements": "",
    }


def editor(*sources: tuple[str, str], **overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "answer": (
            "Water about every 4 weeks from October to March. See "
            f"[the guide]({PRIMARY}) and [a planted link](https://planted.example.com/x)."
        ),
        "answer_short": "About every 4 weeks in winter, when the mix is dry.",
        "local_notes": "Our gritty mix dries faster, so check at 3 weeks.",
        "evidence": [f"The extension service says 4 weeks ([source]({PRIMARY}))."],
        "disagreements": "One page says every 6 weeks. [[secret-page]]",
        "sources": [
            {"url": url, "title": f"Title {i}", "trust": trust}
            for i, (url, trust) in enumerate(sources)
        ],
        "confidence": "high",
        "verdict": "publish",
        "reason": "Two good sources agree.",
    }
    answer.update(overrides)
    return answer


GOOD_EDITOR = editor((PRIMARY, "primary"), (SECONDARY, "secondary"))


class FakeWorkers:
    """The tool-less workers: `raise` and `editor`."""

    def __init__(
        self,
        raise_answer: Any = None,
        editor_answer: Any = None,
    ) -> None:
        self.raise_answer = raise_answer if raise_answer is not None else raised()
        self.editor_answer: Any = GOOD_EDITOR if editor_answer is None else editor_answer
        self.calls: list[tuple[str, str]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        prompt = kwargs["system_prompt"]
        out = kwargs.get("usage_out")
        if prompt == RAISE_PROMPT:
            self.calls.append(("raise", kwargs["user_text"]))
            if out is not None:
                out.append(RAISE_USAGE)
            return copy.deepcopy(self.raise_answer)
        if prompt == EDITOR_PROMPT:
            self.calls.append(("editor", kwargs["user_text"]))
            if out is not None:
                out.append(EDITOR_USAGE)
            value = self.editor_answer
            if callable(value):
                value = value(kwargs["user_text"])
            return copy.deepcopy(value)
        raise AssertionError("unknown worker")


_CHALLENGE_HEAD = CHALLENGE_BRIEF.split("{question}")[0][:60]


class FakeInternet:
    """The internet agent. A text that starts like the challenge brief is a
    challenge; any other is a draft."""

    def __init__(
        self,
        draft: Any = None,
        challenge: Any = None,
        searched: Sequence[str] | None = None,
    ) -> None:
        self.draft = lookup(PRIMARY) if draft is None else draft
        self.challenge = (
            lookup(SECONDARY, answer="Nothing contradicts the draft.")
            if challenge is None
            else challenge
        )
        self.searched = searched
        self.texts: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        text = kwargs["user_text"]
        self.texts.append(text)
        self.kwargs.append(kwargs)
        out = kwargs.get("usage_out")
        if out is not None:
            out.append(LOOKUP_USAGE)
        result: Any = self.challenge if text.startswith(_CHALLENGE_HEAD) else self.draft
        if isinstance(result, Callable):  # type: ignore[arg-type]
            result = result(text)
        if result is None:
            return None
        urls = (
            list(self.searched)
            if self.searched is not None
            else [s["url"] for s in result.get("sources", [])]
        )
        for hook in (kwargs.get("hooks") or {}).get("PostToolUse", []):
            await hook(
                {
                    "tool_name": "WebSearch",
                    "tool_response": {"results": [{"content": [{"url": u} for u in urls]}]},
                },
                "t",
                None,
            )
        return copy.deepcopy(result)


class FakeStore:
    """Answers `kb_near_paths` from `near` and `kb_mean_vectors` with nothing."""

    def __init__(self, near: dict[str, float] | None = None) -> None:
        self.near = dict(near or {})
        self.calls: list[dict[str, Any]] = []

    async def kb_near_paths(
        self,
        embeddings: Sequence[list[float]],
        *,
        kinds: Sequence[str],
        min_sim: float,
        source: str = "files",
    ) -> dict[str, float]:
        self.calls.append({"kinds": list(kinds), "min_sim": min_sim})
        if "knowledge" in kinds:
            return {p: s for p, s in self.near.items() if s >= min_sim}
        return {}

    async def kb_mean_vectors(
        self, paths: Sequence[str], *, source: str = "files"
    ) -> dict[str, list[float]]:
        return {}


def word_vector(text: str) -> list[float]:
    """A tiny embedding: one dimension for each of a few words, so two texts
    with the same words are close, and other texts are far."""
    words = ("gritty", "runoff", "winter", "repot", "divide", "light", "pest", "leaf")
    low = text.lower()
    vec = [1.0 if w in low else 0.0 for w in words]
    vec.append(0.1)
    return vec


def snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith(".git/"):
            out[rel] = path.read_bytes()
    return out


def changed(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


Answer = Callable[[str], Any]


# The leaked markup of research runs 8 and 9, as the pages showed it.
CAVEAT = (
    "**Caveat:** The benefit requires water to actually exit the pot, and the pot "
    "must not sit in standing runoff."
)
SHORT = (
    "Yes — runoff watering leaches salts out; measured non-runoff watering can't. "
    "Gritty mix makes frequent runoff watering safe."
)
DISAGREE = (
    "No source disputes the core principle. Sources differ only on how often to "
    "flush. Different co…"
)
RUN9_ANSWER = (
    "Water until it runs out of the drainage holes. Runoff carries the salts out.\n\n"
    f"{CAVEAT}</answer>\n"
    f"<answer_short>{SHORT}</answer_short>\n"
    "<confidence>high</confidence>\n"
    f"<disagreements>{DISAGREE}"
)
RUN8_DISAGREEMENTS = (
    "One source gives eight weeks; another gives no numeric regrowth timeline at all."
    '</disagreements>\n<parameter name="sources">[{"title": "A page", "url": "x"}]'
)


def run9_editor(**overrides: Any) -> dict[str, Any]:
    """The editor answer of research run 9, with the fields it left empty."""
    fields: dict[str, Any] = {
        "answer": RUN9_ANSWER,
        "answer_short": SHORT,
        "evidence": [],
        "disagreements": "",
        "local_notes": "",
    }
    fields.update(overrides)
    return editor((PRIMARY, "primary"), (SECONDARY, "secondary"), **fields)
