"""The MCP package store: install once, reuse, reinstall on a change, fail alone.

Every command goes through a fake runner and every download through a fake
downloader, so the whole flow runs with no network and no npm, uv, or git.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from joshua_gateway import mcp_store
from joshua_shared.mcp_package import parse_package


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Point the store at a temp directory and return it."""
    monkeypatch.setenv(mcp_store.STORE_ENV_VAR, str(tmp_path / "store"))
    return tmp_path / "store"


class FakeRunner:
    """Record every command, and make the files a real install would leave."""

    def __init__(self, *, files: dict[str, str] | None = None, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.files = files or {}
        self.fail_on = fail_on

    def __call__(self, argv, cwd, env):
        self.calls.append(argv)
        self.envs.append(env)
        if self.fail_on and argv[0] == self.fail_on:
            raise mcp_store.InstallError(f"{argv[0]} failed: pretend registry error")
        for rel, body in self.files.items():
            # `cwd` is the staging directory for npm and pypi, and the repo
            # directory for git; the paths below are written from the entry root.
            root = cwd.parent / "repo" if cwd.name == "repo" else cwd
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(0o755)

    @property
    def programs(self) -> list[str]:
        return [argv[0] for argv in self.calls]


def npm_request(name="weather", version="1.6.1", **kwargs):
    return mcp_store.InstallRequest(
        name=name, spec=parse_package(f"npm:weather-mcp@{version}"), **kwargs
    )


# -- npm -----------------------------------------------------------------------


def test_an_npm_install_pins_the_version_and_blocks_install_scripts(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    record = mcp_store.install(npm_request(), runner=runner)
    argv = runner.calls[0]
    assert argv[0] == "npm"
    assert "weather-mcp@1.6.1" in argv
    assert "--ignore-scripts" in argv
    assert record["bin_dirs"] == ["node_modules/.bin"]
    assert (store / "weather" / "node_modules/.bin/weather-mcp").is_file()


def test_allow_scripts_lets_a_postinstall_run(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.install(npm_request(allow_scripts=True), runner=runner)
    assert "--ignore-scripts" not in runner.calls[0]


def test_a_registry_override_reaches_npm(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.install(npm_request(registry="https://npm.example/"), runner=runner)
    argv = runner.calls[0]
    assert argv[argv.index("--registry") + 1] == "https://npm.example/"


def test_an_install_command_gets_no_fleet_token(store, monkeypatch):
    """The installer runs a package author's build. It sees no gateway secret."""
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", "top-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://joshua:pw@postgres/joshua")
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.install(npm_request(), runner=runner)
    for env in runner.envs:
        assert "JOSHUA_TOKEN_GATEWAY" not in env
        assert "DATABASE_URL" not in env


# -- pypi, git, and a released binary ------------------------------------------


def test_a_pypi_install_uses_uv_tool_into_the_entry_directory(store):
    request = mcp_store.InstallRequest(name="ha", spec=parse_package("pypi:ha-mcp==7.8.1"))
    runner = FakeRunner(files={"bin/ha-mcp": "#!/bin/sh\n"})
    record = mcp_store.install(request, runner=runner)
    assert runner.calls[0][:3] == ["uv", "tool", "install"]
    assert runner.calls[0][-1] == "ha-mcp==7.8.1"
    assert runner.envs[0]["UV_TOOL_BIN_DIR"].endswith("/bin")
    assert record["bin_dirs"] == ["bin"]
    assert record["resolved"] == "7.8.1"


def test_a_git_install_checks_out_the_commit_and_syncs(store):
    request = mcp_store.InstallRequest(
        name="wikiserver", spec=parse_package("git+https://example.net/a/b@3f2a9c1")
    )
    runner = FakeRunner(files={".venv/bin/python": "#!/bin/sh\n"})
    record = mcp_store.install(request, runner=runner)
    assert runner.programs == ["git", "git", "git", "git", "uv"]
    assert "3f2a9c1" in runner.calls[2]
    assert record["bin_dirs"] == ["repo/.venv/bin"]
    assert (store / "wikiserver" / "repo/.venv/bin/python").is_file()


def test_a_download_must_match_its_hash(store):
    body = b"#!/bin/sh\necho hi\n"
    digest = hashlib.sha256(body).hexdigest()
    spec = parse_package("https://example.net/dl/weather-mcp", digest)
    request = mcp_store.InstallRequest(name="weather-bin", spec=spec)

    def download(url, dest):
        dest.write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    record = mcp_store.install(request, download=download)
    assert record["resolved"] == digest
    assert (store / "weather-bin" / "bin" / "weather-mcp").is_file()


def test_a_download_that_does_not_match_its_hash_installs_nothing(store):
    spec = parse_package("https://example.net/dl/weather-mcp", "b" * 64)
    request = mcp_store.InstallRequest(name="weather-bin", spec=spec)

    def download(url, dest):
        dest.write_bytes(b"something else")
        return "c" * 64

    with pytest.raises(mcp_store.InstallError, match="does not match sha256"):
        mcp_store.install(request, download=download)
    assert not (store / "weather-bin").exists()


# -- the record, and when an install runs again --------------------------------


def test_the_record_round_trips_and_names_the_exact_spec(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    record = mcp_store.install(npm_request(), runner=runner)
    on_disk = json.loads((store / "weather" / mcp_store.RECORD_NAME).read_text())
    assert on_disk == record
    assert on_disk["package"] == "npm:weather-mcp@1.6.1"
    assert on_disk["installed_at"]
    assert mcp_store.read_record("weather") == record


def test_a_second_start_with_the_same_spec_does_not_install_again(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(), runner=runner)
    first = list(runner.calls)
    mcp_store.ensure(npm_request(), runner=runner)
    assert runner.calls == first, "an unchanged spec must not reach the network"


def test_a_changed_version_installs_again(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(version="1.6.1"), runner=runner)
    mcp_store.ensure(npm_request(version="1.7.0"), runner=runner)
    assert len(runner.calls) == 2
    assert mcp_store.read_record("weather")["package"] == "npm:weather-mcp@1.7.0"


def test_reinstall_runs_even_when_the_store_matches(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(), runner=runner)
    mcp_store.ensure(npm_request(), reinstall=True, runner=runner)
    assert len(runner.calls) == 2


def test_a_changed_allow_scripts_or_registry_installs_again(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(), runner=runner)
    mcp_store.ensure(npm_request(allow_scripts=True), runner=runner)
    mcp_store.ensure(npm_request(allow_scripts=True, registry="https://npm.example"), runner=runner)
    assert len(runner.calls) == 3


def test_a_failed_reinstall_leaves_the_working_install_in_place(store):
    """The swap is the last step, so a version bump that cannot install keeps the
    entry serving the version it already has."""
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(version="1.6.1"), runner=runner)

    broken = FakeRunner(fail_on="npm")
    with pytest.raises(mcp_store.InstallError, match="pretend registry error"):
        mcp_store.ensure(npm_request(version="1.7.0"), runner=broken)

    record = mcp_store.read_record("weather")
    assert record["package"] == "npm:weather-mcp@1.6.1"
    assert (store / "weather" / "node_modules/.bin/weather-mcp").is_file()
    assert not list(store.glob(".weather.*")), "the staging directory is cleaned up"


def test_an_emptied_store_directory_installs_again(store):
    """The record alone is not proof: a lost volume must reinstall, not fail."""
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    mcp_store.ensure(npm_request(), runner=runner)
    shutil.rmtree(store / "weather" / "node_modules")
    mcp_store.ensure(npm_request(), runner=runner)
    assert len(runner.calls) == 2


async def test_two_instances_of_one_entry_install_it_once(store):
    """An identity server has one instance per person and one install."""
    import asyncio

    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    await asyncio.gather(*(mcp_store.ensure_async(npm_request(), runner=runner) for _ in range(3)))
    assert len(runner.calls) == 1


# -- resolving the command -----------------------------------------------------


def test_one_executable_needs_no_command(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    record = mcp_store.install(npm_request(), runner=runner)
    resolved = mcp_store.resolve_command("weather", record, None)
    assert resolved == str(store / "weather" / "node_modules/.bin/weather-mcp")


def test_several_executables_pick_the_one_named_like_the_package(store):
    runner = FakeRunner(
        files={
            "node_modules/.bin/weather-mcp": "#!/bin/sh\n",
            "node_modules/.bin/some-helper": "#!/bin/sh\n",
        }
    )
    record = mcp_store.install(npm_request(), runner=runner)
    assert mcp_store.resolve_command("weather", record, None).endswith("weather-mcp")


def test_several_executables_and_no_match_asks_for_a_command(store):
    runner = FakeRunner(
        files={
            "node_modules/.bin/one": "#!/bin/sh\n",
            "node_modules/.bin/two": "#!/bin/sh\n",
        }
    )
    record = mcp_store.install(npm_request(), runner=runner)
    with pytest.raises(mcp_store.InstallError, match="set 'command'"):
        mcp_store.resolve_command("weather", record, None)


def test_no_executable_at_all_asks_for_a_command(store):
    runner = FakeRunner(files={"node_modules/.bin/.keep": ""})
    record = mcp_store.install(npm_request(), runner=runner)
    (store / "weather" / "node_modules/.bin/.keep").chmod(0o644)
    with pytest.raises(mcp_store.InstallError, match="no executable"):
        mcp_store.resolve_command("weather", record, None)


def test_a_named_command_resolves_inside_the_install_first(store):
    """`command: python` on a git package means the project's own interpreter."""
    request = mcp_store.InstallRequest(
        name="wikiserver", spec=parse_package("git+https://example.net/a/b@3f2a9c1")
    )
    runner = FakeRunner(files={".venv/bin/python": "#!/bin/sh\n"})
    record = mcp_store.install(request, runner=runner)
    resolved = mcp_store.resolve_command("wikiserver", record, "python")
    assert resolved == str(store / "wikiserver" / "repo/.venv/bin/python")


def test_a_command_the_install_does_not_carry_stays_on_the_path(store):
    runner = FakeRunner(files={"node_modules/.bin/weather-mcp": "#!/bin/sh\n"})
    record = mcp_store.install(npm_request(), runner=runner)
    assert mcp_store.resolve_command("weather", record, "node") == "node"
    assert mcp_store.resolve_command("weather", record, "/usr/bin/env") == "/usr/bin/env"


# -- the request itself --------------------------------------------------------


def test_a_connect_config_without_a_package_makes_no_request():
    assert mcp_store.InstallRequest.from_connect_cfg("x", {"type": "stdio", "command": "a"}) is None


def test_a_connect_config_carries_the_package_fields():
    request = mcp_store.InstallRequest.from_connect_cfg(
        "weather",
        {
            "type": "stdio",
            "package": "npm:weather-mcp@1.6.1",
            "registry": "https://npm.example",
            "allow_scripts": True,
        },
    )
    assert request.spec.version == "1.6.1"
    assert request.registry == "https://npm.example"
    assert request.allow_scripts is True


def test_the_store_root_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(mcp_store.STORE_ENV_VAR, raising=False)
    assert str(mcp_store.store_root()) == mcp_store.DEFAULT_STORE
    monkeypatch.setenv(mcp_store.STORE_ENV_VAR, str(tmp_path))
    assert mcp_store.entry_dir("weather") == tmp_path / "weather"


def test_a_missing_or_broken_record_reads_as_none(store):
    assert mcp_store.read_record("absent") is None
    (store / "weather").mkdir(parents=True)
    (store / "weather" / mcp_store.RECORD_NAME).write_text("not json")
    assert mcp_store.read_record("weather") is None
    assert mcp_store.is_current(npm_request(), None) is False


def test_a_command_that_is_not_in_the_image_says_so(store, monkeypatch):
    """The real runner, not the fake one: a missing npm must be one clear line."""
    with pytest.raises(mcp_store.InstallError, match="not in the gateway image"):
        mcp_store._run(["joshua-no-such-program"], store, {})


def test_an_install_runs_at_the_path_the_server_runs_from(store):
    """npm and uv write the install path into a symlink and a shebang. An install
    built somewhere else and moved here would point at a path that is gone."""

    class SymlinkRunner:
        def __call__(self, argv, cwd, env):
            real = cwd / "tools" / "weather-mcp"
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text("#!/bin/sh\n")
            real.chmod(0o755)
            link = cwd / "node_modules" / ".bin" / "weather-mcp"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(real)  # absolute, as uv and npm write it

    record = mcp_store.install(npm_request(), runner=SymlinkRunner())
    resolved = mcp_store.resolve_command("weather", record, None)
    assert Path(resolved).is_file(), "the install's own symlink must still resolve"
    assert Path(resolved).resolve() == store / "weather" / "tools" / "weather-mcp"


def test_a_failed_install_keeps_a_symlinked_install_working(store):
    class SymlinkRunner:
        def __init__(self, fail=False):
            self.fail = fail

        def __call__(self, argv, cwd, env):
            if self.fail:
                raise mcp_store.InstallError("npm failed: pretend registry error")
            real = cwd / "tools" / "weather-mcp"
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text("#!/bin/sh\n")
            real.chmod(0o755)
            link = cwd / "node_modules" / ".bin" / "weather-mcp"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(real)

    mcp_store.ensure(npm_request(version="1.6.1"), runner=SymlinkRunner())
    with pytest.raises(mcp_store.InstallError):
        mcp_store.ensure(npm_request(version="1.7.0"), runner=SymlinkRunner(fail=True))

    record = mcp_store.read_record("weather")
    assert record["package"] == "npm:weather-mcp@1.6.1"
    assert Path(mcp_store.resolve_command("weather", record, None)).is_file()
    assert not list(store.glob(".weather.*")), "nothing is left behind"


def test_the_download_caches_sit_beside_the_entries(store):
    """A cache inside an entry would be thrown away on every version bump."""
    runner = FakeRunner(files={"bin/ha-mcp": "#!/bin/sh\n"})
    request = mcp_store.InstallRequest(name="ha", spec=parse_package("pypi:ha-mcp==7.8.1"))
    mcp_store.install(request, runner=runner)
    env = runner.envs[0]
    assert env["UV_CACHE_DIR"] == str(store / ".uv-cache")
    assert env["npm_config_cache"] == str(store / ".npm-cache")
    assert not env["UV_CACHE_DIR"].startswith(str(store / "ha"))
