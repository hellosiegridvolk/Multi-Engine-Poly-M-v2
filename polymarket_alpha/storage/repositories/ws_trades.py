"""Raw WS trade captures (pre-reconciliation)."""

from __future__ import annotations

import aiosqlite


async def insert_raw(
    conn: aiosqlite.Connection,
    *,
    payload_json: str,
    market_token: str | None,
    tx_hash: str | None,
    ts: int,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO ws_trades_raw (payload_json, market_token, tx_hash, ts)
        VALUES (?, ?, ?, ?)
        """,
        (payload_json, market_token, tx_hash, ts),
    )
    return int(cur.lastrowid)


async def unpromoted(
    conn: aiosqlite.Connection, *, limit: int = 500
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT * FROM ws_trades_raw WHERE promoted_activity_id IS NULL "
        "ORDER BY raw_id LIMIT ?",
        (limit,),
    )
    return list(await cur.fetchall())


async def mark_promoted(
    conn: aiosqlite.Connection, *, raw_id: int, activity_id: int
) -> None:
    await conn.execute(
        "UPDATE ws_trades_raw SET promoted_activity_id = ? WHERE raw_id = ?",
        (activity_id, raw_id),
    )


async def count_unpromoted(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM ws_trades_raw WHERE promoted_activity_id IS NULL"
    )
    row = await cur.fetchone()
    return int(row[0])
