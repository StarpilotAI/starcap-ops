#!/usr/bin/env bash
# drive-health (is this drive/enclosure healthy?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== drive-health"
need_cmd python3  python3
need_cmd lsblk    util-linux
need_cmd findmnt  util-linux
install_deps
link_cmd drive-health      "$HERE/drive_health.py"
link_cmd fix-usb-enclosure "$HERE/fix_usb_enclosure.py"
