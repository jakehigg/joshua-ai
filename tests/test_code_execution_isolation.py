"""The isolation of code execution, in both deployment paths.

The code execution container runs code that the agent writes, so the network is what keeps the
data of a run from leaving: the container reaches nothing, and only the gateway
reaches it. A real fetch from the container needs a built stack and a public
host, which this suite does not have. So each test here asserts the
configuration that a deployment applies: the compose file, and the objects
that helm renders, where helm is installed (the `chart` job of CI).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "joshua"
VALUES = CHART / "ci" / "code-execution-values.yaml"

needs_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm renders the chart; the chart job of CI has it"
)


def compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def render(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["helm", "template", "joshua", str(CHART), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind} {name}"
    return found[0]


# -- compose ------------------------------------------------------------------


def test_the_compose_code_execution_is_on_its_own_closed_network() -> None:
    spec = compose()
    svc = spec["services"]["code-execution"]
    assert svc["networks"] == ["code-execution"]
    assert spec["networks"]["code-execution"] == {"internal": True}
    on_it = sorted(
        name
        for name, svc in spec["services"].items()
        if "code-execution" in (svc.get("networks") or [])
    )
    assert on_it == ["code-execution", "gateway"]


def test_the_compose_code_execution_is_a_profile_with_no_ports_volumes_or_privileges() -> None:
    svc = compose()["services"]["code-execution"]
    assert svc["profiles"] == ["code-execution"]
    assert "ports" not in svc
    assert "volumes" not in svc
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert set(svc["environment"]) == {"JUPYTER_TOKEN"}
    assert "@sha256:" in svc["image"]
    assert "--ServerApp.terminals_enabled=False" in svc["command"]
    # An empty token turns the auth of Jupyter off, so the command refuses it.
    assert 'test -n "$$JUPYTER_TOKEN"' in svc["command"][2]


def test_the_compose_gateway_holds_the_code_execution_token() -> None:
    gateway = compose()["services"]["gateway"]
    assert gateway["environment"]["CODE_EXECUTION_TOKEN"] == "${CODE_EXECUTION_TOKEN:-}"


# -- helm ---------------------------------------------------------------------


@needs_helm
def test_the_chart_renders_no_code_execution_by_default() -> None:
    docs = render("-f", str(CHART / "ci" / "test-values.yaml"))
    assert not [d for d in docs if d["metadata"]["name"] == "joshua-code-execution"]


@needs_helm
def test_the_code_execution_policy_admits_the_gateway_alone_and_allows_no_egress() -> None:
    policy = named(render("-f", str(VALUES)), "NetworkPolicy", "joshua-code-execution")["spec"]
    assert policy["podSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "joshua-code-execution"}
    }
    assert sorted(policy["policyTypes"]) == ["Egress", "Ingress"]
    assert policy["egress"] == []
    assert policy["ingress"] == [
        {
            "from": [
                {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "joshua-gateway"}}}
            ],
            "ports": [{"protocol": "TCP", "port": 8888}],
        }
    ]


@needs_helm
def test_the_code_execution_pod_holds_no_token_volume_or_privilege() -> None:
    docs = render("-f", str(VALUES))
    pod = named(docs, "Deployment", "joshua-code-execution")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert all("emptyDir" in volume for volume in pod["volumes"])
    (container,) = pod["containers"]
    context = container["securityContext"]
    assert context["readOnlyRootFilesystem"] is True
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert [env["name"] for env in container["env"]] == ["JUPYTER_TOKEN"]
    assert 'test -n "$JUPYTER_TOKEN"' in container["command"][2]
    assert container["image"] == compose()["services"]["code-execution"]["image"]
    gateway = named(docs, "Deployment", "joshua-gateway")["spec"]["template"]["spec"]
    env = {e["name"]: e for e in gateway["containers"][0]["env"]}
    assert env["CODE_EXECUTION_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "joshua-code-execution-secrets",
        "key": "JUPYTER_TOKEN",
        "optional": True,
    }
