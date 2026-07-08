#!/usr/bin/env bash
# opm-lan launcher — starts the web UI and opens it in a browser.
# Needs: python3, sshpass, rsync, ssh  (sudo apt install sshpass rsync openssh-client)
# Optional: ffmpeg + opencv-python-headless + numpy on THIS PC for contact sheets
# (the recorders' own ffmpeg is used for thumbnails/previews).
set -e
cd "$(dirname "$0")"

for bin in python3 sshpass rsync ssh; do
  command -v "$bin" >/dev/null || { echo "missing: $bin  (sudo apt install $bin)"; exit 1; }
done

PORT=$(python3 - <<'PY' 2>/dev/null || echo 8770
try:
    import yaml; print(yaml.safe_load(open("config.yaml")).get("port", 8770))
except Exception:
    print(8770)
PY
)
PORT=${OPM_LAN_PORT:-$PORT}

# Free the port first: stop any old opm-lan instance still holding it, so a restart
# never fails with "Address already in use".
if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "port ${PORT} busy — stopping the old instance…"
  fuser -k "${PORT}/tcp" 2>/dev/null || true
  sleep 1
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    # still held — almost always an instance started with sudo (root-owned; your user
    # can't kill it). Clear it once with sudo, then re-run this script.
    echo "  ⚠ port ${PORT} is STILL held — it was likely started with sudo. Run:"
    echo "        sudo fuser -k ${PORT}/tcp"
    echo "     then run this script again."
    exit 1
  fi
fi

# setsid + full fd detachment: if Firefox wasn't already running, xdg-open's browser
# becomes a real child of this terminal's session, so closing the terminal sends it
# SIGHUP and Firefox quits ENTIRELY (every window/tab) — not just this one.
( sleep 1; setsid xdg-open "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 || true ) &
disown
exec python3 webui.py
