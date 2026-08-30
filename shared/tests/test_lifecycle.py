import asyncio

from joshua_shared import lifecycle


async def test_runs_startup_then_shutdown_in_order() -> None:
    events: list[str] = []

    def sync_start() -> None:
        events.append("start-sync")

    async def async_start() -> None:
        events.append("start-async")

    async def async_stop() -> None:
        events.append("stop")

    lifespan = lifecycle.lifespan_with([sync_start, async_start], [async_stop])
    async with lifespan(object()):
        events.append("serving")

    assert events == ["start-sync", "start-async", "serving", "stop"]


async def test_no_hooks_is_a_noop() -> None:
    lifespan = lifecycle.lifespan_with()
    async with lifespan(object()):
        pass


async def test_shutdown_hook_failure_does_not_break_exit() -> None:
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("nope")

    def after() -> None:
        ran.append("after")

    lifespan = lifecycle.lifespan_with(shutdown=[boom, after])
    async with lifespan(object()):
        pass
    assert ran == ["after"]


async def test_shutdown_timeout_is_bounded() -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    lifespan = lifecycle.lifespan_with(shutdown=[slow], graceful_shutdown_timeout=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with lifespan(object()):
        pass
    assert loop.time() - start < 1.0
