import sys

import uvicorn
from joshua_shared import config, log

from joshua_assignments.server import build_app
from joshua_assignments.settings import SettingsError, settings_from_env
from joshua_assignments.store import PgStore


def main() -> None:
    logger = log.configure_from_env("joshua-assignments")
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not cfg.assignments.enabled:
        logger.error(
            {
                "message": "assignments is disabled; set assignments.enabled: true "
                "in joshua.yaml to start it"
            }
        )
        raise SystemExit(1)
    try:
        settings = settings_from_env(cfg)
    except SettingsError as exc:
        print(f"settings error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    log.install_healthcheck_filter()
    app = build_app(settings, PgStore(settings.database_url))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
