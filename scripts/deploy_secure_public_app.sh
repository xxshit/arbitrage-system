#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${ARBITRAGE_APP_ROOT:-/opt/arbitrage-hub}"

"${APP_ROOT}/scripts/backup_mysql.sh"

install -m 640 -o arbhub -g arbhub /tmp/arbitrage-app.py "${APP_ROOT}/app.py"
install -m 640 -o arbhub -g arbhub /tmp/arbitrage-app.js "${APP_ROOT}/static/app.js"
install -m 640 -o arbhub -g arbhub /tmp/arbitrage-style.css "${APP_ROOT}/static/style.css"
install -m 640 -o arbhub -g arbhub /tmp/arbitrage-index.html "${APP_ROOT}/templates/index.html"
rm -f /tmp/arbitrage-app.py /tmp/arbitrage-app.js /tmp/arbitrage-style.css /tmp/arbitrage-index.html

if grep -q '^SESSION_COOKIE_SECURE=' "${APP_ROOT}/.env"; then
  sed -i 's/^SESSION_COOKIE_SECURE=.*/SESSION_COOKIE_SECURE=1/' "${APP_ROOT}/.env"
else
  printf '\nSESSION_COOKIE_SECURE=1\n' >> "${APP_ROOT}/.env"
fi
chmod 600 "${APP_ROOT}/.env"
chown arbhub:arbhub "${APP_ROOT}/.env"

sudo -u arbhub "${APP_ROOT}/.venv/bin/python" -m py_compile "${APP_ROOT}/app.py"

mariadb <<'SQL'
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'arbi_hub'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES
  ON arbitrage_hub.* TO 'arbi_hub'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

pm2 restart arbitrage-private --update-env
sleep 8
curl -fsS -o /dev/null -w 'LOCAL_HTTP=%{http_code}\n' http://127.0.0.1:15831/login

echo "DB_BIND"
ss -lntp | grep ':3306 '

echo "DB_GRANTS"
mariadb -N <<'SQL'
SELECT PRIVILEGE_TYPE
FROM information_schema.SCHEMA_PRIVILEGES
WHERE TABLE_SCHEMA='arbitrage_hub'
  AND GRANTEE='\'arbi_hub\'@\'127.0.0.1\''
ORDER BY PRIVILEGE_TYPE;
SQL
