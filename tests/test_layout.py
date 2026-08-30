"""Structure guards for the monorepo skeleton."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMBERS = {
    "shared": "joshua_shared",
    "channels": "joshua_channels",
    "core": "joshua_core",
    "gateway": "joshua_gateway",
}
CONTAINERS = ["channels", "core", "gateway"]


def test_members_exist() -> None:
    for member, package in MEMBERS.items():
        assert (ROOT / member / "pyproject.toml").is_file()
        assert (ROOT / member / package / "__init__.py").is_file()


def test_containers_do_not_import_each_other() -> None:
    for member in CONTAINERS:
        others = [f"joshua_{other}" for other in CONTAINERS if other != member]
        for source in (ROOT / member).rglob("*.py"):
            text = source.read_text()
            for other in others:
                assert other not in text, f"{source} references {other}"


def _compose_services() -> dict:
    """Return the compose services, with each YAML merge key applied."""
    import yaml

    raw = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = {}
    for name, body in raw["services"].items():
        environment = {}
        merged = body.get("environment", {})
        if isinstance(merged, dict):
            for key, value in merged.items():
                if key == "<<":
                    for block in value if isinstance(value, list) else [value]:
                        environment.update(block)
                else:
                    environment[key] = value
        services[name] = environment
    return services


def test_every_app_service_can_expand_the_example_config() -> None:
    """All three apps load the same joshua.yaml, so all three need each variable.

    A variable that only one service holds makes the other two fail the config
    load and crash-loop.
    """
    import re

    text = (ROOT / "joshua.example.yaml").read_text()
    # Only a reference with no ``:-`` default has to be present. One that carries
    # a default expands to the default, so it cannot fail the load.
    referenced = {match.group(1) for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)}
    services = _compose_services()
    for service in CONTAINERS:
        missing = referenced - set(services[service])
        assert not missing, f"{service} cannot expand {sorted(missing)}"


def test_named_volume_mount_points_are_owned_in_the_image() -> None:
    """A named volume takes its ownership from the image path it mounts over.

    The apps run as uid 1000. A mount point the image does not create comes up
    root-owned, and every write to it fails. Each service that mounts a named
    volume must create and chown that path in its Dockerfile.
    """
    import re

    import yaml

    raw = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    # docker-compose.yml names released images. The Dockerfile of a service is
    # in the developer override, which is the file that builds them.
    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text())
    volumes = set(raw.get("volumes") or {})
    for service in CONTAINERS:
        body = raw["services"][service]
        build = dev["services"][service]["build"]
        dockerfile = (ROOT / build["dockerfile"]).read_text()
        for mount in body.get("volumes") or []:
            source, _, target = str(mount).partition(":")
            target = target.split(":")[0]
            if source not in volumes:
                continue  # a bind mount takes its ownership from the host
            owned = re.search(
                rf"mkdir -p {re.escape(target)}\b.*chown 1000:1000 {re.escape(target)}\b",
                dockerfile,
            )
            assert owned, f"{service}: {build['dockerfile']} does not own {target}"


def test_every_entrypoint_configures_logging() -> None:
    """Each container must install the JSON log handler before it serves.

    Without it the root logger keeps its default level, so INFO records vanish
    and the formatter that redacts credentials never runs.
    """
    import re

    for member, package in MEMBERS.items():
        if member == "shared":
            continue
        source = (ROOT / member / package / "__main__.py").read_text()
        assert "log.configure_from_env(" in source, f"{member} does not configure logging"
        assert re.search(r"uvicorn\.run\([^)]*log_config=None", source, re.S), (
            f"{member} lets uvicorn install its own handlers, so the format is mixed"
        )
