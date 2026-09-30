#!/usr/bin/env bash
# build_nanopi_sd_image.sh — build a single flat, SD-card-flashable .img from a
# FriendlyElec RK3576 per-partition image package (the same one flash_nanopi_emmc.sh
# pushes over maskrom).
#
# HOW THIS DIFFERS FROM build_nanopi_sd.sh
# ----------------------------------------
# build_nanopi_sd.sh writes directly to a block device — you must have the card
# in this machine at build time, and it needs root. This one writes a FILE. The
# result is an ordinary disk image you can hand to someone and they flash it with
# balenaEtcher / Raspberry Pi Imager / dd, no Rockchip tooling and no maskrom.
# Because the target is a regular file, THIS SCRIPT DOES NOT NEED ROOT.
#
# WHY A FLAT IMAGE IS POSSIBLE AT ALL
# -----------------------------------
# A Rockchip "image" is normally one file per partition plus a parameter.txt
# describing the layout — there is no single blob. But an SD card is just that
# layout materialised: GPT at sector 0, the bootloader IDBlock at sector 64
# (where the BootROM looks), and each partition's payload at its own offset. So
# a flat .img is exactly the per-partition set assembled at the right offsets.
# That is what this builds.
#
# Layout is parsed from the package's parameter.txt (mtdparts CMDLINE, 512-byte
# sectors, `size@offset(name)`) — never hardcoded, so a newer package revision
# with a different layout still works.
#
# rootfs.img is an Android sparse image (magic 0x3aff26ed) and is expanded with
# simg2img into a scratch file, written, then deleted before the next expansion
# so peak disk use stays bounded.
#
# THE LAST PARTITION DOES NOT AUTO-GROW.
# parameter.txt marks it `userdata:grow`, which upgrade_tool honours when it
# flashes eMMC — it stretches the partition to fill the chip. Nothing does that
# for a dd'd card: the GPT in this file says userdata ends where its payload
# ends, and systemd-growfs only grows a FILESYSTEM to fill its PARTITION, never
# the partition itself. So the image is built at its minimum size and the rest
# of the card is left unallocated. Run `flash_sd.sh --expand-only /dev/sdX` on
# the card afterwards to claim it. This matters here: userdata ships with only ~1.6 GB free.
#
# Usage:
#   ./build_nanopi_sd_image.sh [-o out.img] [image_dir] [--zip] [--dry-run]
#
#   -o PATH     output image (default: ./nanopi-r76s-starcap-sd-<date>.img)
#   image_dir   package dir containing parameter.txt (default: newest under
#               ./nanopiIMAGE)
#   --zip       also produce PATH.zip containing the image alone — one file, so
#               balenaEtcher and Raspberry Pi Imager can read it directly
#   --dry-run   print the plan and exit without writing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=""
IMAGE_DIR=""
DO_ZIP=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="${2:?-o needs a path}"; shift ;;
    --zip) DO_ZIP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    -*) echo "unknown option: $1"; exit 1 ;;
    *) IMAGE_DIR="$1" ;;
  esac
  shift
done

for t in simg2img sfdisk dd truncate od stat; do
  command -v "$t" >/dev/null || { echo "missing required tool: $t"; exit 1; }
done
[ "$DO_ZIP" -eq 1 ] && { command -v zip >/dev/null || { echo "missing: zip"; exit 1; }; }

# ---------------------------------------------------------------------------
# Locate the image package. Prefer the golden set over the stock vendor one.
# ---------------------------------------------------------------------------
if [ -z "$IMAGE_DIR" ]; then
  for root in "$SCRIPT_DIR/nanopiIMAGE"; do
    [ -d "$root" ] || continue
    IMAGE_DIR="$(find "$root" -maxdepth 2 -name parameter.txt -printf '%T@ %h\n' 2>/dev/null \
                   | sort -rn | head -1 | cut -d' ' -f2-)"
    [ -n "$IMAGE_DIR" ] && break
  done
fi
IMAGE_DIR="${IMAGE_DIR%/}"
[ -n "$IMAGE_DIR" ] && [ -f "$IMAGE_DIR/parameter.txt" ] \
  || { echo "not an RKDevTool image package (no parameter.txt): ${IMAGE_DIR:-<none found>}"; exit 1; }

IDB="$IMAGE_DIR/idbloader.img"
[ -f "$IDB" ] || { echo "missing idbloader.img — without it the BootROM has nothing to load from SD"; exit 1; }

[ -n "$OUT" ] || OUT="$PWD/nanopi-r76s-starcap-sd-$(date +%Y%m%d).img"
OUT_DIR="$(cd "$(dirname "$OUT")" && pwd)"; OUT="$OUT_DIR/$(basename "$OUT")"

TITLE=""
[ -f "$IMAGE_DIR/info.conf" ] && TITLE="$(sed -n 's/^title=//p' "$IMAGE_DIR/info.conf" | head -1)"
echo "== image package: $IMAGE_DIR =="
[ -n "$TITLE" ] && echo "   $TITLE"
echo "== output: $OUT =="

# ---------------------------------------------------------------------------
# Partition layout out of parameter.txt
# ---------------------------------------------------------------------------
CMDLINE="$(grep -m1 '^CMDLINE:' "$IMAGE_DIR/parameter.txt" || true)"
[ -n "$CMDLINE" ] || { echo "parameter.txt has no CMDLINE — can't derive the layout"; exit 1; }
MTD="${CMDLINE#*mtdparts=}"; MTD="${MTD%%[[:space:]]*}"; MTD="${MTD#*:}"

is_sparse() { [ "$(od -An -tx4 -N4 -- "$1" | tr -d ' \n')" = "ed26ff3a" ]; }
payload_bytes() {
  local blk cnt
  # sparse header: blk_sz @ offset 12, total_blks @ offset 16
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

# The `grow` partition has no size in the table. On a card upgrade_tool would
# stretch it to the chip; in a file the only sane size is its payload, rounded
# up to a sector. Anything larger is dead weight in the download the friend gets.
LAST=$(( ${#P_NAMES[@]} - 1 ))
if [ "${P_SIZES[$LAST]}" -lt 0 ]; then
  [ "${P_BYTES[$LAST]}" -gt 0 ] || { echo "!! last partition ${P_NAMES[$LAST]} is 'grow' but ships no image — can't size the file"; exit 1; }
  P_SIZES[$LAST]=$(( (P_BYTES[LAST] + 511) / 512 ))
fi

# Whole-image size: end of the last partition + room for the backup GPT (33
# sectors + the header), rounded up to 1 MiB so the file lands on a clean boundary.
END=$(( P_STARTS[LAST] + P_SIZES[LAST] + 34 ))
IMG_SECTORS=$(( (END + 2047) / 2048 * 2048 ))
IMG_BYTES=$(( IMG_SECTORS * 512 ))

echo
printf '   %-10s %10s %10s %9s  %s\n' partition start sectors size file
printf '   %-10s %10s %10s %9s  %s\n' bootloader 64 - "$(human "$(stat -c%s "$IDB")")" "idbloader.img"
for i in "${!P_NAMES[@]}"; do
  printf '   %-10s %10s %10s %9s  %s%s\n' "${P_NAMES[$i]}" "${P_STARTS[$i]}" "${P_SIZES[$i]}" \
    "$( [ -n "${P_FILES[$i]}" ] && human "${P_BYTES[$i]}" || echo '-' )" \
    "$( [ -n "${P_FILES[$i]}" ] && basename "${P_FILES[$i]}" || echo '(no image — partition created empty)' )" \
    "$( [ -n "${P_FILES[$i]}" ] && is_sparse "${P_FILES[$i]}" && echo '  (sparse)' )"
done
echo
echo "   image size: $IMG_SECTORS sectors = $((IMG_BYTES / 1048576)) MB  (needs a >= $(( (IMG_BYTES / 1000000000) + 1 )) GB card)"

# Scratch for simg2img lives beside the output, so it shares that filesystem's
# free space — the check below is meaningful only if both are on one mount.
BIGGEST_SPARSE=0
for i in "${!P_NAMES[@]}"; do
  [ -n "${P_FILES[$i]}" ] || continue
  if is_sparse "${P_FILES[$i]}" && [ "${P_BYTES[$i]}" -gt "$BIGGEST_SPARSE" ]; then
    BIGGEST_SPARSE="${P_BYTES[$i]}"
  fi
done
NEED=$(( IMG_BYTES + BIGGEST_SPARSE ))
[ "$DO_ZIP" -eq 1 ] && NEED=$(( NEED + IMG_BYTES / 2 ))
AVAIL="$(df -B1 --output=avail "$OUT_DIR" | tail -1 | tr -d ' ')"
echo "   peak disk needed: ~$((NEED / 1048576)) MB   available here: $((AVAIL / 1048576)) MB"
if [ "$AVAIL" -lt "$NEED" ]; then
  echo "!! not enough free space on $OUT_DIR — aborting before writing anything."; exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then echo; echo "--dry-run: nothing written."; exit 0; fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
TMP="$(mktemp -d -p "$OUT_DIR" .sdimg.XXXXXX)"
PARTIAL=0
cleanup() {
  rm -rf -- "$TMP"
  # A half-written image is worse than none — it looks flashable and isn't.
  [ "$PARTIAL" -eq 1 ] && { echo "!! build failed — removing partial $OUT"; rm -f -- "$OUT"; }
}
trap cleanup EXIT
PARTIAL=1

echo
echo "== allocating $((IMG_BYTES / 1048576)) MB =="
rm -f -- "$OUT"
truncate -s "$IMG_BYTES" "$OUT"

echo "== writing GPT =="
{
  echo 'label: gpt'
  echo 'unit: sectors'
  echo 'first-lba: 34'
  for i in "${!P_NAMES[@]}"; do
    echo "start=${P_STARTS[$i]}, size=${P_SIZES[$i]}, name=\"${P_NAMES[$i]}\""
  done
} | sfdisk --no-reread --no-tell-kernel "$OUT" >/dev/null

# The IDBlock lives at sector 64 — ahead of the first partition (16384) and clear
# of the primary GPT (sectors 1-33). Written AFTER sfdisk so nothing clobbers it.
echo "== writing bootloader IDBlock -> sector 64 =="
dd if="$IDB" of="$OUT" bs=512 seek=64 conv=notrunc,fsync status=none

for i in "${!P_NAMES[@]}"; do
  FILE="${P_FILES[$i]}"
  [ -n "$FILE" ] || { echo "-- ${P_NAMES[$i]}: no image, left empty --"; continue; }
  SRC="$FILE"
  if is_sparse "$FILE"; then
    echo "-- expanding ${P_NAMES[$i]} ($(human "${P_BYTES[$i]}")) --"
    SRC="$TMP/${P_NAMES[$i]}.raw"
    simg2img "$FILE" "$SRC"
  fi
  echo "-- ${P_NAMES[$i]} -> sector ${P_STARTS[$i]} ($(human "${P_BYTES[$i]}")) --"
  # conv=notrunc is what keeps dd from truncating the image at the write offset.
  dd if="$SRC" of="$OUT" bs=4M oflag=seek_bytes seek=$(( P_STARTS[$i] * 512 )) \
     conv=notrunc,fsync status=progress
  [ "$SRC" = "$FILE" ] || rm -f -- "$SRC"   # reclaim before the next expansion
done

sync
PARTIAL=0

echo
echo "== partition table in the image =="
sfdisk -l "$OUT" 2>/dev/null | sed 's/^/   /'

if [ "$DO_ZIP" -eq 1 ]; then
  echo
  echo "== zipping (single file inside, so Etcher/Imager can read it directly) =="
  rm -f -- "$OUT.zip"
  # -j so the archive holds a bare filename with no leading path.
  zip -j -6 "$OUT.zip" "$OUT"
  unzip -tq "$OUT.zip" && echo "   archive verified"
  ls -la "$OUT.zip"
fi

echo
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Flash with balenaEtcher / Raspberry Pi Imager, or:"
echo "   sudo dd if=$OUT of=/dev/sdX bs=4M status=progress conv=fsync"
echo "Then reclaim the rest of the card:  sudo ./flash_sd.sh --expand-only /dev/sdX"
