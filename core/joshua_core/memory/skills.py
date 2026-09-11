"""Taught skills — parsing and phrase matching for ``wiki/skills/<slug>.md``.

A taught skill is a Markdown file a member writes to the shared wiki. The
frontmatter says what the page is, who it is for, and how it is matched; the
body is the instructions the agent follows.

Two kinds of page live in the folder:

- ``kind: command`` is a behaviour a person asks for by name. It holds trigger
  phrases and it fires when one of them matches the turn. It can change the
  state of the world, so it must fire only when a person asks for it.
- ``kind: convention`` is reference material for a domain. It holds no
  triggers and it never fires from a phrase. The wiki index still holds it, so
  a turn reaches it through ordinary recall.

A command page is matched by phrase, not by meaning. ``match: phrase`` (the
default) is near-exact: the turn must open with the trigger, and it may hold
only a small number of extra words. ``match: semantic`` opts the page in to the
vector match for an intent that is genuinely said many ways. Match strictness
follows the cost of a wrong fire: a page that only shapes an answer can be
loose, a page that calls a device must not be.

This module is dependency-light (stdlib only) so the offline parser tests import
it without psycopg.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from joshua_shared.log import get_logger

logger = get_logger("memory.skills")

# The kind stored on every trigger row. It is filtered out of ordinary recall.
SKILL_KIND = "skill"

# Page kinds. ``command`` fires from a trigger phrase; ``convention`` never does.
KIND_COMMAND = "command"
KIND_CONVENTION = "convention"
KINDS = (KIND_COMMAND, KIND_CONVENTION)

# Match modes. ``phrase`` is the near-exact lexical match in this module;
# ``semantic`` is the vector match in ``engine/taught_skills.py``.
MATCH_PHRASE = "phrase"
MATCH_SEMANTIC = "semantic"
MATCH_MODES = (MATCH_PHRASE, MATCH_SEMANTIC)

# Audience words that name a role instead of one person.
AUDIENCE_EVERYONE = "everyone"
AUDIENCE_MEMBERS = "members"
AUDIENCE_GUESTS = "guests"
# The words that mean "everyone". A skill is usually taught by talking, so the
# agent writes this value and it does not always write the one word in the
# documentation. Each of these is unambiguous, and reading them costs nothing.
# Without them "all" parses as a person id, matches nobody, and the skill dies
# quietly with no warning to say why.
_EVERYONE_WORDS = frozenset({AUDIENCE_EVERYONE, "all", "anyone", "anybody", "everybody", "any"})
_ROLE_WORDS = {
    AUDIENCE_MEMBERS: "member",
    "member": "member",
    AUDIENCE_GUESTS: "guest",
    "guest": "guest",
}
# "a and b", written out. A person id cannot hold a space, so this never splits
# one in half.
_AND_RE = re.compile(r"\s+and\s+|\s*&\s*")

# A skill file is ``wiki/skills/<slug>.md`` at that exact depth. A file deeper in
# the tree (``wiki/skills/sub/x.md``) is an ordinary wiki page — the slug pattern
# forbids a slash, so a deeper path never matches.
_SKILL_PREFIX = "wiki/skills/"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# A person id is the same slug the config uses.
_PERSON_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# One trigger phrase is capped at this many characters; a file keeps at most this
# many phrases. A phrase past the cap is dropped with one warning.
MAX_TRIGGER_CHARS = 200
MAX_TRIGGERS = 20

# A trigger must hold at least this many words. One word is not a trigger: it
# fires on every turn that mentions it.
MIN_TRIGGER_WORDS = 2

# A trigger must not end with one of these. An article or a conjunction demands
# a word that the trigger does not name, so the phrase is the front half of a
# sentence: "turn on the" is a fragment, "turn on the lights" is a phrase.
#
# A preposition is NOT here on purpose. "add a note for", "set the timer to",
# and "turn the lamp on" end with one and are complete triggers: the object of
# the request follows, and the person supplies it ("add a note for *the
# plumber*"). Refusing those would refuse the way people speak.
_FRAGMENT_TAILS = frozenset(
    {
        "a", "an", "the", "my", "your", "our", "their", "his", "her", "its",
        "and", "or", "but",
    }
)  # fmt: skip

# Words a turn may open with before the trigger starts. The list holds address
# and politeness, and nothing else. A word that carries meaning ("did", "is",
# "what", "have") is not here, so a turn that talks *about* a behaviour does
# not fire it.
#
# A verb of intent is NOT here, and neither is the subject in front of one.
# "i", "need", "want" and "to" look like filler, but "I need to <trigger>" is a
# person saying something about themselves, not asking for the behaviour: "I
# need to wind down after this week" fired a device skill while these were on
# the list. The cost is that "I want to <trigger>" no longer fires either. That
# is the safe direction for a page that can change the state of the world, and
# the person can say the trigger on its own.
_LEAD_FILLERS = frozenset(
    {
        "hey", "hi", "hello", "ok", "okay", "yeah", "yes",
        "please", "can", "could", "would", "will", "you", "joshua",
        "just", "now", "a", "an", "the", "my", "our",
    }
)  # fmt: skip

# Words that do not count against the extra-word budget. They carry no request.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "my", "your", "our", "their", "his", "her", "its",
        "to", "of", "in", "on", "at", "for", "with", "and", "or", "from", "by",
        "is", "are", "was", "were", "be", "been", "am",
        "please", "just", "now", "some", "any", "that", "this", "these", "those",
        "it", "me", "him", "them", "us", "you", "i", "we", "they",
        "up", "down", "out", "over", "there", "here",
    }
)  # fmt: skip

# A turn that opens with one of these asks for the opposite of the behaviour.
# The anchor rule already stops most of them, because none of these is a lead
# filler. The check is kept because a denied negation is security-relevant and
# a test must be able to name it.
_NEGATIONS = frozenset({"dont", "don't", "do", "never", "stop", "cancel", "without", "not"})

_WORD = re.compile(r"[a-z0-9']+")


def normalize(text: str) -> tuple[str, ...]:
    """Split text into comparable words: lower case, no punctuation.

    Both sides of a match go through this, so a trigger and a turn are compared
    the same way. The curly apostrophe folds to the straight one, because a
    phone writes one and a wiki page writes the other.
    """
    folded = (text or "").lower().replace("’", "'").replace("ʼ", "'")
    return tuple(_WORD.findall(folded))


def _content_words(words: Iterable[str]) -> list[str]:
    return [w for w in words if w not in _STOPWORDS]


@dataclass(frozen=True)
class Audience:
    """Who a skill fires for.

    ``everyone`` is the default and allows every person. Otherwise a skill
    allows a person named in ``people``, or a person whose role is in
    ``roles``. An audience that names nobody allows nobody.
    """

    everyone: bool = True
    people: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()

    def allows(self, person_id: str | None, role: str | None) -> bool:
        """True when this skill may fire for the given person and role."""
        if self.everyone:
            return True
        if person_id and person_id in self.people:
            return True
        return bool(role) and role in self.roles

    def describe(self) -> str:
        """A short, stable rendering, for a log line and an audit row."""
        if self.everyone:
            return AUDIENCE_EVERYONE
        parts = sorted(self.roles) + sorted(self.people)
        return ",".join(parts) if parts else "nobody"


EVERYONE = Audience()


def is_skill_path(uri: str) -> bool:
    """True for a taught-skill file ``wiki/skills/<slug>.md`` at that exact depth."""
    if not uri.startswith(_SKILL_PREFIX):
        return False
    rest = uri[len(_SKILL_PREFIX) :]
    if not rest.endswith(".md"):
        return False
    return bool(_SLUG.match(rest[:-3]))


def skill_slug(uri: str) -> str:
    """The ``<slug>`` of a skill path, or ``""`` when the path is not a skill."""
    if not is_skill_path(uri):
        return ""
    return uri[len(_SKILL_PREFIX) : -len(".md")]


def _parse_list(raw: str) -> list[str] | None:
    """Read a frontmatter value as a list, or None when it is not a list.

    The value is a raw string such as ``["movie time", "start the film"]``. A
    value that is not a bracketed list returns None. An empty list ``[]``
    returns an empty list.
    """
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [part.strip().strip("'\"") for part in inner.split(",")]


def _audience_items(raw: str, text: str) -> list[str]:
    """Split a ``for`` value into words, however it was written.

    A skill is usually taught by talking to Joshua, so this value is written by
    the agent and not by a person editing YAML. It arrives as a bracketed list
    (``[a, b]``), but just as often as a bare list (``a, b``) or as English
    (``a and b``). All three mean the same thing, so all three are read.
    """
    items = _parse_list(raw)
    if items is not None:
        return items
    return [part for chunk in text.split(",") for part in _AND_RE.split(chunk)]


def parse_audience(
    raw: str | None, *, path: str = "", known_people: Iterable[str] | None = None
) -> Audience:
    """Read the frontmatter ``for`` value.

    Accepts ``everyone`` (and the words that mean it), ``members``, ``guests``,
    one person id, or a list of those in any of the forms above. An absent or
    empty value is ``everyone``.

    A value that names nothing usable logs one warning and allows nobody: a
    skill whose audience cannot be read must not fall open. That is the safe
    direction, but it is also a skill that silently never fires, so every way
    of getting there logs why. ``known_people`` is the roster, when the caller
    has it: a name that is a well-formed id but belongs to nobody is a typo,
    and it is worth a warning of its own.
    """
    if raw is None:
        return EVERYONE
    text = raw.strip().strip("\"'")
    if not text:
        return EVERYONE

    roster = {p.lower() for p in known_people} if known_people is not None else None
    people: set[str] = set()
    roles: set[str] = set()
    unknown: list[str] = []
    strangers: list[str] = []
    for item in _audience_items(raw, text):
        word = item.strip().strip("\"'").lower()
        if not word:
            continue
        if word in _EVERYONE_WORDS:
            return EVERYONE
        if word in _ROLE_WORDS:
            roles.add(_ROLE_WORDS[word])
        elif _PERSON_ID.match(word):
            people.add(word)
            if roster is not None and word not in roster:
                strangers.append(word)
        else:
            unknown.append(word)

    if unknown:
        logger.warning(
            {
                "message": "taught skill audience has unreadable names",
                "path": path,
                "names": unknown,
            }
        )
    if strangers:
        # Well-formed, but nobody on the roster. The skill will never fire for
        # them, and nothing else would say so.
        logger.warning(
            {
                "message": "taught skill audience names nobody on the roster",
                "path": path,
                "names": strangers,
            }
        )
    if not people and not roles:
        logger.warning({"message": "taught skill audience allows nobody", "path": path})
    return Audience(everyone=False, people=frozenset(people), roles=frozenset(roles))


def trigger_problem(phrase: str) -> str | None:
    """Say why a trigger phrase is unusable, or None when it is good.

    A trigger must be something a person says. A single word fires on every
    turn that mentions it, and a phrase that ends with an article is the front
    half of a sentence.

    A trigger made only of small words is allowed. "have we run out of" carries
    no word the stop list would keep, and it is still what a person says.
    """
    words = normalize(phrase)
    if len(words) < MIN_TRIGGER_WORDS:
        return "fewer than two words"
    if words[-1] in _FRAGMENT_TAILS:
        return f"ends with {words[-1]!r}, so it is a fragment"
    return None


def clean_triggers(triggers: Iterable[str], *, path: str = "") -> tuple[str, ...]:
    """Strip, lower-case, drop empties, duplicates, and unusable phrases.

    Logs one warning per rejected phrase that names ``path`` and the reason, so
    a person who writes a bad trigger can find out why it never fires.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in triggers:
        phrase = raw.strip().lower()[:MAX_TRIGGER_CHARS]
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        problem = trigger_problem(phrase)
        if problem:
            logger.warning(
                {
                    "message": "taught skill trigger rejected",
                    "path": path,
                    "trigger": phrase,
                    "reason": problem,
                }
            )
            continue
        out.append(phrase)
    if len(out) > MAX_TRIGGERS:
        logger.warning(
            {
                "message": "taught skill trigger cap; dropping phrases",
                "path": path,
                "kept": MAX_TRIGGERS,
                "dropped": len(out) - MAX_TRIGGERS,
            }
        )
        out = out[:MAX_TRIGGERS]
    return tuple(out)


@dataclass(frozen=True)
class Skill:
    """One parsed taught skill.

    ``name`` is the display name (empty when the frontmatter omits it; the
    caller then falls back to the slug). ``triggers`` is the cleaned tuple of
    phrases — empty means the skill never fires. ``instructions`` is the body.
    """

    name: str
    triggers: tuple[str, ...]
    instructions: str
    kind: str = KIND_COMMAND
    audience: Audience = EVERYONE
    match: str = MATCH_PHRASE
    path: str = ""

    @property
    def fires_from_phrase(self) -> bool:
        return self.kind == KIND_COMMAND and self.match == MATCH_PHRASE and bool(self.triggers)

    @property
    def fires_from_meaning(self) -> bool:
        return self.kind == KIND_COMMAND and self.match == MATCH_SEMANTIC and bool(self.triggers)

    def display_name(self) -> str:
        return self.name or skill_slug(self.path) or "skill"


def _one_of(raw: str | None, allowed: Sequence[str], default: str, *, field: str, path: str) -> str:
    if raw is None:
        return default
    word = raw.strip().strip("\"'").lower()
    if not word:
        return default
    if word in allowed:
        return word
    logger.warning(
        {
            "message": f"taught skill {field} not understood; using the default",
            "path": path,
            "value": word,
            "default": default,
        }
    )
    return default


def parse_skill(
    frontmatter: Mapping[str, str],
    body: str,
    *,
    path: str = "",
    known_people: Iterable[str] | None = None,
) -> Skill | None:
    """Parse a taught skill, or None when the file is not a usable skill.

    Returns None when the frontmatter is missing or the body is empty. A
    command page with no readable ``triggers`` key is not a skill. A convention
    page needs no triggers and drops any it holds.
    """
    if not frontmatter:
        return None
    instructions = (body or "").strip()
    if not instructions:
        return None

    kind = _one_of(frontmatter.get("kind"), KINDS, KIND_COMMAND, field="kind", path=path)
    match = _one_of(frontmatter.get("match"), MATCH_MODES, MATCH_PHRASE, field="match", path=path)
    audience = parse_audience(frontmatter.get("for"), path=path, known_people=known_people)
    name = (frontmatter.get("name") or "").strip().strip("\"'")

    if kind == KIND_CONVENTION:
        if frontmatter.get("triggers"):
            logger.warning(
                {"message": "convention page holds triggers; ignoring them", "path": path}
            )
        return Skill(
            name=name,
            triggers=(),
            instructions=instructions,
            kind=kind,
            audience=audience,
            match=match,
            path=path,
        )

    if "triggers" not in frontmatter:
        return None
    parsed = _parse_list(frontmatter["triggers"])
    if parsed is None:
        return None
    return Skill(
        name=name,
        triggers=clean_triggers(parsed, path=path),
        instructions=instructions,
        kind=kind,
        audience=audience,
        match=match,
        path=path,
    )


@dataclass(frozen=True)
class PhraseMatch:
    """One trigger that matched a turn, and how closely."""

    trigger: str
    # How many words of the trigger were matched. A longer trigger is more
    # specific, so it wins over a shorter one.
    length: int
    # Extra words the turn held inside the matched span. Fewer is closer.
    extra: int


def match_trigger(turn: str, trigger: str, *, max_extra: int) -> PhraseMatch | None:
    """Match one trigger against one turn, near-exact. None when it does not.

    Three rules make this near-exact and not merely "contains":

    1. **Anchor.** The turn must open with the trigger. Only address and
       politeness words may come first ("please", "can you"). A turn that says
       something else first talks *about* the behaviour and does not ask for
       it, so "what does the movie time skill do" does not fire "movie time".
    2. **Order.** Every word of the trigger that carries the request must
       appear, in order. An article or a pronoun the person left out does not
       break the match, so the trigger "turn on the reading lights" still fires
       on "turn on reading lights".
    3. **Budget.** Between the first and the last matched word the turn may
       hold at most ``max_extra`` words that carry meaning. Articles and
       pronouns are free, so "turn on THE reading lights" still matches, and
       one inserted object still matches ("add MILK to the list"),
       but a sentence that merely contains the words does not.

    Words after the last matched word are free. A person may ask for a
    behaviour and then ask for something else in the same message.
    """
    t_words = normalize(trigger)
    m_words = normalize(turn)
    if not t_words or not m_words:
        return None

    # Rule 2: the trigger words, in order. A stop word the person left out is
    # skipped; a word that carries the request is not.
    matched: list[int] = []
    pos = -1
    for word in t_words:
        try:
            found = m_words.index(word, pos + 1)
        except ValueError:
            if word in _STOPWORDS:
                continue
            return None
        matched.append(found)
        pos = found
    if not matched:
        return None

    # Rule 1: refuse anything but address and politeness before the trigger.
    lead = m_words[: matched[0]]
    if any(w in _NEGATIONS for w in lead):
        return None
    if any(w not in _LEAD_FILLERS for w in lead):
        return None

    # Rule 3: the extra-word budget, inside the matched span only.
    span = set(range(matched[0], matched[-1] + 1))
    inside = span - set(matched)
    extra = len(_content_words(m_words[i] for i in sorted(inside)))
    if extra > max_extra:
        return None
    return PhraseMatch(trigger=trigger, length=len(matched), extra=extra)


def match_skill(turn: str, skill: Skill, *, max_extra: int) -> PhraseMatch | None:
    """The closest trigger of one skill that matches the turn, or None.

    A skill fires once. When two of its triggers match, the longer one is the
    better description of what the person said.
    """
    best: PhraseMatch | None = None
    for trigger in skill.triggers:
        found = match_trigger(turn, trigger, max_extra=max_extra)
        if found is None:
            continue
        if best is None or (found.length, -found.extra) > (best.length, -best.extra):
            best = found
    return best


def match_skills(
    turn: str,
    skills: Iterable[Skill],
    *,
    person_id: str | None,
    role: str | None,
    max_extra: int,
    top_k: int = 1,
) -> list[tuple[Skill, PhraseMatch]]:
    """Every phrase skill the person may run that matches the turn, best first.

    "Best" is the most specific trigger: the one that matched the most words,
    and then the one that needed the fewest extra words. A skill the audience
    denies never reaches the list.
    """
    hits: list[tuple[Skill, PhraseMatch]] = []
    for skill in skills:
        if not skill.fires_from_phrase:
            continue
        if not skill.audience.allows(person_id, role):
            continue
        found = match_skill(turn, skill, max_extra=max_extra)
        if found is not None:
            hits.append((skill, found))
    hits.sort(key=lambda pair: (-pair[1].length, pair[1].extra, pair[0].path))
    return hits[:top_k]
