"""Typed schema, loader, and validator for the single joshua.yaml config file.

All three containers read the same file. The loader expands ``${VAR}`` and
``${VAR:-default}`` from the environment on the raw text, refuses literal
credentials, then validates against the pydantic models below. Unknown keys are
errors so a typo fails fast.

Run ``python -m joshua_shared.config validate <file>`` to check a file before you
deploy it, or ``python -m joshua_shared.config schema`` to print the JSON schema.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import strictyaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from joshua_shared.ids import PERSON_ID_PATTERN, PERSON_ID_RE
from joshua_shared.log import CREDENTIAL_PREFIXES

DEFAULT_CONFIG_PATH = "/etc/joshua/joshua.yaml"
CONFIG_ENV_VAR = "JOSHUA_CONFIG"

# The runtime roster people. ``add_user`` and the people CLI write it; the loader
# merges it over ``people`` at load time (see ``_merge_people_file``). It
# lives under the data volume, so the config file stays human-owned.
DATA_DIR_ENV = "JOSHUA_DATA_DIR"
DEFAULT_DATA_DIR = "/data"
PEOPLE_FILE_NAME = "people.yaml"

_PERSON_ID_RE = PERSON_ID_RE

# One MCP server entry. The name is the gateway route.
MCP_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"
_MCP_NAME_RE = re.compile(MCP_NAME_PATTERN)
# Builtin servers run in-process in the gateway. Only these names are valid.
BUILTIN_SERVER_NAMES = ("files", "viewer", "research")
MCP_TRANSPORTS = ("stdio", "http", "sse")
# Memory source adapters the indexer can wire. ``files`` is the kernel and is
# always active; the rest are optional and land with their own tickets.
MEMORY_SOURCE_ADAPTERS = ("files", "memos")
# Reserved policy vocabulary (recorded now, enforced later).
TOOL_CLASSES = ("read", "write-local", "act")
URL_ARGS = ("none", "grant-required")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ``${VAR}`` or ``${VAR:-default}``. The default runs to the next ``}``.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_CREDENTIAL_RE = re.compile("(?:" + "|".join(re.escape(p) for p in CREDENTIAL_PREFIXES) + r")\S*")


class ConfigError(Exception):
    """A config file is missing, cannot be expanded, or fails validation."""


# --- models ----------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Person(_Model):
    id: str
    name: str
    role: str = "member"
    handles: dict[str, str] = {}
    prompt: str | None = None
    # Consent for what Joshua's own journal records about this person's life
    # updates. There is no journal belonging to a person; this gates whether
    # Joshua writes an entry naming them. ``auto`` writes on its own, ``ask``
    # offers first, ``off`` writes one only when the person asks.
    journal: str = "auto"

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _PERSON_ID_RE.match(value):
            raise ValueError(f"person id must match {PERSON_ID_PATTERN}")
        return value

    @field_validator("journal")
    @classmethod
    def _check_journal(cls, value: str) -> str:
        if value not in ("auto", "ask", "off"):
            raise ValueError("journal must be 'auto', 'ask', or 'off'")
        return value

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: str) -> str:
        if value not in ("member", "guest"):
            raise ValueError("role must be 'member' or 'guest'")
        return value

    @field_validator("handles")
    @classmethod
    def _check_handles(cls, handles: dict[str, str]) -> dict[str, str]:
        imessage = handles.get("imessage")
        if imessage is not None and not (_E164_RE.match(imessage) or _EMAIL_RE.match(imessage)):
            raise ValueError("imessage handle must be E.164 or a lowercase email")
        if imessage is not None and imessage != imessage.lower():
            raise ValueError("imessage handle must be lowercase")
        return handles


class Group(_Model):
    """One group chat Joshua answers.

    ``members`` holds the platform handles admitted in that chat. It is both
    the way in and the source of the role: see ``JoshuaConfig.group_role``.
    """

    id: str
    channel: str
    chat_id: str
    members: list[str] = []


class TelegramChannel(_Model):
    bot_token: str
    unknown_sender: str = "drop"

    @field_validator("unknown_sender")
    @classmethod
    def _check_unknown_sender(cls, value: str) -> str:
        return _check_unknown_sender(value)


class IMessageChannel(_Model):
    bluebubbles_url: str
    bluebubbles_password: str
    webhook_path_secret: str
    unknown_sender: str = "drop"
    coalesce_window_s: float = Field(default=2.0, ge=0.0)
    stale_max_age_s: float = Field(default=900.0, ge=0.0)
    reconcile_interval_s: int = Field(default=60, ge=0)
    reconcile_lookback_s: int = Field(default=300, ge=0)
    send_chunk_chars: int = Field(default=4000, gt=0)

    @field_validator("unknown_sender")
    @classmethod
    def _check_unknown_sender(cls, value: str) -> str:
        return _check_unknown_sender(value)


class Webhooks(_Model):
    allowed_callers: list[str] = ["laptop", "ci"]


class Limits(_Model):
    max_text_chars: int = 8000
    max_attachment_bytes: int = 26214400
    per_handle_per_minute: int = 20
    attachment_retention_days: int = 365
    keep_originals: bool = False


class Channels(_Model):
    telegram: TelegramChannel | None = None
    imessage: IMessageChannel | None = None
    webhooks: Webhooks = Webhooks()
    destinations: dict[str, str] = {}
    limits: Limits = Limits()


class Core(_Model):
    model: str = "claude-opus-5"
    max_turns: int = 20
    session_idle_seconds: int = 1800
    pool_max: int = 20
    # None uses the prompts packaged in the image. Set a path only to read a
    # prompts directory mounted into the container.
    prompts_dir: str | None = None
    reflection_model: str = "claude-sonnet-5"
    scheduler_tick_seconds: int = 15


class Inject(_Model):
    enabled: bool = True
    full_sim: float = 0.72
    hint_sim: float = 0.60
    top_k: int = 2
    max_chars: int = 1200


class Skills(_Model):
    # Taught skills: a member writes ``wiki/skills/<slug>.md`` with trigger
    # phrases; a matching turn fires the instructions.
    #
    # ``top_k`` is 1 because only the trigger phrase is embedded. Two skills
    # that answer the same shape of message sit close together: "here's a
    # plant" and "here's a receipt" measure 0.72, above ``min_sim``. A second
    # skill then rides in below the one that was asked for, and the agent gets
    # two sets of instructions. Raise it when you want two.
    enabled: bool = True
    min_sim: float = Field(default=0.62, ge=0.0, le=1.0)
    top_k: int = Field(default=1, ge=1)


class Memory(_Model):
    # No-ops, kept only so a config file written before the journal moved into
    # the wiki still validates. The recency tier holds no journal text now; a
    # turn reaches the journal through retrieval only.
    recent_posts: int = 3
    recent_max_chars: int = Field(default=6000, gt=0)
    # Character cap for the system-prompt shared-profile section.
    shared_max_chars: int = Field(default=2000, gt=0)
    nightly_at: str = "03:30"
    daily_rollover: bool = True
    # Joshua needs at least this many characters of the day's combined
    # transcript for the nightly reflection to write the day's journal page.
    min_chars_for_post: int = Field(default=200, ge=0)
    inject: Inject = Inject()
    skills: Skills = Skills()
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Source adapters the indexer runs, name → adapter options. ``files`` is the
    # kernel and always runs even if absent here; an option ``schedule`` sets a
    # non-file source's cadence in seconds (default: daily). An adapter with no
    # options is an empty value in YAML (strictyaml has no ``{}``).
    sources: dict[str, Any] = Field(default_factory=lambda: {"files": {}})
    # How often the ``files`` source re-indexes the data volume.
    index_interval_s: int = Field(default=60, ge=1)
    # Retrieval tuning (ported from joshua-kb defaults).
    chunk_chars: int = Field(default=1600, gt=0)
    min_sim: float = Field(default=0.55, ge=0.0, le=1.0)
    per_doc_cap: int = Field(default=3, ge=1)
    recency_bonus: float = Field(default=0.08, ge=0.0)
    recency_half_life_days: float = Field(default=14.0, gt=0.0)

    @field_validator("sources")
    @classmethod
    def _check_sources(cls, value: dict[str, Any]) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for name, options in value.items():
            if name not in MEMORY_SOURCE_ADAPTERS:
                raise ValueError(
                    f"unknown memory source adapter '{name}'; valid: {MEMORY_SOURCE_ADAPTERS}"
                )
            if options in (None, ""):
                options = {}
            if not isinstance(options, dict):
                raise ValueError(f"memory source '{name}' options must be a mapping")
            normalized[name] = options
        return normalized


class Wiki(_Model):
    """Whether Joshua keeps `/data/wiki` as a git repository.

    Joshua makes the first commit at launch, commits everything it writes,
    and commits anything else that changed at each start and at the nightly
    run, so a wiki frontend never shows a page as not under version control.
    Set `git: false` to leave git to a frontend or a sync tool instead.
    """

    git: bool = True


class McpToolPolicy(_Model):
    """Per-server tool filter. ``deny`` beats ``allow``; an absent ``allow`` means
    every tool. Patterns are ``fnmatch`` globs matched against the tool name."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allow: list[str] = []
    deny: list[str] = []
    # Reserved for the trust policy: tool or pattern -> read | write-local | act (default act).
    tool_class: dict[str, str] = Field(default_factory=dict, alias="class")

    @field_validator("tool_class")
    @classmethod
    def _check_class(cls, value: dict[str, str]) -> dict[str, str]:
        for pattern, klass in value.items():
            if klass not in TOOL_CLASSES:
                raise ValueError(f"tool class for '{pattern}' must be one of {TOOL_CLASSES}")
        return value


class McpIdentity(_Model):
    """Per-person connect overrides for one upstream. The identities ticket routes
    each person to their own session; this ticket validates and carries them."""

    headers: dict[str, str] = {}
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}


class McpServer(_Model):
    """One MCP server hosted by the gateway.

    An entry is either a builtin (``kind: builtin``, run in-process) or an external
    upstream (``type: stdio|http|sse``). ``allow`` is ``all`` or a list of person
    ids. The name is the gateway route, so two entries can share one upstream under
    different names and filters.
    """

    kind: str | None = None
    type: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    headers: dict[str, str] = {}
    allow: str | list[str] = "all"
    tools: McpToolPolicy | None = None
    identities: dict[str, McpIdentity] = {}
    options: dict[str, Any] = {}
    # Reserved for the trust policy: none | grant-required (default grant-required).
    url_args: str = "grant-required"

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str | None) -> str | None:
        if value is not None and value != "builtin":
            raise ValueError("kind must be 'builtin'")
        return value

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str | None) -> str | None:
        if value is not None and value not in MCP_TRANSPORTS:
            raise ValueError(f"type must be one of {MCP_TRANSPORTS}")
        return value

    @field_validator("url_args")
    @classmethod
    def _check_url_args(cls, value: str) -> str:
        if value not in URL_ARGS:
            raise ValueError(f"url_args must be one of {URL_ARGS}")
        return value

    @field_validator("allow")
    @classmethod
    def _check_allow(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str) and value != "all":
            raise ValueError("allow must be 'all' or a list of person ids")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> McpServer:
        if self.kind is not None:
            if self.type is not None:
                raise ValueError("a builtin server must not set 'type'")
            if self.identities:
                raise ValueError("identities are not valid on a builtin server")
            return self
        if self.type is None:
            raise ValueError("server must set 'kind' (builtin) or 'type' (stdio|http|sse)")
        if self.type == "stdio" and not self.command:
            raise ValueError("a stdio server requires 'command'")
        if self.type in ("http", "sse") and not self.url:
            raise ValueError(f"a {self.type} server requires 'url'")
        self._check_identity_shapes()
        return self

    def _check_identity_shapes(self) -> None:
        """A stdio identity overrides ``command``/``args``/``env``; an http or sse
        identity overrides ``headers``/``url``. Reject a cross-transport override."""
        for pid, identity in self.identities.items():
            if self.type == "stdio":
                if identity.headers or identity.url is not None:
                    raise ValueError(
                        f"identities.{pid}: headers/url apply only to an http or sse server"
                    )
            elif identity.command is not None or identity.args or identity.env:
                raise ValueError(f"identities.{pid}: command/args/env apply only to a stdio server")


class Viewer(_Model):
    # The read-only web viewer. ``users`` maps a person id to a password
    # reference: a ``$2`` bcrypt hash, or a literal password, normally injected
    # from ``VIEWER_PW_<ID>`` through ``${VIEWER_PW_ALEX:-}``. A key must name a
    # person in ``people`` (checked in ``JoshuaConfig._check_viewer``). A person
    # with no entry, or an empty value, cannot sign in.
    enabled: bool = False
    users: dict[str, str] = {}


class JoshuaConfig(_Model):
    name: str
    timezone: str
    people: list[Person]
    groups: list[Group] = []
    channels: Channels = Channels()
    core: Core = Core()
    memory: Memory = Memory()
    wiki: Wiki = Wiki()
    mcp: dict[str, McpServer] = {}
    modules: list[str] = []
    viewer: Viewer = Viewer()

    @model_validator(mode="after")
    def _check_unique(self) -> JoshuaConfig:
        seen_ids: set[str] = set()
        for person in self.people:
            if person.id in seen_ids:
                raise ValueError(f"duplicate person id '{person.id}'")
            seen_ids.add(person.id)
        seen_handles: set[tuple[str, str]] = set()
        for person in self.people:
            for handle_type, handle_id in person.handles.items():
                key = (handle_type, handle_id)
                if key in seen_handles:
                    raise ValueError(f"duplicate handle {handle_type}:{handle_id}")
                seen_handles.add(key)
        return self

    @model_validator(mode="after")
    def _check_mcp(self) -> JoshuaConfig:
        person_ids = {person.id for person in self.people}
        for name, server in self.mcp.items():
            if not _MCP_NAME_RE.match(name):
                raise ValueError(f"mcp.{name}: name must match {MCP_NAME_PATTERN}")
            if server.kind is not None and name not in BUILTIN_SERVER_NAMES:
                raise ValueError(
                    f"mcp.{name}: kind builtin is only valid for {BUILTIN_SERVER_NAMES}"
                )
            if isinstance(server.allow, list):
                for pid in server.allow:
                    if pid not in person_ids:
                        raise ValueError(f"mcp.{name}.allow: unknown person id '{pid}'")
            for pid in server.identities:
                if pid not in person_ids:
                    raise ValueError(f"mcp.{name}.identities: unknown person id '{pid}'")
            if server.identities:
                if server.allow == "all":
                    raise ValueError(
                        f"mcp.{name}: identities need an explicit allow list, not 'all'"
                    )
                for pid in server.allow:
                    if pid not in server.identities:
                        raise ValueError(f"mcp.{name}: {pid} is allowed but has no identity")
        return self

    @model_validator(mode="after")
    def _check_viewer(self) -> JoshuaConfig:
        person_ids = {person.id for person in self.people}
        for pid in self.viewer.users:
            if pid not in person_ids:
                raise ValueError(f"viewer.users: unknown person id '{pid}'")
        return self

    def person(self, person_id: str) -> Person | None:
        """Return the person with this id, or None."""
        for person in self.people:
            if person.id == person_id:
                return person
        return None

    def group_role(self, group: Group) -> tuple[str, str | None]:
        """The role of a group chat, and the handle that lowered it.

        A group is a ``member`` chat only when ``members`` is not empty and every
        handle in it names a person whose role is ``member``. Anything else is a
        ``guest`` chat: an empty list admits anybody, and a handle that names
        nobody carries no role to trust.

        The whole chat carries one role, because a session cannot change its role
        for one turn. So one guest in a family chat stops Joshua writing for the
        members too. That is the safe direction, and it is not obvious from the
        outside, which is why the second value names the handle that caused it
        and ``core`` logs it at start.
        """
        if not group.members:
            return "guest", None
        for handle in group.members:
            person = self.people_by_handle(group.channel, handle)
            if person is None or person.role != "member":
                return "guest", handle
        return "member", None

    def people_by_handle(self, handle_type: str, handle_id: str) -> Person | None:
        """Return the person who owns ``handle_type``:``handle_id``, or None.

        A ``cli`` handle is the person id. The terminal channel needs no entry in
        ``handles``, because a person who can run the command is already on the
        host.
        """
        if handle_type == "cli":
            return self.person(handle_id)
        for person in self.people:
            if person.handles.get(handle_type) == handle_id:
                return person
        return None

    def groups_by_chat(self, channel: str, chat_id: str) -> Group | None:
        """Return the group for ``channel`` and ``chat_id``, or None."""
        for group in self.groups:
            if group.channel == channel and group.chat_id == chat_id:
                return group
        return None

    def destination(self, name: str) -> str:
        """Return the channel address for a destination alias.

        Raises ``KeyError`` when the alias is not defined.
        """
        return self.channels.destinations[name]


def _check_unknown_sender(value: str) -> str:
    if value not in ("reply", "drop"):
        raise ValueError("unknown_sender must be 'reply' or 'drop'")
    return value


# --- expansion and credential scan -----------------------------------------


def expand_env(text: str, env: dict[str, str], *, source: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in ``text``.

    Raises ``ConfigError`` for an unset variable that has no default, naming the
    variable and ``source``.
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConfigError(f"{source}: environment variable ${{{name}}} is not set")

    return _ENV_REF_RE.sub(replace, text)


def _iter_string_leaves(data: Any, path: str = "") -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _iter_string_leaves(value, child)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _iter_string_leaves(value, f"{path}[{index}]")
    elif isinstance(data, str):
        yield path, data


def _refuse_literal_credentials(data: Any, raw_text: str, *, source: str) -> None:
    """Raise ``ConfigError`` for a credential value that was a literal.

    A credential injected through ``${VAR}`` never appears in the raw text, so a
    match that also occurs in ``raw_text`` came from a literal in the file.
    """
    for path, value in _iter_string_leaves(data):
        for match in _CREDENTIAL_RE.findall(value):
            if match in raw_text:
                raise ConfigError(f"{source}: {path}: credentials belong in .env, not joshua.yaml")


def _format_validation_error(exc: ValidationError, *, source: str) -> str:
    first = exc.errors()[0]
    path = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"{source}: {path}: {first['msg']}"


def parse(
    text: str,
    env: dict[str, str] | None = None,
    *,
    source: str = "<string>",
    file_people: list[dict[str, Any]] | None = None,
) -> JoshuaConfig:
    """Expand, scan, and validate config ``text`` into a ``JoshuaConfig``.

    ``file_people`` is the runtime roster from ``read_people_file``; the
    loader merges it over ``people`` before validation. Raises
    ``ConfigError`` on any failure, with a message that names the source and the
    YAML path.
    """
    env = dict(os.environ) if env is None else env
    expanded = expand_env(text, env, source=source)
    try:
        data = strictyaml.load(expanded).data
    except strictyaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    if "household" in data:
        raise ConfigError(
            f"{source}: 'household' is not a section. Put name, timezone, people, "
            "and groups at the top level."
        )
    _refuse_literal_credentials(data, text, source=source)
    if file_people:
        _merge_people_file(data, file_people)
    try:
        return JoshuaConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source=source)) from exc


# --- people file --------------------------------------------------------


def people_file_path(env: Mapping[str, str] | None = None) -> Path:
    """The path of the runtime roster people, ``<data_dir>/people.yaml``."""
    source = os.environ if env is None else env
    return Path(source.get(DATA_DIR_ENV, DEFAULT_DATA_DIR)) / PEOPLE_FILE_NAME


def read_people_file(path: Path) -> list[dict[str, Any]]:
    """Return the ``people`` list from the people file.

    Returns [] when the file is absent or empty. Raises ``ConfigError`` when the
    file is present but not valid YAML.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    if not text.strip():
        return []
    try:
        data = strictyaml.load(text).data
    except strictyaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        return []
    people = data.get("people")
    return people if isinstance(people, list) else []


def _apply_people_file(
    base: list[dict[str, Any]], people: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge ``people file`` people over ``base`` by id.

    A people file entry replaces the base entry with the same id, or appends a new
    one. An entry with ``role: removed`` drops that id from the roster.
    """
    result: list[dict[str, Any] | None] = [dict(person) for person in base]
    index = {p["id"]: i for i, p in enumerate(result) if isinstance(p, dict) and "id" in p}
    for entry in people:
        pid = entry.get("id")
        if pid is None:
            continue
        removed = str(entry.get("role", "")).strip().lower() == "removed"
        if pid in index:
            result[index[pid]] = None if removed else entry
        elif not removed:
            index[pid] = len(result)
            result.append(entry)
    return [person for person in result if person is not None]


def _merge_people_file(data: dict[str, Any], people: list[dict[str, Any]]) -> None:
    base = data.get("people")
    data["people"] = _apply_people_file(base if isinstance(base, list) else [], people)


# --- load / cache ----------------------------------------------------------

_cache: JoshuaConfig | None = None
_cache_path: Path | None = None
# The people file mtime the cache was built from. `add_user` writes the people file from
# core, and every other container has to notice without a restart.
_cache_people_file_mtime: float | None = None


def _resolve_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))


def _people_file_mtime() -> float | None:
    """The people file's modification time, or None when it is not there."""
    try:
        return people_file_path().stat().st_mtime
    except OSError:
        return None


def _read(path: Path) -> JoshuaConfig:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    people = read_people_file(people_file_path())
    return parse(text, source=str(path), file_people=people)


def load(path: Path | str | None = None) -> JoshuaConfig:
    """Return the config, reading and caching it on first call.

    The path comes from ``path``, else the ``JOSHUA_CONFIG`` env var, else
    ``/etc/joshua/joshua.yaml``. Raises ``ConfigError`` on any failure.
    """
    global _cache, _cache_path, _cache_people_file_mtime
    if _cache is None:
        resolved = _resolve_path(path)
        _cache = _read(resolved)
        _cache_path = resolved
        _cache_people_file_mtime = _people_file_mtime()
        return _cache
    # A person added or removed at run time writes the people. Every container
    # reads the roster from its own cache, so each one has to see that write. A
    # `stat` per call is cheap; the file is re-read only when it changed.
    current = _people_file_mtime()
    if current != _cache_people_file_mtime:
        _cache = _read(_cache_path or _resolve_path(path))
        _cache_people_file_mtime = current
    return _cache


def reload(path: Path | str | None = None) -> JoshuaConfig:
    """Clear the cache and read the config again."""
    global _cache, _cache_path, _cache_people_file_mtime
    resolved = _resolve_path(path if path is not None else _cache_path)
    _cache = _read(resolved)
    _cache_path = resolved
    _cache_people_file_mtime = _people_file_mtime()
    return _cache


# --- CLI -------------------------------------------------------------------


def _validate_cli(path: str) -> int:
    try:
        config = _read(Path(path))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    channels = [
        name for name in ("telegram", "imessage") if getattr(config.channels, name) is not None
    ]
    print(f"ok: {path}")
    print(f"  people: {len(config.people)}")
    print(f"  channels enabled: {', '.join(channels) or 'none'}")
    print(f"  mcp servers: {', '.join(config.mcp) or 'none'}")
    return 0


def _schema_cli() -> int:
    print(json.dumps(JoshuaConfig.model_json_schema(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="joshua_shared.config")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a config file")
    validate.add_argument("file")
    sub.add_parser("schema", help="print the JSON schema")
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate_cli(args.file)
    return _schema_cli()


if __name__ == "__main__":
    raise SystemExit(main())
