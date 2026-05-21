"""refresh orchestrator: per-step failure isolation, diff vs previous, YAML write."""

from decimal import Decimal
from pathlib import Path

import pytest

from polymarket_alpha.refresh import _read_previous_yaml, run_refresh
from polymarket_alpha.storage.repositories import leaderboard as lb_repo
from polymarket_alpha.storage.repositories import traders as traders_repo
from polymarket_alpha.storage.repositories.activities import (
    ActivityRow,
    insert_or_ignore,
)
from polymarket_alpha.storage.repositories import markets as markets_repo


NOW = 1_900_000_000


async def _seed_winner(db_conn, wallet: str):
    """Seed a wallet with one resolved-YES winning trade so shortlist accepts it."""
    await traders_repo.upsert(db_conn, wallet, now_ts=NOW)
    snap_id = await lb_repo.create_snapshot(
        db_conn,
        period="day",
        snapshot_ts=NOW,
        ingest_duration_ms=1,
        entries_count=1,
        hit_pagination_cap=False,
    )
    await lb_repo.add_entry(
        db_conn,
        snapshot_id=snap_id,
        rank=1,
        wallet=wallet,
        pnl_usd=Decimal("100"),
        volume_usd=Decimal("50"),
        trade_count=None,
    )
    cid = "0xc" + wallet[-4:]
    await markets_repo.upsert_market(
        db_conn,
        condition_id=cid,
        slug="s",
        question="q",
        end_date_ts=NOW,
        resolved=True,
        resolution="YES",
        category_tags=["crypto"],
        is_crypto=True,
        resolved_at_ts=NOW,
        metadata_fetched_ts=NOW,
    )
    await markets_repo.upsert_token(
        db_conn, token_id="y" + wallet[-4:], condition_id=cid, outcome="YES", outcome_index=0
    )
    await markets_repo.upsert_token(
        db_conn, token_id="n" + wallet[-4:], condition_id=cid, outcome="NO", outcome_index=1
    )
    await insert_or_ignore(
        db_conn,
        ActivityRow(
            wallet=wallet, activity_type="TRADE", condition_id=cid,
            token_id="y" + wallet[-4:], side="BUY", outcome="YES",
            shares=Decimal("100"), usdc=Decimal("40"), price=Decimal("0.40"),
            timestamp=NOW, tx_hash="0xt" + wallet[-4:], source="REST",
        ),
        ingested_ts=NOW,
    )
    await db_conn.commit()


async def test_refresh_writes_yaml_when_shortlist_nonempty(db_conn, tmp_path, monkeypatch):
    # Stub the three workers so we test the orchestrator's wiring, not the API.
    from polymarket_alpha import refresh as refresh_mod
    from polymarket_alpha.workers import activity_poller, leaderboard_poller, market_resolver

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(leaderboard_poller, "run", _noop)
    monkeypatch.setattr(activity_poller, "run", _noop)
    monkeypatch.setattr(market_resolver, "run", _noop)

    wallet = "0x" + "a" * 40
    await _seed_winner(db_conn, wallet)

    out_path = tmp_path / "sources.yaml"
    result = await refresh_mod.run_refresh(
        db_conn, top=2, output_path=out_path, diff_against=out_path,
    )
    assert result.leaderboard_ok and result.activity_ok and result.resolver_ok
    assert result.errors == []
    assert len(result.shortlist_entries) == 1
    assert out_path.exists()
    text = out_path.read_text()
    assert wallet in text
    # First write: previous-file is empty, so all entries are ADDED
    assert any("ADDED:" in c for c in result.changes_vs_previous)


async def test_refresh_continues_on_step_failure(db_conn, tmp_path, monkeypatch):
    """One failing step must not abort the rest of the cycle."""
    from polymarket_alpha import refresh as refresh_mod
    from polymarket_alpha.workers import activity_poller, leaderboard_poller, market_resolver

    async def _ok(*args, **kwargs):
        return None

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(leaderboard_poller, "run", _boom)
    monkeypatch.setattr(activity_poller, "run", _ok)
    monkeypatch.setattr(market_resolver, "run", _ok)

    await _seed_winner(db_conn, "0x" + "b" * 40)

    result = await refresh_mod.run_refresh(
        db_conn, top=2, output_path=tmp_path / "sources.yaml",
    )
    assert result.leaderboard_ok is False
    assert result.activity_ok is True
    assert result.resolver_ok is True
    assert any("leaderboard" in e for e in result.errors)
    # Shortlist still ran on whatever data was already there
    assert len(result.shortlist_entries) == 1


async def test_refresh_diff_detects_changes(db_conn, tmp_path, monkeypatch):
    from polymarket_alpha import refresh as refresh_mod
    from polymarket_alpha.workers import activity_poller, leaderboard_poller, market_resolver

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(leaderboard_poller, "run", _noop)
    monkeypatch.setattr(activity_poller, "run", _noop)
    monkeypatch.setattr(market_resolver, "run", _noop)

    out_path = tmp_path / "sources.yaml"
    out_path.write_text(
        'sources:\n  - id: old\n    address: "0xdead000000000000000000000000000000000000"\n',
        encoding="utf-8",
    )
    wallet = "0x" + "c" * 40
    await _seed_winner(db_conn, wallet)

    result = await refresh_mod.run_refresh(
        db_conn, top=2, output_path=out_path, diff_against=out_path,
    )
    assert any("REMOVED: 0xdead" in c for c in result.changes_vs_previous)
    assert any(f"ADDED:   {wallet.lower()}" in c for c in result.changes_vs_previous)


def test_read_previous_yaml_handles_missing(tmp_path):
    assert _read_previous_yaml(tmp_path / "absent.yaml") == set()
    assert _read_previous_yaml(None) == set()
    p = tmp_path / "p.yaml"
    p.write_text(
        '\n'.join([
            "sources:",
            "  - id: a",
            '    address: "0xAaa"',
            "  - id: b",
            "    address: 0xBBB",
        ]),
        encoding="utf-8",
    )
    assert _read_previous_yaml(p) == {"0xaaa", "0xbbb"}
