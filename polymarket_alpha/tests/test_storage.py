"""Storage: migrations, dedup, crypto filter."""

import time
from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_alpha import storage
from polymarket_alpha.storage import current_version, run_migrations
from polymarket_alpha.storage.repositories import markets as markets_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    by_wallet,
    insert_or_ignore,
)

NOW = 1_700_000_000


async def test_migration_roundtrip_idempotent(tmp_path):
    conn = await storage.connect(tmp_path / "m.db")
    try:
        assert await current_version(conn) == 0
        v = await run_migrations(conn)
        assert v == 1
        assert await current_version(conn) == 1
        # idempotent re-run
        v2 = await run_migrations(conn)
        assert v2 == 1
        stats = await storage.table_stats(conn)
        assert "activities" in stats and "schema_version" in stats
    finally:
        await conn.close()


async def test_activity_dedup_rest_then_ws(db_conn):
    await traders_repo.upsert(db_conn, "0xWALLET", now_ts=NOW)
    base = dict(
        wallet="0xwallet",
        activity_type="TRADE",
        condition_id="0xc",
        token_id="111",
        side="BUY",
        outcome="YES",
        shares=Decimal("100"),
        usdc=Decimal("50"),
        price=Decimal("0.5"),
        timestamp=NOW,
        tx_hash="0xdeadbeef",
    )
    rest = ActivityRow(source="REST", **base)
    ws = ActivityRow(source="WS", **base)  # same economic tuple
    id1 = await insert_or_ignore(db_conn, rest, ingested_ts=NOW)
    id2 = await insert_or_ignore(db_conn, ws, ingested_ts=NOW)
    await db_conn.commit()
    assert id1 == id2  # same dedup_key -> same row
    cur = await db_conn.execute("SELECT COUNT(*) FROM activities")
    assert (await cur.fetchone())[0] == 1


async def test_db_backup_produces_gzipped_artifacts(db_conn, tmp_path):
    import gzip
    import json as _json

    await traders_repo.upsert(db_conn, "0xbk", now_ts=NOW)
    await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet="0xbk",
            activity_type="TRADE",
            condition_id="0xc",
            token_id="1",
            side="BUY",
            outcome="YES",
            shares=Decimal("3"),
            usdc=Decimal("2"),
            price=Decimal("0.66"),
            timestamp=NOW,
            tx_hash="0xbktx",
            source="REST",
        ),
        ingested_ts=NOW,
    )
    await db_conn.commit()

    out = await storage.backup(
        db_conn, out_dir=tmp_path / "bk", base_name="poly-wallet-data-strategy"
    )
    db_gz = Path(out["db_gz"])
    jsonl_gz = Path(out["jsonl_gz"])
    assert db_gz.exists() and db_gz.name.endswith(".db.gz")
    assert jsonl_gz.exists() and jsonl_gz.name.endswith(".jsonl.gz")

    # db.gz is a valid gzip of a usable SQLite snapshot
    import sqlite3

    restored = tmp_path / "restored.db"
    with gzip.open(db_gz, "rb") as g:
        restored.write_bytes(g.read())
    rc = sqlite3.connect(restored)
    assert rc.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1

    # jsonl.gz: one normalized activity row per line
    with gzip.open(jsonl_gz, "rt", encoding="utf-8") as g:
        lines = [l for l in g.read().splitlines() if l.strip()]
    assert len(lines) == 1
    row = _json.loads(lines[0])
    assert row["wallet"] == "0xbk" and row["dedup_key"]


async def test_crypto_only_filter(db_conn):
    await traders_repo.upsert(db_conn, "0xw", now_ts=NOW)
    for cid, crypto in (("0xCRYPTO", True), ("0xSTOCK", False)):
        await markets_repo.upsert_market(
            db_conn,
            condition_id=cid,
            slug=cid,
            question="q",
            end_date_ts=None,
            resolved=False,
            resolution=None,
            category_tags=["crypto"] if crypto else ["business"],
            is_crypto=crypto,
            resolved_at_ts=None,
            metadata_fetched_ts=NOW,
        )
        await insert_or_ignore(
            db_conn,
            ActivityRow(
                wallet="0xw",
                activity_type="TRADE",
                condition_id=cid,
                token_id="t" + cid,
                side="BUY",
                outcome="YES",
                shares=Decimal("1"),
                usdc=Decimal("1"),
                price=Decimal("0.5"),
                timestamp=NOW,
                tx_hash="0x" + cid,
                source="REST",
            ),
            ingested_ts=NOW,
        )
    await db_conn.commit()

    all_rows = await by_wallet(db_conn, "0xw")
    crypto_rows = await by_wallet(db_conn, "0xw", crypto_only=True)
    assert len(all_rows) == 2
    assert len(crypto_rows) == 1
    assert crypto_rows[0]["condition_id"] == "0xCRYPTO"
