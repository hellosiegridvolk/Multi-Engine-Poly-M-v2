"""Prometheus text-exposition metrics from the SQLite DB.

Designed for the node_exporter textfile-collector pattern: write the output
to a `.prom` file in node_exporter's textfile directory and it gets scraped.
No embedded HTTP server — keeps the tool dependency-free.
"""

from __future__ import annotations

import aiosqlite

from polymarket_alpha.storage import table_stats

_PREFIX = "polymarket_alpha"


def _line(name: str, value, labels: dict[str, str] | None = None) -> str:
    if labels:
        lbl = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{_PREFIX}_{name}{{{lbl}}} {value}"
    return f"{_PREFIX}_{name} {value}"


async def render_prometheus(conn: aiosqlite.Connection) -> str:
    """Render all metrics in Prometheus text-exposition format."""
    out: list[str] = []

    # --- table row counts ---
    out.append(f"# HELP {_PREFIX}_table_rows Row count per table.")
    out.append(f"# TYPE {_PREFIX}_table_rows gauge")
    for table, count in (await table_stats(conn)).items():
        out.append(_line("table_rows", count, {"table": table}))

    # --- per-worker ingest run aggregates ---
    rows = await (
        await conn.execute(
            """
            SELECT worker,
                   COUNT(*)                       AS runs,
                   COALESCE(SUM(errors_count), 0) AS errors,
                   COALESCE(MAX(finished_ts), 0)  AS last_finished,
                   COALESCE(SUM(items_written), 0) AS items_written
            FROM ingest_runs
            GROUP BY worker
            """
        )
    ).fetchall()

    out.append(f"# HELP {_PREFIX}_ingest_runs_total Ingest runs per worker.")
    out.append(f"# TYPE {_PREFIX}_ingest_runs_total counter")
    for r in rows:
        out.append(_line("ingest_runs_total", r["runs"], {"worker": r["worker"]}))

    out.append(f"# HELP {_PREFIX}_ingest_errors_total Ingest errors per worker.")
    out.append(f"# TYPE {_PREFIX}_ingest_errors_total counter")
    for r in rows:
        out.append(
            _line("ingest_errors_total", r["errors"], {"worker": r["worker"]})
        )

    out.append(
        f"# HELP {_PREFIX}_last_run_timestamp_seconds "
        f"Unix time of the last finished run per worker."
    )
    out.append(f"# TYPE {_PREFIX}_last_run_timestamp_seconds gauge")
    for r in rows:
        out.append(
            _line(
                "last_run_timestamp_seconds",
                r["last_finished"],
                {"worker": r["worker"]},
            )
        )

    out.append(
        f"# HELP {_PREFIX}_items_written_total Items written per worker (lifetime)."
    )
    out.append(f"# TYPE {_PREFIX}_items_written_total counter")
    for r in rows:
        out.append(
            _line("items_written_total", r["items_written"], {"worker": r["worker"]})
        )

    return "\n".join(out) + "\n"
