# Multi-Engine-Poly-M-v2

## polymarket_alpha — Polymarket top-trader strategy auditor

A standalone, modular research tool that scrapes Polymarket's most profitable
wallets (crypto category), pulls their recent trades, and produces a per-trader
**strategy dossier**: quant metrics + behavioral tags. Designed to be wrapped
into a larger algorithmic-trading bot later (clients are written sync but with
an async-friendly surface).

> **API truth:** the original spec guessed endpoint paths/params and several
> were wrong. The real, verified API surface — and every spec correction — is
> documented in [`API_NOTES.md`](./API_NOTES.md). There are **no mock
> fallbacks** in production code: on API failure the tool raises and exits 1.

### Install

```bash
pip install -e ".[dev]"     # Python 3.11 / 3.12 (pinned: >=3.11,<3.13)
```

### Run

```bash
# top 5 crypto traders this week, pretty console output
python -m polymarket_alpha --period week --limit 5

# JSON dossier to a file (Decimals serialized as strings)
python -m polymarket_alpha --period week --limit 5 --format json --output report.json

# audit a single wallet (skip the leaderboard)
python -m polymarket_alpha --wallet 0xABC... --period all
```

Flags: `--period {day,week,month,all}`, `--limit`, `--category` (default
`crypto`; server-side best-effort), `--output PATH|-`, `--format {json,human}`,
`--include-raw`, `--max-trades`, `--wallet`, `-v`/`-vv`.

### What it computes

- **Implied P(YES) per trade** — all four BUY/SELL × YES/NO cases.
- **VWAP entry** (usdc-weighted) per market.
- **Position sizing**: median / p90 / max trade USDC.
- **Concentration**: % of capital in the top-3 markets.
- **Realized PnL & hit-rate** for resolved markets (open markets noted, excluded).
- **Median holding period** per market.
- **Behavioral tags** (thresholds tunable via `DossierConfig`):
  sizing `SCALING_IN/SINGLE_SHOT/PYRAMID/MIXED`,
  conviction `HIGH_CONVICTION/EDGE_SEEKER/MIXED`,
  direction `BULL/BEAR/NEUTRAL`, speed `SCALPER/SWING/POSITIONAL`.

All metrics are **crypto-only** (scoped via Gamma per-market tags).

### Architecture

```
polymarket_alpha/
  http.py                 # shared httpx client: retry/backoff on 429/5xx,
                          #   Retry-After (int + HTTP-date), no mock fallback
  models.py               # Trader/Trade/Market/StrategyDossier (Decimal money)
  clients/leaderboard.py  # /v1/leaderboard
  clients/activity.py     # /activity (paginated; offset-3000 ceiling handled)
  clients/gamma.py        # /markets (double-decode, open+closed merge, tags)
  analysis/metrics.py     # implied prob + quant metrics
  analysis/dossier.py     # DossierConfig + behavior classifiers
  cli.py                  # argparse + json/human serialization
  tests/                  # pytest + respx (no real network in tests)
```

## v2 — persistent storage + WebSocket + multi-timeframe

A SQLite layer + scheduled REST/WS workers that continuously ingest
leaderboards, wallet activity, market metadata and live crypto trade prints.
The v1 verified clients are reused as-is (with additive pagination only).

```bash
# bring it up cold
python -m polymarket_alpha db init
python -m polymarket_alpha worker all            # 5 workers in one process
#   (or run individually: worker leaderboard|activity|ws|resolver|reconciler)
#   --once = single cycle (cron/test); --interval N = override loop seconds

# read-only queries (SQLite only — never hits the live API)
python -m polymarket_alpha leaderboard week --at 2026-05-18T12:00:00Z
python -m polymarket_alpha wallet 0xABC... --hours 24 --crypto-only
python -m polymarket_alpha table --wallets 0xABC...,0xDEF...
python -m polymarket_alpha export --since 7d --out activity.jsonl

# db lifecycle: db init | migrate | vacuum | stats | backup
#   (--db-path / $POLYMARKET_ALPHA_DB)
```

### Backups (Google Drive, every 24h)

`db backup` writes a transactionally-consistent, compacted snapshot
(`VACUUM INTO`, safe under WAL) plus an all-activities JSONL, both gzipped:

```bash
python -m polymarket_alpha db backup \
  --db-path ~/.polymarket_alpha/data.db \
  --out-dir ~/.polymarket_alpha/backups
# -> poly-wallet-data-strategy-<UTC>.db.gz   (full DB, ~13MB for ~82k activities)
#    poly-wallet-data-strategy-<UTC>.jsonl.gz (all activities, ~8MB)
```

Durable 24h upload runs as a **host cron + rclone** job (independent of any
agent session — the agent's container is ephemeral). Drive folder
**"poly wallet data strategy"** already holds a metadata digest and a
`...-README.md` with the exact cron/rclone/restore commands:

```cron
17 3 * * *  /usr/local/bin/poly-backup.sh   # db backup + rclone copy to Drive
```

The multi-MB binaries go via rclone (not inline agent upload, which is
payload-limited). Restore: `gunzip -c <file>.db.gz > data.db` then
`db stats` to verify.

### Deployment (runs independently of any agent session)

The tool is standalone Python — no agent/MCP dependency at runtime. Run it
on a host or in a container; the agent only ever monitors on demand.

**Docker / OrbStack** (same image both; OrbStack = Linux containers on macOS):

```bash
rclone config                       # one-time: create remote 'gdrive'
docker compose up -d                # worker (all 5) + daily backup service
docker compose logs -f worker
docker compose exec worker python -m polymarket_alpha db stats
```

`docker-compose.yml` runs two services off one image sharing a **named
volume** `poly_data` (the SQLite DB — survives rebuilds; keep it on a local
volume, not a network/overlay FS, for WAL correctness). The `backup` service
runs `scripts/backup-loop.sh` (daily `db backup` + `rclone copy` to the
"poly wallet data strategy" Drive folder). `SIGTERM` (docker stop) triggers
a graceful worker shutdown.

**systemd** (non-Docker): `deploy/polymarket-alpha.service` runs `worker
all`; `deploy/polymarket-alpha-backup.{service,timer}` does the daily
backup. Edit the `CHANGE_ME`/paths, copy to `/etc/systemd/system/`,
`daemon-reload`, then enable both units.

**Persistence is the only hard requirement** — the DB must live on a
durable volume/path. **Monitoring**: read the tool's own signals
(`db stats`, `ingest_runs.errors_count`, container/journal logs), not the
agent. The agent session is optional and on-call only (PR webhooks /
explicit invocation); it cannot be a 24/7 daemon.

Key verified realities driving the v2 design (full detail in `API_NOTES.md`):

- **Leaderboard** page size caps at **50** (not ~3000) — offset-paginated, deep.
- **Activity** has a hard **offset-3000** ceiling; one query returns all
  activity types mixed (poller stores everything, filters at query time).
- **No `log_index`** anywhere → dedup is a deterministic content hash.
- **CLOB WS** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) trade
  prints (`last_trade_price`) carry **no wallet** — WS is a market tape; the
  reconciler links WS prints to REST activities by `tx_hash`.

Storage: `aiosqlite`, WAL mode, versioned `.sql` migrations, all money as
TEXT↔`Decimal` at the repository boundary.

### Test

```bash
pytest -q          # v1 + v2: implied-prob math, clobTokenIds double-decode,
                   # sizing classifier, migrations, REST/WS dedup, crypto
                   # filter, WS re-subscribe, pagination-cap, backfill,
                   # reconciler, byte-for-byte staleness table.
```
