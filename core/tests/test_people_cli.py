"""Unit tests for the ``python -m joshua_core people`` CLI."""

from __future__ import annotations

from pathlib import Path

from joshua_core import people, people_cli
from joshua_core.store.models import Person
from people_fakes import PeopleFakeRepo


def _run_cli(monkeypatch, repo: PeopleFakeRepo, argv: list[str], data_dir: Path) -> int:
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data_dir))

    async def fake_run(action):  # type: ignore[no-untyped-def]
        return await action(repo)

    monkeypatch.setattr(people_cli, "_run", fake_run)
    return people_cli.main(argv)


def test_parser_add() -> None:
    args = people_cli._parser().parse_args(
        ["add", "--id", "sam", "--name", "Sam", "--handle", "telegram:1", "--role", "guest"]
    )
    assert (args.command, args.id, args.name, args.handle, args.role) == (
        "add",
        "sam",
        "Sam",
        "telegram:1",
        "guest",
    )


def test_cli_add(monkeypatch, tmp_path, capsys) -> None:
    repo = PeopleFakeRepo()
    rc = _run_cli(
        monkeypatch,
        repo,
        ["add", "--id", "sam", "--name", "Sam", "--handle", "telegram:1"],
        tmp_path,
    )
    assert rc == 0
    assert repo.people["sam"].display_name == "Sam"
    assert "added sam" in capsys.readouterr().out


def test_cli_add_conflict_returns_1(monkeypatch, tmp_path, capsys) -> None:
    repo = PeopleFakeRepo([Person(id="alex", display_name="Alex", role="member")])
    repo.handles[("telegram", "1")] = "alex"
    rc = _run_cli(
        monkeypatch,
        repo,
        ["add", "--id", "sam", "--name", "Sam", "--handle", "telegram:1"],
        tmp_path,
    )
    assert rc == 1
    assert "already belongs" in capsys.readouterr().err


def test_cli_list(monkeypatch, tmp_path, capsys) -> None:
    repo = PeopleFakeRepo([Person(id="alex", display_name="Alex", role="member")])
    repo.handles[("telegram", "9")] = "alex"
    rc = _run_cli(monkeypatch, repo, ["list"], tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "alex" in out and "telegram:9" in out


def test_cli_remove(monkeypatch, tmp_path, capsys) -> None:
    repo = PeopleFakeRepo([Person(id="alex", display_name="Alex", role="member")])
    rc = _run_cli(monkeypatch, repo, ["remove", "--id", "alex"], tmp_path)
    assert rc == 0
    assert repo.people["alex"].role == "removed"
    assert "removed alex" in capsys.readouterr().out


def test_cli_remove_missing_returns_1(monkeypatch, tmp_path, capsys) -> None:
    repo = PeopleFakeRepo()
    rc = _run_cli(monkeypatch, repo, ["remove", "--id", "ghost"], tmp_path)
    assert rc == 1
    assert "no such person" in capsys.readouterr().err


async def test_run_without_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def action(_repo):  # pragma: no cover — never reached
        return 0

    assert await people_cli._run(action) == 1


def test_peopleerror_is_from_people_module() -> None:
    # The CLI catches people.PeopleError; keep the reference stable.
    assert issubclass(people.PeopleError, Exception)
