#!/usr/bin/env bash
# start_csv_viewer.sh — live-refreshing local web view of pi_emmc_speeds.csv (or
# any CSV passed as an argument), so it stays up to date without ever needing to
# close/reopen it in a spreadsheet app.
set -e
cd "$(dirname "$0")"

PORT="${CSV_VIEWER_PORT:-8772}"

if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "port ${PORT} busy — stopping the old instance…"
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 1
fi

( sleep 1; setsid xdg-open "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 || true ) &
disown
exec python3 csv_viewer.py "$@"
