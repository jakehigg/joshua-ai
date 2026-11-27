"""The egress of the assignments container, in both deployment paths.

An assignment session holds private data — the wiki, the documents of the
person, each repository it cloned — and it holds a shell. Research on the open
web goes to the `internet` MCP server through the gateway, a clone goes to the
git proxy in its own container, and the network, not the prompt of the session,
is what keeps the shell off that web and off the git hosts.

A session is a process in a container, so a real fetch from it needs a built
image, a cluster, and a public host, and this suite has none of the three. So
each test here asserts the configuration that a deployment applies: the compose
networks, and the NetworkPolicy that helm renders, where helm is installed (the
`chart` job of CI). The two paths that must stay open are tested where they
run: `assignments/tests/test_resume.py` clones a granted repository through a
proxy on another address, `assignments/tests/test_session.py` points a session
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
# One installation with the assignments container, the git proxy, their
# policies, and the viewer.
VALUES = CHART / "ci" / "assignments-values.yaml"
# The two address ranges that values file names: the git host of the proxy, and
# an external database. The git-host range belongs to the proxy alone.
GIT_HOST_CIDR = "192.0.2.10/32"
DATABASE_CIDR = "198.51.100.0/24"
# The port of the git proxy, in the chart and in compose.
PROXY_PORT = 8003

needs_helm = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm renders the chart; the chart job of CI has it"
)


def compose() -> dict[str, Any]:
    """The compose file, with each YAML merge key applied."""
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    """Every object the chart renders for that installation."""
    out = subprocess.run(
        ["helm", "template", "joshua", str(CHART), "-f", str(VALUES)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def policy_named(rendered: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The one NetworkPolicy of a pod."""
    found = [
        doc
        for doc in rendered
        if doc.get("kind") == "NetworkPolicy" and doc["metadata"]["name"] == name
    ]
    assert len(found) == 1, f"the chart renders one policy named {name}"
    return found[0]


@pytest.fixture(scope="module")
def policy(rendered: list[dict[str, Any]]) -> dict[str, Any]:
    """The rendered NetworkPolicy of the assignments pod."""
    return policy_named(rendered, "joshua-assignments")


@pytest.fixture(scope="module")
def proxy_policy(rendered: list[dict[str, Any]]) -> dict[str, Any]:
    """The rendered NetworkPolicy of the git proxy pod."""
    return policy_named(rendered, "joshua-git-proxy")


def destinations(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `to` entry of every egress rule."""
    return [entry for rule in policy["spec"]["egress"] for entry in rule["to"]]


def ranges(policy: dict[str, Any]) -> set[str]:
    """Every address range of every egress rule."""
    return {entry["ipBlock"]["cidr"] for entry in destinations(policy) if "ipBlock" in entry}


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
    # The git proxy is the container that reaches the git hosts, and it is
    # another container: the session does not share its network.
    proxy = raw["services"]["assignments-git-proxy"]
    assert "default" in proxy["networks"] and "closed" in proxy["networks"]
    assert "default" not in raw["services"]["assignments"]["networks"]


@needs_helm
def test_the_rendered_policy_gives_the_pod_no_route_to_a_public_host(
    policy: dict[str, Any],
) -> None:
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"], "an empty list allows all"
    entries = destinations(policy)
    for entry in entries:
        assert set(entry) <= {"ipBlock", "podSelector", "namespaceSelector"}, entry
    # The database of the installation, and nothing else: no git host, and no
    # range that holds the internet.
    assert ranges(policy) == {DATABASE_CIDR}
    # The resolver of the cluster answers, and no other name server.
    resolvers = [entry for entry in entries if "namespaceSelector" in entry]
    assert len(resolvers) == 1 and "podSelector" in resolvers[0]
    assert json.dumps(policy["spec"]["egress"]).count("0.0.0.0/0") == 0


@needs_helm
def test_the_git_host_ranges_are_in_the_policy_of_the_proxy_alone(
    policy: dict[str, Any], proxy_policy: dict[str, Any]
) -> None:
    """The session pod holds no git-host range; the proxy pod holds them all."""
    assert GIT_HOST_CIDR not in ranges(policy)
    assert GIT_HOST_CIDR in ranges(proxy_policy)
    # The proxy speaks https to the host and nothing else.
    assert rule_for(proxy_policy, GIT_HOST_CIDR)["ports"] == [{"protocol": "TCP", "port": 443}]
    # Only the session pod reaches the proxy, and only on its port.
    [ingress] = proxy_policy["spec"]["ingress"]
    assert ingress["ports"] == [{"protocol": "TCP", "port": PROXY_PORT}]
    assert [
        entry["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for entry in ingress["from"]
    ] == ["joshua-assignments"]
    assert proxy_policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert json.dumps(proxy_policy["spec"]["egress"]).count("0.0.0.0/0") == 0


def test_the_session_prompt_sends_research_to_the_internet_server() -> None:
    """`use_internet` is the one way out, so the prompt of a session says so."""
    text = (ROOT / "assignments" / "joshua_assignments" / "prompts" / "assignment.md").read_text()
    assert "Your shell has no route to the open web" in text
    assert "`use_internet`" in text
    for command in ("curl", "wget", "git"):
        assert f"`{command}`" in text


# The git proxy and the model forwarder stay reachable.


def test_the_compose_container_still_reaches_core_the_gateway_and_the_proxy() -> None:
    raw = compose()
    environment = raw["services"]["assignments"]["environment"]
    # The model calls of a session go to the forwarder in core, and its tools
    # to the gateway. Both are on the closed network, so both still answer.
    assert environment["CORE_URL"] == "http://core:8000"
    assert environment["GATEWAY_URL"] == "http://gateway:8000"
    for name in ("postgres", "gateway", "core", "channels", "viewer"):
        assert "closed" in raw["services"][name]["networks"], name
    # A clone goes to the proxy, by the name of its service on that network.
    assert environment["GIT_PROXY_URL"] == f"http://assignments-git-proxy:{PROXY_PORT}"
    proxy = raw["services"]["assignments-git-proxy"]
    assert proxy["profiles"] == ["assignments"], "it starts with the assignments container"
    # The proxy holds the git tokens and the database, and no fleet token.
    assert proxy["env_file"] == [{"path": ".env.assignments", "required": False}]
    assert "postgres:5432" in proxy["environment"]["DATABASE_URL"]
    assert not any(key.startswith("JOSHUA_TOKEN_") for key in proxy["environment"])
    # The git tokens went with it: the session container reads no token file.
    assert "env_file" not in raw["services"]["assignments"]


@needs_helm
def test_the_rendered_policy_keeps_the_forwarder_and_the_database_reachable(
    policy: dict[str, Any],
) -> None:
    # Without core a session fails at its first model call.
    assert pods_on(policy, 8000) == {"joshua-gateway", "joshua-channels", "joshua-core"}
    assert pods_on(policy, 5432) == {"joshua-postgres"}
    assert rule_for(policy, DATABASE_CIDR)["ports"] == [{"protocol": "TCP", "port": 5432}]
    # And a clone reaches the proxy.
    assert pods_on(policy, PROXY_PORT) == {"joshua-git-proxy"}
    ports = [rule["ports"] for rule in policy["spec"]["egress"] if "ports" in rule]
    assert [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}] in ports


@needs_helm
def test_the_rendered_proxy_reaches_the_database_and_the_resolver(
    rendered: list[dict[str, Any]], proxy_policy: dict[str, Any]
) -> None:
    """The proxy reads a grant and a row, and resolves each git host by name."""
    assert pods_on(proxy_policy, 5432) == {"joshua-postgres"}
    assert rule_for(proxy_policy, DATABASE_CIDR)["ports"] == [{"protocol": "TCP", "port": 5432}]
    ports = [rule["ports"] for rule in proxy_policy["spec"]["egress"] if "ports" in rule]
    assert [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}] in ports
    # The session reaches the proxy by the name of its Service.
    [service] = [
        doc
        for doc in rendered
        if doc.get("kind") == "Service" and doc["metadata"]["name"] == "joshua-git-proxy"
    ]
    assert service["spec"]["ports"] == [
        {"name": "http", "port": PROXY_PORT, "targetPort": PROXY_PORT}
    ]
    [deployment] = [
        doc
        for doc in rendered
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "joshua-assignments"
    ]
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    assert {"name": "GIT_PROXY_URL", "value": f"http://joshua-git-proxy:{PROXY_PORT}"} in (
        container["env"]
    )
    # The git token is in the proxy pod and in no other.
    for doc in rendered:
        if doc.get("kind") != "Deployment":
            continue
        proxy = doc["metadata"]["name"] == "joshua-git-proxy"
        for one in doc["spec"]["template"]["spec"]["containers"]:
            names = [entry["name"] for entry in one.get("env", [])]
            holds = "ASSIGNMENTS_GITLAB_TOKEN" in names
            assert holds is proxy, doc["metadata"]["name"]
