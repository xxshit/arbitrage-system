#!/usr/bin/env bash
set -Eeuo pipefail

# Install only our application-scoped drop-in; never overwrite a different one.
# Caller backs up the DB, schedules app restart and verifies/rolls back rollout.
test "$(id -u)" = 0
source_file="$(dirname "${BASH_SOURCE[0]}")/../deploy/arbitrage-allocator.conf"
target=/etc/systemd/system/arbitrage-private.service.d/60-allocator.conf
test -f "$source_file"
systemctl is-active --quiet arbitrage-private
if [[ -e "$target" ]]; then
  cmp "$source_file" "$target"
  echo 'Matching allocator configuration already present; no change.'
  exit 0
fi
if systemctl show arbitrage-private -p Environment --value | grep -q 'MALLOC_ARENA_MAX='; then
  echo 'Existing allocator environment detected; refusing to override.' >&2
  exit 1
fi
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?MALLOC_ARENA_MAX[[:space:]]*=' /opt/arbitrage-hub/.env; then
  echo 'Existing allocator setting in private environment; refusing to override.' >&2
  exit 1
fi
mkdir -p /etc/systemd/system/arbitrage-private.service.d
install -m 644 "$source_file" "$target"
systemctl daemon-reload
echo 'Allocator drop-in installed. App restart and runtime verification still required.'
