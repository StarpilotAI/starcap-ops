#!/usr/bin/env bash
# dock LAN (make the ethernet port accept docked boards) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/install_common.sh"
echo "== station"
chmod +x "$HERE/setup_dock_lan.sh"
echo "  this one needs root and changes the network, so it is never run for you:"
echo "    sudo $HERE/setup_dock_lan.sh        (then: $HERE/setup_dock_lan.sh --check)"
