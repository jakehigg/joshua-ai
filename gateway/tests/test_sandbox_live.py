"""The sandbox against the real image: a Jupyter server in a container.

These tests need the sandbox container, so they are integration tests. The
`sandbox` job of CI starts the pinned image with the command of the compose
file and sets ``SANDBOX_TEST_URL`` and ``SANDBOX_TEST_TOKEN``. Locally::

    docker run -d -p 127.0.0.1:18888:8888 -e JUPYTER_TOKEN=t <image> \\
        jupyter server --ServerApp.ip=0.0.0.0 --ServerApp.root_dir=/home/jovyan
    SANDBOX_TEST_URL=http://127.0.0.1:18888 SANDBOX_TEST_TOKEN=t \\
        uv run --package joshua-gateway pytest gateway/tests/test_sandbox_live.py -m integration

The network of the container is not under test here: CI runs it with a route
out. ``tests/test_sandbox_isolation.py`` asserts the networks of both
deployment paths.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from joshua_gateway.sandbox_mcp.jupyter import Jupyter
from joshua_gateway.sandbox_mcp.server import Sandbox, _Request
from joshua_shared.config import SandboxConfig

URL = os.environ.get("SANDBOX_TEST_URL", "")
TOKEN = os.environ.get("SANDBOX_TEST_TOKEN", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL or not TOKEN, reason="SANDBOX_TEST_URL and SANDBOX_TEST_TOKEN"),
]

# Two exports of one reading list. The second has one book less, and one book
# that the first logs twice.
FIRST = (
    "title,author,finished\n"
    "Wolf,A. Writer,2026-01-03\n"
    "Tide,B. Writer,2026-02-10\n"
    "Tide,B. Writer,2026-06-01\n"
    "Stone,C. Writer,2026-03-14\n"
)
SECOND = "Title,Author,Date Read\nWolf,A. Writer,2026/01/03\nTide,B. Writer,2026/02/10\n"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "first.md").write_text(FIRST)
    (root / "documents" / "alex").mkdir(parents=True)
    (root / "documents" / "alex" / "second.md").write_text(SECOND)
    return root


@pytest.fixture
async def box(data_root: Path):
    client = Jupyter(URL, TOKEN)
    sandbox = Sandbox(SandboxConfig(enabled=True, url=URL, run_timeout_s=60), client, data_root)
    yield sandbox
    await client.close()


def text_of(result) -> str:
    return result.content[0].text


async def test_pandas_diffs_two_lists_and_the_kernel_keeps_them(box):
    req = _Request("alex", True, f"live-{uuid.uuid4().hex}")
    first = await box.run(
        req,
        {
            "code": (
                "import pandas as pd\n"
                "a = pd.read_csv('in/first.md')\n"
                "b = pd.read_csv('in/second.md')\n"
                "b.columns = ['title', 'author', 'finished']\n"
                "missing = sorted(set(a.title) - set(b.title))\n"
                "print(len(a), len(b), missing)\n"
                "a[a.title.isin(missing)].to_csv('out/missing.csv', index=False)\n"
            ),
            "files": ["wiki/first.md", "documents/alex/second.md"],
        },
    )
    assert "4 2 ['Stone']" in text_of(first), text_of(first)
    assert "out/missing.csv" in text_of(first)
    assert "Stone,C. Writer,2026-03-14" in text_of(first)

    again = await box.run(req, {"code": "a.title.value_counts().max()"})
    assert "result:\n2" in text_of(again), text_of(again)

    reset = await box.reset(req)
    assert "shut down" in text_of(reset)
    gone = await box.run(req, {"code": "print('a' in globals())"})
    assert "stdout:\nFalse" in text_of(gone)
    await box.reset(req)


async def test_a_chart_comes_back_as_an_image(box):
    req = _Request("alex", True, f"live-{uuid.uuid4().hex}")
    result = await box.run(
        req,
        {
            "code": (
                "import matplotlib\nmatplotlib.use('Agg')\n"
                "import matplotlib.pyplot as plt\n"
                "plt.bar(['a', 'b'], [1, 2])\nplt.savefig('out/chart.png')\n"
            )
        },
    )
    assert [c.mime_type for c in result.content if c.type == "image"] == ["image/png"]
    await box.reset(req)


async def test_a_run_past_its_time_is_interrupted_and_the_kernel_lives(box):
    req = _Request("alex", True, f"live-{uuid.uuid4().hex}")
    await box.run(req, {"code": "x = 41"})
    slow = await box.run(req, {"code": "import time\ntime.sleep(60)", "timeout_s": 2})
    assert "the run was interrupted" in text_of(slow)
    after = await box.run(req, {"code": "x + 1"})
    assert "result:\n42" in text_of(after)
    await box.reset(req)


async def test_an_error_gives_the_traceback(box):
    req = _Request("alex", True, None)
    result = await box.run(req, {"code": "1 / 0"})
    assert "ZeroDivisionError" in text_of(result)
    assert "\x1b[" not in text_of(result)
