"""strategies subcommand: SQLite -> dossier classifiers -> aggregated buckets."""

from decimal import Decimal

from polymarket_alpha.helpers.strategies import analyze, render_human, report_to_dict
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import markets as markets_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)

NOW = 1_800_000_000


async def _seed(db_conn, wallet: str, *, side: str, profitable: bool):
    """Seed one crypto market (resolved YES) + 1 BUY activity for wallet."""
    await traders_repo.upsert(db_conn, wallet, now_ts=NOW)
    # Market: resolved YES
    cid = "0xc" + wallet[-4:]
    await markets_repo.upsert_market(
        db_conn,
        condition_id=cid,
        slug="s",
        question="q",
        end_date_ts=NOW,
        resolved=True,
        resolution="YES",
        category_tags=["crypto"],
        is_crypto=True,
        resolved_at_ts=NOW,
        metadata_fetched_ts=NOW,
    )
    yes_tok, no_tok = "y" + wallet[-4:], "n" + wallet[-4:]
    await markets_repo.upsert_token(
        db_conn, token_id=yes_tok, condition_id=cid, outcome="YES", outcome_index=0
    )
    await markets_repo.upsert_token(
        db_conn, token_id=no_tok, condition_id=cid, outcome="NO", outcome_index=1
    )
    # BUY YES @ 0.40 -> profitable when YES wins (settle 1 - 0.40 = +0.60 per share)
    # BUY NO  @ 0.40 -> loses when YES wins (settle -0.40 per share)
    outcome = "YES" if side == "BUY_YES" else "NO"
    token = yes_tok if outcome == "YES" else no_tok
    await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet=wallet,
            activity_type="TRADE",
            condition_id=cid,
            token_id=token,
            side="BUY",
            outcome=outcome,
            shares=Decimal("100"),
            usdc=Decimal("40"),
            price=Decimal("0.40"),
            timestamp=NOW,
            tx_hash="0xt" + wallet[-4:],
            source="REST",
        ),
        ingested_ts=NOW,
    )


async def test_analyze_buckets_and_dossiers(db_conn):
    # one BULL winner, one BEAR loser, same leaderboard snapshot
    w_win = "0x" + "1" * 40
    w_lose = "0x" + "2" * 40
    await _seed(db_conn, w_win, side="BUY_YES", profitable=True)
    await _seed(db_conn, w_lose, side="BUY_NO", profitable=False)

    snap_id = await lb_repo.create_snapshot(
        db_conn,
        period="day",
        snapshot_ts=NOW,
        ingest_duration_ms=1,
        entries_count=2,
        hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn,
        snapshot_id=snap_id,
        rank=1,
        wallet=w_win,
        pnl_usd=Decimal("60"),
        volume_usd=Decimal("40"),
        trade_count=None,
    )
    await lb_repo.add_entry(
        db_conn,
        snapshot_id=snap_id,
        rank=2,
        wallet=w_lose,
        pnl_usd=Decimal("-40"),
        volume_usd=Decimal("40"),
        trade_count=None,
    )
    await db_conn.commit()

    report = await analyze(db_conn, period="day", top=10)
    assert report.snapshot_ts == NOW
    assert len(report.dossiers) == 2

    by_wallet = {d.trader.proxy_wallet: d for d in report.dossiers}
    win = by_wallet[w_win]
    lose = by_wallet[w_lose]
    # winner: BUY YES @0.40 on YES-resolved market -> cash -40 + settle 100 = +60
    assert win.realized_pnl == Decimal("60")
    assert win.hit_rate == Decimal("1.0000")
    assert win.direction_bias == "BULL"
    # loser: BUY NO @0.40, YES wins -> cash -40 + settle 0 = -40
    assert lose.realized_pnl == Decimal("-40")
    assert lose.hit_rate == Decimal("0.0000")
    assert lose.direction_bias == "BEAR"

    # buckets sorted by total_pnl desc
    assert report.buckets[0].total_pnl == Decimal("60")
    assert report.buckets[0].n == 1
    assert report.buckets[-1].total_pnl == Decimal("-40")

    # human render is non-empty and contains both wallets
    out = render_human(report)
    assert w_win in out and w_lose in out and "STRATEGY BUCKETS" in out

    # json shape
    d = report_to_dict(report)
    assert d["period"] == "day"
    assert len(d["wallets"]) == 2
    assert d["wallets"][0]["realized_pnl"] in ("60", "-40")
    assert all("median_hit_rate" in b for b in d["buckets"])


async def test_analyze_no_snapshot_is_empty(db_conn):
    report = await analyze(db_conn, period="week", top=10)
    assert report.dossiers == [] and report.buckets == []
