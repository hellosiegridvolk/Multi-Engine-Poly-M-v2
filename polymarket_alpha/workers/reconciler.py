"""Reconciler: link WS trade prints to REST-sourced activities by tx_hash.

The public CLOB market channel has no wallet (API_NOTES.md §A), so WS rows
cannot mint wallet-attributed activities. Instead each unpromoted
`ws_trades_raw` row is matched to an existing REST `activities` row by
transaction hash (+ token / side / size when available) and linked via
`promoted_activity_id`. WS-only prints with no REST match stay unpromoted —
expected, surfaced in `ingest_runs.notes`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiosqlite

from polymarket_alpha.storage.repositories import activities as act_repo
from polymarket_alpha.storage.repositories import ws_trades as ws_repo
from polymarket_alpha.workers import run_loop

log = logging.getLogger("polymarket_alpha.workers.reconciler")


def _best_match(rows: list[aiosqlite.Row], payload: dict) -> aiosqlite.Row | None:
    if not rows:
        return None
    token = str(payload.get("asset_id", "")) or None
    side = str(payload.get("side", "")).upper() or None
    size = str(payload.get("size", "")) or None
    # Prefer an exact economic match; else fall back to the tx-hash row.
    for r in rows:
        if (
            (token is None or r["token_id"] == token)
            and (side is None or r["side"] == side)
            and (size is None or r["shares"] == size)
        ):
            return r
    return rows[0]


async def run(
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    *,
    interval: int,
    once: bool,
    batch: int = 500,
) -> None:
    async def cycle(_run_id: int) -> tuple[int, int]:
        pending = await ws_repo.unpromoted(conn, limit=batch)
        promoted = 0
        unmatched = 0
        for row in pending:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                unmatched += 1
                continue
            tx = payload.get("transaction_hash") or row["tx_hash"]
            if not tx:
                unmatched += 1
                continue
            candidates = await act_repo.find_by_tx(conn, tx)
            match = _best_match(candidates, payload)
            if match is None:
                unmatched += 1
                continue
            await ws_repo.mark_promoted(
                conn, raw_id=row["raw_id"], activity_id=int(match["activity_id"])
            )
            promoted += 1
        await conn.commit()
        if unmatched:
            log.info(
                "reconciler: %d promoted, %d WS-only prints left unpromoted "
                "(no REST match — expected, no wallet on WS)",
                promoted,
                unmatched,
            )
        return len(pending), promoted

    await run_loop(
        worker="reconciler",
        conn=conn,
        shutdown=shutdown,
        interval=interval,
        once=once,
        cycle=cycle,
    )
