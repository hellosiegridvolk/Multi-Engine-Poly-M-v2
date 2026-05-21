"""TDD anchor: implied-probability math, all 4 BUY/SELL x YES/NO cases."""

from decimal import Decimal

import pytest

from polymarket_alpha.analysis.metrics import ImpliedProb, implied_yes_probability


def test_buy_yes():
    assert implied_yes_probability("YES", "BUY", Decimal("0.62")) == ImpliedProb(
        ">=", Decimal("0.6200")
    )


def test_buy_no():
    # P(YES) <= 1 - 0.62 = 0.38
    assert implied_yes_probability("NO", "BUY", Decimal("0.62")) == ImpliedProb(
        "<=", Decimal("0.3800")
    )


def test_sell_yes():
    assert implied_yes_probability("YES", "SELL", Decimal("0.62")) == ImpliedProb(
        "<=", Decimal("0.6200")
    )


def test_sell_no():
    assert implied_yes_probability("NO", "SELL", Decimal("0.62")) == ImpliedProb(
        ">=", Decimal("0.3800")
    )


def test_round_down_price():
    # 0.62559 must round DOWN to 0.6255 (not 0.6256)
    assert implied_yes_probability("YES", "BUY", Decimal("0.62559")).value == Decimal(
        "0.6255"
    )


def test_round_down_after_subtraction():
    # 1 - 0.62559 = 0.37441 -> ROUND_DOWN -> 0.3744
    assert implied_yes_probability("NO", "BUY", Decimal("0.62559")).value == Decimal(
        "0.3744"
    )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("1")])
@pytest.mark.parametrize(
    "direction,side", [("YES", "BUY"), ("NO", "BUY"), ("YES", "SELL"), ("NO", "SELL")]
)
def test_boundary_prices_no_exception(direction, side, price):
    result = implied_yes_probability(direction, side, price)
    assert isinstance(result.value, Decimal)
    assert result.value.as_tuple().exponent == -4
    assert Decimal("0") <= result.value <= Decimal("1")


@pytest.mark.parametrize(
    "direction,side", [("YES", "BUY"), ("NO", "BUY"), ("YES", "SELL"), ("NO", "SELL")]
)
@pytest.mark.parametrize("price", [Decimal("0.01"), Decimal("0.5"), Decimal("0.99")])
def test_grid_returns_decimal_4dp(direction, side, price):
    result = implied_yes_probability(direction, side, price)
    assert result.bound in (">=", "<=")
    assert result.value.as_tuple().exponent == -4


def test_invalid_combo_raises():
    with pytest.raises(ValueError):
        implied_yes_probability("MAYBE", "BUY", Decimal("0.5"))
