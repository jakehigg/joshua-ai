"""`scripts/enhancements.sh` and the overlays it turns on.

The script decides which `enhancements/<slot>/<choice>/docker-compose.yml`
overlays join the compose command line. It never touches Docker, so the
tests below run it directly against a temporary yaml file. Each overlay is
checked against the same rules `tests/test_layout.py` checks the base
compose file against: no fleet token, every port on loopback, and no Joshua
image.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enhancements.sh"
OVERLAYS = sorted((ROOT / "enhancements").glob("*/*/docker-compose*.yml"))


def run(yaml_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = [str(SCRIPT)]
    if yaml_path is not None:
        args.append(str(yaml_path))
    return subprocess.run(args, capture_output=True, text=True)


def write(tmp_path: Path, text: str) -> Path:
    yaml_file = tmp_path / "enhancements.yaml"
    yaml_file.write_text(text)
    return yaml_file


def test_no_file_prints_nothing(tmp_path: Path) -> None:
    result = run(tmp_path / "does-not-exist.yaml")
    assert result.returncode == 0
    assert result.stdout == ""


def test_use_none_prints_nothing(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  wiki:\n    use: none\n")
    result = run(yaml_file)
    assert result.returncode == 0
    assert result.stdout == ""


def test_use_otterwiki_prints_its_overlay_flag(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  wiki:\n    use: otterwiki\n")
    result = run(yaml_file)
    assert result.returncode == 0
    assert result.stdout == "-f enhancements/wiki/otterwiki/docker-compose.yml\n"


def test_use_custom_with_no_custom_file_fails(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  wiki:\n    use: custom\n")
    result = run(yaml_file)
    assert result.returncode == 1
    assert "docker-compose.example.yml" in result.stderr


def test_use_an_unknown_choice_fails_naming_it(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  wiki:\n    use: nosuch\n")
    result = run(yaml_file)
    assert result.returncode == 1
    assert "nosuch" in result.stderr
    assert "enhancements/wiki/nosuch/docker-compose.yml" in result.stderr


def test_an_unknown_slot_fails_naming_its_overlay(tmp_path: Path) -> None:
    yaml_file = write(
        tmp_path,
        "enhancements:\n  wiki:\n    use: none\n  foo:\n    use: bar\n",
    )
    result = run(yaml_file)
    assert result.returncode == 1
    assert "enhancements/foo/bar/docker-compose.yml" in result.stderr


def test_an_uppercase_slot_name_fails(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  Wiki:\n    use: otterwiki\n")
    result = run(yaml_file)
    assert result.returncode == 1
    assert "Wiki" in result.stderr


def test_a_slashed_choice_fails(tmp_path: Path) -> None:
    yaml_file = write(tmp_path, "enhancements:\n  wiki:\n    use: other/thing\n")
    result = run(yaml_file)
    assert result.returncode == 1
    assert "other/thing" in result.stderr


def test_comments_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    yaml_file = write(
        tmp_path,
        "# a comment\n"
        "enhancements:\n"
        "  # another comment\n"
        "  wiki:\n"
        "\n"
        "    use: otterwiki  # inline comment\n",
    )
    result = run(yaml_file)
    assert result.returncode == 0
    assert result.stdout == "-f enhancements/wiki/otterwiki/docker-compose.yml\n"


def test_there_are_overlays_to_check() -> None:
    """Guard the guard: a bad glob would make every test below vacuous."""
    assert OVERLAYS, "no enhancements/*/*/docker-compose*.yml found"


@pytest.mark.parametrize(
    "overlay", OVERLAYS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}"
)
def test_an_overlay_service_holds_no_fleet_token_or_sdk_credential(overlay: Path) -> None:
    """An enhancement runs someone else's code. It must never hold a Joshua secret."""
    raw = yaml.safe_load(overlay.read_text())
    for name, body in raw["services"].items():
        environment = body.get("environment") or {}
        for key in environment:
            assert not key.startswith("JOSHUA_TOKEN_"), f"{name} in {overlay} holds {key}"
            assert key != "CLAUDE_CODE_OAUTH_TOKEN", f"{name} in {overlay} holds {key}"
            assert key != "POSTGRES_PASSWORD", f"{name} in {overlay} holds {key}"


@pytest.mark.parametrize(
    "overlay", OVERLAYS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}"
)
def test_every_overlay_port_is_on_the_loopback(overlay: Path) -> None:
    raw = yaml.safe_load(overlay.read_text())
    for name, body in raw["services"].items():
        for mapping in body.get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} in {overlay} publishes {mapping} on every interface"
            )


@pytest.mark.parametrize(
    "overlay", OVERLAYS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}"
)
def test_every_overlay_data_mount_is_the_wiki_subpath(overlay: Path) -> None:
    raw = yaml.safe_load(overlay.read_text())
    for name, body in raw["services"].items():
        for mount in body.get("volumes") or []:
            if isinstance(mount, dict) and mount.get("source") == "data":
                assert mount["volume"]["subpath"] == "wiki", (
                    f"{name} in {overlay} mounts data at a subpath other than wiki"
                )


@pytest.mark.parametrize(
    "overlay", OVERLAYS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}"
)
def test_an_overlay_image_is_not_a_joshua_image(overlay: Path) -> None:
    raw = yaml.safe_load(overlay.read_text())
    for name, body in raw["services"].items():
        image = body.get("image", "")
        assert "ghcr.io/jakehigg" not in image, f"{name} in {overlay} uses a Joshua image"


@pytest.mark.parametrize(
    "overlay", OVERLAYS, ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}"
)
def test_an_overlay_service_runs_as_uid_1000(overlay: Path) -> None:
    raw = yaml.safe_load(overlay.read_text())
    for name, body in raw["services"].items():
        assert body.get("user") == "1000:1000", f"{name} in {overlay} does not set user: 1000:1000"
