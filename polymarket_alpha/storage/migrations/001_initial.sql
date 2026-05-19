-- polymarket_alpha schema v1
-- All monetary amounts stored as TEXT to preserve Decimal precision.
-- NOTE: Polymarket exposes no log_index on REST activity or WS prints, so the
-- spec's UNIQUE(tx_hash, log_index) is replaced by a deterministic content
-- hash `dedup_key`. log_index is kept as a nullable column for forward-compat.

CREATE TABLE IF NOT EXISTS traders (
  wallet           TEXT PRIMARY KEY,
  username         TEXT,
  profile_visible  INTEGER NOT NULL DEFAULT 1,
  first_seen_ts    INTEGER NOT NULL,
  last_seen_ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
  snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  period           TEXT NOT NULL CHECK (period IN ('day','week','month')),
  snapshot_ts      INTEGER NOT NULL,
  ingest_duration_ms INTEGER,
  entries_count    INTEGER,
  hit_pagination_cap INTEGER NOT NULL DEFAULT 0,
  UNIQUE (period, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_lb_snap_period_ts
  ON leaderboard_snapshots(period, snapshot_ts DESC);

CREATE TABLE IF NOT EXISTS leaderboard_entries (
  snapshot_id      INTEGER NOT NULL REFERENCES leaderboard_snapshots(snapshot_id),
  rank             INTEGER NOT NULL,
  wallet           TEXT NOT NULL REFERENCES traders(wallet),
  pnl_usd          TEXT NOT NULL,
  volume_usd       TEXT,
  trade_count      INTEGER,
  PRIMARY KEY (snapshot_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_lb_entries_wallet ON leaderboard_entries(wallet);

CREATE TABLE IF NOT EXISTS markets (
  condition_id     TEXT PRIMARY KEY,
  slug             TEXT,
  question         TEXT,
  end_date_ts      INTEGER,
  resolved         INTEGER NOT NULL DEFAULT 0,
  resolution       TEXT,
  category_tags    TEXT,
  is_crypto        INTEGER NOT NULL DEFAULT 0,
  resolved_at_ts   INTEGER,
  metadata_fetched_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_crypto ON markets(is_crypto, end_date_ts);

CREATE TABLE IF NOT EXISTS tokens (
  token_id         TEXT PRIMARY KEY,
  condition_id     TEXT NOT NULL REFERENCES markets(condition_id),
  outcome          TEXT NOT NULL CHECK (outcome IN ('YES','NO')),
  outcome_index    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_condition ON tokens(condition_id);

CREATE TABLE IF NOT EXISTS activities (
  activity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet           TEXT NOT NULL REFERENCES traders(wallet),
  activity_type    TEXT NOT NULL,
  condition_id     TEXT,
  token_id         TEXT,
  side             TEXT,
  outcome          TEXT,
  shares           TEXT,
  usdc             TEXT,
  price            TEXT,
  timestamp        INTEGER NOT NULL,
  tx_hash          TEXT,
  log_index        INTEGER,
  dedup_key        TEXT NOT NULL,
  source           TEXT NOT NULL CHECK (source IN ('REST','WS')),
  ingested_ts      INTEGER NOT NULL,
  UNIQUE (dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_activities_wallet_ts
  ON activities(wallet, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activities_condition_ts
  ON activities(condition_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activities_type_ts
  ON activities(activity_type, timestamp DESC);

CREATE TABLE IF NOT EXISTS ws_trades_raw (
  raw_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  payload_json     TEXT NOT NULL,
  market_token     TEXT,
  tx_hash          TEXT,
  ts               INTEGER NOT NULL,
  promoted_activity_id INTEGER REFERENCES activities(activity_id)
);
CREATE INDEX IF NOT EXISTS idx_ws_raw_ts ON ws_trades_raw(ts DESC);
CREATE INDEX IF NOT EXISTS idx_ws_raw_unpromoted
  ON ws_trades_raw(promoted_activity_id) WHERE promoted_activity_id IS NULL;

CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  worker           TEXT NOT NULL,
  started_ts       INTEGER NOT NULL,
  finished_ts      INTEGER,
  items_seen       INTEGER,
  items_written    INTEGER,
  errors_count     INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  notes            TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
