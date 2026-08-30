import sys

import uvicorn
from joshua_shared import config, log
from joshua_shared.fleet_auth import load_fleet_tokens

from joshua_gateway.main import app


def main() -> None:
    log.configure_from_env("joshua-gateway")
    try:
        config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not load_fleet_tokens():
        print("no JOSHUA_TOKEN_* fleet tokens configured", file=sys.stderr)
        raise SystemExit(1)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
