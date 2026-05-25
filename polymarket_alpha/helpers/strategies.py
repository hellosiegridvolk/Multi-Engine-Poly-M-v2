"""Strategy categorization for stored wallets (read-only, SQLite-backed).

Reuses the v1 dossier classifiers / metrics. For each top-N leaderboard
wallet of a period, reconstructs Trades + Markets from the DB, runs
:func:`compute_trader_metrics` + :func:`build_dossier`, and aggregates per
``(sizing, conviction, direction, speed)`` combo.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from decimal import Decimal

import aiosqlite

from polymarket_alpha.analysis.dossier import DossierConfig, build_dossier
from polymarket_alpha.analysis.metrics import compute_trader_metrics
from polymarket_alpha.models import Market, StrategyDossier, Trade, Trader

log = logging.getLogger("polymarket_alpha.strategies")

ZERO = Decimal(0)
PCT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class StrategyBucket:
    tags: tuple[str, str, str, str]
    n: int
    avg_pnl: Decimal
    total_pnl: Decimal
    median_hit_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class StrategyReport:
    period: str
    snapshot_ts: int
    dossiers: list[StrategyDossier]
    buckets: list[StrategyBucket]


def _D(x) -> Decimal | None:
    if x is None or x == "":
        return None
    return Decimal(str(x))


async def _load_markets(conn: aiosqlite.Connection) -> dict[str, Market]:
    mk_rows = await (await conn.execute("SELECT * FROM markets")).fetchall()
    tok_rows = await (await conn.execute("SELECT * FROM tokens")).fetchall()
    toks_by_cid: dict[str, list] = collections.defaultdict(list)
    for t in tok_rows:
        toks_by_cid[t["condition_id"]].append(t)
    markets: dict[str, Market] = {}
    for m in mk_rows:
        cid = m["condition_id"]
        toks = sorted(toks_by_cid.get(cid, []), key=lambda r: r["outcome_index"])
        if len(toks) != 2:
            continue  # only binary markets contribute to YES/NO scoring
        outcomes = tuple("Yes" if t["outcome"] == "YES" else "No" for t in toks)
        token_ids = tuple(t["token_id"] for t in toks)
        yes_index = outcomes.index("Yes") if "Yes" in outcomes else 0
        win: int | None = None
        if m["resolved"] and m["resolution"] in ("YES", "NO"):
            win = yes_index if m["resolution"] == "YES" else (1 - yes_index)
        if win == 0:
            prices = (Decimal(1), Decimal(0))
        elif win == 1:
            prices = (Decimal(0), Decimal(1))
        else:
            prices = (Decimal("0.5"), Decimal("0.5"))
        markets[cid] = Market(
            condition_id=cid,
            question=m["question"] or "",
            slug=m["slug"] or "",
            end_date_iso=None,
            closed=bool(m["resolved"]),
            outcomes=outcomes,
            outcome_prices=prices,
            clob_token_ids=token_ids,
            is_binary=True,
            is_crypto=bool(m["is_crypto"]),
            winning_outcome_index=win,
        )
    return markets


async def _wallet_trades(
    conn: aiosqlite.Connection,
    wallet: str,
    markets: dict[str, Market],
    token_to_index: dict[str, int],
) -> list[Trade]:
    rows = await (
        await conn.execute(
            "SELECT * FROM activities WHERE wallet=? AND activity_type='TRADE' "
            "ORDER BY timestamp",
            (wallet,),
        )
    ).fetchall()
    out: list[Trade] = []
    for r in rows:
        cid = r["condition_id"]
        if not cid or cid not in markets:
            continue
        token_id = r["token_id"] or ""
        oi = token_to_index.get(token_id)
        if oi is None:
            oi = 0 if (r["outcome"] or "YES") == "YES" else 1
        out.append(
            Trade(
                proxy_wallet=wallet,
                timestamp=int(r["timestamp"]),
                condition_id=cid,
                size=_D(r["shares"]) or ZERO,
                usdc_size=_D(r["usdc"]) or ZERO,
                price=_D(r["price"]) or ZERO,
                asset=token_id,
                side=(r["side"] or "BUY"),
                outcome_index=oi,
                outcome_label=(r["outcome"] or ""),
                title="",
                slug="",
                event_slug="",
                transaction_hash=(r["tx_hash"] or ""),
            )
        )
    return out


async def analyze(
    conn: aiosqlite.Connection,
    *,
    period: str,
    top: int = 20,
    at_ts: int | None = None,
    cfg: DossierConfig | None = None,
) -> StrategyReport:
    cfg = cfg or DossierConfig()
    if at_ts is None:
        snap = await (
            await conn.execute(
                "SELECT snapshot_id, snapshot_ts FROM leaderboard_snapshots "
                "WHERE period=? ORDER BY snapshot_ts DESC LIMIT 1",
                (period,),
            )
        ).fetchone()
    else:
        snap = await (
            await conn.execute(
                "SELECT snapshot_id, snapshot_ts FROM leaderboard_snapshots "
                "WHERE period=? AND snapshot_ts<=? ORDER BY snapshot_ts DESC LIMIT 1",
                (period, at_ts),
            )
        ).fetchone()
    if snap is None:
        return StrategyReport(period=period, snapshot_ts=0, dossiers=[], buckets=[])

    entries = await (
        await conn.execute(
            "SELECT rank, wallet, pnl_usd, volume_usd FROM leaderboard_entries "
            "WHERE snapshot_id=? ORDER BY rank LIMIT ?",
            (snap["snapshot_id"], top),
        )
    ).fetchall()

    markets = await _load_markets(conn)
    token_to_index = {
        r["token_id"]: int(r["outcome_index"])
        for r in await (
            await conn.execute("SELECT token_id, outcome_index FROM tokens")
        ).fetchall()
    }

    dossiers: list[StrategyDossier] = []
    for e in entries:
        wallet = e["wallet"]
        trades = await _wallet_trades(conn, wallet, markets, token_to_index)
        if not trades:
            continue
        m = compute_trader_metrics(trades, markets, crypto_only=True)
        trader = Trader(
            rank=int(e["rank"]),
            proxy_wallet=wallet,
            user_name="",
            x_username=None,
            verified_badge=False,
            volume=_D(e["volume_usd"]) or ZERO,
            pnl=_D(e["pnl_usd"]) or ZERO,
            profile_image=None,
        )
        dossiers.append(build_dossier(trader, m, window=period, cfg=cfg))

    bucket_map: dict[tuple[str, str, str, str], list[StrategyDossier]] = (
        collections.defaultdict(list)
    )
    for d in dossiers:
        bucket_map[(d.sizing_style, d.conviction, d.direction_bias, d.speed)].append(d)

    buckets: list[StrategyBucket] = []
    for combo, items in bucket_map.items():
        pnls = [d.realized_pnl for d in items]
        hits = sorted([d.hit_rate for d in items if d.hit_rate is not None])
        total = sum(pnls, ZERO)
        avg = (total / Decimal(len(items))).quantize(PCT)
        median_hit = hits[len(hits) // 2] if hits else None
        buckets.append(
            StrategyBucket(
                tags=combo,
                n=len(items),
                avg_pnl=avg,
                total_pnl=total,
                median_hit_rate=median_hit,
            )
        )
    buckets.sort(key=lambda b: b.total_pnl, reverse=True)
    return StrategyReport(
        period=period,
        snapshot_ts=int(snap["snapshot_ts"]),
        dossiers=dossiers,
        buckets=buckets,
    )


@dataclass(frozen=True, slots=True)
class ShortlistEntry:
    rank: int
    wallet: str
    realized_pnl: Decimal
    hit_rate: Decimal | None
    leaderboard_pnl: Decimal
    strategy: tuple[str, str, str, str]
    persisted_days: int  # number of distinct day-snapshots this wallet appeared in
    suggested_weight: Decimal
    justification: str


async def shortlist(
    conn: aiosqlite.Connection,
    *,
    top: int = 2,
    period: str = "day",
    min_hit_rate: Decimal = Decimal("0.55"),
    min_realized_pnl: Decimal = Decimal("0"),
    at_ts: int | None = None,
    diversify: bool = False,
    cfg: DossierConfig | None = None,
) -> list[ShortlistEntry]:
    """Produce a shortlist of wallets to shadow-copy.

    Filters: positive realized PnL, hit_rate >= min_hit_rate, must appear in
    at least 2 distinct day-snapshots (5+ days apart) when available.
    Weight is scaled by ``persisted_days`` so multi-snapshot wallets get
    weight=1.0 and single-snapshot wallets get weight=0.7.

    ``at_ts`` restricts to the snapshot at-or-before that time (used by the
    backtest). ``diversify`` greedily spreads picks across direction biases
    (BULL/BEAR/NEUTRAL) instead of taking the top-N regardless of strategy.
    """
    report = await analyze(conn, period=period, top=50, at_ts=at_ts, cfg=cfg)
    if at_ts is None:
        snap_query = (
            "SELECT snapshot_id FROM leaderboard_snapshots "
            "WHERE period='day' ORDER BY snapshot_ts DESC"
        )
        snap_params: tuple = ()
    else:
        snap_query = (
            "SELECT snapshot_id FROM leaderboard_snapshots "
            "WHERE period='day' AND snapshot_ts <= ? ORDER BY snapshot_ts DESC"
        )
        snap_params = (at_ts,)
    snap_ids = [
        r["snapshot_id"]
        for r in await (await conn.execute(snap_query, snap_params)).fetchall()
    ]

    out: list[ShortlistEntry] = []
    for d in report.dossiers:
        if d.realized_pnl <= min_realized_pnl:
            continue
        if d.hit_rate is None or d.hit_rate < min_hit_rate:
            continue
        # Count distinct day-snapshots where this wallet appears
        persisted = 0
        for sid in snap_ids:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM leaderboard_entries WHERE snapshot_id=? "
                    "AND wallet=? LIMIT 1",
                    (sid, d.trader.proxy_wallet),
                )
            ).fetchone()
            if row:
                persisted += 1
        weight = Decimal("1.0") if persisted >= 2 else Decimal("0.7")
        justification = (
            f"realized=${d.realized_pnl} hit={d.hit_rate} "
            f"strategy={d.sizing_style}/{d.conviction}/{d.direction_bias}/{d.speed} "
            f"persisted_in_{persisted}_snapshots"
        )
        out.append(
            ShortlistEntry(
                rank=d.trader.rank,
                wallet=d.trader.proxy_wallet,
                realized_pnl=d.realized_pnl,
                hit_rate=d.hit_rate,
                leaderboard_pnl=d.trader.pnl,
                strategy=(d.sizing_style, d.conviction, d.direction_bias, d.speed),
                persisted_days=persisted,
                suggested_weight=weight,
                justification=justification,
            )
        )

    # Sort: persisted desc, realized_pnl desc, hit_rate desc
    out.sort(
        key=lambda e: (
            -e.persisted_days,
            -e.realized_pnl,
            -(e.hit_rate or Decimal(0)),
        )
    )
    if not diversify:
        return out[:top]

    # Diversify: greedily pick to spread direction_bias (BULL/BEAR/NEUTRAL),
    # taking the best wallet of each unseen direction before doubling up.
    picked: list[ShortlistEntry] = []
    seen_dirs: set[str] = set()
    for e in out:
        if len(picked) >= top:
            break
        if e.strategy[2] not in seen_dirs:
            picked.append(e)
            seen_dirs.add(e.strategy[2])
    # Fill remaining slots with the next-best regardless of direction.
    for e in out:
        if len(picked) >= top:
            break
        if e not in picked:
            picked.append(e)
    return picked[:top]


def render_copy_sources_yaml(entries: list[ShortlistEntry]) -> str:
    """Emit a copy_sources.yaml drop-in `sources:` block."""
    lines = ["# Generated by `polymarket_alpha shortlist` — re-run weekly.", "sources:"]
    for i, e in enumerate(entries, start=1):
        direction = e.strategy[2].lower()
        lines.extend(
            [
                f"  - id: shadow_{i}_{direction}",
                f'    address: "{e.wallet}"',
                f"    weight: {e.suggested_weight}",
                "    latency_budget_seconds: 8",
                f"    per_trade_size_cap_usd: {3.0 if e.suggested_weight == Decimal('1.0') else 2.0}",
                f"    daily_size_cap_usd: {15.0 if e.suggested_weight == Decimal('1.0') else 10.0}",
                "    classes_eligible:",
                "      - intraday_15m",
                "      - event_daily",
                "      - event_weekly",
                f"    # {e.justification}",
                "",
            ]
        )
    return "\n".join(lines)

def render_human(report: StrategyReport) -> str:
    lines: list[str] = []
    lines.append(
        f"# strategies — period={report.period} "
        f"snapshot_ts={report.snapshot_ts} wallets={len(report.dossiers)}"
    )
    lines.append("")
    lines.append("STRATEGY BUCKETS (sorted by total realized PnL on resolved crypto markets):")
    lines.append(f"  {'n':>3} {'avg_pnl':>10} {'med_hit':>8} {'tot_pnl':>11}  STRATEGY")
    for b in report.buckets:
        hit = f"{b.median_hit_rate}" if b.median_hit_rate is not None else "-"
        lines.append(
            f"  {b.n:>3} {float(b.avg_pnl):>10.2f} {hit:>8} "
            f"{float(b.total_pnl):>11.2f}  "
            f"{b.tags[0]}/{b.tags[1]}/{b.tags[2]}/{b.tags[3]}"
        )
    lines.append("")
    lines.append("TOP WALLETS:")
    lines.append(
        f"  {'rank':>4} {'wallet':<44} {'lb_pnl':>10} {'realized':>10} "
        f"{'hit':>6}  STRATEGY"
    )
    for d in report.dossiers:
        hit = f"{d.hit_rate}" if d.hit_rate is not None else "-"
        lines.append(
            f"  {d.trader.rank:>4} {d.trader.proxy_wallet}  "
            f"{float(d.trader.pnl):>9.0f}  {float(d.realized_pnl):>9.2f}  "
            f"{hit:>6}  "
            f"{d.sizing_style}/{d.conviction}/{d.direction_bias}/{d.speed}"
        )
    return "\n".join(lines)


def report_to_dict(report: StrategyReport) -> dict:
    return {
        "period": report.period,
        "snapshot_ts": report.snapshot_ts,
        "buckets": [
            {
                "sizing_style": b.tags[0],
                "conviction": b.tags[1],
                "direction_bias": b.tags[2],
                "speed": b.tags[3],
                "n": b.n,
                "avg_pnl": str(b.avg_pnl),
                "total_pnl": str(b.total_pnl),
                "median_hit_rate": (
                    str(b.median_hit_rate) if b.median_hit_rate is not None else None
                ),
            }
            for b in report.buckets
        ],
        "wallets": [
            {
                "rank": d.trader.rank,
                "wallet": d.trader.proxy_wallet,
                "leaderboard_pnl": str(d.trader.pnl),
                "realized_pnl": str(d.realized_pnl),
                "hit_rate": str(d.hit_rate) if d.hit_rate is not None else None,
                "n_trades": d.n_trades,
                "n_crypto_markets": d.n_crypto_markets,
                "n_resolved_markets": d.n_resolved_markets,
                "median_holding_period_seconds": d.median_holding_period_seconds,
                "sizing_style": d.sizing_style,
                "conviction": d.conviction,
                "direction_bias": d.direction_bias,
                "speed": d.speed,
            }
            for d in report.dossiers
        ],
    }
