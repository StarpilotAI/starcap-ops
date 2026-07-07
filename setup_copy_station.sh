#!/usr/bin/env bash
# setup_copy_station.sh — turn this Ubuntu machine into a StarCap copy-station.
#
# Reproduces the environment described in CLAUDE.md:
#   1. installs opm-lan's runtime deps (sshpass, rsync, ssh, ffmpeg, PyYAML,
#      opencv/numpy for contact sheets)
#   2. shares the wired NIC on 10.77.0.1/24 (NOT the recorder-hotspot default
#      10.42.0.1/24, which would collide with a docked pi's own subnet)
#   3. raises the ARP neighbor-table thresholds so a full-subnet ping-sweep
#      doesn't evict live recorders (persisted via /etc/sysctl.d)
#
# Needs sudo for: apt install, nmcli connection modify, sysctl/sysctl.d.
# Safe to re-run — every step is idempotent.
#
# Usage:  sudo ./setup_copy_station.sh [wired-iface]
#   wired-iface is optional; auto-detected if omitted (first non-wifi,
#   non-loopback interface reported by `ip -br link`).

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This needs root (apt install, nmcli, sysctl). Re-run:"
  echo "    sudo $0 $*"
  exit 1
fi

# Resolve the real user (for messages / file ownership), since we're running as root.
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== StarCap copy-station setup =="
echo "repo: $REPO_DIR"
echo

# ---------------------------------------------------------------------------
# 1. Packages
# ---------------------------------------------------------------------------
echo "-- [1/3] installing packages --"
apt-get update -qq
apt-get install -y \
  sshpass \
  openssh-client \
  rsync \
  ffmpeg \
  python3 \
  python3-yaml \
  python3-opencv \
  python3-numpy

echo "packages OK: sshpass, ssh, rsync, ffmpeg, python3(+yaml,opencv,numpy)"
echo

# ---------------------------------------------------------------------------
# 2. Wired NIC → shared, 10.77.0.1/24
# ---------------------------------------------------------------------------
echo "-- [2/3] wired NIC sharing (10.77.0.1/24) --"

WIRED_IFACE="${1:-}"
if [ -z "$WIRED_IFACE" ]; then
  # first interface that's ethernet-looking (not lo, not wl*/wifi), per `ip -br link`
  WIRED_IFACE="$(ip -br link show | awk '{print $1}' | grep -Ev '^(lo|wl|wwan|docker|veth|virbr|br-)' | head -n1)"
fi

if [ -z "$WIRED_IFACE" ]; then
  echo "  ⚠ couldn't auto-detect a wired interface. Skipping network setup."
  echo "    Re-run with the interface name, e.g.: sudo $0 enp2s0"
else
  echo "  wired interface: $WIRED_IFACE"

  CONN_NAME="$(nmcli -t -f NAME,DEVICE connection show | awk -F: -v d="$WIRED_IFACE" '$2==d{print $1; exit}')"
  if [ -z "$CONN_NAME" ]; then
    # no active connection bound to this device yet — fall back to any ethernet-type profile
    CONN_NAME="$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
  fi

  if [ -z "$CONN_NAME" ]; then
    echo "  no NetworkManager connection profile found for $WIRED_IFACE — creating one."
    CONN_NAME="opm-lan-wired"
    nmcli connection add type ethernet ifname "$WIRED_IFACE" con-name "$CONN_NAME"
  fi

  echo "  connection profile: $CONN_NAME"
  nmcli connection modify "$CONN_NAME" ipv4.method shared ipv4.addresses 10.77.0.1/24
  nmcli connection down "$CONN_NAME" >/dev/null 2>&1 || true
  nmcli connection up "$CONN_NAME" >/dev/null 2>&1 || echo "  (bring-up failed — plug in a cable/dock a recorder, then: nmcli connection up $CONN_NAME)"

  echo "  verify later with: ip -br addr show $WIRED_IFACE   (want 10.77.0.1/24)"
fi
echo

# ---------------------------------------------------------------------------
# 3. ARP cache thresholds
# ---------------------------------------------------------------------------
# opm-lan discovers recorders by ping-sweeping the whole /24 every scan. The
# default ARP table caps at gc_thresh1=128; a full sweep floods it to ~253, so the
# kernel's garbage collector EVICTS live recorders to make room — which is why pis
# flicker in and out of opm-lan ("shows up then disappears"). This raises the
# limits so a full-subnet sweep fits with headroom, applies it now, and persists
# it across reboots via /etc/sysctl.d.
echo "-- [3/3] ARP cache thresholds --"
ARP_CONF=/etc/sysctl.d/99-arp-cache.conf
cat > "$ARP_CONF" <<'EOF'
# opm-lan ping-sweeps the whole /24; the default ARP table (gc_thresh1=128)
# overflows to ~253 and the GC evicts live recorders, making pis flicker in and out.
# Enlarge it so a full-subnet sweep fits with headroom.
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 2048
net.ipv4.neigh.default.gc_thresh3 = 4096
EOF
sysctl -p "$ARP_CONF" >/dev/null
echo "  wrote $ARP_CONF and applied it"
echo

# ---------------------------------------------------------------------------
# Wrap up
# ---------------------------------------------------------------------------
chmod +x "$REPO_DIR"/opm-lan/start_opm_lan.sh "$REPO_DIR"/tools/*.sh 2>/dev/null || true

echo "== Done =="
echo "Next steps:"
echo "  1. Edit $REPO_DIR/opm-lan/config.yaml → set 'destination:' to your SSD's mount path"
echo "     (e.g. /media/$REAL_USER/<DriveLabel>)."
echo "  2. cd $REPO_DIR/opm-lan && ./start_opm_lan.sh   → http://localhost:8770"
echo "  3. Dock a recorder over ethernet — it should appear in the UI."
