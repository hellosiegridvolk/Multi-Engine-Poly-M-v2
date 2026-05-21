"""Quant metrics. ``Decimal`` throughout; prices rounded ``ROUND_DOWN``."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Mapping, Sequence

from polymarket_alpha.models import Market, Trade

PRICE_QUANT = Decimal("0.0001")  # 4dp
PCT_QUANT = Decimal("0.01")  # 2dp
ONE = Decimal(1)
ZERO = Decimal(0)

Direction = Literal["YES", "NO"]


@dataclass(frozen=True, slots=True)
class ImpliedProb:
    bound: str  # ">=" or "<="
    value: Decimal


def _q_down(value: Decimal, quant: Decimal = PRICE_QUANT) -> Decimal:
    return value.quantize(quant, rounding=ROUND_DOWN)


def implied_yes_probability(direction: str, side: str, price: Decimal) -> ImpliedProb:
    """Implied bound on P(YES) revealed by a single trade.

    BUY YES @p  -> P(YES) >= p
    BUY NO  @p  -> P(YES) <= 1 - p
    SELL YES @p -> P(YES) <= p
    SELL NO  @p -> P(YES) >= 1 - p

    The price-rounding rule (ROUND_DOWN, 4dp) is applied to the final reported
    bound: ``1 - p`` is computed from the raw price first, then quantized.
    """
    key = (direction.upper(), side.upper())
    if key == ("YES", "BUY"):
        return ImpliedProb(">=", _q_down(price))
    if key == ("NO", "BUY"):
        return ImpliedProb("<=", _q_down(ONE - price))
    if key == ("YES", "SELL"):
        return ImpliedProb("<=", _q_down(price))
    if key == ("NO", "SELL"):
        return ImpliedProb(">=", _q_down(ONE - price))
    raise ValueError(f"invalid (direction, side): {direction!r}, {side!r}")


def classify_trade_direction(trade: Trade, market: Market) -> Direction | None:
    """Map a trade to YES/NO for 2-outcome markets; ``None`` for multi-outcome.

    For literal Yes/No markets the "yes" outcome is located (handles a
    reversed ``["No","Yes"]`` order). For other 2-outcome markets (e.g.
    ``["Up","Down"]``) outcome index 0 is the YES-equivalent.
    """
    if not market.is_binary or len(market.outcomes) != 2:
        return None
    labels = [o.strip().lower() for o in market.outcomes]
    if "yes" in labels and "no" in labels:
        yes_index = labels.index("yes")
    else:
        yes_index = 0
    return "YES" if trade.outcome_index == yes_index else "NO"


@dataclass(frozen=True, slots=True)
class TraderMetrics:
    n_markets: int
    n_crypto_markets: int
    n_trades: int
    realized_pnl: Decimal
    hit_rate: Decimal | None
    n_resolved_markets: int
    median_position_usdc: Decimal
    p90_position_usdc: Decimal
    max_position_usdc: Decimal
    top3_concentration_pct: Decimal
    median_holding_period_seconds: int | None
    vwap_entry_by_market: dict[str, Decimal]
    implied_prob_by_market: dict[str, Decimal | None]
    weighted_avg_entry_pyes: Decimal | None
    buy_yes_count: int
    buy_no_count: int
    buy_yes_usdc: Decimal
    buy_no_usdc: Decimal
    per_market_entries: dict[str, list[Trade]]
    notes: list[str]


def _vwap_entry(buys: Sequence[Trade]) -> Decimal | None:
    total_usdc = sum((t.usdc_size for t in buys), ZERO)
    if total_usdc <= ZERO:
        return None
    weighted = sum((t.price * t.usdc_size for t in buys), ZERO)
    return _q_down(weighted / total_usdc)


def _position_distribution(
    trades: Sequence[Trade],
) -> tuple[Decimal, Decimal, Decimal]:
    sizes = sorted(t.usdc_size for t in trades)
    if not sizes:
        return ZERO, ZERO, ZERO
    n = len(sizes)
    mid = n // 2
    if n % 2 == 1:
        median = sizes[mid]
    else:
        median = (sizes[mid - 1] + sizes[mid]) / Decimal(2)
    p90_idx = max(math.ceil(Decimal("0.9") * n) - 1, 0)
    p90 = sizes[p90_idx]
    return median, p90, sizes[-1]


def _concentration(market_notional: Mapping[str, Decimal]) -> Decimal:
    total = sum(market_notional.values(), ZERO)
    if total <= ZERO:
        return ZERO
    top3 = sum(sorted(market_notional.values(), reverse=True)[:3], ZERO)
    return _q_down(top3 / total * Decimal(100), PCT_QUANT)


def _holding_periods(
    per_market_trades: Mapping[str, list[Trade]],
    notes: list[str],
) -> int | None:
    durations: list[int] = []
    for cid, trades in per_market_trades.items():
        buys = [t for t in trades if t.side == "BUY"]
        sells = [t for t in trades if t.side == "SELL"]
        if not buys:
            continue
        first_entry = min(t.timestamp for t in buys)
        if sells:
            last_exit = max(t.timestamp for t in sells)
        else:
            last_exit = max(t.timestamp for t in trades)
            notes.append(f"market {cid}: no exit (open) — holding period is lower bound")
        durations.append(max(last_exit - first_entry, 0))
    if not durations:
        return None
    durations.sort()
    n = len(durations)
    mid = n // 2
    if n % 2 == 1:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) // 2


def _realized_pnl_for_market(trades: Sequence[Trade], market: Market) -> Decimal:
    w = market.winning_outcome_index
    assert w is not None
    cash = ZERO
    net_shares: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for t in trades:
        if t.side == "BUY":
            cash -= t.usdc_size
            net_shares[t.outcome_index] += t.size
        else:  # SELL
            cash += t.usdc_size
            net_shares[t.outcome_index] -= t.size
    settlement = net_shares.get(w, ZERO)  # winning share pays 1, losing pays 0
    return cash + settlement


def compute_trader_metrics(
    trades: Sequence[Trade],
    markets: Mapping[str, Market],
    *,
    crypto_only: bool = True,
) -> TraderMetrics:
    notes: list[str] = []

    all_conditions = {t.condition_id for t in trades}
    crypto_conditions = {
        cid for cid in all_conditions if cid in markets and markets[cid].is_crypto
    }

    excluded = all_conditions - crypto_conditions
    for cid in sorted(excluded):
        if cid not in markets:
            notes.append(f"no Gamma metadata for {cid} — excluded (treated non-crypto)")
        else:
            notes.append(f"market {cid} not crypto-tagged — excluded from metrics")

    if crypto_only:
        scoped = [t for t in trades if t.condition_id in crypto_conditions]
    else:
        scoped = list(trades)

    if not scoped:
        notes.append("no crypto trades for wallet")
        return TraderMetrics(
            n_markets=len(all_conditions),
            n_crypto_markets=len(crypto_conditions),
            n_trades=0,
            realized_pnl=ZERO,
            hit_rate=None,
            n_resolved_markets=0,
            median_position_usdc=ZERO,
            p90_position_usdc=ZERO,
            max_position_usdc=ZERO,
            top3_concentration_pct=ZERO,
            median_holding_period_seconds=None,
            vwap_entry_by_market={},
            implied_prob_by_market={},
            weighted_avg_entry_pyes=None,
            buy_yes_count=0,
            buy_no_count=0,
            buy_yes_usdc=ZERO,
            buy_no_usdc=ZERO,
            per_market_entries={},
            notes=notes,
        )

    by_market: dict[str, list[Trade]] = defaultdict(list)
    for t in scoped:
        by_market[t.condition_id].append(t)

    vwap_by_market: dict[str, Decimal] = {}
    implied_by_market: dict[str, Decimal | None] = {}
    per_market_entries: dict[str, list[Trade]] = {}
    market_notional: dict[str, Decimal] = {}

    buy_yes_count = buy_no_count = 0
    buy_yes_usdc = buy_no_usdc = ZERO
    weighted_prob_num = ZERO  # Σ implied_pyes * usdc
    weighted_prob_den = ZERO  # Σ usdc

    for cid, mtrades in by_market.items():
        market = markets[cid]
        market_notional[cid] = sum((t.usdc_size for t in mtrades), ZERO)

        buys = sorted(
            (t for t in mtrades if t.side == "BUY"), key=lambda t: t.timestamp
        )
        per_market_entries[cid] = buys
        vwap = _vwap_entry(buys)
        if vwap is not None:
            vwap_by_market[cid] = vwap

        m_prob_num = ZERO
        m_prob_den = ZERO
        for t in mtrades:
            direction = classify_trade_direction(t, market)
            if direction is None:
                continue
            ip = implied_yes_probability(direction, t.side, t.price)
            m_prob_num += ip.value * t.usdc_size
            m_prob_den += t.usdc_size
            if t.side == "BUY":
                if direction == "YES":
                    buy_yes_count += 1
                    buy_yes_usdc += t.usdc_size
                else:
                    buy_no_count += 1
                    buy_no_usdc += t.usdc_size

        if m_prob_den > ZERO:
            implied_by_market[cid] = _q_down(m_prob_num / m_prob_den)
            weighted_prob_num += m_prob_num
            weighted_prob_den += m_prob_den
        else:
            implied_by_market[cid] = None
            if not market.is_binary:
                notes.append(
                    f"market {cid}: non-binary outcomes "
                    f"{list(market.outcomes)} — excluded from conviction/direction"
                )

    median_usdc, p90_usdc, max_usdc = _position_distribution(scoped)
    concentration = _concentration(market_notional)
    median_hold = _holding_periods(by_market, notes)

    realized_pnl = ZERO
    n_resolved = 0
    wins = 0
    for cid, mtrades in by_market.items():
        market = markets[cid]
        if market.winning_outcome_index is None:
            notes.append(f"market {market.slug or cid}: open/unresolved — excluded from PnL")
            continue
        n_resolved += 1
        pnl_m = _realized_pnl_for_market(mtrades, market)
        realized_pnl += pnl_m
        if pnl_m > ZERO:
            wins += 1

    hit_rate = (
        _q_down(Decimal(wins) / Decimal(n_resolved), PRICE_QUANT)
        if n_resolved > 0
        else None
    )
    weighted_avg_pyes = (
        _q_down(weighted_prob_num / weighted_prob_den)
        if weighted_prob_den > ZERO
        else None
    )

    return TraderMetrics(
        n_markets=len(all_conditions),
        n_crypto_markets=len(crypto_conditions),
        n_trades=len(scoped),
        realized_pnl=realized_pnl,
        hit_rate=hit_rate,
        n_resolved_markets=n_resolved,
        median_position_usdc=median_usdc,
        p90_position_usdc=p90_usdc,
        max_position_usdc=max_usdc,
        top3_concentration_pct=concentration,
        median_holding_period_seconds=median_hold,
        vwap_entry_by_market=vwap_by_market,
        implied_prob_by_market=implied_by_market,
        weighted_avg_entry_pyes=weighted_avg_pyes,
        buy_yes_count=buy_yes_count,
        buy_no_count=buy_no_count,
        buy_yes_usdc=buy_yes_usdc,
        buy_no_usdc=buy_no_usdc,
        per_market_entries=per_market_entries,
        notes=notes,
    )
