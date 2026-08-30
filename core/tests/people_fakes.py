"""Offline fake repo for the people/registration unit tests.

It holds the roster and handles in memory and mirrors the ``Repo`` methods the
people code calls, so the tests run with no DB.
"""

from __future__ import annotations

from joshua_core.engine.tools import ToolDeps
from joshua_core.store.models import Channel, Conversation, Person


class PeopleFakeRepo:
    def __init__(self, people: list[Person] | None = None) -> None:
        self.people: dict[str, Person] = {p.id: p for p in (people or [])}
        self.handles: dict[tuple[str, str], str] = {}

    async def get_person(self, person_id: str) -> Person | None:
        return self.people.get(person_id)

    async def list_people(self) -> list[Person]:
        return sorted(self.people.values(), key=lambda p: p.id)

    async def upsert_person(self, person_id: str, display_name: str, role: str = "member") -> None:
        self.people[person_id] = Person(id=person_id, display_name=display_name, role=role)

    async def get_person_by_handle(self, channel_type: str, handle: str) -> Person | None:
        pid = self.handles.get((channel_type, handle))
        return self.people.get(pid) if pid else None

    async def upsert_person_handle(self, channel_type: str, handle: str, person_id: str) -> None:
        self.handles[(channel_type, handle)] = person_id

    async def handles_for_person(self, person_id: str) -> list[tuple[str, str]]:
        return sorted(k for k, v in self.handles.items() if v == person_id)

    async def remove_person(self, person_id: str) -> bool:
        person = self.people.get(person_id)
        if person is None:
            return False
        self.people[person_id] = Person(
            id=person_id, display_name=person.display_name, role="removed"
        )
        self.handles = {k: v for k, v in self.handles.items() if v != person_id}
        return True


def make_deps(
    repo: PeopleFakeRepo, *, person_id: str | None, channel_type: str = "telegram"
) -> ToolDeps:
    channel = Channel(id=f"{channel_type}:1", channel_type=channel_type, session_mode="per_person")
    conversation = Conversation(id="c1", channel_id=channel.id, person_id=person_id)
    return ToolDeps(
        repo=repo, conversation=conversation, channel=channel, tz="UTC", person_id=person_id
    )
