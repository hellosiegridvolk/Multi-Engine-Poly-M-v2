"""Async CLOB market-channel WebSocket client.

Verified protocol (API_NOTES.md §A): subscribe with
``{"assets_ids": [...], "type": "market"}``; trade prints arrive as
``last_trade_price`` events (no wallet/log_index — market tape only).
Reconnect with capped exponential backoff and re-subscribe.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import websockets

from polymarket_alpha.config import CLOB_WS_URL, WS_QUEUE_MAXSIZE

log = logging.getLogger("polymarket_alpha.clob_ws")

_BACKOFF_START_S = 1.0
_BACKOFF_CAP_S = 60.0
_APP_PING_INTERVAL_S = 10.0


@dataclass(frozen=True, slots=True)
class TradeEvent:
    condition_id: str
    token_id: str
    price: str
    size: str
    side: str
    timestamp_ms: int
    tx_hash: str | None
    raw: dict


def _parse_trade(ev: dict) -> TradeEvent | None:
    if ev.get("event_type") != "last_trade_price":
        return None
    try:
        return TradeEvent(
            condition_id=str(ev.get("market", "")),
            token_id=str(ev.get("asset_id", "")),
            price=str(ev.get("price", "")),
            size=str(ev.get("size", "")),
            side=str(ev.get("side", "")).upper(),
            timestamp_ms=int(ev.get("timestamp", 0)),
            tx_hash=ev.get("transaction_hash"),
            raw=ev,
        )
    except (TypeError, ValueError):
        log.warning("malformed last_trade_price event: %s", ev)
        return None


class ClobWsClient:
    def __init__(
        self,
        *,
        url: str = CLOB_WS_URL,
        queue_maxsize: int = WS_QUEUE_MAXSIZE,
    ) -> None:
        self._url = url
        self._tokens: set[str] = set()
        self._queue: asyncio.Queue[TradeEvent] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()

    # -- subscription churn (no socket teardown) --
    async def subscribe_trades(self, token_ids: list[str]) -> None:
        new = {t for t in token_ids if t} - self._tokens
        self._tokens |= {t for t in token_ids if t}
        if new and self._ws is not None:
            await self._send_subscription(sorted(new))

    async def unsubscribe(self, token_ids: list[str]) -> None:
        self._tokens -= set(token_ids)

    async def _send_subscription(self, tokens: list[str]) -> None:
        assert self._ws is not None
        await self._ws.send(
            json.dumps({"assets_ids": tokens, "type": "market"})
        )
        log.info("subscribed %d token ids", len(tokens))

    async def connect(self) -> None:
        if self._reader_task is None:
            self._stop.clear()
            self._reader_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        backoff = _BACKOFF_START_S
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = _BACKOFF_START_S
                    if self._tokens:
                        await self._send_subscription(sorted(self._tokens))
                    ping = asyncio.create_task(self._app_ping(ws))
                    try:
                        await self._read_loop(ws)
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self._connected.clear()
                self._ws = None
                if self._stop.is_set():
                    break
                log.warning(
                    "WS disconnected (%s); reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, _BACKOFF_CAP_S)
            else:
                self._connected.clear()
                self._ws = None
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)

    async def _app_ping(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(_APP_PING_INTERVAL_S)
                await ws.send("PING")
        except (asyncio.CancelledError, Exception):
            return

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            text = raw if isinstance(raw, str) else raw.decode()
            if text.strip() in ("PONG", "PING"):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                log.debug("non-JSON WS frame: %s", text[:120])
                continue
            events = payload if isinstance(payload, list) else [payload]
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                trade = _parse_trade(ev)
                if trade is not None:
                    await self._enqueue(trade)

    async def _enqueue(self, trade: TradeEvent) -> None:
        # Backpressure: never silently drop. Warn at >80% full, then block.
        if self._queue.maxsize:
            fill = self._queue.qsize() / self._queue.maxsize
            if fill > 0.8:
                log.warning(
                    "WS queue %.0f%% full (%d/%d) — consumer is slow",
                    fill * 100,
                    self._queue.qsize(),
                    self._queue.maxsize,
                )
        await self._queue.put(trade)

    async def stream_trades(self) -> AsyncIterator[TradeEvent]:
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
