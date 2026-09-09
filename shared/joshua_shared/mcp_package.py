"""Parse and validate the ``package:`` spec of a stdio MCP server.

Most MCP servers ship as a package, not as a container: an npm package, a PyPI
package, a git repository, or one released executable. A ``package`` entry in
``joshua.yaml`` names one of those, and the gateway installs it into its store
volume at start. This module is the grammar for that name. The gateway installer
consumes the parsed spec; the config loader uses the same parser, so a bad spec
fails ``joshua-config validate`` instead of the gateway boot.

Every kind must pin an exact version, commit, or file hash. A range, a tag such
as ``latest``, and a branch name are refused, because the person who runs Joshua
must get the same code on every install.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# The four ways a server is published.
PACKAGE_KINDS = ("npm", "pypi", "git", "url")

# npm: an optional @scope, then the name. npm itself allows these characters.
_NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
# An exact npm version: 1.2.3, with an optional prerelease and build part.
_NPM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

# PyPI: a normalized-ish distribution name.
_PYPI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# An exact PEP 440 version. `*` and a local-version wildcard are not exact.
_PYPI_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.!+-]*$")

# A git commit. Seven characters is the shortest abbreviation git resolves.
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# A release tag: v1.2.3, 1.2.3, 2026.09.1, each with an optional suffix.
_GIT_TAG_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[-.][0-9A-Za-z.-]+)?$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# What a person writes when they mean "any version". Named so the error can say
# what to write instead.
_UNPINNED = ("latest", "next", "stable", "main", "master", "head", "*", "")

_PIN_HELP = (
    "pin an exact version: npm:<name>@1.2.3, pypi:<name>==1.2.3, "
    "git+https://<url>@<commit>, or an https:// URL with sha256"
)


class PackageError(ValueError):
    """A ``package`` spec that cannot be installed as written."""


@dataclass(frozen=True)
class PackageSpec:
    """One parsed ``package:`` value.

    ``raw`` is the text from the config, and it is what the installer records and
    compares, so a re-parse is never needed to know whether a spec changed.
    """

    kind: str  # one of PACKAGE_KINDS
    raw: str
    name: str  # package name, git URL, or download URL
    version: str  # exact version, commit or tag, or "" for a URL
    sha256: str | None = None  # required for the url kind, unused otherwise

    @property
    def bare_name(self) -> str:
        """The name without an npm scope: ``@scope/thing`` -> ``thing``."""
        return self.name.rsplit("/", 1)[-1]

    @property
    def requirement(self) -> str:
        """The argument for the installer: ``name@version`` or ``name==version``."""
        if self.kind == "npm":
            return f"{self.name}@{self.version}"
        if self.kind == "pypi":
            return f"{self.name}=={self.version}"
        if self.kind == "git":
            return f"{self.name}@{self.version}"
        return self.name


def parse_package(raw: str, sha256: str | None = None) -> PackageSpec:
    """Return the ``PackageSpec`` for ``raw``, or raise ``PackageError``.

    ``sha256`` comes from the entry's own ``sha256:`` field. It is required for a
    plain ``https://`` executable and refused for every other kind, because the
    other kinds pin by version or commit.
    """
    text = (raw or "").strip()
    if not text:
        raise PackageError(f"package is empty; {_PIN_HELP}")

    if text.startswith("npm:"):
        spec = _parse_npm(text)
    elif text.startswith("pypi:"):
        spec = _parse_pypi(text)
    elif text.startswith("git+"):
        spec = _parse_git(text)
    elif text.startswith("https://"):
        return _parse_url(text, sha256)
    elif text.startswith("http://"):
        raise PackageError("package download must use https://, not http://")
    else:
        kinds = ", ".join(PACKAGE_KINDS)
        raise PackageError(f"package must name one of {kinds}; {_PIN_HELP}")

    if sha256 is not None:
        raise PackageError(f"sha256 applies only to an https:// package, not to {spec.kind}")
    return spec


def _reject_unpinned(kind: str, version: str) -> None:
    if version.lower() in _UNPINNED:
        raise PackageError(f"{kind} version '{version}' is not a pin; {_PIN_HELP}")


def _parse_npm(text: str) -> PackageSpec:
    body = text[len("npm:") :]
    name, sep, version = body.rpartition("@")
    if not sep or not name:
        raise PackageError(f"npm package '{body}' has no version; {_PIN_HELP}")
    if not _NPM_NAME_RE.match(name):
        raise PackageError(f"npm package name '{name}' is not valid")
    _reject_unpinned("npm", version)
    if not _NPM_VERSION_RE.match(version):
        raise PackageError(
            f"npm version '{version}' is a range or a tag, not an exact version; {_PIN_HELP}"
        )
    return PackageSpec(kind="npm", raw=text, name=name, version=version)


def _parse_pypi(text: str) -> PackageSpec:
    body = text[len("pypi:") :]
    if "==" not in body:
        raise PackageError(f"pypi package '{body}' must pin with '=='; {_PIN_HELP}")
    name, _, version = body.partition("==")
    if not _PYPI_NAME_RE.match(name):
        raise PackageError(f"pypi package name '{name}' is not valid")
    _reject_unpinned("pypi", version)
    if version.endswith("*") or not _PYPI_VERSION_RE.match(version):
        raise PackageError(f"pypi version '{version}' is not an exact version; {_PIN_HELP}")
    return PackageSpec(kind="pypi", raw=text, name=name, version=version)


def _parse_git(text: str) -> PackageSpec:
    url = text[len("git+") :]
    if not url.startswith("https://"):
        raise PackageError("a git package must use git+https://")
    url_body, sep, ref = url.rpartition("@")
    if not sep or not url_body:
        raise PackageError(f"git package '{url}' names no commit or tag; {_PIN_HELP}")
    if not _GIT_SHA_RE.match(ref) and not _GIT_TAG_RE.match(ref):
        raise PackageError(
            f"git ref '{ref}' is a branch, not a commit or a release tag; {_PIN_HELP}"
        )
    return PackageSpec(kind="git", raw=text, name=url_body, version=ref)


def _parse_url(text: str, sha256: str | None) -> PackageSpec:
    if not sha256:
        raise PackageError("an https:// package needs 'sha256' with the file hash")
    digest = sha256.strip().lower()
    if not _SHA256_RE.match(digest):
        raise PackageError("sha256 must be 64 hexadecimal characters")
    path = urlparse(text).path
    if not path or path.endswith("/"):
        raise PackageError("an https:// package must name a file to download")
    return PackageSpec(kind="url", raw=text, name=text, version="", sha256=digest)


def default_command(spec: PackageSpec) -> str | None:
    """The command to run when the entry sets no ``command``.

    A URL package installs one file, so its name is the command. Every other kind
    installs a bin directory, and the installer picks the executable from it.
    """
    if spec.kind == "url":
        return urlparse(spec.name).path.rsplit("/", 1)[-1]
    return None
