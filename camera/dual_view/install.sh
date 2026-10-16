#!/usr/bin/env bash
# dual_view (DECXIN multi-camera live viewer + recorder) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== dual_view"
need_cmd python3  python3
need_py  numpy    python3-numpy
need_py  cv2      python3-opencv
need_cmd ffmpeg   ffmpeg
need_cmd v4l2-ctl v4l-utils
install_deps
chmod +x "$HERE"/dual_view.py
launcher "DECXIN Dual View" "DECXIN Dual View" "$HERE/dual_view.py" camera-video \
  "One window: stereo 168 on top, every mono DECXIN below; r records all cameras (MJPEG copy, 60 fps) to ~/Videos" true
