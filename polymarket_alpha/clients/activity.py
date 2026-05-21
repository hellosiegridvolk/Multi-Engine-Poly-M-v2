"""Per-wallet trade history client.

Verified endpoint (see API_NOTES.md): param is `type` (not `activity_types`).
Real: GET https://data-api.polymarket.com/activity?user=&type=TRADE&limit=&offset=
"""

from __future__ import annotations

import logging

import httpx

from polymarket_alpha.http import PolymarketAPIError, request_json
from polymarket_alpha.models import Trade, to_decimal

log = logging.getLogger("polymarket_alpha.activity")

DATA_API = "https://data-api.polymarket.com"
ACTIVITY_PATH = "/activity"
# Verified: data-api /activity rejects offset > 3000 with HTTP 400 (hard ceiling).
MAX_OFFSET = 3000


class ActivityClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch_trades(
        self,
        wallet: str,
        *,
        max_trades: int | None = None,
        page_size: int = 100,
    ) -> list[Trade]:
        wallet = wallet.lower()
        collected: list[Trade] = []
        offset = 0
        seen: set[tuple[str, str, str, str]] = set()

        while True:
            if offset > MAX_OFFSET:
                log.warning(
                    "stopping at offset %d (API ceiling %d); %d trades collected",
                    offset,
                    MAX_OFFSET,
                    len(collected),
                )
                break
            params: list[tuple[str, str]] = [
                ("user", wallet),
                ("type", "TRADE"),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            try:
                payload = request_json(
                    self._client, "GET", f"{DATA_API}{ACTIVITY_PATH}", params=params
                )
            except PolymarketAPIError as exc:
                if exc.status_code == 400 and collected:
                    log.warning(
                        "activity pagination stopped at offset %d (HTTP 400); "
                        "%d trades collected",
                        offset,
                        len(collected),
                    )
                    break
                raise
            if not isinstance(payload, list):
                from polymarket_alpha.http import PolymarketAPIError

                raise PolymarketAPIError(
                    f"activity returned {type(payload).__name__}, expected list",
                    url=f"{DATA_API}{ACTIVITY_PATH}",
                )
            if not payload:
                break

            for rec in payload:
                if rec.get("type") != "TRADE":
                    continue
                trade = self._parse_trade(rec)
                key = (
                    trade.transaction_hash,
                    trade.asset,
                    trade.side,
                    str(trade.size),
                )
                if key in seen:
                    continue
                seen.add(key)
                collected.append(trade)
                if max_trades is not None and len(collected) >= max_trades:
                    return collected[:max_trades]

            if len(payload) < page_size:
                break
            offset += page_size

        return collected

    def fetch_activity(
        self,
        wallet: str,
        *,
        max_items: int | None = None,
        since_ts: int = 0,
        page_size: int = 100,
    ) -> list[dict]:
        """Fetch ALL activity types (no `type` filter) as raw records.

        Same verified pagination + offset-3000 ceiling + HTTP-400 stop as
        :meth:`fetch_trades`. Stops early once records older than `since_ts`
        are reached (results are newest-first).
        """
        wallet = wallet.lower()
        collected: list[dict] = []
        offset = 0
        while True:
            if offset > MAX_OFFSET:
                log.warning(
                    "activity stop at offset %d (ceiling %d); %d records",
                    offset,
                    MAX_OFFSET,
                    len(collected),
                )
                break
            params: list[tuple[str, str]] = [
                ("user", wallet),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            try:
                payload = request_json(
                    self._client, "GET", f"{DATA_API}{ACTIVITY_PATH}", params=params
                )
            except PolymarketAPIError as exc:
                if exc.status_code == 400 and collected:
                    log.warning(
                        "activity pagination stopped at offset %d (HTTP 400)",
                        offset,
                    )
                    break
                raise
            if not isinstance(payload, list):
                raise PolymarketAPIError(
                    f"activity returned {type(payload).__name__}, expected list",
                    url=f"{DATA_API}{ACTIVITY_PATH}",
                )
            if not payload:
                break
            stop = False
            for rec in payload:
                if int(rec.get("timestamp", 0)) < since_ts:
                    stop = True
                    break
                collected.append(rec)
                if max_items is not None and len(collected) >= max_items:
                    return collected[:max_items]
            if stop or len(payload) < page_size:
                break
            offset += page_size
        return collected

    @staticmethod
    def _parse_trade(record: dict) -> Trade:
        return Trade(
            proxy_wallet=str(record["proxyWallet"]).lower(),
            timestamp=int(record["timestamp"]),
            condition_id=str(record["conditionId"]),
            size=to_decimal(record.get("size", 0)),
            usdc_size=to_decimal(record.get("usdcSize", 0)),
            price=to_decimal(record.get("price", 0)),
            asset=str(record.get("asset", "")),
            side=str(record["side"]).upper(),
            outcome_index=int(record.get("outcomeIndex", 0)),
            outcome_label=str(record.get("outcome", "")),
            title=record.get("title") or "",
            slug=record.get("slug") or "",
            event_slug=record.get("eventSlug") or "",
            transaction_hash=str(record.get("transactionHash", "")),
        )
