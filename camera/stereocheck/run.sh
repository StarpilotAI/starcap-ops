#!/usr/bin/env bash
# Stereo Check desktop launcher: start the web app (or reuse the running one) and
# open it in the browser. http://127.0.0.1:8778
cd "$(dirname "$0")" || exit 1
exec ./app.py "$@"
