"""Shared test fixtures and sample payloads (mocks live here only)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_alpha.models import Market, Trade


def make_trade(
    *,
    condition_id: str = "0xcond",
    asset: str = "111",
    side: str = "BUY",
    outcome_index: int = 0,
    size: str = "100",
    usdc: str = "50",
    price: str = "0.50",
    timestamp: int = 1_000_000,
    tx: str = "0xtx",
    outcome_label: str = "Yes",
) -> Trade:
    return Trade(
        proxy_wallet="0xwallet",
        timestamp=timestamp,
        condition_id=condition_id,
        size=Decimal(size),
        usdc_size=Decimal(usdc),
        price=Decimal(price),
        asset=asset,
        side=side,
        outcome_index=outcome_index,
        outcome_label=outcome_label,
        title="T",
        slug="market-slug",
        event_slug="event-slug",
        transaction_hash=tx,
    )


def make_market(
    *,
    condition_id: str = "0xcond",
    outcomes: tuple[str, ...] = ("Yes", "No"),
    prices: tuple[str, ...] = ("0.5", "0.5"),
    token_ids: tuple[str, ...] = ("111", "222"),
    closed: bool = False,
    is_crypto: bool = True,
    winning_outcome_index: int | None = None,
) -> Market:
    is_binary = len(outcomes) == 2
    return Market(
        condition_id=condition_id,
        question="Q?",
        slug="market-slug",
        end_date_iso=None,
        closed=closed,
        outcomes=outcomes,
        outcome_prices=tuple(Decimal(p) for p in prices),
        clob_token_ids=token_ids,
        is_binary=is_binary,
        is_crypto=is_crypto,
        winning_outcome_index=winning_outcome_index,
    )


@pytest.fixture
async def db_conn(tmp_path):
    from polymarket_alpha import storage

    conn = await storage.connect(tmp_path / "test.db")
    await storage.run_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def trade_factory():
    return make_trade


@pytest.fixture
def market_factory():
    return make_market
