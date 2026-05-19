"""Market resolver: backfill market/token metadata; sweep resolutions."""

from __future__ import annotations

import asyncio
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
MAX_TOKENS_PER_CYCLE = 1500
RESOLVE_GROUP = 40
RESOLVE_BATCH = 20


async def _resolve_resilient(
    gamma: GammaClient, token_ids: list[str], *, crypto_tag_id: str
) -> dict:
    resolved: dict = {}
    for i in range(0, len(token_ids), RESOLVE_GROUP):
        group = token_ids[i : i + RESOLVE_GROUP]
        try:
            part = await asyncio.to_thread(
                gamma.fetch_markets_with_tags,
                group,
                crypto_tag_id=crypto_tag_id,
                batch_size=RESOLVE_BATCH,
            )
            resolved.update(part)
        except PolymarketAPIError as exc:
            log.warning(
                "skipping %d-token group after Gamma error: %s",
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


async def _missing_token_ids(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute(
        """
        SELECT DISTINCT a.token_id FROM activities a
        WHERE a.token_id IS NOT NULL AND a.condition_id NOT IN
            (SELECT condition_id FROM markets)
        """
    )
    tokens = {r[0] for r in await cur.fetchall()}
    cur = await conn.execute(
        """
        SELECT DISTINCT market_token FROM ws_trades_raw
        WHERE market_token IS NOT NULL AND market_token NOT IN
            (SELECT token_id FROM tokens)
        """
    )
    tokens |= {r[0] for r in await cur.fetchall()}
    return [t for t in tokens if t]


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
        token_ids = (await _missing_token_ids(conn))[:MAX_TOKENS_PER_CYCLE]
        written = 0
        if token_ids:
            resolved = await _resolve_resilient(
                gamma, token_ids, crypto_tag_id=crypto_tag_id
            )
            for market, slugs in resolved.values():
                await _persist(conn, market, slugs, now_ts=now_ts)
                written += 1
            await conn.commit()

        # Slower hourly resolution sweep.
        if now_ts - state["last_sweep"] >= RESOLUTION_SWEEP_S:
            state["last_sweep"] = now_ts
            stale = await markets_repo.unresolved_past_end(conn, now_ts=now_ts)
            if stale:
                cur = await conn.execute(
                    "SELECT token_id FROM tokens WHERE condition_id IN "
                    "(%s)" % ",".join("?" * len(stale)),
                    [m["condition_id"] for m in stale],
                )
                sweep_tokens = [r[0] for r in await cur.fetchall()]
                if sweep_tokens:
                    re_resolved = await _resolve_resilient(
                        gamma,
                        sweep_tokens[:MAX_TOKENS_PER_CYCLE],
                        crypto_tag_id=crypto_tag_id,
                    )
                    for market, slugs in re_resolved.values():
                        await _persist(conn, market, slugs, now_ts=now_ts)
                        written += 1
                    await conn.commit()
                    log.info("resolution sweep updated %d markets", len(re_resolved))

        return len(token_ids), written

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
