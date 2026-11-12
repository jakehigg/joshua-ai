"""A turn on a ``workspace:`` channel, offline: core resolves it only for the
owner, never creates the channel, and delivers nothing."""

from __future__ import annotations

from typing import Any

from joshua_core.store.models import Channel, Person, Workspace
from test_turns_api import (
    CHANNELS_TOKEN,
    TurnsFakeRepo,
    _fleet_tokens,  # noqa: F401 — the autouse fixture of the fleet tokens
    bearer,
    build_turns,
    core_client,
    dm_event,
    make_settings,
)

WS_ID = "33333333-3333-3333-3333-333333333333"


class WorkspaceRepo(TurnsFakeRepo):
    """The turns fake repo with one workspace of alex and one of mia."""

    def __init__(self) -> None:
        super().__init__()
        self.people["mia"] = Person(id="mia", display_name="Mia", role="member")
        self.workspaces = {
            WS_ID: Workspace(id=WS_ID, slug="solar", title="Solar", owner="alex"),
            "44444444-4444-4444-4444-444444444444": Workspace(
                id="44444444-4444-4444-4444-444444444444", slug="trip", title="Trip", owner="mia"
            ),
        }
        self.channels[f"workspace:{WS_ID}"] = Channel(
            id=f"workspace:{WS_ID}", channel_type="workspace", default_person_id="alex"
        )

    async def find_workspace(self, owner: str, ref: str) -> Workspace | None:
        for w in self.workspaces.values():
            if w.owner == owner and ref in (w.slug, w.id):
                return w
        return None

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        return self.workspaces.get(workspace_id)


def _event(channel: str, person: str = "alex", **extra: Any) -> dict[str, Any]:
    return dm_event(
        channel=channel,
        chat={"kind": "dm", "id": person},
        handle={"type": "cli", "id": person},
        message_id=None,
        **extra,
    )


async def test_an_owner_turn_runs_and_nothing_is_delivered(tmp_path) -> None:
    repo = WorkspaceRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)

    async with core_client(turns) as client:
        for ref in ("solar", WS_ID):
            resp = await client.post(
                "/v1/turns", json=_event(f"workspace:{ref}"), headers=bearer(CHANNELS_TOKEN)
            )
            assert resp.status_code == 202
        await turns.drain()

    assert received == []  # transcript-only
    rows = [r for r in repo.transcripts if r["direction"] == "out"]
    assert len(rows) == 2 and rows[0]["content"].startswith("(stub)")
    assert {k[0] for k in repo.conversations} == {f"workspace:{WS_ID}"}


async def test_the_stream_runs_on_the_workspace_thread(tmp_path) -> None:
    repo = WorkspaceRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        async with client.stream(
            "POST",
            "/v1/turns/stream",
            json=_event("workspace:solar"),
            headers=bearer(CHANNELS_TOKEN),
        ) as resp:
            assert resp.status_code == 200
            body = "".join([c async for c in resp.aiter_text()])
    assert "event: done" in body
    assert (f"workspace:{WS_ID}", "alex") in repo.conversations


async def test_a_foreign_or_unknown_workspace_is_refused_and_creates_nothing(tmp_path) -> None:
    repo = WorkspaceRepo()
    before = set(repo.channels)
    turns = build_turns(repo, make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        for channel, person in (
            ("workspace:trip", "alex"),  # the slug of mia's workspace
            ("workspace:44444444-4444-4444-4444-444444444444", "alex"),  # her id
            ("workspace:solar", "mia"),  # alex's, asked for by mia
            ("workspace:nothing", "alex"),
            ("workspace:55555555-5555-5555-5555-555555555555", "alex"),
        ):
            resp = await client.post(
                "/v1/turns", json=_event(channel, person), headers=bearer(CHANNELS_TOKEN)
            )
            assert resp.status_code == 403, channel
            assert resp.json()["reason"] == "unknown_sender"
            stream = await client.post(
                "/v1/turns/stream", json=_event(channel, person), headers=bearer(CHANNELS_TOKEN)
            )
            assert stream.status_code == 403

    assert set(repo.channels) == before
    assert repo.conversations == {}
    assert repo.transcripts == []


async def test_a_workspace_turn_from_a_group_is_refused(tmp_path) -> None:
    repo = WorkspaceRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    event = _event("workspace:solar")
    event["chat"] = {"kind": "group", "id": "-100"}
    event["handle"] = {"type": "telegram", "id": "998877"}
    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=event, headers=bearer(CHANNELS_TOKEN))
    assert resp.status_code == 403
