#!/usr/bin/env bash
set -Eeuo pipefail

latest_snapshot() {
  mariadb -N <<'SQL'
SELECT MAX(captured_at) FROM arbitrage_hub.latest_market_snapshot;
SQL
}

BEFORE="$(latest_snapshot)"
sleep 7
AFTER="$(latest_snapshot)"
echo "WRITE_BEFORE=${BEFORE}"
echo "WRITE_AFTER=${AFTER}"

echo "PRIVATE_LISTENERS"
ss -lntp | grep -E ':(3306|15831) '

echo "PUBLIC_LISTENER"
ss -lntp | grep ':18443 '

echo "APP_LOG_ERRORS"
pm2 logs arbitrage-private --err --lines 15 --nostream
