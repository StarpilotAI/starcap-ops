#!/usr/bin/env bash
# stereo-check (is a docked DECXIN still on its calibration?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== stereo-check"
need_cmd python3  python3
need_cmd ffmpeg   ffmpeg
need_cmd fuser    psmisc
need_py  cv2      python3-opencv
need_py  numpy    python3-numpy
need_py  psycopg2 python3-psycopg2
install_deps
link_cmd stereo-check "$HERE/stereo_check.py"
chmod +x "$HERE/run.sh"
launcher stereo-check "Stereo Check" "$HERE/run.sh" "$HERE/icon.png" \
  "Is the docked DECXIN still on its calibration? Captures, corrects and marks both eyes; robot bench button" false
