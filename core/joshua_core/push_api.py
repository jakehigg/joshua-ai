"""``/v1/push``: the push subscription of a browser, and a test notification.

The viewer proxies these four routes to the person who signed in, under
``/api/v1/push/`` (the push module of its own API), and names them with the
``person`` query parameter, as it does for the workspaces. The caller check is
the one of the ``/auth/*`` routes (``AUTH_CALLERS``, default ``viewer``): no
request body ever names the person.

- ``GET /v1/push/key`` — the public VAPID key a browser subscribes with.
- ``POST /v1/push/subscribe`` — ``{endpoint, keys: {p256dh, auth},
  user_agent?}``: store the subscription of one browser, or update the one that
  endpoint already has.
- ``DELETE /v1/push/subscribe`` — ``{endpoint}``: drop one subscription of that
  person. It is theirs alone, so the endpoint of another person is not found.
- ``POST /v1/push/test`` — send that person a notification now, and answer what
  each of their browsers did with it. A send that never reached a browser,
  because the VAPID header did not build, is a 400 whose ``error`` is the
  message of that failure, so the page says what is wrong with the settings.

**Every route is 503 while push is not configured**: ``push.enabled`` false, a
VAPID key missing, or ``JOSHUA_SETTINGS_KEY`` unset. The ``error`` is the
sentence of ``joshua_core.push``, so the page says what the owner has to do.
This is why the branch is safe to deploy before any key exists.

An ``endpoint`` must be an ``https`` URL: it is the address core posts to, and
it comes from a browser, so nothing else is accepted.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from joshua_core.auth_api import require_auth_caller
from joshua_core.push import Notification, SendReport, push_config, send_to_person
from joshua_core.store.push import ENDPOINT_MAX, KEY_MAX, USER_AGENT_MAX

logger = get_logger("push_api")

PUSH_PATH = "/v1/push"
KEY_PATH = f"{PUSH_PATH}/key"
SUBSCRIBE_PATH = f"{PUSH_PATH}/subscribe"
TEST_PATH = f"{PUSH_PATH}/test"

# What the test notification says. It names no workspace, so its tag is its
# own: a second test replaces the first one in the browser.
TEST_TITLE = "Joshua"
TEST_BODY = "Notifications are on."
TEST_URL = "/"
TEST_TAG = "push-test"

NO_SUBSCRIPTION = "this browser is not subscribed"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Keys(_Body):
    """The two keys of a browser, as it sends them: base64url text."""

    p256dh: str = Field(min_length=1, max_length=KEY_MAX)
    auth: str = Field(min_length=1, max_length=KEY_MAX)


class EndpointBody(_Body):
    endpoint: str = Field(min_length=1, max_length=ENDPOINT_MAX)

    @field_validator("endpoint")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("endpoint must be an https URL")
        return value


class SubscribeBody(EndpointBody):
    keys: Keys
    # What the browser calls itself, so the owner can tell two devices apart.
    user_agent: str = Field(default="", max_length=USER_AGENT_MAX)


def _person(request: Request) -> tuple[JSONResponse | None, str | None]:
    """The person the caller vouches for: one of ``people``."""
    person_id = (request.query_params.get("person") or "").strip()
    if not person_id:
        return JSONResponse({"error": "a person is required"}, status_code=400), None
    person = request.app.state.ctx.settings.person(person_id)
    if person is None:
        return JSONResponse({"error": "unknown person"}, status_code=403), None
    return None, person.id


def _touched(report: SendReport) -> bool:
    """Whether the send reached a browser at all: one of the four counts."""
    return bool(report.sent or report.failed or report.dropped or report.held)


def _report_json(report: SendReport) -> dict[str, object]:
    return {
        "sent": report.sent,
        "failed": report.failed,
        "dropped": report.dropped,
        "held": report.held,
        "error": report.error,
    }


def build_push_router() -> APIRouter:
    router = APIRouter()

    async def _ready(request: Request) -> tuple[JSONResponse | None, str]:
        """``(None, public key)``, or the 503 that says push is not configured."""
        config, reason = await push_config(request.app.state.ctx.repo)
        if config is None:
            return JSONResponse({"error": reason}, status_code=503), ""
        return None, config.public_key

    @router.get(KEY_PATH)
    async def public_key(  # type: ignore[no-untyped-def]
        request: Request, caller: str = Depends(require_auth_caller)
    ):
        denied, key = await _ready(request)
        if denied is not None:
            return denied
        return JSONResponse({"key": key})

    @router.post(SUBSCRIBE_PATH)
    async def subscribe(  # type: ignore[no-untyped-def]
        body: SubscribeBody, request: Request, caller: str = Depends(require_auth_caller)
    ):
        denied, _ = await _ready(request)
        if denied is not None:
            return denied
        refused, person_id = _person(request)
        if refused is not None:
            return refused
        assert person_id is not None
        await request.app.state.ctx.repo.push_subscriptions.upsert(
            person_id,
            body.endpoint,
            body.keys.p256dh,
            body.keys.auth,
            user_agent=body.user_agent,
        )
        logger.info({"message": "push subscribed", "caller": caller, "person": person_id})
        return JSONResponse({"subscribed": True})

    @router.delete(SUBSCRIBE_PATH)
    async def unsubscribe(  # type: ignore[no-untyped-def]
        body: EndpointBody, request: Request, caller: str = Depends(require_auth_caller)
    ):
        denied, _ = await _ready(request)
        if denied is not None:
            return denied
        refused, person_id = _person(request)
        if refused is not None:
            return refused
        removed = await request.app.state.ctx.repo.push_subscriptions.delete(
            body.endpoint, person_id=person_id
        )
        logger.info({"message": "push unsubscribed", "person": person_id, "removed": removed})
        if not removed:
            return JSONResponse({"error": NO_SUBSCRIPTION}, status_code=404)
        return JSONResponse({"removed": True})

    @router.post(TEST_PATH)
    async def test_notification(  # type: ignore[no-untyped-def]
        request: Request, caller: str = Depends(require_auth_caller)
    ):
        denied, _ = await _ready(request)
        if denied is not None:
            return denied
        refused, person_id = _person(request)
        if refused is not None:
            return refused
        assert person_id is not None
        repo = request.app.state.ctx.repo
        if not await repo.push_subscriptions.list_for(person_id):
            return JSONResponse({"error": NO_SUBSCRIPTION}, status_code=404)
        report = await send_to_person(
            repo,
            person_id,
            Notification(title=TEST_TITLE, body=TEST_BODY, url=TEST_URL, tag=TEST_TAG),
        )
        logger.info({"message": "push test", "person": person_id, "sent": report.sent})
        if report.reason and not _touched(report):
            # Nothing reached a browser and no row changed: the send itself
            # could not be built. The reason is the sentence of the failure.
            return JSONResponse({"error": report.reason}, status_code=400)
        return JSONResponse(_report_json(report))

    return router
