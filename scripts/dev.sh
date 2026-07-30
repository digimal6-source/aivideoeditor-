#!/usr/bin/env bash
# Start Clipforge.
#
# There is only one process to run: the backend serves both the JSON API and
# the static web interface, so there is no separate frontend build or dev
# server to keep alive.

set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

# Load .env if present, without clobbering variables already set in the shell.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export API_HOST="${API_HOST:-0.0.0.0}"
export API_PORT="${API_PORT:-8000}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  printf '\033[31mFFmpeg was not found on PATH.\033[0m Run ./scripts/setup.sh first.\n'
  exit 1
fi

mkdir -p data/uploads data/outputs data/jobs fonts

printf '\n\033[1mClipforge\033[0m\n'
printf '  API + web interface: http://localhost:%s\n' "$API_PORT"
if [ -n "${CODESPACE_NAME:-}" ]; then
  printf '  Codespaces URL:      https://%s-%s.%s\n' \
    "$CODESPACE_NAME" "$API_PORT" "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  printf '  (If the page will not load, set port %s to Public in the PORTS tab.)\n' "$API_PORT"
fi
printf '  Press Ctrl+C to stop.\n\n'

exec "$PY" -m app.main
