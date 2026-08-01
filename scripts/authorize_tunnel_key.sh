#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_KEY_FILE="${1:-}"
DEVICE_LABEL="${2:-device}"
TUNNEL_USER="${TUNNEL_USER:-arbtunnel}"
PERMITTED_DESTINATION="${PERMITTED_DESTINATION:-127.0.0.1:15831}"

if [[ -z "$PUBLIC_KEY_FILE" || ! -f "$PUBLIC_KEY_FILE" ]]; then
  echo "Usage: $0 PUBLIC_KEY_FILE [DEVICE_LABEL]" >&2
  exit 2
fi

read -r KEY_TYPE KEY_DATA _ < "$PUBLIC_KEY_FILE"
if [[ "$KEY_TYPE" != "ssh-ed25519" || -z "$KEY_DATA" ]]; then
  echo "Only a valid ssh-ed25519 public key is accepted." >&2
  exit 2
fi

if ! id "$TUNNEL_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /usr/sbin/nologin "$TUNNEL_USER"
else
  usermod --shell /usr/sbin/nologin "$TUNNEL_USER"
fi

HOME_DIR="$(getent passwd "$TUNNEL_USER" | cut -d: -f6)"
SSH_DIR="${HOME_DIR}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
install -d -m 700 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "$SSH_DIR"
touch "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"
chown "$TUNNEL_USER:$TUNNEL_USER" "$AUTHORIZED_KEYS"

if ! grep -Fq "$KEY_DATA" "$AUTHORIZED_KEYS"; then
  printf 'restrict,port-forwarding,permitopen="%s" %s %s arbitrage-hub:%s\n' \
    "$PERMITTED_DESTINATION" "$KEY_TYPE" "$KEY_DATA" "$DEVICE_LABEL" >> "$AUTHORIZED_KEYS"
fi

echo "Authorized ${DEVICE_LABEL} for ${TUNNEL_USER} -> ${PERMITTED_DESTINATION}."
