#!/usr/bin/env bash
# check_onboard_storage.sh — report whether a connected board (in maskrom mode)
# has a real onboard eMMC, or just a small SPI-NOR flash with no eMMC at all.
#
# Why this matters: some fleet units are the cheaper BOM option with no eMMC
# populated — just a tiny (~16MB) SPI-NOR flash for boot firmware, SD-card-only
# for the OS. Others have a real eMMC (tens to hundreds of GB) that can run the
# recorder OS directly via clone_sd_to_emmc.sh. This is a read-only check — it
# loads the DDR-init loader into RAM and reads flash info, but writes nothing.
#
# Requires: the board connected via a USB-C cable DIRECT to this machine (no
# hub) into its OTG/data port, in maskrom mode:
#   1. fully unplug the board's power
#   2. hold the MaskRom button (pin/toothpick)
#   3. plug in DC power while still holding it
#   4. hold ~2-3s more, then release
#
# Usage:  ./check_onboard_storage.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADER="$SCRIPT_DIR/assets/rk3588_spl_loader_v1.21.114.bin"

command -v rkdeveloptool >/dev/null || { echo "missing rkdeveloptool — install with: sudo apt-get install -y rkdeveloptool"; exit 1; }
[ -f "$LOADER" ] || { echo "missing loader binary: $LOADER"; exit 1; }

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

echo "== loading DDR-init/bootloader into RAM (read-only from here — nothing gets written) =="
rkdeveloptool boot "$LOADER"
sleep 2

echo "== chip info =="
rkdeveloptool read-chip-info

echo "== flash info =="
FLASH_INFO="$(rkdeveloptool read-flash-info 2>&1)"
echo "$FLASH_INFO"

FLASH_MB="$(echo "$FLASH_INFO" | grep -oP 'Flash Size:\s*\K[0-9]+(?= MB)' | head -1)"

echo
if [ -z "${FLASH_MB:-}" ]; then
  echo "!! couldn't parse a flash size out of read-flash-info — see raw output above."
elif [ "$FLASH_MB" -ge 1000 ]; then
  GB="$(( (FLASH_MB + 512) / 1024 ))"
  echo ">> Real onboard eMMC detected: ~${GB}GB (${FLASH_MB}MB). This board can run the"
  echo "   recorder OS directly from eMMC — see clone_sd_to_emmc.sh."
else
  echo ">> Only ${FLASH_MB}MB of onboard flash — that's a small SPI-NOR chip, NOT a real"
  echo "   eMMC (too small to hold an OS). This board has no usable onboard storage for"
  echo "   the recorder OS; it's SD-card-only. Don't run clone_sd_to_emmc.sh on this one."
fi

echo
echo "== resetting board back to normal boot (nothing was written) =="
rkdeveloptool reset || true
