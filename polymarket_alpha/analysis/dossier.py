"""Behavior-pattern profiling. All thresholds live in :class:`DossierConfig`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from polymarket_alpha.analysis.metrics import TraderMetrics
from polymarket_alpha.models import StrategyDossier, Trade, Trader

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class DossierConfig:
    # Sizing
    single_shot_max_entries: int = 1
    pyramid_min_increasing_ratio: Decimal = Decimal("0.75")
    scaling_in_min_decreasing_ratio: Decimal = Decimal("0.75")
    # Conviction (on usdc-weighted implied P(YES))
    high_conviction_high: Decimal = Decimal("0.70")
    high_conviction_low: Decimal = Decimal("0.30")
    edge_seeker_low: Decimal = Decimal("0.40")
    edge_seeker_high: Decimal = Decimal("0.60")
    # Direction (share of BUY-YES vs BUY-NO by trade count)
    direction_bull_min: Decimal = Decimal("0.60")
    direction_bear_min: Decimal = Decimal("0.60")
    near_tie_band: Decimal = Decimal("0.02")
    # Speed (median holding period, seconds)
    scalper_max_seconds: int = 24 * 3600
    swing_max_seconds: int = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class DossierTags:
    sizing_style: str
    conviction: str
    direction_bias: str
    speed: str
    notes: list[str]


def classify_sizing_style(
    per_market_entries: Mapping[str, list[Trade]],
    cfg: DossierConfig,
) -> str:
    entry_counts = [len(v) for v in per_market_entries.values() if v]
    if not entry_counts:
        return "MIXED"
    if all(c <= cfg.single_shot_max_entries for c in entry_counts):
        return "SINGLE_SHOT"

    positive = 0
    non_positive = 0
    for entries in per_market_entries.values():
        for prev, cur in zip(entries, entries[1:]):
            if cur.usdc_size > prev.usdc_size:
                positive += 1
            else:
                non_positive += 1

    total = positive + non_positive
    if total == 0:
        return "SINGLE_SHOT"
    pos_ratio = Decimal(positive) / Decimal(total)
    nonpos_ratio = Decimal(non_positive) / Decimal(total)
    if pos_ratio >= cfg.pyramid_min_increasing_ratio:
        return "PYRAMID"
    if nonpos_ratio >= cfg.scaling_in_min_decreasing_ratio:
        return "SCALING_IN"
    return "MIXED"


def classify_conviction(
    weighted_avg_entry_pyes: Decimal | None,
    cfg: DossierConfig,
    notes: list[str],
) -> str:
    if weighted_avg_entry_pyes is None:
        notes.append("conviction: no binary crypto data — defaulting MIXED")
        return "MIXED"
    p = weighted_avg_entry_pyes
    if p >= cfg.high_conviction_high or p <= cfg.high_conviction_low:
        return "HIGH_CONVICTION"
    if cfg.edge_seeker_low <= p <= cfg.edge_seeker_high:
        return "EDGE_SEEKER"
    return "MIXED"


def classify_direction_bias(
    buy_yes_count: int,
    buy_no_count: int,
    buy_yes_usdc: Decimal,
    buy_no_usdc: Decimal,
    cfg: DossierConfig,
    notes: list[str],
) -> str:
    total = buy_yes_count + buy_no_count
    if total == 0:
        notes.append("direction: no BUY trades in crypto binary markets — NEUTRAL")
        return "NEUTRAL"
    share_yes = Decimal(buy_yes_count) / Decimal(total)
    share_no = Decimal(buy_no_count) / Decimal(total)

    near = cfg.near_tie_band
    if abs(share_yes - cfg.direction_bull_min) <= near or abs(
        share_no - cfg.direction_bear_min
    ) <= near:
        usdc_total = buy_yes_usdc + buy_no_usdc
        if usdc_total > ZERO:
            share_yes = buy_yes_usdc / usdc_total
            share_no = buy_no_usdc / usdc_total

    if share_yes >= cfg.direction_bull_min:
        return "BULL"
    if share_no >= cfg.direction_bear_min:
        return "BEAR"
    return "NEUTRAL"


def classify_speed(
    median_holding_seconds: int | None,
    cfg: DossierConfig,
    notes: list[str],
) -> str:
    if median_holding_seconds is None:
        notes.append("speed: no completed round-trips — defaulting POSITIONAL")
        return "POSITIONAL"
    if median_holding_seconds < cfg.scalper_max_seconds:
        return "SCALPER"
    if median_holding_seconds <= cfg.swing_max_seconds:
        return "SWING"
    return "POSITIONAL"


def build_dossier(
    trader: Trader,
    metrics: TraderMetrics,
    *,
    window: str,
    cfg: DossierConfig = DossierConfig(),
    include_raw: bool = False,
    raw_payloads: dict | None = None,
) -> StrategyDossier:
    tag_notes: list[str] = []
    sizing = classify_sizing_style(metrics.per_market_entries, cfg)
    conviction = classify_conviction(metrics.weighted_avg_entry_pyes, cfg, tag_notes)
    direction = classify_direction_bias(
        metrics.buy_yes_count,
        metrics.buy_no_count,
        metrics.buy_yes_usdc,
        metrics.buy_no_usdc,
        cfg,
        tag_notes,
    )
    speed = classify_speed(metrics.median_holding_period_seconds, cfg, tag_notes)

    return StrategyDossier(
        trader=trader,
        generated_at=datetime.now(timezone.utc).isoformat(),
        window=window,
        n_markets=metrics.n_markets,
        n_crypto_markets=metrics.n_crypto_markets,
        n_trades=metrics.n_trades,
        realized_pnl=metrics.realized_pnl,
        hit_rate=metrics.hit_rate,
        n_resolved_markets=metrics.n_resolved_markets,
        median_position_usdc=metrics.median_position_usdc,
        p90_position_usdc=metrics.p90_position_usdc,
        max_position_usdc=metrics.max_position_usdc,
        top3_concentration_pct=metrics.top3_concentration_pct,
        median_holding_period_seconds=metrics.median_holding_period_seconds,
        vwap_entry_by_market=metrics.vwap_entry_by_market,
        implied_prob_by_market=metrics.implied_prob_by_market,
        sizing_style=sizing,
        conviction=conviction,
        direction_bias=direction,
        speed=speed,
        notes=[*metrics.notes, *tag_notes],
        raw=raw_payloads if include_raw else None,
    )
