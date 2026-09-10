"""The ``mcp`` subcommands of ``python -m joshua_gateway``.

``mcp check`` reads joshua.yaml and says, for each ``package`` entry, whether the
store already holds it. ``mcp install`` installs the entries that the store does
not hold, and prints the install output as it goes. Both run from a shell inside
the container:

    docker compose exec gateway python -m joshua_gateway mcp check
    docker compose exec gateway python -m joshua_gateway mcp install weather

They are a pre-flight, so a person sees an install log before a restart instead
of finding a failure in ``/readyz``.
"""

from __future__ import annotations

import argparse
import sys

from joshua_shared import config

from joshua_gateway import mcp_store
from joshua_gateway.catalog import build_catalog


def _package_entries(names: list[str]) -> dict[str, mcp_store.InstallRequest]:
    """The install request for each ``package`` entry, filtered by ``names``."""
    specs = build_catalog(config.load())
    requests: dict[str, mcp_store.InstallRequest] = {}
    for name, spec in specs.items():
        request = mcp_store.InstallRequest.from_connect_cfg(name, spec.connect_cfg)
        if request is not None:
            requests[name] = request
    if not names:
        return requests
    unknown = [n for n in names if n not in requests]
    if unknown:
        known = ", ".join(sorted(requests)) or "none"
        raise SystemExit(f"no package server named {', '.join(unknown)} (known: {known})")
    return {n: requests[n] for n in names}


def _check(names: list[str]) -> int:
    requests = _package_entries(names)
    if not requests:
        print("no package servers in joshua.yaml")
        return 0
    missing = 0
    print(f"store: {mcp_store.store_root()}")
    for name, request in sorted(requests.items()):
        record = mcp_store.read_record(name)
        if mcp_store.is_current(request, record):
            assert record is not None
            print(f"  {name}: installed  {request.spec.raw}  ({record.get('installed_at')})")
        else:
            missing += 1
            state = "changed" if record else "not installed"
            print(f"  {name}: {state}  {request.spec.raw}")
    if missing:
        print(f"{missing} entr{'y' if missing == 1 else 'ies'} need an install")
    return 1 if missing else 0


def _install(names: list[str], *, reinstall: bool) -> int:
    requests = _package_entries(names)
    if not requests:
        print("no package servers in joshua.yaml")
        return 0
    failed = 0
    for name, request in sorted(requests.items()):
        print(f"==> {name}: {request.spec.raw}")
        try:
            record = mcp_store.ensure(request, reinstall=reinstall)
        except Exception as exc:  # noqa: BLE001 — report and keep going
            failed += 1
            print(f"    failed: {exc}", file=sys.stderr)
            continue
        print(f"    resolved {record.get('resolved')} at {record.get('installed_at')}")
    return 1 if failed else 0


def mcp_command(argv: list[str]) -> int:
    """Run one ``mcp`` subcommand. Returns the process exit code.

    The entrypoint has already configured logging, so an install logs through the
    same JSON handler as the server.
    """
    parser = argparse.ArgumentParser(prog="python -m joshua_gateway mcp")
    sub = parser.add_subparsers(dest="action", required=True)

    check = sub.add_parser("check", help="say which package servers the store holds")
    check.add_argument("server", nargs="*", help="entry names; default every package entry")

    install = sub.add_parser("install", help="install the package servers")
    install.add_argument("server", nargs="*", help="entry names; default every package entry")
    install.add_argument(
        "--reinstall", action="store_true", help="install again even when the store matches"
    )

    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            return _check(args.server)
        return _install(args.server, reinstall=args.reinstall)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
