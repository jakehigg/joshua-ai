"""Pure-unit test for the roster seeder, driven by a fake repo."""

from __future__ import annotations

from joshua_core.store.models import Person
from joshua_core.store.seed import seed_people
from joshua_shared import config as config_module

CONFIG_YAML = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
    handles:
      telegram: "998877"
      imessage: "+15551234567"
  - id: mia
    name: Mia
    role: guest
    handles:
      telegram: "112233"
"""


class FakeRepo:
    """Records upserts; ``list_people`` returns the current roster."""

    def __init__(self, existing: list[Person] | None = None) -> None:
        self.people: dict[str, Person] = {p.id: p for p in (existing or [])}
        self.handles: list[tuple[str, str, str]] = []

    async def upsert_person(self, person_id: str, display_name: str, role: str = "member") -> None:
        self.people[person_id] = Person(id=person_id, display_name=display_name, role=role)

    async def upsert_person_handle(self, channel_type: str, handle: str, person_id: str) -> None:
        self.handles.append((channel_type, handle, person_id))

    async def list_people(self) -> list[Person]:
        return list(self.people.values())


async def test_seed_upserts_people_and_handles() -> None:
    cfg = config_module.parse(CONFIG_YAML)
    repo = FakeRepo()

    await seed_people(repo, cfg)

    assert set(repo.people) == {"alex", "mia"}
    assert repo.people["mia"].role == "guest"
    assert ("telegram", "998877", "alex") in repo.handles
    assert ("imessage", "+15551234567", "alex") in repo.handles
    assert ("telegram", "112233", "mia") in repo.handles
    assert len(repo.handles) == 3


async def test_seed_keeps_person_absent_from_config() -> None:
    cfg = config_module.parse(CONFIG_YAML)
    stranger = Person(id="ghost", display_name="Ghost", role="member")
    repo = FakeRepo(existing=[stranger])

    await seed_people(repo, cfg)

    # The configured people are added; the stranger is left in place.
    assert set(repo.people) == {"alex", "mia", "ghost"}


async def test_seed_updates_renamed_display_name() -> None:
    cfg = config_module.parse(CONFIG_YAML)
    repo = FakeRepo(existing=[Person(id="alex", display_name="Old Name", role="member")])

    await seed_people(repo, cfg)

    assert repo.people["alex"].display_name == "Alex"
