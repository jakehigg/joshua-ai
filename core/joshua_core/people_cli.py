"""``python -m joshua_core people {add,list,remove}`` — operator roster management.

Runs with ``DATABASE_URL`` set. Writes the DB roster cache and the
``/data/people.yaml`` sidecar the loader merges over ``joshua.yaml``. ``remove``
marks a person removed and drops their handles; it keeps the data dirs and the
transcripts, so removal of data stays a human action.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable

from joshua_core import people
from joshua_core.store.db import Database
from joshua_core.store.repo import Repo


def _data_dir() -> str:
    return os.environ.get("JOSHUA_DATA_DIR", "/data")


async def _run(action: Callable[[Repo], Awaitable[int]]) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    db = Database(url)
    await db.connect()
    try:
        return await action(Repo(db))
    finally:
        await db.close()


async def _add(repo: Repo, args: argparse.Namespace) -> int:
    result = await people.add_person(
        repo, _data_dir(), person_id=args.id, name=args.name, handle=args.handle, role=args.role
    )
    handles = ", ".join(f"{t}:{h}" for t, h in result["handles"].items())
    print(f"added {result['id']} ({result['role']}) {handles}")
    return 0


async def _list(repo: Repo, _args: argparse.Namespace) -> int:
    rows = await people.roster(repo)
    if not rows:
        print("no people")
        return 0
    for row in rows:
        handles = ", ".join(f"{t}:{h}" for t, h in row["handles"].items()) or "no handles"
        print(f"{row['id']}\t{row['name']}\t{row['role']}\t{handles}")
    return 0


async def _remove(repo: Repo, args: argparse.Namespace) -> int:
    if await people.remove_person(repo, _data_dir(), person_id=args.id):
        print(f"removed {args.id}")
        return 0
    print(f"no such person: {args.id}", file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m joshua_core people")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add or update a person")
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--handle", required=True, help="e.g. telegram:998877")
    add.add_argument("--role", default="member", choices=["member", "guest"])
    sub.add_parser("list", help="list the roster")
    remove = sub.add_parser("remove", help="mark a person removed and drop handles")
    remove.add_argument("--id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers: dict[str, Callable[[Repo, argparse.Namespace], Awaitable[int]]] = {
        "add": _add,
        "list": _list,
        "remove": _remove,
    }
    handler = handlers[args.command]
    try:
        return asyncio.run(_run(lambda repo: handler(repo, args)))
    except people.PeopleError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
