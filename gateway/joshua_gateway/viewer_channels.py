"""The client of the Joshua channels service, for the chat routes of the viewer.

The base URL is ``CHANNELS_URL`` and the bearer is ``JOSHUA_TOKEN_VIEWER``;
``from_env`` gives None when either is missing, so the chat routes answer 503
and the rest of the viewer is not affected. Four settings have defaults and
can be set in the environment: ``CHANNELS_CONNECT_TIMEOUT_S``,
``OUTBOX_TIMEOUT_S``, ``SSE_KEEPALIVE_S`` and ``TURN_MAX_BYTES``.

One ``httpx.AsyncClient`` holds the base URL and the bearer token. A turn is
``POST /v1/cli/turns/stream``; the answer is ``text/event-stream``. The
history is ``GET /v1/cli/transcript``. The stream
has no read timeout, because a turn can take minutes. A refusal before the
stream starts raises ``ChannelsError``; a connection failure raises
``ChannelsUnavailable``. Neither error carries the token.

A turn with a file is ``multipart/form-data``: the fields ``person`` and
``text`` beside one ``file`` part. httpx reads the file in chunks while it
sends the request, so the body is never held in memory.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import IO, Any

import httpx

CHANNELS_URL_ENV = "CHANNELS_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
TURN_PATH = "/v1/cli/turns/stream"
TRANSCRIPT_PATH = "/v1/cli/transcript"
HEALTH_PATH = "/healthz"
LINE_END = re.compile(r"[\r\n]")


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    """A positive number from the environment, or ``default`` when it is unset or bad."""
    try:
        value = float(env.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ChatSettings:
    """The numbers of the chat that the environment can change."""

    connect_timeout_s: float = 10.0
    outbox_timeout_s: float = 10.0
    sse_keepalive_s: float = 20.0
    # One turn body, the text and one file together. Channels allows more.
    turn_max_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ChatSettings:
        return cls(
            connect_timeout_s=_number(env, "CHANNELS_CONNECT_TIMEOUT_S", 10.0),
            outbox_timeout_s=_number(env, "OUTBOX_TIMEOUT_S", 10.0),
            sse_keepalive_s=_number(env, "SSE_KEEPALIVE_S", 20.0),
            turn_max_bytes=int(_number(env, "TURN_MAX_BYTES", 10 * 1024 * 1024)),
        )


# A file to send beside the text: its name, its bytes as a stream, its type.
Attachment = tuple[str, IO[bytes], str]


class ChannelsError(Exception):
    """Channels answered a call with a non-2xx status."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(f"channels answered {status} {reason}")
        self.status = status
        self.reason = reason
        self.detail = detail


class ChannelsUnavailable(Exception):
    """Channels could not be reached, or the connection failed before an answer."""


class SseParser:
    """An incremental parser of ``text/event-stream``.

    ``feed`` takes decoded text in chunks of any size and returns the frames
    that each chunk completes, as ``(event, data)``. ``data`` is the parsed
    JSON value, or the raw string when it is not JSON. A frame with no
    ``event:`` line is ``message``. Lines end with ``\\n``, ``\\r\\n``, or ``\\r``.
    Comment lines and the ``id`` and ``retry`` fields are ignored.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: list[str] = []

    def feed(self, chunk: str) -> list[tuple[str, Any]]:
        self._buffer += chunk
        frames: list[tuple[str, Any]] = []
        while True:
            match = LINE_END.search(self._buffer)
            if match is None:
                break
            start = match.start()
            if self._buffer[start] == "\r":
                if start + 1 == len(self._buffer):
                    break  # a "\n" can still follow in the next chunk
                end = start + 2 if self._buffer[start + 1] == "\n" else start + 1
            else:
                end = start + 1
            line = self._buffer[:start]
            self._buffer = self._buffer[end:]
            frame = self._line(line)
            if frame is not None:
                frames.append(frame)
        return frames

    def close(self) -> list[tuple[str, Any]]:
        """Finish the stream. A frame with no blank line after it is dropped, as in SSE."""
        frames: list[tuple[str, Any]] = []
        if self._buffer.endswith("\r"):
            frame = self._line(self._buffer[:-1])
            if frame is not None:
                frames.append(frame)
        self._buffer = ""
        self._event = ""
        self._data = []
        return frames

    def _line(self, line: str) -> tuple[str, Any] | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def _dispatch(self) -> tuple[str, Any] | None:
        if not self._data:
            self._event = ""
            return None
        raw = "\n".join(self._data)
        event = self._event or "message"
        self._event = ""
        self._data = []
        try:
            return event, json.loads(raw)
        except ValueError:
            return event, raw


def _refusal(response: httpx.Response, body: bytes) -> ChannelsError:
    reason, detail = "", ""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or "")
        detail = str(payload.get("detail") or "")
    return ChannelsError(response.status_code, reason or f"http_{response.status_code}", detail)


class TurnStream:
    """An open turn stream. Read it with ``events()``; ``aclose()`` ends it."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def events(self) -> AsyncIterator[tuple[str, Any]]:
        parser = SseParser()
        try:
            async for chunk in self._response.aiter_text():
                for frame in parser.feed(chunk):
                    yield frame
            for frame in parser.close():
                yield frame
        finally:
            await self._response.aclose()

    async def aclose(self) -> None:
        await self._response.aclose()


class ChannelsClient:
    """Calls channels with ``Authorization: Bearer <JOSHUA_TOKEN_VIEWER>``."""

    def __init__(
        self,
        base_url: str,
        token: str,
        settings: ChatSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or ChatSettings()
        connect = self.settings.connect_timeout_s
        self._health_timeout = httpx.Timeout(connect)
        self._outbox_timeout = httpx.Timeout(self.settings.outbox_timeout_s, connect=connect)
        # No read timeout: a turn can take minutes.
        self._stream_timeout = httpx.Timeout(
            connect=connect, read=None, write=connect, pool=connect
        )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def open_turn(
        self, person: str, text: str, attachment: Attachment | None = None
    ) -> TurnStream:
        """Start a turn and return its stream once channels has accepted it.

        With an ``attachment`` the call is ``multipart/form-data``, else JSON.
        The attachment is read in chunks as the request goes out; the caller
        owns its stream and closes it once this call returns.
        """
        headers = {"Accept": "text/event-stream"}
        if attachment is None:
            request = self._client.build_request(
                "POST",
                TURN_PATH,
                json={"person": person, "text": text},
                headers=headers,
                timeout=self._stream_timeout,
            )
        else:
            request = self._client.build_request(
                "POST",
                TURN_PATH,
                data={"person": person, "text": text},
                files={"file": attachment},
                headers=headers,
                timeout=self._stream_timeout,
            )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ChannelsUnavailable(type(exc).__name__) from None
        if not response.is_success:
            try:
                body = await response.aread()
            except httpx.HTTPError:
                body = b""
            finally:
                await response.aclose()
            raise _refusal(response, body)
        return TurnStream(response)

    async def transcript(
        self, person: str, limit: int | None = None, before: int | None = None
    ) -> dict[str, Any]:
        """Return one page of the conversation of ``person``, oldest first.

        The answer is ``{"conversation_id", "messages", "next_before"}``. The
        call uses the outbox timeout.
        """
        params: dict[str, str | int] = {"person": person}
        if limit is not None:
            params["limit"] = limit
        if before is not None:
            params["before"] = before
        try:
            response = await self._client.get(
                TRANSCRIPT_PATH, params=params, timeout=self._outbox_timeout
            )
        except httpx.HTTPError as exc:
            raise ChannelsUnavailable(type(exc).__name__) from None
        if not response.is_success:
            raise _refusal(response, response.content)
        try:
            page = response.json()
        except ValueError:
            raise ChannelsError(response.status_code, "bad_answer") from None
        if not isinstance(page, dict):
            raise ChannelsError(response.status_code, "bad_answer")
        return page

    async def healthz(self) -> bool:
        """Return True when channels answers ``/healthz`` with 200 and ``{"ok": true}``."""
        try:
            response = await self._client.get(HEALTH_PATH, timeout=self._health_timeout)
            return response.status_code == 200 and response.json().get("ok") is True
        except (httpx.HTTPError, ValueError, AttributeError):
            return False


def from_env(env: Mapping[str, str] | None = None) -> ChannelsClient | None:
    """A client from ``CHANNELS_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None."""
    env = os.environ if env is None else env
    base_url = env.get(CHANNELS_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return ChannelsClient(base_url, token, ChatSettings.from_env(env))
