#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

DEPLOY_HOST="${ARBISCOPE_DEPLOY_HOST:-5.61.208.92}"
DEPLOY_PORT="${ARBISCOPE_DEPLOY_PORT:-16206}"
DEPLOY_USER="${ARBISCOPE_DEPLOY_USER:-arbdeploy}"
DEPLOY_KEY="${ARBISCOPE_DEPLOY_KEY:-$HOME/.ssh/arbitrage_deploy_mac}"
REMOTE_STAGE="/home/arbdeploy/arbiscope-release"
PUBLIC_URL="https://arbi-k7m4p2.5-61-208-92.sslip.io:18443/login"

if [[ ! -f "$DEPLOY_KEY" ]]; then
  echo "Deployment key not found: $DEPLOY_KEY" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain=v1)" ]]; then
  echo "The working tree is not clean. Commit or preserve changes before deployment." >&2
  exit 3
fi

git fetch origin master
[[ "$(git branch --show-current)" == "master" ]] || {
  echo "Deployment is only allowed from master." >&2
  exit 3
}
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/master)" ]] || {
  echo "Local master and origin/master are not identical." >&2
  exit 3
}

PYTHON="$REPOSITORY_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || {
  echo "Virtual environment is missing. Create .venv and install requirements first." >&2
  exit 4
}

"$PYTHON" -m unittest discover -s tests
if command -v node >/dev/null 2>&1; then
  node --check static/app.js
fi

SSH_OPTIONS=(
  -i "$DEPLOY_KEY"
  -p "$DEPLOY_PORT"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
)
SCP_OPTIONS=(
  -i "$DEPLOY_KEY"
  -P "$DEPLOY_PORT"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
)
TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

ssh "${SSH_OPTIONS[@]}" "$TARGET" "mkdir -p '$REMOTE_STAGE/static' '$REMOTE_STAGE/templates'"
scp "${SCP_OPTIONS[@]}" app.py "$TARGET:$REMOTE_STAGE/app.py"
scp "${SCP_OPTIONS[@]}" static/app.js "$TARGET:$REMOTE_STAGE/static/app.js"
scp "${SCP_OPTIONS[@]}" static/style.css "$TARGET:$REMOTE_STAGE/static/style.css"
scp "${SCP_OPTIONS[@]}" templates/index.html "$TARGET:$REMOTE_STAGE/templates/index.html"
scp "${SCP_OPTIONS[@]}" templates/auth.html "$TARGET:$REMOTE_STAGE/templates/auth.html"

ssh "${SSH_OPTIONS[@]}" "$TARGET" "sudo /usr/local/sbin/arbitrage-deploy-staged"
ssh "${SSH_OPTIONS[@]}" "$TARGET" "sudo /usr/local/sbin/arbitrage-verify-runtime"
curl --fail --silent --show-error --output /dev/null --write-out 'PUBLIC_HTTPS=%{http_code}\n' "$PUBLIC_URL"

echo "Deployment and runtime verification completed from $(git rev-parse --short HEAD)."
