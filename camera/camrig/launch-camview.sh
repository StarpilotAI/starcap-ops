#!/usr/bin/env bash
# CamView launcher. Used by the desktop icon; also fine to run by hand.
here="$(dirname "$(readlink -f "$0")")"
cd "$here" || exit 1

# OpenCV's Qt window only full-screens properly under X11; on a Wayland session this runs it
# through XWayland instead of the native Wayland plugin.
export QT_QPA_PLATFORM=xcb

# install.sh builds a private venv only when the system OpenCV cannot open a window.
PY="python3 -s"   # -s: ignore a pip OpenCV in ~/.local, use the one install.sh checked
[ -x "$here/.venv/bin/python3" ] && PY="$here/.venv/bin/python3"

$PY camview.py --take "$@"
rc=$?
# Launched from the icon, the terminal would vanish with the error in it.
if [ "$rc" -ne 0 ] && [ -t 0 ]; then
    echo
    echo "CamView exited with code $rc. Common causes:"
    echo "  - no DECXIN plugged in (it needs a USB 3 port, the blue one)"
    echo "  - you were just added to the 'video' group: log out and back in once"
    read -rp "Press Enter to close..." _
fi
exit "$rc"
