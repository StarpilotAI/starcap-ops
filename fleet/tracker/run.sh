#!/usr/bin/env bash
# Open StarCap Tracker in the browser, starting it first if it isn't running.
#   ./run.sh                 -> http://127.0.0.1:8774
#   ./run.sh --no-browser    -> just serve it
#   ./run.sh --demo          -> the page on test fixtures, no database
#   STARCAP_TRACKER_PORT=9000 ./run.sh
# Normally the installed user service is already running it; then this only opens
# the page.
set -e
cd "$(dirname "$0")"

# One-shot commands (--sync-once, --rebuild, --summary ...) go straight through.
case " $* " in
  *" --sync-once "*|*" --rebuild "*|*" --summary"*|*" --help "*|*" -h "*)
    exec python3 -u tracker.py "$@" ;;
esac

PORT="${STARCAP_TRACKER_PORT:-8774}"
OPEN=1
ARGS=()
for a in "$@"; do
  if [ "$a" = "--no-browser" ]; then OPEN=0; else ARGS+=("$a"); fi
done
URL="http://127.0.0.1:$PORT"
[[ " ${ARGS[*]} " == *" --demo "* ]] && URL="$URL/?demo"

open_page() { setsid xdg-open "$URL" </dev/null >/dev/null 2>&1 || true; }

if curl -s --max-time 2 "http://127.0.0.1:$PORT/api/ping" 2>/dev/null | grep -q starcap-tracker; then
  echo "StarCap Tracker is already running on http://127.0.0.1:$PORT"
  [ "$OPEN" = 1 ] && open_page
  exit 0
fi

if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "port ${PORT} is held by something that is not StarCap Tracker."
  echo "Free it, or start on another port:  STARCAP_TRACKER_PORT=8775 $0"
  exit 1
fi

if [ "$OPEN" = 1 ]; then
  ( sleep 1; open_page ) &
  disown
fi
exec python3 -u tracker.py "${ARGS[@]}"
