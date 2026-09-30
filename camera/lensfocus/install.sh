#!/usr/bin/env bash
# lensfocus / focusbarrel (hand-focus a lens barrel, live) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== lensfocus"
need_cmd python3  python3
need_py  cv2      python3-opencv
need_py  numpy    python3-numpy
need_cmd v4l2-ctl v4l-utils
install_deps
chmod +x "$HERE/run.sh" "$HERE/focusbarrel.py"
launcher focusbarrel "Lens Barrel Focus" "$HERE/run.sh" camera-video \
  "Screw the barrel and find the sharpest point - live view, green when at the peak"
