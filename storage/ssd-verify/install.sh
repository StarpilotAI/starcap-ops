#!/usr/bin/env bash
# ssd-verify (is this drive's footage really in the cloud?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== ssd-verify"
need_cmd python3 python3
need_cmd ffprobe ffmpeg
install_deps
link_cmd check-ssd         "$HERE/check_ssd.py"
link_cmd ssd-audit         "$HERE/ssd_audit.py"
link_cmd check-cloud-hours "$HERE/check_cloud_hours.py"
link_cmd check-drive-hours "$HERE/check_drive_hours.py"
