#!/usr/bin/env bash
# arucocube installer. Needs OpenCV >= 4.7 (cv2.aruco.ArucoDetector); Ubuntu's
# python3-opencv 4.6 does not have it, so a private venv is built when needed.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== arucocube"
need_cmd python3 python3
need_py  numpy   python3-numpy
need_cmd fuser   psmisc          # names who holds the camera when it won't open
install_deps
chmod +x "$HERE"/*.sh "$HERE"/*.py
if python3 -c "import cv2; cv2.aruco.ArucoDetector" 2>/dev/null; then
  rm -rf "${HERE:?}/.venv"; echo "  opencv: system one has ArucoDetector"
else
  echo "  opencv: too old / missing, building $HERE/.venv"
  python3 -m venv "$HERE/.venv" && "$HERE/.venv/bin/pip" install -q opencv-python-headless numpy
fi
python3 "$HERE/make_icon.py" 2>/dev/null || [ -f "$HERE/icon.png" ]
launcher arucocube "ArUco Cube Check" "$HERE/run.sh" "$HERE/icon.png" \
  "Wear the docked DECXIN and turn the cube: finds misprinted, faded, dirty or curled ArUco faces" false
