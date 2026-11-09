"""The git proxy: a clone through it, the refused push, and the token negatives.

A fake git host serves a local bare repository over smart HTTP: it runs
``git upload-pack --stateless-rpc`` for each request. It sits behind an
``httpx.MockTransport``, so no network is used. The real ``git`` binary
clones through the proxy on ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import make_config
from fakes import FakeStore
from joshua_assignments.git_proxy import (
    PUSH_REFUSED,
    GitProxy,
    git_config,
    split_path,
    write_git_config,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")

TOKEN = "glpat-fake-token-0123456789"
REPO = "gitlab.example.net/infra/gitops"
EXTRA = """
  repos:
    - gitlab.example.net/infra/*
    - public.example.org/open/*
  platforms:
    gitlab.example.net:
      kind: gitlab
      token_env: ASSIGNMENTS_GITLAB_TOKEN
    nokey.example.net:
      kind: github
      token_env: ASSIGNMENTS_NOKEY_TOKEN
"""
CONFIG = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
assignments:
  enabled: true
"""


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout


def make_bare(tmp_path: Path, commits: int = 3) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    _git("init", "-q", "-b", "main", cwd=src, env=env)
    for n in range(commits):
        (src / "file.txt").write_text(f"version {n}\n")
        _git("add", "file.txt", cwd=src, env=env)
        _git(
            "-c", "user.name=T", "-c", "user.email=t@example.net",
            "commit", "-q", "-m", f"commit {n}", cwd=src, env=env,
        )  # fmt: skip
    bare = tmp_path / "gitops.git"
    _git("clone", "-q", "--bare", str(src), str(bare), env=env)
    return bare


class FakeGitHost:
    """Smart HTTP for one bare repository, for every request path under ``/infra/gitops.git``."""

    def __init__(self, bare: Path, expect_auth: str | None) -> None:
        self.bare = bare
        self.expect_auth = expect_auth
        self.requests: list[httpx.Request] = []
        self.status: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx.Response(self.status)
        if request.headers.get("authorization") != self.expect_auth:
            return httpx.Response(401)
        path = request.url.path
        if path.endswith("/info/refs"):
            assert request.url.params.get("service") == "git-upload-pack"
            out = subprocess.run(
                ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(self.bare)],
                capture_output=True,
                check=True,
            ).stdout
            line = b"# service=git-upload-pack\n"
            head = f"{len(line) + 4:04x}".encode() + line + b"0000"
            return httpx.Response(
                200,
                content=head + out,
                headers={"content-type": "application/x-git-upload-pack-advertisement"},
            )
        if path.endswith("/git-upload-pack"):
            body = request.content
            if request.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            out = subprocess.run(
                ["git", "upload-pack", "--stateless-rpc", str(self.bare)],
                input=body,
                capture_output=True,
                check=True,
            ).stdout
            return httpx.Response(
                200, content=out, headers={"content-type": "application/x-git-upload-pack-result"}
            )
        return httpx.Response(404)


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


async def running_row(store: FakeStore, repos: list[str]) -> dict[str, Any]:
    row = await store.create(
        person_id="alex",
        conversation_id=None,
        spec={"goal": "g", "repos": repos, "budget": {"five_hour_pct": 70, "seven_day_pct": 85}},
        model="claude-sonnet-5",
    )
    await store.update(row["id"], status="running")
    return row


class Setup:
    def __init__(self, tmp_path: Path, environ: dict[str, str] | None = None) -> None:
        self.tmp = tmp_path
        self.bare = make_bare(tmp_path)
        self.host = FakeGitHost(self.bare, basic("oauth2", TOKEN))
        self.store = FakeStore()
        self.cfg = make_config(EXTRA, text=CONFIG)
        self.proxy = GitProxy(
            self.store,
            lambda: self.cfg,
            environ=environ if environ is not None else {"ASSIGNMENTS_GITLAB_TOKEN": TOKEN},
            transport=httpx.MockTransport(self.host.handler),
        )
        self.home = tmp_path / "home"
        self.home.mkdir()

    async def __aenter__(self) -> Setup:
        await self.proxy.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.proxy.close()

    def session_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def configure(self, assignment_id: int, hosts: tuple[str, ...] = ()) -> str:
        assert self.proxy.port is not None
        secret = self.proxy.grant(assignment_id)
        write_git_config(self.home, git_config(self.proxy.port, secret, hosts))
        return secret

    async def git(self, *args: str, cwd: Path | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=self.session_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.proxy.port}/{path}"


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


async def test_a_clone_through_the_proxy_has_the_full_history(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        secret = s.configure(row["id"])
        dest = tmp_path / "work" / "repos" / "gitops"
        rc, out = await s.git("clone", f"https://{REPO}", str(dest))
        assert rc == 0, out
        assert _git("rev-list", "--count", "HEAD", cwd=dest).strip() == "3"
        assert _git("rev-parse", "--is-shallow-repository", cwd=dest).strip() == "false"
        # The remote is the real URL; git config sends it to the proxy each time.
        assert _git("remote", "get-url", "origin", cwd=dest).strip() == f"https://{REPO}"
        rc, out = await s.git("fetch", "origin", cwd=dest)
        assert rc == 0, out

    # The host got the token, as HTTP Basic, from the manager.
    assert s.host.requests and all(
        r.headers["authorization"] == basic("oauth2", TOKEN) for r in s.host.requests
    )
    assert {r.url.host for r in s.host.requests} == {"gitlab.example.net"}
    # The token is in no file of the session: not in HOME, not in the clone.
    for path in files_under(s.home) + files_under(dest):
        assert TOKEN.encode() not in path.read_bytes(), path
    # The token and the secret are in no log line.
    logged = "\n".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert "git proxy request" in logged
    assert TOKEN not in logged and secret not in logged


async def test_a_push_is_refused_and_never_reaches_the_host(tmp_path) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        s.configure(row["id"])
        dest = tmp_path / "clone"
        rc, out = await s.git("clone", f"https://{REPO}", str(dest))
        assert rc == 0, out
        (dest / "new.txt").write_text("x\n")
        _git("add", "new.txt", cwd=dest)
        _git("-c", "user.name=T", "-c", "user.email=t@example.net", "commit", "-qm", "x", cwd=dest)
        rc, out = await s.git("push", "origin", "HEAD:main", cwd=dest)
        assert rc != 0
        assert "403" in out
        rc, out = await s.git("push", "origin", "HEAD:other", cwd=dest)
        assert rc != 0
    assert not any("receive-pack" in str(r.url) for r in s.host.requests)
    # The bare repository did not change.
    assert _git("rev-list", "--count", "main", cwd=s.bare).strip() == "3"


async def test_an_ssh_url_of_the_host_goes_through_the_proxy(tmp_path) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        s.configure(row["id"], hosts=("gitlab.example.net",))
        rc, out = await s.git(
            "clone", "git@gitlab.example.net:infra/gitops.git", str(tmp_path / "c")
        )
        assert rc == 0, out


async def test_a_repository_outside_the_spec_or_the_allowlist_is_refused(tmp_path) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO, "gitlab.example.net/other/x"])
        s.configure(row["id"])
        # In assignments.repos, not in the spec.
        rc, out = await s.git(
            "clone", "https://gitlab.example.net/infra/secret", str(tmp_path / "a")
        )
        assert rc != 0 and "403" in out
        # In the spec, not in assignments.repos.
        rc, out = await s.git("clone", "https://gitlab.example.net/other/x", str(tmp_path / "b"))
        assert rc != 0 and "403" in out
    assert s.host.requests == []


async def test_a_wrong_or_revoked_secret_and_a_parked_assignment_get_403(tmp_path) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        secret = s.proxy.grant(row["id"])
        refs = s.url(f"{REPO}/info/refs?service=git-upload-pack")
        async with httpx.AsyncClient() as client:
            ok = await client.get(refs, headers={"Authorization": f"Bearer {secret}"})
            assert ok.status_code == 200
            wrong = await client.get(refs, headers={"Authorization": "Bearer nope"})
            missing = await client.get(refs)
            await s.store.update(row["id"], status="waiting")
            parked = await client.get(refs, headers={"Authorization": f"Bearer {secret}"})
            await s.store.update(row["id"], status="running")
            s.proxy.revoke(row["id"])
            revoked = await client.get(refs, headers={"Authorization": f"Bearer {secret}"})
    for reply in (wrong, missing, parked, revoked):
        assert reply.status_code == 403
    assert len(s.host.requests) == 1


async def test_only_the_two_requests_of_a_fetch_are_forwarded(tmp_path) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        auth = {"Authorization": f"Bearer {s.proxy.grant(row['id'])}"}
        async with httpx.AsyncClient() as client:
            receive = await client.get(
                s.url(f"{REPO}/info/refs?service=git-receive-pack"), headers=auth
            )
            receive_post = await client.post(s.url(f"{REPO}/git-receive-pack"), headers=auth)
            dumb = await client.get(s.url(f"{REPO}/HEAD"), headers=auth)
            get_pack = await client.get(s.url(f"{REPO}/git-upload-pack"), headers=auth)
            no_service = await client.get(s.url(f"{REPO}/info/refs"), headers=auth)
            not_repo = await client.get(
                s.url("nohost/info/refs?service=git-upload-pack"), headers=auth
            )
    assert receive.status_code == receive_post.status_code == 403
    assert receive.text == PUSH_REFUSED
    for reply in (dumb, get_pack, no_service, not_repo):
        assert reply.status_code == 403
    assert s.host.requests == []


async def test_a_missing_token_is_refused_and_a_host_with_no_platform_gets_no_credential(
    tmp_path,
) -> None:
    async with Setup(tmp_path, environ={}) as s:
        row = await running_row(s.store, [REPO, "public.example.org/open/x"])
        auth = {"Authorization": f"Bearer {s.proxy.grant(row['id'])}"}
        s.host.expect_auth = None
        async with httpx.AsyncClient() as client:
            missing = await client.get(
                s.url(f"{REPO}/info/refs?service=git-upload-pack"), headers=auth
            )
            public = await client.get(
                s.url("public.example.org/open/x/info/refs?service=git-upload-pack"), headers=auth
            )
    assert missing.status_code == 403
    assert public.status_code == 200
    [request] = s.host.requests
    assert request.url.host == "public.example.org"
    assert "authorization" not in request.headers


@pytest.mark.parametrize(("upstream", "expected"), [(401, 403), (403, 403), (404, 404), (500, 502)])
async def test_a_refusal_of_the_host_never_asks_git_for_a_password(
    tmp_path, upstream, expected
) -> None:
    async with Setup(tmp_path) as s:
        row = await running_row(s.store, [REPO])
        auth = {"Authorization": f"Bearer {s.proxy.grant(row['id'])}"}
        s.host.status = upstream
        async with httpx.AsyncClient() as client:
            reply = await client.get(
                s.url(f"{REPO}/info/refs?service=git-upload-pack"), headers=auth
            )
    assert reply.status_code == expected


async def test_a_host_that_does_not_answer_is_502(tmp_path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with Setup(tmp_path) as s:
        s.proxy.transport = httpx.MockTransport(down)
        await s.proxy.close()
        await s.proxy.start()
        row = await running_row(s.store, [REPO])
        auth = {"Authorization": f"Bearer {s.proxy.grant(row['id'])}"}
        async with httpx.AsyncClient() as client:
            reply = await client.get(
                s.url(f"{REPO}/info/refs?service=git-upload-pack"), headers=auth
            )
    assert reply.status_code == 502


def test_split_path_and_the_git_config() -> None:
    assert split_path("/h.example/a/b.git/info/refs") == ("h.example/a/b.git", "info/refs")
    assert split_path("/h.example/a/b/git-upload-pack") == ("h.example/a/b", "git-upload-pack")
    assert split_path("/h.example/a/b/HEAD") is None
    text = git_config(4321, "s3cret", ["gitlab.example.net"])
    assert '[url "http://127.0.0.1:4321/"]\n\tinsteadOf = https://' in text
    assert "insteadOf = git@gitlab.example.net:" in text
    assert "extraHeader = Authorization: Bearer s3cret" in text
    assert "helper =" in text


def test_the_git_config_file_is_for_its_owner_only(tmp_path) -> None:
    path = write_git_config(tmp_path, "x\n")
    assert path.read_text() == "x\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_new_grant_replaces_the_old_secret() -> None:
    proxy = GitProxy(FakeStore(), lambda: make_config(EXTRA, text=CONFIG), environ={})
    first = proxy.grant(1)
    second = proxy.grant(1)
    assert first != second
    assert proxy.owner(f"Bearer {first}") is None
    assert proxy.owner(f"Bearer {second}") == 1
    assert proxy.owner("Basic x") is None
