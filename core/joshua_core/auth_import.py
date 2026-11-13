"""The one-time import of the viewer passwords into ``auth_secret``.

Before this, a viewer password lived in the config and the environment:
``viewer.users`` in ``joshua.yaml``, ``VIEWER_PASSWORDS``, or
``VIEWER_PW_<ID>``. Core owns the secrets now, so each of those is read once
and written to ``auth_secret``, and nobody has to sign in again after the
upgrade. ``gateway.viewer`` reads the same three sources in the same order.

This runs unattended, against live data, on the first start after the upgrade.
Four rules hold it:

- **The marker, not the table.** ``import_once`` returns at once when
  ``auth_import_marker`` is set, so a restart, a restored database, or a
  roster whose secrets were all dropped never runs it again. "The table is
  empty" would re-run it on each of those, and would give a person back a
  password they deliberately dropped.
- **Never overwrite.** A person who already has a row is skipped, on the
  first run as on any later one. The row in the database is the newer one.
- **One transaction.** The rows and the marker are written together, so a
  crash part way leaves neither half-imported rows nor a marker.
- **Never stop core.** Every failure is logged and swallowed, and the marker
  stays unset, so the next start tries again. One line names each person id,
  and one summary line closes the run. No line carries a secret or a hash.

A reference that starts with ``$2`` is already a bcrypt hash and is stored as
it is; anything else is a literal password and is hashed first.
"""

from __future__ import annotations

import os
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.auth_api import hash_secret
from joshua_core.store.repo import Repo

logger = get_logger("auth_import")

# The same two environment sources the viewer reads.
VIEWER_PASSWORDS_ENV = "VIEWER_PASSWORDS"
VIEWER_PW_PREFIX = "VIEWER_PW_"
# A reference that starts with this is a bcrypt hash, not a password.
BCRYPT_PREFIX = "$2"


def _passwords_from_env() -> dict[str, str]:
    """Parse ``VIEWER_PASSWORDS`` into person id -> password reference.

    The format is ``<id>=<reference>``, comma separated, as the viewer reads
    it: one variable carries every password, so the compose file names no
    person.
    """
    out: dict[str, str] = {}
    for item in os.environ.get(VIEWER_PASSWORDS_ENV, "").split(","):
        person_id, separator, reference = item.strip().partition("=")
        if separator and person_id.strip() and reference.strip():
            out[person_id.strip()] = reference.strip()
    return out


def password_reference(settings: JoshuaConfig, person_id: str) -> str:
    """The password reference of ``person_id``, or "" when there is none.

    The entry of ``viewer.users`` first, then ``VIEWER_PASSWORDS``, then
    ``VIEWER_PW_<ID>`` — the order of ``gateway.viewer``, so the import reads
    what the viewer would have read.
    """
    reference = (settings.viewer.users.get(person_id) or "").strip()
    if reference:
        return reference
    reference = _passwords_from_env().get(person_id, "")
    if reference:
        return reference
    return os.environ.get(f"{VIEWER_PW_PREFIX}{person_id.upper().replace('-', '_')}", "").strip()


async def import_once(repo: Repo, settings: JoshuaConfig) -> dict[str, Any]:
    """Import the viewer passwords into ``auth_secret``, once ever.

    Returns a summary: ``ran`` is False when the marker was already set or the
    import failed. Raises nothing: core starts whatever happens here.
    """
    summary: dict[str, Any] = {"ran": False, "imported": 0, "skipped": 0, "unreadable": 0}
    try:
        if await repo.get_auth_import_marker() is not None:
            logger.info({"message": "auth import already done"})
            return summary

        imported: list[str] = []
        skipped: list[str] = []
        unreadable: list[str] = []
        async with repo.transaction() as conn:
            for person_id in sorted(settings.viewer.users):
                if await repo.get_person(person_id) is None:
                    # No row to hang a secret on. A person off the roster is
                    # not a failure of the import.
                    skipped.append(person_id)
                    logger.info(
                        {"message": "auth import skipped", "person": person_id, "why": "no person"}
                    )
                    continue
                if await repo.get_auth_secret(person_id, conn=conn) is not None:
                    skipped.append(person_id)
                    logger.info(
                        {
                            "message": "auth import skipped",
                            "person": person_id,
                            "why": "has a secret",
                        }
                    )
                    continue
                reference = password_reference(settings, person_id)
                if not reference:
                    unreadable.append(person_id)
                    logger.info({"message": "auth import unreadable", "person": person_id})
                    continue
                secret_hash = (
                    reference if reference.startswith(BCRYPT_PREFIX) else hash_secret(reference)
                )
                await repo.set_auth_secret(person_id, secret_hash, conn=conn)
                imported.append(person_id)
                logger.info({"message": "auth import imported", "person": person_id})
            await repo.set_auth_import_marker(len(imported), conn=conn)
    except Exception as exc:  # noqa: BLE001 — the import never stops core
        # The marker stays unset, so the next start tries the whole import
        # again. The transaction took the half-written rows with it.
        logger.error({"message": "auth import failed", "error": str(exc)})
        return summary

    summary = {
        "ran": True,
        "imported": len(imported),
        "skipped": len(skipped),
        "unreadable": len(unreadable),
    }
    logger.info({"message": "auth import done", **summary})
    return summary
