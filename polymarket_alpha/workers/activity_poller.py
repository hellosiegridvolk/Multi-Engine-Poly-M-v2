"""Activity poller: per-watchlist-wallet, ALL activity types, REST source.

Watchlist = union of last 24h leaderboard top-100 per period. Incremental by
max stored timestamp; first-sight wallets get a 7-day backfill.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from polymarket_alpha.clients.activity import ActivityClient
from polymarket_alpha.http import build_client
from polymarket_alpha.models import to_decimal
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)
from polymarket_alpha.workers import run_loop

log = logging.getLogger("polymarket_alpha.workers.activity")

DAY_S = 86_400
BACKFILL_S = 7 * DAY_S


def _to_row(rec: dict) -> ActivityRow:
    atype = str(rec.get("type", "UNKNOWN"))
    is_trade = atype == "TRADE"
    outcome = None
    if is_trade:
        outcome = "YES" if int(rec.get("outcomeIndex", 0)) == 0 else "NO"
    return ActivityRow(
        wallet=str(rec.get("proxyWallet", "")).lower(),
        activity_type=atype,
        condition_id=rec.get("conditionId") or None,
        token_id=str(rec["asset"]) if rec.get("asset") else None,
        side=(str(rec["side"]).upper() if rec.get("side") else None),
        outcome=outcome,
        shares=to_decimal(rec["size"]) if rec.get("size") is not None else None,
        usdc=to_decimal(rec["usdcSize"]) if rec.get("usdcSize") is not None else None,
        price=to_decimal(rec["price"]) if rec.get("price") is not None else None,
        timestamp=int(rec.get("timestamp", 0)),
        tx_hash=rec.get("transactionHash") or None,
        source="REST",
    )


async def _last_ts(conn: aiosqlite.Connection, wallet: str) -> int | None:
    cur = await conn.execute(
        "SELECT MAX(timestamp) FROM activities WHERE wallet = ?",
        (wallet.lower(),),
    )
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def _watchlist(conn: aiosqlite.Connection, now_ts: int) -> set[str]:
    return await lb_repo.wallets_since(
        conn, since_ts=now_ts - DAY_S, top_n=100
    )


async def run(
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    *,
    interval: int,
    once: bool,
    max_items_per_wallet: int | None = 2000,
) -> None:
    http = build_client()
    client = ActivityClient(http)

    async def cycle(_run_id: int) -> tuple[int, int]:
        now_ts = int(time.time())
        wallets = await _watchlist(conn, now_ts)
        seen = written = 0
        for wallet in sorted(wallets):
            if shutdown.is_set():
                break
            last = await _last_ts(conn, wallet)
            since = last if last is not None else now_ts - BACKFILL_S
            if last is None:
                log.info("first sight %s — backfilling 7d", wallet)
            try:
                records = await asyncio.to_thread(
                    client.fetch_activity,
                    wallet,
                    since_ts=since,
                    max_items=max_items_per_wallet,
                )
            except Exception:  # noqa: BLE001 - one bad wallet must not kill cycle
                log.exception("activity fetch failed for %s", wallet)
                continue
            seen += len(records)
            await traders_repo.upsert(conn, wallet, now_ts=now_ts)
            for rec in records:
                row = _to_row(rec)
                if not row.wallet:
                    continue
                aid = await insert_or_ignore(conn, row, ingested_ts=now_ts)
                if aid is not None:
                    written += 1
            await conn.commit()
        return seen, written

    try:
        await run_loop(
            worker="activity",
            conn=conn,
            shutdown=shutdown,
            interval=interval,
            once=once,
            cycle=cycle,
        )
    finally:
        http.close()
