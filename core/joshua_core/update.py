"""The release check: the running release of this instance and the latest one.

The image build writes the release tag into the image
(``JOSHUA_RELEASE``), and :func:`running_version` reads it. An image built
with no tag reports ``dev``.

:class:`UpdateCheck` reads the latest release of one repository from the
GitHub releases API, unauthenticated, with a five second timeout, and keeps
the answer for :data:`CACHE_FOR`. A call inside that window gets the kept
answer and makes no call to GitHub, so a page load never reaches GitHub and
the anonymous rate of sixty calls an hour stays far away. A failed call is
kept too: the page says the check did not reach GitHub, and nothing retries
until the window is over.

The repository is the setting ``admin.update_repo``, and ``jakehigg/joshua-ai``
with no setting. A value that is not ``owner/name`` is refused, and the
default stands.

``answer`` is ``{current, latest, url, checked_at, error}``. ``latest`` and
``url`` are the tag and the page of the latest release, ``checked_at`` is the
time of the last check that reached GitHub, and ``error`` is the one line a
failed check shows. The check offers no upgrade and runs none.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from joshua_shared.log import get_logger

logger = get_logger("core.update")

# The environment variable the image build writes the release tag into.
RELEASE_ENV = "JOSHUA_RELEASE"
# What an image with no release tag reports.
DEV_VERSION = "dev"

# The setting that names the repository the check reads, and its default.
REPO_SETTING = "admin.update_repo"
DEFAULT_REPO = "jakehigg/joshua-ai"
# ``owner/name``, as GitHub writes it.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# The releases API of one repository, and the headers of an unauthenticated read.
RELEASES_URL = "https://api.github.com/repos/{repo}/releases/latest"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# The call carries no credential, so it gets the anonymous rate of sixty calls
# an hour. The cache keeps the real rate at a few calls a day.
TIMEOUT_S = 5.0
# How long one answer, good or failed, stands.
CACHE_FOR = timedelta(hours=6)

# The one line a failed check shows. It names no host and no error.
UNREACHABLE = "The release check did not reach GitHub"


def running_version() -> str:
    """The release this instance runs, or ``dev`` for an image with no tag."""
    return os.environ.get(RELEASE_ENV, "").strip() or DEV_VERSION


def is_repo_name(value: str) -> bool:
    """True when ``value`` is a repository as GitHub writes it, ``owner/name``."""
    return bool(_REPO_RE.match(value.strip()))


def repo_name(value: str | None) -> str:
    """The repository the check reads: the setting, or the default.

    A value that is not ``owner/name`` is refused and logged, so a bad setting
    cannot point the check at another address.
    """
    name = (value or "").strip()
    if not name:
        return DEFAULT_REPO
    if not is_repo_name(name):
        logger.warning({"message": "update_repo is not owner/name", "setting": REPO_SETTING})
        return DEFAULT_REPO
    return name


def _release_of(data: Any) -> tuple[str, str]:
    """The tag and the page of the latest release, from the answer of GitHub."""
    if not isinstance(data, dict):
        raise ValueError("the answer is not a release")
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        raise ValueError("the answer names no tag")
    return tag, str(data.get("html_url") or "").strip()


@dataclass
class _Kept:
    """One checked answer and the repository it is about."""

    repo: str
    at: datetime
    latest: str | None
    url: str | None
    error: str | None


class UpdateCheck:
    """The release check, with its cache. Core holds one of these."""

    def __init__(
        self,
        *,
        cache_for: timedelta = CACHE_FOR,
        now: Any = None,
        client_factory: Any = None,
    ) -> None:
        self.cache_for = cache_for
        self._now = now or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._kept: _Kept | None = None
        # The last check that reached GitHub, for the line of a failed one.
        self._good_at: datetime | None = None

    async def answer(self, repo: str) -> dict[str, Any]:
        """The answer of ``GET /admin/update`` for that repository.

        It reads GitHub only when no kept answer of the same repository is
        inside the window.
        """
        now = self._now()
        kept = self._kept
        if kept is None or kept.repo != repo or now - kept.at >= self.cache_for:
            kept = await self._check(repo, now)
        return {
            "current": running_version(),
            "latest": kept.latest,
            "url": kept.url,
            "checked_at": self._good_at.isoformat() if self._good_at else None,
            "error": kept.error,
        }

    async def _check(self, repo: str, now: datetime) -> _Kept:
        """One read of the releases API. A failure is kept as an error."""
        try:
            tag, url = _release_of(await self._fetch(repo))
        except Exception as exc:  # noqa: BLE001 — every failure is the one line
            logger.info({"message": "release check failed", "error": type(exc).__name__})
            kept = _Kept(repo=repo, at=now, latest=None, url=None, error=UNREACHABLE)
        else:
            kept = _Kept(repo=repo, at=now, latest=tag, url=url or None, error=None)
            self._good_at = now
            logger.info({"message": "release check", "repo": repo, "latest": tag})
        self._kept = kept
        return kept

    async def _fetch(self, repo: str) -> Any:
        """The JSON of the latest release of ``repo``. No credential goes on it."""
        import httpx

        factory = self._client_factory or (lambda: httpx.AsyncClient(timeout=TIMEOUT_S))
        async with factory() as client:
            response = await client.get(
                RELEASES_URL.format(repo=repo), headers=HEADERS, timeout=TIMEOUT_S
            )
            response.raise_for_status()
            return response.json()


# The one check of this process: its cache is what keeps the page off GitHub.
CHECK = UpdateCheck()


async def answer(settings_store: Any, check: UpdateCheck | None = None) -> dict[str, Any]:
    """The answer of the route: the repository of the setting, then the check."""
    stored = await settings_store.get(REPO_SETTING, decrypt=False)
    return await (check or CHECK).answer(repo_name(stored.value if stored else None))
