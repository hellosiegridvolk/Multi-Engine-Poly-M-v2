"""4h refresh orchestration: leaderboard -> activity -> resolver -> shortlist.

Runs as a single sequenced cycle (not concurrent workers). Designed to be
invoked from a host cron / systemd timer every 4 hours. Continues on per-step
failure so a transient outage doesn't lose the whole cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite

from polymarket_alpha.helpers.strategies import (
    ShortlistEntry,
    render_copy_sources_yaml,
    shortlist,
)
from polymarket_alpha.workers import (
    activity_poller,
    leaderboard_poller,
    market_resolver,
)

log = logging.getLogger("polymarket_alpha.refresh")


@dataclass(frozen=True, slots=True)
class RefreshResult:
    started_ts: int
    finished_ts: int
    leaderboard_ok: bool
    activity_ok: bool
    resolver_ok: bool
    shortlist_entries: list[ShortlistEntry]
    changes_vs_previous: list[str]
    errors: list[str]


def _read_previous_yaml(path: Path | None) -> set[str]:
    """Extract the set of `address:` values from a previous sources.yaml."""
    if path is None or not path.exists():
        return set()
    addrs: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("address:"):
            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if value:
                addrs.add(value.lower())
    return addrs


async def _run_step(
    name: str,
    coro_factory,
    errors: list[str],
) -> bool:
    """Run a single step and capture failure; never re-raise."""
    log.info("refresh: starting step '%s'", name)
    try:
        await coro_factory()
        log.info("refresh: step '%s' OK", name)
        return True
    except Exception as exc:  # noqa: BLE001 - one bad step must not abort cycle
        log.exception("refresh: step '%s' failed", name)
        errors.append(f"{name}: {type(exc).__name__}: {exc}")
        return False


async def run_refresh(
    conn: aiosqlite.Connection,
    *,
    top: int = 2,
    min_hit_rate: Decimal = Decimal("0.55"),
    output_path: Path | None = None,
    diff_against: Path | None = None,
    max_items_per_wallet: int = 300,
) -> RefreshResult:
    """Run one full refresh cycle. Returns a structured result; never raises."""
    started = int(time.time())
    errors: list[str] = []
    shutdown = asyncio.Event()  # never set; workers run once and return

    lb_ok = await _run_step(
        "leaderboard",
        lambda: leaderboard_poller.run(
            conn, shutdown, interval=1, once=True, max_rank=500
        ),
        errors,
    )
    act_ok = await _run_step(
        "activity",
        lambda: activity_poller.run(
            conn,
            shutdown,
            interval=1,
            once=True,
            max_items_per_wallet=max_items_per_wallet,
        ),
        errors,
    )
    res_ok = await _run_step(
        "resolver",
        lambda: market_resolver.run(conn, shutdown, interval=1, once=True),
        errors,
    )

    # Shortlist: always attempted (works off whatever data was ingested).
    entries: list[ShortlistEntry] = []
    try:
        entries = await shortlist(conn, top=top, min_hit_rate=min_hit_rate)
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh: shortlist failed")
        errors.append(f"shortlist: {type(exc).__name__}: {exc}")

    # Compute diff vs. previous YAML (operator's "what changed" signal).
    prev_addrs = _read_previous_yaml(diff_against or output_path)
    new_addrs = {e.wallet.lower() for e in entries}
    changes: list[str] = []
    added = new_addrs - prev_addrs
    removed = prev_addrs - new_addrs
    for addr in sorted(added):
        changes.append(f"ADDED:   {addr}")
    for addr in sorted(removed):
        changes.append(f"REMOVED: {addr}")
    if prev_addrs and not added and not removed:
        changes.append("UNCHANGED: same wallets as previous run")

    # Write output YAML atomically (write to .tmp then rename).
    if output_path is not None and entries:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp.write_text(render_copy_sources_yaml(entries), encoding="utf-8")
        tmp.replace(output_path)
        log.info("refresh: wrote %d entries to %s", len(entries), output_path)

    return RefreshResult(
        started_ts=started,
        finished_ts=int(time.time()),
        leaderboard_ok=lb_ok,
        activity_ok=act_ok,
        resolver_ok=res_ok,
        shortlist_entries=entries,
        changes_vs_previous=changes,
        errors=errors,
    )
