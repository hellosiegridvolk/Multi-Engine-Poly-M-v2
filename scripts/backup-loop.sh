#!/usr/bin/env sh
# Daily backup loop for the containerized `backup` service.
# Produces gzipped DB + activities snapshots and rclone-copies them to Drive.
# Not an event poll — a scheduled job, so a sleep loop is correct here.
set -eu

: "${POLYMARKET_ALPHA_DB:=/data/data.db}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_INTERVAL:=86400}"          # seconds (24h)
: "${RCLONE_REMOTE:=gdrive:poly wallet data strategy}"
: "${BACKUP_RETENTION_DAYS:=14}"

echo "backup-loop: db=$POLYMARKET_ALPHA_DB interval=${BACKUP_INTERVAL}s remote='$RCLONE_REMOTE'"

while true; do
    if python -m polymarket_alpha db backup \
            --db-path "$POLYMARKET_ALPHA_DB" \
            --out-dir "$BACKUP_DIR"; then
        if command -v rclone >/dev/null 2>&1; then
            rclone copy "$BACKUP_DIR" "$RCLONE_REMOTE" \
                --include 'poly-wallet-data-strategy-*.gz' --max-age 25h \
                || echo "backup-loop: rclone copy failed (will retry next cycle)"
        else
            echo "backup-loop: rclone not installed; snapshot kept locally only"
        fi
        find "$BACKUP_DIR" -name 'poly-wallet-data-strategy-*.gz' \
            -mtime +"$BACKUP_RETENTION_DAYS" -delete 2>/dev/null || true
    else
        echo "backup-loop: db backup failed (will retry next cycle)"
    fi
    sleep "$BACKUP_INTERVAL"
done
