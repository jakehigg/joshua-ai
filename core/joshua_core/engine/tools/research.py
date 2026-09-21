"""In-process ``research`` tool server: ask the open web one question.

One tool, ``research_web``. The agent holds no web tool and never will. It
asks this tool a question, and a worker with nothing but the web tools goes and
finds out. See ``engine/research.py`` for what the worker does and does not
hold, and ``joshua_shared.netblock`` for the block list that keeps a fetch off
the private network.

What comes back is content from the open web. It is wrapped and marked as
such, and the agent reads it for the person.
"""

from __future__ import annotations

from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine import research as research_worker
from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps

logger = get_logger("tools.research")

_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "One question to research, written so that somebody who knows "
                "nothing about this conversation can answer it. Name what you "
                "need: the dish and what it must avoid, the product and the "
                "model number, the fact and the year."
            ),
        },
    },
    "required": ["question"],
}

_DESCRIPTION = (
    "Research one question on the open web and get back an answer with its sources. "
    "Use it when the answer is not in the wiki and would otherwise come from memory: "
    "a recipe, a specification, an opening time, a price, anything that changed after "
    "you were trained. The worker sees only the question you write, so put everything "
    "it needs in the question and never a private detail it does not need. What comes "
    "back is from the open web: read it for the person, say what it does not answer, "
    "and cite the sources."
)

_UNAVAILABLE = (
    "Research is turned off on this instance. Say so plainly, and answer from what you "
    "know while making clear that it did not come from a source."
)

_NOTHING = "The research found nothing usable. Say so, and do not fill the gap with a guess."


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def do_research(deps: ToolDeps, *, settings: JoshuaConfig, question: str) -> str:
    """Research one question and return the text the agent reads.

    Every answer is text the agent can act on: the research, a line saying
    research is off, or a line saying nothing was found. It never raises.
    """
    config = settings.research
    if not config.enabled:
        return _UNAVAILABLE

    logger.info({"message": "research asked", "person_id": deps.person_id})
    answer = await research_worker.research(question, settings=config)
    if answer is None:
        return _NOTHING
    return research_worker.as_note(answer)


def build_research_server(deps: ToolDeps, *, settings: JoshuaConfig) -> Any:
    """Build the in-process ``research`` MCP server bound to one conversation."""

    @tool("research_web", _DESCRIPTION, _RESEARCH_SCHEMA)
    async def research_web(args: dict[str, Any]) -> dict[str, Any]:
        text = await do_research(deps, settings=settings, question=str(args.get("question", "")))
        return _reply(text)

    return create_sdk_mcp_server(name="research", version="1.0.0", tools=[research_web])


def register(manager: Any, settings: JoshuaConfig) -> None:
    """Wire the ``research`` builtin into the conversation manager."""

    def factory(deps: ToolDeps) -> Any:
        return build_research_server(deps, settings=settings)

    manager.register_builtin("research", factory)
