"""Wallet staleness/activity view. Log-only, no gating, all types count.

Reads exclusively from SQLite — workers keep the DB fresh, never the live API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

import aiosqlite

Tier = Literal["live", "active", "slow", "idle"]

# Tier thresholds on last-activity lag (seconds).
_LIVE_S = 5 * 60
_ACTIVE_S = 60 * 60
_SLOW_S = 24 * 60 * 60

_RECENT_LIMIT = 20  # activities used to compute recent gaps


@dataclass(frozen=True, slots=True)
class WalletActivity:
    wallet: str
    last_activity_lag_s: int
    last_action_type: str
    last_trade_lag_s: int | None
    tier: Tier
    recent_gaps_s: list[int]
    per_type_last_ts: dict[str, int]


def _tier(lag_s: int) -> Tier:
    if lag_s < _LIVE_S:
        return "live"
    if lag_s < _ACTIVE_S:
        return "active"
    if lag_s < _SLOW_S:
        return "slow"
    return "idle"


def _fmt_lag(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class WalletActivityTracker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        now_fn: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._db = db
        self._now = now_fn

    async def get(self, wallet: str) -> WalletActivity:
        wallet = wallet.lower()
        now = self._now()
        cur = await self._db.execute(
            "SELECT activity_type, timestamp FROM activities "
            "WHERE wallet = ? ORDER BY timestamp DESC LIMIT ?",
            (wallet, _RECENT_LIMIT),
        )
        rows = await cur.fetchall()
        if not rows:
            return WalletActivity(
                wallet=wallet,
                last_activity_lag_s=-1,
                last_action_type="-",
                last_trade_lag_s=None,
                tier="idle",
                recent_gaps_s=[],
                per_type_last_ts={},
            )

        last_ts = int(rows[0]["timestamp"])
        last_type = str(rows[0]["activity_type"])
        last_lag = max(now - last_ts, 0)

        per_type: dict[str, int] = {}
        for r in rows:
            t = str(r["activity_type"])
            ts = int(r["timestamp"])
            if t not in per_type or ts > per_type[t]:
                per_type[t] = ts

        cur = await self._db.execute(
            "SELECT MAX(timestamp) FROM activities "
            "WHERE wallet = ? AND activity_type = 'TRADE'",
            (wallet,),
        )
        trow = await cur.fetchone()
        last_trade_lag = (
            max(now - int(trow[0]), 0) if trow and trow[0] is not None else None
        )

        times = [int(r["timestamp"]) for r in rows]  # desc
        gaps = [times[i] - times[i + 1] for i in range(len(times) - 1)]

        return WalletActivity(
            wallet=wallet,
            last_activity_lag_s=last_lag,
            last_action_type=last_type,
            last_trade_lag_s=last_trade_lag,
            tier=_tier(last_lag),
            recent_gaps_s=gaps,
            per_type_last_ts=per_type,
        )

    async def get_many(
        self, wallets: list[str]
    ) -> dict[str, WalletActivity]:
        return {w.lower(): await self.get(w) for w in wallets}

    async def render_table(self, wallets: list[str]) -> str:
        """Fixed-width staleness table (stable, byte-for-byte testable)."""
        header = (
            f"{'WALLET':<42}  {'TIER':<6}  {'LAST_ACT':<8}  "
            f"{'ACT_LAG':>7}  {'TRADE_LAG':>9}"
        )
        lines = [header, "-" * len(header)]
        data = await self.get_many(wallets)
        for w in wallets:
            wa = data[w.lower()]
            act_lag = (
                _fmt_lag(wa.last_activity_lag_s)
                if wa.last_activity_lag_s >= 0
                else "-"
            )
            lines.append(
                f"{wa.wallet:<42}  {wa.tier:<6}  {wa.last_action_type:<8}  "
                f"{act_lag:>7}  {_fmt_lag(wa.last_trade_lag_s):>9}"
            )
        return "\n".join(lines)
