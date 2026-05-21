"""Ingest-run audit log (operational visibility)."""

from __future__ import annotations

import aiosqlite


async def start_run(
    conn: aiosqlite.Connection, *, worker: str, started_ts: int
) -> int:
    cur = await conn.execute(
        "INSERT INTO ingest_runs (worker, started_ts) VALUES (?, ?)",
        (worker, started_ts),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def finish_run(
    conn: aiosqlite.Connection,
    *,
    run_id: int,
    finished_ts: int,
    items_seen: int = 0,
    items_written: int = 0,
    errors_count: int = 0,
    last_error: str | None = None,
    notes: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE ingest_runs SET
            finished_ts = ?, items_seen = ?, items_written = ?,
            errors_count = ?, last_error = ?, notes = ?
        WHERE run_id = ?
        """,
        (
            finished_ts,
            items_seen,
            items_written,
            errors_count,
            last_error,
            notes,
            run_id,
        ),
    )
    await conn.commit()
