"""The chart runs the assignments pod with no root and no capability, and the session as uid 1001.

The test renders the chart with ``charts/joshua/ci/assignments-values.yaml``.
It needs ``helm`` on PATH, and is skipped without it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "joshua"
SOCKET = "/run/joshua-session/launcher.sock"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="needs helm to render the chart (#75)"
)


def _pod() -> dict:
    out = subprocess.run(
        [
            "helm",
            "template",
            "joshua",
            str(CHART),
            "-f",
            str(CHART / "ci" / "assignments-values.yaml"),
            "-s",
            "templates/assignments.deployment.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Deployment"]
    assert len(docs) == 1
    return docs[0]["spec"]["template"]["spec"]


def test_the_manager_and_the_session_run_as_two_users_with_no_privilege() -> None:
    pod = _pod()
    assert pod["securityContext"]["fsGroup"] == 1001
    assert pod.get("shareProcessNamespace") is not True
    containers = {c["name"]: c for c in pod["containers"]}
    assert set(containers) == {"assignments", "session"}
    for name, uid in (("assignments", 1000), ("session", 1001)):
        sc = containers[name]["securityContext"]
        assert sc["runAsUser"] == uid and sc["runAsGroup"] == uid
        assert sc["runAsNonRoot"] is True
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["capabilities"] == {"drop": ["ALL"]}


def test_the_session_container_gets_no_secret_and_shares_the_socket_and_work() -> None:
    containers = {c["name"]: c for c in _pod()["containers"]}
    session = containers["session"]
    assert session["command"] == ["python", "-m", "joshua_assignments.launcher"]
    assert session["env"] == [{"name": "ASSIGNMENTS_SESSION_SOCKET", "value": SOCKET}]
    assert "envFrom" not in session
    manager_env = {e["name"]: e for e in containers["assignments"]["env"]}
    assert manager_env["ASSIGNMENTS_SESSION_SOCKET"]["value"] == SOCKET
    for name in ("assignments", "session"):
        mounts = {m["name"]: m["mountPath"] for m in containers[name]["volumeMounts"]}
        assert mounts["work"] == "/work"
        assert mounts["session-socket"] == "/run/joshua-session"
    session_mounts = {m["name"] for m in session["volumeMounts"]}
    assert session_mounts == {"work", "session-socket"}, "no /data and no config"
