#!/usr/bin/env bash
# clone_sd_to_emmc.sh — write a golden eMMC image straight onto a board's eMMC
# over USB/maskrom, so it boots the full recorder OS from eMMC directly — no SD
# card needed at all.
#
# The default image (assets/golden/stereocam_emmc.img) is NOT just a captured SD
# card (see capture_golden_image.sh) — plain SD captures of this fleet's own
# image do NOT boot reliably from eMMC on their own. Getting a real eMMC boot
# working took two separate fixes, both baked into stereocam_emmc.img already:
#
#   1. Wrong device tree: this fleet's image is built with BOARD=orangepi5 (the
#      plain 5, not 5B), and /boot/orangepiEnv.txt points fdtfile at
#      rockchip/rk3588s-orangepi-5.dtb — which has no eMMC controller wired up
#      at all, even though the SAME kernel package ships a working
#      rk3588s-orangepi-5b.dtb right next to it. Confirmed by lsblk: booting the
#      unpatched dtb shows ZERO mmc devices for eMMC; the patched one shows
#      mmcblk1 + mmcblk1boot0/boot1 (eMMC's hardware boot partitions) present
#      and mounted as root. Fix: edit orangepiEnv.txt's fdtfile to the -5b dtb
#      (that's the vendor-documented way to change board target on this build —
#      see the comment at the top of /boot/boot.cmd).
#   2. Wrong bootloader: even with the dtb fixed, this fleet's OWN idbloader/
#      u-boot (from orangepi-build) gets rejected by this chip's boot ROM when
#      read from eMMC specifically — it boots fine from an SD card, just not
#      from eMMC. The fix: splice in the idbloader (sector 64) + u-boot (sector
#      16384) from the OFFICIAL orangepi.dev-built image instead (proven to
#      boot clean from eMMC on the very first try), keeping this fleet's own
#      rootfs/partition data everywhere else. Different bootloader, same OS.
#
# If you ever need to rebuild stereocam_emmc.img from a fresh stereocam.img:
#   1. Patch the dtb (mount the rootfs partition rw, offset = start-sector*512):
#        sed -i 's/rk3588s-orangepi-5\.dtb/rk3588s-orangepi-5b.dtb/' boot/orangepiEnv.txt
#   2. Splice in a known-good bootloader extracted from an official image's
#      first ~16MB (idbloader at byte 64*512, u-boot FIT at byte 16384*512 —
#      find the u-boot offset by scanning for the FIT magic d0 0d fe ed).
#   3. SD-card-as-storage (orangepi-recorder >= 1.24.0): eMMC is only tens of
#      GB but sessions can run to ~100GB, so eMMC-booted boards use their SD
#      slot as pure recording storage instead — see stereocam/app.py's
#      storage_ready()/StorageWatchdog and install.sh's "SD-card-as-storage"
#      step in the orangepi-recorder repo. That step is normally gated behind
#      "is this board eMMC-booted", but since THIS image only ever runs on
#      eMMC-booted boards, it's baked in unconditionally: copy in an updated
#      stereocam/app.py + stereocam/VERSION + install.sh, and manually write
#      /etc/udev/rules.d/99-starcap-sdcard.rules + /usr/local/sbin/starcap-
#      sdcard-automount.sh + mkdir /media/sdcard (can't just run install.sh
#      against an unmounted, not-booted image — replicate its effects by hand).
#
# On first boot, the OS's own first-boot services (armed by make-image.sh on the
# source card) self-assign a fresh device_id, SSH host keys, and machine-id, and
# grow the rootfs partition to fill however much eMMC space is actually there —
# same as a freshly-flashed SD card does, just landing on eMMC instead.
#
# THIS PERMANENTLY OVERWRITES THE BOARD'S EMMC (Android or anything else on it).
#
# Requires: the board connected via a USB-C cable DIRECT to this machine (no
# hub) into its OTG/data port, in maskrom mode:
#   1. fully unplug the board's power
#   2. hold the MaskRom button (pin/toothpick)
#   3. plug in DC power while still holding it
#   4. hold ~2-3s more, then release
#
# Usage:  ./clone_sd_to_emmc.sh [image_path] [--yes]
#   image_path   default: assets/golden/stereocam_emmc.img
#   --yes        skip the confirmation prompt
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADER="$SCRIPT_DIR/assets/rk3588_spl_loader_v1.21.114.bin"
IMAGE="$SCRIPT_DIR/assets/golden/stereocam_emmc.img"
AUTO_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) AUTO_YES=1 ;;
    *) IMAGE="$arg" ;;
  esac
done

command -v rkdeveloptool >/dev/null || { echo "missing rkdeveloptool — install with: sudo apt-get install -y rkdeveloptool"; exit 1; }
[ -f "$LOADER" ] || { echo "missing loader binary: $LOADER"; exit 1; }
[ -f "$IMAGE" ] || { echo "missing golden image: $IMAGE — see this script's header comment for how to rebuild it"; exit 1; }
IMAGE_BYTES="$(stat -c%s "$IMAGE")"
echo "== image: $IMAGE ($((IMAGE_BYTES / 1024 / 1024)) MB) =="

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
EMMC_MB="$(echo "$FLASH_INFO" | grep -oP 'Flash Size:\s*\K[0-9]+(?= MB)' | head -1)"
if [ -n "${EMMC_MB:-}" ]; then
  IMAGE_MB=$((IMAGE_BYTES / 1024 / 1024))
  if [ "$IMAGE_MB" -gt "$EMMC_MB" ]; then
    echo "!! image ($IMAGE_MB MB) is BIGGER than this board's eMMC ($EMMC_MB MB) — aborting, would truncate the image."
    exit 1
  fi
  echo "   image fits: $IMAGE_MB MB onto $EMMC_MB MB eMMC"
fi

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to OVERWRITE this board's ENTIRE eMMC with the golden image (Android and everything else gone for good) !!"
  read -r -p "Type 'clone' to continue: " CONFIRM
  [ "$CONFIRM" = "clone" ] || { echo "aborted — nothing written."; exit 1; }
fi

echo "== writing golden image to eMMC (sector 0) — this takes a while for a multi-GB image =="
rkdeveloptool write 0 "$IMAGE"

echo "== rebooting the board =="
rkdeveloptool reset || true

cat <<'EOF'

Done. This board now boots the recorder OS straight from eMMC — no SD card
needed. On this first boot it will: grow its rootfs to fill the eMMC, generate
fresh SSH host keys + machine-id, and self-assign a fresh StarCap device_id.
Give it a minute or two for that first-boot work before expecting it on the
dashboard/opm-lan.
EOF
