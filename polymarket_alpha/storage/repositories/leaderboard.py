"""Leaderboard snapshots + entries repository."""

from __future__ import annotations

from decimal import Decimal

import aiosqlite

from polymarket_alpha.storage.repositories import to_text


async def create_snapshot(
    conn: aiosqlite.Connection,
    *,
    period: str,
    snapshot_ts: int,
    ingest_duration_ms: int | None,
    entries_count: int,
    hit_pagination_cap: bool,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO leaderboard_snapshots
            (period, snapshot_ts, ingest_duration_ms, entries_count,
             hit_pagination_cap)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(period, snapshot_ts) DO UPDATE SET
            entries_count = excluded.entries_count,
            ingest_duration_ms = excluded.ingest_duration_ms,
            hit_pagination_cap = excluded.hit_pagination_cap
        RETURNING snapshot_id
        """,
        (
            period,
            snapshot_ts,
            ingest_duration_ms,
            entries_count,
            1 if hit_pagination_cap else 0,
        ),
    )
    row = await cur.fetchone()
    return int(row[0])


async def add_entry(
    conn: aiosqlite.Connection,
    *,
    snapshot_id: int,
    rank: int,
    wallet: str,
    pnl_usd: Decimal,
    volume_usd: Decimal | None,
    trade_count: int | None,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO leaderboard_entries
            (snapshot_id, rank, wallet, pnl_usd, volume_usd, trade_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            rank,
            wallet.lower(),
            to_text(pnl_usd),
            to_text(volume_usd),
            trade_count,
        ),
    )


async def latest_snapshot(
    conn: aiosqlite.Connection, period: str, *, at_ts: int | None = None
) -> aiosqlite.Row | None:
    if at_ts is None:
        cur = await conn.execute(
            "SELECT * FROM leaderboard_snapshots WHERE period = ? "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            (period,),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM leaderboard_snapshots WHERE period = ? "
            "AND snapshot_ts <= ? ORDER BY snapshot_ts DESC LIMIT 1",
            (period, at_ts),
        )
    return await cur.fetchone()


async def entries_for_snapshot(
    conn: aiosqlite.Connection, snapshot_id: int
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT * FROM leaderboard_entries WHERE snapshot_id = ? ORDER BY rank",
        (snapshot_id,),
    )
    return list(await cur.fetchall())


async def wallets_since(
    conn: aiosqlite.Connection, *, since_ts: int, top_n: int = 100
) -> set[str]:
    """Union of top-N wallets across all snapshots since `since_ts`."""
    cur = await conn.execute(
        """
        SELECT DISTINCT e.wallet
        FROM leaderboard_entries e
        JOIN leaderboard_snapshots s ON s.snapshot_id = e.snapshot_id
        WHERE s.snapshot_ts >= ? AND e.rank <= ?
        """,
        (since_ts, top_n),
    )
    return {r[0] for r in await cur.fetchall()}
