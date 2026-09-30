#!/usr/bin/env bash
# Launch focusbarrel and open it in the browser.
#   ./run.sh                    -> http://127.0.0.1:8150
#   ./run.sh --device /dev/video2
#   ./run.sh --host 0.0.0.0     -> reach it from a phone on the LAN
#   ./run.sh --no-lock          -> leave the camera's auto modes alone
cd "$(dirname "$0")" || exit 1
exec python3 focusbarrel.py "$@"
