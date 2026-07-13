#!/usr/bin/env bash
# force_sd_boot.sh — fix the "board always boots Android from eMMC, ignores the SD
# card" issue seen on some fleet units.
#
# Why this is needed: on RK3588(S), the boot ROM checks SPI flash -> eMMC -> SD
# card and uses the FIRST valid bootloader it finds. If eMMC already has Android's
# bootloader on it, it always wins over the SD card, no matter what's on the SD
# card or whether it's even inserted.
#
# What this does (and why, not just erase-flash): the fix is to overwrite eMMC's
# bootloader region with a KNOWN-GOOD idbloader+u-boot (pulled once from a card
# that's already proven to boot fine on this fleet — see assets/), not just blank
# it. Blanking alone (plain erase-flash) makes the boot ROM fall through to SD,
# but on some units the boot ROM's own SD probe is too minimal/picky and still
# finds nothing — confirmed on a real board where erase-flash alone left it stuck
# in maskrom with a verified-good card inserted. Writing a real bootloader means
# U-BOOT (a far more complete MMC driver than the boot ROM's) gets to run and hand
# off to the SD card instead, which succeeded where the bare boot-ROM probe didn't.
# This still permanently overwrites Android's own boot chain — same irreversibility
# as erasing, just a smaller/faster write (~10MB vs the whole chip).
#
# THIS PERMANENTLY REMOVES ANDROID'S ABILITY TO BOOT FROM EMMC. Not easily
# reversible — only run this on a board you mean to move to SD-card
# Ubuntu/orangepi-recorder for good.
#
# Requires: the board connected via a USB-C cable DIRECT to this machine (no
# hub — hubs routinely fail to pass through Rockchip's low-level maskrom USB
# protocol) into its OTG/data port, in maskrom mode:
#   1. fully unplug the board's power
#   2. hold the MaskRom button (pin/toothpick)
#   3. plug in DC power while still holding it
#   4. hold ~2-3s more, then release
#
# Usage:  ./force_sd_boot.sh [--yes]
#   --yes   skip the confirmation prompt (for scripted/non-interactive use, e.g.
#           after a human has already confirmed out-of-band)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADER="$SCRIPT_DIR/assets/rk3588_spl_loader_v1.21.114.bin"
IDBLOADER="$SCRIPT_DIR/assets/idbloader.img"
UBOOT="$SCRIPT_DIR/assets/uboot.itb"
# Standard RK3588 raw-storage offsets (sectors, 512B each) — same convention on
# eMMC or SD, matches where these were originally read from on a known-good card.
IDBLOADER_SECTOR=64
UBOOT_SECTOR=16384
AUTO_YES=0
[ "${1:-}" = "--yes" ] && AUTO_YES=1

command -v rkdeveloptool >/dev/null || { echo "missing rkdeveloptool — install with: sudo apt-get install -y rkdeveloptool"; exit 1; }
for f in "$LOADER" "$IDBLOADER" "$UBOOT"; do
  [ -f "$f" ] || { echo "missing bundled asset: $f"; exit 1; }
done

echo "== looking for a board in maskrom mode =="
FOUND=0
for _ in $(seq 1 20); do
  if rkdeveloptool list 2>/dev/null | grep -qi maskrom; then FOUND=1; break; fi
  sleep 1
done
if [ "$FOUND" -ne 1 ]; then
  cat <<'EOF'
No board found in maskrom mode after 20s. Checklist:
  - cable plugged DIRECTLY into this machine, no USB hub in between
  - cable is a real DATA cable (some are charge-only)
  - plugged into the board's OTG/data USB-C port (not a power-only port)
  - fully unplugged power first, held the MaskRom button, plugged power in
    WHILE holding it, released ~2-3s after
Run `lsusb` — a Rockchip device (vendor 2207) should appear once it's right.
EOF
  exit 1
fi
echo "   found:"
rkdeveloptool list

echo "== loading DDR-init/bootloader into RAM =="
rkdeveloptool boot "$LOADER"
sleep 2

echo "== reading chip + eMMC info =="
rkdeveloptool read-chip-info
FLASH_INFO="$(rkdeveloptool read-flash-info 2>&1)"
echo "$FLASH_INFO"

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to OVERWRITE the eMMC bootloader shown above (Android will never boot from it again) !!"
  read -r -p "Type 'erase' to continue: " CONFIRM
  [ "$CONFIRM" = "erase" ] || { echo "aborted — nothing written."; exit 1; }
fi

echo "== writing known-good idbloader to eMMC sector $IDBLOADER_SECTOR =="
rkdeveloptool write "$IDBLOADER_SECTOR" "$IDBLOADER"

echo "== writing known-good u-boot to eMMC sector $UBOOT_SECTOR =="
rkdeveloptool write "$UBOOT_SECTOR" "$UBOOT"

echo "== rebooting the board =="
rkdeveloptool reset || true

cat <<'EOF'

Done. eMMC now boots a known-good U-Boot instead of Android, which will read and
boot from the SD card — make sure a flashed, bootable Orange Pi/Ubuntu SD card is
inserted before/soon after this reboot. No maskrom button needed again after this.

If it still doesn't come up, check the SD card itself next (see the other SD-card
checks in this repo) before assuming the board's hardware is at fault.
EOF
