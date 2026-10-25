"""Topics: the leaf pages of the wiki that a job works on one at a time.

A topic is a `wiki/**.md` page that is none of these:

- `Home.md`, the front page;
- a section index page: `X.md` with a folder `X/` beside it;
- a page under `journal/`, `people/`, `joshua-docs/`, `skills/`,
  `knowledge/`, `insights/`, `questions/`, or `attachments/`;
- a page with a part of its path that starts with a dot;
- a page that a glob in `jobs.topics.exclude` matches. A glob is an
  `fnmatch` pattern, matched case-sensitively against the path relative to the
  wiki root, and its `*` matches a `/` too;
- a person page: a page whose file stem is the id of a person on the roster,
  or whose title, or a name from its title or file stem, is the id or the
  display name of a person. The comparison is the whole name, in any case.
  The caller gives the roster (`settings.people`); with no roster, this rule
  does not apply.

`houseplants/snake-plant.md` is a topic. `houseplants.md` is not, because the
folder `houseplants/` is beside it.

The names of a topic are the words a text uses for it (`topic_names`): the
title, the title with a parenthetical at its end removed, each alias, and the
first two, three, and four words of the file stem, with hyphens as spaces. A
stem of one word is a name too. `names_pattern` finds a name in a text as whole
words in any case, with any white space between the words. It also finds the
name with a plural `s` or `es`, and without one when the name ends in `s` or
`es`. A name shorter than three characters is not matched.

With a roster, a person's name never makes a text about a topic. These are not
names of the topic:

- the id or the display name of a person;
- that name with `s` or `'s` after it (`alexs`, `alex s`, `Alex's`), and a
  name whose first words are such a form (`alex s caldo`);
- a phrase of the file stem that starts with a person's name, with or without
  an `s` after it.

A possessive names the thing, not its owner. A possessive is a person's name
with `'s` or `s'` after it, or with `'` alone after a name that ends in `s`.
The generic name of a title (`generic_title`) is the title without its
possessive: `Alex's Caldo Verde` gives `Caldo Verde`, and `Pancakes, Alex's
way` gives `Pancakes`. A name that holds a possessive gives its generic name
in its place, so `Alex's Caldo Verde` is not a name and `Caldo Verde` is.
`possessive_forms` gives the forms of the title that hold a possessive, for
the research gate.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from joshua_shared import frontmatter, layout
from joshua_shared.config import Person

HOME_PAGE = "Home.md"
# The top-level wiki folders that hold no topic. Each has its own writer or
# its own purpose.
RESERVED_FOLDERS = (
    "journal",
    "people",
    "joshua-docs",
    "skills",
    layout.KNOWLEDGE,
    layout.INSIGHTS,
    layout.QUESTIONS,
    layout.JOB_RUNS,
    layout.ATTACHMENTS,
)
# A shorter name matches too many words to mean the topic.
MIN_NAME_CHARS = 3
# The phrases of the file stem that name the topic: its first 2 to 4 words.
STEM_PHRASE_WORDS = (2, 3, 4)
_PAREN_TAIL_RE = re.compile(r"\s*\([^()]*\)\s*$")
# An apostrophe, straight or curly.
_APOSTROPHE = "['\u2019]"
# The separators and the short words that join a possessive to the rest of a
# title (`Pancakes, Alex's way`, `Chili in Bo's style`).
_JOIN_CHARS = " ,;:()[]-\u2013\u2014/&+"
_JOIN_WORDS = frozenset(
    {"a", "an", "and", "the", "by", "in", "of", "for", "from", "with", "la", "\u00e0"}
)
_SEPARATOR_RE = re.compile(r"[,;:()\[\]\u2013\u2014]|\s-\s")
# The section of a topic page at the wiki root, for a job page that has a section.
ROOT_SECTION = "general"
_SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


@dataclass(frozen=True)
class Topic:
    """One topic page.

    `path` is relative to the wiki root (`houseplants/snake-plant.md`).
    `section` is its first path segment, or `""` for a page at the wiki root.
    `title` is the first `# ` heading of the body, else the frontmatter
    `title`, else the file stem.
    """

    path: str
    section: str
    title: str


def _excluded(rel_path: str, exclude: Sequence[str]) -> bool:
    return any(fnmatchcase(rel_path, pattern) for pattern in exclude)


def _shape_ok(rel_path: str) -> bool:
    """True when the path, by its text alone, can name a topic page."""
    if not rel_path.endswith(".md") or rel_path.startswith("/") or "\\" in rel_path:
        return False
    parts = rel_path.split("/")
    if any(not part or part.startswith(".") for part in parts):
        return False
    if rel_path == HOME_PAGE:
        return False
    return not (len(parts) > 1 and parts[0] in RESERVED_FOLDERS)


def roster_keys(people: Sequence[Person] | None) -> set[str]:
    """The id and the display name of each person, in one case and one space."""
    keys: set[str] = set()
    for person in people or ():
        for name in (person.id, person.name):
            key = " ".join(str(name).split()).casefold()
            if key:
                keys.add(key)
    return keys


def is_person_page(topic: Topic, people: Sequence[Person] | None) -> bool:
    """True when `topic` is a page about one person on the roster. See the
    module docstring."""
    keys = roster_keys(people)
    if not keys:
        return False
    if PurePosixPath(topic.path).stem.casefold() in keys:
        return True
    return any(name.casefold() in keys for name in _raw_names(topic, ()))


def is_topic_page(
    rel_path: str,
    wiki_root: Path,
    exclude: Sequence[str],
    *,
    people: Sequence[Person] | None = None,
) -> bool:
    """True when `rel_path`, relative to `wiki_root`, is a topic page.

    The page must be a file on the disk. `people` is the roster for the
    person page rule. See the module docstring for the rules.
    """
    if not _shape_ok(rel_path):
        return False
    absolute = wiki_root / rel_path
    if layout.is_hidden(absolute, wiki_root) or not absolute.is_file():
        return False
    if absolute.with_suffix("").is_dir():
        return False
    if _excluded(rel_path, exclude):
        return False
    return not (people and is_person_page(load_topic(rel_path, wiki_root), people))


def page_title(text: str, stem: str) -> str:
    """The title of a page: its first `# ` heading, its frontmatter `title`, or `stem`."""
    meta, body = frontmatter.split(text)
    for line in body.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return stem


def read_page(path: Path) -> str:
    """The text of a page, or `""` when it cannot be read as UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def load_topic(rel_path: str, wiki_root: Path) -> Topic:
    """The `Topic` for a page, read from the disk. It does not check the rules."""
    pure = PurePosixPath(rel_path)
    section = pure.parts[0] if len(pure.parts) > 1 else ""
    title = page_title(read_page(wiki_root / rel_path), pure.stem)
    return Topic(path=rel_path, section=section, title=title)


def list_topics(
    wiki_root: Path, exclude: Sequence[str], *, people: Sequence[Person] | None = None
) -> list[Topic]:
    """Every topic page under `wiki_root`, sorted by path. `people` is the
    roster for the person page rule."""
    wiki_root = Path(wiki_root)
    if not wiki_root.is_dir():
        return []
    found: list[str] = []
    for current, dirs, files in os.walk(wiki_root):
        here = Path(current)
        rel_dir = here.relative_to(wiki_root).as_posix()
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and not (rel_dir == "." and d in RESERVED_FOLDERS)
        ]
        for name in files:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if is_topic_page(rel, wiki_root, exclude, people=people):
                found.append(rel)
    return [load_topic(rel, wiki_root) for rel in sorted(found)]


def section_slug(topic_rel: str) -> str:
    """The section of a topic for a job page: its first path segment as a
    slug, or `general` for a page at the wiki root."""
    parts = PurePosixPath(topic_rel).parts
    if len(parts) < 2:
        return ROOT_SECTION
    first = parts[0]
    if _SECTION_RE.match(first):
        return first
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")[:64].strip("-")
    return slug or ROOT_SECTION


# -- names --------------------------------------------------------------------


def _unique(names: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        name = " ".join(str(name).split())
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _title_names(topic: Topic, aliases: Sequence[str]) -> list[str]:
    """The title, the title without its parenthetical tail, and each alias."""
    return [topic.title, _PAREN_TAIL_RE.sub("", topic.title), *aliases]


def _stem_names(rel_path: str) -> list[str]:
    """The phrases of the file stem that name the topic."""
    words = PurePosixPath(rel_path).stem.replace("-", " ").split()
    found = [words[0]] if len(words) == 1 else []
    found.extend(" ".join(words[:n]) for n in STEM_PHRASE_WORDS if n <= len(words))
    return found


def _raw_names(topic: Topic, aliases: Sequence[str]) -> list[str]:
    """The names of `topic` before the roster rule."""
    return _unique([*_title_names(topic, aliases), *_stem_names(topic.path)])


def _possessive_re(people: Sequence[Person] | None) -> re.Pattern[str] | None:
    """The pattern that finds a possessive of a person in a text, or None with
    no roster. See the module docstring."""
    forms: list[str] = []
    for key in sorted(roster_keys(people), key=lambda k: (-len(k), k)):
        words = r"\s+".join(re.escape(word) for word in key.split())
        forms.extend([f"{words}{_APOSTROPHE}s", f"{words}s{_APOSTROPHE}"])
        if key.endswith("s"):
            forms.append(f"{words}{_APOSTROPHE}")
    if not forms:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(forms)})(?!\w)", re.IGNORECASE)


def _strip_join(text: str, *, end: int) -> str:
    """`text` without the separators and the short joining words at its last
    word (`end=-1`) or at its first word (`end=0`)."""
    words = text.strip(_JOIN_CHARS).split()
    while words and words[end].strip(_JOIN_CHARS).casefold() in _JOIN_WORDS | {""}:
        words.pop(end)
    return " ".join(words).strip(_JOIN_CHARS)


def generic_title(title: str, people: Sequence[Person] | None) -> str:
    """The generic name of `title`: the title without each possessive of a
    person on the roster, or `""` when nothing is left. A title with no
    possessive is unchanged. See the module docstring.

    A possessive at the start leaves the words after it: `Alex's Caldo
    Verde` gives `Caldo Verde`. A possessive after the start removes itself
    and the words after it up to the next separator: `Pancakes, Alex's way`
    gives `Pancakes`.
    """
    text = " ".join(str(title).split())
    pattern = _possessive_re(people)
    if pattern is None:
        return text
    while (match := pattern.search(text)) is not None:
        before = _strip_join(text[: match.start()], end=-1)
        after = text[match.end() :]
        if before:
            cut = _SEPARATOR_RE.search(after)
            after = after[cut.end() :] if cut is not None else ""
            text = " ".join(p for p in (before, after.strip(_JOIN_CHARS)) if p)
        else:
            text = _strip_join(after, end=0)
    return " ".join(text.split())


def possessive_forms(title: str, people: Sequence[Person] | None) -> list[str]:
    """The forms of `title` that hold a possessive of a person: the title and
    the title without its parenthetical tail, longest first. A title with no
    possessive gives `[]`."""
    pattern = _possessive_re(people)
    if pattern is None:
        return []
    forms = _unique([title, _PAREN_TAIL_RE.sub("", title)])
    return sorted((f for f in forms if pattern.search(f)), key=len, reverse=True)


def _starts_with_person(name: str, keys: set[str], *, bare: bool) -> bool:
    """True when the first words of `name` are a person's name with `s` or
    `'s` after it, or, with `bare`, the person's name alone."""
    tokens = name.casefold().replace("\u2019", "'").split()
    for key in keys:
        head = key.split()
        size = len(head)
        if len(tokens) < size or tokens[: size - 1] != head[:-1]:
            continue
        last = tokens[size - 1]
        if last in {f"{head[-1]}s", f"{head[-1]}'s", f"{head[-1]}s'"}:
            return True
        if last == head[-1] and (bare or tokens[size : size + 1] == ["s"]):
            return True
    return False


def drop_people(names: Sequence[str], people: Sequence[Person] | None) -> list[str]:
    """`names` by the roster rule of the module docstring: no bare name of a
    person, no name that starts with a person's name and `s` or `'s`, and the
    generic name in place of each name that holds a possessive."""
    keys = roster_keys(people)
    if not keys:
        return _unique(names)
    found: list[str] = []
    for raw in names:
        name = generic_title(str(raw), people)
        if not name or name.casefold() in keys or _starts_with_person(name, keys, bare=False):
            continue
        found.append(name)
    return _unique(found)


def topic_names(
    topic: Topic, aliases: Sequence[str] = (), *, people: Sequence[Person] | None = None
) -> list[str]:
    """The names of `topic`, once each, in order. With `people`, no name holds
    a person's name by the roster rule. See the module docstring."""
    keys = roster_keys(people)
    if not keys:
        return _raw_names(topic, aliases)
    stems = [s for s in _stem_names(topic.path) if not _starts_with_person(s, keys, bare=True)]
    return drop_people([*_title_names(topic, aliases), *stems], people)


def _forms(name: str) -> list[str]:
    """`name`, and `name` without the plural end of its last word. A last
    word that is only the plural end keeps it."""
    forms = [name]
    lower = name.casefold()
    last = lower.split()[-1] if lower.split() else ""
    if last.endswith("es") and len(last) > 2:
        forms.append(name[:-2])
    if last.endswith("s") and not last.endswith("ss") and len(last) > 1:
        forms.append(name[:-1])
    return [f for f in forms if len(f) >= MIN_NAME_CHARS]


def names_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    """The pattern that finds one of `names` in a text, by the rule of the
    module docstring, or None when no name is long enough."""
    forms = {f for name in names for f in _forms(" ".join(str(name).split()))}
    if not forms:
        return None
    body = "|".join(
        r"\s+".join(re.escape(word) for word in form.split())
        for form in sorted(forms, key=lambda f: (-len(f), f))
    )
    return re.compile(rf"(?<!\w)(?:{body})(?:e?s)?(?!\w)", re.IGNORECASE)
