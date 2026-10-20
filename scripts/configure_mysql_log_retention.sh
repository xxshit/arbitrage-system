#!/usr/bin/env bash
# MySQL 8 only. No file deletion, database mutation, or service restart.
# Automatic expiration of BINLOGS is separate from SQL backup/history retention.
set -Eeuo pipefail
umask 077
mode="${1:---check}"
[[ "$mode" == --check || "$mode" == --apply ]] || exit 2
backup_root=/var/backups/arbitrage-hub/mysql
latest="$(readlink -f "$backup_root/latest.sql.gz")"
[[ "$latest" == "$backup_root"/six-hour/arbitrage_hub-*.sql.gz && -s "$latest" ]]
age=$(( $(date +%s) - $(stat -c %Y "$latest") ))
[[ "$age" -ge 0 && "$age" -le 28800 ]] || { echo 'Backup older than 8 hours; refusing policy change.' >&2; exit 1; }
sha256sum -c "$latest.sha256"
gzip -t "$latest"
cmp -s /opt/arbitrage-hub/.chat_encryption.key /var/backups/arbitrage-hub/secrets/chat-encryption.key
[[ "$(timeout 15 mysql -N -e 'SELECT VERSION();')" == 8.0.* ]]
# Offline/external replica inventories must also be reviewed by the operator.
replicas="$(timeout 15 mysql -N -e "SHOW REPLICAS; SHOW REPLICA STATUS; SELECT CHANNEL_NAME FROM performance_schema.replication_connection_configuration; SELECT PROCESSLIST_COMMAND FROM performance_schema.threads WHERE PROCESSLIST_COMMAND LIKE 'Binlog Dump%';")"
[[ -z "$replicas" ]] || { echo 'Replication dependency found; refusing policy change.' >&2; exit 1; }
[[ "$(timeout 15 mysql -N -e 'SELECT @@GLOBAL.expire_logs_days;')" == 0 ]]
if [[ "$mode" == --apply ]]; then
  timeout 15 mysql -e 'SET PERSIST binlog_expire_logs_seconds = 172800; SET PERSIST binlog_expire_logs_auto_purge = ON;'
fi
timeout 15 mysql -e 'SELECT @@GLOBAL.binlog_expire_logs_seconds AS binlog_retention_seconds, @@GLOBAL.binlog_expire_logs_auto_purge AS auto_purge;'
echo 'SQL backup retention and business/history tables unchanged.'
