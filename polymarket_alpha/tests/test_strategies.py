"""strategies subcommand: SQLite -> dossier classifiers -> aggregated buckets."""

from decimal import Decimal

from polymarket_alpha.helpers.strategies import (
    analyze,
    render_copy_sources_yaml,
    render_human,
    report_to_dict,
    shortlist,
)
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


async def _seed_profitable(db_conn, wallet: str, *, direction: str):
    """Seed a profitable wallet of a given direction.

    direction='BULL'  -> BUY YES on a YES-resolved market (wins)
    direction='BEAR'  -> BUY NO  on a NO-resolved market  (wins)
    """
    await traders_repo.upsert(db_conn, wallet, now_ts=NOW)
    cid = "0xc" + wallet[-4:]
    resolution = "YES" if direction == "BULL" else "NO"
    await markets_repo.upsert_market(
        db_conn, condition_id=cid, slug="s", question="q", end_date_ts=NOW,
        resolved=True, resolution=resolution, category_tags=["crypto"],
        is_crypto=True, resolved_at_ts=NOW, metadata_fetched_ts=NOW,
    )
    await markets_repo.upsert_token(
        db_conn, token_id="y" + wallet[-4:], condition_id=cid,
        outcome="YES", outcome_index=0,
    )
    await markets_repo.upsert_token(
        db_conn, token_id="n" + wallet[-4:], condition_id=cid,
        outcome="NO", outcome_index=1,
    )
    outcome = "YES" if direction == "BULL" else "NO"
    await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet=wallet, activity_type="TRADE", condition_id=cid,
            token_id=("y" if outcome == "YES" else "n") + wallet[-4:],
            side="BUY", outcome=outcome,
            shares=Decimal("100"), usdc=Decimal("40"), price=Decimal("0.40"),
            timestamp=NOW, tx_hash="0xt" + wallet[-4:], source="REST",
        ),
        ingested_ts=NOW,
    )


async def test_shortlist_diversify_spreads_direction(db_conn):
    bull1 = "0x" + "1" * 39 + "a"
    bull2 = "0x" + "2" * 39 + "b"
    bear1 = "0x" + "3" * 39 + "c"
    await _seed_profitable(db_conn, bull1, direction="BULL")
    await _seed_profitable(db_conn, bull2, direction="BULL")
    await _seed_profitable(db_conn, bear1, direction="BEAR")
    snap = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=NOW,
        ingest_duration_ms=1, entries_count=3, hit_pagination_cap=False,
    )
    for rank, w in enumerate((bull1, bull2, bear1), start=1):
        await lb_repo.add_entry(
            db_conn, snapshot_id=snap, rank=rank, wallet=w,
            pnl_usd=Decimal("100"), volume_usd=Decimal("50"), trade_count=None,
        )
    await db_conn.commit()

    plain = await shortlist(db_conn, top=2, min_hit_rate=Decimal("0.5"))
    diverse = await shortlist(
        db_conn, top=2, min_hit_rate=Decimal("0.5"), diversify=True
    )
    plain_dirs = {e.strategy[2] for e in plain}
    diverse_dirs = {e.strategy[2] for e in diverse}
    # Diversified pick must span both directions; plain need not.
    assert diverse_dirs == {"BULL", "BEAR"}
    assert len(diverse) == 2


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


# -- shortlist --

async def _seed_two_snapshots(db_conn, w_keep: str, w_drop: str, w_new: str):
    """w_keep appears in both snapshots; w_drop only in snap1; w_new only in snap2."""
    for wallet in (w_keep, w_drop, w_new):
        await traders_repo.upsert(db_conn, wallet, now_ts=NOW)
    # snap1 (older) — has w_keep + w_drop
    snap1 = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=NOW - 86400 * 5,
        ingest_duration_ms=1, entries_count=2, hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap1, rank=1, wallet=w_keep,
        pnl_usd=Decimal("100"), volume_usd=Decimal("50"), trade_count=None,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap1, rank=2, wallet=w_drop,
        pnl_usd=Decimal("50"), volume_usd=Decimal("25"), trade_count=None,
    )
    # snap2 (today) — has w_keep + w_new
    snap2 = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=NOW,
        ingest_duration_ms=1, entries_count=2, hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap2, rank=1, wallet=w_keep,
        pnl_usd=Decimal("60"), volume_usd=Decimal("40"), trade_count=None,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap2, rank=2, wallet=w_new,
        pnl_usd=Decimal("60"), volume_usd=Decimal("40"), trade_count=None,
    )
    return snap1, snap2


async def test_shortlist_prefers_persisted_wallets(db_conn):
    w_keep = "0x" + "1" * 40
    w_drop = "0x" + "2" * 40
    w_new = "0x" + "3" * 40
    await _seed_two_snapshots(db_conn, w_keep, w_drop, w_new)
    # Seed a winning trade for each so they pass min_realized_pnl / min_hit_rate
    for wallet in (w_keep, w_new):  # w_drop not in latest snapshot, won't be analyzed
        await _seed(db_conn, wallet, side="BUY_YES", profitable=True)
    await db_conn.commit()

    entries = await shortlist(db_conn, top=2, min_hit_rate=Decimal("0.5"))
    # Both candidates qualify; w_keep persisted in 2 snapshots, w_new in 1
    assert len(entries) == 2
    assert entries[0].wallet == w_keep
    assert entries[0].persisted_days == 2
    assert entries[0].suggested_weight == Decimal("1.0")
    assert entries[1].wallet == w_new
    assert entries[1].persisted_days == 1
    assert entries[1].suggested_weight == Decimal("0.7")


async def test_shortlist_rejects_low_hit_rate(db_conn):
    wallet = "0x" + "9" * 40
    await traders_repo.upsert(db_conn, wallet, now_ts=NOW)
    snap_id = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=NOW,
        ingest_duration_ms=1, entries_count=1, hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap_id, rank=1, wallet=wallet,
        pnl_usd=Decimal("100"), volume_usd=Decimal("50"), trade_count=None,
    )
    await _seed(db_conn, wallet, side="BUY_NO", profitable=False)  # YES wins, NO loses → hit_rate 0
    await db_conn.commit()
    entries = await shortlist(db_conn, top=2, min_hit_rate=Decimal("0.5"))
    assert entries == []


def test_render_copy_sources_yaml_shape():
    from polymarket_alpha.helpers.strategies import ShortlistEntry

    e = ShortlistEntry(
        rank=4,
        wallet="0x1c01e123daca82058b51e61f679c25cfb4ddaa0f",
        realized_pnl=Decimal("9681"),
        hit_rate=Decimal("0.7650"),
        leaderboard_pnl=Decimal("5881"),
        strategy=("MIXED", "EDGE_SEEKER", "BEAR", "SCALPER"),
        persisted_days=2,
        suggested_weight=Decimal("1.0"),
        justification="strong",
    )
    out = render_copy_sources_yaml([e])
    assert "sources:" in out
    assert "shadow_1_bear" in out
    assert '"0x1c01e123daca82058b51e61f679c25cfb4ddaa0f"' in out
    assert "weight: 1.0" in out
    assert "per_trade_size_cap_usd: 3.0" in out
    assert "classes_eligible:" in out
    assert "intraday_15m" in out
