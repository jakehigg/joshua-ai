"""In-process ``workspace`` tool server: collect things into a workspace, keep
its notes, send the person a message in normal chat, and reach another person.

``add_to_workspace`` is in every chat. Inside a workspace thread it adds to the
current workspace unless the call names another. In a normal chat it must name
one of the person's workspaces, by slug or by title, and the item shows in the
block of that workspace as new. ``update_workspace_notes`` and
``send_to_normal_chat`` are in a workspace thread only: the server builder reads
``deps.channel.channel_type``. ``notify_user`` is in a shared workspace thread
only, where the builder also reads ``deps.channel.session_mode``.

A direct chat that is not a workspace also gets ``send_to_workspace`` (a row and
items in the thread, no turn), ``tell_workspace`` (the same, then one workspace
turn whose reply stays in the thread), ``list_workspaces`` and
``workspace_status`` (the block as text). A workspace name is a slug or a title,
matched with no regard to case among the workspaces of the person.

Every tool acts for ``deps.person_id``, the person who wrote the turn: the
workspaces they reach are their own and the shared ones they are in. A
workspace they do not reach is never found.

``notify_user`` and ``notify_person`` are the two tools that reach another
person, and both send on one chain (``_deliver_notification``): every push
subscription of that person, then their normal chat when no browser took it.

``notify_user`` belongs to the thread of a shared workspace. Three guards are
in the tool and not in the prompt: it never notifies the person who wrote the
turn, it never notifies a person who has read the thread to its newest row,
and it sends at most one notification per person per workspace in
``NOTIFY_QUIET_S``. A line inside that time folds into the notification
already sent, which carries the tag of the workspace and opens where that one
pointed. The mode of the person (``store/notify.py``) governs the rest: ``off``
refuses.
Each call writes one decision line (``_log_decision``): the person, the
workspace, whether a notification went, and the guard that stopped it.

Both tools take an optional ``url``, the path the tap opens, so a notification
lands on the thing to read: a document, a wiki page, or the workspace thread,
which is what ``notify_user`` opens when the call names no path. ``_app_path``
is the one validator of both: a path of this app and nothing else, as the
service worker's ``notificationclick`` refuses an address of another site.
The push payload carries that path, because the worker follows a path; the
normal-chat fallback carries the absolute address (``app_link``), because a
person reads that one in a chat and clicks it.

``notify_person`` is in every chat of a member: a normal chat, a personal
workspace, and a shared one. It names anybody of the roster, not of a
workspace, because Joshua decides somebody needs to know or a person asks him
to tell somebody. A guest does not get the tool. One target person gets one
notification from it in ``NOTIFY_QUIET_S``, held in ``_NOTIFIED``, this
process's own map: a restart of core forgets the limit, which is a known and
accepted cost of keeping a per-person limit out of ``notify_prefs``, which is
keyed by person and workspace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from joshua_shared.config import JoshuaConfig, Person, is_member
from joshua_shared.item_url import check_item_url
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.workspace import workspace_id_of
from joshua_core.push import Notification, SendReport, send_to_person
from joshua_core.store.models import WORKSPACE_ITEM_KINDS, Workspace
from joshua_core.store.notify import OFF, default_mode
from joshua_core.store.repo import SHARED_NAMESPACE, WORKSPACE_CHANNEL_TYPE

logger = get_logger("tools.workspace")

SERVER_NAME = "workspace"

# The notes are in every turn of the workspace, so they stay short.
NOTES_MAX_CHARS = 4000

Deliver = Callable[[str, str], Awaitable[bool]]
# Starts one turn in a workspace thread, in the background: (workspace, text).
RunTurn = Callable[[Workspace, str], Awaitable[None]]
# Renders the workspace block as text, with no side effect.
RenderBlock = Callable[[Workspace], Awaitable[str]]
# Sends one notification to every browser of one person.
Notify = Callable[[str, Notification], Awaitable[SendReport]]

GOAL_LINE_CHARS = 120
# send_to_workspace and tell_workspace cut a message at this length.
MESSAGE_MAX_CHARS = 4000
CHAT_PREFIX = "[From normal chat] "

# One person gets one notification of one workspace in this time. A line
# inside it folds into the notification they already have.
NOTIFY_QUIET_S = 600
# A notification is a knock on the door, not the message, so its body is one
# short line. The thread holds the rest.
NOTIFY_BODY_CHARS = 160

# What ``notify_person`` says it is from, and the tag it carries, so a second
# one replaces the first in the browser instead of stacking up.
NOTIFY_TITLE = "Joshua"
NOTIFY_TAG = "from-joshua"
# Where a ``notify_person`` notification opens when the call names no path: the
# app itself. ``notify_user`` defaults to the thread of its workspace.
APP_ROOT = "/"
# What both notify tools answer for a ``url`` that is not a path of this app.
BAD_URL = "The url must be a path inside Joshua, as /chat or /wiki."

# Why a notification did not go, on the decision line of a ``notify_user``
# call. The first four are the guards of the tool, and the last one is a
# delivery that reached neither a browser nor a normal chat.
GUARD_WRITER = "writer"
GUARD_READING = "reading"
GUARD_FOLD = "fold"
GUARD_OFF = "mode:off"
GUARD_UNREACHED = "unreached"

# The moment each person was last notified by ``notify_person``, so one person
# gets one of those in ``NOTIFY_QUIET_S``. The map is this process's own: a
# restart of core forgets it and the next call goes out, which is cheaper than
# a row per notification. It is keyed by person alone, because this tool names
# a person and no workspace.
_NOTIFIED: dict[str, datetime] = {}


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _in_workspace(deps: ToolDeps) -> bool:
    return deps.channel.channel_type == WORKSPACE_CHANNEL_TYPE


def _in_shared_workspace(deps: ToolDeps) -> bool:
    """The thread of a shared workspace: several people write in it, so one of
    them can be reached alone. The channel of a shared workspace is the one
    workspace channel whose ``session_mode`` is ``shared``."""
    return _in_workspace(deps) and deps.channel.session_mode == "shared"


async def _current(deps: ToolDeps) -> Workspace | None:
    """The workspace of this chat, for the person of the turn. A turn with no
    person, a job event or a scheduled task, still gets it: the thread is the
    workspace. A person who does not reach the workspace gets None."""
    workspace_id = workspace_id_of(deps.channel.id)
    found = await deps.repo.get_workspace(workspace_id) if workspace_id else None
    if found is None:
        return None
    return found if deps.person_id is None or deps.person_id in found.people else None


async def _named(deps: ToolDeps, name: str) -> Workspace | None:
    """A workspace the person reaches, by slug, id, or title (not
    case-sensitive). An ambiguous title gives None."""
    found, _ = await _match(deps, name)
    return found


async def _match(deps: ToolDeps, name: str) -> tuple[Workspace | None, list[Workspace]]:
    """``(workspace, candidates)``. ``workspace`` is the one match among the
    workspaces the person reaches, their own and the shared ones they are in.
    With no match it is None and ``candidates`` is empty. With an ambiguous name
    it is None and ``candidates`` lists the workspaces that match."""
    if not deps.person_id or not (name or "").strip():
        return None, []
    wanted = name.strip().casefold()
    reached = await deps.repo.list_workspaces(deps.person_id)
    exact = [w for w in reached if wanted in (w.slug.casefold(), w.id.casefold())]
    if len(exact) == 1:
        return exact[0], []
    by_title = [w for w in reached if w.title.strip().casefold() == wanted]
    if len(by_title) == 1:
        return by_title[0], []
    if by_title:
        return None, by_title
    return None, []


async def do_add_to_workspace(
    deps: ToolDeps,
    *,
    kind: str,
    ref: str,
    title: str,
    url: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Add an item to a workspace, or update the item with the same kind and ref."""
    kind, ref, title = (kind or "").strip(), (ref or "").strip(), (title or "").strip()
    if kind not in WORKSPACE_ITEM_KINDS:
        return _reply(f"Unknown kind '{kind}'. Use one of: {', '.join(WORKSPACE_ITEM_KINDS)}.")
    if not ref:
        return _reply("Give a ref: the id, slug, path, or address of the item.")
    try:
        address = check_item_url(url)
    except ValueError as exc:
        return _reply(f"The url was not added: {exc}.")
    target: Workspace | None
    if workspace and workspace.strip():
        target = await _named(deps, workspace)
    elif _in_workspace(deps):
        target = await _current(deps)
    else:
        return _reply("Name the workspace: its slug or its title.")
    if target is None:
        # Only a direct chat may see the names of the workspaces: a group chat
        # shows to other people.
        if deps.channel.session_mode != "per_person":
            return _reply("No such workspace.")
        names = ", ".join(w.slug for w in await _active(deps)) or "none"
        return _reply(f"No such workspace. Your workspaces: {names}.")
    here = _in_workspace(deps) and target.channel_id == deps.channel.id
    await deps.repo.add_workspace_item(
        target.id,
        kind,
        ref,
        title or ref,
        url=address,
        added_by="agent",
        # An item sent in from another chat is new to the workspace.
        seen=here,
    )
    return _reply(f"Added {kind} '{title or ref}' to workspace '{target.title}'.")


async def _active(deps: ToolDeps) -> list[Workspace]:
    if not deps.person_id:
        return []
    return await deps.repo.list_workspaces(deps.person_id, "active")


async def do_update_workspace_notes(deps: ToolDeps, *, notes: str) -> dict[str, Any]:
    """Replace the notes of the current workspace."""
    if not _in_workspace(deps):
        return _reply("This chat is not a workspace.")
    notes = (notes or "").strip()
    if len(notes) > NOTES_MAX_CHARS:
        return _reply(
            f"The notes are {len(notes)} characters. Keep them under {NOTES_MAX_CHARS}: "
            "cut what is old, and put long material in a document."
        )
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    await deps.repo.update_workspace(workspace.id, notes=notes)
    return _reply("Notes updated.")


def app_link(settings: JoshuaConfig, path: str) -> str:
    """The address of one path of the viewer: the public address and the path, or
    the path alone when ``workspaces.viewer_url`` is not set."""
    base = settings.workspaces.viewer_url
    return f"{base.rstrip('/')}{path}" if base else path


def chat_path(workspace: Workspace) -> str:
    """The path of a workspace thread in the viewer. It names the namespace of
    the workspace: ``shared`` when it is shared, its owner when it is not."""
    namespace = SHARED_NAMESPACE if workspace.shared else workspace.owner
    return f"/chat/w/{namespace}/{workspace.slug}"


def chat_link(settings: JoshuaConfig, workspace: Workspace) -> str:
    """The address of a workspace in the viewer."""
    return app_link(settings, chat_path(workspace))


async def do_send_to_normal_chat(
    deps: ToolDeps, *, text: str, settings: JoshuaConfig, deliver: Deliver | None
) -> dict[str, Any]:
    """Send ``text`` and a link to this workspace to the main chat of one
    person: the one who wrote the turn. A turn with no person, a job event or a
    scheduled task, sends to the owner, so the message reaches somebody. There
    is no fan-out: to tell a whole shared workspace something, the agent writes
    in the thread."""
    if not _in_workspace(deps):
        return _reply("This chat is not a workspace.")
    text = (text or "").strip()
    if not text:
        return _reply("Give the text to send.")
    if deliver is None:
        return _reply("Sending to normal chat is not available here.")
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    main = await deps.repo.main_conversation(deps.person_id or workspace.owner)
    if main is None:
        return _reply("The person has no normal chat to send to.")
    target = main.last_channel_id or main.channel_id
    message = f"{text}\n\n{workspace.title}: {chat_link(settings, workspace)}"
    if not await deliver(target, message):
        return _reply("The message was not delivered. Tell the person in this chat.")
    return _reply("Sent to the person's normal chat.")


def _person_in(workspace: Workspace, name: str, settings: JoshuaConfig) -> str | None:
    """The id of the person of this workspace that ``name`` means, by id or by
    the name the roster gives them, with no regard to case. None for anybody
    else: a person outside the workspace is never notified."""
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for person_id in workspace.people:
        person = settings.person(person_id)
        names = {person_id.casefold()} | ({person.name.casefold()} if person else set())
        if wanted in names:
            return person_id
    return None


def _minutes(span: timedelta) -> int:
    """The whole minutes of a span, at least one, for a sentence the agent
    reads."""
    return max(1, int(span.total_seconds() // 60))


def _app_path(url: str | None, default: str) -> str | None:
    """The path a notification opens, inside the app: ``default`` when the call
    names none. None for anything else, an address of another site included:
    these tools send people to Joshua, and nowhere else, as the service
    worker's ``notificationclick`` refuses an address of another site.

    It is the one validator of ``notify_user`` and ``notify_person``.
    """
    path = (url or "").strip() or default
    if not path.startswith("/") or path.startswith("//"):
        return None
    return path


def _log_decision(person: str, workspace: Workspace, *, sent: bool, guard: str | None) -> None:
    """One line for one ``notify_user`` call: the person, the workspace,
    whether a notification went, and the guard that stopped it. A notification
    that did not arrive has a reason, and this is where it is read."""
    logger.info(
        {
            "message": "notify decision",
            "person": person,
            "workspace": workspace.id,
            "sent": sent,
            "guard": guard,
        }
    )


async def do_notify_user(
    deps: ToolDeps,
    *,
    person: str,
    text: str,
    url: str | None = None,
    settings: JoshuaConfig,
    deliver: Deliver | None,
    notify: Notify | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reach one person of this shared workspace: their push subscriptions, or
    their normal chat when they have none.

    ``url`` is the path the tap opens, so the notification lands on the thing
    to read: a document, a wiki page, or this thread, which is the default. The
    payload of the push carries that path; the normal-chat fallback carries the
    absolute address of it.

    The guards are here and not in the prompt: the person who wrote the turn is
    never notified, a person who has read the thread to its newest row is
    looking at it already, and one person gets one notification of one
    workspace in ``NOTIFY_QUIET_S``. Their mode (``store/notify.py``) comes
    first: ``off`` sends nothing. Each decision is one log line.
    """
    if not _in_shared_workspace(deps):
        return _reply("This tool is for a shared workspace.")
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    target = _person_in(workspace, person, settings)
    if target is None:
        people = ", ".join(workspace.people)
        return _reply(f"'{person}' is not in this workspace. Its people are: {people}.")
    path = _app_path(url, chat_path(workspace))
    if path is None:
        return _reply(BAD_URL)
    if deps.person_id is not None and target == deps.person_id:
        _log_decision(target, workspace, sent=False, guard=GUARD_WRITER)
        return _reply(f"{target} wrote this turn, so they get no notification of it.")
    pref = await deps.repo.notify_prefs.get(target, workspace.id)
    mode = pref.mode if pref is not None else default_mode(workspace.shared)
    if mode == OFF:
        _log_decision(target, workspace, sent=False, guard=GUARD_OFF)
        return _reply(f"Notifications are off for {target} in this workspace. Nothing was sent.")
    conversation = await deps.repo.workspace_conversation(workspace)
    unread = await deps.repo.unread_counts(target, [conversation.id])
    if not unread.get(conversation.id, 0):
        _log_decision(target, workspace, sent=False, guard=GUARD_READING)
        return _reply(
            f"{target} has read this thread to its newest row, so they are reading it now."
        )
    moment = now or datetime.now(UTC)
    last = pref.last_notified_at if pref is not None else None
    if last is not None and moment - last < timedelta(seconds=NOTIFY_QUIET_S):
        _log_decision(target, workspace, sent=False, guard=GUARD_FOLD)
        return _reply(
            f"{target} was notified of this workspace {_minutes(moment - last)} minute(s) ago. "
            "This line folds into that notification, which opens where that one pointed. "
            "Nothing was sent."
        )
    link = app_link(settings, path)
    reached, how = await _deliver_notification(
        deps,
        target,
        notification=Notification(
            title=workspace.title,
            body=_one_line(text, NOTIFY_BODY_CHARS),
            # The path, not ``link``: the service worker follows a path of the
            # app, and the absolute address goes in the chat message below.
            url=path,
            # The tag of the workspace, so its notifications collapse into the
            # newest one instead of stacking up.
            tag=workspace.slug,
        ),
        message=f"{text}\n\n{workspace.title}: {link}",
        deliver=deliver,
        notify=notify,
    )
    if not reached:
        _log_decision(target, workspace, sent=False, guard=GUARD_UNREACHED)
        return _reply(f"The notification did not reach {target}. Say it in this thread as well.")
    await deps.repo.notify_prefs.mark_sent(target, workspace.id, mode=mode, at=now)
    _log_decision(target, workspace, sent=True, guard=None)
    return _reply(f"Notified {target} {how}.")


async def _deliver_notification(
    deps: ToolDeps,
    target: str,
    *,
    notification: Notification,
    message: str,
    deliver: Deliver | None,
    notify: Notify | None,
) -> tuple[bool, str]:
    """The one chain both notify tools send on: every push subscription of the
    person first, and their normal chat when no browser of theirs took it.

    ``(reached, how)``: ``how`` is the half-sentence the tool answers with.
    """
    if notify is not None and await deps.repo.push_subscriptions.list_for(target):
        report = await notify(target, notification)
        if report.sent:
            return True, "on their phone"
    if deliver is None:
        return False, ""
    main = await deps.repo.main_conversation(target)
    if main is None:
        return False, ""
    sent = await deliver(main.last_channel_id or main.channel_id, message)
    return sent, "in their normal chat"


def _person_named(settings: JoshuaConfig, name: str) -> tuple[Person | None, list[Person]]:
    """``(person, candidates)``: the one person of the roster that ``name``
    means, by id or by name, with no regard to case.

    With no match the person is None and ``candidates`` is empty. With a name
    two people share it is None and ``candidates`` lists them, so the tool can
    ask which one.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None, []
    by_id = [person for person in settings.people if person.id.casefold() == wanted]
    if by_id:
        return by_id[0], []
    by_name = [person for person in settings.people if person.name.strip().casefold() == wanted]
    if len(by_name) == 1:
        return by_name[0], []
    return None, by_name


async def do_notify_person(
    deps: ToolDeps,
    *,
    person: str,
    text: str,
    url: str | None,
    settings: JoshuaConfig,
    deliver: Deliver | None,
    notify: Notify | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reach one person of the roster, from any chat: their push subscriptions,
    or their normal chat when no browser of theirs took it.

    ``url`` is the path the tap opens, the app itself when the call names none.
    The payload of the push carries that path; the normal-chat fallback carries
    the absolute address of it.

    The guard is here and not in the prompt: one person gets one notification
    from this tool in ``NOTIFY_QUIET_S``, whoever asked for it and whatever
    chat it came from.
    """
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    target, candidates = _person_named(settings, person)
    if target is None:
        if candidates:
            names = ", ".join(f"{p.name} ({p.id})" for p in candidates)
            return _reply(f"More than one person is called '{person}'. Use an id: {names}.")
        return _reply(f"'{person}' is nobody here. Use the id or the name of a person.")
    path = _app_path(url, APP_ROOT)
    if path is None:
        return _reply(BAD_URL)
    moment = now or datetime.now(UTC)
    last = _NOTIFIED.get(target.id)
    if last is not None and moment - last < timedelta(seconds=NOTIFY_QUIET_S):
        return _reply(
            f"{target.id} was notified {_minutes(moment - last)} minute(s) ago. "
            "Nothing was sent: say the rest where they will read it."
        )
    link = app_link(settings, path)
    reached, how = await _deliver_notification(
        deps,
        target.id,
        notification=Notification(
            title=NOTIFY_TITLE,
            body=_one_line(text, NOTIFY_BODY_CHARS),
            # The path, not ``link``: the service worker follows a path of the
            # app, and the absolute address goes in the chat message below.
            url=path,
            tag=NOTIFY_TAG,
        ),
        message=text if path == APP_ROOT else f"{text}\n\n{link}",
        deliver=deliver,
        notify=notify,
    )
    if not reached:
        return _reply(f"The notification did not reach {target.id}. Tell the person who asked.")
    _NOTIFIED[target.id] = moment
    logger.info({"message": "person notified", "person": target.id, "tool": "notify_person"})
    return _reply(f"Notified {target.id} {how}.")


def clear_notified() -> None:
    """Forget who was notified by ``notify_person``. A restart does the same."""
    _NOTIFIED.clear()


def _normal_chat(deps: ToolDeps) -> bool:
    """A direct chat that is not a workspace: the home of the four tools."""
    return not _in_workspace(deps) and deps.channel.session_mode == "per_person"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


async def _resolve(deps: ToolDeps, name: str) -> tuple[Workspace | None, str]:
    """The workspace that ``name`` means, or None and the answer to give."""
    if not (name or "").strip():
        return None, f"Name the workspace: its slug or its title.{await _known(deps)}"
    found, candidates = await _match(deps, name)
    if found is not None:
        return found, ""
    if candidates:
        slugs = ", ".join(f"{w.slug} ({w.title})" for w in candidates)
        return None, f"More than one workspace has that title. Use a slug: {slugs}."
    return None, f"No such workspace.{await _known(deps)}"


async def _known(deps: ToolDeps) -> str:
    names = ", ".join(f"{w.slug}" for w in await _active(deps)) or "none"
    return f" Your workspaces: {names}."


async def _items_in(deps: ToolDeps, workspace: Workspace, items: Any) -> tuple[int, list[str]]:
    """Add the items of ``send_to_workspace`` as unseen. Returns the number
    added and the problems found."""
    added, problems = 0, []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            problems.append("an item that is not an object")
            continue
        kind = (raw.get("kind") or "link").strip()
        ref, title = (raw.get("ref") or "").strip(), (raw.get("title") or "").strip()
        if kind not in WORKSPACE_ITEM_KINDS or not ref:
            problems.append(f"item '{title or ref or kind}' needs a known kind and a ref")
            continue
        try:
            address = check_item_url(raw.get("url") or None)
        except ValueError as exc:
            problems.append(f"item '{title or ref}': {exc}")
            continue
        await deps.repo.add_workspace_item(
            workspace.id, kind, ref, title or ref, url=address, added_by="agent", seen=False
        )
        added += 1
    return added, problems


def _message(text: str) -> str:
    return (text or "").strip()[:MESSAGE_MAX_CHARS]


async def do_send_to_workspace(
    deps: ToolDeps, *, workspace: str, text: str, items: Any = None
) -> dict[str, Any]:
    """Write ``text`` into the thread of a workspace as a row from normal chat,
    and add the items as unseen. No turn runs: the block of the workspace shows
    both on its next turn, once."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    if target.status == "archived":
        return _reply(f"Workspace '{target.title}' is archived.")
    conversation = await deps.repo.workspace_conversation(target)
    added, problems = await _items_in(deps, target, items)
    await deps.repo.add_chat_row(conversation.id, text)
    note = f" with {added} item(s)" if added else ""
    tail = f" Not added: {'; '.join(problems)}." if problems else ""
    return _reply(f"Recorded in workspace '{target.title}'{note}. It shows on its next turn.{tail}")


async def do_tell_workspace(
    deps: ToolDeps, *, workspace: str, text: str, run: RunTurn | None
) -> dict[str, Any]:
    """Run one turn in the workspace thread with ``text``. The reply stays in the
    thread. The turn runs in the background."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    if run is None:
        return _reply("Telling a workspace is not available here.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    if target.status != "active":
        return _reply(f"Workspace '{target.title}' is {target.status}. Resume it first.")
    await run(target, CHAT_PREFIX + text)
    return _reply(
        f"Told workspace '{target.title}'. It is working on it, and its reply "
        "stays in the workspace thread."
    )


async def do_list_workspaces(deps: ToolDeps) -> dict[str, Any]:
    """The workspaces of the person: title, slug, status, and the goal."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    owned = await deps.repo.list_workspaces(deps.person_id) if deps.person_id else []
    if not owned:
        return _reply("The person has no workspaces.")
    lines = []
    for w in owned:
        goal = f" - {_one_line(w.goal, GOAL_LINE_CHARS)}" if w.goal else ""
        lines.append(f"- {w.title} ({w.slug}, {w.status}){goal}")
    return _reply("\n".join(lines))


async def do_workspace_status(
    deps: ToolDeps, *, workspace: str, render: RenderBlock | None
) -> dict[str, Any]:
    """The workspace block as text, never the thread."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    if render is None:
        return _reply("The workspace status is not available here.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    return _reply(await render(target))


_ADD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(WORKSPACE_ITEM_KINDS),
            "description": "What the item is.",
        },
        "ref": {
            "type": "string",
            "description": "The id, slug, path, or address of the item. It identifies the item.",
        },
        "title": {"type": "string", "description": "A short title the person reads."},
        "url": {"type": "string", "description": "An address that opens the item."},
        "workspace": {
            "type": "string",
            "description": (
                "The slug or the title of the workspace. Optional in a workspace chat, "
                "where it defaults to this workspace. Required in a normal chat."
            ),
        },
    },
    "required": ["kind", "ref", "title"],
}

_NOTES_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "string", "description": "The full new notes."}},
    "required": ["notes"],
}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(WORKSPACE_ITEM_KINDS)},
        "ref": {"type": "string", "description": "The id, slug, path, or address of the item."},
        "title": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["ref"],
}

_WORKSPACE_ARG = {
    "type": "string",
    "description": "The slug or the title of one of the person's workspaces.",
}

_SEND_WS_SCHEMA = {
    "type": "object",
    "properties": {
        "workspace": _WORKSPACE_ARG,
        "text": {"type": "string", "description": "What to record in the workspace."},
        "items": {
            "type": "array",
            "items": _ITEM_SCHEMA,
            "description": "Links, documents, or other items to add with it.",
        },
    },
    "required": ["workspace", "text"],
}

_TELL_SCHEMA = {
    "type": "object",
    "properties": {"workspace": _WORKSPACE_ARG, "text": {"type": "string"}},
    "required": ["workspace", "text"],
}

_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"workspace": _WORKSPACE_ARG},
    "required": ["workspace"],
}

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}}

_SEND_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "The message."}},
    "required": ["text"],
}

_NOTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": "The id or the name of one person of this workspace.",
        },
        "text": {
            "type": "string",
            "description": "One short line: what waits on them, in your own words.",
        },
        "url": {
            "type": "string",
            "description": (
                "A path inside Joshua that the notification opens, so the tap lands on "
                "what they must read: a document (/documents/<owner>/<slug>) or a wiki "
                "page (/wiki/...). This workspace thread by default."
            ),
        },
    },
    "required": ["person", "text"],
}

_NOTIFY_PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "person": {
            "type": "string",
            "description": "The id or the name of one person of this Joshua.",
        },
        "text": {
            "type": "string",
            "description": "One short line: what they need to know, in your own words.",
        },
        "url": {
            "type": "string",
            "description": (
                "A path inside Joshua that the notification opens, as "
                "/chat or /wiki/recipes. The app itself by default."
            ),
        },
    },
    "required": ["person", "text"],
}


def build_workspace_server(
    deps: ToolDeps,
    settings: JoshuaConfig,
    deliver: Deliver | None = None,
    run: RunTurn | None = None,
    render: RenderBlock | None = None,
    notify: Notify | None = None,
) -> Any | None:
    """Build the ``workspace`` MCP server for one conversation.

    A workspace thread gets three tools, and a shared one ``notify_user`` as
    well: a personal workspace has nobody else to reach. A direct chat gets
    ``add_to_workspace`` and the four tools of normal chat. A shared (group)
    chat gets no server: the workspaces of one person do not belong in a chat
    that others read.

    ``notify_person`` is in every one of those, for a member: the roster says
    who the person of the turn is, and a guest does not get a tool that reaches
    somebody else.
    """
    if not _in_workspace(deps) and deps.channel.session_mode != "per_person":
        return None

    @tool(
        "add_to_workspace",
        "Add an item to a workspace: a document, plan, wiki page, link, or anything made "
        "or found outside Joshua. Adding the same kind and ref again updates the item. "
        "In a workspace chat the workspace is this one unless you name another.",
        _ADD_SCHEMA,
    )
    async def add_to_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_add_to_workspace(
            deps,
            kind=args.get("kind", ""),
            ref=args.get("ref", ""),
            title=args.get("title", ""),
            url=args.get("url") or None,
            workspace=args.get("workspace") or None,
        )

    @tool(
        "update_workspace_notes",
        "Replace the notes of this workspace. The notes are standing facts and rules, and "
        "they ride in every turn: keep them short and current, and drop what is old.",
        _NOTES_SCHEMA,
    )
    async def update_workspace_notes(args: dict[str, Any]) -> dict[str, Any]:
        return await do_update_workspace_notes(deps, notes=args.get("notes", ""))

    @tool(
        "send_to_normal_chat",
        "Send the person one message in their normal chat, with a link back to this "
        "workspace. Use it only when they asked for a message, or when the notify "
        "setting of this workspace allows it.",
        _SEND_SCHEMA,
    )
    async def send_to_normal_chat(args: dict[str, Any]) -> dict[str, Any]:
        return await do_send_to_normal_chat(
            deps, text=args.get("text", ""), settings=settings, deliver=deliver
        )

    @tool(
        "notify_user",
        "Notify one person of this shared workspace: a line on their phone, or in their "
        "normal chat when they have no device. Use it when the thread needs that person: "
        "a question for them, a decision that waits on them, something written or added "
        "for them, or a request to tell them. Give the url of the thing to read, so the "
        "tap opens it instead of this thread. It never notifies the person who wrote this "
        "turn, and one person gets one notification of this workspace in ten minutes.",
        _NOTIFY_SCHEMA,
    )
    async def notify_user(args: dict[str, Any]) -> dict[str, Any]:
        return await do_notify_user(
            deps,
            person=args.get("person", ""),
            text=args.get("text", ""),
            url=args.get("url") or None,
            settings=settings,
            deliver=deliver,
            notify=notify,
        )

    @tool(
        "notify_person",
        "Tell one person of this Joshua something now: a line on their phone, or in their "
        "normal chat when they have no device. Use it when you decide somebody needs to "
        "know (their plan changed, what they waited for arrived), or when a person asks you "
        "to tell somebody ('tell Lauren to look at the recipe'). It is a knock on the door: "
        "one short line, and a path that opens what it is about. One person gets one of "
        "these in ten minutes, so send the thing that matters.",
        _NOTIFY_PERSON_SCHEMA,
    )
    async def notify_person(args: dict[str, Any]) -> dict[str, Any]:
        return await do_notify_person(
            deps,
            person=args.get("person", ""),
            text=args.get("text", ""),
            url=args.get("url") or None,
            settings=settings,
            deliver=deliver,
            notify=notify,
        )

    @tool(
        "send_to_workspace",
        "Record something in one of the person's workspaces, without a turn: use it when "
        "what they said belongs to a workspace (a recipe for the dinner plans, a quote for "
        "the solar project). The workspace reads it on its next turn. Add links or "
        "documents with items. Say in one line that you did it.",
        _SEND_WS_SCHEMA,
    )
    async def send_to_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_send_to_workspace(
            deps,
            workspace=args.get("workspace", ""),
            text=args.get("text", ""),
            items=args.get("items"),
        )

    @tool(
        "tell_workspace",
        "Give a workspace a message and have it act on it now: it runs one turn in its own "
        "thread. Its reply stays in the workspace. Use send_to_workspace when no action is "
        "needed.",
        _TELL_SCHEMA,
    )
    async def tell_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_tell_workspace(
            deps, workspace=args.get("workspace", ""), text=args.get("text", ""), run=run
        )

    @tool(
        "list_workspaces",
        "List the person's workspaces: title, slug, status, and goal.",
        _NO_ARGS_SCHEMA,
    )
    async def list_workspaces(args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_workspaces(deps)

    @tool(
        "workspace_status",
        "The state of one workspace: its goal, notes, items, and open work. It does not "
        "show the thread.",
        _STATUS_SCHEMA,
    )
    async def workspace_status(args: dict[str, Any]) -> dict[str, Any]:
        return await do_workspace_status(deps, workspace=args.get("workspace", ""), render=render)

    tools = [add_to_workspace]
    person = settings.person(deps.person_id) if deps.person_id else None
    if person is not None and is_member(person.role):
        tools.append(notify_person)
    if _in_workspace(deps):
        tools += [update_workspace_notes, send_to_normal_chat]
        if _in_shared_workspace(deps):
            tools.append(notify_user)
    else:
        tools += [send_to_workspace, tell_workspace, list_workspaces, workspace_status]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def register(manager: Any, settings: JoshuaConfig, deliverer: Any = None, repo: Any = None) -> None:
    """Wire the ``workspace`` builtin into the conversation manager.

    ``deliverer`` is core's ``Deliverer``. Without one, ``send_to_normal_chat``
    says it is not available. ``tell_workspace`` runs its turn through the
    manager, and ``workspace_status`` renders the block of ``manager``.
    ``notify_user`` and ``notify_person`` send through core's push sender, which
    is a no-op while push is off, and then fall back to the normal chat of the
    person.
    """
    deliver: Deliver | None = deliverer.deliver if deliverer is not None else None
    repo = repo or getattr(manager, "_repo", None)
    background: set[asyncio.Task[None]] = set()

    async def run(workspace: Workspace, text: str) -> None:
        """Start one turn in the thread of ``workspace`` and do not wait for it.
        The turn is an event turn: the delivery gate keeps its reply in the
        transcript."""
        channel = await repo.get_channel(workspace.channel_id)
        if channel is None:
            return
        conversation = await repo.workspace_conversation(workspace)

        async def turn() -> None:
            try:
                await manager.run_turn(channel, conversation, text, direction="inject")
            except Exception as exc:  # noqa: BLE001 — a failed turn is in the thread already
                logger.warning(
                    {"message": "tell_workspace turn failed", "error": type(exc).__name__}
                )

        task = asyncio.create_task(turn())
        background.add(task)
        task.add_done_callback(background.discard)

    async def notify(person_id: str, notification: Notification) -> SendReport:
        """One notification to every browser of one person. It never raises."""
        return await send_to_person(repo, person_id, notification)

    async def render(workspace: Workspace) -> str:
        block = getattr(manager, "workspace_block", None)
        if block is None:
            return "The workspace status is not available here."
        conversation = await repo.workspace_conversation(workspace)
        return await block.render(workspace, conversation.id, fresh=False, mark_seen=None)

    manager.register_builtin(
        SERVER_NAME,
        lambda deps: build_workspace_server(deps, settings, deliver, run, render, notify),
    )
