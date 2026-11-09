#!/usr/bin/env bash
# ArUco cube checker: live per-marker defect check from the docked DECXIN.
#   ./run.sh                          -> http://127.0.0.1:8786
#   ./run.sh --expected 0-5           -> also lists faces never seen
#   ./run.sh --dict 4X4_50 --dev /dev/video21
cd "$(dirname "$0")" || exit 1
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" arucocube.py "$@"
