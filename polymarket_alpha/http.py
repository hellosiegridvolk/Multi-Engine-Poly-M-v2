"""Shared httpx client with retry/backoff. No mock fallbacks: on failure, raise."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("polymarket_alpha.http")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_USER_AGENT = "polymarket-alpha/0.1"
_RETRY_AFTER_CAP_S = 60.0
_BACKOFF_BASE_S = 0.5


class PolymarketAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


def _default_headers(extra: dict[str, str] | None) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def build_client(
    *,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers=_default_headers(headers))


def build_async_client(
    *,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers=_default_headers(headers))


def _parse_retry_after(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        header = resp.headers.get("Retry-After")
        if header:
            parsed = _parse_retry_after(header)
            if parsed is not None:
                return min(parsed, _RETRY_AFTER_CAP_S)
    return _BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, 0.25)


def _should_retry(status_code: int, attempt: int, max_retries: int) -> bool:
    return status_code in RETRYABLE_STATUS and attempt < max_retries


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict | list[tuple[str, str]] | None = None,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Perform a request and return parsed JSON, retrying transient failures."""
    for attempt in range(max_retries + 1):
        try:
            resp = client.request(method, url, params=params)
        except httpx.TransportError as exc:
            if attempt < max_retries:
                delay = _retry_delay(None, attempt)
                log.warning(
                    "transport error %s on %s (attempt %d/%d); retrying in %.2fs",
                    exc,
                    url,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            log.error("transport error on %s after %d attempts: %s", url, attempt + 1, exc)
            raise PolymarketAPIError(f"transport error: {exc}", url=url) from exc

        if _should_retry(resp.status_code, attempt, max_retries):
            delay = _retry_delay(resp, attempt)
            log.warning(
                "HTTP %d on %s (attempt %d/%d); retrying in %.2fs",
                resp.status_code,
                url,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            continue

        if resp.is_error:
            log.error("HTTP %d on %s", resp.status_code, resp.url)
            raise PolymarketAPIError(
                f"HTTP {resp.status_code} for {resp.url}",
                status_code=resp.status_code,
                url=str(resp.url),
            )

        try:
            return resp.json()
        except ValueError as exc:
            log.error("non-JSON body from %s", resp.url)
            raise PolymarketAPIError(
                f"non-JSON response body from {resp.url}", url=str(resp.url)
            ) from exc

    # Unreachable: loop either returns or raises.
    raise PolymarketAPIError(f"request to {url} exhausted retries", url=url)


async def request_json_async(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict | list[tuple[str, str]] | None = None,
    max_retries: int = MAX_RETRIES,
) -> Any:
    """Async mirror of :func:`request_json` (kept for future async usage)."""
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, params=params)
        except httpx.TransportError as exc:
            if attempt < max_retries:
                delay = _retry_delay(None, attempt)
                log.warning(
                    "transport error %s on %s (attempt %d/%d); retrying in %.2fs",
                    exc,
                    url,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise PolymarketAPIError(f"transport error: {exc}", url=url) from exc

        if _should_retry(resp.status_code, attempt, max_retries):
            delay = _retry_delay(resp, attempt)
            log.warning(
                "HTTP %d on %s (attempt %d/%d); retrying in %.2fs",
                resp.status_code,
                url,
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if resp.is_error:
            raise PolymarketAPIError(
                f"HTTP {resp.status_code} for {resp.url}",
                status_code=resp.status_code,
                url=str(resp.url),
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise PolymarketAPIError(
                f"non-JSON response body from {resp.url}", url=str(resp.url)
            ) from exc

    raise PolymarketAPIError(f"request to {url} exhausted retries", url=url)
