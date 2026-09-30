#!/usr/bin/env bash
# Launch StarCap Finder and open it in the browser.
#   ./run.sh                 -> http://127.0.0.1:8090
#   ./run.sh --port 9100     -> different port
#   ./run.sh --no-browser    -> just serve, don't open a browser
cd "$(dirname "$0")" || exit 1
exec python3 starcapfinder.py "$@"
