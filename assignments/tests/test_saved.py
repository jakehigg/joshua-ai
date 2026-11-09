"""Saved assignments: save, replace, list, delete, start by name, and the person scope."""

from __future__ import annotations

import pytest
from conftest import make_config, spec
from test_manager import CONFIG_OFF, make_manager


async def test_save_checks_the_spec_and_stores_it_as_given(channels) -> None:
    manager = make_manager()
    saved = await manager.save("alex", "Docs-Cleanup ", spec(budget={"five_hour_pct": 99}))
    assert saved["name"] == "docs-cleanup"
    assert saved["title"] == "Audit the gitops repo"
    assert saved["notes"] == ["budget.five_hour_pct 99 is above the maximum 90, so it is 90"]
    stored = manager.store.saved[saved["id"]]
    # The spec is kept as given: the defaults and the clamps apply at each run.
    assert stored["spec"] == spec(budget={"five_hour_pct": 99}) | {
        "goal": "Audit the gitops repo\nand file one issue per finding."
    }
    assert "model" not in stored["spec"]
    assert manager.store.rows == {}


@pytest.mark.parametrize(
    ("name", "raw", "reason"),
    [
        ("docs", {"goal": ""}, "invalid_spec"),
        ("docs", "not an object", "invalid_spec"),
        ("docs", {"goal": "x", "model": "gpt"}, "unknown_model"),
        ("docs", {"goal": "x", "repos": ["github.com/example-org/app"]}, "repo_not_allowed"),
        ("d", {"goal": "x"}, "invalid_arguments"),
        ("no spaces", {"goal": "x"}, "invalid_arguments"),
        ("x" * 61, {"goal": "x"}, "invalid_arguments"),
        (None, {"goal": "x"}, "invalid_arguments"),
    ],
)
async def test_save_refuses_what_start_refuses(channels, name, raw, reason) -> None:
    manager = make_manager()
    result = await manager.save("alex", name, raw)
    assert result["status"] == "rejected"
    assert result["reason"] == reason
    assert manager.store.saved == {}


async def test_save_refuses_when_disabled(channels) -> None:
    manager = make_manager()
    manager.load_config = lambda: make_config(text=CONFIG_OFF)
    assert (await manager.save("alex", "docs", spec()))["reason"] == "disabled"
    assert (await manager.start("alex", saved="docs"))["reason"] == "disabled"
    assert manager.store.saved == {}


async def test_save_with_the_same_name_replaces_the_spec(channels) -> None:
    manager = make_manager()
    first = await manager.save("alex", "docs", spec())
    second = await manager.save("alex", "docs", {"goal": "Clean the docs.", "title": "Docs"})
    assert second["id"] == first["id"]
    assert "has a new spec" in second["message"]
    assert "is saved" in first["message"]
    assert manager.store.saved[first["id"]]["spec"] == {"goal": "Clean the docs.", "title": "Docs"}
    assert len(manager.store.saved) == 1


async def test_list_gives_the_runs_and_the_last_run(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "never-run", spec(title="Never"))
    saved = await manager.save("alex", "docs", spec(title="Docs"))
    first = await manager.start("alex", saved=saved["name"])
    await manager.runner.idle()
    second = await manager.start("alex", saved="docs")
    await manager.runner.idle()
    await manager.draft("alex", saved="docs")
    await manager.start("alex", spec())
    listed = (await manager.list_saved("alex"))["saved"]
    assert [s["name"] for s in listed] == ["docs", "never-run"]
    docs, never = listed
    assert docs["title"] == "Docs"
    assert docs["runs"] == 2
    assert docs["last_run"]["id"] == second["id"] != first["id"]
    assert docs["last_run"]["status"] == "done"
    assert docs["last_run"]["finished_at"] is not None
    assert never["runs"] == 0 and never["last_run"] is None


async def test_start_by_name_with_and_without_an_override(channels) -> None:
    manager = make_manager("  max_concurrent: 2\n", delay=None)
    saved = await manager.save("alex", "docs", spec(title="Docs", max_turns=50))
    plain = await manager.start("alex", saved="docs")
    override = await manager.start("alex", {"model": "opus", "max_turns": 80}, saved="docs")
    rows = manager.store.rows
    assert rows[plain["id"]]["saved_id"] == saved["id"]
    assert rows[plain["id"]]["spec"]["model"] == "sonnet"
    assert rows[plain["id"]]["spec"]["max_turns"] == 50
    assert rows[override["id"]]["spec"]["model"] == "opus"
    assert rows[override["id"]]["spec"]["max_turns"] == 80
    assert rows[override["id"]]["spec"]["title"] == "Docs"
    assert override["saved"] == {"id": saved["id"], "name": "docs"}
    # The override is for one run: the saved spec does not change.
    assert manager.store.saved[saved["id"]]["spec"]["max_turns"] == 50


async def test_an_override_is_checked_as_a_spec(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "docs", spec())
    bad = await manager.start("alex", {"model": "gpt"}, saved="docs")
    unknown = await manager.start("alex", {"nope": 1}, saved="docs")
    not_object = await manager.start("alex", "x", saved="docs")
    assert bad["reason"] == "unknown_model"
    assert unknown["reason"] == "invalid_spec"
    assert not_object["reason"] == "invalid_spec"
    assert manager.store.rows == {}


async def test_start_needs_a_spec_or_a_saved_name(channels) -> None:
    manager = make_manager()
    neither = await manager.start("alex")
    bad_name = await manager.start("alex", saved="?")
    missing = await manager.start("alex", saved="nothing-here")
    assert neither["reason"] == "invalid_arguments"
    assert bad_name["reason"] == "invalid_arguments"
    assert missing["reason"] == "not_found"
    assert manager.store.rows == {}


async def test_a_person_sees_and_starts_only_their_own_saved_assignments(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "docs", spec())
    assert (await manager.list_saved("mia")) == {"saved": []}
    assert (await manager.start("mia", saved="docs"))["reason"] == "not_found"
    assert (await manager.delete_saved("mia", "docs"))["reason"] == "not_found"
    # The same name for another person is another saved assignment.
    theirs = await manager.save("mia", "docs", {"goal": "Mia's docs."})
    assert len(manager.store.saved) == 2
    assert manager.store.saved[theirs["id"]]["person_id"] == "mia"


async def test_delete_keeps_the_runs(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "docs", spec())
    started = await manager.start("alex", saved="docs")
    await manager.runner.idle()
    deleted = await manager.delete_saved("alex", "docs")
    assert deleted["status"] == "deleted"
    assert (await manager.delete_saved("alex", "docs"))["reason"] == "not_found"
    assert (await manager.delete_saved("alex", "?"))["reason"] == "invalid_arguments"
    row = await manager.store.get(started["id"])
    assert row is not None and row["saved_id"] is None
    assert (await manager.status(row))["saved"] is None


async def test_status_and_list_name_the_saved_assignment(channels) -> None:
    manager = make_manager()
    saved = await manager.save("alex", "docs", spec())
    started = await manager.start("alex", saved="docs")
    await manager.runner.idle()
    plain = await manager.start("alex", spec())
    ref = {"id": saved["id"], "name": "docs"}
    status = await manager.status(await manager.store.get(started["id"]))
    listed = (await manager.recent("alex", 10, None))["assignments"]
    assert status["saved"] == ref
    assert {a["id"]: a["saved"] for a in listed} == {plain["id"]: None, started["id"]: ref}
