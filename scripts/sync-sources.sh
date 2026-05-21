#!/usr/bin/env sh
# Sync the locally-generated polymarket_alpha shortlist (sources.yaml) to a
# remote bot host, and optionally trigger a restart of the bot's copy_watcher.
#
# Designed to be called either right after `polymarket_alpha refresh`, or in
# its own systemd timer / crontab.
#
# Required env (no defaults — fails loudly if missing):
#   REMOTE_USER     ssh user on the remote host (e.g. "nicola")
#   REMOTE_HOST     remote hostname or IP (e.g. "bot-mac.local")
#   REMOTE_PATH     absolute path to copy_sources.yaml on the remote host
#                   (e.g. "/Users/nicola/Downloads/Assited trading Bot/config/copy_sources.yaml")
#
# Optional env:
#   LOCAL_SOURCES        default: ~/.polymarket_alpha/sources.yaml
#   REMOTE_RESTART_CMD   shell command to run on the remote host after a
#                        successful copy (e.g. "cd /path/to/bot && docker compose
#                        build copy_watcher && docker compose up -d copy_watcher")
#                        Empty by default → no restart triggered.
#   SSH_OPTS             extra options for ssh / scp (e.g. "-p 2222 -i ~/.ssh/id_x")
#
# Exits 0 on no-op (file unchanged), 0 on successful copy + (optional) restart,
# non-zero on any failure.
set -eu

: "${LOCAL_SOURCES:=$HOME/.polymarket_alpha/sources.yaml}"
: "${REMOTE_USER:?REMOTE_USER is required}"
: "${REMOTE_HOST:?REMOTE_HOST is required}"
: "${REMOTE_PATH:?REMOTE_PATH is required}"
: "${REMOTE_RESTART_CMD:=}"
: "${SSH_OPTS:=}"

if [ ! -f "$LOCAL_SOURCES" ]; then
    echo "ERROR: $LOCAL_SOURCES not found — run `polymarket_alpha refresh` first" >&2
    exit 2
fi

REMOTE="$REMOTE_USER@$REMOTE_HOST"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Fetch the current remote file (if any) for comparison.
# shellcheck disable=SC2086
ssh $SSH_OPTS "$REMOTE" "cat '$REMOTE_PATH'" > "$TMP" 2>/dev/null || true

if diff -q "$LOCAL_SOURCES" "$TMP" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) sources.yaml unchanged on $REMOTE_HOST; no action"
    exit 0
fi

echo "$(date -u +%FT%TZ) syncing $LOCAL_SOURCES -> $REMOTE:$REMOTE_PATH"
# shellcheck disable=SC2086
scp $SSH_OPTS "$LOCAL_SOURCES" "$REMOTE:$REMOTE_PATH"
echo "$(date -u +%FT%TZ) copy ok"

if [ -n "$REMOTE_RESTART_CMD" ]; then
    echo "$(date -u +%FT%TZ) running remote restart command"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$REMOTE" "$REMOTE_RESTART_CMD"
    echo "$(date -u +%FT%TZ) remote restart ok"
fi
