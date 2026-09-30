#!/usr/bin/env bash
# flash_uboot_only.sh — write ONLY the U-Boot FIT (sector 16384) over maskrom.
#
# WHY THIS EXISTS
# The stock ubuntu-rockchip U-Boot ships boot_targets with the SD slot ahead of
# the eMMC, and this board HANGS inside `mmc dev` on the SD rather than erroring
# and moving on — so a card in the slot at power-on means no boot at all. The
# order cannot be changed at runtime: U-Boot's env backend is ENV_IS_NOWHERE, so
# setenv/saveenv has nowhere to persist.
#
# uboot-emmc-first.itb is the stock FIT with exactly that string reordered:
#   from  boot_targets=usb0 mmc1 nvme0 nvme1 scsi0 mmc0 mtd2 mtd1 mtd0 pxe dhcp
#   to    boot_targets=mmc0 usb0 mmc1 nvme0 nvme1 scsi0 mtd2 mtd1 mtd0 pxe dhcp
# (mmc0 = /mmc@fe2e0000, 8-bit non-removable = the eMMC, read from this
# bootloader's OWN fdt aliases — they are reversed vs Linux, and reversed again
# in the fleet's old 2017.09 bootloader, so never assume the number.)
#
# The FIT carries sha256 on every image, so the uboot hash was recomputed. It
# also carries a /configurations/conf/signature node — but that node has NO
# `value` property (algo + key-name-hint "dev" only), i.e. the FIT was never
# actually signed and nothing verifies a signature. Only the hashes matter, and
# all five verify.
#
# Usage:
#   ./flash_uboot_only.sh              write the eMMC-first U-Boot
#   ./flash_uboot_only.sh --rollback   put the ORIGINAL stock U-Boot back
#
# Board into maskrom first: unplug DC AND USB-C, wait 10 s, hold MaskRom, apply
# DC while holding, hold 2-3 s, release, plug USB-C into the laptop.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UBOOT_SECTOR=16384
LOADER="$HERE/rk3588_spl_loader.bin"
IMG="$HERE/uboot-emmc-first.itb"
WHAT="eMMC-FIRST"
if [ "${1:-}" = "--rollback" ]; then IMG="$HERE/uboot-ORIGINAL-rollback.itb"; WHAT="ORIGINAL (stock)"; fi
[ -f "$IMG" ] || { echo "missing $IMG"; exit 1; }

command -v rkdeveloptool >/dev/null || { echo "missing rkdeveloptool"; exit 1; }

echo "== about to write: $WHAT"
echo "   $(basename "$IMG")  ($(stat -c%s "$IMG") bytes)  -> sector $UBOOT_SECTOR"
echo "   boot_targets in this image:"
{ strings "$IMG" || true; } | grep -a -m1 "^boot_targets=" | sed 's/^/     /' || true
echo

echo "== waiting for a board in maskrom/loader =="
MODE=""
for _ in $(seq 1 30); do
  L="$(rkdeveloptool list 2>/dev/null || true)"
  if echo "$L" | grep -qi maskrom; then MODE=maskrom; break; fi
  if echo "$L" | grep -qi loader;  then MODE=loader;  break; fi
  sleep 1
done
[ -n "$MODE" ] || { echo "no board found. Put it in maskrom and retry."; exit 1; }
echo "   found: $MODE"

if [ "$MODE" = maskrom ]; then
  echo "== loading DDR init (rk3588_spl_loader.bin) =="
  [ -f "$LOADER" ] || { echo "missing $LOADER"; exit 1; }
  rkdeveloptool db "$LOADER" || { echo "loader download failed"; exit 1; }
  sleep 2
fi

echo "== writing U-Boot at sector $UBOOT_SECTOR =="
rkdeveloptool write "$UBOOT_SECTOR" "$IMG" || { echo "WRITE FAILED"; exit 1; }

echo "== reading it back to verify =="
SZ=$(stat -c%s "$IMG"); SECT=$(( (SZ + 511) / 512 ))
TMP="$(mktemp)"
if rkdeveloptool read "$UBOOT_SECTOR" "$SECT" "$TMP" >/dev/null 2>&1; then
  if cmp -n "$SZ" "$TMP" "$IMG" >/dev/null 2>&1; then
    echo "   VERIFIED — readback matches"
  else
    echo "   READBACK DIFFERS — do NOT power cycle; investigate."
    rm -f "$TMP"; exit 1
  fi
else
  echo "   (readback unavailable — write itself reported OK. Do NOT re-run 'db' to retry:
    re-downloading the loader on a board that already has one hangs forever — trap 4.)"
fi
rm -f "$TMP"

cat <<'MSG'

Done. Now power-cycle PROPERLY (the board back-powers over USB-C):
  1. unplug the USB-C from the laptop
  2. pull DC, wait 10 s
  3. DC back on

If the board does NOT come up, put it back in maskrom and run:
  ./flash_uboot_only.sh --rollback
That restores the byte-exact original U-Boot. Nothing else on the eMMC is touched
by either direction — this only ever writes sector 16384.
MSG
