"""Repositories: TEXT <-> Decimal at the boundary; never expose float."""

from __future__ import annotations

import hashlib
from decimal import Decimal


def to_text(value: Decimal | int | str | None) -> str | None:
    """Decimal -> stable TEXT for storage. None passes through."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def to_decimal(value: str | None) -> Decimal | None:
    """Stored TEXT -> Decimal. None passes through."""
    if value is None or value == "":
        return None
    return Decimal(value)


def dedup_key(
    *,
    tx_hash: str | None,
    activity_type: str,
    condition_id: str | None,
    token_id: str | None,
    side: str | None,
    shares: str | None,
    timestamp: int,
) -> str:
    """Deterministic natural key (Polymarket exposes no log_index).

    A genuinely unique fill is identified by its on-chain tx plus the
    normalized economic tuple. Stable across re-polls and across REST/WS.
    """
    parts = [
        tx_hash or "",
        activity_type or "",
        condition_id or "",
        token_id or "",
        side or "",
        shares or "",
        str(timestamp),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
