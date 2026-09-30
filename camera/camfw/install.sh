#!/usr/bin/env bash
# camfw (which firmware family is this DECXIN camera on?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== camfw"
need_cmd python3  python3
need_py  numpy    python3-numpy
need_cmd ffmpeg   ffmpeg
need_cmd v4l2-ctl v4l-utils
need_cmd lsusb    usbutils
need_cmd curl     curl
need_cmd xdg-open xdg-utils
install_deps
chmod +x "$HERE"/*.py "$HERE"/*/run.sh
launcher "Camera Firmware" "Camera Firmware" "$HERE/fwidentapp/run.sh" camera-web \
  "Identify whether a docked DECXIN camera is on the old (jittering) or latest firmware, and record it on the dashboard"
launcher "Camera Firmware LAN" "Camera Firmware LAN" "$HERE/fwlanapp/run.sh" network-wired \
  "Scan the dock cable and show which starcaps have a camera on the firmware that jitters"
[ -d "${STARCAP_DASHBOARD:-$HOME/starcap-dashboard}/opm-lan" ] \
  || echo "  note: Camera Firmware LAN needs a starcap-dashboard checkout (opm-lan) at ~/starcap-dashboard"
