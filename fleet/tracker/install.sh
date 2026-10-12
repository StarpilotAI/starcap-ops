#!/usr/bin/env bash
# StarCap Tracker: daily fleet, calibration and maintenance tracker (always-on service).
#
#   ./install.sh              install, or re-install after moving the checkout
#   ./install.sh --no-service launcher + command only; run it by hand
#   ./install.sh remove       take the launcher, command and service back off
#                             (the cache in data/ and your notes are left alone)
#
# It runs as a user service, not just a window, because it has work to do when no
# page is open: it syncs every 10 minutes and writes yesterday's summary into the
# Slack drafts file at 09:00. The unit and launchers need absolute paths, so they
# are generated for wherever this folder lives now.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== starcap-tracker"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="starcap-tracker.service"

if [ "${1:-}" = "remove" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" "$HOME/.local/bin/starcap-tracker" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/applications/starcap-tracker.desktop" \
        "$(_desktop_dir)/starcap-tracker.desktop"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "  removed (data/ kept)."
  exit 0
fi

need_cmd python3     python3
need_py  psycopg2    python3-psycopg2   # sources.py: read-only Postgres
need_cmd curl        curl               # run.sh: is it already running?
need_cmd notify-send libnotify-bin      # the 09:00 "summary written" toast
install_deps
chmod +x "$HERE/run.sh" "$HERE/tracker.py"

# The dashboard's credentials are read where they already live, never copied.
found=""
for f in "${STARCAP_TRACKER_ENV:-}" "$HOME/starcap-dashboard/env.local" \
         "$HOME/starcap-dashboard/.env.local" "$HOME/Desktop/env.local"; do
  [ -n "$f" ] && [ -f "$f" ] && { found="$f"; break; }
done
if [ -n "$found" ]; then
  echo "  credentials: $found"
else
  echo "  !! no dashboard env file found (DATABASE_URL + UPSTASH_REDIS_REST_*): it will start but cannot sync"
fi

launcher starcap-tracker "StarCap Tracker" "$HERE/run.sh" "$HERE/icon.png" \
  "How the StarCap fleet did yesterday: hours, timing, calibration, maintenance"
link_cmd starcap-tracker "$HERE/run.sh"

if [ "${1:-}" != "--no-service" ] && command -v systemctl >/dev/null; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=StarCap Tracker: daily fleet tracker (http://127.0.0.1:8774)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 -u $HERE/tracker.py
WorkingDirectory=$HERE
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null 2>&1
  systemctl --user restart "$UNIT"
  echo "  service: $UNIT (running from $HERE)"
fi
