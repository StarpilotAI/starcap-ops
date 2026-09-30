#!/usr/bin/env bash
# setup_dock_lan.sh — make this laptop's ethernet port accept docked StarCap boards
# (NanoPi R76S or Orange Pi 5), so opm-name, dock-check, vnc, starcapfinder and
# every other fleet tool can see them.
#
#   sudo ./setup_dock_lan.sh [iface]   set it up (or repair it). Safe to re-run.
#   ./setup_dock_lan.sh --check        no root, changes nothing: is the dock port
#                                      up, and which boards answer on which login
#
# What the setup does:
#   1. installs sshpass + ssh, which every fleet tool uses to log in to a board
#   2. makes a NetworkManager profile `starcap-dock` on the wired port: shared
#      (NAT + DHCP) on 10.77.0.1/24, so a docked board gets a 10.77.0.x address
#      and internet through the laptop. NOT the NM default 10.42.0.1/24: the boards
#      run their own hotspot on that subnet, the gateway collides, and the laptop
#      can no longer reach a docked board at all.
#   3. pins that profile to autoconnect priority 100, and turns autoconnect off for
#      any OTHER profile on the same port. Docking a board bounces the carrier, NM
#      re-picks a profile on every bounce, and a plain DHCP-client profile winning
#      takes the shared subnet down with it: the board never gets an IP and simply
#      never shows up. Profiles on other ports (an office uplink) are left alone.
#   4. raises the ARP table limits. Discovery ping-sweeps the whole /24, the
#      default table (gc_thresh1=128) overflows, and the kernel evicts LIVE boards:
#      the "shows up, vanishes, comes back" symptom.
#
# A profile left over under an older name (`opm-lan`, `PI-Dock`, "Wired connection 1")
# on the same port is renamed to `starcap-dock` rather than duplicated: two profiles
# on one port race over which one autoconnects.
#
# [iface] picks the dock port when the laptop has more than one wired interface;
# with exactly one it is found automatically.

set -uo pipefail

CONN_NAME="starcap-dock"
SUBNET_ADDR="10.77.0.1/24"
LOGINS=("orangepi:orangepi" "pi:12345")   # Orange Pi 5, then NanoPi R76S

wired_ifaces() {
  # real ethernet devices only: NM's own view, minus virtual bridges/containers
  nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
    | awk -F: '$2=="ethernet"{print $1}' | grep -Ev '^(docker|veth|virbr|br-)'
}

# ------------------------------------------------------------------ --check
if [ "${1:-}" = "--check" ]; then
  IFACE="$(ip -br addr | awk -v a="$SUBNET_ADDR" '$0 ~ a {print $1; exit}')"
  ACTIVE="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
            | awk -F: -v d="${IFACE:-none}" '$2==d{print $1}')"
  if [ -z "$IFACE" ]; then
    echo "dock port: DOWN (nothing holds $SUBNET_ADDR)"
    echo "  plug in the cable/switch, then: nmcli connection up $CONN_NAME"
    echo "  still nothing? run: sudo $0"
    exit 1
  fi
  echo "dock port: $IFACE up on $SUBNET_ADDR (profile: ${ACTIVE:-?})"
  command -v sshpass >/dev/null || { echo "  sshpass missing: sudo $0"; exit 1; }
  echo "sweeping 10.77.0.2-254 ..."
  tmp="$(mktemp -d)"
  for i in $(seq 2 254); do
    ( ping -c1 -W1 "10.77.0.$i" >/dev/null 2>&1 && touch "$tmp/10.77.0.$i" ) &
  done
  wait
  found=0
  for f in $(ls "$tmp" | sort -t. -k4 -n); do
    ip="$f"; who="no ssh login worked"
    for l in "${LOGINS[@]}"; do
      u="${l%%:*}"; p="${l#*:}"
      name="$(sshpass -p "$p" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
               -o ConnectTimeout=4 -o LogLevel=ERROR "$u@$ip" \
               'cat ~/.config/starcap/device_name 2>/dev/null || hostname' 2>/dev/null)"
      if [ -n "$name" ]; then
        board="Orange Pi"; [ "$u" = pi ] && board="NanoPi"
        who="$board ($u) $name"; break
      fi
    done
    printf '  %-12s %s\n' "$ip" "$who"; found=$((found+1))
  done
  rm -rf "$tmp"
  [ "$found" -gt 0 ] || echo "  no boards answered. A board takes ~1-2 min after power-on."
  exit 0
fi

# ------------------------------------------------------------------ setup
if [ "$(id -u)" -ne 0 ]; then
  echo "This needs root (apt, nmcli, sysctl). Re-run:  sudo $0 $*"
  echo "Or check the current state without changing anything:  $0 --check"
  exit 1
fi
command -v nmcli >/dev/null || { echo "!! no nmcli: this needs NetworkManager"; exit 1; }

echo "== StarCap dock LAN setup =="

echo "-- [1/4] packages"
missing=()
command -v sshpass >/dev/null || missing+=(sshpass)
command -v ssh     >/dev/null || missing+=(openssh-client)
command -v ping    >/dev/null || missing+=(iputils-ping)
if [ ${#missing[@]} -gt 0 ]; then
  apt-get install -y "${missing[@]}" || echo "  ⚠ install failed: apt install ${missing[*]}"
else
  echo "  sshpass, ssh, ping: present"
fi

echo "-- [2/4] wired profile '$CONN_NAME' on $SUBNET_ADDR"
IFACE="${1:-}"
if [ -z "$IFACE" ]; then
  mapfile -t CANDS < <(wired_ifaces)
  if [ ${#CANDS[@]} -eq 1 ]; then
    IFACE="${CANDS[0]}"
  elif [ ${#CANDS[@]} -eq 0 ]; then
    echo "!! no wired interface found. Plug in the ethernet adapter and re-run."; exit 1
  else
    echo "!! ${#CANDS[@]} wired interfaces (${CANDS[*]}): say which one is the dock port:"
    echo "     sudo $0 ${CANDS[0]}"
    exit 1
  fi
fi
echo "  dock port: $IFACE"

# profiles already tied to this port (bound by name, or active on it right now)
on_port() {
  nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1}' |
  while read -r c; do
    bound="$(nmcli -g connection.interface-name connection show "$c" 2>/dev/null)"
    dev="$(nmcli -g GENERAL.DEVICES connection show "$c" 2>/dev/null)"
    { [ "$bound" = "$IFACE" ] || [ "$dev" = "$IFACE" ]; } && echo "$c"
  done
}
mapfile -t MINE < <(on_port)

if nmcli -t -f NAME connection show | grep -qx "$CONN_NAME"; then
  :
else
  OLD=""
  for c in "${MINE[@]:-}"; do
    case "$c" in opm-lan|PI-Dock) OLD="$c"; break ;; esac
  done
  [ -n "$OLD" ] || OLD="${MINE[0]:-}"
  if [ -n "$OLD" ]; then
    echo "  renaming '$OLD' -> '$CONN_NAME'"
    nmcli connection modify "$OLD" connection.id "$CONN_NAME"
  else
    echo "  creating '$CONN_NAME'"
    nmcli connection add type ethernet ifname "$IFACE" con-name "$CONN_NAME" >/dev/null
  fi
fi
nmcli connection modify "$CONN_NAME" connection.interface-name "$IFACE" \
  ipv4.method shared ipv4.addresses "$SUBNET_ADDR" \
  connection.autoconnect yes connection.autoconnect-priority 100

echo "-- [3/4] nothing else may grab $IFACE"
while read -r c; do
  [ -n "$c" ] && [ "$c" != "$CONN_NAME" ] || continue
  echo "  autoconnect off: '$c' (undo: nmcli connection modify '$c' connection.autoconnect yes)"
  nmcli connection modify "$c" connection.autoconnect no || true
done < <(on_port)

nmcli connection down "$CONN_NAME" >/dev/null 2>&1 || true
nmcli connection up "$CONN_NAME" >/dev/null 2>&1 \
  || echo "  (not up yet: it comes up when a cable or board is plugged in)"

echo "-- [4/4] ARP cache limits"
cat > /etc/sysctl.d/99-arp-cache.conf <<'EOF'
# StarCap fleet tools ping-sweep the whole dock /24; the default ARP table
# (gc_thresh1=128) overflows and the GC evicts live boards, so they flicker in
# and out of every tool. Enlarge it so a full sweep fits with headroom.
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 2048
net.ipv4.neigh.default.gc_thresh3 = 4096
EOF
sysctl -p /etc/sysctl.d/99-arp-cache.conf >/dev/null && echo "  written and applied"

echo
echo "done. $IFACE shares $SUBNET_ADDR as '$CONN_NAME'."
echo "Dock a NanoPi or Orange Pi, give it ~1-2 min, then:  $0 --check"
echo "If the port ever drops:  nmcli connection up $CONN_NAME"
