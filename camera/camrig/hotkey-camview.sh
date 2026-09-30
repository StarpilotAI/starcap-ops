#!/usr/bin/env bash
# Opens CamView from a keyboard shortcut.
#
# Two things a hotkey needs that a desktop icon does not:
#
#   1. A hotkey gets pressed again while the viewer is already up, and a second instance would
#      fight the first for /dev/video* and lose - ONE process at a time owns a camera node. So
#      raise the window that exists instead of starting a rival for it.
#   2. There is no terminal behind a hotkey, so a failure to open has nowhere to print. That
#      is indistinguishable from the key not working at all, which is the one outcome this is
#      meant to fix, so say so on the desktop instead.
set -u
here="$(dirname "$(readlink -f "$0")")"

if pgrep -f "python3 .*camview\.py" >/dev/null 2>&1; then
    wmctrl -a camview 2>/dev/null || xdotool search --name '^camview$' windowactivate 2>/dev/null
    exit 0
fi

out="$("$here/launch-camview.sh" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ]; then
    msg="$(printf '%s' "$out" | grep -v '^$' | tail -3)"
    notify-send -u critical "CamView could not open" "${msg:-exit $rc}" 2>/dev/null
fi
exit "$rc"
