"""The sandbox MCP server, hosted in-process by the gateway as the ``sandbox`` builtin.

It runs Python for the agent in the sandbox container: a stock Jupyter server
in a container of its own, with no network except the one route from the
gateway. The agent counts, filters, joins and charts data there, and does not
do arithmetic in its head.

**One kernel for each conversation.** The first ``run_python`` of a
conversation starts a kernel in the folder of that conversation, and the next
calls use the same kernel. A table that one call loads is still loaded in the
next call. ``reset_python`` shuts the kernel down and removes the folder. The
Jupyter server shuts down a kernel that is idle for too long
(``docs/sandbox.md``). A request with no conversation gets a kernel for that
one call.

**Files.** ``files`` names files that the person may read: a path of the files
server (``wiki/...``, ``people/...``, ``shared/...``) or a document
(``documents/<owner>/<slug>.md``). Each one is copied into ``in/`` of the
folder before the code runs. The code writes what it makes into ``out/``. The
result gives back each file of ``out/``: an image as an image, a text file as
text, and any other file by its name and size. ``out/`` is empty at the start
of each run.

**Access.** Members only, as for documents: a guest, and a request with no
role, gets no tools. The person and the role come from the request, never from
a tool argument. With ``sandbox.enabled: false`` the list of tools is empty.

**What the sandbox cannot do.** It has no route to the network, and it holds no
credential except its own Jupyter token. It sees only the files that a call
copies in. Every kernel runs in the same container, so one conversation's code
can read the folder of another conversation. ``docs/security.md`` records
this as a known limitation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import posixpath
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import mcp_types as types
from joshua_shared import layout
from joshua_shared.config import SandboxConfig, is_member
from joshua_shared.log import get_logger
from mcp.server.lowlevel import Server

from joshua_gateway.files_mcp.paths import MEMBER, UNKNOWN, PathError, resolve
from joshua_gateway.files_mcp.paths import roots as roots_for_role
from joshua_gateway.observability import conversation_ctx, identity_ctx, person_ctx, role_ctx
from joshua_gateway.sandbox_mcp.jupyter import Execution, Jupyter, SandboxError

_logger = get_logger("sandbox_mcp")

# The environment variable of the gateway that holds the Jupyter token.
TOKEN_ENV = "SANDBOX_TOKEN"
# The folder under the Jupyter root that holds one folder for each conversation.
CONVERSATIONS = "conversations"
INPUT = "in"
OUTPUT = "out"
# The longest code of one run, in characters.
MAX_CODE_CHARS = 100_000
# The most input files of one run, and the most output files that come back.
MAX_FILES = 20
MAX_OUTPUTS = 10
# The largest image that comes back from out/, in bytes.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# The most images that come back from one run, displays and out/ together.
MAX_IMAGES = 6
# After an interrupt, the kernel has this many seconds to become idle.
INTERRUPT_GRACE_S = 5.0
# The sweep of old conversation folders runs at most this often.
SWEEP_EVERY_S = 600.0

_TEXT_SUFFIXES = frozenset(
    {".csv", ".tsv", ".txt", ".md", ".json", ".html", ".svg", ".xml", ".py", ".log"}
)
_IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_MEMBERS_ONLY = "the sandbox is for members"
_OFF = "the sandbox is off: sandbox.enabled is false"
_NO_TOKEN = f"the sandbox has no token: set {TOKEN_ENV} in the environment of the gateway"

TOOLS = [
    types.Tool(
        name="run_python",
        description=(
            "Run Python 3 code in the sandbox, a container with no network. Use it "
            "for any count, filter, sort, comparison, join, or total of more than a "
            "few rows, and for a chart. pandas, numpy, scipy, matplotlib, seaborn, "
            "openpyxl and scikit-learn are installed; pip cannot install more. The "
            "kernel stays alive for this conversation: a variable or a table from "
            "one call is there in the next. files is a list of paths to copy into "
            "in/ before the code runs: a files path (wiki/..., people/..., "
            "shared/...) or a document (documents/<owner>/<slug>.md). The result "
            "says the name of each copy. Write each file you make into out/; the "
            "result gives back each one, an image as an image and a text file as "
            "text. out/ is empty at the start of each run. Print what you need to "
            "see: the result holds stdout, stderr, the value of the last line, and "
            "the traceback of an error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}, "default": []},
                "timeout_s": {
                    "type": "integer",
                    "description": "stop the run after this many seconds",
                },
            },
            "required": ["code"],
        },
    ),
    types.Tool(
        name="reset_python",
        description=(
            "Shut down the Python kernel of this conversation and remove its files. "
            "The next run_python starts clean. Use it when the kernel is stuck or "
            "its state is wrong."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
]


@dataclass
class _Slot:
    """The kernel of one conversation."""

    kernel_id: str
    folder: str
    last_used: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class _Request:
    person: str | None
    member: bool
    conversation: str | None


class Sandbox:
    """The kernels of the conversations, and one run at a time for each of them."""

    def __init__(self, settings: SandboxConfig, client: Jupyter, root_dir: Path) -> None:
        self.settings = settings
        self.client = client
        self.root_dir = root_dir
        self._slots: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        # None until the first sweep, which runs at the first call.
        self._last_sweep: float | None = None

    # -- kernels -------------------------------------------------------------

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def _prepare(self, folder: str) -> None:
        for path in (CONVERSATIONS, folder, f"{folder}/{INPUT}", f"{folder}/{OUTPUT}"):
            await self.client.mkdir(path)

    async def _slot(self, key: str, folder: str) -> _Slot:
        """The live kernel of ``key``, started when there is none."""
        slot = self._slots.get(key)
        if slot is not None and await self.client.kernel_alive(slot.kernel_id):
            slot.last_used = time.monotonic()
            return slot
        self._slots.pop(key, None)
        async with self._start_lock:
            while len(self._slots) >= self.settings.max_kernels:
                oldest = min(self._slots, key=lambda k: self._slots[k].last_used)
                await self._drop(oldest, remove_folder=False)
            await self._prepare(folder)
            kernel_id = await self.client.start_kernel(folder)
            slot = self._slots[key] = _Slot(kernel_id=kernel_id, folder=folder)
        return slot

    async def _drop(self, key: str, *, remove_folder: bool) -> None:
        slot = self._slots.pop(key, None)
        if slot is None:
            return
        await self.client.shutdown(slot.kernel_id)
        if remove_folder:
            await self.client.remove_tree(slot.folder)

    async def sweep(self) -> None:
        """Remove the conversation folders that no kernel used for the TTL."""
        now = time.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < SWEEP_EVERY_S:
            return
        self._last_sweep = now
        live = {slot.folder for slot in self._slots.values()}
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.folder_ttl_hours)
        for entry in await self.client.listdir(CONVERSATIONS):
            if entry.type != "directory" or entry.path in live:
                continue
            if entry.last_modified is not None and entry.last_modified < cutoff:
                await self.client.remove_tree(entry.path)

    # -- the tools -----------------------------------------------------------

    async def run(self, req: _Request, args: dict[str, Any]) -> types.CallToolResult:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise SandboxError("code is empty")
        if len(code) > MAX_CODE_CHARS:
            raise SandboxError(f"code is longer than {MAX_CODE_CHARS} characters")
        paths = args.get("files") or []
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise SandboxError("files must be a list of paths")
        if len(paths) > MAX_FILES:
            raise SandboxError(f"a run takes at most {MAX_FILES} files")
        timeout = _timeout(args.get("timeout_s"), self.settings.run_timeout_s)
        inputs = _read_inputs(req, paths, self.root_dir, self.settings.max_input_bytes)

        await self.sweep()
        once = req.conversation is None
        key = req.conversation or f"once-{uuid.uuid4().hex}"
        folder = f"{CONVERSATIONS}/{_folder_name(key)}"
        started = time.monotonic()
        async with self._lock(key):
            slot = await self._slot(key, folder)
            try:
                for entry in await self.client.listdir(f"{folder}/{OUTPUT}"):
                    if entry.type == "directory":
                        await self.client.remove_tree(entry.path)
                    else:
                        await self.client.delete(entry.path)
                copied = []
                for name, data, source in inputs:
                    await self.client.put_file(f"{folder}/{INPUT}/{name}", data)
                    copied.append((source, f"{INPUT}/{name}"))
                result = await self.client.execute(slot.kernel_id, code, timeout)
                restarted = False
                if result.timed_out:
                    restarted = await self._stop(key, slot)
                outputs = [] if restarted else await self._outputs(folder)
            finally:
                if once:
                    await self._drop(key, remove_folder=True)
        _logger.info(
            {
                "message": "sandbox run",
                "files": len(inputs),
                "outputs": len(outputs),
                "error": result.error is not None,
                "timed_out": result.timed_out,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return _result(result, copied, outputs, timeout, restarted, self.settings.max_output_chars)

    async def _stop(self, key: str, slot: _Slot) -> bool:
        """Interrupt a run past its time. True when the kernel had to be shut down."""
        await self.client.interrupt(slot.kernel_id)
        check = await self.client.execute(slot.kernel_id, "pass", INTERRUPT_GRACE_S)
        if not check.timed_out:
            return False
        await self._drop(key, remove_folder=False)
        return True

    async def _outputs(self, folder: str) -> list[tuple[str, int, bytes | None]]:
        """``(name, size, data)`` of each file in ``out/``. ``data`` is None for a
        file that does not come back whole."""
        found = []
        for entry in await self.client.listdir(f"{folder}/{OUTPUT}"):
            if entry.type == "directory":
                continue
            if len(found) >= MAX_OUTPUTS:
                found.append((entry.name, entry.size or 0, None))
                continue
            suffix = PurePosixPath(entry.name).suffix.lower()
            size = entry.size or 0
            wanted = suffix in _TEXT_SUFFIXES or (
                suffix in _IMAGE_SUFFIXES and size <= MAX_IMAGE_BYTES
            )
            data = await self.client.get_file(entry.path) if wanted else None
            found.append((entry.name, size if data is None else len(data), data))
        return found

    async def reset(self, req: _Request) -> types.CallToolResult:
        if req.conversation is None:
            return _text("this request has no conversation, so it has no kernel")
        async with self._lock(req.conversation):
            had = req.conversation in self._slots
            folder = f"{CONVERSATIONS}/{_folder_name(req.conversation)}"
            await self._drop(req.conversation, remove_folder=True)
            if not had:
                await self.client.remove_tree(folder)
        return _text("the kernel of this conversation is shut down and its files are gone")


def _folder_name(key: str) -> str:
    """The folder of a conversation: a hash, so the folder names no person."""
    return "c-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _timeout(raw: Any, limit: int) -> float:
    if raw is None:
        return float(limit)
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        raise SandboxError("timeout_s must be a positive number")
    return float(min(raw, limit))


def _read_inputs(
    req: _Request, paths: list[str], root_dir: Path, max_bytes: int
) -> list[tuple[str, bytes, str]]:
    """Read every input file that this request may read: ``(name, data, path)``.

    A files path goes through ``files_mcp.paths.resolve`` with the root set of
    the role. A document path must name the person's own folder or ``shared/``.
    """
    roots = roots_for_role(root_dir, role=MEMBER)
    out: list[tuple[str, bytes, str]] = []
    taken: set[str] = set()
    total = 0
    for raw in paths:
        path = raw.strip()
        if path.startswith(f"{layout.DOCUMENTS}/"):
            abs_path = _document_path(req, path, root_dir)
        else:
            try:
                _, abs_path = resolve(path, roots, write=False)
            except PathError as exc:
                raise SandboxError(f"{path}: {exc.message}") from exc
        if not abs_path.is_file():
            raise SandboxError(f"{path}: file not found")
        size = abs_path.stat().st_size
        total += size
        if total > max_bytes:
            raise SandboxError(f"the files of one run are larger than {max_bytes} bytes")
        out.append((_unique(abs_path.name, taken), abs_path.read_bytes(), path))
    return out


def _document_path(req: _Request, path: str, root_dir: Path) -> Path:
    parts = PurePosixPath(path).parts
    if len(parts) != 3 or ".." in parts or "\x00" in path:
        raise SandboxError(f"{path}: a document path is documents/<owner>/<slug>.md")
    owner, name = parts[1], parts[2]
    own = req.person if req.person and req.person != UNKNOWN else None
    if owner not in (own, layout.DOCUMENTS_SHARED):
        raise SandboxError(f"{path}: that folder is not yours")
    if name.startswith(".") or not name.endswith((".md", ".html")):
        raise SandboxError(f"{path}: a document path is documents/<owner>/<slug>.md")
    base = Path(os.path.realpath(layout.documents_root(root_dir)))
    abs_path = layout.documents_owner_dir(owner, root_dir) / name
    if not Path(os.path.realpath(abs_path)).is_relative_to(base):
        raise SandboxError(f"{path}: path escapes documents")
    return abs_path


def _unique(name: str, taken: set[str]) -> str:
    stem, suffix = posixpath.splitext(name)
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    taken.add(candidate)
    return candidate


def _cut(text: str, budget: list[int]) -> str:
    """Cut ``text`` to what is left of the shared budget, and say so."""
    if len(text) <= budget[0]:
        budget[0] -= len(text)
        return text
    kept = text[: max(budget[0], 0)]
    budget[0] = 0
    return f"{kept}\n[... cut: {len(text) - len(kept)} more characters]"


def _result(
    run: Execution,
    copied: list[tuple[str, str]],
    outputs: list[tuple[str, int, bytes | None]],
    timeout: float,
    restarted: bool,
    max_chars: int,
) -> types.CallToolResult:
    budget = [max_chars]
    parts: list[str] = []
    if copied:
        parts.append("files:\n" + "\n".join(f"- {src} -> {dst}" for src, dst in copied))
    if run.timed_out:
        if restarted:
            parts.append(
                f"timed out after {timeout:g} s; the kernel did not stop, so it was "
                "shut down and its variables are gone"
            )
        else:
            parts.append(f"timed out after {timeout:g} s; the run was interrupted")
    if run.stdout:
        parts.append("stdout:\n" + _cut(run.stdout.rstrip(), budget))
    if run.stderr:
        parts.append("stderr:\n" + _cut(run.stderr.rstrip(), budget))
    for value in run.results:
        parts.append("result:\n" + _cut(value, budget))
    if run.error:
        parts.append("error:\n" + _cut(run.error, budget))
    images: list[types.ImageContent] = []
    for mime, data in run.images:
        if len(images) < MAX_IMAGES:
            images.append(types.ImageContent(type="image", data=data, mime_type=mime))
    for name, size, data in outputs:
        suffix = PurePosixPath(name).suffix.lower()
        if data is None:
            parts.append(f"out/{name}: {size} bytes, not shown")
        elif suffix in _IMAGE_SUFFIXES:
            if len(images) < MAX_IMAGES:
                images.append(
                    types.ImageContent(
                        type="image",
                        data=base64.b64encode(data).decode("ascii"),
                        mime_type=_IMAGE_SUFFIXES[suffix],
                    )
                )
                parts.append(f"out/{name}: image, {size} bytes, shown below")
            else:
                parts.append(f"out/{name}: image, {size} bytes, not shown")
        else:
            text = data.decode("utf-8", errors="replace")
            parts.append(f"out/{name} ({size} bytes):\n" + _cut(text, budget))
    if not parts and not images:
        parts.append("no output")
    content: list[Any] = [types.TextContent(type="text", text="\n\n".join(parts))]
    content.extend(images)
    return types.CallToolResult(content=content)


def _text(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _role_from_config(person: str) -> str | None:
    from joshua_shared import config

    entry = config.load().person(person)
    return entry.role if entry is not None else None


# One Sandbox for each Jupyter URL, so a rebuild of the server keeps the kernels.
_SANDBOXES: dict[str, Sandbox] = {}


def build_sandbox_server(
    root_dir: Path,
    settings: SandboxConfig,
    *,
    token: str | None = None,
    role_for: Callable[[str], str | None] | None = None,
    client: Jupyter | None = None,
) -> Server:
    """Build the sandbox ``Server`` for a data volume at ``root_dir``.

    ``token`` defaults to ``SANDBOX_TOKEN`` from the environment. ``client`` is
    a seam for the tests.
    """
    roles = role_for or _role_from_config
    secret = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    sandbox: Sandbox | None = None
    if settings.enabled and secret:
        if client is not None:
            sandbox = Sandbox(settings, client, root_dir)
        else:
            sandbox = _SANDBOXES.get(settings.url)
            if sandbox is None:
                sandbox = Sandbox(settings, Jupyter(settings.url, secret), root_dir)
                _SANDBOXES[settings.url] = sandbox

    def request() -> _Request:
        person = person_ctx.get()
        role = role_ctx.get()
        if role is None and person is not None and person != UNKNOWN:
            role = roles(person)
        return _Request(person=person, member=is_member(role), conversation=conversation_ctx.get())

    async def on_list_tools(ctx, params):
        if not settings.enabled:
            return types.ListToolsResult(tools=[])
        # The gateway lists the tools once at connect, with no request, for the
        # inventory. Every request of a person who is not a member sees none.
        if identity_ctx.get() == UNKNOWN and person_ctx.get() is None and role_ctx.get() is None:
            return types.ListToolsResult(tools=TOOLS)
        return types.ListToolsResult(tools=TOOLS if request().member else [])

    async def on_call_tool(ctx, params):
        args = params.arguments or {}
        try:
            if not settings.enabled:
                raise SandboxError(_OFF)
            req = request()
            if not req.member:
                raise SandboxError(_MEMBERS_ONLY)
            if sandbox is None:
                raise SandboxError(_NO_TOKEN)
            if params.name == "run_python":
                return await sandbox.run(req, args)
            if params.name == "reset_python":
                return await sandbox.reset(req)
            raise SandboxError(f"unknown tool: {params.name}")
        except SandboxError as exc:
            return _error(exc.message)

    return Server("sandbox", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
