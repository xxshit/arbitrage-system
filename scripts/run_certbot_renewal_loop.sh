#!/usr/bin/env bash
set -Eeuo pipefail

INTERVAL_SECONDS="${CERTBOT_RENEW_INTERVAL_SECONDS:-43200}"

while true; do
  certbot renew --quiet --deploy-hook "nginx -t && nginx -s reload" || true
  sleep "$INTERVAL_SECONDS"
done
