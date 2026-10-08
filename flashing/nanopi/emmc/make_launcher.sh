#!/usr/bin/env bash
# make_launcher.sh — put a "Flash NanoPis" icon on the desktop. Double-click it
# and a terminal opens running `sudo ./flash_nanopi_emmc.sh --all`: it asks for
# your password, shows every board in maskrom, asks you to type 'flash', then
# flashes them all at once and stays open on the PASSED/FAILED summary.
# Re-run it if this folder moves. No root needed.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
OUT="$DESK/flash-nanopis.desktop"
command -v gnome-terminal >/dev/null || { echo "needs gnome-terminal"; exit 1; }

cat >"$OUT" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Flash NanoPis
Comment=Flash every NanoPi R76S in maskrom on USB, all at once
Icon=media-flash
Terminal=false
Exec=gnome-terminal --title="Flash NanoPis" -- bash -c 'cd "$DIR" && sudo ./flash_nanopi_emmc.sh --all; echo; read -r -p "Press Enter to close. "'
DESKTOP
chmod +x "$OUT"
# GNOME won't launch a desktop file until it's marked trusted
gio set "$OUT" metadata::trusted true 2>/dev/null || true
echo "made $OUT"
echo "If GNOME still shows it greyed out: right-click it -> Allow Launching."
