#!/usr/bin/env bash
# opm-name launcher — starts the bulk-rename web UI and opens it in a browser.
# Needs: python3, sshpass, ssh, curl  (sudo apt install sshpass curl openssh-client)
# Needs UPSTASH_REDIS_REST_URL/TOKEN set (env, or ~/starcap-dashboard/dashboard/.env.local)
# for renaming to actually work — the table still shows docked devices without it.
set -e
cd "$(dirname "$0")"

for bin in python3 sshpass ssh curl; do
  command -v "$bin" >/dev/null || { echo "missing: $bin  (sudo apt install $bin)"; exit 1; }
done

PORT="${OPM_NAME_PORT:-8771}"

# Free the port first: stop any old opm-name instance still holding it, so a restart
# never fails with "Address already in use".
if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "port ${PORT} busy — stopping the old instance…"
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 1
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "  ⚠ port ${PORT} is STILL held — it was likely started with sudo. Run:"
    echo "        sudo fuser -k ${PORT}/tcp"
    echo "     then run this script again."
    exit 1
  fi
fi

( sleep 1; setsid xdg-open "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 || true ) &
disown
exec python3 webui.py
