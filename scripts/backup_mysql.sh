#!/usr/bin/env bash
set -Eeuo pipefail

# MySQL/MariaDB hot backup for the arbitrage service.
# Run this as a user that can read the database (root uses the local socket on
# the current private deployment). Secrets are deliberately not written here.

umask 077

DATABASE_NAME="${BACKUP_DATABASE_NAME:-arbitrage_hub}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/arbitrage-hub/mysql}"
SIX_HOUR_DIR="${BACKUP_ROOT}/six-hour"
DAILY_DIR="${BACKUP_ROOT}/daily"
LOCK_FILE="${BACKUP_ROOT}/.backup.lock"

mkdir -p "$SIX_HOUR_DIR" "$DAILY_DIR"
chmod 700 "$BACKUP_ROOT" "$SIX_HOUR_DIR" "$DAILY_DIR"

if command -v mariadb-dump >/dev/null 2>&1; then
  DUMP_BIN="$(command -v mariadb-dump)"
elif command -v mysqldump >/dev/null 2>&1; then
  DUMP_BIN="$(command -v mysqldump)"
else
  echo "mariadb-dump or mysqldump is required" >&2
  exit 1
fi

STAMP="$(date '+%Y%m%d-%H%M%S')"
DAY="$(date '+%Y%m%d')"
FILE_NAME="${DATABASE_NAME}-${STAMP}.sql.gz"
FINAL_PATH="${SIX_HOUR_DIR}/${FILE_NAME}"
TEMP_PATH="${FINAL_PATH}.partial"
CHECKSUM_PATH="${FINAL_PATH}.sha256"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "A database backup is already running; skipping this cycle."
  exit 0
fi

cleanup() {
  rm -f "$TEMP_PATH"
}
trap cleanup EXIT

DUMP_ARGS=(
  --single-transaction
  --quick
  --routines
  --events
  --triggers
  --hex-blob
  --default-character-set=utf8mb4
  --databases "$DATABASE_NAME"
)

if [[ -n "${MYSQL_DEFAULTS_FILE:-}" ]]; then
  DUMP_ARGS=("--defaults-extra-file=${MYSQL_DEFAULTS_FILE}" "${DUMP_ARGS[@]}")
fi

"$DUMP_BIN" "${DUMP_ARGS[@]}" | gzip -9 > "$TEMP_PATH"
gzip -t "$TEMP_PATH"
mv "$TEMP_PATH" "$FINAL_PATH"
sha256sum "$FINAL_PATH" > "$CHECKSUM_PATH"

# The latest pointer gives the local pull script a stable, atomic target.
ln -sfn "$FINAL_PATH" "${BACKUP_ROOT}/latest.sql.gz"
ln -sfn "$CHECKSUM_PATH" "${BACKUP_ROOT}/latest.sql.gz.sha256"

# Preserve one independent copy per calendar day for longer retention.
DAILY_PATH="${DAILY_DIR}/${DATABASE_NAME}-${DAY}.sql.gz"
if [[ ! -e "$DAILY_PATH" ]]; then
  cp --reflink=auto "$FINAL_PATH" "$DAILY_PATH"
  sha256sum "$DAILY_PATH" > "${DAILY_PATH}.sha256"
fi

# Four backups per day for 14 days, plus one daily backup for 90 days.
find "$SIX_HOUR_DIR" -type f -name "${DATABASE_NAME}-*.sql.gz" -mtime +14 -delete
find "$SIX_HOUR_DIR" -type f -name "${DATABASE_NAME}-*.sql.gz.sha256" -mtime +14 -delete
find "$DAILY_DIR" -type f -name "${DATABASE_NAME}-*.sql.gz" -mtime +90 -delete
find "$DAILY_DIR" -type f -name "${DATABASE_NAME}-*.sql.gz.sha256" -mtime +90 -delete

SIZE="$(du -h "$FINAL_PATH" | awk '{print $1}')"
echo "Backup completed: ${FINAL_PATH} (${SIZE})"
