"""Gamma parsing: the clobTokenIds/outcomes double-JSON-decode gotcha."""

from decimal import Decimal

import httpx
import pytest
import respx

from polymarket_alpha.analysis.metrics import classify_trade_direction
from polymarket_alpha.clients.gamma import GAMMA_API, GammaClient, _parse_market
from polymarket_alpha.http import build_client
from polymarket_alpha.tests.conftest import make_trade


def _raw(**over) -> dict:
    base = {
        "conditionId": "0xcond",
        "question": "Will BTC moon?",
        "slug": "btc-moon",
        "endDate": "2026-07-31T12:00:00Z",
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.5", "0.5"]',
    }
    base.update(over)
    return base


def test_double_decode_binary():
    m = _parse_market(_raw())
    assert m is not None
    assert m.clob_token_ids == ("111", "222")
    assert m.outcomes == ("Yes", "No")
    assert m.outcome_prices == (Decimal("0.5"), Decimal("0.5"))
    assert m.is_binary is True


def test_resolved_winner_from_prices():
    m = _parse_market(_raw(closed=True, outcomePrices='["1", "0"]'))
    assert m.winning_outcome_index == 0


def test_reversed_outcomes_order():
    m = _parse_market(
        _raw(closed=True, outcomes='["No", "Yes"]', outcomePrices='["0", "1"]')
    )
    assert m.is_binary is True
    assert m.winning_outcome_index == 1
    # outcome_index 1 is "Yes" here -> classify as YES
    trade = make_trade(outcome_index=1)
    assert classify_trade_direction(trade, m) == "YES"
    assert classify_trade_direction(make_trade(outcome_index=0), m) == "NO"


def test_up_down_market_is_binary():
    # Crypto markets are mostly ["Up","Down"] — treated binary, idx0 == YES-equiv.
    m = _parse_market(_raw(outcomes='["Up", "Down"]', outcomePrices='["0.5", "0.5"]'))
    assert m.is_binary is True
    assert classify_trade_direction(make_trade(outcome_index=0), m) == "YES"
    assert classify_trade_direction(make_trade(outcome_index=1), m) == "NO"


def test_multi_outcome_market_is_non_binary():
    m = _parse_market(
        _raw(
            closed=True,
            outcomes='["Alice", "Bob", "Carol"]',
            clobTokenIds='["111", "222", "333"]',
            outcomePrices='["0", "1", "0"]',
        )
    )
    assert m.is_binary is False
    assert m.winning_outcome_index == 1  # still derivable
    assert classify_trade_direction(make_trade(), m) is None


def test_open_market_no_winner():
    m = _parse_market(_raw(closed=False, outcomePrices='["1", "0"]'))
    assert m.winning_outcome_index is None


def test_degenerate_resolved_is_unresolved():
    m = _parse_market(_raw(closed=True, outcomePrices='["0.5", "0.5"]'))
    assert m.winning_outcome_index is None


def test_bad_encoded_field_skipped():
    assert _parse_market(_raw(clobTokenIds="{not-json")) is None


@respx.mock
def test_fetch_markets_by_token_ids_batches():
    route = respx.get(f"{GAMMA_API}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                _raw(conditionId="0xA", clobTokenIds='["111", "222"]'),
                _raw(conditionId="0xB", clobTokenIds='["333", "444"]'),
            ],
        )
    )
    with build_client() as client:
        markets = GammaClient(client).fetch_markets_by_token_ids(["111", "333"])
    assert set(markets) == {"0xA", "0xB"}
    sent = route.calls[0].request.url
    assert "clob_token_ids=111" in str(sent)
    assert "clob_token_ids=333" in str(sent)


@respx.mock
def test_resolve_crypto_tag_id_ok():
    respx.get(f"{GAMMA_API}/tags/slug/crypto").mock(
        return_value=httpx.Response(200, json={"id": "21", "slug": "crypto"})
    )
    with build_client() as client:
        assert GammaClient(client).resolve_crypto_tag_id() == "21"


@respx.mock
def test_resolve_crypto_tag_id_fallback(caplog):
    respx.get(f"{GAMMA_API}/tags/slug/crypto").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with build_client() as client:
        with caplog.at_level("WARNING"):
            assert GammaClient(client).resolve_crypto_tag_id() == "21"
    assert any("falling back" in r.message for r in caplog.records)
