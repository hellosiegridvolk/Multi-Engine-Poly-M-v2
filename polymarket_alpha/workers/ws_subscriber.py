"""WS subscriber: capture live crypto-market trade prints into ws_trades_raw.

No dedup/normalization here (reconciler's job) — keep the hot path fast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiosqlite

from polymarket_alpha.clients.clob_ws import ClobWsClient
from polymarket_alpha.storage.repositories import markets as markets_repo
from polymarket_alpha.storage.repositories import runs as runs_repo
from polymarket_alpha.storage.repositories import ws_trades as ws_repo
from polymarket_alpha.workers import sleep_or_stop

log = logging.getLogger("polymarket_alpha.workers.ws")


async def _consume(
    conn: aiosqlite.Connection,
    client: ClobWsClient,
    shutdown: asyncio.Event,
    counter: dict,
) -> None:
    async for trade in client.stream_trades():
        if shutdown.is_set():
            break
        try:
            await ws_repo.insert_raw(
                conn,
                payload_json=json.dumps(trade.raw),
                market_token=trade.token_id,
                tx_hash=trade.tx_hash,
                ts=int(trade.timestamp_ms // 1000) or int(time.time()),
            )
            await conn.commit()
            counter["written"] += 1
        except Exception:  # noqa: BLE001 - never let one row kill the stream
            log.exception("failed to persist WS trade")


async def run(
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    *,
    interval: int,
    once: bool,
) -> None:
    client = ClobWsClient()
    counter = {"written": 0}
    await client.connect()

    consumer = asyncio.create_task(_consume(conn, client, shutdown, counter))
    try:
        while not shutdown.is_set():
            started = int(time.time())
            run_id = await runs_repo.start_run(
                conn, worker="ws", started_ts=started
            )
            now_ts = int(time.time())
            tokens = await markets_repo.active_crypto_token_ids(
                conn, now_ts=now_ts
            )
            await client.subscribe_trades(tokens)
            log.info(
                "ws: %d active crypto tokens subscribed; %d raw captured so far",
                len(tokens),
                counter["written"],
            )
            await runs_repo.finish_run(
                conn,
                run_id=run_id,
                finished_ts=int(time.time()),
                items_seen=len(tokens),
                items_written=counter["written"],
                notes=f"{len(tokens)} tokens subscribed",
            )
            if once:
                break
            if await sleep_or_stop(shutdown, interval):
                break
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await client.close()
