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
