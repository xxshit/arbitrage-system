#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_HOSTNAME="${1:-}"

echo "LISTENERS"
ss -lntp | grep -E '(:80 |:443 |:3306 |:15831 )' || true

echo "NGINX"
command -v nginx || true
nginx -v 2>&1 || true
grep -Rhs '^[[:space:]]*server_name' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | head -30 || true

echo "CERTBOT"
command -v certbot || true
certbot --version 2>/dev/null || true

if [[ -n "$PUBLIC_HOSTNAME" ]]; then
  echo "DNS"
  getent ahostsv4 "$PUBLIC_HOSTNAME" | head -2 || true
fi

echo "DB_BIND"
ss -lntp | grep ':3306 ' || true

echo "DB_USERS_AND_SCHEMA_PRIVILEGES"
mariadb -N -e \
  "SELECT User,Host FROM mysql.user WHERE User IN ('arbi_hub','arbi') ORDER BY User,Host; SELECT GRANTEE,PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA='arbitrage_hub' ORDER BY GRANTEE,PRIVILEGE_TYPE;"
