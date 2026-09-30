#!/usr/bin/env bash
# pi-fixes (one-shot fixes run against a docked board) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== pi-fixes"
need_cmd sshpass sshpass
need_cmd ssh     openssh-client
install_deps
chmod +x "$HERE"/*.sh
echo "  run from here: ./fix_boot_buzzer.sh  ./fix_timezone.sh  (Orange Pi 5)"
echo "  check_cam_link.sh runs ON a board: scp it over, then sudo ./check_cam_link.sh"
