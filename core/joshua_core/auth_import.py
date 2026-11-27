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
- **Nothing imported, no marker.** A run that wrote no row leaves the marker
  unset, so a run that read nothing but broken values is repeatable once they
  are corrected.

A reference that starts with ``$2`` is already a bcrypt hash and is stored as
it is; anything else is a literal password and is hashed first. Either way the
value that would be stored is checked against the bcrypt format first, so a
truncated or mistyped hash is counted ``unreadable`` instead of becoming a
secret that nobody can sign in with. The log names the person, never the
value.
"""

from __future__ import annotations

import os
import re
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
# The whole shape of a bcrypt hash: the version, the cost, and the 53
# characters of salt and digest — 60 characters in all. A value that claims to
# be a hash and does not match this one is not a hash.
BCRYPT_RE = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")


def is_bcrypt_hash(value: str) -> bool:
    """True when ``value`` is a bcrypt hash, by its shape alone."""
    return bool(BCRYPT_RE.match(value))


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


def _env_ids() -> set[str]:
    """The person ids that ``VIEWER_PASSWORDS`` and ``VIEWER_PW_<ID>`` name.

    ``VIEWER_PW_<ID>`` is read back to an id the way ``password_reference``
    builds the name: lower case, with ``_`` for ``-``. A variable with an
    empty value names nobody.
    """
    ids = set(_passwords_from_env())
    for name, value in os.environ.items():
        if name.startswith(VIEWER_PW_PREFIX) and value.strip():
            person_id = name[len(VIEWER_PW_PREFIX) :].lower().replace("_", "-")
            if person_id:
                ids.add(person_id)
    return ids


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
    import failed. The marker is written only when a row was, so a run that
    imported nothing runs again at the next start. Raises nothing: core starts
    whatever happens here.
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
                if not is_bcrypt_hash(secret_hash):
                    # A reference that claims to be a hash and is not: storing
                    # it would be a secret that matches no input.
                    unreadable.append(person_id)
                    logger.info(
                        {
                            "message": "auth import unreadable",
                            "person": person_id,
                            "why": "not a bcrypt hash",
                        }
                    )
                    continue
                await repo.set_auth_secret(person_id, secret_hash, conn=conn)
                imported.append(person_id)
                logger.info({"message": "auth import imported", "person": person_id})
            # The old viewer let only the ids of ``viewer.users`` sign in, so
            # an id that only the environment names gets no secret. Name each
            # one, because a run that skips it in silence looks like a run
            # that found nothing.
            for person_id in sorted(_env_ids() - set(settings.viewer.users)):
                skipped.append(person_id)
                logger.info(
                    {
                        "message": "auth import skipped",
                        "person": person_id,
                        "why": "not in viewer.users",
                    }
                )
            if imported:
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
