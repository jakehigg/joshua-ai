"""A fake core app for the channels contract tests.

It implements the two inbound routes core exposes to channels: ``POST /v1/turns``
and ``POST /v1/turns/stream``. It records every ``TurnEvent`` it receives, and it
can call back ``POST /v1/deliver`` on the channels app under test. A test drives
the whole channels → core → deliver round trip in one process.

The routes use the shared contract models and the fleet auth, so the fake holds
the channels → core contract to the same shape as the real core.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.contracts import DeliverRequest, TurnEvent
from joshua_shared.fleet_auth import require_identity
from joshua_shared.http import FleetClient

Reply = Callable[[TurnEvent], str] | str | None


class FakeCore:
    """A stand-in core that records turns and optionally delivers a reply.

    ``reply`` is the text (or a function of the event) core sends back through
    ``/v1/deliver``. ``deliver_client`` is a ``FleetClient`` bound to the channels
    app under test; without it the fake only records turns.
    """

    def __init__(self, *, reply: Reply = None, deliver_client: FleetClient | None = None) -> None:
        self.events: list[TurnEvent] = []
        self._reply = reply
        self._deliver = deliver_client
        self.app = self._build_app()

    def _reply_text(self, event: TurnEvent) -> str | None:
        if self._reply is None:
            return None
        return self._reply(event) if callable(self._reply) else self._reply

    async def _deliver_reply(self, event: TurnEvent) -> None:
        reply = self._reply_text(event)
        if reply is None or self._deliver is None:
            return
        body = DeliverRequest(channel=event.channel, text=reply).model_dump(mode="json")
        await self._deliver.post_json("/v1/deliver", json=body)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/turns")
        async def turns(  # type: ignore[no-untyped-def]
            event: TurnEvent,
            _identity: str = Depends(require_identity(["channels"])),
        ):
            self.events.append(event)
            await self._deliver_reply(event)
            return JSONResponse({"accepted": True, "turn_id": "t-fake"}, status_code=202)

        @app.post("/v1/turns/stream")
        async def turns_stream(  # type: ignore[no-untyped-def]
            event: TurnEvent,
            identity: str = Depends(require_identity(["channels", "laptop"])),
        ):
            if identity == "laptop" and (event.handle is None or event.handle.type != "cli"):
                return JSONResponse({"reason": "forbidden"}, status_code=403)
            self.events.append(event)
            reply = self._reply_text(event) or ""
            return StreamingResponse(_sse(reply), media_type="text/event-stream")

        return app


async def _sse(reply: str):  # type: ignore[no-untyped-def]
    """Stream one delta per word, then a ``done`` frame with the full text."""
    for word in reply.split():
        yield f"event: delta\ndata: {json.dumps({'text': word + ' '})}\n\n"
    yield f"event: done\ndata: {json.dumps({'turn_id': 't-fake', 'text': reply})}\n\n"
