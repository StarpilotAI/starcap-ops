#!/usr/bin/env bash
# Launch camrig and open it in the browser.
#   ./run.sh                 -> http://127.0.0.1:8088
#   ./run.sh --port 9000     -> different port
#   ./run.sh --host 0.0.0.0  -> reachable from a phone/tablet on the LAN
cd "$(dirname "$0")" || exit 1
exec python3 camrig.py "$@"
