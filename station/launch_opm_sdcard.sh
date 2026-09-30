#!/usr/bin/env bash
# launch_opm_sdcard.sh — desktop-shortcut launcher for opm (the SD-card copier in
# the separate orangepi-manager repo). Runs its own start.sh (installs deps,
# self-elevates via sudo, runs the web UI in a restart loop) and opens the browser
# once the server actually answers — start.sh can take a while on a first run
# (apt install + venv/pip setup), so a fixed short delay isn't reliable.
set -uo pipefail

OPM_DIR="${STARCAP_DASHBOARD:-$HOME/starcap-dashboard}/orangepi-manager"
PORT="${OPM_PORT:-8765}"

cd "$OPM_DIR" || { echo "missing $OPM_DIR"; read -rp "press enter to close..."; exit 1; }

( for _ in $(seq 1 90); do
    if curl -s -o /dev/null "http://127.0.0.1:$PORT"; then
      # setsid + full fd detachment: xdg-open's browser (if Firefox wasn't already
      # running) otherwise inherits this terminal's controlling tty, so closing the
      # terminal sends it SIGHUP and Firefox quits ENTIRELY — every window/tab, not
      # just this one. Detaching keeps it alive independent of this terminal.
      setsid xdg-open "http://127.0.0.1:$PORT" < /dev/null > /dev/null 2>&1 &
      break
    fi
    sleep 1
  done ) &
disown

./start.sh
echo
read -rp "press enter to close..."
