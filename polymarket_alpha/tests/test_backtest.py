"""backtest: forward-PnL measurement of a past shortlist vs the field."""

from decimal import Decimal

from polymarket_alpha.helpers.backtest import (
    backtest,
    backtest_to_dict,
    render_backtest_human,
)
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import markets as markets_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)

AS_OF = 1_800_000_000
BEFORE = AS_OF - 86400          # a trade placed before the pick date
AFTER = AS_OF + 86400           # a trade placed after the pick date (forward)


async def _market(db_conn, cid: str):
    await markets_repo.upsert_market(
        db_conn, condition_id=cid, slug=cid, question="q", end_date_ts=AS_OF,
        resolved=True, resolution="YES", category_tags=["crypto"],
        is_crypto=True, resolved_at_ts=AS_OF, metadata_fetched_ts=AS_OF,
    )
    await markets_repo.upsert_token(
        db_conn, token_id="y" + cid, condition_id=cid, outcome="YES", outcome_index=0
    )
    await markets_repo.upsert_token(
        db_conn, token_id="n" + cid, condition_id=cid, outcome="NO", outcome_index=1
    )


async def _trade(db_conn, wallet, cid, *, ts, outcome="YES", tx):
    await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet=wallet, activity_type="TRADE", condition_id=cid,
            token_id=("y" if outcome == "YES" else "n") + cid,
            side="BUY", outcome=outcome,
            shares=Decimal("100"), usdc=Decimal("40"), price=Decimal("0.40"),
            timestamp=ts, tx_hash=tx, source="REST",
        ),
        ingested_ts=AS_OF,
    )


async def test_backtest_measures_forward_pnl(db_conn):
    # One snapshot at AS_OF; one wallet that wins both before and after it.
    wallet = "0x" + "1" * 40
    await traders_repo.upsert(db_conn, wallet, now_ts=AS_OF)
    snap = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=AS_OF,
        ingest_duration_ms=1, entries_count=1, hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap, rank=1, wallet=wallet,
        pnl_usd=Decimal("100"), volume_usd=Decimal("50"), trade_count=None,
    )
    await _market(db_conn, "0xc1")
    await _market(db_conn, "0xc2")
    # before-pick trade (counts toward as-of PnL only)
    await _trade(db_conn, wallet, "0xc1", ts=BEFORE, tx="0xbefore")
    # forward trade (counts toward forward PnL): BUY YES @0.40 on YES market
    #   cash -40 + settle 100 = +60
    await _trade(db_conn, wallet, "0xc2", ts=AFTER, tx="0xafter")
    await db_conn.commit()

    report = await backtest(db_conn, top=2, as_of_ts=AS_OF)
    assert report.as_of_ts == AS_OF
    assert report.n_day_snapshots_available == 1
    assert len(report.picks) == 1
    pick = report.picks[0]
    assert pick.wallet == wallet
    # forward window: only the AFTER trade → +60 on 1 resolved market
    assert pick.forward_realized_pnl == Decimal("60")
    assert pick.forward_resolved_markets == 1
    assert pick.forward_hit_rate == Decimal("1.0000")


async def test_backtest_verdict_and_serialization(db_conn):
    wallet = "0x" + "2" * 40
    await traders_repo.upsert(db_conn, wallet, now_ts=AS_OF)
    snap = await lb_repo.create_snapshot(
        db_conn, period="day", snapshot_ts=AS_OF,
        ingest_duration_ms=1, entries_count=1, hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn, snapshot_id=snap, rank=1, wallet=wallet,
        pnl_usd=Decimal("100"), volume_usd=Decimal("50"), trade_count=None,
    )
    await _market(db_conn, "0xcA")
    await _trade(db_conn, wallet, "0xcA", ts=BEFORE, tx="0xb")
    await db_conn.commit()

    report = await backtest(db_conn, top=2, as_of_ts=AS_OF)
    # no forward trades anywhere → verdict reflects that
    assert report.verdict in (
        "PICKS MATCHED FIELD", "PICKS LAGGED FIELD",
        "PICKS BEAT FIELD", "INSUFFICIENT DATA",
    )
    human = render_backtest_human(report)
    assert "VERDICT:" in human
    assert "only one day-snapshot" in human  # thin-data note fires

    d = backtest_to_dict(report)
    assert d["as_of_ts"] == AS_OF
    assert "verdict" in d and "picks" in d
    assert isinstance(d["edge"], str)


async def test_backtest_empty_db(db_conn):
    report = await backtest(db_conn, top=2)
    assert report.verdict == "INSUFFICIENT DATA"
    assert report.picks == []
