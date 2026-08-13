#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

BACKUP_HOST="${ARBISCOPE_BACKUP_HOST:-5.61.208.92}"
BACKUP_PORT="${ARBISCOPE_BACKUP_PORT:-16206}"
BACKUP_USER="${ARBISCOPE_BACKUP_USER:-arbbackup}"
BACKUP_KEY="${ARBISCOPE_BACKUP_KEY:-$HOME/.ssh/arbitrage_backup_mac}"
RETENTION_DAYS="${ARBISCOPE_BACKUP_RETENTION_DAYS:-180}"
MYSQL_ROOT="$REPOSITORY_ROOT/backups/cloud-mysql"

[[ -f "$BACKUP_KEY" ]] || {
  echo "Read-only backup key not found: $BACKUP_KEY" >&2
  exit 2
}
mkdir -p "$MYSQL_ROOT"
chmod 700 "$MYSQL_ROOT"
TEMP_ROOT="$(mktemp -d "$MYSQL_ROOT/.download.XXXXXX")"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT

SFTP_OPTIONS=(
  -i "$BACKUP_KEY"
  -P "$BACKUP_PORT"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
)
TARGET="${BACKUP_USER}@${BACKUP_HOST}"

printf 'get LATEST %s/LATEST\n' "$TEMP_ROOT" | sftp "${SFTP_OPTIONS[@]}" -b - "$TARGET"
RELEASE_ID="$(tr -d '\r\n' < "$TEMP_ROOT/LATEST")"
[[ "$RELEASE_ID" =~ ^[0-9]{8}-[0-9]{6}$ ]] || {
  echo "Server returned an invalid backup release identifier." >&2
  exit 3
}

cat > "$TEMP_ROOT/download.batch" <<EOF
get releases/$RELEASE_ID/arbitrage_hub.sql.gz $TEMP_ROOT/arbitrage_hub.sql.gz
get releases/$RELEASE_ID/arbitrage_hub.sql.gz.sha256 $TEMP_ROOT/arbitrage_hub.sql.gz.sha256
EOF
sftp "${SFTP_OPTIONS[@]}" -b "$TEMP_ROOT/download.batch" "$TARGET"

verify_sha256() {
  local file_path="$1"
  local checksum_path="$2"
  local expected actual
  expected="$(awk 'NR==1 {print $1}' "$checksum_path")"
  actual="$(shasum -a 256 "$file_path" | awk '{print $1}')"
  [[ "$expected" == "$actual" ]] || {
    echo "Checksum verification failed for $(basename "$file_path")." >&2
    exit 4
  }
}

verify_sha256 "$TEMP_ROOT/arbitrage_hub.sql.gz" "$TEMP_ROOT/arbitrage_hub.sql.gz.sha256"
gzip -t "$TEMP_ROOT/arbitrage_hub.sql.gz"

LOCAL_BACKUP="$MYSQL_ROOT/arbitrage_hub-$RELEASE_ID.sql.gz"
LOCAL_BACKUP_CHECKSUM="${LOCAL_BACKUP}.sha256"
install -m 600 "$TEMP_ROOT/arbitrage_hub.sql.gz" "$LOCAL_BACKUP"
install -m 600 "$TEMP_ROOT/arbitrage_hub.sql.gz.sha256" "$LOCAL_BACKUP_CHECKSUM"

find "$MYSQL_ROOT" -type f -name 'arbitrage_hub-*.sql.gz*' -mtime "+$RETENTION_DAYS" -delete

SIZE_MB="$(du -m "$LOCAL_BACKUP" | awk '{print $1}')"
echo "Verified read-only cloud backup: $LOCAL_BACKUP (${SIZE_MB} MB)"
echo "Database-only backup downloaded. Encrypted chat cannot be recovered without the separately protected chat key."
