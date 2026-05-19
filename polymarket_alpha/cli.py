"""Argparse entry point and JSON / human serialization."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from decimal import Decimal
from typing import Any

from polymarket_alpha.analysis.dossier import build_dossier
from polymarket_alpha.analysis.metrics import compute_trader_metrics
from polymarket_alpha.clients.activity import ActivityClient
from polymarket_alpha.clients.gamma import GammaClient
from polymarket_alpha.clients.leaderboard import LeaderboardClient
from polymarket_alpha.http import PolymarketAPIError, build_client
from polymarket_alpha.models import StrategyDossier, Trader

log = logging.getLogger("polymarket_alpha.cli")


def _add_audit_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--period",
        choices=["day", "week", "month", "all"],
        default="month",
        help="leaderboard window (default: month)",
    )
    p.add_argument("--limit", type=int, default=25, help="number of traders (default: 25)")
    p.add_argument(
        "--category",
        default="crypto",
        help="leaderboard category server-side filter (default: crypto; '' to disable)",
    )
    p.add_argument(
        "--output",
        default="-",
        help="output path, or '-' for stdout (default: -)",
    )
    p.add_argument(
        "--format",
        choices=["json", "human"],
        default="human",
        help="output format (default: human)",
    )
    p.add_argument(
        "--include-raw",
        action="store_true",
        help="include raw trades alongside the dossier",
    )
    p.add_argument(
        "--max-trades",
        type=int,
        default=None,
        help="optional per-wallet trade cap",
    )
    p.add_argument(
        "--wallet",
        default=None,
        help="skip the leaderboard and dossier a single wallet address",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v INFO, -vv DEBUG (default: WARNING)",
    )


def _add_db_path(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db-path",
        default=None,
        help="SQLite path (default: $POLYMARKET_ALPHA_DB or ~/.polymarket_alpha/data.db)",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v INFO, -vv DEBUG"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polymarket_alpha",
        description="Polymarket top-trader auditor + storage/WS pipeline.",
    )
    sub = p.add_subparsers(dest="command")

    audit = sub.add_parser("audit", help="one-shot top-trader dossier (v1)")
    _add_audit_args(audit)

    db = sub.add_parser("db", help="database lifecycle")
    db.add_argument("action", choices=["init", "migrate", "vacuum", "stats"])
    _add_db_path(db)

    worker = sub.add_parser("worker", help="run an ingest worker")
    worker.add_argument(
        "name",
        choices=["leaderboard", "activity", "ws", "resolver", "reconciler", "all"],
    )
    worker.add_argument("--interval", type=int, default=None, help="loop seconds")
    worker.add_argument("--once", action="store_true", help="single cycle then exit")
    _add_db_path(worker)

    w = sub.add_parser("wallet", help="query stored activity for a wallet")
    w.add_argument("address")
    w.add_argument("--hours", type=int, default=24)
    w.add_argument("--crypto-only", action="store_true")
    _add_db_path(w)

    lb = sub.add_parser("leaderboard", help="query a stored leaderboard snapshot")
    lb.add_argument("period", choices=["day", "week", "month"])
    lb.add_argument("--at", default=None, help="ISO8601; latest at-or-before")
    _add_db_path(lb)

    tbl = sub.add_parser("table", help="wallet staleness table")
    tbl.add_argument("--wallets", required=True, help="comma-separated addresses")
    _add_db_path(tbl)

    exp = sub.add_parser("export", help="export activities as JSONL")
    exp.add_argument("--since", default="7d", help="e.g. 7d, 48h, 3600s")
    exp.add_argument("--format", choices=["jsonl"], default="jsonl")
    exp.add_argument("--out", default="-")
    _add_db_path(exp)

    return p


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _decimal_default(obj: Any) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _dossier_to_dict(d: StrategyDossier) -> dict:
    out = dataclasses.asdict(d)
    return out


def _render_human(dossiers: list[StrategyDossier]) -> str:
    lines: list[str] = []
    for d in dossiers:
        t = d.trader
        lines.append("=" * 72)
        lines.append(f"#{t.rank} {t.user_name or '(anon)'}  {t.proxy_wallet}")
        lines.append(
            f"  window={d.window}  leaderboard_pnl={t.pnl}  volume={t.volume}"
        )
        lines.append(
            f"  TAGS: sizing={d.sizing_style} conviction={d.conviction} "
            f"direction={d.direction_bias} speed={d.speed}"
        )
        lines.append(
            f"  markets={d.n_markets} crypto={d.n_crypto_markets} "
            f"trades={d.n_trades} resolved={d.n_resolved_markets}"
        )
        lines.append(
            f"  realized_pnl={d.realized_pnl}  hit_rate={d.hit_rate}"
        )
        lines.append(
            f"  position_usdc: median={d.median_position_usdc} "
            f"p90={d.p90_position_usdc} max={d.max_position_usdc}"
        )
        lines.append(
            f"  top3_concentration={d.top3_concentration_pct}%  "
            f"median_hold_s={d.median_holding_period_seconds}"
        )
        if d.notes:
            lines.append(f"  notes ({len(d.notes)}):")
            for n in d.notes[:10]:
                lines.append(f"    - {n}")
            if len(d.notes) > 10:
                lines.append(f"    ... (+{len(d.notes) - 10} more)")
    lines.append("=" * 72)
    return "\n".join(lines)


def _emit(text: str, output: str) -> None:
    if output == "-":
        sys.stdout.write(text + "\n")
    else:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        log.info("wrote output to %s", output)


def run_audit(args: argparse.Namespace) -> int:
    configure_logging(args.verbose)

    try:
        with build_client() as client:
            lb = LeaderboardClient(client)
            act = ActivityClient(client)
            gamma = GammaClient(client)

            if args.wallet:
                traders = [
                    Trader(
                        rank=0,
                        proxy_wallet=args.wallet.lower(),
                        user_name="",
                        x_username=None,
                        verified_badge=False,
                        volume=Decimal(0),
                        pnl=Decimal(0),
                        profile_image=None,
                    )
                ]
            else:
                traders = lb.fetch(
                    window=args.period,
                    limit=args.limit,
                    category=(args.category or None),
                )

            crypto_tag_id = gamma.resolve_crypto_tag_id()

            dossiers: list[StrategyDossier] = []
            for trader in traders:
                trades = act.fetch_trades(
                    trader.proxy_wallet, max_trades=args.max_trades
                )
                assets = [t.asset for t in trades if t.asset]
                markets = gamma.fetch_markets_by_token_ids(
                    assets, crypto_tag_id=crypto_tag_id
                )

                metrics = compute_trader_metrics(trades, markets, crypto_only=True)
                raw = None
                if args.include_raw:
                    raw = {
                        "trades": [dataclasses.asdict(t) for t in trades],
                    }
                dossier = build_dossier(
                    trader,
                    metrics,
                    window=args.period,
                    include_raw=args.include_raw,
                    raw_payloads=raw,
                )
                dossiers.append(dossier)
                log.info(
                    "dossier for %s: %s/%s/%s/%s",
                    trader.proxy_wallet,
                    dossier.sizing_style,
                    dossier.conviction,
                    dossier.direction_bias,
                    dossier.speed,
                )
    except PolymarketAPIError as exc:
        log.error("API error: %s", exc)
        return 1

    if args.format == "json":
        payload = json.dumps(
            [_dossier_to_dict(d) for d in dossiers],
            default=_decimal_default,
            indent=2,
        )
    else:
        payload = _render_human(dossiers)
    _emit(payload, args.output)
    return 0


_SUBCOMMANDS = {"audit", "db", "worker", "wallet", "leaderboard", "table", "export"}


def _parse_since(s: str) -> int:
    s = s.strip().lower()
    unit = s[-1]
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None:
        return int(s)
    return int(s[:-1]) * mult


async def _cmd_db(args: argparse.Namespace) -> int:
    from polymarket_alpha import storage

    path = storage.resolve_db_path(args.db_path)
    conn = await storage.connect(path)
    try:
        if args.action in ("init", "migrate"):
            v = await storage.run_migrations(conn)
            print(f"schema at version {v} ({path})")
        elif args.action == "vacuum":
            await storage.vacuum(conn)
            print(f"vacuumed {path}")
        elif args.action == "stats":
            for name, cnt in (await storage.table_stats(conn)).items():
                print(f"{name:<24} {cnt}")
    finally:
        await conn.close()
    return 0


_WORKERS = {
    "leaderboard": ("polymarket_alpha.workers.leaderboard_poller", 300),
    "activity": ("polymarket_alpha.workers.activity_poller", 300),
    "ws": ("polymarket_alpha.workers.ws_subscriber", 300),
    "resolver": ("polymarket_alpha.workers.market_resolver", 60),
    "reconciler": ("polymarket_alpha.workers.reconciler", 60),
}


async def _cmd_worker(args: argparse.Namespace) -> int:
    import asyncio
    import importlib
    import signal

    from polymarket_alpha import storage

    db_path = storage.resolve_db_path(args.db_path)
    boot = await storage.connect(db_path)
    await storage.run_migrations(boot)
    await boot.close()

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, ValueError):
            pass

    open_conns: list = []

    async def _run_for(name: str):
        # One connection per worker (WAL): no cross-worker txn interleaving.
        conn = await storage.connect(db_path)
        open_conns.append(conn)
        mod_path, default_interval = _WORKERS[name]
        mod = importlib.import_module(mod_path)
        interval = args.interval or default_interval
        await mod.run(conn, shutdown, interval=interval, once=args.once)

    try:
        if args.name == "all" and args.once:
            # Single coherent cold cycle: respect data dependencies
            # (activity -> resolver -> reconciler; ws after markets exist).
            for name in (
                "leaderboard",
                "activity",
                "resolver",
                "reconciler",
                "ws",
            ):
                if shutdown.is_set():
                    break
                await _run_for(name)
        else:
            names = list(_WORKERS) if args.name == "all" else [args.name]
            await asyncio.gather(
                *(asyncio.create_task(_run_for(n), name=n) for n in names)
            )
    finally:
        for c in open_conns:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


async def _cmd_wallet(args: argparse.Namespace) -> int:
    import time

    from polymarket_alpha import storage
    from polymarket_alpha.storage.repositories import activities as act_repo

    conn = await storage.connect(storage.resolve_db_path(args.db_path))
    try:
        since = int(time.time()) - args.hours * 3600
        rows = await act_repo.by_wallet(
            conn,
            args.address,
            since_ts=since,
            crypto_only=args.crypto_only,
        )
        for r in rows:
            print(
                json.dumps(
                    {k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()}
                )
            )
        print(f"# {len(rows)} activities", file=sys.stderr)
    finally:
        await conn.close()
    return 0


async def _cmd_leaderboard(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from polymarket_alpha import storage
    from polymarket_alpha.storage.repositories import leaderboard as lb_repo

    conn = await storage.connect(storage.resolve_db_path(args.db_path))
    try:
        at_ts = None
        if args.at:
            at_ts = int(
                datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .timestamp()
            )
        snap = await lb_repo.latest_snapshot(conn, args.period, at_ts=at_ts)
        if snap is None:
            print("no snapshot found", file=sys.stderr)
            return 1
        entries = await lb_repo.entries_for_snapshot(conn, snap["snapshot_id"])
        print(f"# {args.period} snapshot {snap['snapshot_ts']} ({len(entries)} entries)")
        for e in entries:
            print(f"{e['rank']:>4}  {e['wallet']}  pnl={e['pnl_usd']}")
    finally:
        await conn.close()
    return 0


async def _cmd_table(args: argparse.Namespace) -> int:
    from polymarket_alpha import storage
    from polymarket_alpha.helpers.wallet_activity import WalletActivityTracker

    conn = await storage.connect(storage.resolve_db_path(args.db_path))
    try:
        wallets = [w.strip() for w in args.wallets.split(",") if w.strip()]
        print(await WalletActivityTracker(conn).render_table(wallets))
    finally:
        await conn.close()
    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    import time

    from polymarket_alpha import storage

    conn = await storage.connect(storage.resolve_db_path(args.db_path))
    try:
        since = int(time.time()) - _parse_since(args.since)
        cur = await conn.execute(
            "SELECT * FROM activities WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        )
        rows = await cur.fetchall()
        out = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
        try:
            for r in rows:
                out.write(json.dumps(dict(r)) + "\n")
        finally:
            if out is not sys.stdout:
                out.close()
        print(f"# exported {len(rows)} rows", file=sys.stderr)
    finally:
        await conn.close()
    return 0


def main() -> None:
    import asyncio

    argv = sys.argv[1:]
    if not argv or (argv[0] not in _SUBCOMMANDS and argv[0].startswith("-")):
        argv = ["audit", *argv]  # back-compat: bare flags == v1 audit

    args = build_arg_parser().parse_args(argv)
    if args.command in (None, "audit"):
        if args.command is None:
            build_arg_parser().print_help()
            sys.exit(0)
        sys.exit(run_audit(args))

    configure_logging(getattr(args, "verbose", 0))
    handlers = {
        "db": _cmd_db,
        "worker": _cmd_worker,
        "wallet": _cmd_wallet,
        "leaderboard": _cmd_leaderboard,
        "table": _cmd_table,
        "export": _cmd_export,
    }
    sys.exit(asyncio.run(handlers[args.command](args)))


if __name__ == "__main__":
    main()
