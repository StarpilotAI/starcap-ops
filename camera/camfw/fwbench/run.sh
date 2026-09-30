#!/usr/bin/env bash
# Firmware jitter bench — start the browser front end.
#
# Reuses an already-running instance rather than failing on a busy port: this is
# double-clicked, and a second click must not look broken.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
PORT="${PORT:-8092}"
URL="http://127.0.0.1:$PORT"
if curl -sf -o /dev/null --max-time 2 "$URL/api/state" 2>/dev/null; then
    echo "already running -> $URL"
    xdg-open "$URL" >/dev/null 2>&1 &
    exit 0
fi
exec python3 ui.py "$@"
