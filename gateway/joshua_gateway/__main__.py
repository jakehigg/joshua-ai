import sys

import uvicorn
from joshua_shared import config, log
from joshua_shared.fleet_auth import load_fleet_tokens

from joshua_gateway.cli import mcp_command
from joshua_gateway.main import app


def serve() -> None:
    try:
        config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not load_fleet_tokens():
        print("no JOSHUA_TOKEN_* fleet tokens configured", file=sys.stderr)
        raise SystemExit(1)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


def main(argv: list[str] | None = None) -> None:
    """Run the gateway, or one ``mcp`` subcommand.

    With no argument the process serves, which is what the image entrypoint does.
    ``mcp check`` and ``mcp install`` are for a person at a shell:
    ``docker compose exec gateway python -m joshua_gateway mcp check``.
    """
    args = sys.argv[1:] if argv is None else argv
    log.configure_from_env("joshua-gateway")
    if args and args[0] == "mcp":
        raise SystemExit(mcp_command(args[1:]))
    if args:
        print(f"unknown command: {args[0]} (try 'mcp check')", file=sys.stderr)
        raise SystemExit(2)
    serve()


if __name__ == "__main__":
    main()
