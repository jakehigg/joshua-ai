"""A fake SDK client and a fake core, for the runner tests. No network, no CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    UserMessage,
)
from joshua_assignments.core_client import CoreClient
from joshua_assignments.session import ASSIGNMENT_SERVER, server_handlers
from joshua_assignments.settings import Settings

SESSION = "sess-1"
USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 5,
}


def init(session_id: str = SESSION) -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": session_id})


def assistant(message_id: str = "m1", usage: dict | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[], model="claude-sonnet-5", usage=usage or USAGE, message_id=message_id
    )


def result(
    subtype: str = "error_max_turns",
    *,
    is_error: bool | None = None,
    turns: int = 10,
    session_id: str = SESSION,
    usage: dict | None = None,
    api_error_status: int | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=(subtype != "success") if is_error is None else is_error,
        num_turns=turns,
        session_id=session_id,
        usage=usage or USAGE,
        result="secret error text from a host.example" if subtype != "success" else "ok",
        api_error_status=api_error_status,
    )


def tool_error(text: str | list[dict[str, Any]], tool_use_id: str = "t1") -> UserMessage:
    """The message that carries one tool result that failed."""
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_use_id, content=text, is_error=True)]
    )


def rate_limit(utilization: float, kind: str = "five_hour", resets_at: int = 1794108600):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning",
            rate_limit_type=kind,  # type: ignore[arg-type]
            utilization=utilization,
            resets_at=resets_at,
        ),
        uuid="u1",
        session_id=SESSION,
    )


def call(name: str, **args: Any) -> tuple[str, str, dict[str, Any]]:
    """A script step: the model calls an ``assignment`` tool."""
    return ("tool", name, args)


def shell(run: Callable[[Any], Any]) -> tuple[str, Callable[[Any], Any]]:
    """A script step: the session runs a command. ``run`` gets the options."""
    return ("shell", run)


HANG = "hang"


class FakeClient:
    """One SDK client. It yields its script; a tool step calls the real handler."""

    def __init__(self, sdk: FakeSdk, options: Any) -> None:
        self.sdk = sdk
        self.options = options
        self.script = sdk.scripts.pop(0) if sdk.scripts else [result("success")]
        self.prompt: str | None = None
        self.interrupted = False
        self.disconnected = False
        self.replies: list[dict] = []

    async def connect(self) -> None:
        if self.sdk.connect_error is not None:
            raise self.sdk.connect_error

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self):
        handlers = server_handlers(self.options.mcp_servers[ASSIGNMENT_SERVER])
        assert handlers is not None
        for step in self.script:
            if step == HANG:
                await asyncio.Event().wait()
            elif isinstance(step, tuple) and step[0] == "tool":
                self.replies.append(await handlers[step[1]](step[2]))
            elif isinstance(step, tuple) and step[0] == "shell":
                # What a Bash call of the session does: run with the session options.
                self.replies.append(await step[1](self.options))
            elif isinstance(step, BaseException):
                raise step
            else:
                yield step

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeSdk:
    """The client factory. ``scripts`` holds one list of steps for each slice."""

    def __init__(self, *scripts: list[Any]) -> None:
        self.scripts = list(scripts)
        self.clients: list[FakeClient] = []
        self.connect_error: Exception | None = None

    def __call__(self, *, options: Any) -> FakeClient:
        client = FakeClient(self, options)
        self.clients.append(client)
        return client


def window(used: float, resets_at: str = "2026-03-04T14:05:00+00:00") -> dict[str, Any]:
    return {"used_pct": used, "left_pct": 100 - used, "resets_at": resets_at}


class FakeCore:
    """Core's ``GET /v1/limits`` and ``POST /v1/usage``."""

    def __init__(self, five_hour: float | None = 20.0, seven_day: float | None = 10.0) -> None:
        self.five_hour = five_hour
        self.seven_day = seven_day
        self.available = True
        self.limit_status = 200
        self.usage_replies: list[int] = []
        self.usage_rows: list[dict[str, Any]] = []
        self.usage_requests: list[httpx.Request] = []
        self.limit_reads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/limits":
            self.limit_reads += 1
            if self.limit_status != 200:
                return httpx.Response(self.limit_status, json={"error": "x"})
            if not self.available:
                return httpx.Response(200, json={"available": False, "reason": "no token"})
            body: dict[str, Any] = {"available": True}
            if self.five_hour is not None:
                body["five_hour"] = window(self.five_hour)
            if self.seven_day is not None:
                body["seven_day"] = window(self.seven_day)
            return httpx.Response(200, json=body)
        if request.method == "POST" and request.url.path == "/v1/usage":
            self.usage_requests.append(request)
            status = self.usage_replies.pop(0) if self.usage_replies else 200
            if status == 200:
                self.usage_rows.extend(json.loads(request.content)["rows"])
            return httpx.Response(status, json={"accepted": 1})
        return httpx.Response(404)

    def client(self, settings: Settings) -> CoreClient:
        return CoreClient(settings, make_client=self.make_client, backoff_s=0.0)

    @property
    def make_client(self) -> Callable[[], httpx.AsyncClient]:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
