"""The developer jobs of the addon, as the charm of a workspace reads them.

The developer addon runs a coding job on a repository: it writes the branch,
pushes it, and reports what it did (``docs/developer.md``). The viewer reads
the jobs that have not ended, so the charm beside the name of a workspace says
that Joshua is working on one, or that one asks the person
(``viewer_workspaces``). Nothing else here reads the addon.

The read route is ``GET /v1/jobs`` on the addon, with the bearer in the
``Authorization`` header, the shape of its MCP endpoint. It answers
``{"jobs": [...]}``, the active jobs with no query, and a comma-separated
``status`` filters. Each job carries:

``id``, ``kind``, ``project``, ``branch``, ``status``, ``started_at``,
``notify_channel``, ``question``, ``tokens`` and ``cost``.

The charm reads four of them: ``id``, ``status``, ``question``, and
``notify_channel``. A status of :data:`ACTIVE` is a job in flight; any other
name has ended and carries no charm. A job that asked the person stays
active and carries the question, so waiting comes from that field and from no
status name. ``notify_channel`` is the channel the addon reports to, which is
``workspace:<id>`` for a job a workspace dispatched: :func:`workspace_of`
reads the id out of it, and a job of another channel belongs to no workspace.

The viewer needs ``DEVELOPER_URL`` and ``DEVELOPER_TOKEN``, as the assignments
manager needs ``ASSIGNMENTS_URL`` and its token. With either one missing,
:func:`from_env` returns None and the charm is what the other sources say. A
call that fails, times out, or answers anything but 200 is one log line and an
empty list, so the workspace list never breaks on the addon.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, ValidationError

logger = get_logger("viewer.developer")

DEVELOPER_URL_ENV = "DEVELOPER_URL"
DEVELOPER_TOKEN_ENV = "DEVELOPER_TOKEN"
JOBS_PATH = "/v1/jobs"

# A person waits for the workspace list, so a slow addon must not hold it.
TIMEOUT_S = 2.0

# The statuses of a job that has not ended. ``in_progress`` is the name an
# older developer service gives a running job. Every other name has ended.
ACTIVE = frozenset({"dispatched", "running", "in_progress"})

# The channel of a workspace, which a job of that workspace reports to.
CHANNEL_PREFIX = "workspace:"


class Job(BaseModel):
    """One job of the addon. A newer addon can add a field, so an unknown
    field is ignored, and a field this charm does not read is left out."""

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str = ""
    status: str = ""
    question: str | None = None
    notify_channel: str | None = None

    @property
    def active(self) -> bool:
        """True while the job has not ended."""
        return self.status in ACTIVE


class JobList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[Job]


def workspace_of(channel: str | None) -> str:
    """The id of the workspace a job reports to, or "" for a job that reports
    to another channel or to none."""
    text = (channel or "").strip()
    if not text.startswith(CHANNEL_PREFIX):
        return ""
    return text[len(CHANNEL_PREFIX) :].strip()


class DeveloperClient:
    """Read the jobs of the developer addon."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def active_jobs(self) -> list[Job]:
        """The jobs of the addon that have not ended. An addon that does not
        answer, or answers a shape this viewer does not know, gives an empty
        list: the charm is then what the other sources say."""
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.get_json(JOBS_PATH)
        except httpx.HTTPError as exc:
            logger.warning({"message": "developer jobs read failed", "error": type(exc).__name__})
            return []
        if response.status_code != 200:
            logger.warning(
                {"message": "developer jobs read refused", "status": response.status_code}
            )
            return []
        try:
            body = response.json()
        except ValueError:
            logger.warning({"message": "developer jobs read answered an unknown shape"})
            return []
        try:
            answer = JobList.model_validate(body)
        except ValidationError:
            logger.warning({"message": "developer jobs read answered an unknown shape"})
            return []
        return [job for job in answer.jobs if job.active]


def from_env(env: Mapping[str, str]) -> DeveloperClient | None:
    """A client from ``DEVELOPER_URL`` and ``DEVELOPER_TOKEN``, or None."""
    base_url = env.get(DEVELOPER_URL_ENV)
    token = env.get(DEVELOPER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return DeveloperClient(base_url, token)
