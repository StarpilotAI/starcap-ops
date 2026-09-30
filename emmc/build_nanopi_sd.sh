#!/usr/bin/env bash
# build_nanopi_sd.sh — build a bootable SD card for a FriendlyElec RK3576 NanoPi
# straight out of one of their `rk3576-usb-*` image packages.
#
# WHY THIS EXISTS
# ---------------
# flash_nanopi_emmc.sh pushes the same package into the board's eMMC over
# USB/maskrom. When maskrom download-boot won't work (see that script's header,
# and the 471/index(1) err=-7 wall we hit on the R76S on 2026-08-01), the whole
# USB path is unavailable. This script sidesteps it: the BootROM reads the
# IDBlock off the SD card at sector 64, so the DDR-init blob runs from flash
# instead of being pushed down a link that keeps timing out.
#
# A FriendlyElec `-sd-` image is, structurally, just a GPT plus these same
# partitions with the bootloader at sector 64 — which is exactly what this
# builds, using the images we already have on disk.
#
# Layout comes from the package's parameter.txt (mtdparts CMDLINE, 512-byte
# sectors, `size@offset(name)`), never hardcoded, so a newer package revision
# with a different layout still works.
#
# rootfs.img and userdata.img are Android sparse images (magic 0x3aff26ed) and
# are expanded with simg2img before writing.
#
# THIS PERMANENTLY ERASES THE TARGET DEVICE.
#
# Usage: sudo ./build_nanopi_sd.sh /dev/sdX [image_dir] [--yes] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV=""
IMAGE_DIR=""
AUTO_YES=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) AUTO_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    -*) echo "unknown option: $1"; exit 1 ;;
    /dev/*) DEV="$1" ;;
    *) IMAGE_DIR="$1" ;;
  esac
  shift
done

[ -n "$DEV" ] || { echo "no target device given (e.g. /dev/sdb)"; exit 1; }
if [ "$DRY_RUN" -ne 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "needs root to write a block device. Re-run: sudo $0 $*"; exit 1
fi

# ---------------------------------------------------------------------------
# Refuse to eat the wrong disk
# ---------------------------------------------------------------------------
[ -b "$DEV" ] || { echo "not a block device: $DEV"; exit 1; }
BASE="$(basename "$DEV")"
case "$BASE" in
  *[0-9]) echo "$DEV looks like a PARTITION. Pass the whole disk (e.g. /dev/sdb)."; exit 1 ;;
esac
[ -f "/sys/block/$BASE/removable" ] || { echo "can't read /sys/block/$BASE — is $DEV a whole disk?"; exit 1; }
if [ "$(cat "/sys/block/$BASE/removable")" != "1" ]; then
  echo "!! $DEV is NOT flagged removable. Refusing — this is how internal drives die."
  exit 1
fi
# Anything from this disk currently mounted at / or /home is an instant no.
while read -r part mnt; do
  case "$mnt" in
    /|/home|/boot|/boot/efi) echo "!! $part from $DEV is mounted at $mnt. Refusing."; exit 1 ;;
  esac
done < <(lsblk -nro NAME,MOUNTPOINT "$DEV" | awk 'NF==2 {print "/dev/"$1, $2}')

# /sys is world-readable; blockdev needs root, which --dry-run deliberately isn't.
DEV_SECTORS="$(cat "/sys/block/$BASE/size" 2>/dev/null || true)"
[ -n "${DEV_SECTORS:-}" ] && [ "$DEV_SECTORS" -gt 0 ] 2>/dev/null \
  || DEV_SECTORS="$(blockdev --getsz "$DEV" 2>/dev/null || echo 0)"
[ "$DEV_SECTORS" -gt 0 ] || { echo "couldn't determine the size of $DEV"; exit 1; }
DEV_GB=$((DEV_SECTORS / 2 / 1024 / 1024))

# ---------------------------------------------------------------------------
# Locate the image package
# ---------------------------------------------------------------------------
if [ -z "$IMAGE_DIR" ]; then
  IMAGE_DIR="$(find "$SCRIPT_DIR/nanopiIMAGE" -maxdepth 2 -name parameter.txt -printf '%T@ %h\n' 2>/dev/null \
                 | sort -rn | head -1 | cut -d' ' -f2-)"
fi
IMAGE_DIR="${IMAGE_DIR%/}"
[ -f "$IMAGE_DIR/parameter.txt" ] || { echo "not an RKDevTool image package (no parameter.txt): $IMAGE_DIR"; exit 1; }
IDB="$IMAGE_DIR/idbloader.img"
[ -f "$IDB" ] || { echo "missing idbloader.img — without it the BootROM has nothing to load from SD"; exit 1; }

TITLE=""
[ -f "$IMAGE_DIR/info.conf" ] && TITLE="$(sed -n 's/^title=//p' "$IMAGE_DIR/info.conf" | head -1)"
echo "== image package: $IMAGE_DIR =="
[ -n "$TITLE" ] && echo "   $TITLE"
echo "== target: $DEV  (${DEV_GB} GB, $DEV_SECTORS sectors, removable) =="

# ---------------------------------------------------------------------------
# Partition layout out of parameter.txt
# ---------------------------------------------------------------------------
CMDLINE="$(grep -m1 '^CMDLINE:' "$IMAGE_DIR/parameter.txt" || true)"
[ -n "$CMDLINE" ] || { echo "parameter.txt has no CMDLINE — can't derive the layout"; exit 1; }
MTD="${CMDLINE#*mtdparts=}"; MTD="${MTD%%[[:space:]]*}"; MTD="${MTD#*:}"

is_sparse() { [ "$(od -An -tx4 -N4 -- "$1" | tr -d ' \n')" = "ed26ff3a" ]; }
payload_bytes() {
  local blk cnt
  if is_sparse "$1"; then read -r blk cnt < <(od -An -tu4 -j12 -N8 -- "$1"); echo $((blk * cnt))
  else stat -c%s -- "$1"; fi
}
human() { if [ "$1" -ge 1048576 ]; then echo "$(($1 / 1048576)) MB"; else echo "$(($1 / 1024)) KB"; fi; }

P_NAMES=(); P_STARTS=(); P_SIZES=(); P_FILES=(); P_BYTES=()
IFS=',' read -ra RAW_PARTS <<<"$MTD"
for part in "${RAW_PARTS[@]}"; do
  [[ "$part" =~ ^(-|0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)\(([^\)]+)\)$ ]] || {
    echo "can't parse mtdparts entry: $part"; exit 1; }
  SZ="${BASH_REMATCH[1]}"; OFF=$((BASH_REMATCH[2])); NAME="${BASH_REMATCH[3]%%:*}"
  SECTORS=-1; [ "$SZ" = "-" ] || SECTORS=$((SZ))
  FILE="$IMAGE_DIR/$NAME.img"
  BYTES=0
  if [ -f "$FILE" ]; then
    BYTES="$(payload_bytes "$FILE")"
    if [ "$SECTORS" -ge 0 ] && [ "$BYTES" -gt $((SECTORS * 512)) ]; then
      echo "!! $NAME.img ($((BYTES/1024/1024)) MB) exceeds its $((SECTORS/2048)) MB partition — aborting."; exit 1
    fi
  else
    FILE=""   # declared in the table but no image ships (e.g. recovery)
  fi
  P_NAMES+=("$NAME"); P_STARTS+=("$OFF"); P_SIZES+=("$SECTORS"); P_FILES+=("$FILE"); P_BYTES+=("$BYTES")
done

# The grow partition needs a real size; leave room for the backup GPT (34 sectors).
LAST=$(( ${#P_NAMES[@]} - 1 ))
if [ "${P_SIZES[$LAST]}" -lt 0 ]; then
  P_SIZES[$LAST]=$(( DEV_SECTORS - P_STARTS[$LAST] - 34 ))
  [ "${P_SIZES[$LAST]}" -gt 0 ] || { echo "!! card too small: ${P_NAMES[$LAST]} starts past the end of $DEV"; exit 1; }
fi
NEED_SECTORS=$(( P_STARTS[$LAST] + 34 ))
if [ "$DEV_SECTORS" -lt "$NEED_SECTORS" ]; then
  echo "!! $DEV has $DEV_SECTORS sectors but the layout needs at least $NEED_SECTORS — aborting."; exit 1
fi

echo
printf '   %-10s %10s %10s %9s  %s\n' partition start sectors size file
printf '   %-10s %10s %10s %9s  %s\n' bootloader 64 - "$(human "$(stat -c%s "$IDB")")" "idbloader.img"
for i in "${!P_NAMES[@]}"; do
  printf '   %-10s %10s %10s %9s  %s%s\n' "${P_NAMES[$i]}" "${P_STARTS[$i]}" "${P_SIZES[$i]}" \
    "$( [ -n "${P_FILES[$i]}" ] && human "${P_BYTES[$i]}" || echo '-' )" \
    "$( [ -n "${P_FILES[$i]}" ] && basename "${P_FILES[$i]}" || echo '(no image — partition created empty)' )" \
    "$( [ -n "${P_FILES[$i]}" ] && is_sparse "${P_FILES[$i]}" && echo '  (sparse)' )"
done

if [ "$DRY_RUN" -eq 1 ]; then echo; echo "--dry-run: nothing written."; exit 0; fi

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to ERASE $DEV (${DEV_GB} GB) completely !!"
  read -r -p "Type 'erase' to continue: " C
  [ "$C" = "erase" ] || { echo "aborted — nothing written."; exit 1; }
fi

# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
echo
echo "== unmounting anything on $DEV =="
while read -r p; do
  [ -n "$p" ] || continue
  umount "$p" 2>/dev/null && echo "   unmounted $p" || true
done < <(lsblk -nro NAME,MOUNTPOINT "$DEV" | awk 'NF==2 {print "/dev/"$1}')

echo "== wiping old signatures + partition table =="
wipefs -a "$DEV" >/dev/null 2>&1 || true
dd if=/dev/zero of="$DEV" bs=1M count=32 conv=fsync status=none

echo "== writing GPT =="
{
  echo 'label: gpt'
  echo 'unit: sectors'
  echo 'first-lba: 34'
  for i in "${!P_NAMES[@]}"; do
    echo "start=${P_STARTS[$i]}, size=${P_SIZES[$i]}, name=\"${P_NAMES[$i]}\""
  done
} | sfdisk --no-reread --no-tell-kernel "$DEV" >/dev/null
partprobe "$DEV" 2>/dev/null || true

# The IDBlock lives at sector 64, ahead of the first partition (16384) and clear
# of the primary GPT (sectors 1-33). Written AFTER sfdisk so nothing clobbers it.
echo "== writing bootloader IDBlock → sector 64 =="
dd if="$IDB" of="$DEV" bs=512 seek=64 conv=fsync status=none

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

for i in "${!P_NAMES[@]}"; do
  FILE="${P_FILES[$i]}"
  [ -n "$FILE" ] || { echo "-- ${P_NAMES[$i]}: no image, left empty --"; continue; }
  SRC="$FILE"
  if is_sparse "$FILE"; then
    echo "-- expanding ${P_NAMES[$i]} ($(human "${P_BYTES[$i]}")) --"
    SRC="$TMP/${P_NAMES[$i]}.raw"
    simg2img "$FILE" "$SRC"
  fi
  echo "-- ${P_NAMES[$i]} → sector ${P_STARTS[$i]} ($(human "${P_BYTES[$i]}")) --"
  dd if="$SRC" of="$DEV" bs=4M oflag=seek_bytes seek=$(( P_STARTS[$i] * 512 )) conv=fsync status=progress
  [ "$SRC" = "$FILE" ] || rm -f -- "$SRC"   # reclaim before the next expansion
done

echo "== flushing =="
sync
blockdev --flushbufs "$DEV" 2>/dev/null || true

echo
echo "== partition table now on the card =="
sfdisk -l "$DEV" 2>/dev/null | sed 's/^/   /'

cat <<EOF

Done. Eject the card, put it in the R76S, and power on with NO USB cable to the
host — the BootROM should find the IDBlock at sector 64 and boot
${TITLE:-the flashed OS} from SD.

First boot does the image's own setup (filesystem resize, SSH host keys), so
give it a couple of minutes before expecting a desktop or a network answer.

If it does NOT boot, that is itself informative: the DDR blob in this package's
idbloader is the same one maskrom kept choking on, so a dead SD boot points at
the package being wrong for this board rather than at the USB link.
EOF
