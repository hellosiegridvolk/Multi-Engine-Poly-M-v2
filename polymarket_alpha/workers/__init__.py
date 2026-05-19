"""Long-running async ingest workers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

import aiosqlite

from polymarket_alpha.storage.repositories import runs as runs_repo

log = logging.getLogger("polymarket_alpha.workers")

T = TypeVar("T")


async def next_event(
    queue: "asyncio.Queue[T]",
    shutdown: asyncio.Event,
    timeout: float,
) -> T | None:
    """Graceful-shutdown queue read.

    Returns the next item, or ``None`` if `timeout` elapses or shutdown is
    requested. Never raises on shutdown.
    """
    if shutdown.is_set():
        return None
    getter = asyncio.ensure_future(queue.get())
    stopper = asyncio.ensure_future(shutdown.wait())
    try:
        done, pending = await asyncio.wait(
            {getter, stopper},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if getter in done:
            return getter.result()
        return None
    finally:
        for fut in (getter, stopper):
            if not fut.done():
                fut.cancel()


async def sleep_or_stop(shutdown: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True if shutdown was requested."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def run_loop(
    *,
    worker: str,
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    interval: int,
    once: bool,
    cycle: Callable[[int], Awaitable[tuple[int, int]]],
) -> None:
    """Standard worker loop.

    `cycle(run_id)` does one unit of work and returns
    ``(items_seen, items_written)``. Crashes are caught, logged to
    `ingest_runs`, and the loop continues unless shutdown is set.
    """
    log.info("worker '%s' starting (interval=%ds once=%s)", worker, interval, once)
    while not shutdown.is_set():
        started = int(time.time())
        run_id = await runs_repo.start_run(conn, worker=worker, started_ts=started)
        seen = written = 0
        errors = 0
        last_error = None
        try:
            seen, written = await cycle(run_id)
        except asyncio.CancelledError:
            await runs_repo.finish_run(
                conn,
                run_id=run_id,
                finished_ts=int(time.time()),
                items_seen=seen,
                items_written=written,
                errors_count=1,
                last_error="cancelled",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - keep worker alive
            errors = 1
            last_error = f"{type(exc).__name__}: {exc}"
            log.exception("worker '%s' cycle failed", worker)
        await runs_repo.finish_run(
            conn,
            run_id=run_id,
            finished_ts=int(time.time()),
            items_seen=seen,
            items_written=written,
            errors_count=errors,
            last_error=last_error,
        )
        log.info(
            "worker '%s' cycle done: seen=%d written=%d errors=%d",
            worker,
            seen,
            written,
            errors,
        )
        if once:
            return
        if await sleep_or_stop(shutdown, interval):
            break
    log.info("worker '%s' stopped", worker)
