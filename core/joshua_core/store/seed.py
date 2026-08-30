"""Seed the people + person_handles cache from joshua.yaml.

``joshua.yaml`` is the source of truth for the roster; the DB rows are a cache so
joins work. Seeding upserts every configured person and handle. A person that is
in the DB but not in the config is left in place (their transcripts reference
them) and logged once.
"""

from __future__ import annotations

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.store.repo import Repo

logger = get_logger("store.seed")


async def seed_people(repo: Repo, config: JoshuaConfig) -> None:
    """Upsert people and handles from ``config.people``.

    Never deletes a row. A DB person absent from the config is logged once.
    """
    config_ids: set[str] = set()
    for person in config.people:
        await repo.upsert_person(person.id, person.name, person.role)
        config_ids.add(person.id)
        for channel_type, handle in person.handles.items():
            await repo.upsert_person_handle(channel_type, handle, person.id)

    for row in await repo.list_people():
        if row.id not in config_ids:
            logger.info(
                {"message": "person in db not in config; left in place", "person_id": row.id}
            )
