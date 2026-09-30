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
# tools/ in the zip, ../tools/ when this is run straight out of the starcap-ops
# checkout. Only the 🖥️ button needs it, so its absence is a warning, not a stop.
VNC_SRC="$(ls "$SRC/tools/vnc_into_pi.sh" "$SRC/../tools/vnc_into_pi.sh" 2>/dev/null | head -1 || true)"
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
if [ "$DO_NETWORK" != 1 ]; then
  echo "  skipped (re-run with --network to set it up)."
  echo "  You need it if docked recorders never show up: they get their IP from"
  echo "  this laptop, so the wired NIC has to be sharing a subnet. Note it must"
  echo "  NOT be 10.42.0.1/24 — that's the recorders' own hotspot subnet, and the"
  echo "  address collision stops this laptop resolving them."
elif ! command -v nmcli >/dev/null; then
  echo "  ⚠ no nmcli (NetworkManager) — set the wired NIC to 10.77.0.1/24 shared by hand."
else
  WIRED_IFACE="$(ip -br link show | awk '{print $1}' | grep -Ev '^(lo|wl|wwan|docker|veth|virbr|br-)' | head -n1)"
  if [ -z "$WIRED_IFACE" ]; then
    echo "  ⚠ couldn't find a wired interface — plug in the ethernet adapter and re-run."
  else
    echo "  wired interface: $WIRED_IFACE"
    CONN_NAME=opm-lan     # same profile name every station uses, so the recovery
                          # command (`nmcli connection up opm-lan`) is universal
    EXISTING="$(nmcli -t -f NAME,DEVICE connection show | awk -F: -v d="$WIRED_IFACE" '$2==d{print $1; exit}')"
    [ -n "$EXISTING" ] || EXISTING="$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
    if [ -z "$EXISTING" ]; then
      echo "  creating profile '$CONN_NAME'"
      sudo nmcli connection add type ethernet ifname "$WIRED_IFACE" con-name "$CONN_NAME"
    elif [ "$EXISTING" != "$CONN_NAME" ]; then
      # rename rather than add a second profile — two profiles on one NIC race
      echo "  renaming existing profile '$EXISTING' → '$CONN_NAME'"
      sudo nmcli connection modify "$EXISTING" connection.id "$CONN_NAME"
    fi
    sudo nmcli connection modify "$CONN_NAME" connection.interface-name "$WIRED_IFACE" \
                                              ipv4.method shared ipv4.addresses 10.77.0.1/24 \
                                              connection.autoconnect yes \
                                              connection.autoconnect-priority 100

    # Make sure NOTHING else grabs this NIC. Docking a recorder bounces the
    # carrier, and on every bounce NetworkManager re-picks the highest-priority
    # autoconnect profile for the device. If that's a plain DHCP-CLIENT profile
    # (the distro default, priority 0), the shared subnet disappears along with
    # its dnsmasq — so the recorder you just plugged in can never get an IP and
    # simply never appears, while the tool looks broken. Seen in the wild: a
    # laptop where the dock profile sat at priority -999 and lost every time.
    # Ours is pinned to 100 above; anything else ethernet gets its autoconnect
    # turned off (reversible: nmcli connection modify "<name>" connection.autoconnect yes).
    while read -r other; do
      [ -n "$other" ] && [ "$other" != "$CONN_NAME" ] || continue
      echo "  demoting '$other' — it would grab $WIRED_IFACE back on every replug"
      sudo nmcli connection modify "$other" connection.autoconnect no || true
    done < <(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1}')

    sudo nmcli connection down "$CONN_NAME" >/dev/null 2>&1 || true
    sudo nmcli connection up "$CONN_NAME" >/dev/null 2>&1 \
      || echo "  (bring-up failed — dock a recorder, then: nmcli connection up $CONN_NAME)"
    echo "  $WIRED_IFACE → 10.77.0.1/24 shared, and it now wins on every replug"
    ACTIVE_NOW="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$WIRED_IFACE" '$2==d{print $1}')"
    echo "  active on $WIRED_IFACE: ${ACTIVE_NOW:-none yet} ($(ip -br addr show "$WIRED_IFACE" | awk '{print $3}'))"

    # Discovery ping-sweeps the whole /24 every pass. The default ARP table caps
    # at gc_thresh1=128, so a sweep floods it and the kernel evicts LIVE boards —
    # that's the "recorder shows up, then vanishes, then comes back" symptom.
    sudo tee /etc/sysctl.d/99-arp-cache.conf >/dev/null <<'EOF'
# opm-name/opm-lan ping-sweep the whole /24; the default ARP table (gc_thresh1=128)
# overflows and the GC evicts live recorders, making them flicker in and out.
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 2048
net.ipv4.neigh.default.gc_thresh3 = 4096
EOF
    sudo sysctl -p /etc/sysctl.d/99-arp-cache.conf >/dev/null
    echo "  raised the ARP cache limits (persisted in /etc/sysctl.d/99-arp-cache.conf)"
  fi
fi

say "done."
echo "Start it from the desktop icon, the app menu, or by running:  opm-name"
echo "It opens http://127.0.0.1:8771 — dock a recorder over ethernet and it appears."
