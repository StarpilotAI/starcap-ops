#!/usr/bin/env bash
# Firmware jitter bench — the double-click front end.
#
# Written for someone standing at the bench with a camera in one hand: no flags
# to remember, no way to typo a device path, and the two things that silently
# ruin a measurement (a camera that cannot stream, and a camera that cannot see
# the screen) are offered as their own menu item before any real run.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
BENCH="./fwbench.py"
[ -x "$BENCH" ] || { echo "fwbench.py missing or not executable"; read -r; exit 1; }

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

while true; do
    echo
    bold "===== Firmware jitter bench ====="
    dim  "proves whether a camera firmware build causes the frame-timing fault"
    echo
    ./fwbench.py status 2>/dev/null | tail -8
    echo
    echo "  1) Check aim        (15 s — do this once per camera, before measuring)"
    echo "  2) Measure a camera (~4 min — two repeats on one firmware)"
    echo "  3) Show progress    (what is still missing)"
    echo "  4) Write the report (to the Desktop)"
    echo "  q) Quit"
    echo
    read -rp "  choose: " choice
    case "$choice" in
      1) echo; ./fwbench.py probe; echo; read -rp "  press enter..." _ ;;
      2)
        echo
        read -rp "  camera number as the camera reports it (e.g. 4.1, 169): " cam
        [ -n "${cam:-}" ] || { echo "  need a camera number"; continue; }
        read -rp "  firmware build now on it (e.g. 114, 3): " fw
        [ -n "${fw:-}" ] || { echo "  need a firmware label"; continue; }
        echo
        bold "  camera $cam on firmware $fw — 2 repeats, about 4 minutes."
        echo  "  The screen flashes black/white. Do not touch it, and do not"
        echo  "  move the camera, until it says it is done."
        read -rp "  enter to start, or ctrl-c to back out..." _
        ./fwbench.py run --camera "$cam" --fw "$fw" --reps 2
        echo; read -rp "  press enter..." _ ;;
      3) echo; ./fwbench.py status; echo; read -rp "  press enter..." _ ;;
      4)
        out="$HOME/Desktop/firmware_jitter_report.md"
        ./fwbench.py report -o "$out" >/dev/null && {
            bold "  written to $out"
            command -v xdg-open >/dev/null && xdg-open "$out" >/dev/null 2>&1 &
        }
        echo; read -rp "  press enter..." _ ;;
      q|Q) exit 0 ;;
      *) echo "  ?" ;;
    esac
done
