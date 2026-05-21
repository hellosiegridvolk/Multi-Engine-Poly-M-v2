"""Leaderboard poller: snapshot day/week/month every interval.

Verified (API_NOTES.md §B): page size caps at 50, offset-paginated, deep.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from polymarket_alpha.clients.leaderboard import LeaderboardClient
from polymarket_alpha.http import build_client
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.workers import run_loop

log = logging.getLogger("polymarket_alpha.workers.leaderboard")

PERIODS = ("day", "week", "month")
PAGE_SIZE = 50  # verified hard cap


async def _poll_period(
    conn: aiosqlite.Connection,
    client: LeaderboardClient,
    period: str,
    *,
    max_rank: int,
    category: str | None,
    now_ts: int,
) -> tuple[int, int]:
    t0 = time.time()
    traders: list = []
    offset = 0
    hit_cap = False
    while offset < max_rank:
        page = await asyncio.to_thread(
            client.fetch,
            window=period,
            limit=PAGE_SIZE,
            category=category,
            offset=offset,
        )
        if not page:
            break
        traders.extend(page)
        if len(page) < PAGE_SIZE:
            break  # board exhausted
        offset += PAGE_SIZE
        if offset >= max_rank:
            hit_cap = True  # more data exists beyond our configured cap
    traders = traders[:max_rank]
    duration_ms = int((time.time() - t0) * 1000)

    snapshot_id = await lb_repo.create_snapshot(
        conn,
        period=period,
        snapshot_ts=now_ts,
        ingest_duration_ms=duration_ms,
        entries_count=len(traders),
        hit_pagination_cap=hit_cap,
    )
    for idx, tr in enumerate(traders, start=1):
        await traders_repo.upsert(
            conn,
            tr.proxy_wallet,
            now_ts=now_ts,
            username=tr.user_name or None,
        )
        await lb_repo.add_entry(
            conn,
            snapshot_id=snapshot_id,
            rank=tr.rank or idx,
            wallet=tr.proxy_wallet,
            pnl_usd=tr.pnl,
            volume_usd=tr.volume,
            trade_count=None,  # not exposed by the API
        )
    await conn.commit()
    if hit_cap:
        log.warning(
            "leaderboard %s hit configured pagination cap at rank %d",
            period,
            max_rank,
        )
    return len(traders), len(traders)


async def run(
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    *,
    interval: int,
    once: bool,
    max_rank: int = 500,
    category: str | None = "crypto",
) -> None:
    http = build_client()
    client = LeaderboardClient(http)

    async def cycle(_run_id: int) -> tuple[int, int]:
        now_ts = int(time.time())
        total_seen = total_written = 0
        for period in PERIODS:
            seen, written = await _poll_period(
                conn,
                client,
                period,
                max_rank=max_rank,
                category=category,
                now_ts=now_ts,
            )
            total_seen += seen
            total_written += written
        return total_seen, total_written

    try:
        await run_loop(
            worker="leaderboard",
            conn=conn,
            shutdown=shutdown,
            interval=interval,
            once=once,
            cycle=cycle,
        )
    finally:
        http.close()
