"""The `/data` volume layout, shared by channels, core, and gateway.

Every path under the data volume comes from this module, so the three containers
agree on where a file lives. The functions build paths; `bootstrap_person`,
`bootstrap_wiki`, `bootstrap_shared`, and `bootstrap_shared_profile` create the
tree; `migrate_to_one_wiki` moves an older layout onto this one;
`validate_layout` reports problems for `/readyz`.

There is one wiki, at `<data>/wiki/`. It is Joshua's memory:

- `Home.md` is the front page. It is created once, from a template, and never
  rewritten, so a person's own edits to it survive every start.
- `joshua/` is the documentation the repo ships. Core replaces it at each
  start.
- `journal/YYYY/MM/DD/` is Joshua's day-by-day memory: the nightly page for
  that day, and any entry Joshua wrote during it. This is Joshua's own
  episodic memory now, not a person's.
- `people/<id>.md` is Joshua's profile of a person. `people/everyone.md` is
  the shared profile. `everyone` is reserved: no person may use it as an id.

Every person reads the wiki and a member writes it. A person's own files
outside the wiki are `attachments/` and, for the terminal channel, `cli/`,
under `<data>/people/<id>/`.

Production mounts the volume at `/data`. Tests override the root with the
`JOSHUA_DATA_DIR` environment variable. A function also takes an optional `root`
so a caller with its own data directory (the people CLI, the registration tool)
can pass it directly.
"""

from __future__ import annotations

import os
import re
from datetime import date
from importlib import resources
from pathlib import Path

from joshua_shared.fs import is_writable
from joshua_shared.ids import PERSON_ID_RE
from joshua_shared.log import get_logger

# The default mount point. `JOSHUA_DATA_DIR` overrides it for tests.
DATA = Path("/data")
DATA_DIR_ENV = "JOSHUA_DATA_DIR"

# The kinds `person_dir` accepts. The profile page lives in the wiki now,
# reached with `profile_path`.
PERSON_KINDS = ("attachments",)

DOCS_DIR_ENV = "JOSHUA_DOCS_DIR"
DEFAULT_DOCS_DIR = "/app/docs"

# The directories `bootstrap_person` creates under a person's root.
_PERSON_DIRS = ("attachments",)

# The directories `bootstrap_wiki` creates under the wiki.
_WIKI_DIRS = ("skills", "journal", "people")

# `everyone` names the shared profile page, so no person id may use it.
SHARED_PROFILE_NAME = "everyone"

_PERSON_ID_RE = PERSON_ID_RE
_JOURNAL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

# A pre-single-wiki `blog/` post: the nightly digest, `YYYY-MM-DD.md` ...
_LEGACY_DIGEST_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md\Z")
# ... or an in-conversation post, `YYYY-MM-DD-HHMM-<slug>.md`. The HHMM group
# is kept: it disambiguates two same-day, same-slug posts by one person.
_LEGACY_ENTRY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{4})-(.+)\.md\Z")

# Front matter this module adds during migration: a `---` block at the top
# of the file, non-greedy so a later `---` (a markdown rule) is not mistaken
# for the closing fence.
_FRONT_MATTER_RE = re.compile(r"\A---\n(?P<body>.*?)\n---\n?", re.DOTALL)

_logger = get_logger("joshua_shared.layout")


def safe_segment(pid: str) -> str:
    """Return `pid` if it is a valid person id, else raise `ValueError`.

    A person id is a slug (`^[a-z0-9][a-z0-9-]{0,31}$`). The check blocks a path
    segment that could escape the data volume (`..`, `/`, an absolute path).
    """
    if not _PERSON_ID_RE.match(pid):
        raise ValueError(f"invalid person id: {pid!r}")
    return pid


def is_hidden(path: Path, base: Path) -> bool:
    """True when a part of `path`, below `base`, starts with a dot.

    A wiki frontend or a note tool keeps its own state inside the data volume
    as a dot directory: `.git`, `.obsidian`, `.trash`, and so on. Every place
    that lists, searches, or indexes the corpus uses this one rule to ignore
    such an entry, at any depth, so a tool's own state never counts as content.

    Only the parts of `path` relative to `base` count. `base` itself, and a
    dot anywhere above it (a host temp directory, for example), never make the
    result true. A `path` that is not under `base` returns `False`.
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    return any(part.startswith(".") for part in parts)


def data_root() -> Path:
    """The data volume root: `JOSHUA_DATA_DIR` if set, else `/data`."""
    return Path(os.environ.get(DATA_DIR_ENV) or DATA)


def _root(root: Path | str | None) -> Path:
    return data_root() if root is None else Path(root)


def person_root(pid: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>`."""
    return _root(root) / "people" / safe_segment(pid)


def person_dir(pid: str, kind: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>/<kind>` for `kind` in `attachments`."""
    if kind not in PERSON_KINDS:
        raise ValueError(f"kind must be one of {PERSON_KINDS}, got {kind!r}")
    return person_root(pid, root) / kind


def legacy_blog_dir(pid: str, root: Path | str | None = None) -> Path:
    """`<data>/people/<pid>/blog`, the pre-single-wiki journal.

    `migrate_to_one_wiki` uses this to find posts to move. Nothing else
    writes here any more.
    """
    return person_root(pid, root) / "blog"


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


def home_path(root: Path | str | None = None) -> Path:
    """`<data>/wiki/Home.md`, the wiki's front page."""
    return wiki_root(root) / "Home.md"


def people_pages_root(root: Path | str | None = None) -> Path:
    """`<data>/wiki/people`, Joshua's profile pages."""
    return wiki_root(root) / "people"


def profile_path(pid: str, root: Path | str | None = None) -> Path:
    """`<data>/wiki/people/<pid>.md`, Joshua's profile of a person.

    `everyone` is reserved for the shared profile; use `shared_profile_path`
    for it. Raises `ValueError` for `everyone` or an invalid person id.
    """
    if pid == SHARED_PROFILE_NAME:
        raise ValueError(f"{SHARED_PROFILE_NAME!r} is reserved for the shared profile")
    safe_segment(pid)
    return people_pages_root(root) / f"{pid}.md"


def shared_profile_path(root: Path | str | None = None) -> Path:
    """`<data>/wiki/people/everyone.md`, the shared profile."""
    return people_pages_root(root) / f"{SHARED_PROFILE_NAME}.md"


def journal_root(root: Path | str | None = None) -> Path:
    """`<data>/wiki/journal`, Joshua's day-by-day memory."""
    return wiki_root(root) / "journal"


def journal_day_dir(day: date, root: Path | str | None = None) -> Path:
    """`<data>/wiki/journal/YYYY/MM/DD`."""
    return journal_root(root) / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"


def journal_day_page(day: date, root: Path | str | None = None) -> Path:
    """`<data>/wiki/journal/YYYY/MM/DD/YYYY-MM-DD.md`, the nightly page for that day."""
    return journal_day_dir(day, root) / f"{day.isoformat()}.md"


def journal_entry_path(day: date, slug: str, root: Path | str | None = None) -> Path:
    """`<data>/wiki/journal/YYYY/MM/DD/<slug>.md`, an entry Joshua wrote that day.

    `slug` matches `^[a-z0-9][a-z0-9-]{0,63}$`. It must differ from the day's
    own page name (`YYYY-MM-DD`), or the entry would collide with the nightly
    page. Raises `ValueError` for a bad slug.
    """
    if not _JOURNAL_SLUG_RE.match(slug):
        raise ValueError(f"invalid journal slug: {slug!r}")
    if slug == day.isoformat():
        raise ValueError(f"journal slug cannot be the day page name: {slug!r}")
    return journal_day_dir(day, root) / f"{slug}.md"


def journal_day_from_path(path: Path, root: Path | str | None = None) -> date | None:
    """The day a journal path belongs to, parsed from `YYYY/MM/DD` under `journal_root`.

    Returns `None` when `path` is not under `journal_root`, or the first three
    parts under it are not a valid `YYYY/MM/DD` (this includes `journal/legacy/`).
    """
    try:
        parts = Path(path).relative_to(journal_root(root)).parts
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def inbox_root(root: Path | str | None = None) -> Path:
    """`<data>/inbox`."""
    return _root(root) / "inbox"


def _template(name: str) -> str:
    return resources.files("joshua_shared").joinpath("templates", name).read_text()


def bootstrap_person(pid: str, display_name: str, root: Path | str | None = None) -> None:
    """Create a person's directory tree and profile. Idempotent.

    Creates `attachments/` under `people/<pid>/`. Writes the profile at
    `wiki/people/<pid>.md` from the template only when it is absent, so a
    second call changes nothing.
    """
    home = person_root(pid, root)
    for sub in _PERSON_DIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    profile = profile_path(pid, root)
    if not profile.exists():
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(_template("profile.md").replace("<Display name>", display_name))


def bootstrap_wiki(root: Path | str | None = None) -> None:
    """Create `wiki/`, its `skills/`, `journal/`, and `people/`, and the front page. Idempotent.

    Writes `Home.md` from the template only when it is absent, and never
    touches it again, so a person's edits to their own front page survive
    every start. Removes `wiki/README.md` if an older layout left one: the
    front page is `Home.md` now, and a person read the repository README
    before they ever ran Joshua.
    """
    wiki = wiki_root(root)
    wiki.mkdir(parents=True, exist_ok=True)
    for sub in _WIKI_DIRS:
        (wiki / sub).mkdir(parents=True, exist_ok=True)
    readme = wiki / "README.md"
    if readme.is_file():
        readme.unlink()
    home = home_path(root)
    if not home.exists():
        home.write_text(_template("wiki_home.md"))


def bootstrap_shared(root: Path | str | None = None) -> None:
    """Create `shared/attachments/`. Idempotent.

    The shared profile is not here any more; it lives in the wiki. See
    `bootstrap_shared_profile`.
    """
    (shared_root(root) / "attachments").mkdir(parents=True, exist_ok=True)


def bootstrap_shared_profile(name: str = "Joshua", root: Path | str | None = None) -> None:
    """Create the shared profile at `wiki/people/everyone.md`. Idempotent.

    Writes it from the template only when it is absent, so a second call
    changes nothing. Kept separate from `bootstrap_wiki`, which needs no
    name, so the two concerns (the wiki skeleton, and this one titled page)
    stay apart. Call it after `bootstrap_wiki` has made `wiki/people/`.
    """
    profile = shared_profile_path(root)
    profile.parent.mkdir(parents=True, exist_ok=True)
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


def _add_front_matter_keys(text: str, pid: str, day: date) -> str:
    """Ensure `text` starts with front matter that has `people` and `date` keys.

    Adds a `---` block when `text` has none. Adds a missing key without
    touching one that is already there, so a value set by hand before the
    move survives.
    """
    date_str = day.isoformat()
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return f"---\npeople: [{pid}]\ndate: {date_str}\n---\n{text}"
    body = match.group("body")
    lines = body.split("\n") if body else []
    if not any(line.startswith("people:") for line in lines):
        lines.append(f"people: [{pid}]")
    if not any(line.startswith("date:") for line in lines):
        lines.append(f"date: {date_str}")
    rest = text[match.end() :]
    return f"---\n{chr(10).join(lines)}\n---\n{rest}"


def _migrate_person_blog(pid: str, root: Path | str | None, counts: dict[str, int]) -> None:
    """Move `people/<pid>/blog/*.md` onto the journal. See `migrate_to_one_wiki`."""
    blog = legacy_blog_dir(pid, root)
    if not blog.is_dir():
        return
    for file in sorted(blog.iterdir()):
        if not file.is_file():
            continue
        digest = _LEGACY_DIGEST_RE.match(file.name)
        entry = _LEGACY_ENTRY_RE.match(file.name)
        day: date | None
        if digest:
            day = date(int(digest.group(1)), int(digest.group(2)), int(digest.group(3)))
            # `YYYY-MM-DD.md` is reserved for the new instance-wide nightly
            # page, so an old per-person digest gets a page of its own,
            # named by the person id.
            target = journal_day_dir(day, root) / f"{pid}.md"
        elif entry:
            day = date(int(entry.group(1)), int(entry.group(2)), int(entry.group(3)))
            slug = entry.group(5)
            target = journal_day_dir(day, root) / f"{pid}-{slug}.md"
            if target.exists():
                # Two posts, same person, same day, same slug: fall back to
                # the source file's own time so the second post is not
                # skipped and left behind.
                target = journal_day_dir(day, root) / f"{pid}-{slug}-{entry.group(4)}.md"
        else:
            day = None
            target = journal_root(root) / "legacy" / pid / file.name

        if target.exists():
            counts["skipped"] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if day is None:
            os.replace(file, target)
            counts["journal_legacy"] += 1
        else:
            target.write_text(_add_front_matter_keys(file.read_text(encoding="utf-8"), pid, day))
            file.unlink()
            counts["journal_moved"] += 1

    if not any(blog.iterdir()):
        blog.rmdir()


def _is_unedited_template(target: Path, template_name: str) -> bool:
    """True when `target` is still the shipped template, nobody's touched it.

    `bootstrap_person` and `bootstrap_shared_profile` render the template by
    substituting a display name into its first line only; every other line is
    fixed. `migrate_to_one_wiki` runs before it knows any display name, so it
    compares the two texts with the first line dropped from each.
    """
    template_rest = _template(template_name).split("\n", 1)[1:]
    target_rest = target.read_text(encoding="utf-8").split("\n", 1)[1:]
    return target_rest == template_rest


def _migrate_person_profile(pid: str, root: Path | str | None, counts: dict[str, int]) -> None:
    """Move `people/<pid>/profile.md` to `wiki/people/<pid>.md`. See `migrate_to_one_wiki`."""
    legacy = person_root(pid, root) / "profile.md"
    if not legacy.is_file():
        return
    target = profile_path(pid, root)
    if target.exists() and not _is_unedited_template(target, "profile.md"):
        counts["skipped"] += 1
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(legacy, target)
    counts["profiles_moved"] += 1


def _migrate_shared_profile(root: Path | str | None, counts: dict[str, int]) -> None:
    """Move `shared/profile.md` to `wiki/people/everyone.md`. See `migrate_to_one_wiki`."""
    legacy = shared_root(root) / "profile.md"
    if not legacy.is_file():
        return
    target = shared_profile_path(root)
    if target.exists() and not _is_unedited_template(target, "shared_profile.md"):
        counts["skipped"] += 1
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(legacy, target)
    counts["shared_profile_moved"] += 1


def _remove_stale_readmes(root: Path | str | None, counts: dict[str, int]) -> None:
    """Delete `shared/README.md` and `wiki/README.md`, which `Home.md` replaces."""
    for readme in (shared_root(root) / "README.md", wiki_root(root) / "README.md"):
        if readme.is_file():
            readme.unlink()
            counts["readmes_removed"] += 1


def migrate_to_one_wiki(root: Path | str | None = None) -> dict[str, int]:
    """Move a pre-single-wiki layout onto the wiki. Idempotent; safe at every start.

    For each `people/<id>/`: moves `blog/*.md` into the journal (a digest
    `YYYY-MM-DD.md` becomes `journal/YYYY/MM/DD/<id>.md` — `YYYY-MM-DD.md`
    itself is reserved for the new instance-wide nightly page; an entry
    `YYYY-MM-DD-HHMM-<slug>.md` becomes `journal/YYYY/MM/DD/<id>-<slug>.md`,
    the id prefixed so two people's entries never collide; if that name is
    already taken (two posts, one person, one day, the same slug), it falls
    back to `journal/YYYY/MM/DD/<id>-<slug>-HHMM.md`, the time taken from the
    source name; any other file name moves untouched to
    `journal/legacy/<id>/<name>`), and moves `profile.md` to
    `wiki/people/<id>.md`. A moved journal file gets front matter with
    `people: [<id>]` and `date: YYYY-MM-DD` added, either to an existing
    `---` block or as a new one, and any existing key is left alone.

    Also moves `shared/profile.md` to `wiki/people/everyone.md`, and deletes
    `shared/README.md` and `wiki/README.md` (the front page, `Home.md`,
    replaces them).

    A target that already exists and is not still the shipped, unedited
    template is left alone: the source stays in place and the move counts
    under `skipped`, not under its own kind. When the existing target *is*
    still the unedited template (`bootstrap_person` or
    `bootstrap_shared_profile` wrote it and nobody has changed it since),
    this replaces it with the real file instead of skipping — a template
    written before this ran must never shadow the profile it stands in for.
    An empty `blog/` is removed once its files are gone. `everyone` is
    reserved for the shared profile, so a `people/everyone/` directory, if
    one ever existed, is left untouched.

    Returns a count for `journal_moved`, `journal_legacy`, `profiles_moved`,
    `shared_profile_moved`, `skipped`, and `readmes_removed`. Logs one INFO
    line with these counts when anything moved.
    """
    counts = {
        "journal_moved": 0,
        "journal_legacy": 0,
        "profiles_moved": 0,
        "shared_profile_moved": 0,
        "skipped": 0,
        "readmes_removed": 0,
    }

    people = _root(root) / "people"
    if people.is_dir():
        for entry in sorted(people.iterdir()):
            if not entry.is_dir() or entry.name == SHARED_PROFILE_NAME:
                continue
            _migrate_person_blog(entry.name, root, counts)
            _migrate_person_profile(entry.name, root, counts)

    _migrate_shared_profile(root, counts)
    _remove_stale_readmes(root, counts)

    if any(counts.values()):
        _logger.info({"message": "migrated to one wiki", **counts})
    return counts


def validate_layout(root: Path | str | None = None) -> list[str]:
    """Return a list of layout problems. An empty list means the layout is good.

    Checks the data root exists and is writable, `wiki/` and `shared/` exist,
    and every person directory carries `attachments/` and a profile page at
    `wiki/people/<id>.md`. `/readyz` reports `layout: false` when the list is
    not empty.

    The writability check writes a probe file. The mode bits are not the answer
    on an NFS export, where the server decides; see `joshua_shared.fs`.
    """
    base = _root(root)
    problems: list[str] = []
    if not base.is_dir():
        return [f"data root missing: {base}"]
    if not is_writable(base):
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
            try:
                profile = profile_path(entry.name, root)
            except ValueError:
                problems.append(f"people/{entry.name} is not a valid person id")
                continue
            if not profile.is_file():
                problems.append(f"wiki/people/{entry.name}.md missing")
    return problems
