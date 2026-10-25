"""The files MCP refuses a write into `wiki/knowledge/`, `wiki/questions/`,
`wiki/insights/`, and `wiki/job-runs/`.

The jobs own these folders. The agent reads them and writes none of them,
through `write_file` or through `rename_file`.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from conftest import gateway_session
from joshua_gateway.files_mcp import server
from joshua_gateway.main import lifespan

FOLDERS = ("knowledge", "questions", "insights", "job-runs")
MEMBER = {"X-Joshua-Person": "alex"}


@pytest.fixture
def data_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "data"
    (root / "wiki" / "houseplants").mkdir(parents=True)
    (root / "people" / "alex" / "attachments").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "wiki" / "houseplants" / "snake-plant.md").write_text("# Snake plant\n")
    (root / "wiki" / "knowledge" / "houseplants").mkdir(parents=True)
    (root / "wiki" / "knowledge" / "houseplants" / "snake-plant.md").write_text(
        "---\ntopic: houseplants/snake-plant.md\n---\nThe dossier.\n"
    )
    (root / "wiki" / "questions" / "houseplants").mkdir(parents=True)
    (root / "wiki" / "questions" / "houseplants" / "feed-dose.md").write_text(
        "---\nstatus: open\n---\nThe question.\n"
    )
    (root / "wiki" / "insights" / "garden").mkdir(parents=True)
    (root / "wiki" / "insights" / "garden" / "autumn.md").write_text(
        "---\nstatus: new\n---\nThe insight.\n"
    )
    (root / "wiki" / "job-runs" / "2026" / "03").mkdir(parents=True)
    (root / "wiki" / "job-runs" / "2026" / "03" / "curator-20260314-7.md").write_text(
        "---\njob: curator\nrun_id: 7\nindex: false\n---\nThe report.\n"
    )
    monkeypatch.setenv(server.DATA_ENV, str(root))
    return root


def files_yaml() -> str:
    return textwrap.dedent("""\
        files:
          kind: builtin
          allow: all
        """)


EXISTING = {
    "knowledge": "wiki/knowledge/houseplants/snake-plant.md",
    "questions": "wiki/questions/houseplants/feed-dose.md",
    "insights": "wiki/insights/garden/autumn.md",
    "job-runs": "wiki/job-runs/2026/03/curator-20260314-7.md",
}


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("mode", ["create", "overwrite", "append"])
async def test_write_file_refuses_a_job_folder(gateway, data_root, folder, mode) -> None:
    app = gateway(files_yaml())
    target = f"wiki/{folder}/houseplants/new-page.md"
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            new = await session.call_tool(
                "write_file", {"path": target, "content": "x\n", "mode": mode}
            )
            old = await session.call_tool(
                "write_file",
                {"path": EXISTING[folder], "content": "clobbered\n", "mode": "overwrite"},
            )
    for res in (new, old):
        assert res.is_error is True
        text = res.content[0].text
        assert "researcher job" in text and "insights job" in text
        assert f"wiki/{folder}/" in text
        assert "wiki/job-runs/" in text
    assert not (data_root / target).exists()
    assert "clobbered" not in (data_root / EXISTING[folder]).read_text()


@pytest.mark.parametrize("folder", FOLDERS)
async def test_rename_file_refuses_a_job_folder(gateway, data_root, folder) -> None:
    app = gateway(files_yaml())
    source = data_root / EXISTING[folder]
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            res = await session.call_tool(
                "rename_file", {"path": EXISTING[folder], "new_name": "renamed.md"}
            )
    assert res.is_error is True
    assert "researcher job" in res.content[0].text
    assert "wiki/job-runs/" in res.content[0].text
    assert source.exists()
    assert not (source.parent / "renamed.md").exists()


async def test_rename_into_a_job_run_name_and_append_to_a_report_are_refused(
    gateway, data_root
) -> None:
    """A report is the record of its run: an append does not add to it."""
    app = gateway(files_yaml())
    report = data_root / EXISTING["job-runs"]
    before = report.read_text()
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            appended = await session.call_tool(
                "write_file",
                {"path": EXISTING["job-runs"], "content": "more\n", "mode": "append"},
            )
            nested = await session.call_tool(
                "write_file", {"path": "wiki/job-runs/2026/04/new.md", "content": "x\n"}
            )
    assert appended.is_error is True and nested.is_error is True
    assert report.read_text() == before
    assert not (data_root / "wiki" / "job-runs" / "2026" / "04" / "new.md").exists()


@pytest.mark.parametrize("folder", FOLDERS)
async def test_read_file_in_a_job_folder_is_allowed(gateway, data_root, folder) -> None:
    app = gateway(files_yaml())
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            res = await session.call_tool("read_file", {"path": EXISTING[folder]})
            listed = await session.call_tool(
                "list_files", {"root": "wiki", "subpath": folder, "recursive": True}
            )
    assert res.is_error is not True
    assert "The " in res.content[0].text
    paths = [item["path"] for item in json.loads(listed.content[0].text)]
    assert EXISTING[folder] in paths


async def test_a_wiki_page_outside_the_job_folders_is_still_written(gateway, data_root) -> None:
    app = gateway(files_yaml())
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            res = await session.call_tool(
                "write_file", {"path": "wiki/houseplants/knowledge.md", "content": "ok\n"}
            )
    assert res.is_error is not True
    assert (data_root / "wiki" / "houseplants" / "knowledge.md").read_text() == "ok\n"


@pytest.mark.parametrize("folder", ["questionsx", "knowledgex", "insightsx", "job-runsx"])
async def test_a_folder_that_only_starts_like_a_job_folder_is_written(
    gateway, data_root, folder
) -> None:
    app = gateway(files_yaml())
    target = f"wiki/{folder}/note.md"
    async with lifespan(app):
        async with gateway_session(app, "/files", "core", MEMBER) as session:
            res = await session.call_tool("write_file", {"path": target, "content": "ok\n"})
    assert res.is_error is not True
    assert (data_root / target).read_text() == "ok\n"


def test_in_job_folder_unit(tmp_path) -> None:
    wiki = tmp_path / "wiki"
    assert server._in_job_folder(wiki / "knowledge", tmp_path)
    assert server._in_job_folder(wiki / "knowledge" / "a" / "b.md", tmp_path)
    assert server._in_job_folder(wiki / "insights" / "g" / "x.md", tmp_path)
    assert server._in_job_folder(wiki / "questions", tmp_path)
    assert server._in_job_folder(wiki / "questions" / "g" / "x.md", tmp_path)
    assert server._in_job_folder(wiki / "job-runs", tmp_path)
    assert server._in_job_folder(wiki / "job-runs" / "2026" / "03" / "x.md", tmp_path)
    assert not server._in_job_folder(wiki / "job-runsx" / "x.md", tmp_path)
    assert not server._in_job_folder(wiki / "questionsx" / "x.md", tmp_path)
    assert not server._in_job_folder(wiki / "knowledge.md", tmp_path)
    assert not server._in_job_folder(wiki / "garden" / "insights" / "x.md", tmp_path)
