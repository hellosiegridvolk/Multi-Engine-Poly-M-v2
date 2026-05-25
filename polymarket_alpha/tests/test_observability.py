"""Worker run_loop integration + Prometheus metrics rendering."""

import asyncio

from polymarket_alpha.helpers.metrics import render_prometheus
from polymarket_alpha.storage.repositories import runs as runs_repo
from polymarket_alpha.workers import next_event, run_loop


# ---- run_loop ----

async def test_run_loop_once_single_cycle(db_conn):
    calls: list[int] = []

    async def cycle(run_id: int) -> tuple[int, int]:
        calls.append(run_id)
        return (5, 3)

    shutdown = asyncio.Event()
    await run_loop(
        worker="t", conn=db_conn, shutdown=shutdown,
        interval=1, once=True, cycle=cycle,
    )
    assert len(calls) == 1
    row = await (
        await db_conn.execute(
            "SELECT worker, items_seen, items_written, errors_count, finished_ts "
            "FROM ingest_runs"
        )
    ).fetchone()
    assert row["worker"] == "t"
    assert row["items_seen"] == 5
    assert row["items_written"] == 3
    assert row["errors_count"] == 0
    assert row["finished_ts"] is not None


async def test_run_loop_catches_cycle_error_and_records_it(db_conn):
    async def cycle(run_id: int) -> tuple[int, int]:
        raise RuntimeError("simulated cycle boom")

    shutdown = asyncio.Event()
    # Must NOT raise — a failing cycle is logged, not propagated.
    await run_loop(
        worker="t", conn=db_conn, shutdown=shutdown,
        interval=1, once=True, cycle=cycle,
    )
    row = await (
        await db_conn.execute(
            "SELECT errors_count, last_error FROM ingest_runs"
        )
    ).fetchone()
    assert row["errors_count"] == 1
    assert "simulated cycle boom" in row["last_error"]


async def test_run_loop_shutdown_preset_skips_cycle(db_conn):
    calls: list[int] = []

    async def cycle(run_id: int) -> tuple[int, int]:
        calls.append(run_id)
        return (0, 0)

    shutdown = asyncio.Event()
    shutdown.set()  # already requested before the loop starts
    await run_loop(
        worker="t", conn=db_conn, shutdown=shutdown,
        interval=1, once=False, cycle=cycle,
    )
    assert calls == []


async def test_run_loop_continuous_stops_on_shutdown(db_conn):
    calls: list[int] = []
    shutdown = asyncio.Event()

    async def cycle(run_id: int) -> tuple[int, int]:
        calls.append(run_id)
        shutdown.set()  # request stop after the first cycle
        return (1, 1)

    await run_loop(
        worker="t", conn=db_conn, shutdown=shutdown,
        interval=1, once=False, cycle=cycle,
    )
    assert len(calls) == 1  # looped once, then the shutdown was honored


# ---- next_event ----

async def test_next_event_returns_item():
    q: asyncio.Queue = asyncio.Queue()
    await q.put("x")
    shutdown = asyncio.Event()
    assert await next_event(q, shutdown, timeout=1.0) == "x"


async def test_next_event_timeout_returns_none():
    q: asyncio.Queue = asyncio.Queue()
    shutdown = asyncio.Event()
    assert await next_event(q, shutdown, timeout=0.05) is None


async def test_next_event_shutdown_returns_none():
    q: asyncio.Queue = asyncio.Queue()
    shutdown = asyncio.Event()
    shutdown.set()
    assert await next_event(q, shutdown, timeout=5.0) is None


# ---- metrics ----

async def test_render_prometheus_shape(db_conn):
    rid = await runs_repo.start_run(db_conn, worker="leaderboard", started_ts=1000)
    await runs_repo.finish_run(
        db_conn, run_id=rid, finished_ts=1100,
        items_seen=10, items_written=8, errors_count=0,
    )
    rid2 = await runs_repo.start_run(db_conn, worker="activity", started_ts=2000)
    await runs_repo.finish_run(
        db_conn, run_id=rid2, finished_ts=2200,
        items_seen=50, items_written=40, errors_count=2,
    )

    text = await render_prometheus(db_conn)
    assert 'polymarket_alpha_table_rows{table="activities"} 0' in text
    assert 'polymarket_alpha_ingest_runs_total{worker="leaderboard"} 1' in text
    assert 'polymarket_alpha_items_written_total{worker="leaderboard"} 8' in text
    assert 'polymarket_alpha_last_run_timestamp_seconds{worker="activity"} 2200' in text
    assert 'polymarket_alpha_ingest_errors_total{worker="activity"} 2' in text
    # Valid exposition format: every non-comment line ends with a numeric value.
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert line.rsplit(" ", 1)[1].lstrip("-").isdigit()
