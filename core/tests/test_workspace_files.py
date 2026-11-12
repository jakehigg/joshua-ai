"""A photo sent on a workspace thread becomes a ``file`` item of the workspace.

The describer is faked, and the repo keeps the items in memory the way the
store does: one item for each (workspace, kind, ref).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
)
from joshua_core.engine.manager import ConversationManager, _file_url
from joshua_core.engine.types import Attachment
from joshua_shared import config as config_module
from joshua_shared.attachments import AttachmentMeta, Description, sha256_file, write_meta

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
attachments:
  auto_save: false
"""


class ItemsRepo(FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def get_workspace(self, workspace_id: str) -> None:
        # No workspace row: the thread runs with the ordinary session.
        return None

    async def add_workspace_item(self, workspace_id, kind, ref, title, **kw):  # type: ignore[no-untyped-def]
        key = (workspace_id, kind, ref)
        if key in self.items:
            if kw.get("rename", True):
                self.items[key]["title"] = title
        else:
            self.items[key] = {"title": title, **kw}
        return self.items[key]


@pytest.fixture
def described(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(abs_path: Path, **kwargs: Any) -> AttachmentMeta:
        meta = AttachmentMeta(
            mime=kwargs["mime"],
            original_name="IMG_1.jpg",
            sha256=sha256_file(abs_path),
            description=Description(kind="photo", subject="an e-paper display", slug="epaper"),
            received_at="2026-11-09T14:05:09+00:00",
        )
        write_meta(abs_path, meta)
        return meta

    monkeypatch.setattr("joshua_core.engine.manager.describe_attachment", fake)


def _manager(repo: ItemsRepo, tmp_path: Path) -> ConversationManager:
    settings = config_module.parse(CONFIG, env={}, source="<test>")
    return ConversationManager(
        repo=repo,
        settings=settings,
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


def _photo(tmp_path: Path, name: str = "a.jpg") -> Attachment:
    rel = f"people/alex/attachments/2026/11/{name}"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff" + name.encode())
    write_meta(path, AttachmentMeta(mime="image/jpeg", original_name="IMG_1.jpg"))
    return Attachment(path=rel, mime="image/jpeg", name=name, original_name="IMG_1.jpg")


def _workspace_channel() -> Any:
    channel = make_channel("workspace:w1")
    return channel.__class__(id="workspace:w1", channel_type="workspace", session_mode="per_person")


async def test_a_photo_on_a_workspace_thread_is_one_file_item(
    tmp_path: Path, described: None
) -> None:
    repo = ItemsRepo()
    manager = _manager(repo, tmp_path)
    channel = _workspace_channel()
    conversation = make_conversation("c1", "alex", channel_id="workspace:w1")
    attachment = _photo(tmp_path)

    await manager.run_turn(
        channel, conversation, "look", attachments=[attachment], person_id="alex"
    )

    assert len(repo.items) == 1
    (workspace_id, kind, ref), item = next(iter(repo.items.items()))
    assert (workspace_id, kind) == ("w1", "file")
    assert ref.startswith("people/alex/attachments/2026/11/") and ref.endswith(".jpg")
    assert item["title"] == "an e-paper display"
    assert item["added_by"] == "person"
    assert item["url"] == "/api/v1/files/attachments/2026/11/" + ref.rsplit("/", 1)[1]


async def test_the_same_file_added_again_is_not_a_second_item(
    tmp_path: Path, described: None
) -> None:
    repo = ItemsRepo()
    manager = _manager(repo, tmp_path)
    channel = _workspace_channel()
    conversation = make_conversation("c1", "alex", channel_id="workspace:w1")
    await manager.run_turn(
        channel, conversation, "look", attachments=[_photo(tmp_path)], person_id="alex"
    )
    (key, _) = next(iter(repo.items.items()))
    # The agent adds the same file by its path, with its own title.
    await repo.add_workspace_item("w1", "file", key[2], "Photo of the display", added_by="tool:x")
    assert len(repo.items) == 1


async def test_a_turn_that_is_not_on_a_workspace_adds_nothing(
    tmp_path: Path, described: None
) -> None:
    repo = ItemsRepo()
    manager = _manager(repo, tmp_path)
    await manager.run_turn(
        make_channel(),
        make_conversation(person_id="alex"),
        "look",
        attachments=[_photo(tmp_path)],
        person_id="alex",
    )
    assert repo.items == {}


async def test_a_turn_with_no_file_adds_nothing(tmp_path: Path) -> None:
    repo = ItemsRepo()
    manager = _manager(repo, tmp_path)
    await manager.run_turn(
        _workspace_channel(), make_conversation("c1", "alex", channel_id="workspace:w1"), "hi"
    )
    assert repo.items == {}


async def test_a_failing_store_does_not_fail_the_turn(tmp_path: Path, described: None) -> None:
    repo = ItemsRepo()

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    repo.add_workspace_item = boom  # type: ignore[method-assign]
    manager = _manager(repo, tmp_path)
    result = await manager.run_turn(
        _workspace_channel(),
        make_conversation("c1", "alex", channel_id="workspace:w1"),
        "look",
        attachments=[_photo(tmp_path)],
        person_id="alex",
    )
    assert result.text


def test_the_viewer_address_of_a_filed_file() -> None:
    assert (
        _file_url("wiki/attachments/2026/11/a b.jpg")
        == "/api/v1/files/wiki/attachments/2026/11/a%20b.jpg"
    )
    assert _file_url("shared/attachments/x.jpg") == "/api/v1/files/shared/attachments/x.jpg"
    assert (
        _file_url("people/alex/attachments/2026/11/a.jpg")
        == "/api/v1/files/attachments/2026/11/a.jpg"
    )
    assert _file_url("people/alex/notes/a.jpg") is None
    assert _file_url("wiki/../x/y.jpg") is None
