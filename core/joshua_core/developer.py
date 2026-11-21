"""The core side of a developer task: the pull request Joshua opens.

A developer task runs in the ``developer`` MCP server, outside this
repository. A task that finished leaves a pushed branch and no pull request,
because the text of a pull request is written here: Joshua reads the diff of
the branch and the report of the task, composes the title and the body by
``prompts/builtin/skills/developer.md``, shows the text to the owner, and
opens the pull request with ``create_pull_request`` on the ``github`` server
or ``create_merge_request`` on the ``gitlab`` server.

Whether Joshua shows the text to the owner before it opens the pull request is
a setting of the instance, ``developer.pr_skip_approval``, and not a rule of
the product. The setting skips the approval, so an instance that has not
written it asks for the approval: a setting nobody has written has no row, and
no row reads as off. The owner turns the approval off under Admin → Instance,
and then the prompt of a session that holds the ``developer`` server carries
``PR_APPROVAL_OFF`` (``engine/prompts.py``), which tells Joshua to open the
pull request and to give the text afterwards.

``docs/developer.md`` has the flow.
"""

from __future__ import annotations

from joshua_core.store.repo import Repo

# The MCP server of the developer tasks. It ships its own skill page
# (``SERVER_SKILLS`` in ``engine/prompts.py``).
SERVER = "developer"

# The setting that skips the approval of a pull request, and what a value says
# when it is on. Any other value, the empty one included, keeps the approval.
# The false words are what the write of the setting takes beside them, so a
# value neither side reads is refused there.
SKIP_APPROVAL_KEY = "developer.pr_skip_approval"
TRUE_WORDS = ("true", "yes", "on", "1")
FALSE_WORDS = ("false", "no", "off", "0")


def approval_from(value: str | None) -> bool:
    """Whether a stored value of ``developer.pr_skip_approval`` keeps the approval.

    The approval is kept unless the value is one of :data:`TRUE_WORDS`, so a
    missing row and an empty value ask the owner.
    """
    return (value or "").strip().lower() not in TRUE_WORDS


async def approval_required(repo: Repo) -> bool:
    """Whether this instance asks the owner before a pull request is opened."""
    stored = await repo.settings.get(SKIP_APPROVAL_KEY, decrypt=False)
    return approval_from(stored.value if stored is not None else "")
