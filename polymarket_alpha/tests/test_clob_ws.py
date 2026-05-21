"""CLOB WS client: trade parse + re-subscribe on reconnect."""

import asyncio
import json

import pytest

import polymarket_alpha.clients.clob_ws as ws_mod
from polymarket_alpha.clients.clob_ws import ClobWsClient, _parse_trade


def test_parse_last_trade_price():
    ev = {
        "event_type": "last_trade_price",
        "market": "0xcond",
        "asset_id": "111",
        "price": "0.68",
        "size": "23.6",
        "side": "buy",
        "timestamp": "1779183027283",
        "transaction_hash": "0xabc",
    }
    t = _parse_trade(ev)
    assert t is not None
    assert t.condition_id == "0xcond"
    assert t.token_id == "111"
    assert t.side == "BUY"
    assert t.tx_hash == "0xabc"


def test_parse_ignores_non_trade():
    assert _parse_trade({"event_type": "book"}) is None
    assert _parse_trade({"event_type": "price_change"}) is None


async def test_resubscribe_on_reconnect(monkeypatch):
    monkeypatch.setattr(ws_mod, "_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(ws_mod, "_APP_PING_INTERVAL_S", 3600)

    sent: list[str] = []
    connects = {"n": 0}

    class FakeWS:
        def __init__(self, idx):
            self.idx = idx

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, msg):
            sent.append(msg)

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            # Simulate an immediate disconnect on every connection.
            if self.idx >= 2:
                client._stop.set()
            raise RuntimeError("simulated drop")

    def fake_connect(url, **kw):
        connects["n"] += 1
        return FakeWS(connects["n"])

    monkeypatch.setattr(ws_mod.websockets, "connect", fake_connect)

    client = ClobWsClient(url="wss://fake/ws/market")
    await client.subscribe_trades(["TOKEN_A"])
    await client.connect()
    await asyncio.wait_for(client._reader_task, timeout=5)

    sub_frames = [s for s in sent if isinstance(s, str) and "assets_ids" in s]
    assert len(sub_frames) >= 2  # initial + at least one re-subscribe
    assert json.loads(sub_frames[0])["assets_ids"] == ["TOKEN_A"]
    assert connects["n"] >= 2
