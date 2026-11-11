#!/usr/bin/env bash
# CamView launcher. Used by the desktop icon and the ± hotkey; also fine to run by hand.
# CamView is a single-instance GTK app: launching it again just raises the open window.
here="$(dirname "$(readlink -f "$0")")"
cd "$here" || exit 1

# -s: ignore a pip OpenCV in ~/.local and use the apt one install.sh checked
err="$(mktemp)"; trap 'rm -f "$err"' EXIT
python3 -s camview.py --take "$@" 2> >(tee "$err" >&2); rc=$?
out="$(cat "$err")"
# Launched from the icon there is no terminal to show an error in, so say it on the desktop.
if [ "$rc" -ne 0 ]; then
    msg="$(printf '%s' "$out" | grep -v -e '^$' -e WARN | tail -3)"
    notify-send -u critical "CamView could not open" \
        "${msg:-exit $rc}"$'\n'"Missing packages? Run camera/camrig/install.sh" 2>/dev/null
fi
exit "$rc"
