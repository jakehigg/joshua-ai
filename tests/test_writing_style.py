"""The worker prompt and the documents skill page give the same writing rules."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "assignments" / "joshua_assignments" / "prompts" / "assignment.md"
SKILL = ROOT / "core" / "joshua_core" / "prompts" / "builtin" / "skills" / "documents.md"


def _section(path: Path, heading: str) -> str:
    text = path.read_text("utf-8")
    start = text.index(heading) + len(heading)
    return text[start : text.index("\n#", start)].strip()


def test_the_worker_and_joshua_get_the_same_style_section() -> None:
    worker = _section(WORKER, "## How you write\n")
    assert "Simplified Technical English" in worker
    assert _section(SKILL, "### How you write\n") == worker
