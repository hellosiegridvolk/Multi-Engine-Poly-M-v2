"""Wallet staleness table — byte-for-byte format."""

from decimal import Decimal

from polymarket_alpha.helpers.wallet_activity import WalletActivityTracker
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)

NOW = 2_000_000_000


def _row(wallet, atype, ts, tx):
    return ActivityRow(
        wallet=wallet,
        activity_type=atype,
        condition_id="0xc",
        token_id="111",
        side="BUY" if atype == "TRADE" else None,
        outcome="YES" if atype == "TRADE" else None,
        shares=Decimal("1"),
        usdc=Decimal("1"),
        price=Decimal("0.5"),
        timestamp=ts,
        tx_hash=tx,
        source="REST",
    )


async def test_render_table_exact(db_conn):
    w1 = "0x1111111111111111111111111111111111111111"
    w2 = "0x2222222222222222222222222222222222222222"
    await traders_repo.upsert(db_conn, w1, now_ts=NOW)
    await traders_repo.upsert(db_conn, w2, now_ts=NOW)
    # w1: TRADE 30s ago -> live ; w2: REDEEM 3d ago -> idle, no trade
    await insert_or_ignore(db_conn, _row(w1, "TRADE", NOW - 30, "0xa"), ingested_ts=NOW)
    await insert_or_ignore(
        db_conn, _row(w2, "REDEEM", NOW - 3 * 86400, "0xb"), ingested_ts=NOW
    )
    await db_conn.commit()

    tracker = WalletActivityTracker(db_conn, now_fn=lambda: NOW)
    out = await tracker.render_table([w1, w2])

    expected = "\n".join(
        [
            f"{'WALLET':<42}  {'TIER':<6}  {'LAST_ACT':<8}  {'ACT_LAG':>7}  {'TRADE_LAG':>9}",
            "-" * 80,
            f"{w1:<42}  {'live':<6}  {'TRADE':<8}  {'30s':>7}  {'30s':>9}",
            f"{w2:<42}  {'idle':<6}  {'REDEEM':<8}  {'3d':>7}  {'-':>9}",
        ]
    )
    assert out == expected


async def test_unknown_wallet_idle(db_conn):
    tracker = WalletActivityTracker(db_conn, now_fn=lambda: NOW)
    wa = await tracker.get("0xabsent")
    assert wa.tier == "idle"
    assert wa.last_trade_lag_s is None
    assert wa.recent_gaps_s == []
