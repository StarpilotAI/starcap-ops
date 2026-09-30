#!/usr/bin/env bash
# Double-click entry point. Re-execs into a terminal when launched from the
# desktop, because the whole output is text nobody would otherwise see - same
# pattern as the arm bench's launch_bench.sh.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -t 1 ] && [ -z "${CALIBCHECK_IN_TERMINAL:-}" ]; then
    for term in gnome-terminal x-terminal-emulator konsole xfce4-terminal xterm; do
        if command -v "$term" >/dev/null 2>&1; then
            exec "$term" -- bash -c \
                "CALIBCHECK_IN_TERMINAL=1 '$0' $* ; echo; echo 'Press Enter to close.'; read"
        fi
    done
fi

python3 "$here/check.py" "$@"
# Exit 1 just means "not all live" - it is the report, not a failure.
exit 0
