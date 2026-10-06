"""What an image knows about itself: version, revision, ref, and build time.

The release workflow and the branch workflow pass four build arguments into
each Dockerfile, and the Dockerfile exports them as environment. A release has
a version. A branch build has a revision and a ref, and no version. A build
with none of them, such as a plain ``docker build`` on a laptop, is ``dev``.

The image is the unit that has a version. The Python packages keep
``__version__ = "0.0.0"`` on purpose.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_shared.fleet_auth import authenticate, load_fleet_tokens

VERSION_ENV = "JOSHUA_BUILD_VERSION"
REVISION_ENV = "JOSHUA_BUILD_REVISION"
REF_ENV = "JOSHUA_BUILD_REF"
DATE_ENV = "JOSHUA_BUILD_DATE"

# What a build with no arguments reports as its version.
DEV = "dev"

# Who may read `GET /v1/version`. The gateway reads the other two containers for
# the `instance` builtin, and the operator reads any of them from a shell.
VERSION_CALLERS = ("gateway", "laptop")
VERSION_PATH = "/v1/version"


@dataclass(frozen=True)
class BuildInfo:
    """One image's build facts. Every field is a string; an unknown field is empty."""

    version: str
    revision: str
    ref: str
    built_at: str

    def display(self) -> str:
        """One short string for a log line or a sentence.

        A release shows its version, ``2026.10.1``. A branch build shows its ref
        and seven characters of its revision, ``branch-main@10ef1a4``. A build
        that knows nothing shows ``dev``.
        """
        if self.version and self.version != DEV:
            return self.version
        if self.revision:
            return f"{self.ref or 'branch'}@{self.revision[:7]}"
        return DEV

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "revision": self.revision,
            "ref": self.ref,
            "built_at": self.built_at,
            "display": self.display(),
        }


def read(env: Mapping[str, str] = os.environ) -> BuildInfo:
    """Read the build facts from ``env``.

    With none of the four variables set, or all of them empty, the version is
    ``dev``: the image was built with no arguments.
    """
    version = env.get(VERSION_ENV, "").strip()
    revision = env.get(REVISION_ENV, "").strip()
    ref = env.get(REF_ENV, "").strip()
    built_at = env.get(DATE_ENV, "").strip()
    if not version and not revision and not ref:
        version = DEV
    return BuildInfo(version=version, revision=revision, ref=ref, built_at=built_at)


def log_build(logger: logging.Logger, container: str) -> None:
    """Write the one start line that names the build."""
    logger.info({"message": "build", "container": container, **read().as_dict()})


def version_endpoint(container: str):  # type: ignore[no-untyped-def]
    """Build the ``GET /v1/version`` handler for ``container``.

    The handler answers ``gateway`` and ``laptop`` with the build facts. A
    missing or wrong bearer gets 401, another identity 403. The body holds no
    secret and no name of a person.
    """

    async def endpoint(request: Request) -> JSONResponse:
        status, identity = authenticate(
            request.headers.get("authorization"), load_fleet_tokens(), allowed=VERSION_CALLERS
        )
        if status == 401:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if status == 403:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"container": container, **read().as_dict()})

    return endpoint
