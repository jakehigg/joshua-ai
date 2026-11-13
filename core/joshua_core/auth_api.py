"""``/auth/*``: the secret of a person, hashed with bcrypt.

Core owns the roster and the database, so a person's secret lives here and
nowhere else. The viewer calls these routes to check a secret at sign-in, and
to set, change, or drop one.

``AUTH_CALLERS`` (env, a comma list, default ``viewer``) names the fleet
identities that may call. It is checked on every route with no exception, the
way ``ADMIN_CALLERS`` is checked on every admin route: a missing or invalid
bearer is 401, an identity outside the list is 403, and an empty list turns the
routes off, so every caller gets 403.

- ``POST /auth/verify`` — ``{person, secret}`` → ``{ok}``. ``ok`` is false for
  a wrong secret, for a person with no secret, and for a person who is not on
  the roster. The answer never says which of the three it was.
- ``POST /auth/secret`` — ``{person, secret, new_secret}``: a person changes
  their own secret and sends the current one. A wrong current secret, and a
  person who has none yet, get 403.
- ``POST /auth/secret/admin`` — ``{person, new_secret}``: set the secret of a
  person without the current one, for a first sign-in or a reset.
- ``DELETE /auth/secret/{person_id}`` — drop the secret of a person.
- ``GET /auth/people`` — the ids of the people who have a secret. A person
  with no secret is absent.

Every route but ``/auth/verify`` answers 404 for a person who is not on the
roster. No answer and no log line ever carries a secret or a hash.
"""

from __future__ import annotations

import os

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from joshua_shared.fleet_auth import parse_callers, require_identity
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger("auth_api")

AUTH_PATH = "/auth"
AUTH_CALLERS_ENV = "AUTH_CALLERS"
DEFAULT_AUTH_CALLERS = "viewer"

# The cost of one hash. 12 is the common default, about a tenth of a second.
BCRYPT_ROUNDS = 12
# bcrypt reads at most 72 bytes of a secret; the rest is cut off before the
# call, because a longer one raises.
BCRYPT_MAX_BYTES = 72


def _secret_bytes(secret: str) -> bytes:
    return secret.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_secret(secret: str) -> str:
    """The bcrypt hash of ``secret``."""
    return bcrypt.hashpw(_secret_bytes(secret), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode(
        "utf-8"
    )


def verify_secret(secret: str, secret_hash: str | None) -> bool:
    """True when ``secret`` matches ``secret_hash``.

    No secret, no hash, and a hash that bcrypt cannot read never match.
    """
    if not secret or not secret_hash:
        return False
    try:
        return bcrypt.checkpw(_secret_bytes(secret), secret_hash.encode("utf-8"))
    except ValueError:
        return False


def require_auth_caller(request: Request) -> str:
    """The identity of the caller, or 401/403.

    The allowlist is read on each request, so the environment of the container
    decides it. An empty ``AUTH_CALLERS`` turns the routes off.
    """
    allowed = parse_callers(os.environ.get(AUTH_CALLERS_ENV, DEFAULT_AUTH_CALLERS))
    if not allowed:
        raise HTTPException(status_code=403, detail="identity not allowed")
    return require_identity(allowed)(request)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyBody(_Body):
    person: str = Field(min_length=1, max_length=64)
    secret: str = Field(min_length=1, max_length=256)


class ChangeBody(_Body):
    person: str = Field(min_length=1, max_length=64)
    secret: str = Field(min_length=1, max_length=256)
    new_secret: str = Field(min_length=1, max_length=256)


class SetBody(_Body):
    person: str = Field(min_length=1, max_length=64)
    new_secret: str = Field(min_length=1, max_length=256)


def _unknown_person() -> JSONResponse:
    return JSONResponse({"error": "unknown person"}, status_code=404)


async def _on_roster(request: Request, person_id: str) -> bool:
    return (await request.app.state.ctx.repo.get_person(person_id)) is not None


def build_auth_router() -> APIRouter:
    router = APIRouter()

    @router.post(f"{AUTH_PATH}/verify")
    async def verify(  # type: ignore[no-untyped-def]
        body: VerifyBody, request: Request, caller: str = Depends(require_auth_caller)
    ):
        repo = request.app.state.ctx.repo
        ok = verify_secret(body.secret, await repo.get_auth_secret(body.person))
        logger.info({"message": "auth verify", "caller": caller, "ok": ok})
        return JSONResponse({"ok": ok})

    @router.post(f"{AUTH_PATH}/secret")
    async def set_own_secret(  # type: ignore[no-untyped-def]
        body: ChangeBody, request: Request, caller: str = Depends(require_auth_caller)
    ):
        repo = request.app.state.ctx.repo
        if not await _on_roster(request, body.person):
            return _unknown_person()
        if not verify_secret(body.secret, await repo.get_auth_secret(body.person)):
            logger.info({"message": "auth secret refused", "caller": caller})
            return JSONResponse({"error": "wrong secret"}, status_code=403)
        await repo.set_auth_secret(body.person, hash_secret(body.new_secret))
        logger.info({"message": "auth secret changed", "caller": caller})
        return JSONResponse({"ok": True, "person": body.person})

    @router.post(f"{AUTH_PATH}/secret/admin")
    async def set_secret(  # type: ignore[no-untyped-def]
        body: SetBody, request: Request, caller: str = Depends(require_auth_caller)
    ):
        repo = request.app.state.ctx.repo
        if not await _on_roster(request, body.person):
            return _unknown_person()
        await repo.set_auth_secret(body.person, hash_secret(body.new_secret))
        logger.info({"message": "auth secret set", "caller": caller})
        return JSONResponse({"ok": True, "person": body.person})

    @router.delete(f"{AUTH_PATH}/secret/{{person_id}}")
    async def delete_secret(  # type: ignore[no-untyped-def]
        person_id: str, request: Request, caller: str = Depends(require_auth_caller)
    ):
        repo = request.app.state.ctx.repo
        if not await _on_roster(request, person_id):
            return _unknown_person()
        deleted = await repo.delete_auth_secret(person_id)
        logger.info({"message": "auth secret deleted", "caller": caller, "deleted": deleted})
        return JSONResponse({"ok": True, "person": person_id, "deleted": deleted})

    @router.get(f"{AUTH_PATH}/people")
    async def list_people(  # type: ignore[no-untyped-def]
        request: Request, caller: str = Depends(require_auth_caller)
    ):
        people = await request.app.state.ctx.repo.list_auth_person_ids()
        return JSONResponse({"people": people})

    return router
