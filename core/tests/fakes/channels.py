"""A fake channels app for the core contract tests.

It implements the two routes channels exposes to core: ``POST /v1/deliver`` and
``GET /v1/channels/resolve``. It records deliveries and answers resolve from a
small ``ref → (channel_id, chat_kind, title)`` map, so a core test pins the
deliver and resolve contract without the real channels container.

The routes use the shared contract models and the fleet auth, so the fake holds
the core → channels contract to the same shape as the real channels app.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from joshua_shared.contracts import Chat, DeliverRequest, ResolveResponse
from joshua_shared.fleet_auth import require_identity


@dataclass
class FakeChannels:
    """A stand-in channels container that records deliveries and resolves refs.

    ``resolvable`` maps a destination ref to ``(channel_id, chat_kind, title)``.
    ``delivered`` holds every ``DeliverRequest`` the fake accepted.
    """

    resolvable: dict[str, tuple[str, str, str | None]] = field(default_factory=dict)
    delivered: list[DeliverRequest] = field(default_factory=list)

    def build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/deliver")
        async def deliver(  # type: ignore[no-untyped-def]
            request: DeliverRequest,
            _identity: str = Depends(require_identity(["core"])),
        ):
            self.delivered.append(request)
            return JSONResponse({"delivered": True, "parts": 1})

        @app.get("/v1/channels/resolve")
        async def resolve(  # type: ignore[no-untyped-def]
            ref: str,
            _identity: str = Depends(require_identity(["core"])),
        ):
            entry = self.resolvable.get(ref)
            if entry is None:
                return JSONResponse({"reason": "unknown_channel"}, status_code=404)
            channel_id, chat_kind, title = entry
            chat_id = channel_id.split(":", 1)[1] if ":" in channel_id else channel_id
            return JSONResponse(
                ResolveResponse(
                    channel=channel_id,
                    chat=Chat(kind=chat_kind, id=chat_id, title=title),
                    channel_id=channel_id,
                    chat_kind=chat_kind,
                ).model_dump()
            )

        return app
