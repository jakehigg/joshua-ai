"""Read GitHub for the ``instance`` builtin: releases, a compare, a file.

Two fixed hosts. Every URL is built here from the ``owner/repo`` strings in
``updates.repos`` and nothing else; a caller cannot name a URL. There is no
token, no cache, and no retry. A rate-limit answer names the reset time, and a
network error names the host, each in one line.

``Fetcher`` takes an async ``fetch`` callable, so the tests drive every path
with no network, the way ``mcp_store`` takes an injected ``download``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from joshua_shared.log import get_logger
from joshua_shared.netblock import block_reason

from joshua_gateway.instance_mcp import versions

logger = get_logger("instance_mcp.github")

API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
ALLOWED_HOSTS = frozenset({API_HOST, RAW_HOST})

# The product, and not the version: GitHub learns that an instance exists at
# this address, and nothing else.
USER_AGENT = "joshua-ai-gateway"
TIMEOUT_S = 15.0
RELEASES_PER_PAGE = 10


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    text: str


Fetch = Callable[[str], Awaitable[Response]]


class CheckError(Exception):
    """One line for the person: why the check could not be done."""


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    published_at: str | None
    url: str | None


@dataclass(frozen=True)
class CatalogAddon:
    name: str  # the directory in joshua-addons
    server: str  # what the addon's MCPServer calls itself; the name when the catalog says nothing
    summary: str
    image: str | None
    docs: str | None
    since: str | None
    requires_joshua_ai: str | None


@dataclass
class Fetcher:
    """Reads for one check. ``blocked`` is the operator's block list."""

    fetch: Fetch
    blocked: Sequence[str] = ()
    resolve_hosts: bool = True
    requests: int = field(default=0, init=False)

    async def _get(self, url: str) -> Response:
        host = httpx.URL(url).host
        if host not in ALLOWED_HOSTS:
            raise CheckError(f"the instance builtin reads only {sorted(ALLOWED_HOSTS)}")
        reason = block_reason(url, list(self.blocked), resolve=self.resolve_hosts)
        if reason is not None:
            raise CheckError(f"the block list refuses {host}: {reason}")
        self.requests += 1
        try:
            response = await self.fetch(url)
        except Exception as exc:  # noqa: BLE001 — one line, no retry
            logger.warning({"message": "update check failed", "host": host, "error": str(exc)})
            raise CheckError(f"{host} did not answer; try again later") from exc
        if response.status in (403, 429):
            raise CheckError(_rate_limited(host, response.headers))
        if response.status == 404:
            raise CheckError(f"{host} has no such file or release")
        if response.status >= 400:
            raise CheckError(f"{host} answered {response.status}")
        return response

    async def latest_release(self, repo: str) -> Release | None:
        """The newest release that is not a draft, or None when there is none."""
        releases = await self.releases(repo)
        return releases[0] if releases else None

    async def releases(self, repo: str) -> list[Release]:
        """The releases GitHub lists, newest first, drafts left out."""
        url = f"https://{API_HOST}/repos/{_repo_path(repo)}/releases?per_page={RELEASES_PER_PAGE}"
        body = _json(await self._get(url))
        if not isinstance(body, list):
            raise CheckError(f"{API_HOST} answered with no release list")
        out: list[Release] = []
        for item in body:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = str(item.get("tag_name") or "")
            version = versions.strip_v(tag)
            if versions.parse(version) is None:
                continue
            out.append(
                Release(
                    version=version,
                    tag=tag,
                    published_at=item.get("published_at"),
                    url=item.get("html_url"),
                )
            )
        return out

    async def compare(self, repo: str, base_tag: str, revision: str) -> dict[str, Any]:
        """``{"status", "ahead_by", "behind_by"}`` of ``revision`` against ``base_tag``."""
        url = (
            f"https://{API_HOST}/repos/{_repo_path(repo)}/compare/"
            f"{quote(base_tag, safe='')}...{quote(revision, safe='')}"
        )
        body = _json(await self._get(url))
        if not isinstance(body, dict):
            raise CheckError(f"{API_HOST} answered with no comparison")
        return {
            "status": body.get("status"),
            "ahead_by": body.get("ahead_by"),
            "behind_by": body.get("behind_by"),
        }

    async def file_at_tag(self, repo: str, tag: str, path: str) -> str:
        """The text of ``path`` in ``repo`` at ``tag``."""
        url = f"https://{RAW_HOST}/{_repo_path(repo)}/{quote(tag, safe='')}/{quote(path)}"
        return (await self._get(url)).text

    async def changelog(self, repo: str, tag: str) -> str:
        return await self.file_at_tag(repo, tag, "docs/CHANGELOG.md")

    async def catalog(self, repo: str, tag: str) -> list[CatalogAddon]:
        text = await self.file_at_tag(repo, tag, "catalog.yaml")
        return parse_catalog(text)


def parse_catalog(text: str) -> list[CatalogAddon]:
    """The addons a ``catalog.yaml`` names. A malformed entry is left out."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise CheckError("the addon catalog could not be read") from exc
    items = data.get("addons") if isinstance(data, dict) else None
    out: list[CatalogAddon] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(
            CatalogAddon(
                name=str(item["name"]),
                server=str(item.get("server") or item["name"]),
                summary=str(item.get("summary") or ""),
                image=_opt(item.get("image")),
                docs=_opt(item.get("docs")),
                since=_opt(item.get("since")),
                requires_joshua_ai=_opt(item.get("requires_joshua_ai")),
            )
        )
    return out


def _opt(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _repo_path(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _json(response: Response) -> Any:
    try:
        return json.loads(response.text)
    except ValueError as exc:
        raise CheckError("GitHub answered with something that is not JSON") from exc


def _rate_limited(host: str, headers: dict[str, str]) -> str:
    reset = {k.lower(): v for k, v in headers.items()}.get("x-ratelimit-reset")
    when = ""
    if reset and reset.isdigit():
        when = datetime.fromtimestamp(int(reset), tz=UTC).strftime(" after %H:%M UTC")
    return f"GitHub limits checks from this address; ask again{when}"


async def http_fetch(url: str) -> Response:
    """The real fetch: one GET, no redirect off the two hosts, no token."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json, text/plain"},
    ) as client:
        response = await client.get(url)
    return Response(status=response.status_code, headers=dict(response.headers), text=response.text)
