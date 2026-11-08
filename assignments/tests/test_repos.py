"""Repository names and the allowlist."""

from __future__ import annotations

import pytest
from joshua_assignments.repos import RepoError, normalize_repo, repo_allowed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gitlab.example.net/infra/gitops", "gitlab.example.net/infra/gitops"),
        ("https://GitLab.example.net/Infra/GitOps.git", "gitlab.example.net/infra/gitops"),
        ("ssh://git@gitlab.example.net:2222/infra/gitops", "gitlab.example.net:2222/infra/gitops"),
        ("git@gitlab.example.net:infra/sub/gitops.git", "gitlab.example.net/infra/sub/gitops"),
        ("https://user:secret@gitlab.example.net/infra/gitops", "gitlab.example.net/infra/gitops"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "gitlab.example.net/infra",
        "ftp://gitlab.example.net/a/b",
        "https://gitlab.example.net:99999/a/b",
        "https://gitlab.example.net:0/a/b",
        "localhost/a/b",
        "gitlab.example.net/a/../b",
        "gitlab.example.net/a/_b",
        "https://gitlab.example.net:x/a/b",
    ],
)
def test_normalize_refuses_and_does_not_repeat_the_input(raw: str) -> None:
    with pytest.raises(RepoError) as exc:
        normalize_repo(raw)
    if raw:
        assert raw not in str(exc.value)


def test_repo_allowed() -> None:
    patterns = ["gitlab.example.net/infra/*", "github.com/example-org/app"]
    assert repo_allowed("gitlab.example.net/infra/gitops", patterns)
    assert repo_allowed("gitlab.example.net/infra/sub/gitops", patterns)
    assert repo_allowed("github.com/example-org/app", patterns)
    assert not repo_allowed("github.com/example-org/other", patterns)
    assert not repo_allowed("gitlab.example.net/infra2/x", patterns)
    assert not repo_allowed("gitlab.example.net/infra/gitops", [])
