#!/usr/bin/env bash
# make_launcher.sh — put two icons on the desktop. Double-click one and a
# terminal opens, asks for your password, shows every board in maskrom, asks
# you to type 'flash', then flashes them all at once and stays open on the
# PASSED/FAILED summary.
#   Flash NanoPis             sudo ./flash_nanopi_emmc.sh --all
#   Flash + Update NanoPis    sudo ./flash_nanopi_emmc.sh --all --update
#                             (boards' ethernet in the dock port too: each is
#                             updated to latest main before the run says done)
# Re-run it if this folder moves. No root needed.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
command -v gnome-terminal >/dev/null || { echo "needs gnome-terminal"; exit 1; }

# make_icon <file> <name> <comment> <script args>
make_icon() {
  local out="$DESK/$1"
  cat >"$out" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Icon=media-flash
Terminal=false
Exec=gnome-terminal --title="$2" -- bash -c 'cd "$DIR" && sudo ./flash_nanopi_emmc.sh $4; echo; read -r -p "Press Enter to close. "'
DESKTOP
  chmod +x "$out"
  # GNOME won't launch a desktop file until it's marked trusted
  gio set "$out" metadata::trusted true 2>/dev/null || true
  echo "made $out"
}
make_icon flash-nanopis.desktop "Flash NanoPis" \
  "Flash every NanoPi R76S in maskrom on USB, all at once" "--all"
make_icon flash-update-nanopis.desktop "Flash + Update NanoPis" \
  "Flash every NanoPi in maskrom, then update each to latest main over the dock LAN" "--all --update"
echo "If GNOME shows one greyed out: right-click it -> Allow Launching."
