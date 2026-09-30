#!/usr/bin/env bash
# opm-name installer — sets this tool up on a fresh Ubuntu laptop.
#
# Run it from inside the unzipped folder:
#
#     ./install.sh                 # app + dependencies + launcher
#     ./install.sh --network       # ...and set the wired NIC up for docking pis
#
# What it does:
#   1. installs the packages opm-name shells out to (sshpass, ssh, curl, ping,
#      and TigerVNC for the 🖥️ button)
#   2. copies the app to ~/.local/share/opm-name
#   3. adds an `opm-name` command, an app-menu entry, and a desktop icon
#   4. points renaming at a dashboard (nothing to answer unless it is not the usual
#      one — the defaults work as shipped) via ~/.config/opm-name/env
#   5. --network only: shares the wired NIC on 10.77.0.1/24 and raises the ARP
#      cache limits, so docked recorders get an IP and stop flickering in and out
#
# Steps 1 and 5 need sudo; it asks when it gets there. Re-running is safe.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/opm-name"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opm-name"
DESKTOP_DIR="$HOME/.local/share/applications"

DO_NETWORK=0
for arg in "$@"; do
  case "$arg" in
    --network) DO_NETWORK=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
say "-- [1/4] packages --"
# Checked by COMMAND, not package name: these arrive under different package
# names on different machines (the viewer especially), and what the tool actually
# needs is the binary on PATH. iputils-ping because discovery is a ping-sweep;
# sshpass is how every SSH here is made; curl is how Redis is written.
NEED_CMDS=(sshpass:sshpass ssh:openssh-client curl:curl ping:iputils-ping python3:python3)
MISSING=()
for pair in "${NEED_CMDS[@]}"; do
  command -v "${pair%%:*}" >/dev/null || MISSING+=("${pair#*:}")
done
if [ ${#MISSING[@]} -eq 0 ]; then
  echo "  already present: sshpass, ssh, curl, ping, python3"
elif command -v apt-get >/dev/null; then
  echo "  installing: ${MISSING[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${MISSING[@]}"
else
  echo "  ⚠ not a Debian/Ubuntu system — install these yourself: ${MISSING[*]}"
  exit 1
fi

# The VNC viewer is only for the 🖥️ button, so a laptop that can't get it still
# gets a working renamer — warn instead of failing the whole install.
if command -v xtigervncviewer >/dev/null; then
  echo "  already present: xtigervncviewer (the 🖥️ VNC button)"
elif command -v apt-get >/dev/null; then
  echo "  installing: tigervnc-viewer (for the 🖥️ VNC button)"
  sudo apt-get install -y tigervnc-viewer \
    || echo "  ⚠ couldn't install tigervnc-viewer — everything but the 🖥️ button will work"
fi

# ---------------------------------------------------------------------------
say "-- [2/4] installing the app to $APP_DIR --"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
# Deliberately file-by-file: never copy a board_registry.json (one station's own
# board log) or a __pycache__ from wherever this zip was built.
cp "$SRC/opmname.py" "$SRC/webui.py" "$SRC/start_opm_name.sh" "$APP_DIR/"
mkdir -p "$APP_DIR/web" "$APP_DIR/tools"
cp "$SRC/web/index.html" "$APP_DIR/web/"
chmod +x "$APP_DIR/start_opm_name.sh"
# tools/ in the zip, ../vnc/ when this is run straight out of the starcap-ops
# checkout. Only the 🖥️ button needs it, so its absence is a warning, not a stop.
VNC_SRC="$(ls "$SRC/tools/vnc_into_pi.sh" "$SRC/../vnc/vnc_into_pi.sh" 2>/dev/null | head -1 || true)"
if [ -n "$VNC_SRC" ]; then
  cp "$VNC_SRC" "$APP_DIR/tools/"
  chmod +x "$APP_DIR/tools/vnc_into_pi.sh"
else
  echo "  ⚠ vnc_into_pi.sh not found — everything but the 🖥️ VNC button will work"
fi
echo "  installed"

cat > "$BIN_DIR/opm-name" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/start_opm_name.sh" "\$@"
EOF
chmod +x "$BIN_DIR/opm-name"

DESKTOP_ENTRY="[Desktop Entry]
Type=Application
Name=opm-name (Fleet renamer)
Comment=Rename docked StarCap recorders (device + dashboard) as they appear over ethernet
Exec=bash -c \"$APP_DIR/start_opm_name.sh; echo; read -p 'press enter to close...'\"
Icon=system-users
Terminal=true
Categories=Utility;"
printf '%s\n' "$DESKTOP_ENTRY" > "$DESKTOP_DIR/opm-name.desktop"
chmod +x "$DESKTOP_DIR/opm-name.desktop"
# The desktop folder is localized (Bureau/Escritorio/...) — ask, don't assume.
USER_DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$USER_DESKTOP" ]; then
  printf '%s\n' "$DESKTOP_ENTRY" > "$USER_DESKTOP/opm-name.desktop"
  chmod +x "$USER_DESKTOP/opm-name.desktop"
  gio set "$USER_DESKTOP/opm-name.desktop" metadata::trusted true 2>/dev/null || true
  echo "  added: desktop icon, app-menu entry, and the \`opm-name\` command"
else
  echo "  added: app-menu entry and the \`opm-name\` command"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  note: $BIN_DIR isn't on your PATH — use the desktop icon, or add it to ~/.profile" ;;
esac

# ---------------------------------------------------------------------------
say "-- [3/4] dashboard --"
# Renaming is one POST to the dashboard (/api/ingest/starcap-name), which is the only
# thing allowed to write a name — see opmname.py's header for what happened when this
# tool wrote the store itself. Both the address and the station password have defaults
# that are correct for the fleet, so there is normally NOTHING to answer here: this step
# exists for a laptop that has to point at a different dashboard.
mkdir -p "$CFG_DIR"
if [ -s "$CFG_DIR/env" ] && grep -q UPSTASH_REDIS "$CFG_DIR/env"; then
  echo "  note: $CFG_DIR/env still holds UPSTASH_REDIS_* lines from an older install."
  echo "  They are no longer read (names go through the dashboard now) and can be deleted."
fi
if [ -n "${STARCAP_DASHBOARD_URL:-}" ] || [ -n "${STARCAP_PASSWORD:-}" ]; then
  : > "$CFG_DIR/env"
  [ -n "${STARCAP_DASHBOARD_URL:-}" ] && printf 'STARCAP_DASHBOARD_URL=%s\n' "$STARCAP_DASHBOARD_URL" >> "$CFG_DIR/env"
  [ -n "${STARCAP_PASSWORD:-}" ] && printf 'STARCAP_PASSWORD=%s\n' "$STARCAP_PASSWORD" >> "$CFG_DIR/env"
  chmod 600 "$CFG_DIR/env"
  echo "  saved from the environment → $CFG_DIR/env"
elif [ -s "$CFG_DIR/env" ] && grep -q STARCAP_ "$CFG_DIR/env"; then
  echo "  already configured ($CFG_DIR/env) — leaving it alone"
else
  echo "  using the defaults (https://starcap.dev + the station password) — renaming works as is."
  echo "  For a different dashboard, write $CFG_DIR/env:"
  echo "      STARCAP_DASHBOARD_URL=https://…"
  echo "      STARCAP_PASSWORD=…"
fi

# ---------------------------------------------------------------------------
say "-- [4/4] wired network --"
# One script sets up the dock port for every fleet tool: station/setup_dock_lan.sh
# in the starcap-ops checkout, or the copy make_zip.sh bundles next to this file.
DOCK_LAN="$(ls "$SRC/setup_dock_lan.sh" "$SRC/../../station/setup_dock_lan.sh" 2>/dev/null | head -1 || true)"
if [ "$DO_NETWORK" != 1 ]; then
  echo "  skipped (re-run with --network to set it up)."
  echo "  You need it if docked recorders never show up: they get their IP from"
  echo "  this laptop, so the wired port has to share 10.77.0.1/24."
elif [ -z "$DOCK_LAN" ]; then
  echo "  ⚠ setup_dock_lan.sh not found next to this installer: set it up by hand"
else
  sudo bash "$DOCK_LAN"
fi

say "done."
echo "Start it from the desktop icon, the app menu, or by running:  opm-name"
echo "It opens http://127.0.0.1:8771 — dock a recorder over ethernet and it appears."
