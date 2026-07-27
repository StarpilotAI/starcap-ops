#!/usr/bin/env bash
# format_sd_storage.sh — reformat an old recorder SD card (previously an OS
# boot card) into a plain ext4 storage card for eMMC-booted boards.
#
# eMMC-booted boards use their SD-card slot purely as recording storage now
# (see orangepi-recorder's storage_ready()/StorageWatchdog + this repo's
# starcap-sdcard-automount.sh) — the OS lives on the eMMC, so the card no
# longer needs a bootloader/reserved boot area, just one big data partition.
#
# Why ext4 (not exFAT/FAT32): the automount script already prefers ext4 (real
# chown-based ownership, no uid=/gid=/umask= workaround needed), no per-file
# size limit, and better crash resilience for a card that gets yanked mid-write
# in the field (ext4's journal + the automount script's fsck -p retry). Nothing
# in this pipeline (the recorder Pis, this laptop) is ever Windows/Mac, so
# exFAT's cross-platform advantage doesn't buy anything here.
#
# THIS PERMANENTLY ERASES EVERYTHING ON THE TARGET DEVICE.
#
# Usage:  ./format_sd_storage.sh /dev/sdX [--label NAME] [--yes]
#   /dev/sdX      the WHOLE disk device (not a partition, e.g. /dev/sdb not
#                 /dev/sdb1) — find it with `lsblk` first
#   --label NAME  ext4 volume label (default: starcap_storage)
#   --yes         skip the confirmation prompt
set -euo pipefail

DEV=""
LABEL="starcap_storage"
AUTO_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) AUTO_YES=1 ;;
    --label) NEXT_IS_LABEL=1 ;;
    *)
      if [ "${NEXT_IS_LABEL:-0}" = "1" ]; then LABEL="$arg"; NEXT_IS_LABEL=0
      else DEV="$arg"; fi
      ;;
  esac
done

[ -n "$DEV" ] || { echo "usage: $0 /dev/sdX [--label NAME] [--yes]"; echo; lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT; exit 1; }
[ -b "$DEV" ] || { echo "not a block device: $DEV"; exit 1; }

# refuse anything that looks like this machine's own disk: the disk backing /,
# or any partition of DEV currently mounted anywhere under a live system path
ROOT_DISK="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null || true)"
if [ "$DEV" = "$ROOT_DISK" ]; then
  echo "!! $DEV looks like THIS MACHINE'S OWN DISK (backs /) — refusing."; exit 1
fi
for p in "${DEV}"*[0-9]; do
  [ -b "$p" ] || continue
  mp="$(findmnt -no TARGET "$p" 2>/dev/null || true)"
  if echo "$mp" | grep -qE '^/(boot|home|var|usr|etc)(/|$)|^/$'; then
    echo "!! $p is mounted at '$mp' — looks like a system path, refusing."; exit 1
  fi
done

echo "== target: $DEV =="
lsblk "$DEV" -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to WIPE $DEV completely and reformat as one ext4 partition (label: $LABEL) !!"
  read -r -p "Type 'format' to continue: " CONFIRM
  [ "$CONFIRM" = "format" ] || { echo "aborted — nothing written."; exit 1; }
fi

echo "== unmounting any mounted partitions on $DEV =="
for p in "${DEV}"*[0-9]; do
  [ -b "$p" ] || continue
  sudo umount "$p" 2>/dev/null || true
done

echo "== wiping existing partition table/filesystem signatures =="
sudo wipefs -a "$DEV"

echo "== creating a single GPT partition spanning the whole disk =="
sudo parted -s "$DEV" mklabel gpt mkpart primary ext4 0% 100%
sleep 1
udevadm settle 2>/dev/null || true

PART="${DEV}1"
[ -b "$PART" ] || PART="${DEV}p1"          # nvme-style naming, just in case
[ -b "$PART" ] || { echo "!! couldn't find the new partition (looked for ${DEV}1 / ${DEV}p1)"; exit 1; }

echo "== formatting $PART as ext4 (label: $LABEL) =="
sudo mkfs.ext4 -F -L "$LABEL" "$PART"

cat <<EOF

Done. $PART is now a plain ext4 storage card labeled '$LABEL'.
Insert it into any eMMC-booted board — the udev automount rule already in
place (starcap-sdcard-automount.sh) picks it up automatically, mounts it at
/media/sdcard, and hands ownership to the orangepi user. No further steps
needed on the board itself.
EOF
