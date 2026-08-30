"""Upstream MCP supervisor.

Each hosted upstream is owned by one supervisor task. The anyio client contexts
(stdio/http/sse) must be exited by the task that entered them, so a reload from a
request handler cannot close them directly. Instead the supervisor loops: connect
→ serve → (reload / transport error / stop) → drain in-flight calls → teardown →
reconnect. ``POST /admin/reload`` and any transport error on a forwarded call both
trigger a clean reconnect. Each connect builds a fresh ``Server`` and session
manager; the ASGI route dereferences ``self.mgr`` per request, so a swap needs no
route change and answers 503 while an upstream is down.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from joshua_shared.log import get_logger
from mcp import ClientSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

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
from joshua_gateway.proxy import build_upstream_server, upstream_streams

logger = get_logger("gateway.upstream")

_RECONNECT_BACKOFF_S = (5, 10, 30, 60)
_DRAIN_TIMEOUT_S = 10.0
_RELOAD_TIMEOUT_S = 45.0


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
        self.mgr: StreamableHTTPSessionManager | None = None
        self.error: str | None = None
        self.tool_count: int | None = None
        self.tools: list[str] = []
        # Serializes forwarded upstream calls (stdio is not safe interleaved) and
        # doubles as the drain barrier during a reload.
        self._lock = asyncio.Lock()
        self._reload = asyncio.Event()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
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

    async def stop(self) -> None:
        self._stop.set()
        self._reload.set()  # wake whichever wait the supervisor is in
        if self._task is not None:
            await self._task

    @property
    def connected(self) -> bool:
        return self.mgr is not None

    @property
    def status(self) -> str:
        """Coarse state for ``/readyz`` and ``/admin/inventory``.

        ``connecting`` = never up yet (no mgr, no error), the warm-up state;
        ``connected`` = a live session; ``error`` = the last connect failed.
        """
        if self.connected:
            return "connected"
        return "error" if self.error else "connecting"

    async def wait_connected(self, timeout: float) -> bool:  # noqa: ASYNC109 — control surface
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
        self._connected.clear()
        self._reload.set()
        await asyncio.wait_for(self._connected.wait(), timeout)

    def notify_broken(self, exc: Exception) -> None:
        """A forwarded call failed — assume the kept-alive session is dead and
        reconnect. Spurious triggers are cheap: the reconnect drains first."""
        if self._connected.is_set():
            logger.warning(
                {
                    "message": "upstream call failed; reconnecting",
                    "server": self.name,
                    "error": str(exc),
                }
            )
            self._connected.clear()
            self._reload.set()

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

    async def _supervise(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._reload.clear()
            try:
                await self._serve_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 — reconnect, never die
                self.mgr = None
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

    async def _serve_once(self) -> None:
        spec = self.spec
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(upstream_streams(spec.connect_cfg))
            session = await stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()
            server = build_upstream_server(
                self.name,
                session,
                init.capabilities,
                spec,
                self._lock,
                on_error=self.notify_broken,
                on_call=self._record_call,
            )
            mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=False)
            await stack.enter_async_context(mgr.run())

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

            self.mgr = mgr
            self.error = None
            self._connected.set()
            logger.info(
                {"message": "upstream connected", "server": self.name, "tools": self.tool_count}
            )
            try:
                await self._reload.wait()  # reload or stop (stop sets it too)
            finally:
                self._connected.clear()
                self.mgr = None
                # The bound sessions belong to the manager that is closing; drop
                # them so a reconnect starts each caller fresh.
                SESSIONS.clear_server(self.name)
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
