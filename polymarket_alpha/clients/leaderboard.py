"""Top-traders leaderboard client.

Verified endpoint (see API_NOTES.md): the spec's path was wrong.
Real: GET https://data-api.polymarket.com/v1/leaderboard?window=&limit=&category=
"""

from __future__ import annotations

import logging

import httpx

from polymarket_alpha.http import request_json
from polymarket_alpha.models import Trader, to_decimal

log = logging.getLogger("polymarket_alpha.leaderboard")

DATA_API = "https://data-api.polymarket.com"
LEADERBOARD_PATH = "/v1/leaderboard"
WINDOWS = {"day", "week", "month", "all"}


class LeaderboardClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(
        self,
        *,
        window: str,
        limit: int = 25,
        category: str | None = "crypto",
    ) -> list[Trader]:
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {sorted(WINDOWS)}, got {window!r}")

        params: list[tuple[str, str]] = [
            ("window", window),
            ("limit", str(limit)),
        ]
        if category:
            params.append(("category", category))

        log.info("fetching leaderboard window=%s limit=%d category=%s", window, limit, category)
        payload = request_json(
            self._client, "GET", f"{DATA_API}{LEADERBOARD_PATH}", params=params
        )
        if not isinstance(payload, list):
            from polymarket_alpha.http import PolymarketAPIError

            raise PolymarketAPIError(
                f"leaderboard returned {type(payload).__name__}, expected list",
                url=f"{DATA_API}{LEADERBOARD_PATH}",
            )
        return [self._parse_trader(rec) for rec in payload]

    @staticmethod
    def _parse_trader(record: dict) -> Trader:
        return Trader(
            rank=int(record["rank"]),
            proxy_wallet=str(record["proxyWallet"]).lower(),
            user_name=record.get("userName") or "",
            x_username=(record.get("xUsername") or None),
            verified_badge=bool(record.get("verifiedBadge", False)),
            volume=to_decimal(record.get("vol", 0)),
            pnl=to_decimal(record.get("pnl", 0)),
            profile_image=(record.get("profileImage") or None),
        )
