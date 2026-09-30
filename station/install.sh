#!/usr/bin/env bash
# copy-station setup installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/install_common.sh"
echo "== station"
chmod +x "$HERE/setup_copy_station.sh"
echo "  this one needs root and changes the network, so it is never run for you:"
echo "    sudo $HERE/setup_copy_station.sh"
