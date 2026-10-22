"""The researcher: cited, challenged facts about one topic at a time.

`dossier` holds the state of a topic and the table of every topic, `select`
the choice of the topic, `raise_` the worker that raises questions, `gate` the
specificity gate, `known` the search for the knowledge we hold, `crew` the
steps of one question, `write` the page of one answer, `text` the text
helpers, and `job` the run itself. See the researcher section of
`docs/jobs.md`.
"""

from __future__ import annotations

from joshua_core.jobs.research.crew import (
    CrewResult,
    EditorAnswer,
    check_editor,
    domain_matches,
    registrable_domain,
    research_question,
)
from joshua_core.jobs.research.dossier import (
    Dossier,
    Question,
    load_dossier,
    load_dossiers,
    question_slug,
    save_dossier,
    write_index,
)
from joshua_core.jobs.research.gate import (
    Candidate,
    Gate,
    Verdict,
    names_topic,
    roster_names,
    shape_reason,
    world_reason,
)
from joshua_core.jobs.research.job import ResearchJob, worker_model
from joshua_core.jobs.research.raise_ import raise_questions
from joshua_core.jobs.research.select import (
    MessageStore,
    Selection,
    mention_counts,
    people_messages,
    select_topic,
)
from joshua_core.jobs.research.text import Grounding, bundle_parts, extract_sentences, strip_links
from joshua_core.jobs.research.write import render_page, write_page

__all__ = [
    "Candidate",
    "CrewResult",
    "Dossier",
    "EditorAnswer",
    "Gate",
    "MessageStore",
    "Grounding",
    "Question",
    "ResearchJob",
    "Selection",
    "Verdict",
    "bundle_parts",
    "check_editor",
    "domain_matches",
    "extract_sentences",
    "mention_counts",
    "load_dossier",
    "load_dossiers",
    "names_topic",
    "people_messages",
    "roster_names",
    "question_slug",
    "raise_questions",
    "registrable_domain",
    "render_page",
    "research_question",
    "save_dossier",
    "select_topic",
    "shape_reason",
    "world_reason",
    "strip_links",
    "worker_model",
    "write_index",
    "write_page",
]
