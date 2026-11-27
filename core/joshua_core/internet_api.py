"""``POST /v1/internet``: one lookup on the open web for a caller outside the agent.

The ``internet`` builtin of the gateway calls this route for ``use_internet``,
so an assignment session reads the open web. The internet agent runs here,
because core holds the model credential and the register of granted URLs, and
because the agent must run in its own context: the session gets the answer as
text and never a page with an egress beside it.

The body holds the question, the URLs the caller asks to have read, the person,
and the conversation. The lookup is the one the chat agent makes
(``engine/tools/internet.lookup``), so the same gate applies:

- **the grant rule** of ``engine/url_grants.py``. A URL is read only when a
  person wrote it in that conversation, or when an earlier answer of this
  conversation returned it as a source. Anything else is refused, and the
  refusal says why. A session therefore follows up on a source of its own
  earlier answer, and never on a URL it read in a repository or on a page;
- **the fetch rules** of the internet agent: the block list of
  ``joshua_shared.netblock``, and no page that the run was not given.

Only the ``gateway`` identity may call, because the gateway is the one place
that applies the per-person tool policy. The answer is always text, also when
the internet is off, when nothing was found, or when every URL was refused.
The question, the URLs, and the answer are never logged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import (
    GATEWAY_IDENTITY,
    INTERNET_PATH,
    InternetRequest,
    InternetResponse,
)
from joshua_shared.fleet_auth import require_identity
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_core.engine.tools import internet as internet_tool

logger = get_logger("internet_api")

# The fleet identities that may ask for a lookup. The gateway is the outbound
# boundary and the policy point, so it is the only one.
ALLOWED_CALLERS = (GATEWAY_IDENTITY,)


def _invalid(exc: ValidationError) -> JSONResponse:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "body"
    return JSONResponse({"error": f"{where}: {first.get('msg', 'not valid')}"}, status_code=400)


def build_internet_router() -> APIRouter:
    """The internet route. Only the ``gateway`` may call it."""
    router = APIRouter()

    @router.post(INTERNET_PATH)
    async def use_internet(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(list(ALLOWED_CALLERS))),
    ):
        try:
            body = InternetRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        ctx = request.app.state.ctx
        logger.info(
            {
                "message": "internet route asked",
                "person_id": body.person,
                "urls": len(body.urls),
            }
        )
        text = await internet_tool.lookup(
            repo=ctx.repo,
            person_id=body.person,
            conversation_id=body.conversation or "",
            settings=ctx.settings,
            question=body.question,
            urls=list(body.urls),
            grants=getattr(ctx.manager, "url_grants", None),
            searcher=ctx.internet_searcher,
        )
        return JSONResponse(InternetResponse(text=text).model_dump(mode="json"))

    return router
