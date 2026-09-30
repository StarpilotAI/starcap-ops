#!/usr/bin/env bash
# StarCap Finder installer: launcher + an always-on user service.
#
#   ./install.sh              install, or re-install after moving the checkout
#   ./install.sh --no-service launcher only, run it by hand
#   ./install.sh remove       take the launcher and the service back off
#
# The .desktop spec and the systemd unit both need ABSOLUTE paths, so they are
# generated for wherever this folder lives now. That is why you run this rather
# than copying a prebuilt .desktop around: a stale one silently runs an old copy.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== starcapfinder"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="starcapfinder.service"

if [ "${1:-}" = "remove" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/applications/starcapfinder.desktop" \
        "$(_desktop_dir)/starcapfinder.desktop"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "  removed."
  exit 0
fi

need_cmd python3 python3
need_cmd sshpass sshpass     # the buzzer / WB toggles ssh into boards
need_cmd ssh     openssh-client
install_deps
chmod +x "$HERE/run.sh" "$HERE/starcapfinder.py"

launcher starcapfinder "StarCap Finder" "$HERE/run.sh" "$HERE/icon.png" \
  "Find every StarCap on ethernet and open its dashboard"

if [ "${1:-}" != "--no-service" ] && command -v systemctl >/dev/null; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=StarCap Finder — LAN roll-call dashboard (http://127.0.0.1:8090)
After=network-online.target

[Service]
# --no-browser: the unit restarts on failure, and each restart would otherwise
# open another browser tab.
ExecStart=/usr/bin/python3 $HERE/starcapfinder.py --no-browser
WorkingDirectory=$HERE
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null 2>&1
  systemctl --user restart "$UNIT"
  echo "  service: $UNIT (running from $HERE)"
fi
