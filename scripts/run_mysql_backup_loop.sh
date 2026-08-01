#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_SCRIPT="${BACKUP_SCRIPT:-/opt/arbitrage-hub/scripts/backup_mysql.sh}"
INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-21600}"

while true; do
  if ! "$BACKUP_SCRIPT"; then
    echo "Database backup failed at $(date --iso-8601=seconds); retrying in 10 minutes." >&2
    sleep 600
    continue
  fi
  sleep "$INTERVAL_SECONDS"
done
