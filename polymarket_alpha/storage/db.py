"""aiosqlite connection helper + idempotent migration runner."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import aiosqlite

log = logging.getLogger("polymarket_alpha.storage.db")

ENV_VAR = "POLYMARKET_ALPHA_DB"
DEFAULT_DB_PATH = Path.home() / ".polymarket_alpha" / "data.db"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def resolve_db_path(cli_arg: str | None = None) -> Path:
    """Precedence: explicit CLI arg > env var > default."""
    if cli_arg:
        return Path(cli_arg).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


async def connect(path: Path | str) -> aiosqlite.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    return conn


def _discover_migrations() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found, key=lambda t: t[0])


async def current_version(conn: aiosqlite.Connection) -> int:
    row = await (
        await conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        )
    ).fetchone()
    if row is None:
        return 0
    res = await (
        await conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    ).fetchone()
    return int(res[0]) if res else 0


async def run_migrations(conn: aiosqlite.Connection) -> int:
    """Apply every pending migration in order. Idempotent."""
    version = await current_version(conn)
    applied = version
    for num, path in _discover_migrations():
        if num <= version:
            continue
        log.info("applying migration %s", path.name)
        sql = path.read_text(encoding="utf-8")
        await conn.executescript(sql)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (num,)
        )
        await conn.commit()
        applied = num
    if applied == version:
        log.info("schema up to date at version %d", version)
    else:
        log.info("schema migrated %d -> %d", version, applied)
    return applied


async def vacuum(conn: aiosqlite.Connection) -> None:
    await conn.execute("VACUUM")
    await conn.commit()


async def backup(
    conn: aiosqlite.Connection,
    *,
    out_dir: Path,
    base_name: str = "poly-wallet-data-strategy",
) -> dict[str, str]:
    """Produce a consistent gzipped snapshot + a gzipped activities JSONL.

    Uses ``VACUUM INTO`` for a transactionally-consistent, compacted copy
    (safe under WAL). Returns the written file paths. Drive-agnostic: the
    durable upload is done by a host cron (see README).
    """
    import gzip
    import json
    import shutil
    from datetime import datetime, timezone

    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    snap = out_dir / f"{base_name}-{stamp}.db"
    safe = str(snap).replace("'", "''")
    await conn.execute(f"VACUUM INTO '{safe}'")

    db_gz = out_dir / f"{base_name}-{stamp}.db.gz"
    with open(snap, "rb") as src, gzip.open(db_gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    snap.unlink()

    jsonl_gz = out_dir / f"{base_name}-{stamp}.jsonl.gz"
    cur = await conn.execute("SELECT * FROM activities ORDER BY timestamp")
    cols = [d[0] for d in cur.description]
    rows = await cur.fetchall()
    with gzip.open(jsonl_gz, "wt", encoding="utf-8") as g:
        for r in rows:
            g.write(json.dumps(dict(zip(cols, r))) + "\n")

    log.info(
        "backup: %s (%d bytes), %s (%d rows)",
        db_gz.name,
        db_gz.stat().st_size,
        jsonl_gz.name,
        len(rows),
    )
    return {"db_gz": str(db_gz), "jsonl_gz": str(jsonl_gz)}


async def table_stats(conn: aiosqlite.Connection) -> dict[str, int]:
    rows = await (
        await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ).fetchall()
    stats: dict[str, int] = {}
    for r in rows:
        name = r[0]
        cnt = await (await conn.execute(f"SELECT COUNT(*) FROM {name}")).fetchone()
        stats[name] = int(cnt[0])
    return stats
