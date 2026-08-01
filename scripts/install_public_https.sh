#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_HOSTNAME="${1:-}"
PUBLIC_HTTPS_PORT="${2:-18443}"
UPSTREAM="${ARBITRAGE_UPSTREAM:-127.0.0.1:15831}"
SITE_NAME="arbitrage-public"
WEBROOT="/var/www/arbitrage-letsencrypt"

if [[ -z "$PUBLIC_HOSTNAME" ]]; then
  echo "Usage: $0 PUBLIC_HOSTNAME [HTTPS_PORT]" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 2
fi
command -v nginx >/dev/null
command -v certbot >/dev/null

install -d -m 755 "$WEBROOT/.well-known/acme-challenge"

cat > "/etc/nginx/sites-available/${SITE_NAME}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${PUBLIC_HOSTNAME};

    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type text/plain;
    }

    location / {
        return 404;
    }
}
EOF
ln -sfn "/etc/nginx/sites-available/${SITE_NAME}" "/etc/nginx/sites-enabled/${SITE_NAME}"
nginx -t
nginx -s reload

certbot certonly --webroot -w "$WEBROOT" \
  -d "$PUBLIC_HOSTNAME" \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --keep-until-expiring

cat > /etc/nginx/conf.d/arbitrage-rate-limit.conf <<'EOF'
limit_req_zone $binary_remote_addr zone=arbitrage_login:10m rate=6r/m;
limit_req_zone $binary_remote_addr zone=arbitrage_register:10m rate=2r/m;
EOF

cat > "/etc/nginx/sites-available/${SITE_NAME}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${PUBLIC_HOSTNAME};

    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type text/plain;
    }

    location / {
        return 308 https://\$host:${PUBLIC_HTTPS_PORT}\$request_uri;
    }
}

server {
    listen ${PUBLIC_HTTPS_PORT} ssl http2;
    listen [::]:${PUBLIC_HTTPS_PORT} ssl http2;
    server_name ${PUBLIC_HOSTNAME};

    ssl_certificate /etc/letsencrypt/live/${PUBLIC_HOSTNAME}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${PUBLIC_HOSTNAME}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:ARBITRAGE_TLS:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    server_tokens off;
    client_max_body_size 1m;
    limit_req_status 429;

    location = /api/auth/login {
        limit_req zone=arbitrage_login burst=4 nodelay;
        proxy_pass http://${UPSTREAM};
        include /etc/nginx/snippets/arbitrage-proxy.conf;
    }

    location = /api/auth/register {
        limit_req zone=arbitrage_register burst=2 nodelay;
        proxy_pass http://${UPSTREAM};
        include /etc/nginx/snippets/arbitrage-proxy.conf;
    }

    location / {
        proxy_pass http://${UPSTREAM};
        include /etc/nginx/snippets/arbitrage-proxy.conf;
    }
}
EOF

cat > /etc/nginx/snippets/arbitrage-proxy.conf <<'EOF'
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Connection "";
proxy_connect_timeout 10s;
proxy_send_timeout 90s;
proxy_read_timeout 90s;
EOF

nginx -t
nginx -s reload
echo "Public HTTPS enabled: https://${PUBLIC_HOSTNAME}:${PUBLIC_HTTPS_PORT}"
