"""`scripts/restore.sh` against a fake docker.

The script talks to the world through `docker` and `curl`. Both are replaced
with shell stubs on PATH, so a whole restore runs offline in a temp directory
and each decision the script makes is observable. The stub records every
command it was given, and answers the few the script reads output from.

Two faults are pinned here:

- On a checkout with no image the script died on `docker inspect ""`, because
  `compose create` was allowed to fail quietly. Its own guard for the case sat
  below the line that killed it, so it never ran.
- A rehearsal restore into a second stack kept the live messaging token, and
  the second stack then took real messages from the live one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "restore.sh"

LIVE_VOLUME = "joshua_data"
REHEARSAL_VOLUME = "rehearsal_data"

# The stub docker. `HAS_IMAGE` says whether `compose create` can make a
# container, which is what a checkout with no image cannot do. `VOLUME` is what
# `docker inspect` reports for the /data mount.
DOCKER_STUB = """#!/usr/bin/env bash
echo "$@" >> "$CALL_LOG"
case "$1 $2" in
  "compose ps")
    if [ -f "$STATE/created" ]; then echo "corecontainer"; fi
    exit 0 ;;
  "compose create")
    if [ "$HAS_IMAGE" = "1" ] || [ -f "$STATE/pulled" ]; then
      touch "$STATE/created"; exit 0
    fi
    echo "no such image" >&2; exit 1 ;;
  "compose pull")
    if [ "$PULL_WORKS" = "1" ]; then touch "$STATE/pulled"; exit 0; fi
    echo "pull failed" >&2; exit 1 ;;
esac
case "$1" in
  inspect)
    if [ -f "$STATE/pulled" ] || [ "$HAS_IMAGE" = "1" ]; then echo "$VOLUME"; fi
    exit 0 ;;
  run) exit 0 ;;
esac
# `compose exec ... psql -tAc` asks how many tables there are. An empty
# database is the normal restore target.
case "$*" in
  *information_schema.tables*) echo "0"; exit 0 ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
echo "$@" >> "$CALL_LOG"
exit 0
"""


@pytest.fixture
def stack(tmp_path: Path):
    """A checkout-shaped temp directory, plus stub docker and curl on PATH."""
    work = tmp_path / "joshua"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, work / "scripts" / "restore.sh")
    (work / "scripts" / "restore.sh").chmod(0o755)
    (work / "joshua.yaml").write_text("name: Test\n")
    (work / "docker-compose.yml").write_text("services: {}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    state = tmp_path / "state"
    state.mkdir()

    class Stack:
        root = work
        calls = tmp_path / "calls.log"

        def env_file(self, *, telegram_token: str = "") -> None:
            lines = ["JOSHUA_TOKEN_LAPTOP=laptop-token"]
            if telegram_token:
                lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token}")
            (work / ".env").write_text("\n".join(lines) + "\n")

        def backup(self, *, data_volume: str | None = LIVE_VOLUME) -> Path:
            backup = tmp_path / "backup"
            backup.mkdir(exist_ok=True)
            (backup / "db.dump").write_bytes(b"dump")
            (backup / "data.tar.gz").write_bytes(b"tar")
            if data_volume is not None:
                (backup / "manifest.json").write_text(
                    json.dumps({"created_at": "now", "data_volume": data_volume})
                )
            return backup

        def run(
            self,
            *args: str,
            has_image: str = "1",
            pull_works: str = "1",
            volume: str = LIVE_VOLUME,
        ) -> subprocess.CompletedProcess[str]:
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CALL_LOG": str(self.calls),
                "STATE": str(state),
                "HAS_IMAGE": has_image,
                "PULL_WORKS": pull_works,
                "VOLUME": volume,
            }
            return subprocess.run(
                ["bash", str(work / "scripts" / "restore.sh"), *args],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
            )

        def called(self, needle: str) -> bool:
            return self.calls.is_file() and needle in self.calls.read_text()

    return Stack()


# -- a fresh checkout ----------------------------------------------------------


def test_a_checkout_with_no_image_fails_with_a_joshua_message(stack) -> None:
    """`docker inspect ""` was the message an operator saw. It said nothing."""
    stack.env_file()
    result = stack.run(str(stack.backup()), has_image="0", pull_works="0")
    assert result.returncode != 0
    assert "restore:" in result.stderr
    assert "invalid container name" not in result.stderr


def test_the_message_says_what_to_do(stack) -> None:
    stack.env_file()
    result = stack.run(str(stack.backup()), has_image="0", pull_works="0")
    assert "make pull" in result.stderr


def test_a_checkout_with_no_container_yet_pulls_and_finds_the_volume(stack) -> None:
    """A stack that has never started is the normal case for a new machine."""
    stack.env_file()
    result = stack.run(str(stack.backup()), has_image="0", pull_works="1")
    assert result.returncode == 0, result.stderr
    assert stack.called("compose pull core")
    assert LIVE_VOLUME in result.stdout


def test_nothing_is_dropped_before_the_volume_is_found(stack) -> None:
    """A restore that cannot go on must leave the database whole."""
    stack.env_file()
    stack.run(str(stack.backup()), "--force", has_image="0", pull_works="0")
    assert not stack.called("drop schema")
    assert not stack.called("pg_restore")


def test_a_running_stack_needs_no_pull(stack) -> None:
    stack.env_file()
    result = stack.run(str(stack.backup()), has_image="1")
    assert result.returncode == 0, result.stderr
    assert not stack.called("compose pull core")


# -- a rehearsal restore -------------------------------------------------------


def test_a_rehearsal_with_a_live_token_is_refused(stack) -> None:
    """Telegram gives each message to whichever poller asks first."""
    stack.env_file(telegram_token="123:live")
    result = stack.run(str(stack.backup()), volume=REHEARSAL_VOLUME)
    assert result.returncode != 0
    assert "TELEGRAM_BOT_TOKEN" in result.stderr


def test_the_refusal_names_both_stacks(stack) -> None:
    stack.env_file(telegram_token="123:live")
    result = stack.run(str(stack.backup()), volume=REHEARSAL_VOLUME)
    assert LIVE_VOLUME in result.stderr
    assert REHEARSAL_VOLUME in result.stderr


def test_the_refusal_holds_no_token(stack) -> None:
    stack.env_file(telegram_token="123:live")
    result = stack.run(str(stack.backup()), volume=REHEARSAL_VOLUME)
    assert "123:live" not in result.stderr + result.stdout


def test_a_refused_rehearsal_changes_nothing(stack) -> None:
    stack.env_file(telegram_token="123:live")
    stack.run(str(stack.backup()), "--force", volume=REHEARSAL_VOLUME)
    assert not stack.called("drop schema")
    assert not stack.called("pg_restore")


def test_the_flag_lets_a_rehearsal_through(stack) -> None:
    stack.env_file(telegram_token="123:live")
    result = stack.run(str(stack.backup()), "--keep-token", volume=REHEARSAL_VOLUME)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stdout


def test_a_rehearsal_with_a_blank_token_is_the_wanted_way(stack) -> None:
    stack.env_file(telegram_token="")
    result = stack.run(str(stack.backup()), volume=REHEARSAL_VOLUME)
    assert result.returncode == 0, result.stderr


def test_the_real_recovery_case_still_works(stack) -> None:
    """The original stack with the original token is what a real restore is."""
    stack.env_file(telegram_token="123:live")
    result = stack.run(str(stack.backup()), volume=LIVE_VOLUME)
    assert result.returncode == 0, result.stderr
    assert stack.called("pg_restore")


def test_a_backup_with_no_manifest_still_restores(stack) -> None:
    """The two-file form names no original stack, so there is nothing to compare."""
    stack.env_file(telegram_token="123:live")
    backup = stack.backup(data_volume=None)
    result = stack.run(str(backup / "db.dump"), str(backup / "data.tar.gz"))
    assert result.returncode == 0, result.stderr
