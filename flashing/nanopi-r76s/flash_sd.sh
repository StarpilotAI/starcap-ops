#!/usr/bin/env bash
# flash_sd.sh — take a blank (or not) SD card and hand back one that boots the
# StarCap NanoPi R76S image. This is THE script. Run it and nothing else.
#
#   sudo ./flash_sd.sh /dev/sdX
#
# It does, in order:
#   1. refuses anything that isn't a removable whole disk
#   2. unmounts the card and destroys both GPTs (primary + backup) so no stale
#      partition table survives anywhere on it
#   3. streams the image straight out of the .zip onto the card — the 11.6 GB
#      never has to exist on your disk, only the 4.4 GB archive
#   4. grows the last partition to fill the card and resizes its filesystem
#   5. verifies what actually landed
#
# Self-contained: only needs the .zip sitting next to it. Linux only (it uses
# sgdisk/partprobe/resize2fs). On macOS or Windows use balenaEtcher on the .zip
# instead, then run `flash_sd.sh --expand-only /dev/sdX` from any Linux box —
# without step 4 the board only gets ~1.6 GB of recording space.
#
# Usage: sudo ./flash_sd.sh /dev/sdX [--yes] [image.zip|image.img]
#        sudo ./flash_sd.sh --expand-only /dev/sdX   # steps 4-5 only, on a card
#                                                    # already written (Etcher, dd)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV=""
SRC=""
AUTO_YES=0
EXPAND_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) AUTO_YES=1 ;;
    --expand-only) EXPAND_ONLY=1 ;;
    -h|--help) awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    /dev/*) DEV="$1" ;;
    *) SRC="$1" ;;
  esac
  shift
done

die() { echo "!! $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Find the image
# ---------------------------------------------------------------------------
if [ "$EXPAND_ONLY" -eq 1 ]; then
  SRC="(none: --expand-only)"; IMG_BYTES=1
else
if [ -z "$SRC" ]; then
  SRC="$(ls -1 "$SCRIPT_DIR"/*.img.zip 2>/dev/null | head -1 || true)"
  [ -n "$SRC" ] || SRC="$(ls -1 "$SCRIPT_DIR"/*.img 2>/dev/null | head -1 || true)"
fi
[ -n "$SRC" ] && [ -f "$SRC" ] || die "no .img.zip or .img found next to this script (or passed as an argument)"

case "$SRC" in
  *.zip)
    command -v unzip >/dev/null || die "need unzip (apt install unzip)"
    # One member only — a multi-file zip would make `unzip -p` concatenate them.
    MEMBERS="$(unzip -Z1 "$SRC" | wc -l)"
    [ "$MEMBERS" -eq 1 ] || die "$SRC holds $MEMBERS files; expected exactly 1 image"
    IMG_BYTES="$(unzip -l "$SRC" | awk 'NR==4{print $1}')"
    STREAM=(unzip -p "$SRC")
    ;;
  *)
    IMG_BYTES="$(stat -c%s "$SRC")"
    STREAM=(cat "$SRC")
    ;;
esac
[ "${IMG_BYTES:-0}" -gt 0 ] 2>/dev/null || die "couldn't determine the image size in $SRC"
fi

if [ -z "$DEV" ]; then
  echo "usage: sudo $0 /dev/sdX"
  echo
  echo "removable disks on this machine:"
  for d in /sys/block/*/; do
    n="$(basename "$d")"
    [ -f "$d/removable" ] && [ "$(cat "$d/removable")" = "1" ] && [ "$(cat "$d/size")" -gt 0 ] \
      && printf '   /dev/%-8s %s GB  %s\n' "$n" "$(( $(cat "$d/size") / 2 / 1024 / 1024 ))" \
           "$(cat "$d/device/model" 2>/dev/null || echo '')"
  done
  exit 1
fi

[ "$(id -u)" -eq 0 ] || die "needs root. Re-run: sudo $0 $DEV"
for t in sgdisk wipefs partprobe e2fsck resize2fs lsblk dd; do
  command -v "$t" >/dev/null || die "missing required tool: $t (apt install gdisk util-linux e2fsprogs)"
done

# ---------------------------------------------------------------------------
# Refuse to eat the wrong disk
# ---------------------------------------------------------------------------
[ -b "$DEV" ] || die "not a block device: $DEV"
BASE="$(basename "$DEV")"
case "$BASE" in
  *[0-9]) case "$BASE" in
            mmcblk*|nvme*) ;;                    # these legitimately end in a digit
            *) die "$DEV looks like a PARTITION. Pass the whole disk, e.g. /dev/sdb" ;;
          esac ;;
esac
[ -f "/sys/block/$BASE/removable" ] || die "can't read /sys/block/$BASE — is $DEV a whole disk?"
[ "$(cat "/sys/block/$BASE/removable")" = "1" ] \
  || die "$DEV is NOT flagged removable. Refusing — this is how internal drives die."
while read -r part mnt; do
  case "$mnt" in
    /|/home|/boot|/boot/efi) die "$part from $DEV is mounted at $mnt. Refusing." ;;
  esac
done < <(lsblk -nro NAME,MOUNTPOINT "$DEV" | awk 'NF==2 {print "/dev/"$1, $2}')

DEV_SECTORS="$(cat "/sys/block/$BASE/size")"
DEV_BYTES=$(( DEV_SECTORS * 512 ))
[ "$DEV_BYTES" -ge "$IMG_BYTES" ] \
  || die "$DEV is $((DEV_BYTES/1000000000)) GB but the image needs $((IMG_BYTES/1000000000+1)) GB"

partdev() { case "$DEV" in *[0-9]) echo "${DEV}p$1" ;; *) echo "${DEV}$1" ;; esac; }

echo "== source: $SRC  ($((IMG_BYTES/1048576)) MB image) =="
echo "== target: $DEV  ($((DEV_BYTES/1000000000)) GB, removable) =="
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV" | sed 's/^/   /'

if [ "$EXPAND_ONLY" -eq 1 ]; then
  : # growing the last partition keeps its data; no erase prompt
elif [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! Everything on $DEV will be destroyed. !!"
  read -r -p "Type 'erase' to continue: " C
  [ "$C" = "erase" ] || { echo "aborted — nothing written."; exit 1; }
fi

# ---------------------------------------------------------------------------
# 1. Clean the card
# ---------------------------------------------------------------------------
echo
echo "== unmounting =="
while read -r p; do
  [ -n "$p" ] || continue
  umount "$p" 2>/dev/null && echo "   unmounted $p" || true
done < <(lsblk -nro NAME,MOUNTPOINT "$DEV" | awk 'NF==2 {print "/dev/"$1}')

if [ "$EXPAND_ONLY" -eq 0 ]; then
# sgdisk -Z kills the backup GPT too. Without that, a table from a previous
# larger image survives at the far end of the card and confuses everything.
echo "== destroying old partition tables and signatures =="
sgdisk -Z "$DEV" >/dev/null 2>&1 || true
wipefs -a "$DEV" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 2. Write
# ---------------------------------------------------------------------------
echo "== writing image (this is the slow part) =="
set -o pipefail
"${STREAM[@]}" | dd of="$DEV" bs=4M status=progress conv=fsync
blockdev --flushbufs "$DEV" 2>/dev/null || true
sync
partprobe "$DEV" 2>/dev/null || true
udevadm settle 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 3. Grow the last partition to fill the card
# ---------------------------------------------------------------------------
# The image is built at its minimum size, and nothing on the board fixes that:
# parameter.txt's `userdata:grow` is honoured by upgrade_tool flashing eMMC, not
# by dd, and systemd-growfs only grows a filesystem to fill its PARTITION.
echo
echo "== expanding the last partition to fill the card =="
N="$(sgdisk -p "$DEV" 2>/dev/null | awk '/^[[:space:]]*[0-9]+[[:space:]]/ {n=$1} END{print n}')"
[ -n "${N:-}" ] || die "no GPT partitions on $DEV after writing — the flash did not take"
START="$(sgdisk -i "$N" "$DEV" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')"
NAME="$(sgdisk -i "$N" "$DEV" | sed -n "s/^Partition name: '\(.*\)'$/\1/p")"
OLD_END="$(sgdisk -i "$N" "$DEV" | sed -n 's/^Last sector: \([0-9]*\).*/\1/p')"
PART="$(partdev "$N")"

if [ "$OLD_END" -ge $(( DEV_SECTORS - 34 )) ]; then
  echo "   already fills the card"
else
  while read -r p; do
    [ -n "$p" ] || continue; umount "$p" 2>/dev/null || true
  done < <(lsblk -nro NAME,MOUNTPOINT "$DEV" | awk 'NF==2 {print "/dev/"$1}')
  sgdisk -e "$DEV" >/dev/null              # backup GPT to the end of the CARD
  sgdisk -d "$N" "$DEV" >/dev/null
  sgdisk -n "$N:$START:0" -c "$N:$NAME" -t "$N:8300" "$DEV" >/dev/null
  partprobe "$DEV" 2>/dev/null || true
  udevadm settle 2>/dev/null || true
  for _ in $(seq 1 10); do [ -b "$PART" ] && break; sleep 1; done
  [ -b "$PART" ] || die "$PART never appeared — re-plug the card and run: $0 --expand-only $DEV"
  e2fsck -fy "$PART" || true               # exit 1 just means "found and fixed"
  resize2fs "$PART"
fi
sync

# ---------------------------------------------------------------------------
# 4. Verify
# ---------------------------------------------------------------------------
echo
echo "== verifying =="
FAIL=0
# The BootROM looks for the Rockchip IDBlock at sector 64. No RKNS, no boot.
MAGIC="$(dd if="$DEV" bs=512 skip=64 count=1 status=none | head -c4)"
if [ "$MAGIC" = "RKNS" ]; then echo "   [ok]   bootloader IDBlock present at sector 64"
else echo "   [FAIL] no RKNS magic at sector 64 — this card will not boot"; FAIL=1; fi

for want in rootfs userdata; do
  if lsblk -nro LABEL "$DEV" | grep -qx "$want"; then echo "   [ok]   $want partition present"
  else echo "   [FAIL] no partition labelled $want"; FAIL=1; fi
done

TMPMNT="$(mktemp -d)"
if mount -o ro "$PART" "$TMPMNT" 2>/dev/null; then
  FREE="$(df -BG --output=avail "$TMPMNT" | tail -1 | tr -d ' G')"
  echo "   [ok]   $NAME mounts, ${FREE} GB free"
  [ "$FREE" -lt 5 ] && { echo "   [warn] very little free space — did the expand run?"; }
  if [ -d "$TMPMNT/root/home/pi/orangepi-recorder" ]; then
    echo "   [ok]   StarCap recorder present"
  else
    echo "   [FAIL] no recorder found — wrong image?"; FAIL=1
  fi
  umount "$TMPMNT" 2>/dev/null || true
else
  echo "   [FAIL] could not mount $PART"; FAIL=1
fi
rmdir "$TMPMNT" 2>/dev/null || true

echo
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEV" | sed 's/^/   /'
echo
if [ "$FAIL" -ne 0 ]; then
  echo "SOMETHING IS WRONG — see the [FAIL] lines above. Do not ship this card."
  exit 1
fi
cat <<'EOF'
Card is ready. Eject it, put it in the R76S, power on with no USB cable to a host.

If the board ignores it and boots something else: the RK3576 BootROM checks
SPI -> eMMC -> SD and takes the FIRST valid bootloader, so a board that already
has a working bootloader on eMMC will never look at this card. That is a board
setup problem, not a bad card.

First boot takes a couple of minutes (SSH host keys, filesystem checks) before it
answers on the network. Login user: pi
EOF
