#!/usr/bin/env bash
# StarCap QC launcher: start the QC web app and open it in the browser.
#   ./run.sh                -> http://127.0.0.1:8772
#   ./run.sh --no-browser   -> just serve it
#   STARCAP_QC_PORT=9000 ./run.sh
set -e
cd "$(dirname "$0")"

for bin in python3 sshpass ssh ping; do
  command -v "$bin" >/dev/null || { echo "missing: $bin  (re-run ./install.sh)"; exit 1; }
done

PORT="${STARCAP_QC_PORT:-8772}"
OPEN=1
[ "${1:-}" = "--no-browser" ] && OPEN=0

# Already running (a second double-click): just bring the page up again. Killing it
# would abort any QC run in progress and leave a half-finished test take on a board.
if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/ping" 2>/dev/null | grep -q starcap-qc; then
  echo "StarCap QC is already running on http://127.0.0.1:$PORT"
  [ "$OPEN" = 1 ] && { setsid xdg-open "http://127.0.0.1:$PORT" </dev/null >/dev/null 2>&1 || true; }
  exit 0
fi

if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "port ${PORT} is held by something that is not StarCap QC."
  echo "Free it, or start on another port:  STARCAP_QC_PORT=8773 $0"
  exit 1
fi

if [ "$OPEN" = 1 ]; then
  ( sleep 1; setsid xdg-open "http://127.0.0.1:$PORT" </dev/null >/dev/null 2>&1 || true ) &
  disown
fi
exec python3 -u webui.py   # -u: log lines reach a terminal or file as they happen
