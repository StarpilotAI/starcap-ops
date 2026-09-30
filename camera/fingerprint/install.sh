#!/usr/bin/env bash
# camera-fingerprint (will this DECXIN's JPEGs pass the RK hw decoder?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== camera-fingerprint"
need_cmd python3  python3
need_cmd ffmpeg   ffmpeg
need_cmd v4l2-ctl v4l-utils
install_deps
link_cmd camera-fingerprint "$HERE/camera_fingerprint.py"
