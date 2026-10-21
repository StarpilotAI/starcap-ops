#!/usr/bin/env bash
# Launch RAM Check and open it in the browser.
#   ./run.sh                 -> http://127.0.0.1:8776
#   ./run.sh --port 9100     -> different port
#   ./run.sh --no-browser    -> just serve, don't open a browser
#   ./run.sh --replay FILE   -> print the verdict for a saved log
cd "$(dirname "$0")" || exit 1
exec python3 ramcheck.py "$@"
