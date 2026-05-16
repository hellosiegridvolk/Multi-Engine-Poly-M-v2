"""Metrics + sizing-style classifier."""

from decimal import Decimal

from polymarket_alpha.analysis.dossier import DossierConfig, classify_sizing_style
from polymarket_alpha.analysis.metrics import (
    _concentration,
    _position_distribution,
    _vwap_entry,
    compute_trader_metrics,
)
from polymarket_alpha.tests.conftest import make_market, make_trade

CFG = DossierConfig()


# ---- sizing-style classifier ----

def test_sizing_single_shot():
    entries = {"m": [make_trade(usdc="100")]}
    assert classify_sizing_style(entries, CFG) == "SINGLE_SHOT"


def test_sizing_pyramid():
    entries = {
        "m": [
            make_trade(usdc="100", timestamp=1),
            make_trade(usdc="200", timestamp=2),
            make_trade(usdc="400", timestamp=3),
        ]
    }
    assert classify_sizing_style(entries, CFG) == "PYRAMID"


def test_sizing_scaling_in():
    entries = {
        "m": [
            make_trade(usdc="400", timestamp=1),
            make_trade(usdc="200", timestamp=2),
            make_trade(usdc="100", timestamp=3),
        ]
    }
    assert classify_sizing_style(entries, CFG) == "SCALING_IN"


def test_sizing_mixed():
    entries = {
        "m": [
            make_trade(usdc="100", timestamp=1),
            make_trade(usdc="300", timestamp=2),
            make_trade(usdc="150", timestamp=3),
            make_trade(usdc="280", timestamp=4),
        ]
    }
    assert classify_sizing_style(entries, CFG) == "MIXED"


# ---- pure metric helpers ----

def test_vwap_entry_volume_weighted():
    buys = [
        make_trade(side="BUY", price="0.40", usdc="40"),
        make_trade(side="BUY", price="0.60", usdc="60"),
    ]
    assert _vwap_entry(buys) == Decimal("0.5200")


def test_position_distribution():
    trades = [make_trade(usdc=str(u)) for u in (10, 20, 30, 40, 1000)]
    median, p90, mx = _position_distribution(trades)
    assert median == Decimal("30")
    assert p90 == Decimal("1000")
    assert mx == Decimal("1000")


def test_concentration_top3():
    notional = {"a": Decimal("30"), "b": Decimal("30"), "c": Decimal("20"),
                "d": Decimal("10"), "e": Decimal("10")}
    assert _concentration(notional) == Decimal("80.00")


# ---- aggregate compute_trader_metrics ----

def test_holding_period_round_trip():
    cid = "0xhold"
    market = make_market(condition_id=cid, closed=False, winning_outcome_index=None)
    trades = [
        make_trade(condition_id=cid, side="BUY", outcome_index=0, timestamp=1000),
        make_trade(condition_id=cid, side="SELL", outcome_index=0, timestamp=1000 + 3600),
    ]
    m = compute_trader_metrics(trades, {cid: market})
    assert m.median_holding_period_seconds == 3600


def test_realized_pnl_and_hit_rate():
    cid = "0xwin"
    market = make_market(
        condition_id=cid, closed=True, winning_outcome_index=0
    )
    trade = make_trade(
        condition_id=cid, side="BUY", outcome_index=0, size="100",
        usdc="40", price="0.40",
    )
    m = compute_trader_metrics([trade], {cid: market})
    # cash = -40, settlement = 100 winning shares -> pnl 60
    assert m.realized_pnl == Decimal("60")
    assert m.n_resolved_markets == 1
    assert m.hit_rate == Decimal("1.0000")


def test_open_market_excluded_from_pnl():
    cid = "0xopen"
    market = make_market(condition_id=cid, closed=False, winning_outcome_index=None)
    trade = make_trade(condition_id=cid)
    m = compute_trader_metrics([trade], {cid: market})
    assert m.n_resolved_markets == 0
    assert m.hit_rate is None
    assert any("open/unresolved" in n for n in m.notes)
    assert m.n_markets == 1  # still counted


def test_empty_trades_no_exception():
    m = compute_trader_metrics([], {})
    assert m.n_trades == 0
    assert m.realized_pnl == Decimal("0")
    assert m.hit_rate is None
    assert m.median_holding_period_seconds is None
    assert "no crypto trades for wallet" in m.notes


def test_direction_conviction_speed_combined():
    markets: dict = {}
    trades = []
    # 8 markets: BUY YES @0.75 + SELL YES @0.75 (12h hold) ; 2 markets: BUY NO @0.25
    for i in range(10):
        cid = f"0xc{i}"
        markets[cid] = make_market(condition_id=cid, is_crypto=True)
        if i < 8:
            trades.append(
                make_trade(condition_id=cid, side="BUY", outcome_index=0,
                           price="0.75", usdc="50", timestamp=0)
            )
            trades.append(
                make_trade(condition_id=cid, side="SELL", outcome_index=0,
                           price="0.75", usdc="75", timestamp=43200)
            )
        else:
            trades.append(
                make_trade(condition_id=cid, side="BUY", outcome_index=1,
                           price="0.25", usdc="50", timestamp=0)
            )
            trades.append(
                make_trade(condition_id=cid, side="SELL", outcome_index=1,
                           price="0.25", usdc="60", timestamp=43200)
            )
    m = compute_trader_metrics(trades, markets)

    assert m.buy_yes_count == 8
    assert m.buy_no_count == 2
    # every trade implies P(YES) = 0.75
    assert m.weighted_avg_entry_pyes == Decimal("0.7500")
    assert m.median_holding_period_seconds == 43200

    from polymarket_alpha.analysis.dossier import (
        classify_conviction,
        classify_direction_bias,
        classify_speed,
    )

    notes: list[str] = []
    assert classify_direction_bias(
        m.buy_yes_count, m.buy_no_count, m.buy_yes_usdc, m.buy_no_usdc, CFG, notes
    ) == "BULL"
    assert classify_conviction(m.weighted_avg_entry_pyes, CFG, notes) == "HIGH_CONVICTION"
    assert classify_speed(m.median_holding_period_seconds, CFG, notes) == "SCALPER"


def test_non_crypto_market_excluded():
    cid = "0xstock"
    market = make_market(condition_id=cid, is_crypto=False)
    trade = make_trade(condition_id=cid)
    m = compute_trader_metrics([trade], {cid: market})
    assert m.n_trades == 0
    assert m.n_crypto_markets == 0
    assert any("not crypto-tagged" in n for n in m.notes)
