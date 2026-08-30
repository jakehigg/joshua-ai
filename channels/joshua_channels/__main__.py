import sys

import uvicorn
from joshua_shared import config, log

from joshua_channels.app import app


def main() -> None:
    log.configure_from_env("joshua-channels")
    try:
        config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
