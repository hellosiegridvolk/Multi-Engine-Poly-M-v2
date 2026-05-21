"""Market metadata resolver.

Verified gotchas (see API_NOTES.md):
- `clobTokenIds`, `outcomes`, `outcomePrices` are JSON-encoded *strings* inside
  the JSON response and need a second `json.loads`.
- The spec's `tag=crypto`/`tag_slug=crypto` filters are ignored. The crypto tag
  id is 21 (resolved via /tags/slug/crypto). Per-market crypto classification
  uses `?include_tag=true`, which returns each market's `tags` array — far
  faster and more accurate than scanning the whole tag_id=21 universe.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Sequence

import httpx

from polymarket_alpha.http import request_json
from polymarket_alpha.models import Market, to_decimal

log = logging.getLogger("polymarket_alpha.gamma")

GAMMA_API = "https://gamma-api.polymarket.com"
MARKETS_PATH = "/markets"
TAG_BY_SLUG_PATH = "/tags/slug"
CRYPTO_TAG_SLUG = "crypto"
CRYPTO_TAG_ID_FALLBACK = "21"
ONE = Decimal(1)


def _decode_json_list(raw: object) -> list:
    """Polymarket double-encodes some array fields as JSON strings."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    return []


def _is_crypto_tagged(record: dict, crypto_tag_id: str | None) -> bool:
    tags = record.get("tags")
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        slug = str(tag.get("slug", "")).strip().lower()
        if slug == CRYPTO_TAG_SLUG:
            return True
        if crypto_tag_id is not None and str(tag.get("id", "")) == str(crypto_tag_id):
            return True
    return False


def _parse_market(record: dict, *, crypto_tag_id: str | None = None) -> Market | None:
    try:
        outcomes = tuple(str(o) for o in _decode_json_list(record.get("outcomes")))
        token_ids = tuple(str(t) for t in _decode_json_list(record.get("clobTokenIds")))
        prices = tuple(
            to_decimal(p) for p in _decode_json_list(record.get("outcomePrices"))
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("skipping market %s: bad encoded field (%s)", record.get("conditionId"), exc)
        return None

    # Crypto markets are predominantly "X Up or Down" (outcomes ['Up','Down']),
    # not literal ['Yes','No']. Any 2-outcome market is treated as binary, with
    # outcome index 0 as the YES-equivalent (see API_NOTES.md).
    is_binary = len(outcomes) == 2

    closed = bool(record.get("closed", False))
    winning_index: int | None = None
    if closed and prices:
        ones = [i for i, p in enumerate(prices) if p == ONE]
        if len(ones) == 1:
            winning_index = ones[0]

    return Market(
        condition_id=str(record.get("conditionId", "")),
        question=record.get("question") or "",
        slug=record.get("slug") or "",
        end_date_iso=record.get("endDate") or record.get("endDateIso"),
        closed=closed,
        outcomes=outcomes,
        outcome_prices=prices,
        clob_token_ids=token_ids,
        is_binary=is_binary,
        is_crypto=_is_crypto_tagged(record, crypto_tag_id),
        winning_outcome_index=winning_index,
    )


class GammaClient:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def resolve_crypto_tag_id(self) -> str:
        try:
            payload = request_json(
                self._client,
                "GET",
                f"{GAMMA_API}{TAG_BY_SLUG_PATH}/{CRYPTO_TAG_SLUG}",
            )
            tag_id = str(payload["id"])
            log.info("resolved crypto tag id = %s", tag_id)
            return tag_id
        except Exception as exc:  # noqa: BLE001 - documented fallback
            log.warning(
                "could not resolve crypto tag id (%s); falling back to %s",
                exc,
                CRYPTO_TAG_ID_FALLBACK,
            )
            return CRYPTO_TAG_ID_FALLBACK

    def fetch_markets_by_token_ids(
        self,
        token_ids: Sequence[str],
        *,
        crypto_tag_id: str | None = None,
        batch_size: int = 50,
    ) -> dict[str, Market]:
        """Resolve markets for clob token ids.

        Gamma `/markets?clob_token_ids=` returns ONLY open markets by default;
        resolved markets require `closed=true`. There is no "both" — so each
        chunk is queried twice (open + closed) and merged. Default page limit
        is 20 (max 100), so an explicit limit is sent and ``batch_size`` is
        kept well under 100 markets per response.
        """
        result: dict[str, Market] = {}
        for rec in self._iter_raw_markets(token_ids, batch_size=batch_size):
            market = _parse_market(rec, crypto_tag_id=crypto_tag_id)
            if market and market.condition_id:
                result[market.condition_id] = market
        return result

    def _iter_raw_markets(
        self,
        values: Sequence[str],
        *,
        param: str = "clob_token_ids",
        batch_size: int = 50,
    ):
        """Yield raw market dicts (open + closed merged, include_tag).

        `param` is the Gamma filter key: ``clob_token_ids`` (token ids) or
        ``condition_ids`` (condition ids — recovers delisted/closed markets
        that the token filter misses).
        """
        unique = [t for t in dict.fromkeys(values) if t]
        for start in range(0, len(unique), batch_size):
            chunk = unique[start : start + batch_size]
            base = [(param, t) for t in chunk]
            base += [("include_tag", "true"), ("limit", "100")]
            for closed in (None, "true"):
                params = list(base)
                if closed is not None:
                    params.append(("closed", closed))
                payload = request_json(
                    self._client, "GET", f"{GAMMA_API}{MARKETS_PATH}", params=params
                )
                if not isinstance(payload, list):
                    continue
                yield from payload

    @staticmethod
    def _with_tags(rec: dict, crypto_tag_id: str | None):
        market = _parse_market(rec, crypto_tag_id=crypto_tag_id)
        if not (market and market.condition_id):
            return None
        tags = rec.get("tags")
        slugs = (
            [
                str(t.get("slug"))
                for t in tags
                if isinstance(t, dict) and t.get("slug")
            ]
            if isinstance(tags, list)
            else []
        )
        return market, slugs

    def fetch_markets_with_tags_by_condition(
        self,
        condition_ids: Sequence[str],
        *,
        crypto_tag_id: str | None = None,
        batch_size: int = 25,
    ) -> dict[str, tuple[Market, list[str]]]:
        """Resolve by condition id (recovers historical/closed markets the
        clob_token_ids filter cannot)."""
        result: dict[str, tuple[Market, list[str]]] = {}
        for rec in self._iter_raw_markets(
            condition_ids, param="condition_ids", batch_size=batch_size
        ):
            mt = self._with_tags(rec, crypto_tag_id)
            if mt:
                result[mt[0].condition_id] = mt
        return result

    def fetch_markets_with_tags(
        self,
        token_ids: Sequence[str],
        *,
        crypto_tag_id: str | None = None,
        batch_size: int = 50,
    ) -> dict[str, tuple[Market, list[str]]]:
        """Like :meth:`fetch_markets_by_token_ids` but also returns each
        market's tag slug list (single fetch, no extra HTTP)."""
        result: dict[str, tuple[Market, list[str]]] = {}
        for rec in self._iter_raw_markets(token_ids, batch_size=batch_size):
            mt = self._with_tags(rec, crypto_tag_id)
            if mt:
                result[mt[0].condition_id] = mt
        return result
