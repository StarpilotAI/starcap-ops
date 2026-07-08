#!/usr/bin/env bash
# One-off fix: the PI-Dock wired-share connection keeps ending up on 10.42.0.1/24
# (NM's default), which collides with the recorder's own hotspot subnet and breaks
# opm-lan's discovery/ARP. Pin it to 10.77.0.1/24 as CLAUDE.md specifies.
set -euo pipefail
nmcli connection modify "PI-Dock" ipv4.method shared ipv4.addresses 10.77.0.1/24
nmcli connection down "PI-Dock" || true
nmcli connection up "PI-Dock"
sleep 2
echo "-- enp2s0 now --"
ip -br addr show enp2s0
