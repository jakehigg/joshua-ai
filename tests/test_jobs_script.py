"""`scripts/jobs.sh` against a fake curl.

The stub curl on PATH records its arguments, the config it read on stdin, and
the body it would send, then answers with a canned status and body. So every
request the script makes is observable, offline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "jobs.sh"
TOKEN = "laptop-secret-token"

CURL_STUB = r"""#!/usr/bin/env bash
out=""; method="GET"; data=""; url=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    -o) i=$((i+1)); out="${args[$i]}" ;;
    -w|--max-time|-H) i=$((i+1)) ;;
    -X) i=$((i+1)); method="${args[$i]}" ;;
    --data-binary) i=$((i+1)); data="${args[$i]#@}" ;;
    --config) i=$((i+1)) ;;
    -*) ;;
    *) url="$a" ;;
  esac
  i=$((i+1))
done
config="$(cat)"
body=""
if [ -n "$data" ]; then body="$(cat "$data")"; fi
python3 - "$CALL_LOG" "$method" "$url" "$config" "$body" "$*" <<'PY'
import json, sys
log, method, url, config, body, argv = sys.argv[1:]
with open(log, "a") as fh:
    fh.write(json.dumps({"method": method, "url": url, "config": config,
                         "body": body, "argv": argv}) + "\n")
PY
if [ "${CURL_FAIL:-0}" = "1" ]; then exit 7; fi
printf '%s' "$ANSWER_BODY" > "$out"
printf '%s' "$ANSWER_STATUS"
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "curl"
    stub.write_text(CURL_STUB)
    stub.chmod(0o755)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CALL_LOG": str(tmp_path / "calls.jsonl"),
        "JOSHUA_TOKEN_LAPTOP": TOKEN,
        "ANSWER_STATUS": "202",
        "ANSWER_BODY": '{"run_id": 5}',
    }


def _run(env: dict[str, str], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**env, **extra},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _calls(env: dict[str, str]) -> list[dict[str, str]]:
    path = Path(env["CALL_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


pytestmark = pytest.mark.skipif(shutil.which("python3") is None, reason="needs python3")


def test_run_posts_the_arguments_and_prints_json(env) -> None:
    result = _run(
        env, "run", "bundle", "--arg", "topic=houseplants/snake-plant.md", "--arg", "note=a=b"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"run_id": 5}
    assert '    "run_id": 5' in result.stdout
    (call,) = _calls(env)
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8081/admin/jobs/bundle/run"
    assert json.loads(call["body"]) == {
        "args": {"topic": "houseplants/snake-plant.md", "note": "a=b"},
        "dry_run": False,
    }
    assert call["config"] == f'header = "Authorization: Bearer {TOKEN}"'
    assert TOKEN not in call["argv"]


def test_run_dry_run_and_core_url(env) -> None:
    result = _run(env, "run", "bundle", "--dry-run", CORE_URL="http://core.example.test:9000/")
    assert result.returncode == 0, result.stderr
    (call,) = _calls(env)
    assert call["url"] == "http://core.example.test:9000/admin/jobs/bundle/run"
    assert json.loads(call["body"]) == {"args": {}, "dry_run": True}


@pytest.mark.parametrize(
    ("args", "path"),
    [
        (("status", "bundle"), "/admin/jobs/bundle/status"),
        (("runs", "42"), "/admin/jobs/runs/42"),
        (("list", "bundle"), "/admin/jobs/runs?job=bundle&limit=20"),
        (("list", "bundle", "--limit", "5"), "/admin/jobs/runs?job=bundle&limit=5"),
    ],
)
def test_read_commands(env, args, path) -> None:
    result = _run(env, *args, ANSWER_STATUS="200")
    assert result.returncode == 0, result.stderr
    (call,) = _calls(env)
    assert call["method"] == "GET"
    assert call["url"] == "http://127.0.0.1:8081" + path
    assert call["body"] == ""


@pytest.mark.parametrize("status", ["404", "409", "400", "500", "401"])
def test_a_status_that_is_not_2xx_exits_1(env, status) -> None:
    result = _run(env, "status", "bundle", ANSWER_STATUS=status, ANSWER_BODY='{"error": "no"}')
    assert result.returncode == 1
    assert f"HTTP {status}" in result.stderr
    assert '"error": "no"' in result.stdout


def test_a_body_that_is_not_json_is_printed_as_it_is(env) -> None:
    result = _run(env, "runs", "1", ANSWER_STATUS="502", ANSWER_BODY="Bad Gateway")
    assert result.returncode == 1
    assert "Bad Gateway" in result.stdout


def test_core_that_cannot_be_reached_exits_1(env) -> None:
    result = _run(env, "status", "bundle", CURL_FAIL="1")
    assert result.returncode == 1
    assert "cannot reach core" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("bogus",),
        ("run",),
        ("run", "Bad Name"),
        ("run", "bundle", "--arg"),
        ("run", "bundle", "--arg", "novalue"),
        ("run", "bundle", "--arg", "=x"),
        ("run", "bundle", "--wat"),
        ("status",),
        ("runs", "abc"),
        ("list",),
        ("list", "bundle", "--limit"),
        ("list", "bundle", "--limit", "x"),
        ("help",),
    ],
)
def test_usage_errors_exit_2_and_send_nothing(env, args) -> None:
    result = _run(env, *args)
    assert result.returncode == 2
    assert "scripts/jobs.sh run <job>" in result.stderr
    assert _calls(env) == []


def test_no_token_exits_2_and_sends_nothing(env) -> None:
    env = {k: v for k, v in env.items() if k != "JOSHUA_TOKEN_LAPTOP"}
    result = _run(env, "status", "bundle")
    assert result.returncode == 2
    assert "JOSHUA_TOKEN_LAPTOP" in result.stderr
    assert _calls(env) == []


@pytest.mark.parametrize(
    ("args", "body"),
    [
        (("digest", "plan"), {"args": {"mode": "plan"}, "dry_run": True}),
        (("digest", "run"), {"args": {"mode": "run"}, "dry_run": False}),
        (("digest", "run", "--dry-run"), {"args": {"mode": "run"}, "dry_run": True}),
        (
            ("digest", "undo", "journal/2026/03/08/digest-filter-check.md"),
            {
                "args": {"mode": "undo", "digest": "journal/2026/03/08/digest-filter-check.md"},
                "dry_run": False,
            },
        ),
        (
            ("digest", "undo", "journal/2026/03/08/digest-x.md", "--dry-run"),
            {"args": {"mode": "undo", "digest": "journal/2026/03/08/digest-x.md"}, "dry_run": True},
        ),
    ],
)
def test_digest_posts_the_mode(env, args, body) -> None:
    result = _run(env, *args)
    assert result.returncode == 0, result.stderr
    (call,) = _calls(env)
    assert call["url"] == "http://127.0.0.1:8081/admin/jobs/digest/run"
    assert json.loads(call["body"]) == body


@pytest.mark.parametrize(
    "args",
    [
        ("digest",),
        ("digest", "go"),
        ("digest", "plan", "--dry-run"),
        ("digest", "run", "--wat"),
        ("digest", "undo"),
        ("digest", "undo", "journal/x.md", "--wat"),
    ],
)
def test_digest_usage_errors_exit_2_and_send_nothing(env, args) -> None:
    result = _run(env, *args)
    assert result.returncode == 2
    assert "scripts/jobs.sh digest plan" in result.stderr
    assert _calls(env) == []
