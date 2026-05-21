"""Overridable runtime configuration constants."""

from __future__ import annotations

import os

# Public CLOB market WebSocket (verified — see API_NOTES.md §A).
CLOB_WS_URL = os.environ.get(
    "POLYMARKET_ALPHA_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)

# Default worker loop interval (seconds).
DEFAULT_WORKER_INTERVAL_S = int(
    os.environ.get("POLYMARKET_ALPHA_INTERVAL", "300")
)

# WS consumer queue bound (messages). Backpressure, never silent drop.
WS_QUEUE_MAXSIZE = int(os.environ.get("POLYMARKET_ALPHA_WS_QUEUE", "10000"))
