#!/usr/bin/env bash
# copy-station setup + opm SD-card launcher installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/install_common.sh"
echo "== station"
need_cmd curl     curl
need_cmd xdg-open xdg-utils
install_deps
chmod +x "$HERE"/*.sh
launcher opm-sdcard "opm (SD card copier)" "bash $HERE/launch_opm_sdcard.sh" drive-removable-media \
  "Copy recordings off Orange Pi SD cards via a USB reader" true
echo "  a FRESH copy-station laptop also needs:  sudo $HERE/setup_copy_station.sh"
