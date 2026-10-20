#!/usr/bin/env bash
# Native lossless compression for NEW transactions. Never rewrite old binlogs.
set -Eeuo pipefail
umask 077
mode="${1:---check}"
[[ "$mode" == --check || "$mode" == --apply ]] || exit 2
# Includes verified recent backup/key and replication-dependency checks. This
# invokes only check mode; it does not change the existing 48-hour retention.
bash "$(dirname "$0")/configure_mysql_log_retention.sh" --check
version="$(timeout 15 mysql -N -e 'SELECT VERSION();')"
[[ "$version" =~ ^8\.0\.([0-9]+) ]] && [[ "${BASH_REMATCH[1]}" -ge 20 ]]
reader_version="$(mysqlbinlog --version)"
[[ "$reader_version" =~ Ver\ 8\.0\.([0-9]+) ]] && [[ "${BASH_REMATCH[1]}" -ge 20 ]]
[[ "$(timeout 15 mysql -N -e 'SELECT @@GLOBAL.binlog_format, @@GLOBAL.binlog_row_image, @@GLOBAL.sync_binlog, @@GLOBAL.innodb_flush_log_at_trx_commit;')" == $'ROW\tFULL\t1\t1' ]]
[[ "$(timeout 15 mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='arbitrage_hub' AND table_type='BASE TABLE' AND ENGINE <> 'InnoDB';")" == 0 ]]
if [[ "$mode" == --apply ]]; then
  timeout 15 mysql -e 'SET PERSIST binlog_transaction_compression_level_zstd = 3; SET PERSIST binlog_transaction_compression = ON;'
fi
timeout 15 mysql -e 'SELECT @@GLOBAL.binlog_transaction_compression AS compression, @@GLOBAL.binlog_transaction_compression_level_zstd AS zstd_level; SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.persisted_variables WHERE VARIABLE_NAME IN ("binlog_transaction_compression", "binlog_transaction_compression_level_zstd");'
echo 'New connections inherit compression; reconnect only the arbitrage app during deployment. Existing log files and all business data unchanged.'
