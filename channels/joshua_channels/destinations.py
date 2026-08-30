"""Resolve a channel target to a concrete adapter, chat id, and chat kind.

The resolver is the reimplementation of the old ``repo.resolve_channel`` over
config and adapter state, with no database. A target is one of:

- a literal ``<type>:<platform_chat_id>`` — sent to that adapter as is;
- a logical ``<type>:group:<group_id>`` or ``<type>:dm:<person_id>`` — passed to
  the adapter's ``resolve_ref``;
- a bare destination name — looked up in ``channels.destinations`` and resolved
  again.

Both ``/v1/deliver`` and ``/v1/channels/resolve`` call ``resolve``.
"""

from __future__ import annotations

from dataclasses import dataclass

from joshua_shared.config import Group, JoshuaConfig

from joshua_channels.registry import Adapter, AdapterRegistry

_GROUP_PREFIX = "group:"
_DM_PREFIX = "dm:"


@dataclass(frozen=True)
class Resolved:
    """The result of resolving a target.

    ``channel`` is the concrete ``<type>:<platform_chat_id>`` address. ``person_id``
    is the DM's person when the ref named one; it is None for a group or a literal
    id. ``adapter`` is the plugin that sends the message.
    """

    channel: str
    channel_type: str
    chat_id: str
    kind: str  # "dm" | "group"
    title: str | None
    person_id: str | None
    adapter: Adapter


def resolve(registry: AdapterRegistry, settings: JoshuaConfig, target: str) -> Resolved | None:
    """Resolve a target string to a ``Resolved``, or None when it is unknown."""
    target = target.strip()
    if not target:
        return None
    if ":" not in target:
        ref = settings.channels.destinations.get(target)
        if not ref:
            return None
        return _resolve_ref(registry, settings, ref, name=target)
    return _resolve_ref(registry, settings, target)


def _resolve_ref(
    registry: AdapterRegistry, settings: JoshuaConfig, ref: str, *, name: str | None = None
) -> Resolved | None:
    channel_type, _, rest = ref.partition(":")
    adapter = registry.get(channel_type)
    if adapter is None or not rest:
        return None

    if rest.startswith(_GROUP_PREFIX):
        group_id = rest[len(_GROUP_PREFIX) :]
        chat_id = adapter.resolve_ref(f"{_GROUP_PREFIX}{group_id}")
        if not chat_id:
            return None
        group = _group_by_id(settings, channel_type, group_id)
        title = (group.id if group else group_id) or name
        return _make(channel_type, chat_id, "group", title, None, adapter)

    if rest.startswith(_DM_PREFIX):
        person_id = rest[len(_DM_PREFIX) :]
        chat_id = adapter.resolve_ref(f"{_DM_PREFIX}{person_id}")
        if not chat_id:
            return None
        title = _person_name(settings, person_id) or name
        return _make(channel_type, chat_id, "dm", title, person_id, adapter)

    # Literal <type>:<platform_chat_id>. A known group chat id resolves to a group;
    # anything else is a DM with no known person.
    group = settings.groups_by_chat(channel_type, rest)
    if group is not None:
        return _make(channel_type, rest, "group", group.id, None, adapter)
    return _make(channel_type, rest, "dm", name, None, adapter)


def _make(
    channel_type: str,
    chat_id: str,
    kind: str,
    title: str | None,
    person_id: str | None,
    adapter: Adapter,
) -> Resolved:
    return Resolved(
        channel=f"{channel_type}:{chat_id}",
        channel_type=channel_type,
        chat_id=chat_id,
        kind=kind,
        title=title,
        person_id=person_id,
        adapter=adapter,
    )


def _group_by_id(settings: JoshuaConfig, channel: str, group_id: str) -> Group | None:
    for group in settings.groups:
        if group.channel == channel and group.id == group_id:
            return group
    return None


def _person_name(settings: JoshuaConfig, person_id: str) -> str | None:
    person = settings.person(person_id)
    return person.name if person else None
