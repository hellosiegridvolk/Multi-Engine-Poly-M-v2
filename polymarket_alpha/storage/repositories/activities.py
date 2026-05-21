"""Canonical activity log repository (REST + WS-promoted)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import aiosqlite

from polymarket_alpha.storage.repositories import dedup_key, to_decimal, to_text


@dataclass(frozen=True, slots=True)
class ActivityRow:
    wallet: str
    activity_type: str
    condition_id: str | None
    token_id: str | None
    side: str | None
    outcome: str | None
    shares: Decimal | None
    usdc: Decimal | None
    price: Decimal | None
    timestamp: int
    tx_hash: str | None
    source: str  # 'REST' | 'WS'
    log_index: int | None = None


def compute_dedup_key(a: ActivityRow) -> str:
    return dedup_key(
        tx_hash=a.tx_hash,
        activity_type=a.activity_type,
        condition_id=a.condition_id,
        token_id=a.token_id,
        side=a.side,
        shares=to_text(a.shares),
        timestamp=a.timestamp,
    )


async def insert_or_ignore(
    conn: aiosqlite.Connection, a: ActivityRow, *, ingested_ts: int
) -> int | None:
    """Insert; return the activity_id (new or pre-existing), None on failure.

    Dedup is by content hash (`UNIQUE(dedup_key)`).
    """
    key = compute_dedup_key(a)
    await conn.execute(
        """
        INSERT OR IGNORE INTO activities
            (wallet, activity_type, condition_id, token_id, side, outcome,
             shares, usdc, price, timestamp, tx_hash, log_index, dedup_key,
             source, ingested_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            a.wallet.lower(),
            a.activity_type,
            a.condition_id,
            a.token_id,
            a.side,
            a.outcome,
            to_text(a.shares),
            to_text(a.usdc),
            to_text(a.price),
            a.timestamp,
            a.tx_hash,
            a.log_index,
            key,
            a.source,
            ingested_ts,
        ),
    )
    cur = await conn.execute(
        "SELECT activity_id FROM activities WHERE dedup_key = ?", (key,)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else None


async def find_by_tx(
    conn: aiosqlite.Connection, tx_hash: str
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT * FROM activities WHERE tx_hash = ?", (tx_hash,)
    )
    return list(await cur.fetchall())


def _row_to_dict(r: aiosqlite.Row) -> dict:
    d = dict(r)
    for k in ("shares", "usdc", "price"):
        d[k] = to_decimal(d.get(k))
    return d


async def by_wallet(
    conn: aiosqlite.Connection,
    wallet: str,
    *,
    since_ts: int = 0,
    activity_types: list[str] | None = None,
    crypto_only: bool = False,
) -> list[dict]:
    sql = ["SELECT a.* FROM activities a"]
    params: list = []
    if crypto_only:
        sql.append("JOIN markets m ON m.condition_id = a.condition_id")
    sql.append("WHERE a.wallet = ? AND a.timestamp >= ?")
    params += [wallet.lower(), since_ts]
    if crypto_only:
        sql.append("AND m.is_crypto = 1")
    if activity_types:
        placeholders = ",".join("?" for _ in activity_types)
        sql.append(f"AND a.activity_type IN ({placeholders})")
        params += activity_types
    sql.append("ORDER BY a.timestamp DESC")
    cur = await conn.execute(" ".join(sql), params)
    return [_row_to_dict(r) for r in await cur.fetchall()]


async def distinct_condition_ids(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute(
        "SELECT DISTINCT condition_id FROM activities "
        "WHERE condition_id IS NOT NULL"
    )
    return {r[0] for r in await cur.fetchall()}
