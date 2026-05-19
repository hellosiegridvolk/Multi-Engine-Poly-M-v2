"""Traders repository."""

from __future__ import annotations

import aiosqlite


async def upsert(
    conn: aiosqlite.Connection,
    wallet: str,
    *,
    now_ts: int,
    username: str | None = None,
    profile_visible: bool = True,
) -> None:
    wallet = wallet.lower()
    await conn.execute(
        """
        INSERT INTO traders (wallet, username, profile_visible,
                             first_seen_ts, last_seen_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            last_seen_ts = excluded.last_seen_ts,
            username = COALESCE(excluded.username, traders.username),
            profile_visible = excluded.profile_visible
        """,
        (wallet, username, 1 if profile_visible else 0, now_ts, now_ts),
    )


async def get(conn: aiosqlite.Connection, wallet: str) -> aiosqlite.Row | None:
    cur = await conn.execute(
        "SELECT * FROM traders WHERE wallet = ?", (wallet.lower(),)
    )
    return await cur.fetchone()


async def all_wallets(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute("SELECT wallet FROM traders ORDER BY wallet")
    return [r[0] for r in await cur.fetchall()]
