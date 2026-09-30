#!/usr/bin/env bash
# install_sd_boot_handoff.sh — drop the SD->eMMC boot handoff script onto a
# recorder SD card, so a board with the card inserted still boots off eMMC.
#
# See sdcard-boot.cmd for the full why. Short version: this U-Boot scans mmc1
# (the SD slot) before mmc0 (the eMMC), the order is compiled into a SIGNED FIT,
# and the env backend is ENV_IS_NOWHERE — so the only place left to intervene is
# the card itself.
#
# Usage:
#   ./install_sd_boot_handoff.sh /dev/sdX1          # a card in this laptop
#   ./install_sd_boot_handoff.sh /media/you/CARD    # or an already-mounted path
#
# Idempotent. Safe to re-run; it only adds /boot.scr and touches nothing else.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/sdcard-boot.scr"
[ -f "$SRC" ] || { echo "missing $SRC — run mkimage on sdcard-boot.cmd first"; exit 1; }
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: $0 <sd-partition-device|mountpoint>"; exit 1; }

CLEANUP=""
if [ -b "$TARGET" ]; then
  MNT="$(mktemp -d)"
  echo "== mounting $TARGET at $MNT"
  sudo -A mount "$TARGET" "$MNT"
  CLEANUP="$MNT"
elif [ -d "$TARGET" ]; then
  MNT="$TARGET"
else
  echo "not a block device or directory: $TARGET"; exit 1
fi
trap '[ -n "$CLEANUP" ] && { sudo -A umount "$CLEANUP" && rmdir "$CLEANUP"; }' EXIT

# Refuse to write to the eMMC root by mistake — a recorder data card has no /etc.
if [ -d "$MNT/etc" ] && [ -d "$MNT/usr" ]; then
  echo "REFUSING: $MNT looks like a root filesystem, not a recording card."; exit 1
fi

echo "== installing /boot.scr on $MNT"
sudo -A cp "$SRC" "$MNT/boot.scr"
sudo -A cp "$HERE/sdcard-boot.cmd" "$MNT/boot.cmd"
sudo -A chmod 644 "$MNT/boot.scr" "$MNT/boot.cmd"
sync
ls -l "$MNT/boot.scr"
echo
echo "Done. On the next boot with this card in, U-Boot should print:"
echo "    Found U-Boot script /boot.scr"
echo "    StarCap: SD card present - handing boot to eMMC (mmc0)"
echo "and then boot normally off the eMMC."
