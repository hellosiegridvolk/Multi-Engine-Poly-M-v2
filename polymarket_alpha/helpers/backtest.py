"""Shortlist backtest: would a past shortlist have beaten the field?

Picks a shortlist as of a past snapshot, then measures the *forward* realized
PnL of those wallets (trades after the pick time) against the forward PnL of
the whole analyzed field. If picks beat the field, the shortlist has
predictive value.

Read-only, SQLite-backed. Honest about its limits: forward PnL only counts
crypto markets that have since resolved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import aiosqlite

from polymarket_alpha.analysis.dossier import DossierConfig
from polymarket_alpha.analysis.metrics import compute_trader_metrics
from polymarket_alpha.helpers.strategies import (
    _load_markets,
    _wallet_trades,
    analyze,
    shortlist,
)

log = logging.getLogger("polymarket_alpha.backtest")
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class BacktestPick:
    wallet: str
    as_of_realized_pnl: Decimal
    forward_realized_pnl: Decimal
    forward_hit_rate: Decimal | None
    forward_resolved_markets: int


@dataclass(frozen=True, slots=True)
class BacktestReport:
    as_of_ts: int
    n_day_snapshots_available: int
    picks: list[BacktestPick]
    field_size: int
    picks_avg_forward_pnl: Decimal
    field_avg_forward_pnl: Decimal
    edge: Decimal
    verdict: str


async def _forward_pnl(
    conn: aiosqlite.Connection,
    wallet: str,
    markets: dict,
    token_to_index: dict[str, int],
    *,
    after_ts: int,
) -> tuple[Decimal, Decimal | None, int]:
    """Realized PnL / hit-rate / resolved-count for a wallet's trades placed
    strictly after ``after_ts``."""
    all_trades = await _wallet_trades(conn, wallet, markets, token_to_index)
    fwd = [t for t in all_trades if t.timestamp > after_ts]
    if not fwd:
        return ZERO, None, 0
    m = compute_trader_metrics(fwd, markets, crypto_only=True)
    return m.realized_pnl, m.hit_rate, m.n_resolved_markets


async def backtest(
    conn: aiosqlite.Connection,
    *,
    top: int = 2,
    as_of_ts: int | None = None,
    cfg: DossierConfig | None = None,
) -> BacktestReport:
    """Run a shortlist backtest. Never raises; returns a structured report."""
    cfg = cfg or DossierConfig()
    day_snaps = [
        int(r["snapshot_ts"])
        for r in await (
            await conn.execute(
                "SELECT snapshot_ts FROM leaderboard_snapshots "
                "WHERE period='day' ORDER BY snapshot_ts ASC"
            )
        ).fetchall()
    ]
    # Default pick-date = oldest snapshot, so there's maximum forward window.
    if as_of_ts is None:
        as_of_ts = day_snaps[0] if day_snaps else 0

    picks_entries = await shortlist(conn, top=top, at_ts=as_of_ts, cfg=cfg)
    field_report = await analyze(conn, period="day", top=50, at_ts=as_of_ts, cfg=cfg)

    markets = await _load_markets(conn)
    token_to_index = {
        r["token_id"]: int(r["outcome_index"])
        for r in await (
            await conn.execute("SELECT token_id, outcome_index FROM tokens")
        ).fetchall()
    }

    picks: list[BacktestPick] = []
    for e in picks_entries:
        fpnl, fhit, fres = await _forward_pnl(
            conn, e.wallet, markets, token_to_index, after_ts=as_of_ts
        )
        picks.append(
            BacktestPick(
                wallet=e.wallet,
                as_of_realized_pnl=e.realized_pnl,
                forward_realized_pnl=fpnl,
                forward_hit_rate=fhit,
                forward_resolved_markets=fres,
            )
        )

    field_fwd: list[Decimal] = []
    for d in field_report.dossiers:
        fpnl, _, _ = await _forward_pnl(
            conn, d.trader.proxy_wallet, markets, token_to_index, after_ts=as_of_ts
        )
        field_fwd.append(fpnl)

    def _avg(xs: list[Decimal]) -> Decimal:
        if not xs:
            return ZERO
        return (sum(xs, ZERO) / Decimal(len(xs))).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )

    picks_avg = _avg([p.forward_realized_pnl for p in picks])
    field_avg = _avg(field_fwd)
    edge = picks_avg - field_avg

    if not picks or not field_fwd:
        verdict = "INSUFFICIENT DATA"
    elif edge > ZERO:
        verdict = "PICKS BEAT FIELD"
    elif edge < ZERO:
        verdict = "PICKS LAGGED FIELD"
    else:
        verdict = "PICKS MATCHED FIELD"

    return BacktestReport(
        as_of_ts=as_of_ts,
        n_day_snapshots_available=len(day_snaps),
        picks=picks,
        field_size=len(field_fwd),
        picks_avg_forward_pnl=picks_avg,
        field_avg_forward_pnl=field_avg,
        edge=edge,
        verdict=verdict,
    )


def render_backtest_human(r: BacktestReport) -> str:
    lines = [
        f"# backtest — as_of_ts={r.as_of_ts} "
        f"({r.n_day_snapshots_available} day-snapshots available)",
        "",
        f"  VERDICT: {r.verdict}",
        f"  picks avg forward PnL : {r.picks_avg_forward_pnl}",
        f"  field avg forward PnL : {r.field_avg_forward_pnl}  (n={r.field_size})",
        f"  edge                  : {r.edge}",
        "",
        "  PICKS:",
        f"  {'wallet':<44} {'as-of PnL':>11} {'fwd PnL':>11} {'fwd hit':>8} {'resolved':>9}",
    ]
    for p in r.picks:
        hit = f"{p.forward_hit_rate}" if p.forward_hit_rate is not None else "-"
        lines.append(
            f"  {p.wallet:<44} {p.as_of_realized_pnl:>11} "
            f"{p.forward_realized_pnl:>11} {hit:>8} {p.forward_resolved_markets:>9}"
        )
    if r.n_day_snapshots_available < 2:
        lines.append("")
        lines.append(
            "  NOTE: only one day-snapshot — forward window is thin; "
            "re-run after several days of `worker leaderboard` history."
        )
    return "\n".join(lines)


def backtest_to_dict(r: BacktestReport) -> dict:
    return {
        "as_of_ts": r.as_of_ts,
        "n_day_snapshots_available": r.n_day_snapshots_available,
        "verdict": r.verdict,
        "picks_avg_forward_pnl": str(r.picks_avg_forward_pnl),
        "field_avg_forward_pnl": str(r.field_avg_forward_pnl),
        "field_size": r.field_size,
        "edge": str(r.edge),
        "picks": [
            {
                "wallet": p.wallet,
                "as_of_realized_pnl": str(p.as_of_realized_pnl),
                "forward_realized_pnl": str(p.forward_realized_pnl),
                "forward_hit_rate": (
                    str(p.forward_hit_rate) if p.forward_hit_rate is not None else None
                ),
                "forward_resolved_markets": p.forward_resolved_markets,
            }
            for p in r.picks
        ],
    }
