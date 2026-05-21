"""Markets + tokens repository."""

from __future__ import annotations

import json

import aiosqlite


async def upsert_market(
    conn: aiosqlite.Connection,
    *,
    condition_id: str,
    slug: str | None,
    question: str | None,
    end_date_ts: int | None,
    resolved: bool,
    resolution: str | None,
    category_tags: list[str],
    is_crypto: bool,
    resolved_at_ts: int | None,
    metadata_fetched_ts: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO markets
            (condition_id, slug, question, end_date_ts, resolved, resolution,
             category_tags, is_crypto, resolved_at_ts, metadata_fetched_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(condition_id) DO UPDATE SET
            slug = excluded.slug,
            question = excluded.question,
            end_date_ts = excluded.end_date_ts,
            resolved = excluded.resolved,
            resolution = excluded.resolution,
            category_tags = excluded.category_tags,
            is_crypto = excluded.is_crypto,
            resolved_at_ts = COALESCE(excluded.resolved_at_ts,
                                      markets.resolved_at_ts),
            metadata_fetched_ts = excluded.metadata_fetched_ts
        """,
        (
            condition_id,
            slug,
            question,
            end_date_ts,
            1 if resolved else 0,
            resolution,
            json.dumps(category_tags),
            1 if is_crypto else 0,
            resolved_at_ts,
            metadata_fetched_ts,
        ),
    )


async def upsert_token(
    conn: aiosqlite.Connection,
    *,
    token_id: str,
    condition_id: str,
    outcome: str,
    outcome_index: int,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO tokens
            (token_id, condition_id, outcome, outcome_index)
        VALUES (?, ?, ?, ?)
        """,
        (token_id, condition_id, outcome, outcome_index),
    )


async def get(
    conn: aiosqlite.Connection, condition_id: str
) -> aiosqlite.Row | None:
    cur = await conn.execute(
        "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
    )
    return await cur.fetchone()


async def known_condition_ids(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("SELECT condition_id FROM markets")
    return {r[0] for r in await cur.fetchall()}


async def active_crypto_token_ids(
    conn: aiosqlite.Connection, *, now_ts: int
) -> list[str]:
    cur = await conn.execute(
        """
        SELECT t.token_id
        FROM tokens t
        JOIN markets m ON m.condition_id = t.condition_id
        WHERE m.is_crypto = 1
          AND (m.end_date_ts IS NULL OR m.end_date_ts > ?)
          AND m.resolved = 0
        """,
        (now_ts,),
    )
    return [r[0] for r in await cur.fetchall()]


async def unresolved_past_end(
    conn: aiosqlite.Connection, *, now_ts: int
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT * FROM markets WHERE resolved = 0 "
        "AND end_date_ts IS NOT NULL AND end_date_ts <= ?",
        (now_ts,),
    )
    return list(await cur.fetchall())
