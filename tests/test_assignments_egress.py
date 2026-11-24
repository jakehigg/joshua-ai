"""The egress of the assignments container, in both deployment paths.

An assignment session holds private data — the wiki, the documents of the
person, each repository it cloned — and it holds a shell. Research on the open
web goes to the `internet` MCP server through the gateway, and the network, not
the prompt of the session, is what keeps the shell off that web.

A session is a process in a container, so a real fetch from it needs a built
image, a cluster, and a public host, and this suite has none of the three. So
each test here asserts the configuration that a deployment applies: the compose
networks, and the NetworkPolicy that helm renders, where helm is installed (the
`chart` job of CI). The two paths that must stay open are tested where they
run: `assignments/tests/test_git_proxy.py` clones a granted repository through
the proxy on `127.0.0.1`, `assignments/tests/test_session.py` points a session
at the model forwarder of core, and `assignments/tests/test_prompts.py` holds
the research rule of the prompt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "joshua"
# One installation with the assignments container, its policy, and the viewer.
VALUES = CHART / "ci" / "assignments-values.yaml"
# The two address ranges that values file names: the git host of the proxy, and
# an external database. No other range may be in the rendered policy.
GIT_HOST_CIDR = "192.0.2.10/32"
DATABASE_CIDR = "198.51.100.0/24"

needs_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm renders the chart; the chart job of CI has it"
)


def compose() -> dict[str, Any]:
    """The compose file, with each YAML merge key applied."""
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    """The rendered NetworkPolicy of the assignments pod."""
    out = subprocess.run(
        ["helm", "template", "joshua", str(CHART), "-f", str(VALUES)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = [
        doc
        for doc in yaml.safe_load_all(out)
        if doc
        and doc.get("kind") == "NetworkPolicy"
        and doc["metadata"]["name"] == "joshua-assignments"
    ]
    assert len(found) == 1, "the chart renders one policy for the assignments pod"
    return found[0]


def destinations(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `to` entry of every egress rule."""
    return [entry for rule in policy["spec"]["egress"] for entry in rule["to"]]


def rule_for(policy: dict[str, Any], cidr: str) -> dict[str, Any]:
    """The one egress rule that allows an address range."""
    rules = [
        rule
        for rule in policy["spec"]["egress"]
        if any(entry.get("ipBlock", {}).get("cidr") == cidr for entry in rule["to"])
    ]
    assert len(rules) == 1, f"one rule allows {cidr}"
    return rules[0]


def pods_on(policy: dict[str, Any], port: int) -> set[str]:
    """The names of the pods the policy allows on a port."""
    names: set[str] = set()
    for rule in policy["spec"]["egress"]:
        if {"protocol": "TCP", "port": port} not in rule.get("ports", []):
            continue
        for entry in rule["to"]:
            if "podSelector" in entry and "namespaceSelector" not in entry:
                names.add(entry["podSelector"]["matchLabels"]["app.kubernetes.io/name"])
    return names


# A shell in a session reaches no public host.


def test_the_compose_container_has_no_route_off_the_host() -> None:
    raw = compose()
    assert raw["services"]["assignments"]["networks"] == ["closed"]
    # `internal: true` gives the network no gateway, so nothing on it alone
    # resolves or reaches a host beyond the stack.
    assert raw["networks"]["closed"] == {"internal": True}
    assert raw["networks"]["default"] is None


@needs_helm
def test_the_rendered_policy_gives_the_pod_no_route_to_a_public_host(
    policy: dict[str, Any],
) -> None:
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"], "an empty list allows all"
    entries = destinations(policy)
    for entry in entries:
        assert set(entry) <= {"ipBlock", "podSelector", "namespaceSelector"}, entry
    ranges = {entry["ipBlock"]["cidr"] for entry in entries if "ipBlock" in entry}
    # The database of the installation, and the git host of the proxy. Nothing
    # else, and no range that holds the internet.
    assert ranges == {DATABASE_CIDR, GIT_HOST_CIDR}
    # The git-host range belongs to the git proxy of the manager, which speaks
    # https to the host and nothing else.
    assert rule_for(policy, GIT_HOST_CIDR)["ports"] == [{"protocol": "TCP", "port": 443}]
    # The resolver of the cluster answers, and no other name server.
    resolvers = [entry for entry in entries if "namespaceSelector" in entry]
    assert len(resolvers) == 1 and "podSelector" in resolvers[0]
    assert json.dumps(policy["spec"]["egress"]).count("0.0.0.0/0") == 0


def test_the_session_prompt_sends_research_to_the_internet_server() -> None:
    """`use_internet` is the one way out, so the prompt of a session says so."""
    text = (ROOT / "assignments" / "joshua_assignments" / "prompts" / "assignment.md").read_text()
    assert "Your shell has no route to the open web" in text
    assert "`use_internet`" in text
    for command in ("curl", "wget", "git"):
        assert f"`{command}`" in text


# The git proxy and the model forwarder stay reachable.


def test_the_compose_container_still_reaches_core_and_the_gateway() -> None:
    raw = compose()
    environment = raw["services"]["assignments"]["environment"]
    # The model calls of a session go to the forwarder in core, and its tools
    # to the gateway. Both are on the closed network, so both still answer.
    assert environment["CORE_URL"] == "http://core:8000"
    assert environment["GATEWAY_URL"] == "http://gateway:8000"
    for name in ("postgres", "gateway", "core", "channels", "viewer"):
        assert "closed" in raw["services"][name]["networks"], name
    # The git proxy of the manager listens on 127.0.0.1 in this container, so a
    # clone needs no network at all.
    assert "network_mode" not in raw["services"]["assignments"]


@needs_helm
def test_the_rendered_policy_keeps_the_forwarder_and_the_database_reachable(
    policy: dict[str, Any],
) -> None:
    # Without core a session fails at its first model call.
    assert pods_on(policy, 8000) == {"joshua-gateway", "joshua-channels", "joshua-core"}
    assert pods_on(policy, 5432) == {"joshua-postgres"}
    assert rule_for(policy, DATABASE_CIDR)["ports"] == [{"protocol": "TCP", "port": 5432}]
    # The proxy resolves the git host by name before it fetches.
    ports = [rule["ports"] for rule in policy["spec"]["egress"] if "ports" in rule]
    assert [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}] in ports
