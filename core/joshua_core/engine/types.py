"""SDK-free engine types.

Kept apart from ``agent.py`` so the manager, the stub backend, and the tests
import ``TurnResult`` without importing ``claude_agent_sdk`` (which is absent in
stub mode and in the offline test run).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

OnDelta = Callable[[str], Awaitable[None]]

# What a person gets when a turn fails. The error text of a failed call can
# name a host, so it never reaches a person.
FAILED_REPLY = "Sorry, something went wrong on my end. Please try again."

# The ``error`` label of a turn that used all of its ``max_turns``. The SDK
# session still holds the tool work of that turn.
MAX_TURNS_ERROR = "error_max_turns"

# The message that the manager sends to a session that passed the context cap,
# before the session ends. The reply is the handoff for the fresh session.
HANDOFF_PROMPT = (
    "[system] This session passed its context limit and ends now. The next "
    "session sees only what you write here, the last rows of the thread, and "
    "(in a workspace) the workspace block. Do not call a tool. Write a handoff "
    "for that session, under 700 words, with these headings:\n"
    "Unanswered messages: each message of the person that you have not "
    "answered or done yet, word for word. The next session answers these "
    "first.\n"
    "Open questions: each question you asked the person that has no answer "
    'yet, in the words you used, so a short answer such as "yes" or "the '
    'second one" makes sense.\n'
    "Corrections: each time the person corrected you or gave you a rule in "
    "this session, in their words. A correction wins over anything you wrote "
    "before it.\n"
    "Repositories and environments: each repository, branch, and environment "
    "the work uses, with its full address, and each one the person said is "
    "retired or out of bounds.\n"
    "What you can do: each permission or tool path that you checked in this "
    "session and that works, such as a merge you may make or a repository "
    "you may push to, so the next session does not ask for it again.\n"
    "Waiting on the person: what the person must do, and why.\n"
    "Waiting on you: what you must do next.\n"
    "Decisions: each decision made, with its reason.\n"
    "Running work: each developer task, assignment, or scheduled check, its "
    "id, and what to do when it ends.\n"
    "Last request: what the person last asked for, in their words.\n"
    "Write only the handoff. Leave out a heading that has nothing. Write "
    "only what you checked in this session: do not guess a fact."
)

# The message that the manager sends after a turn reaches ``max_turns``, so the
# person gets a reply from the work the turn did, not the failure reply.
WRAP_UP_PROMPT = (
    "[system] This turn used all of its tool calls. Do not call a tool now. "
    "Reply to the person from what you already have: say what you found, what "
    "is still open, and what you would do next. Then ask whether to continue."
)

# The marker at the end of a workspace wrap-up that asks core for one more
# turn. Core takes it out of the reply.
CONTINUE_MARKER = "[[continue]]"

# The wrap-up of a workspace turn. A workspace holds long work, so a turn that
# used its tool calls may ask to go on instead of asking the person.
WORKSPACE_WRAP_UP_PROMPT = (
    "[system] This turn used all of its tool calls. Do not call a tool now. "
    "Reply from what you already have: say what is done and what is still "
    "open. If the notes of this workspace tell you to keep working, and the "
    "next step needs nothing from the person, do not ask whether to continue: "
    "name the next step, and end the reply with the line "
    f"{CONTINUE_MARKER} alone. Core then starts a new turn, and you go on "
    "from there. Otherwise, ask the person what you need."
)

# The text of the turn that core starts after a wrap-up that ends with
# ``CONTINUE_MARKER``.
CONTINUE_TEXT = (
    "[Continue] The last turn used all of its tool calls, and you asked to go "
    "on. Go on with the next step that you named in your last reply."
)


@dataclass(frozen=True)
class Attachment:
    """One inbound file the agent may view through the files MCP server.

    ``path`` is files-MCP-relative, e.g. ``attachments/2026/08/x.jpg``.
    ``original_name`` is what the sender called the file, so the agent can name it
    back to the person and cite it in a journal post.
    """

    path: str
    mime: str
    name: str
    original_name: str | None = None


@dataclass
class TurnResult:
    text: str
    session_id: str | None = None
    is_error: bool = False
    usage: dict[str, Any] | None = None
    # The input of the last model call of the turn, which is the size of the
    # context the session holds. None when the backend does not report it.
    context_tokens: int | None = None
    cost_usd: float | None = None
    # De-duped names of the tools the agent invoked this turn, first-seen order.
    tools_used: list[str] = field(default_factory=list)
    # Per-call record ``[{"name": ..., "input": {...}}, ...]`` in call order, with
    # duplicates. ``tools_used`` is the de-duped name list derived from it.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # A short label for a failed turn, safe to log: an SDK error kind such as
    # ``authentication_failed``, ``http_422``, or ``timeout``. It never holds
    # the error text.
    error: str | None = None
