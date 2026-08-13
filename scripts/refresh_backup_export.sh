#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/arbitrage-hub/mysql}"
EXPORT_ROOT="${BACKUP_EXPORT_ROOT:-/srv/arbitrage-backup-export}"
EXPORT_GROUP="${BACKUP_EXPORT_GROUP:-arbbackup}"

SOURCE_BACKUP="$(readlink -f "${BACKUP_ROOT}/latest.sql.gz")"
SOURCE_CHECKSUM="${SOURCE_BACKUP}.sha256"

[[ "$SOURCE_BACKUP" == "${BACKUP_ROOT}/six-hour/"* ]] || {
  echo "Latest backup points outside the expected six-hour directory." >&2
  exit 2
}
for source_file in "$SOURCE_BACKUP" "$SOURCE_CHECKSUM"; do
  [[ -f "$source_file" && ! -L "$source_file" ]] || {
    echo "Required recovery file is missing: $source_file" >&2
    exit 2
  }
done

gzip -t "$SOURCE_BACKUP"
EXPECTED_BACKUP_HASH="$(awk 'NR==1 {print $1}' "$SOURCE_CHECKSUM")"
ACTUAL_BACKUP_HASH="$(sha256sum "$SOURCE_BACKUP" | awk '{print $1}')"
[[ "$EXPECTED_BACKUP_HASH" == "$ACTUAL_BACKUP_HASH" ]] || {
  echo "Latest backup checksum does not match." >&2
  exit 3
}
BACKUP_BASENAME="$(basename "$SOURCE_BACKUP")"
RELEASE_ID="${BACKUP_BASENAME#arbitrage_hub-}"
RELEASE_ID="${RELEASE_ID%.sql.gz}"
[[ "$RELEASE_ID" =~ ^[0-9]{8}-[0-9]{6}$ ]] || {
  echo "Unexpected backup release name: $BACKUP_BASENAME" >&2
  exit 4
}

RELEASES_ROOT="${EXPORT_ROOT}/releases"
FINAL_RELEASE="${RELEASES_ROOT}/${RELEASE_ID}"
TEMP_RELEASE="${RELEASES_ROOT}/.${RELEASE_ID}.partial"
install -d -m 755 -o root -g root "$EXPORT_ROOT"
install -d -m 750 -o root -g "$EXPORT_GROUP" "$RELEASES_ROOT"

if [[ ! -d "$FINAL_RELEASE" ]]; then
  rm -rf -- "$TEMP_RELEASE"
  install -d -m 750 -o root -g "$EXPORT_GROUP" "$TEMP_RELEASE"
  install -m 640 -o root -g "$EXPORT_GROUP" "$SOURCE_BACKUP" "$TEMP_RELEASE/arbitrage_hub.sql.gz"
  (
    cd "$TEMP_RELEASE"
    sha256sum arbitrage_hub.sql.gz > arbitrage_hub.sql.gz.sha256
  )
  chmod 640 "$TEMP_RELEASE"/*.sha256
  chown root:"$EXPORT_GROUP" "$TEMP_RELEASE"/*.sha256
  mv "$TEMP_RELEASE" "$FINAL_RELEASE"
fi

printf '%s\n' "$RELEASE_ID" > "${EXPORT_ROOT}/.LATEST.tmp"
chmod 640 "${EXPORT_ROOT}/.LATEST.tmp"
chown root:"$EXPORT_GROUP" "${EXPORT_ROOT}/.LATEST.tmp"
mv -f "${EXPORT_ROOT}/.LATEST.tmp" "${EXPORT_ROOT}/LATEST"

find "$RELEASES_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf -- {} +
echo "Read-only backup export refreshed: ${RELEASE_ID}"
