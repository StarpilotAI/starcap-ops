#!/usr/bin/env bash
# orangepi5 maskrom tools (RK3588) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== orangepi5"
need_cmd rkdeveloptool rkdeveloptool
need_cmd lsusb         usbutils
install_deps
chmod +x "$HERE"/*.sh
