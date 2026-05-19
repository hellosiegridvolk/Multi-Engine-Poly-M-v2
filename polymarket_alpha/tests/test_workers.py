"""Workers: leaderboard pagination-cap, activity backfill, reconciler."""

import asyncio
import json
import logging
import time
from decimal import Decimal

import httpx
import respx

from polymarket_alpha.clients.leaderboard import LeaderboardClient
from polymarket_alpha.http import build_client
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories import ws_trades as ws_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)
from polymarket_alpha.workers import activity_poller, leaderboard_poller, reconciler

DATA_API = "https://data-api.polymarket.com"


def _lb_records(n=50):
    return [
        {
            "rank": str(i),
            "proxyWallet": f"0x{i:040x}",
            "userName": f"u{i}",
            "xUsername": "",
            "verifiedBadge": False,
            "vol": 1000.0 + i,
            "pnl": 5000.0 - i,
            "profileImage": "",
        }
        for i in range(1, n + 1)
    ]


@respx.mock
async def test_leaderboard_pagination_cap(db_conn, caplog):
    respx.get(f"{DATA_API}/v1/leaderboard").mock(
        return_value=httpx.Response(200, json=_lb_records(50))
    )
    http = build_client()
    client = LeaderboardClient(http)
    try:
        with caplog.at_level(logging.WARNING):
            seen, written = await leaderboard_poller._poll_period(
                db_conn, client, "week", max_rank=100, category=None, now_ts=1000
            )
    finally:
        http.close()

    assert seen == 100  # 2 full pages of 50, capped at max_rank
    snap = await lb_repo.latest_snapshot(db_conn, "week")
    assert snap["hit_pagination_cap"] == 1
    assert snap["entries_count"] == 100
    assert any("pagination cap" in r.message for r in caplog.records)


@respx.mock
async def test_activity_backfill_on_first_sight(db_conn):
    now = int(time.time())
    wallet = "0x" + "ab" * 20
    await traders_repo.upsert(db_conn, wallet, now_ts=now)
    snap_id = await lb_repo.create_snapshot(
        db_conn,
        period="day",
        snapshot_ts=now,
        ingest_duration_ms=1,
        entries_count=1,
        hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn,
        snapshot_id=snap_id,
        rank=1,
        wallet=wallet,
        pnl_usd=Decimal("1"),
        volume_usd=Decimal("1"),
        trade_count=None,
    )
    await db_conn.commit()

    five_days_ago = now - 5 * 86400  # within 7d backfill, outside 24h
    rec = {
        "type": "TRADE",
        "proxyWallet": wallet,
        "conditionId": "0xc",
        "asset": "111",
        "side": "BUY",
        "outcomeIndex": 0,
        "size": "10",
        "usdcSize": "5",
        "price": "0.5",
        "timestamp": five_days_ago,
        "transactionHash": "0xtx",
    }
    respx.get(f"{DATA_API}/activity").mock(
        return_value=httpx.Response(200, json=[rec])
    )

    shutdown = asyncio.Event()
    await activity_poller.run(
        db_conn, shutdown, interval=1, once=True, max_items_per_wallet=10
    )

    cur = await db_conn.execute(
        "SELECT COUNT(*) FROM activities WHERE wallet = ?", (wallet,)
    )
    assert (await cur.fetchone())[0] == 1  # 5-day-old record => backfill window used


async def test_reconciler_links_ws_to_rest(db_conn):
    now = int(time.time())
    wallet = "0xrecon"
    await traders_repo.upsert(db_conn, wallet, now_ts=now)
    aid = await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet=wallet,
            activity_type="TRADE",
            condition_id="0xc",
            token_id="111",
            side="BUY",
            outcome="YES",
            shares=Decimal("100"),
            usdc=Decimal("50"),
            price=Decimal("0.5"),
            timestamp=now,
            tx_hash="0xMATCH",
            source="REST",
        ),
        ingested_ts=now,
    )
    raw_id = await ws_repo.insert_raw(
        db_conn,
        payload_json=json.dumps(
            {
                "event_type": "last_trade_price",
                "transaction_hash": "0xMATCH",
                "asset_id": "111",
                "side": "BUY",
                "size": "100",
            }
        ),
        market_token="111",
        tx_hash="0xMATCH",
        ts=now,
    )
    await db_conn.commit()

    shutdown = asyncio.Event()
    await reconciler.run(db_conn, shutdown, interval=1, once=True)

    cur = await db_conn.execute(
        "SELECT promoted_activity_id FROM ws_trades_raw WHERE raw_id = ?",
        (raw_id,),
    )
    assert (await cur.fetchone())[0] == aid
    assert await ws_repo.count_unpromoted(db_conn) == 0
