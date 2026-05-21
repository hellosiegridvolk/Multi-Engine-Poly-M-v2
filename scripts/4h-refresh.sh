#!/usr/bin/env sh
# 4-hour refresh job for polymarket_alpha. Designed to be invoked by a systemd
# timer or a host crontab line. Idempotent and crash-tolerant: each step logs
# its outcome and the cycle continues even if one step fails transiently.
#
# Suggested cron entry (every 4 hours, on the hour):
#   0 */4 * * *  /opt/polymarket_alpha/scripts/4h-refresh.sh
#
# Or systemd timer: see deploy/polymarket-alpha-refresh.timer.
set -eu

: "${POLYMARKET_ALPHA_HOME:=/opt/polymarket_alpha}"
: "${POLYMARKET_ALPHA_DB:=$HOME/.polymarket_alpha/data.db}"
: "${POLYMARKET_ALPHA_OUTPUT:=$HOME/.polymarket_alpha/sources.yaml}"
: "${POLYMARKET_ALPHA_LOG_DIR:=$HOME/.polymarket_alpha/logs}"
: "${POLYMARKET_ALPHA_PYTHON:=$POLYMARKET_ALPHA_HOME/.venv/bin/python}"
: "${POLYMARKET_ALPHA_TOP:=2}"
: "${POLYMARKET_ALPHA_MIN_HIT_RATE:=0.55}"
: "${POLYMARKET_ALPHA_MAX_TRADES_PER_WALLET:=300}"

mkdir -p "$POLYMARKET_ALPHA_LOG_DIR" "$(dirname "$POLYMARKET_ALPHA_OUTPUT")"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$POLYMARKET_ALPHA_LOG_DIR/refresh-$STAMP.log"

echo "$(date -u +%FT%TZ) refresh starting" | tee -a "$LOG"
echo "  db=$POLYMARKET_ALPHA_DB" | tee -a "$LOG"
echo "  out=$POLYMARKET_ALPHA_OUTPUT" | tee -a "$LOG"

cd "$POLYMARKET_ALPHA_HOME"

"$POLYMARKET_ALPHA_PYTHON" -m polymarket_alpha refresh \
    --db-path "$POLYMARKET_ALPHA_DB" \
    --output "$POLYMARKET_ALPHA_OUTPUT" \
    --top "$POLYMARKET_ALPHA_TOP" \
    --min-hit-rate "$POLYMARKET_ALPHA_MIN_HIT_RATE" \
    --max-trades-per-wallet "$POLYMARKET_ALPHA_MAX_TRADES_PER_WALLET" \
    2>&1 | tee -a "$LOG"

RC=$?
echo "$(date -u +%FT%TZ) refresh finished rc=$RC" | tee -a "$LOG"

# Keep 30 days of logs.
find "$POLYMARKET_ALPHA_LOG_DIR" -name 'refresh-*.log' -mtime +30 -delete 2>/dev/null || true

exit "$RC"
