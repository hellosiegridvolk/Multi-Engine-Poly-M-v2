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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polymarket_alpha",
        description="Audit Polymarket top-trader strategies (crypto category).",
    )
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


def run(args: argparse.Namespace) -> int:
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


def main() -> None:
    sys.exit(run(build_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
