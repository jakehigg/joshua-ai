import sys

import uvicorn
from joshua_shared import config, log

from joshua_assignments.core_client import CoreClient
from joshua_assignments.git_proxy import GitProxy
from joshua_assignments.harden import make_non_dumpable
from joshua_assignments.sdk_runner import SdkRunner
from joshua_assignments.server import build_app
from joshua_assignments.settings import SettingsError, settings_from_env
from joshua_assignments.store import PgStore


def main() -> None:
    logger = log.configure_from_env("joshua-assignments")
    # Before the config, the settings, and the port: a session, as the same
    # user, then cannot read the environment or the memory of this process.
    make_non_dumpable()
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
    if not settings.can_run:
        print(
            "settings error: the runner needs CORE_URL, GATEWAY_URL, and JOSHUA_TOKEN_ASSIGNMENTS",
            file=sys.stderr,
        )
        raise SystemExit(1)
    log.install_healthcheck_filter()
    core = CoreClient(settings)
    app = build_app(
        settings,
        PgStore(settings.database_url),
        core=core,
        runner=lambda manager: SdkRunner(
            manager, core=core, git=GitProxy(manager.store, manager.load_config)
        ),
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
