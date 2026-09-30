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
# ---------------------------------------------------------------------------
# WHY THIS SCRIPT ZEROES RAW SECTORS AND DOESN'T JUST `wipefs`
#
# A Rockchip boot card keeps its bootloader (idbloader) at raw LBA sector 64 —
# 32 KB in, in the dead space between the partition table (sector 0, plus GPT
# entries through sector 33) and the first partition (1 MiB / sector 2048).
# That region belongs to no partition and no filesystem, so NOTHING that
# operates at the filesystem or partition-table level removes it:
#   wipefs -a       clears fs/PT *signatures* only        -> sector 64 survives
#   parted mklabel  rewrites sectors 0-33                 -> sector 64 survives
#   mkfs.*          writes inside the partition (>=2048)  -> sector 64 survives
#   Windows format  rewrites sector 0 + the partition     -> sector 64 survives
#
# The RK3576 BootROM boot order probes the SD slot before eMMC. It reads sector
# 64, and if it finds the Rockchip ID block magic it COMMITS to booting that
# card — it does not verify the rest of the chain, and there is no fallback
# after a successful handoff. So a card that was once a boot card, and has
# since been "formatted clean", still hijacks the BootROM: the stale loader
# comes up, finds no OS on the card, and hangs forever without ever trying the
# eMMC. Symptom: the board boots fine with the slot empty and is dead with the
# (apparently blank) card inserted.
#
# Zeroing the head is therefore the whole point of this script, not a
# nicety. We also zero the tail, because a Rockchip GPT image leaves a BACKUP
# GPT header in the last sectors, and partitioning tools will happily offer to
# resurrect the old table from it.
# ---------------------------------------------------------------------------
#
# THIS PERMANENTLY ERASES EVERYTHING ON THE TARGET DEVICE.
#
# Usage:  ./format_sd_storage.sh /dev/sdX [--label NAME] [--yes]
#         ./format_sd_storage.sh /dev/sdX --check
#   /dev/sdX      the WHOLE disk device (not a partition, e.g. /dev/sdb not
#                 /dev/sdb1) — find it with `lsblk` first
#   --label NAME  ext4 volume label (default: starcap_storage)
#   --yes         skip the confirmation prompt
#   --check       read-only: report whether the card would hijack a NanoPi's
#                 boot, and change nothing. Safe to run on any card.
set -euo pipefail

# how much of the head to zero. Sector 64 is all the BootROM reads, but a full
# Rockchip image also lays down uboot/misc/dtbo/resource (8-20 MiB per the
# golden image's parameter.txt), so 64 MiB clears the lot for a few seconds of
# writing and leaves the card unambiguously not-a-boot-card.
HEAD_WIPE_MIB=64
TAIL_WIPE_MIB=4          # enough to take out the backup GPT header
LOADER_SKIP=64           # first sector the BootROM reads
LOADER_COUNT=1984        # ...through sector 2047, i.e. up to the 1 MiB mark

DEV=""
LABEL="starcap_storage"
AUTO_YES=0
CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes)   AUTO_YES=1 ;;
    --check) CHECK_ONLY=1 ;;
    --label) shift; LABEL="${1:?--label needs a value}" ;;
    -*)      echo "unknown option: $1"; exit 1 ;;
    *)       DEV="$1" ;;
  esac
  shift
done

[ -n "$DEV" ] || { echo "usage: $0 /dev/sdX [--label NAME] [--yes] [--check]"; echo; lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT; exit 1; }
[ -b "$DEV" ] || { echo "not a block device: $DEV"; exit 1; }

# refuse a partition handed in by mistake (/dev/sdb1) — we need the whole disk
if [ "$(lsblk -no TYPE "$DEV" | head -1)" != "disk" ]; then
  echo "!! $DEV is not a whole disk (looks like a partition) — pass e.g. /dev/sdb, not /dev/sdb1"; exit 1
fi

# refuse anything that looks like this machine's own disk: the disk backing /,
# or any partition of DEV currently mounted anywhere under a live system path
ROOT_SRC="$(findmnt -no SOURCE / 2>/dev/null || true)"
ROOT_DISK="/dev/$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null || true)"
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

# ---------------------------------------------------------------------------
# read-only inspection: is there a stale Rockchip loader in the boot region?
# Returns 0 if clean, 1 if the card would hijack a NanoPi's BootROM.
loader_bytes() {
  sudo dd if="$DEV" bs=512 skip="$LOADER_SKIP" count="$LOADER_COUNT" status=none 2>/dev/null \
    | tr -d '\0' | wc -c
}

echo "== target: $DEV =="
lsblk "$DEV" -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo
  echo "== read-only check of the BootROM region (sectors ${LOADER_SKIP}-$((LOADER_SKIP+LOADER_COUNT-1))) =="
  NZ="$(loader_bytes)"
  if [ "$NZ" -eq 0 ]; then
    echo "CLEAN — $NZ non-zero bytes. A NanoPi will skip this card and boot from eMMC."
    exit 0
  fi
  echo "DIRTY — $NZ non-zero bytes found. This card WILL hijack the BootROM and hang the board."
  echo "        First 128 bytes at sector $LOADER_SKIP:"
  sudo dd if="$DEV" bs=512 skip="$LOADER_SKIP" count=1 status=none | hexdump -C | head -8
  echo
  echo "        Fix: rerun without --check to zero it and reformat."
  exit 1
fi

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to WIPE $DEV completely and reformat as one ext4 partition (label: $LABEL) !!"
  read -r -p "Type 'format' to continue: " CONFIRM
  [ "$CONFIRM" = "format" ] || { echo "aborted — nothing written."; exit 1; }
fi

echo "== unmounting any mounted partitions on $DEV =="
for p in "${DEV}"*[0-9]; do
  [ -b "$p" ] || continue
  udisksctl unmount -b "$p" >/dev/null 2>&1 || sudo umount "$p" 2>/dev/null || true
done

echo "== clearing partition-table / filesystem signatures =="
sudo wipefs -a "$DEV" >/dev/null

# --- the part that actually stops the board hanging -------------------------
DEV_BYTES="$(sudo blockdev --getsize64 "$DEV")"
DEV_MIB=$(( DEV_BYTES / 1048576 ))
echo "== zeroing first ${HEAD_WIPE_MIB} MiB (kills the sector-64 idbloader) =="
sudo dd if=/dev/zero of="$DEV" bs=1M count="$HEAD_WIPE_MIB" conv=fsync status=none

if [ "$DEV_MIB" -gt $(( HEAD_WIPE_MIB + TAIL_WIPE_MIB )) ]; then
  echo "== zeroing last ${TAIL_WIPE_MIB} MiB (kills the backup GPT header) =="
  sudo dd if=/dev/zero of="$DEV" bs=1M count="$TAIL_WIPE_MIB" \
       seek=$(( DEV_MIB - TAIL_WIPE_MIB )) conv=fsync status=none
fi
sudo blockdev --rereadpt "$DEV" 2>/dev/null || true
udevadm settle 2>/dev/null || true
# ---------------------------------------------------------------------------

echo "== creating a single GPT partition spanning the whole disk =="
sudo parted -s "$DEV" mklabel gpt mkpart primary ext4 0% 100%
sleep 1
udevadm settle 2>/dev/null || true

PART="${DEV}1"
[ -b "$PART" ] || PART="${DEV}p1"          # nvme-style naming, just in case
[ -b "$PART" ] || { echo "!! couldn't find the new partition (looked for ${DEV}1 / ${DEV}p1)"; exit 1; }

echo "== formatting $PART as ext4 (label: $LABEL) =="
sudo mkfs.ext4 -F -L "$LABEL" "$PART" >/dev/null
sync

# verify the boot region really is clear — GPT only writes sectors 0-33 and the
# partition starts at 2048, so a correct run leaves this untouched at zero. If
# this ever trips, the card will hang a NanoPi and must not be shipped.
echo "== verifying the BootROM region is clear =="
NZ="$(loader_bytes)"
if [ "$NZ" -ne 0 ]; then
  echo "!! FAILED: $NZ non-zero bytes still in sectors ${LOADER_SKIP}-$((LOADER_SKIP+LOADER_COUNT-1))."
  echo "!! This card would still hijack a NanoPi's BootROM. Do not use it."
  exit 1
fi
echo "   clean — 0 non-zero bytes in sectors ${LOADER_SKIP}-$((LOADER_SKIP+LOADER_COUNT-1))."

cat <<EOF

Done. $PART is now a plain ext4 storage card labeled '$LABEL'.

The BootROM region is verified empty, so a NanoPi will skip this card at boot
and come up from its eMMC as normal — with the card inserted or not.

Insert it into any eMMC-booted board — the udev automount rule already in
place (starcap-sdcard-automount.sh) picks it up automatically, mounts it at
/media/sdcard, and hands ownership to the orangepi user. No further steps
needed on the board itself.
EOF
