"""The item that a recorded output makes in the workspace of a run."""

from __future__ import annotations

from conftest import make_settings
from fakes import FakeStore
from joshua_assignments.sdk_runner import AssignmentTools
from joshua_assignments.workspace_items import Item, item_of
from sdk_fakes import FakeCore

WORKSPACE = "11111111-1111-4111-8111-111111111111"
DOCUMENT = "documents/alex/ha-backlog-burndown.md"


def test_a_document_output_is_a_document_item_that_opens_the_page() -> None:
    item = item_of({"kind": "document", "ref": DOCUMENT, "title": "HA backlog burndown"})
    assert item == Item(
        "document", DOCUMENT, "HA backlog burndown", "/documents/alex/ha-backlog-burndown"
    )


def test_the_kind_of_an_output_that_names_a_document_comes_from_the_path() -> None:
    assert item_of({"kind": "plan", "ref": DOCUMENT}).kind == "document"
    assert item_of({"kind": "note", "ref": "documents/alex/report.html"}).kind == "artifact"


def test_a_url_output_keeps_its_address_and_an_unknown_kind_is_a_link() -> None:
    url = "https://gitlab.example.net/infra/backlog/-/issues/7"
    item = item_of({"kind": "issue", "ref": url, "title": "Old CI"})
    assert item == Item("link", url, "Old CI", url)
    assert item_of({"kind": "pr", "ref": url}).kind == "pr"
    assert item_of({"kind": "wiki", "ref": "wiki/notes.md"}) == Item("wiki", "wiki/notes.md", "")


def test_an_output_that_names_nothing_is_no_item() -> None:
    assert item_of({"kind": "document", "ref": "  "}) is None
    assert item_of({"kind": "document", "ref": "x" * 1001}) is None


async def tools(core: FakeCore, **fields) -> AssignmentTools:
    class ToolHost:
        def __init__(self) -> None:
            self.store = FakeStore()

    host = ToolHost()
    row = await host.store.create(
        person_id="alex", conversation_id=None, spec={"goal": "Audit.", **fields}, model="m"
    )
    return AssignmentTools(row["id"], host, core=core.client(make_settings()))  # type: ignore[arg-type]


async def test_an_output_of_a_run_of_a_workspace_becomes_one_item() -> None:
    core = FakeCore()
    run = await tools(core, workspace=WORKSPACE)
    await run.record_output({"kind": "document", "ref": DOCUMENT, "title": "Burndown"})
    assert core.items == [
        {
            "kind": "document",
            "ref": DOCUMENT,
            "title": "Burndown",
            "url": "/documents/alex/ha-backlog-burndown",
        }
    ]
    request = core.item_requests[0]
    assert request.url.path == f"/v1/workspaces/{WORKSPACE}/items"
    assert request.url.params["person"] == "alex"
    # The same output again sends the same item: core keeps one row for it.
    await run.record_output({"kind": "document", "ref": DOCUMENT, "title": "Burndown"})
    assert core.items[0] == core.items[1]


async def test_a_run_with_no_workspace_records_no_item() -> None:
    core = FakeCore()
    run = await tools(core)
    await run.record_output({"kind": "document", "ref": DOCUMENT, "title": "Burndown"})
    assert core.item_requests == []


async def test_a_refused_item_does_not_fail_the_tool_call() -> None:
    core = FakeCore()
    # Core answers 404 when the person of the run is not in the workspace.
    core.item_status = 404
    run = await tools(core, workspace=WORKSPACE)
    reply = await run.record_output({"kind": "document", "ref": DOCUMENT})
    assert reply["content"][0]["text"] == "Output recorded. Outputs: 1."
    assert core.items == []
