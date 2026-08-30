"""Talk to Joshua from a terminal.

``python -m joshua_core chat --as <person>`` opens a session. Each line goes to
the terminal channel on ``channels``, which runs the same guard every channel
runs, and the reply prints as it arrives. With a message on the command line it
sends that one message and stops.

The client runs inside a container, so a person needs no Python of their own.
``make chat AS=<person>`` starts it there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator
from typing import Any

import httpx

DEFAULT_URL = "http://channels:8000"
TOKEN_ENV = "JOSHUA_TOKEN_LAPTOP"
URL_ENV = "JOSHUA_CHANNELS_URL"
STREAM_PATH = "/v1/cli/turns/stream"
OUTBOX_PATH = "/v1/cli/outbox"
PROMPT = "you> "
QUIT = ("/exit", "/quit")
# The guard's reason when the id is not a person in joshua.yaml.
UNKNOWN_REASON = "unknown_sender"


def parse_data(lines: list[str]) -> dict[str, Any]:
    raw = "\n".join(lines)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"text": raw}
    return data if isinstance(data, dict) else {"value": data}


def frames(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield `(event, data)` from a text/event-stream body."""
    name: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if name is not None:
                yield name, parse_data(data)
            name, data = None, []
            continue
        field, _, value = line.partition(":")
        if field == "event":
            name = value.strip()
        elif field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if name is not None:
        yield name, parse_data(data)


def _reason(detail: str) -> str:
    """The ``reason`` field of an error body, or the raw body."""
    try:
        data = json.loads(detail)
    except ValueError:
        return detail.strip()
    return str(data.get("reason", detail)) if isinstance(data, dict) else detail.strip()


def drain_outbox(client: httpx.Client, url: str, token: str, person: str) -> int:
    """Print what Joshua sent to this person while they were away. Returns the count.

    A failed request prints nothing; the messages stay in the outbox for the
    next session.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"person": person, "ack": "true"}
    try:
        response = client.get(url + OUTBOX_PATH, params=params, headers=headers)
    except httpx.HTTPError:
        return 0
    if response.status_code != 200:
        return 0
    messages = response.json().get("messages", [])
    for message in messages:
        stamp = str(message.get("ts", ""))[:16].replace("T", " ")
        print(f"[joshua earlier, {stamp}] {message.get('text', '')}")
        for attachment in message.get("attachments") or []:
            print(f"  attachment: {attachment}")
    return len(messages)


def send(client: httpx.Client, url: str, token: str, person: str, text: str) -> bool:
    """Send one message and print the reply. False when the turn failed."""
    headers = {"Authorization": f"Bearer {token}"}
    body = {"person": person, "text": text}
    with client.stream("POST", url + STREAM_PATH, json=body, headers=headers) as response:
        if response.status_code != 200:
            detail = response.read().decode("utf-8", "replace")
            reason = _reason(detail)
            if response.status_code == 403 and reason == UNKNOWN_REASON:
                print(
                    f'error: Joshua does not know "{person}". Add the person to '
                    "joshua.yaml, then restart the stack.",
                    file=sys.stderr,
                )
            elif response.status_code == 403:
                print(f"error: channels refused the message ({reason})", file=sys.stderr)
            else:
                print(f"error: channels answered {response.status_code}: {detail}", file=sys.stderr)
            return False
        ok = True
        for name, data in frames(response.iter_lines()):
            if name == "delta":
                sys.stdout.write(str(data.get("text", "")))
                sys.stdout.flush()
            elif name == "done":
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            elif name == "error":
                sys.stdout.write("\n")
                print(f"error: {data.get('message', 'the turn failed')}", file=sys.stderr)
                ok = False
                break
    return ok


def repl(client: httpx.Client, url: str, token: str, person: str) -> int:
    """Read a line, send it, print the reply. Repeat until the person stops."""
    print(f"Joshua. You are {person}. Type a message, or /exit to stop.")
    drain_outbox(client, url, token, person)
    while True:
        try:
            line = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        text = line.strip()
        if not text:
            continue
        if text.lower() in QUIT:
            return 0
        try:
            send(client, url, token, person, text)
            drain_outbox(client, url, token, person)
        except httpx.HTTPError as exc:
            print(f"error: cannot reach channels at {url}: {exc}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="joshua chat", description="Talk to Joshua from a terminal."
    )
    parser.add_argument("--as", dest="person", required=True, help="person id to speak as")
    parser.add_argument("message", nargs="*", help="send one message and exit")
    parser.add_argument(
        "--url",
        default=os.environ.get(URL_ENV, DEFAULT_URL),
        help=f"channels base URL (default {DEFAULT_URL}, or ${URL_ENV})",
    )
    parser.add_argument(
        "--token-env", default=TOKEN_ENV, help=f"env var holding the bearer (default {TOKEN_ENV})"
    )
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env)
    if not token:
        print(f"error: set {args.token_env} to the laptop fleet token", file=sys.stderr)
        return 2

    url = args.url.rstrip("/")
    with httpx.Client(timeout=None) as client:
        if args.message:
            try:
                drain_outbox(client, url, token, args.person)
                return 0 if send(client, url, token, args.person, " ".join(args.message)) else 1
            except httpx.HTTPError as exc:
                print(f"error: cannot reach channels at {url}: {exc}", file=sys.stderr)
                return 1
        return repl(client, url, token, args.person)
