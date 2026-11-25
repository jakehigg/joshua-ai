"""A small client of the Jupyter server REST and kernel APIs.

The code execution container runs the stock ``jupyter server``. This client uses
three parts of its API, and no extension:

- ``/api/kernels`` starts, interrupts and shuts down a kernel. A kernel starts
  in a folder under the root of the server, and that folder is its working
  directory.
- ``/api/kernels/<id>/channels`` is the websocket of a kernel. The client
  sends one ``execute_request`` and reads the messages that answer it, with
  the JSON wire format of the Jupyter messaging protocol.
- ``/api/contents`` puts a file in a folder, lists a folder, reads a file,
  and deletes one.

Every request carries ``Authorization: token <token>``. A message of an error
names the operation, never the token or the code.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

# The kernel spec of the image: plain Python 3.
KERNEL_NAME = "python3"
# The version of the Jupyter messaging protocol in a request header.
PROTOCOL_VERSION = "5.3"
# One HTTP request to the server, in seconds. A run has its own timeout.
REQUEST_TIMEOUT_S = 30.0

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class CodeExecutionError(Exception):
    """A request to the code execution container failed. The message is safe to give the agent."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Execution:
    """What one ``execute_request`` gave back."""

    stdout: str = ""
    stderr: str = ""
    # The ``text/plain`` of each ``execute_result`` and ``display_data``.
    results: list[str] = field(default_factory=list)
    # ``(mime, base64 data)`` of each image a display gave.
    images: list[tuple[str, str]] = field(default_factory=list)
    # The traceback of an exception, with the terminal colors removed.
    error: str | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class Entry:
    """One file or folder that ``/api/contents`` lists."""

    name: str
    path: str
    type: str
    size: int | None
    last_modified: datetime | None


def strip_ansi(text: str) -> str:
    """Remove the terminal color codes that a traceback holds."""
    return _ANSI.sub("", text)


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Jupyter:
    """The client of one Jupyter server at ``base_url``."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"token {token}"}
        self._session: aiohttp.ClientSession | None = None
        self._session_id = uuid.uuid4().hex

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        what: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._http().request(method, url, json=body, params=params) as resp:
                if resp.status == 404 and missing_ok:
                    return None
                if resp.status >= 400:
                    raise CodeExecutionError(
                        f"the code execution container refused to {what} ({resp.status})"
                    )
                if resp.status == 204:
                    return {}
                text = await resp.text()
                return json.loads(text) if text else {}
        except CodeExecutionError:
            raise
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
            raise CodeExecutionError(
                f"the code execution container did not answer a request to {what}"
            ) from exc

    # -- kernels -------------------------------------------------------------

    async def start_kernel(self, folder: str) -> str:
        """Start a kernel whose working directory is ``folder``, and return its id."""
        found = await self._request(
            "POST", "/api/kernels", "start a kernel", body={"name": KERNEL_NAME, "path": folder}
        )
        kernel_id = found.get("id") if isinstance(found, dict) else None
        if not isinstance(kernel_id, str) or not kernel_id:
            raise CodeExecutionError("the code execution container started no kernel")
        return kernel_id

    async def kernel_alive(self, kernel_id: str) -> bool:
        found = await self._request(
            "GET", f"/api/kernels/{quote(kernel_id)}", "read a kernel", missing_ok=True
        )
        return found is not None

    async def list_kernels(self) -> list[str]:
        found = await self._request("GET", "/api/kernels", "list the kernels")
        if not isinstance(found, list):
            return []
        return [k["id"] for k in found if isinstance(k, dict) and isinstance(k.get("id"), str)]

    async def interrupt(self, kernel_id: str) -> None:
        await self._request(
            "POST",
            f"/api/kernels/{quote(kernel_id)}/interrupt",
            "interrupt a kernel",
            missing_ok=True,
        )

    async def shutdown(self, kernel_id: str) -> None:
        await self._request(
            "DELETE", f"/api/kernels/{quote(kernel_id)}", "shut down a kernel", missing_ok=True
        )

    async def execute(self, kernel_id: str, code: str, timeout_s: float) -> Execution:
        """Run ``code`` in the kernel, and collect its output until it is idle.

        Stops reading after ``timeout_s`` seconds and returns what came, with
        ``timed_out`` set. The kernel can still be busy then: the caller
        interrupts it.
        """
        ws_url = (
            self._base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            + f"/api/kernels/{quote(kernel_id)}/channels"
        )
        msg_id = uuid.uuid4().hex
        request = {
            "header": {
                "msg_id": msg_id,
                "msg_type": "execute_request",
                "username": "joshua",
                "session": self._session_id,
                "date": datetime.now(UTC).isoformat(),
                "version": PROTOCOL_VERSION,
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
            "buffers": [],
        }
        out = Execution()
        deadline = time.monotonic() + timeout_s
        try:
            async with self._http().ws_connect(
                ws_url, params={"session_id": self._session_id}, heartbeat=20.0
            ) as ws:
                await ws.send_str(json.dumps(request))
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        out.timed_out = True
                        return out
                    try:
                        frame = await ws.receive(timeout=left)
                    except TimeoutError:
                        out.timed_out = True
                        return out
                    if frame.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise CodeExecutionError("the kernel closed the connection during the run")
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        continue
                    if _absorb(out, json.loads(frame.data), msg_id):
                        return out
        except CodeExecutionError:
            raise
        except (aiohttp.ClientError, json.JSONDecodeError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CodeExecutionError("the code execution container did not answer the run") from exc

    # -- contents ------------------------------------------------------------

    async def mkdir(self, path: str) -> None:
        await self._request(
            "PUT", f"/api/contents/{quote(path)}", "make a folder", body={"type": "directory"}
        )

    async def put_file(self, path: str, data: bytes) -> None:
        body = {
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }
        await self._request("PUT", f"/api/contents/{quote(path)}", "copy a file in", body=body)

    async def listdir(self, path: str) -> list[Entry]:
        """The entries of a folder, or an empty list when it does not exist."""
        found = await self._request(
            "GET",
            f"/api/contents/{quote(path)}",
            "list a folder",
            params={"content": "1"},
            missing_ok=True,
        )
        if not isinstance(found, dict) or not isinstance(found.get("content"), list):
            return []
        entries = []
        for item in found["content"]:
            if not isinstance(item, dict):
                continue
            size = item.get("size")
            entries.append(
                Entry(
                    name=str(item.get("name", "")),
                    path=str(item.get("path", "")),
                    type=str(item.get("type", "")),
                    size=size if isinstance(size, int) else None,
                    last_modified=_parse_time(item.get("last_modified")),
                )
            )
        return entries

    async def get_file(self, path: str) -> bytes:
        found = await self._request(
            "GET",
            f"/api/contents/{quote(path)}",
            "read a file",
            params={"content": "1", "format": "base64", "type": "file"},
        )
        content = found.get("content") if isinstance(found, dict) else None
        if not isinstance(content, str):
            raise CodeExecutionError("the code execution container gave a file with no content")
        return base64.b64decode(content)

    async def delete(self, path: str) -> None:
        await self._request(
            "DELETE", f"/api/contents/{quote(path)}", "delete a file", missing_ok=True
        )

    async def remove_tree(self, path: str) -> None:
        """Delete a folder and everything in it, one entry at a time."""
        for entry in await self.listdir(path):
            if entry.type == "directory":
                await self.remove_tree(entry.path)
            else:
                await self.delete(entry.path)
        await self.delete(path)


def _absorb(out: Execution, message: Any, msg_id: str) -> bool:
    """Add one message of the run to ``out``. True when the kernel is idle again."""
    if not isinstance(message, dict):
        return False
    parent = message.get("parent_header")
    if not isinstance(parent, dict) or parent.get("msg_id") != msg_id:
        return False
    header = message.get("header")
    kind = header.get("msg_type") if isinstance(header, dict) else message.get("msg_type")
    content = message.get("content")
    if not isinstance(content, dict):
        content = {}
    if kind == "stream":
        text = str(content.get("text", ""))
        if content.get("name") == "stderr":
            out.stderr += text
        else:
            out.stdout += text
    elif kind in ("execute_result", "display_data"):
        data = content.get("data")
        if isinstance(data, dict):
            for mime in ("image/png", "image/jpeg"):
                if isinstance(data.get(mime), str):
                    out.images.append((mime, data[mime].replace("\n", "")))
                    break
            else:
                plain = data.get("text/plain")
                if isinstance(plain, str):
                    out.results.append(plain)
    elif kind == "error":
        trace = content.get("traceback")
        if isinstance(trace, list) and trace:
            out.error = strip_ansi("\n".join(str(line) for line in trace))
        else:
            out.error = f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
    elif kind == "status" and content.get("execution_state") == "idle":
        return True
    return False
