#!/usr/bin/env bash
# fix_arp_cache.sh  — enlarge the PC's ARP/neighbor table for opm-lan (run with sudo).
# ============================================================================
# opm-lan discovers recorders by ping-sweeping the whole 10.42.0.0/24 every scan.
# The default ARP table caps at gc_thresh1=128; a full sweep floods it to ~253, so
# the kernel's garbage collector EVICTS live recorders to make room — which is why
# pis flicker in and out of opm-lan ("shows up then disappears"). This raises the
# limits so a full-subnet sweep fits with headroom, applies it now, and persists it
# across reboots via /etc/sysctl.d.
#
#   Usage:  sudo bash ~/Desktop/starcap-tools/fix_arp_cache.sh
# ============================================================================
set -e
CONF=/etc/sysctl.d/99-arp-cache.conf

if [ "$(id -u)" -ne 0 ]; then
  echo "This needs root (it writes $CONF and applies sysctl)."
  echo "Re-run:  sudo bash $0"
  exit 1
fi

echo "Before: gc_thresh1/2/3 = $(cat /proc/sys/net/ipv4/neigh/default/gc_thresh1)/$(cat /proc/sys/net/ipv4/neigh/default/gc_thresh2)/$(cat /proc/sys/net/ipv4/neigh/default/gc_thresh3)"

cat > "$CONF" <<'EOF'
# opm-lan ping-sweeps the whole 10.42.0.0/24; the default ARP table (gc_thresh1=128)
# overflows to ~253 and the GC evicts live recorders, making pis flicker in and out.
# Enlarge it so a full-subnet sweep fits with headroom.
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 2048
net.ipv4.neigh.default.gc_thresh3 = 4096
EOF
echo "Wrote $CONF"

# apply now (also validates the file parses)
sysctl -p "$CONF"

echo "After:  gc_thresh1/2/3 = $(cat /proc/sys/net/ipv4/neigh/default/gc_thresh1)/$(cat /proc/sys/net/ipv4/neigh/default/gc_thresh2)/$(cat /proc/sys/net/ipv4/neigh/default/gc_thresh3)"
echo "Done — persisted (loads automatically on every boot) and active now."
