"""Domain models. All money/price fields are :class:`~decimal.Decimal`."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


def to_decimal(value: str | int | float | Decimal) -> Decimal:
    """Coerce to ``Decimal`` via ``str`` to avoid float binary artifacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Trader:
    rank: int
    proxy_wallet: str
    user_name: str
    x_username: str | None
    verified_badge: bool
    volume: Decimal
    pnl: Decimal
    profile_image: str | None


@dataclass(frozen=True, slots=True)
class Trade:
    proxy_wallet: str
    timestamp: int  # unix seconds
    condition_id: str
    size: Decimal  # shares
    usdc_size: Decimal  # notional USDC
    price: Decimal  # execution price, nominally 0..1
    asset: str  # clob token id (huge integer string)
    side: str  # "BUY" | "SELL"
    outcome_index: int  # 0 or 1
    outcome_label: str  # raw human label ("Yes"/"No"/team name)
    title: str
    slug: str
    event_slug: str
    transaction_hash: str


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    question: str
    slug: str
    end_date_iso: str | None
    closed: bool
    outcomes: tuple[str, ...]
    outcome_prices: tuple[Decimal, ...]
    clob_token_ids: tuple[str, ...]
    is_binary: bool
    is_crypto: bool
    winning_outcome_index: int | None  # None if open / unresolved / ambiguous


@dataclass(slots=True)
class StrategyDossier:
    trader: Trader
    generated_at: str  # ISO8601 UTC
    window: str  # day|week|month|all

    # --- metrics block ---
    n_markets: int
    n_crypto_markets: int
    n_trades: int
    realized_pnl: Decimal
    hit_rate: Decimal | None  # None if no resolved markets
    n_resolved_markets: int
    median_position_usdc: Decimal
    p90_position_usdc: Decimal
    max_position_usdc: Decimal
    top3_concentration_pct: Decimal  # 0..100
    median_holding_period_seconds: int | None
    vwap_entry_by_market: dict[str, Decimal]
    implied_prob_by_market: dict[str, Decimal | None]

    # --- categorical tags ---
    sizing_style: str
    conviction: str
    direction_bias: str
    speed: str

    notes: list[str] = field(default_factory=list)
    raw: dict | None = None  # populated only if --include-raw
