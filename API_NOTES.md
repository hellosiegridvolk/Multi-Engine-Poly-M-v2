# API_NOTES.md — verified Polymarket API surface

The draft spec was LLM-written and guessed endpoint paths/params. Every
endpoint below was verified with live `curl`/`httpx` probes before and during
implementation. Where the spec was wrong, the correction and the verified
behavior are recorded here.

## 1. Leaderboard — spec was WRONG

- **Spec claimed:** `GET https://data-api.polymarket.com/leaderboard?period=week&category=crypto`
- **Verified:** `GET https://data-api.polymarket.com/v1/leaderboard?window={day|week|month|all}&limit=N&category=crypto`
  - The bare `/leaderboard` path → **HTTP 404**. The often-cited `lb-api.polymarket.com` host → **404**. Only `/v1/leaderboard` on `data-api` works.
  - Param is **`window`**, not `period` (`period=` is silently ignored).
  - Default page size is **25**; `limit` works.
  - Default sort is **`pnl` descending** (record 1 had higher pnl, lower volume than record 2).
  - Record schema: `rank`(str), `proxyWallet`, `userName`, `xUsername`, `verifiedBadge`(bool), `vol`(float), `pnl`(float), `profileImage`.
- **`category` is fuzzy/best-effort.** `category=crypto` returns a *distinct* crypto-leaning board, but `category=sports` and invalid values silently fall back to the global board. So server-side `category` is used only to narrow the candidate pool; **per-market crypto truth is decided by Gamma tags** (see §3), never by this param.

## 2. Activity — spec param name WRONG, hard offset ceiling

- **Spec claimed:** `?user=...&activity_types=TRADE`
- **Verified:** `GET https://data-api.polymarket.com/activity?user={wallet}&type=TRADE&limit=N&offset=M`
  - Param is **`type`**, not `activity_types`.
  - Default `limit` is 100; large limits are honored (500 worked) but pagination uses `offset`.
  - **Hard offset ceiling: `offset > 3000` → HTTP 400.** Verified exactly: `offset=3000` OK, `offset=3001` → 400, `offset=5000` → 400. The client stops at the ceiling and also treats a 400 on a continuation page as end-of-data (instead of aborting). **Practical limit: ~3100 most-recent trades per wallet.**
  - Trade schema: `proxyWallet`, `timestamp`(unix s), `conditionId`, `type`, `size`(shares), `usdcSize`, `transactionHash`, `price`, `asset`(clob token id string), `side`(BUY/SELL), `outcomeIndex`(0/1), `outcome`(human label), `title`, `slug`, `eventSlug`.
  - **`outcome` is a human label** ("Yes"/"No" or "Up"/"Down" or a name), *not* a normalized YES/NO. Normalization is done against Gamma `outcomes` via `outcomeIndex`.

## 3. Gamma markets — double-decode confirmed, crypto-tag mechanism WRONG, plus pagination gotchas

- **Spec claimed crypto filter:** `?tag=crypto` / `?tag_slug=crypto` — **both ignored** (return generic unfiltered markets).
- **Verified crypto mechanism:** crypto tag id = **`21`** via `GET https://gamma-api.polymarket.com/tags/slug/crypto`. Per-market classification uses **`?include_tag=true`**, which returns each market's `tags` array; a market is crypto if any tag has `slug == "crypto"` (or `id == 21`). This replaced the original plan's full `tag_id=21` universe scan, which was slow (~200 requests, 15.6k markets) and truncated by a `max_pages` cap. Documented deviation from the approved plan; the result is faster and not truncated.
- **Double-decode gotcha — CONFIRMED.** `clobTokenIds`, `outcomes`, and `outcomePrices` come back as **JSON-encoded strings inside the JSON response**. They each need a second `json.loads`. `clobTokenIds` decodes to `[yes_token_id, no_token_id]`, index-aligned with `outcomes`.
- **`/markets` returns ONLY open markets by default.** `?clob_token_ids=<token>` for a *resolved* market returns `n=0`; the same query with `&closed=true` returns it. There is no "both" — the client queries each batch **twice (open + `closed=true`) and merges**.
- **Default `limit` is 20; max effective `limit` is 100** (`limit=500` is capped at 100). The client sends an explicit `limit=100` and keeps batch size well under 100 markets/response.
- `clob_token_ids` is a repeatable query param; batch resolution in one call is confirmed.
- Resolution: a `closed=true` market with exactly one `outcomePrices` entry == `1` → that index won. `closed=false`, or degenerate prices (e.g. `["0.5","0.5"]`) → treated unresolved (no PnL), with a note.

## 4. Binary-market definition (domain correction)

The approved plan defined `is_binary` as exactly `{"yes","no"}`. Live data
showed Polymarket **crypto** markets are overwhelmingly **"X Up or Down"**
hourly markets with `outcomes == ["Up","Down"]`, not `["Yes","No"]`. The strict
definition excluded ~all crypto activity from conviction/direction.

**Decision:** any **2-outcome** market is treated as binary. For literal
Yes/No markets the "yes" outcome is located (handles a reversed
`["No","Yes"]`); for other 2-outcome markets (Up/Down) **outcome index 0 is
the YES-equivalent**. True multi-outcome markets (>2 outcomes) remain
non-binary and are excluded from implied-prob / conviction / direction (still
counted in volume / sizing / settlement PnL).

## 5. Implied-probability rounding

`Decimal` everywhere; prices quantized to 4dp with `ROUND_DOWN`. The rule is
applied to the **final reported bound**: for the `NO` cases, `1 - p` is
computed from the **raw** price first, then quantized down
(e.g. `BUY NO @0.62559` → `1 - 0.62559 = 0.37441` → `0.3744`).

## 6. Known limitations

- Per-wallet history capped at ~3100 most-recent trades (API offset ceiling).
- Unresolved/open markets contribute no PnL and are excluded from hit-rate (noted).
- Multi-outcome (>2) markets excluded from conviction/direction (noted).
- Server-side leaderboard `category` is best-effort only.
- Private/empty wallets → near-empty dossier (exit 0, not an error).

---

# v2 verification (storage + WebSocket + multi-timeframe)

## A. CLOB WebSocket — VERIFIED

- **URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market` (the public
  *market* channel). Confirmed live. Stored as an overridable config constant,
  not hardcoded at call sites.
- **Subscribe:** send one JSON text frame `{"assets_ids": [<token_id>, ...],
  "type": "market"}`. Adding more token ids = send another such frame on the
  same socket (no teardown needed).
- **Event types on the market channel** (each frame is a JSON *array* of events):
  - `book` — full order-book snapshot (on subscribe + periodic).
  - `price_change` — order-book delta (`price_changes[]`).
  - `last_trade_price` — **the trade print.** Fields: `market` (conditionId),
    `asset_id` (token id), `price`, `size`, `side` (BUY/SELL), `timestamp`
    (ms string), `fee_rate_bps`, `event_type`, `transaction_hash` (0x…).
- **CRITICAL — no wallet attribution.** The public market channel's
  `last_trade_price` has **no maker/taker wallet and no log_index**. The
  authenticated `user` channel only streams *your own* account and is useless
  for auditing arbitrary traders. Consequence: WS is a **market-level trade
  tape**, not a wallet-keyed feed. The reconciler therefore **links** a
  `ws_trades_raw` row to an existing REST-sourced `activities` row by
  `transaction_hash` (enrichment / confirmation, and sets
  `promoted_activity_id`); it does **not** mint wallet-less activity rows.
  WS prints with no matching REST activity stay unpromoted — expected, not an
  error (flagged in `ingest_runs.notes`).
- **Ping/pong:** sending the text frame `PING` returns text `PONG`. The
  `websockets` library's protocol-level ping (`ping_interval`) also keeps the
  connection alive (verified 60s idle). We rely on the library's ping plus an
  app-level `PING` every ~10s; no custom `while True` reconnect loop.
- **Reconnect:** exponential backoff 1→2→4→…→60s cap; re-send all active
  subscriptions on reconnect.

## B. Leaderboard pagination — spec belief WRONG

- Nicola believed the cap was ~3000. **Reality: per-query page size caps at
  50** (`limit=100/1000/10000` all return 50). Pagination is **offset-based**
  (`&offset=`, verified: `offset=10` → ranks start at 11). Data extends well
  past `offset=5000` (still returns 50). So: page size 50, offset-paginated,
  deep. The leaderboard poller pages with `offset` up to a configurable max
  (default top 500) and sets `hit_pagination_cap=1` only if a page returns
  fewer than 50 before reaching the configured max.
- **Period values:** `window=` accepts `day|week|month|all`. Unknown values
  (`hour`, `year`) don't error but have unverified semantics → only the four
  canonical values are used. (Param is `window`, not `period`.)
- Leaderboard records have **no `trade_count`** field → `leaderboard_entries.
  trade_count` is stored NULL.

## C. Activity pagination / types — re-confirmed + refined

- Page size default 100; **hard offset ceiling at 3000** (HTTP 400 beyond) —
  unchanged from v1.
- **One query returns ALL activity types mixed** (observed `TRADE` + `REDEEM`
  together with no `type` param). `type=` also accepts a comma list
  (`type=TRADE,REDEEM`). The activity poller therefore **omits `type`
  entirely** and stores everything; filtering is at query time only.
- **No `log_index` field anywhere** (neither REST activity nor WS). The
  spec's `UNIQUE (tx_hash, log_index)` is therefore unimplementable as-is.
  **Adaptation:** the natural dedup key is a content hash
  `dedup_key = sha1(tx_hash|activity_type|condition_id|token_id|side|shares|timestamp)`,
  `UNIQUE(dedup_key)`, inserted with `INSERT OR IGNORE`. This makes REST/WS
  dedup deterministic and stable across re-polls. (`log_index` kept as a
  nullable column for forward-compat; populated if the API ever exposes it.)

## D. Market category tagging — VERIFIED

- Gamma exposes tags via `?include_tag=true` → each market gets a `tags`
  array of `{id, slug, label}`. A crypto market carries e.g.
  `[(21,'crypto'), (235,'bitcoin'), (1312,'crypto-prices')]`.
- **`is_crypto` rule:** any tag with `slug == "crypto"` or `id == "21"`
  (canonical; matches v1, confirmed correct on live data). The full slug list
  is stored in `markets.category_tags` (JSON) for richer downstream filtering.

## v2 known limitations

- WS provides market-tape trades only (no wallet); wallet attribution comes
  solely from REST `/activity`. WS rows without a matching REST tx stay
  unpromoted by design.
- No `log_index` → dedup is a deterministic content hash, not the on-chain
  (tx, logIndex) pair. Two genuinely distinct fills with identical
  tx/type/market/side/size/timestamp would collapse to one row (vanishingly
  rare; acceptable for a research tool).
- Leaderboard depth is large but the poller caps at a configurable top-N.

## v2 operational findings (from live cold bring-up)

- **Gamma intermittently 500s on long `clob_token_ids` URLs.** A 50-id batch
  (~4.7 KB URL) succeeds most of the time but returns HTTP 500 sporadically.
  Mitigation: the resolver uses 20-id Gamma batches grouped in 40-token
  chunks with **per-chunk error isolation** (a flaky batch is logged and
  skipped, never aborting the whole cycle) and a per-cycle token cap so
  progress is incremental and bounded.
- **`clob_token_ids` only resolves markets Gamma currently serves** —
  long-tail / delisted historical markets return nothing even with
  `closed=true`. **Fixed:** Gamma also accepts `condition_ids` (verified:
  `?condition_ids=<cid>&closed=true` recovers historical/closed markets the
  token filter misses). The resolver now discovers missing `condition_id`s
  directly from `activities` (and from WS payload `market`) and resolves via
  `condition_ids` (open + `closed=true` two-pass). Live-verified: 8/8
  previously-unresolvable historical condition_ids resolved with correct
  `is_crypto` / `resolved` flags. The clob_token_ids path is retained for the
  v1 dossier flow.
- **`worker all --once` runs sequentially in dependency order**
  (leaderboard → activity → resolver → reconciler → ws) so a single cold
  command yields a coherent populated DB. Continuous `worker all` (no
  `--once`) runs the five workers concurrently, which is correct because the
  resolver/reconciler loops self-heal as upstream data lands.
- Activity backfill of a fresh ~300-wallet watchlist is heavy on the first
  cycle (one-shot ≈ tens of thousands of rows); steady state is incremental
  (poll only since the max stored timestamp per wallet).
