"""Market resolver: backfill market/token metadata; sweep resolutions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiosqlite

from polymarket_alpha.clients.gamma import GammaClient
from polymarket_alpha.http import PolymarketAPIError, build_client
from polymarket_alpha.models import Market
from polymarket_alpha.storage.repositories import markets as markets_repo
from polymarket_alpha.workers import run_loop

log = logging.getLogger("polymarket_alpha.workers.resolver")

RESOLUTION_SWEEP_S = 3600
# Bound work per cycle; isolate transient Gamma 5xx to one small group so a
# flaky batch never loses the whole cycle (Gamma 500s intermittently on long
# clob_token_ids URLs).
MAX_IDS_PER_CYCLE = 1500
RESOLVE_GROUP = 50
RESOLVE_BATCH = 25


async def _resolve_resilient(
    gamma: GammaClient, condition_ids: list[str], *, crypto_tag_id: str
) -> dict:
    """Resolve by condition id (recovers historical/closed markets the
    clob_token_ids filter misses), isolating transient Gamma 5xx per group."""
    resolved: dict = {}
    for i in range(0, len(condition_ids), RESOLVE_GROUP):
        group = condition_ids[i : i + RESOLVE_GROUP]
        try:
            part = await asyncio.to_thread(
                gamma.fetch_markets_with_tags_by_condition,
                group,
                crypto_tag_id=crypto_tag_id,
                batch_size=RESOLVE_BATCH,
            )
            resolved.update(part)
        except PolymarketAPIError as exc:
            log.warning(
                "skipping %d-condition group after Gamma error: %s",
                len(group),
                exc,
            )
    return resolved


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def _resolution(market: Market) -> tuple[bool, str | None]:
    if not market.closed or market.winning_outcome_index is None:
        return False, None
    if not market.is_binary or len(market.outcomes) != 2:
        return True, "INVALID"
    labels = [o.strip().lower() for o in market.outcomes]
    yes_index = labels.index("yes") if "yes" in labels else 0
    return True, ("YES" if market.winning_outcome_index == yes_index else "NO")


async def _missing_condition_ids(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute(
        """
        SELECT DISTINCT condition_id FROM activities
        WHERE condition_id IS NOT NULL AND condition_id NOT IN
            (SELECT condition_id FROM markets)
        """
    )
    cids = {r[0] for r in await cur.fetchall()}
    # WS raw rows carry the conditionId inside the payload (`market`).
    # Bounded scan: newest unpromoted first, capped per cycle.
    cur = await conn.execute(
        "SELECT payload_json FROM ws_trades_raw WHERE promoted_activity_id "
        "IS NULL ORDER BY raw_id DESC LIMIT ?",
        (MAX_IDS_PER_CYCLE * 4,),
    )
    known = await markets_repo.known_condition_ids(conn)
    for (pj,) in await cur.fetchall():
        try:
            cid = json.loads(pj).get("market")
        except (json.JSONDecodeError, AttributeError):
            continue
        if cid and cid not in known:
            cids.add(cid)
    return [c for c in cids if c]


async def _persist(
    conn: aiosqlite.Connection,
    market: Market,
    slugs: list[str],
    *,
    now_ts: int,
) -> None:
    resolved, resolution = _resolution(market)
    end_ts = _iso_to_ts(market.end_date_iso)
    await markets_repo.upsert_market(
        conn,
        condition_id=market.condition_id,
        slug=market.slug or None,
        question=market.question or None,
        end_date_ts=end_ts,
        resolved=resolved,
        resolution=resolution,
        category_tags=slugs,
        is_crypto=market.is_crypto,
        resolved_at_ts=now_ts if resolved else None,
        metadata_fetched_ts=now_ts,
    )
    if market.is_binary and len(market.clob_token_ids) == 2:
        labels = [o.strip().lower() for o in market.outcomes]
        yes_index = labels.index("yes") if "yes" in labels else 0
        for idx, tok in enumerate(market.clob_token_ids):
            await markets_repo.upsert_token(
                conn,
                token_id=tok,
                condition_id=market.condition_id,
                outcome="YES" if idx == yes_index else "NO",
                outcome_index=idx,
            )


async def run(
    conn: aiosqlite.Connection,
    shutdown: asyncio.Event,
    *,
    interval: int,
    once: bool,
) -> None:
    http = build_client()
    gamma = GammaClient(http)
    crypto_tag_id = await asyncio.to_thread(gamma.resolve_crypto_tag_id)
    state = {"last_sweep": 0}

    async def cycle(_run_id: int) -> tuple[int, int]:
        now_ts = int(time.time())
        condition_ids = (await _missing_condition_ids(conn))[:MAX_IDS_PER_CYCLE]
        seen = len(condition_ids)
        written = 0
        if condition_ids:
            resolved = await _resolve_resilient(
                gamma, condition_ids, crypto_tag_id=crypto_tag_id
            )
            for market, slugs in resolved.values():
                await _persist(conn, market, slugs, now_ts=now_ts)
                written += 1
            await conn.commit()

        # Slower hourly resolution sweep — re-resolve unresolved past-end
        # markets directly by their condition ids.
        if now_ts - state["last_sweep"] >= RESOLUTION_SWEEP_S:
            state["last_sweep"] = now_ts
            stale = await markets_repo.unresolved_past_end(conn, now_ts=now_ts)
            stale_cids = [m["condition_id"] for m in stale][:MAX_IDS_PER_CYCLE]
            if stale_cids:
                re_resolved = await _resolve_resilient(
                    gamma, stale_cids, crypto_tag_id=crypto_tag_id
                )
                for market, slugs in re_resolved.values():
                    await _persist(conn, market, slugs, now_ts=now_ts)
                    written += 1
                await conn.commit()
                log.info("resolution sweep updated %d markets", len(re_resolved))

        return seen, written

    try:
        await run_loop(
            worker="resolver",
            conn=conn,
            shutdown=shutdown,
            interval=interval,
            once=once,
            cycle=cycle,
        )
    finally:
        http.close()
