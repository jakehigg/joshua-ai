"""Upstream MCP supervisor.

Each hosted upstream is owned by one supervisor task. The anyio client contexts
(stdio/http/sse) must be exited by the task that entered them, so a reload from a
request handler cannot close them directly. Instead the supervisor loops: connect
→ serve → (reload / transport error / stop) → drain in-flight calls → teardown →
reconnect. ``POST /admin/reload`` and a lost upstream session both trigger a
clean reconnect.

The downstream side is separate from the upstream connection. One ``Server`` and
one session manager serve core for as long as the upstream keeps the same
capabilities, and its handlers read the live upstream session per call through
``Upstream.forward``. A reconnect therefore does not close the downstream
session: a call that arrives during a reconnect waits for it (bounded), and a
call that finds the upstream session gone reconnects and is sent once more when
it is safe to repeat.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import anyio
import mcp_types as types
from joshua_shared.log import get_logger
from mcp import ClientSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from joshua_gateway import mcp_store
from joshua_gateway.catalog import ServerSpec
from joshua_gateway.observability import (
    CALL_LOG,
    CALLERS,
    SESSIONS,
    conversation_ctx,
    identity_ctx,
    now_iso,
    person_ctx,
)
from joshua_gateway.proxy import UpstreamUnavailableError, build_upstream_server, upstream_streams

logger = get_logger("gateway.upstream")

_RECONNECT_BACKOFF_S = (5, 10, 30, 60)
_DRAIN_TIMEOUT_S = 10.0
_RELOAD_TIMEOUT_S = 45.0
# How long a downstream call waits for a reconnect before it gets an error.
SESSION_WAIT_S = 10.0

# The two ways an upstream can lose a session.
#
# ``rejected``: the upstream refused the session id (HTTP 404 "Session not
# found", "invalid or expired session ID"). It refused before it read the
# request, so the request did not run, and it is safe to send it again.
#
# ``closed``: the transport closed. A request that was in flight can have run,
# so only a request that changes nothing is sent again.
REJECTED = "rejected"
CLOSED = "closed"

_SESSION_WORDS = ("not found", "expired", "terminated", "invalid", "unknown")


def session_error_kind(exc: BaseException) -> str | None:
    """Return ``rejected`` or ``closed`` when ``exc`` says the upstream session is
    gone, else None. A JSON-RPC error that the upstream sent for any other reason
    is None: the session answered, so it is alive."""
    if isinstance(exc, MCPError):
        message = (exc.error.message or "").lower()
        if "session" in message and any(word in message for word in _SESSION_WORDS):
            return REJECTED
        if exc.error.code == types.CONNECTION_CLOSED:
            return CLOSED
        return None
    if isinstance(exc, anyio.ClosedResourceError | anyio.BrokenResourceError | anyio.EndOfStream):
        return CLOSED
    return None


class Upstream:
    """One hosted upstream MCP server, its connection owned by a supervisor task."""

    def __init__(
        self,
        spec: ServerSpec,
        *,
        name: str | None = None,
        label: str | None = None,
        person: str | None = None,
    ):
        # ``name`` is the instance name (``spotify:alex`` for an identity server);
        # ``label`` is the server route it belongs to (``spotify``); ``person`` is
        # the person this instance serves, or None for a shared instance.
        self.name = name or spec.name
        self.label = label or spec.name
        self.person = person
        self.spec = spec
        # The downstream session manager that core talks to. It lives across
        # upstream reconnects, and changes only when the capabilities change.
        self.mgr: StreamableHTTPSessionManager | None = None
        self._mgr_caps: tuple[bool, bool, bool] | None = None
        self._mgr_stop: asyncio.Event | None = None
        self._mgr_task: asyncio.Task | None = None
        # The live upstream session. None while down.
        self.session: ClientSession | None = None
        # Counts each connect, so a caller can wait for a session newer than
        # the one that failed.
        self._generation = 0
        # True from a reload or a lost session until the next connect ends,
        # with a session or with an error. A downstream call waits only then.
        self._reconnecting = False
        self._changed = asyncio.Event()
        self.reconnects = 0
        self.last_reconnect_at: str | None = None
        self.last_reconnect_reason: str | None = None
        self.error: str | None = None
        self.tool_count: int | None = None
        self.tools: list[str] = []
        # A `package` entry: the install record once it is in the store, and the
        # two transient states the supervisor passes through.
        self.install: dict[str, Any] | None = None
        self.installing = False
        self.disabled: str | None = None
        # Serializes forwarded upstream calls (stdio is not safe interleaved) and
        # doubles as the drain barrier during a reload.
        self._lock = asyncio.Lock()
        self._reload = asyncio.Event()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        # Set whenever no session is live. `disable` waits on it, so a reload
        # that turns an entry off returns with the entry already off.
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task | None = None

    # -- public control surface ---------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"upstream-{self.name}")

    def update_spec(self, spec: ServerSpec) -> None:
        """Swap the resolved spec. The next connect uses the new connect config and
        tool filter; call ``reload`` to apply it now."""
        self.spec = spec

    def restart_needed(self, spec: ServerSpec) -> bool:
        """True when the session must reconnect to apply ``spec``: a connect config
        or tool filter change. Reserved policy fields alone need no restart."""
        return (
            spec.connect_cfg != self.spec.connect_cfg
            or spec.transport != self.spec.transport
            or spec.tool_filter != self.spec.tool_filter
        )

    async def disable(self, timeout: float = _DRAIN_TIMEOUT_S) -> None:  # noqa: ASYNC109
        """Turn this instance off and return once its session is gone.

        For a spec that cannot connect at all, such as an entry whose credential
        is now empty. The supervisor keeps the instance and picks it up again on
        the reload after the credential is set.
        """
        self.disabled = self.spec.disabled_reason
        self._drop_session()
        self._reload.set()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            logger.warning(
                {"message": "disable timed out; session still closing", "server": self.name}
            )

    async def stop(self) -> None:
        self._stop.set()
        self._reload.set()  # wake whichever wait the supervisor is in
        if self._task is not None:
            await self._task
        self._reconnecting = False
        self._notify()
        await self._close_mgr()

    @property
    def connected(self) -> bool:
        return self.session is not None

    @property
    def serving(self) -> bool:
        """True when a downstream request can be answered now or after a short
        wait: the upstream is connected, or a reconnect is in progress."""
        return self.mgr is not None and (self.connected or self._reconnecting)

    @property
    def status(self) -> str:
        """Coarse state for ``/readyz`` and ``/admin/inventory``.

        ``connecting`` = never up yet (no mgr, no error), the warm-up state;
        ``connected`` = a live session; ``error`` = the last connect or install
        failed; ``installing`` = the package install is running; ``disabled`` =
        a configured credential is empty, so the entry never starts.
        """
        if self.connected:
            return "connected"
        if self.disabled is not None:
            return "disabled"
        if self.installing:
            return "installing"
        return "error" if self.error else "connecting"

    async def wait_connected(self, timeout: float) -> bool:  # noqa: ASYNC109 — control surface
        if self.spec.disabled_reason is not None:
            return False  # it will never connect, so do not hold the boot open
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def reload(self, timeout: float = _RELOAD_TIMEOUT_S) -> None:  # noqa: ASYNC109 — control surface
        """Tear down and re-establish the session; return once it is live again.

        Raises ``TimeoutError`` if it is not up in time; the supervisor keeps
        retrying in the background regardless.
        """
        self._reconnecting = True
        self._drop_session()
        self._reload.set()
        await asyncio.wait_for(self._connected.wait(), timeout)

    def notify_broken(self, exc: BaseException) -> None:
        """A forwarded call failed: treat the session as dead and reconnect."""
        self._reconnect(self._generation, str(exc) or type(exc).__name__)

    def _reconnect(self, generation: int, reason: str) -> None:
        """Start one reconnect for the session of ``generation``.

        A second failure on the same session, from a concurrent call, does
        nothing: the reconnect is already in progress, or already done.
        """
        if generation != self._generation or self.session is None:
            return
        self.reconnects += 1
        self.last_reconnect_at = now_iso()
        self.last_reconnect_reason = reason
        logger.warning({"message": "upstream reconnecting", "server": self.name, "reason": reason})
        self._reconnecting = True
        self._drop_session()
        self._reload.set()

    def _drop_session(self) -> None:
        self._connected.clear()
        self.session = None
        self._notify()

    def _notify(self) -> None:
        """Wake every call that waits for the session state to change."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def _wait_session(self, after: int | None = None) -> tuple[ClientSession, int]:
        """Return the live session and its generation.

        With ``after``, return only a session newer than that generation. Wait up
        to ``SESSION_WAIT_S`` while a reconnect is in progress. Raise
        ``UpstreamUnavailableError`` when there is no session and none is coming.
        """
        deadline = asyncio.get_running_loop().time() + SESSION_WAIT_S
        while True:
            session = self.session
            if session is not None and (after is None or self._generation != after):
                return session, self._generation
            if not self._reconnecting and session is None:
                raise UpstreamUnavailableError(
                    f"upstream {self.label} is unavailable; try again later"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise UpstreamUnavailableError(f"upstream {self.label} is reconnecting; try again")
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                pass

    async def _send(self, session: ClientSession, fn: Callable[[ClientSession], Awaitable[Any]]):
        # The lock serializes calls (stdio is not safe interleaved) and is the
        # drain barrier of a reconnect.
        async with self._lock:
            return await fn(session)

    async def forward(
        self,
        fn: Callable[[ClientSession], Awaitable[Any]],
        *,
        repeatable: bool,
    ) -> Any:
        """Send one request to the upstream session, and recover a lost session.

        When the upstream rejected the session id, the request did not run: the
        gateway reconnects and sends it once more. When the transport closed,
        the gateway reconnects, and sends it again only if ``repeatable`` (the
        request changes nothing, such as a list). Any other error goes back to
        the caller as it is. A second failure is ``UpstreamUnavailableError``.
        """
        session, generation = await self._wait_session()
        try:
            return await self._send(session, fn)
        except ValidationError:
            raise  # a result that does not match the schema; the session is fine
        except Exception as exc:
            kind = session_error_kind(exc)
            if kind is None:
                if not isinstance(exc, MCPError):
                    # Not a JSON-RPC answer, so the transport is in doubt.
                    self._reconnect(generation, str(exc) or type(exc).__name__)
                raise
            self._reconnect(generation, str(exc) or kind)
            if kind == CLOSED and not repeatable:
                raise UpstreamUnavailableError(
                    f"upstream {self.label} closed the connection during the call, "
                    "so the call may have run; it is reconnecting. Check before you try again"
                ) from exc
        session, generation = await self._wait_session(after=generation)
        try:
            return await self._send(session, fn)
        except ValidationError:
            raise
        except Exception as exc:
            kind = session_error_kind(exc)
            if kind is None and isinstance(exc, MCPError):
                raise
            self._reconnect(generation, str(exc) or kind or type(exc).__name__)
            raise UpstreamUnavailableError(
                f"upstream {self.label} is reconnecting; try again"
            ) from exc

    def _record_call(self, tool: str, duration_ms: float, status: str, error: str | None) -> None:
        """``on_call`` hook: attribute the completed call to the request identity
        and add it to the call log ring plus a structured log line."""
        identity = identity_ctx.get()
        entry = {
            "ts": now_iso(),
            "identity": identity,
            "person": person_ctx.get(),
            "conversation": conversation_ctx.get(),
            "server": self.label,
            "tool": tool,
            "duration_ms": round(duration_ms, 1),
            "status": status,
            "error": error,
        }
        CALL_LOG.record(entry)
        CALLERS.record_call(identity)
        logger.info({"message": "tool call", **entry})

    # -- supervisor ----------------------------------------------------------

    async def _ensure_installed(self) -> None:
        """Install this entry's package, unless the store already holds this spec.

        Runs before the first connect and again after a spec change. The install
        is off the event loop, so the other entries keep serving while it runs.
        """
        request = mcp_store.InstallRequest.from_connect_cfg(self.label, self.spec.connect_cfg)
        if request is None:
            self.install = None
            return
        # Read the store first, so a start on a store that already holds this
        # spec neither logs an install nor reports `installing` in /readyz.
        record = self.install or mcp_store.read_record(self.label)
        if mcp_store.is_current(request, record):
            self.install = record
            return
        self.installing = True
        logger.info(
            {"message": "installing mcp package", "server": self.name, "package": request.spec.raw}
        )
        try:
            self.install = await mcp_store.ensure_async(request)
        finally:
            self.installing = False

    def _resolved_cfg(self) -> dict[str, Any]:
        """The connect config with the install's own command and PATH filled in."""
        cfg = dict(self.spec.connect_cfg)
        if self.install is None:
            return cfg
        cfg["command"] = mcp_store.resolve_command(self.label, self.install, cfg.get("command"))
        cfg["cwd"] = str(mcp_store.entry_dir(self.label))
        cfg["path_prepend"] = [str(p) for p in mcp_store.bin_paths(self.label, self.install)]
        return cfg

    async def _supervise(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._reload.clear()
            self._idle.set()
            reason = self.spec.disabled_reason
            if reason is not None:
                # Not an error and not a retry: the entry has no credential to
                # use. It starts on the next reload, when the value is set.
                if self.disabled != reason:
                    logger.warning(
                        {
                            "message": "upstream disabled: the credential is empty",
                            "server": self.name,
                            "detail": reason,
                        }
                    )
                self.disabled = reason
                self.error = None
                self._reconnecting = False
                self._notify()
                await self._reload.wait()
                continue
            self.disabled = None
            try:
                await self._ensure_installed()
                await self._serve_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 — reconnect, never die
                self.session = None
                self._reconnecting = False
                self._notify()
                self.error = str(exc)
                delay = _RECONNECT_BACKOFF_S[min(failures, len(_RECONNECT_BACKOFF_S) - 1)]
                failures += 1
                logger.error(
                    {
                        "message": "upstream connect failed",
                        "server": self.name,
                        "error": str(exc),
                        "retry_in_s": delay,
                    }
                )
                try:
                    await asyncio.wait_for(self._reload.wait(), delay)
                except TimeoutError:
                    pass

    async def _ensure_mgr(self, caps: Any) -> None:
        """Keep the downstream session manager, or replace it when the upstream
        now has other capabilities. The manager runs in its own task, so it
        outlives each upstream connection."""
        key = (
            bool(caps and caps.tools),
            bool(caps and caps.resources),
            bool(caps and caps.prompts),
        )
        if self.mgr is not None and self._mgr_caps == key:
            return
        server = build_upstream_server(self.name, self, caps, on_call=self._record_call)
        mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=False)
        ready, stop = asyncio.Event(), asyncio.Event()

        async def host() -> None:
            async with mgr.run():
                ready.set()
                await stop.wait()

        task = asyncio.create_task(host(), name=f"downstream-{self.name}")
        await ready.wait()
        await self._close_mgr()
        self.mgr, self._mgr_caps, self._mgr_stop, self._mgr_task = mgr, key, stop, task

    async def _close_mgr(self) -> None:
        stop, task = self._mgr_stop, self._mgr_task
        self.mgr = None
        self._mgr_caps = self._mgr_stop = self._mgr_task = None
        if stop is not None and task is not None:
            stop.set()
            await task
            # The bound sessions belong to the manager that closed; drop them
            # so the next request starts each caller fresh.
            SESSIONS.clear_server(self.name)

    async def _serve_once(self) -> None:
        spec = self.spec
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(
                upstream_streams(self._resolved_cfg(), name=self.name)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()

            # Count the filtered tools now — validates the session works past
            # initialize and feeds /readyz and /admin/inventory.
            self.tool_count = None
            self.tools = []
            if init.capabilities and init.capabilities.tools:
                tools = (await session.list_tools()).tools
                names = [t.name for t in tools if spec.tool_filter.permits(t.name)]
                self.tools = names
                self.tool_count = len(names)
                # A configured allow pattern that matches nothing is usually a typo.
                unmatched = spec.tool_filter.unmatched_allow([t.name for t in tools])
                if unmatched:
                    logger.warning(
                        {
                            "message": "tool allow patterns match no upstream tool",
                            "server": self.name,
                            "patterns": unmatched,
                        }
                    )

            await self._ensure_mgr(init.capabilities)
            self.session = session
            self._generation += 1
            self._reconnecting = False
            self.error = None
            self._idle.clear()
            self._connected.set()
            self._notify()
            logger.info(
                {"message": "upstream connected", "server": self.name, "tools": self.tool_count}
            )
            try:
                await self._reload.wait()  # reload or stop (stop sets it too)
            finally:
                self._drop_session()
                self._idle.set()
                # Drain in-flight forwarded calls before closing the session; a
                # hung call must not wedge the reload forever.
                try:
                    await asyncio.wait_for(self._lock.acquire(), _DRAIN_TIMEOUT_S)
                    self._lock.release()
                except TimeoutError:
                    logger.warning(
                        {"message": "drain timed out; closing anyway", "server": self.name}
                    )
            logger.info({"message": "upstream session closed", "server": self.name})
