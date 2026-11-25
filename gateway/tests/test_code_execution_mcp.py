"""The code execution MCP: the kernel of each conversation, the files that go in and
come out, the limits of a run, and who gets the tools.

The Jupyter server is a fake here: an in-memory folder tree and a kernel whose
run gives a scripted ``Execution``. ``test_code_execution_jupyter.py`` tests the client
against a fake server over HTTP, and ``test_code_execution_live.py`` runs the real
image. A few tests host the builtin in a gateway app, so the person and role
hand-off runs as in production.
"""

from __future__ import annotations

import base64
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import gateway_session, joshua_yaml
from joshua_gateway.code_execution_mcp import server as ce_server
from joshua_gateway.code_execution_mcp.jupyter import CodeExecutionError, Entry, Execution
from joshua_gateway.code_execution_mcp.server import CodeExecution, _Request
from joshua_gateway.files_mcp import server as files_server
from joshua_gateway.main import lifespan
from joshua_shared import config
from joshua_shared.config import CodeExecutionConfig

ALEX = _Request(person="alex", member=True, conversation="conv-alex")
URL = "http://code-execution.test:8888"


@dataclass
class FakeJupyter:
    """A Jupyter server in memory. ``runner`` decides what each run gives."""

    runner: Callable[[str, FakeJupyter, str], Execution] | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: dict[str, datetime] = field(default_factory=dict)
    kernels: dict[str, str] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)

    async def start_kernel(self, folder: str) -> str:
        kernel_id = f"k{len(self.started) + 1}"
        self.kernels[kernel_id] = folder
        self.started.append(kernel_id)
        return kernel_id

    async def kernel_alive(self, kernel_id: str) -> bool:
        return kernel_id in self.kernels

    async def interrupt(self, kernel_id: str) -> None:
        self.interrupted.append(kernel_id)

    async def shutdown(self, kernel_id: str) -> None:
        self.kernels.pop(kernel_id, None)
        self.stopped.append(kernel_id)

    async def execute(self, kernel_id: str, code: str, timeout_s: float) -> Execution:
        self.codes.append(code)
        if self.runner is None:
            return Execution(stdout="ok\n")
        return self.runner(code, self, self.kernels[kernel_id])

    async def mkdir(self, path: str) -> None:
        self.dirs.setdefault(path, datetime.now(UTC))

    async def put_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def listdir(self, path: str) -> list[Entry]:
        prefix = path.rstrip("/") + "/"
        out = []
        for name, data in self.files.items():
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                out.append(Entry(name[len(prefix) :], name, "file", len(data), None))
        for name, when in self.dirs.items():
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                out.append(Entry(name[len(prefix) :], name, "directory", None, when))
        return out

    async def get_file(self, path: str) -> bytes:
        return self.files[path]

    async def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.dirs.pop(path, None)

    async def remove_tree(self, path: str) -> None:
        for name in [n for n in self.files if n.startswith(path + "/")]:
            del self.files[name]
        for name in [n for n in self.dirs if n == path or n.startswith(path + "/")]:
            del self.dirs[name]


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "people" / "alex" / "attachments").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "documents" / "alex").mkdir(parents=True)
    (root / "documents" / "mia").mkdir(parents=True)
    (root / "documents" / "shared").mkdir(parents=True)
    return root


def make(data_root: Path, fake: FakeJupyter | None = None, **settings) -> CodeExecution:
    cfg = CodeExecutionConfig(enabled=True, url=URL, **settings)
    return CodeExecution(cfg, fake or FakeJupyter(), data_root)


def text_of(result) -> str:
    return result.content[0].text


# -- the kernel of a conversation ---------------------------------------------


async def test_one_conversation_keeps_one_kernel_across_runs(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(ALEX, {"code": "df = 1"})
    await box.run(ALEX, {"code": "print(df)"})
    assert fake.started == ["k1"]
    assert fake.codes == ["df = 1", "print(df)"]


async def test_two_conversations_get_two_kernels_in_two_folders(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(ALEX, {"code": "1"})
    await box.run(_Request("mia", True, "conv-mia"), {"code": "2"})
    assert fake.started == ["k1", "k2"]
    assert fake.kernels["k1"] != fake.kernels["k2"]


async def test_a_folder_names_no_person_and_no_conversation(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(ALEX, {"code": "1"})
    folder = fake.kernels["k1"]
    assert folder.startswith("conversations/c-")
    assert "alex" not in folder and "conv" not in folder.removeprefix("conversations/")


async def test_a_kernel_that_died_is_started_again(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(ALEX, {"code": "1"})
    fake.kernels.clear()  # the server culled it
    await box.run(ALEX, {"code": "2"})
    assert fake.started == ["k1", "k2"]


async def test_a_request_with_no_conversation_gets_a_kernel_for_one_run(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(_Request("alex", True, None), {"code": "1"})
    assert fake.stopped == ["k1"]
    assert not [d for d in fake.dirs if d.startswith("conversations/c-")]


async def test_over_the_kernel_limit_the_least_recent_kernel_is_shut_down(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake, max_kernels=2)
    for name in ("a", "b", "c"):
        await box.run(_Request("alex", True, f"conv-{name}"), {"code": "1"})
    assert fake.stopped == ["k1"]
    assert sorted(fake.kernels) == ["k2", "k3"]


async def test_reset_shuts_the_kernel_down_and_removes_the_folder(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake)
    await box.run(ALEX, {"code": "1"})
    folder = fake.kernels["k1"]
    result = await box.reset(ALEX)
    assert "shut down" in text_of(result)
    assert fake.stopped == ["k1"]
    assert not [d for d in fake.dirs if d.startswith(folder)]
    await box.run(ALEX, {"code": "2"})
    assert fake.started == ["k1", "k2"]


async def test_the_sweep_removes_old_folders_that_no_kernel_uses(data_root):
    fake = FakeJupyter()
    box = make(data_root, fake, folder_ttl_hours=1)
    fake.dirs["conversations"] = datetime.now(UTC)
    fake.dirs["conversations/c-old"] = datetime.now(UTC) - timedelta(hours=2)
    fake.dirs["conversations/c-new"] = datetime.now(UTC)
    await box.run(ALEX, {"code": "1"})
    assert "conversations/c-old" not in fake.dirs
    assert "conversations/c-new" in fake.dirs


async def test_the_first_call_sweeps_whatever_the_clock_reads(data_root, monkeypatch):
    # A fresh host has a small monotonic clock: the first sweep must still run.
    monkeypatch.setattr(ce_server.time, "monotonic", lambda: 5.0)
    fake = FakeJupyter()
    box = make(data_root, fake, folder_ttl_hours=1)
    fake.dirs["conversations/c-old"] = datetime.now(UTC) - timedelta(hours=2)
    await box.run(ALEX, {"code": "1"})
    assert "conversations/c-old" not in fake.dirs


# -- the files of a run -------------------------------------------------------


async def test_a_wiki_file_and_an_own_document_are_copied_into_in(data_root):
    (data_root / "wiki" / "books.md").write_text("title\nA\n")
    (data_root / "documents" / "alex" / "list.md").write_text("---\ntitle: L\n---\nB\n")
    fake = FakeJupyter()
    box = make(data_root, fake)
    result = await box.run(
        ALEX, {"code": "1", "files": ["wiki/books.md", "documents/alex/list.md"]}
    )
    folder = fake.kernels["k1"]
    assert fake.files[f"{folder}/in/books.md"] == b"title\nA\n"
    assert fake.files[f"{folder}/in/list.md"].endswith(b"B\n")
    assert "wiki/books.md -> in/books.md" in text_of(result)


async def test_two_inputs_with_one_name_get_two_names(data_root):
    (data_root / "wiki" / "a").mkdir()
    (data_root / "wiki" / "a" / "list.md").write_text("1")
    (data_root / "wiki" / "list.md").write_text("2")
    fake = FakeJupyter()
    box = make(data_root, fake)
    result = await box.run(ALEX, {"code": "1", "files": ["wiki/a/list.md", "wiki/list.md"]})
    assert "-> in/list.md" in text_of(result)
    assert "-> in/list-2.md" in text_of(result)


@pytest.mark.parametrize(
    "path",
    [
        "documents/mia/plan.md",
        "documents/alex/../mia/plan.md",
        "wiki/../documents/mia/plan.md",
        "/etc/passwd",
        "people/../../etc/passwd",
        "wiki/.git/config",
        "nowhere/x.md",
    ],
)
async def test_a_file_the_person_may_not_read_is_refused(data_root, path):
    (data_root / "documents" / "mia" / "plan.md").write_text("secret")
    fake = FakeJupyter()
    box = make(data_root, fake)
    with pytest.raises(CodeExecutionError):
        await box.run(ALEX, {"code": "1", "files": [path]})
    assert fake.started == []
    assert not any(b"secret" in data for data in fake.files.values())


async def test_inputs_over_the_size_limit_are_refused(data_root):
    (data_root / "wiki" / "big.md").write_bytes(b"x" * 4096)
    box = make(data_root, max_input_bytes=2048)
    with pytest.raises(CodeExecutionError, match="larger than 2048 bytes"):
        await box.run(ALEX, {"code": "1", "files": ["wiki/big.md"]})


async def test_out_is_emptied_before_each_run(data_root):
    def runner(code, fake, folder):
        if code == "first":
            fake.files[f"{folder}/out/old.csv"] = b"a\n"
        return Execution()

    fake = FakeJupyter(runner=runner)
    box = make(data_root, fake)
    first = await box.run(ALEX, {"code": "first"})
    second = await box.run(ALEX, {"code": "second"})
    assert "out/old.csv" in text_of(first)
    assert "out/old.csv" not in text_of(second)


async def test_an_output_image_is_an_image_and_a_csv_is_text(data_root):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    def runner(code, fake, folder):
        fake.files[f"{folder}/out/chart.png"] = png
        fake.files[f"{folder}/out/rows.csv"] = b"title,n\nA,2\n"
        fake.files[f"{folder}/out/model.pkl"] = b"\x80\x04"
        return Execution()

    box = make(data_root, FakeJupyter(runner=runner))
    result = await box.run(ALEX, {"code": "1"})
    text = text_of(result)
    assert "out/rows.csv (12 bytes):\ntitle,n\nA,2" in text
    assert "out/model.pkl: 2 bytes, not shown" in text
    assert "out/chart.png: image" in text
    (image,) = [c for c in result.content if c.type == "image"]
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == png


# -- what a run gives back -----------------------------------------------------


async def test_the_result_holds_stdout_the_value_and_the_error(data_root):
    def runner(code, fake, folder):
        return Execution(stdout="138\n", results=["'done'"], error="ZeroDivisionError: x")

    result = await make(data_root, FakeJupyter(runner=runner)).run(ALEX, {"code": "1"})
    assert text_of(result) == "stdout:\n138\n\nresult:\n'done'\n\nerror:\nZeroDivisionError: x"
    assert result.is_error is not True


async def test_long_output_is_cut_and_the_result_says_so(data_root):
    def runner(code, fake, folder):
        return Execution(stdout="x" * 5000)

    result = await make(data_root, FakeJupyter(runner=runner), max_output_chars=1000).run(
        ALEX, {"code": "1"}
    )
    assert "[... cut: 4000 more characters]" in text_of(result)
    assert len(text_of(result)) < 1200


async def test_a_run_past_its_time_is_interrupted_and_keeps_the_kernel(data_root):
    def runner(code, fake, folder):
        return Execution(timed_out=code != "pass")

    fake = FakeJupyter(runner=runner)
    result = await make(data_root, fake).run(ALEX, {"code": "while True: pass", "timeout_s": 5})
    assert fake.interrupted == ["k1"]
    assert fake.stopped == []
    assert "timed out after 5 s; the run was interrupted" in text_of(result)


async def test_a_kernel_that_does_not_stop_is_shut_down(data_root):
    fake = FakeJupyter(runner=lambda code, fake, folder: Execution(timed_out=True))
    result = await make(data_root, fake).run(ALEX, {"code": "x", "timeout_s": 5})
    assert fake.stopped == ["k1"]
    assert "shut down and its variables are gone" in text_of(result)


async def test_the_timeout_of_a_call_never_passes_the_limit(data_root):
    seen = []

    class Clock(FakeJupyter):
        async def execute(self, kernel_id, code, timeout_s):
            seen.append(timeout_s)
            return Execution()

    await make(data_root, Clock(), run_timeout_s=30).run(ALEX, {"code": "1", "timeout_s": 600})
    assert seen == [30.0]


@pytest.mark.parametrize(
    "args",
    [
        {"code": ""},
        {"code": "x" * 100_001},
        {"code": "1", "files": "wiki/a.md"},
        {"code": "1", "files": [f"wiki/{i}.md" for i in range(21)]},
        {"code": "1", "timeout_s": -1},
    ],
)
async def test_a_bad_call_is_refused(data_root, args):
    with pytest.raises(CodeExecutionError):
        await make(data_root).run(ALEX, args)


# -- who gets the tools -------------------------------------------------------


def code_execution_yaml() -> str:
    return textwrap.dedent("""\
        code-execution:
          kind: builtin
          allow: all
        """)


@pytest.fixture
def code_execution_app(gateway, config_path, monkeypatch, data_root):
    """A gateway app with the code-execution builtin, on a fake Jupyter server."""

    def _make(enabled: bool = True, fake: FakeJupyter | None = None):
        app = gateway(code_execution_yaml())
        extra = f"code_execution:\n  enabled: {str(enabled).lower()}\n  url: {URL}\n"
        config_path.write_text(joshua_yaml(code_execution_yaml()) + extra)
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setenv(files_server.DATA_ENV, str(data_root))
        monkeypatch.setenv(ce_server.TOKEN_ENV, "test-token")
        cfg = CodeExecutionConfig(enabled=enabled, url=URL)
        monkeypatch.setitem(
            ce_server._EXECUTORS, URL, CodeExecution(cfg, fake or FakeJupyter(), data_root)
        )
        return app

    return _make


ALEX_HEADERS = {
    "X-Joshua-Person": "alex",
    "X-Joshua-Role": "member",
    "X-Joshua-Conversation": "conv-1",
}


async def test_a_member_runs_code_through_the_gateway(code_execution_app):
    fake = FakeJupyter()
    app = code_execution_app(fake=fake)
    async with lifespan(app):
        async with gateway_session(app, "/code-execution", "core", ALEX_HEADERS) as session:
            tools = await session.list_tools()
            result = await session.call_tool("run_python", {"code": "print(1)"})
    assert sorted(t.name for t in tools.tools) == ["reset_python", "run_python"]
    assert result.is_error is False, result.content[0].text
    assert text_of(result) == "stdout:\nok"
    assert fake.codes == ["print(1)"]


async def test_a_guest_gets_no_code_execution_tools_and_a_call_is_refused(code_execution_app):
    fake = FakeJupyter()
    app = code_execution_app(fake=fake)
    headers = {"X-Joshua-Person": "mia", "X-Joshua-Role": "guest"}
    async with lifespan(app):
        async with gateway_session(app, "/code-execution", "core", headers) as session:
            tools = await session.list_tools()
            result = await session.call_tool("run_python", {"code": "print(1)"})
    assert tools.tools == []
    assert result.is_error is True
    assert fake.codes == []


async def test_code_execution_off_has_no_tools_and_runs_nothing(code_execution_app):
    fake = FakeJupyter()
    app = code_execution_app(enabled=False, fake=fake)
    async with lifespan(app):
        async with gateway_session(app, "/code-execution", "core", ALEX_HEADERS) as session:
            tools = await session.list_tools()
            result = await session.call_tool("run_python", {"code": "print(1)"})
    assert tools.tools == []
    assert result.is_error is True
    assert fake.codes == []


async def test_code_execution_with_no_token_says_so(code_execution_app, monkeypatch):
    app = code_execution_app()
    monkeypatch.setenv(ce_server.TOKEN_ENV, "")
    monkeypatch.delitem(ce_server._EXECUTORS, URL)
    async with lifespan(app):
        async with gateway_session(app, "/code-execution", "core", ALEX_HEADERS) as session:
            result = await session.call_tool("run_python", {"code": "print(1)"})
    assert result.is_error is True
    assert "CODE_EXECUTION_TOKEN" in text_of(result)


def test_the_config_warns_when_the_server_is_listed_but_code_execution_is_off():
    cfg = config.JoshuaConfig.model_validate(
        {
            "name": "T",
            "timezone": "UTC",
            "people": [{"id": "alex", "name": "Alex"}],
            "mcp": {"code-execution": {"kind": "builtin", "allow": "all"}},
        }
    )
    assert config.CODE_EXECUTION_OFF_WARNING in cfg.warnings
