import sys

import uvicorn
from joshua_shared import config, log


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "people":
        from joshua_core.people_cli import main as people_main

        raise SystemExit(people_main(argv[1:]))

    if argv and argv[0] == "chat":
        from joshua_core.chat import main as chat_main

        raise SystemExit(chat_main(argv[1:]))

    from joshua_core.main import app

    log.configure_from_env("joshua-core")
    try:
        config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
