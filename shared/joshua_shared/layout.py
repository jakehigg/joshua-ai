"""The `/data` volume layout, shared by channels, core, and gateway.

Every path under the data volume comes from this module, so the three containers
agree on where a file lives. The functions build paths; `bootstrap_person`,
`bootstrap_wiki`, and `bootstrap_shared` create the tree; `validate_layout`
reports problems for `/readyz`.

There is one wiki, at `<data>/wiki/`. Every person reads it and a member writes
it. A person's own files are `profile.md`, `blog/`, and `attachments/` under
`<data>/people/<id>/`.

Production mounts the volume at `/data`. Tests override the root with the
`JOSHUA_DATA_DIR` environment variable. A function also takes an optional `root`
so a caller with its own data directory (the people CLI, the registration tool)
can pass it directly.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from joshua_shared.ids import PERSON_ID_RE

# The default mount point. `JOSHUA_DATA_DIR` overrides it for tests.
DATA = Path("/data")
DATA_DIR_ENV = "JOSHUA_DATA_DIR"

# The kinds `person_dir` accepts. `profile.md` is a single file, reached with
# `profile_path`.
PERSON_KINDS = ("blog", "attachments")

DOCS_DIR_ENV = "JOSHUA_DOCS_DIR"
DEFAULT_DOCS_DIR = "/app/docs"

# The directories `bootstrap_person` creates under a person's root.
_PERSON_DIRS = ("blog", "attachments")

# The directories `bootstrap_wiki` creates under the wiki.
_WIKI_DIRS = ("skills",)

_PERSON_ID_RE = PERSON_ID_RE


def safe_segment(pid: str) -> str:
    """Return `pid` if it is a valid person id, else raise `ValueError`.

    A person id is a slug (`^[a-z0-9][a-z0-9-]{0,31}$`). The check blocks a path
    segment that could escape the data volume (`..`, `/`, an absolute path).
    """
    if not _PERSON_ID_RE.match(pid):
        raise ValueError(f"invalid person id: {pid!r}")
    return pid


def data_root() -> Path:
    """The data volume root: `JOSHUA_DATA_DIR` if set, else `/data`."""
    return Path(os.environ.get(DATA_DIR_ENV) or DATA)


def _root(root: Path | str | None) -> Path:
    return data_root() if root is None else Path(root)


def person_root(pid: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>`."""
    return _root(root) / "people" / safe_segment(pid)


def person_dir(pid: str, kind: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>/<kind>` for `kind` in `blog`, `attachments`."""
    if kind not in PERSON_KINDS:
        raise ValueError(f"kind must be one of {PERSON_KINDS}, got {kind!r}")
    return person_root(pid, root) / kind


def profile_path(pid: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>/profile.md`."""
    return person_root(pid, root) / "profile.md"


def shared_root(root: Path | str | None = None) -> Path:
    """`<data>/shared`."""
    return _root(root) / "shared"


def wiki_root(root: Path | str | None = None) -> Path:
    """`<data>/wiki`, the one wiki."""
    return _root(root) / "wiki"


def docs_root(root: Path | str | None = None) -> Path:
    """`<data>/wiki/joshua`, the documentation the repo ships.

    The name says where the files come from, so nobody mistakes the directory
    for their own and keeps notes in it.
    """
    return wiki_root(root) / "joshua"


def inbox_root(root: Path | str | None = None) -> Path:
    """`<data>/inbox`."""
    return _root(root) / "inbox"


def shared_profile_path(root: Path | str | None = None) -> Path:
    """`<data>/shared/profile.md`."""
    return shared_root(root) / "profile.md"


def _template(name: str) -> str:
    return resources.files("joshua_shared").joinpath("templates", name).read_text()


def bootstrap_person(pid: str, display_name: str, root: Path | str | None = None) -> None:
    """Create a person's directory tree and profile. Idempotent.

    Creates `blog/` and `attachments/`. Writes
    `profile.md` from the template only when it is absent, so a second call
    changes nothing.
    """
    home = person_root(pid, root)
    for sub in _PERSON_DIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    profile = profile_path(pid, root)
    if not profile.exists():
        profile.write_text(_template("profile.md").replace("<Display name>", display_name))


def bootstrap_wiki(root: Path | str | None = None) -> int:
    """Create `wiki/` with its README and `skills/`. Idempotent.

    Also moves the pages of a pre-single-wiki layout: every file under
    `people/<id>/wiki/` goes to `wiki/<id>/`, and the empty directory is
    removed. Returns the number of pages moved. A page that already exists at
    the target stays where it is.
    """
    wiki = wiki_root(root)
    wiki.mkdir(parents=True, exist_ok=True)
    for sub in _WIKI_DIRS:
        (wiki / sub).mkdir(parents=True, exist_ok=True)
    readme = wiki / "README.md"
    if not readme.exists():
        readme.write_text(_template("wiki_readme.md"))

    moved = 0
    people = _root(root) / "people"
    if people.is_dir():
        for entry in sorted(people.iterdir()):
            legacy = entry / "wiki"
            if not legacy.is_dir():
                continue
            for file in sorted(legacy.rglob("*")):
                if not file.is_file():
                    continue
                target = wiki / entry.name / file.relative_to(legacy)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                file.rename(target)
                moved += 1
            for directory in sorted(legacy.rglob("*"), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            if not any(legacy.iterdir()):
                legacy.rmdir()
    return moved


def bootstrap_shared(name: str = "Joshua", root: Path | str | None = None) -> None:
    """Create `shared/` with its README and the shared profile. Idempotent.

    Writes `README.md` and `shared/profile.md` from templates only when they are
    absent, so a second call changes nothing.
    """
    shared = shared_root(root)
    shared.mkdir(parents=True, exist_ok=True)
    readme = shared / "README.md"
    if not readme.exists():
        readme.write_text(_template("shared_readme.md"))
    profile = shared_profile_path(root)
    if not profile.exists():
        profile.write_text(_template("shared_profile.md").replace("<Name>", name))


def bootstrap_docs(root: Path | str | None = None, source: Path | str | None = None) -> int:
    """Copy the shipped documentation into `wiki/joshua/`. Returns the file count.

    These files belong to Joshua, not to the people, so each start replaces
    them and an upgrade brings the new text. `source` defaults to
    `JOSHUA_DOCS_DIR`, then to `/app/docs`. A source that is not there returns 0,
    which keeps a dev checkout and the offline tests working.
    """
    src = Path(source or os.environ.get(DOCS_DIR_ENV) or DEFAULT_DOCS_DIR)
    if not src.is_dir():
        return 0
    target = docs_root(root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "README.md").write_text(_template("docs_readme.md"))
    count = 0
    for page in sorted(src.glob("*.md")):
        (target / page.name).write_text(page.read_text())
        count += 1
    return count


def validate_layout(root: Path | str | None = None) -> list[str]:
    """Return a list of layout problems. An empty list means the layout is good.

    Checks the data root exists and is writable, `wiki/` and `shared/` exist,
    and every person directory carries its subdirectories and `profile.md`. `/readyz`
    reports `layout: false` when the list is not empty.
    """
    base = _root(root)
    problems: list[str] = []
    if not base.is_dir():
        return [f"data root missing: {base}"]
    if not os.access(base, os.W_OK):
        problems.append(f"data root not writable: {base}")
    if not wiki_root(root).is_dir():
        problems.append("wiki/ missing")
    if not shared_root(root).is_dir():
        problems.append("shared/ missing")

    people = base / "people"
    if people.is_dir():
        for entry in sorted(people.iterdir()):
            if not entry.is_dir():
                continue
            for sub in _PERSON_DIRS:
                if not (entry / sub).is_dir():
                    problems.append(f"people/{entry.name}/{sub}/ missing")
            if not (entry / "profile.md").is_file():
                problems.append(f"people/{entry.name}/profile.md missing")
    return problems
