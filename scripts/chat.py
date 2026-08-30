#!/usr/bin/env python3
"""Talk to Joshua from the host, against the published channels port.

The client lives in ``joshua_core.chat``. This wrapper runs it from a checkout,
where channels is on the host port that compose publishes. Inside the container
use ``python -m joshua_core chat`` or ``make chat AS=<person>``.

    JOSHUA_TOKEN_LAPTOP=<token> python scripts/chat.py --as alex "what is for dinner"
"""

from __future__ import annotations

import os
import sys

from joshua_core.chat import main

HOST_URL = "http://127.0.0.1:8080"

if __name__ == "__main__":
    os.environ.setdefault("JOSHUA_CHANNELS_URL", os.environ.get("CHANNELS_URL") or HOST_URL)
    raise SystemExit(main(sys.argv[1:]))
