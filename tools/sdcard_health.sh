#!/usr/bin/env bash
# sdcard_health.sh [device] [--full]   — READ-ONLY health check of an SD card in the
# ============================================================================
# PC's USB card reader. For a card PULLED FROM A PI (e.g. a suspect one) — confirms
# whether the card itself is failing, without needing a pi. It NEVER writes to the
# card, so footage on it is safe.
#
# It reports: which device, size/vendor, read speed, and I/O errors seen while reading.
#   default : quick — read speed + sample reads at start/middle/end + kernel I/O errors
#   --full  : full-surface read-only scan (badblocks) — thorough but slow (~size/speed)
#
# NOTE: the pi's SD *port* mode (SDR104 / 1.8V / clock / copy-speed) CANNOT be seen
# through a USB reader — that's the pi's mmc slot. Use sdcard_probe.sh on a pi for that.
#   Usage:  ./sdcard_health.sh              # auto-detect the card in the reader
#           ./sdcard_health.sh /dev/sdX     # be explicit
#           ./sdcard_health.sh --full       # + full surface scan
# ============================================================================
FULL=""; DEV=""
for a in "$@"; do case "$a" in --full) FULL=1;; /dev/*) DEV="$a";; esac; done

# --- find the card (exclude the known fixed drives) --------------------------
if [ -z "$DEV" ]; then
  mapfile -t cands < <(lsblk -dn -o NAME,TRAN,MODEL 2>/dev/null \
    | awk '$2=="usb" && $0 !~ /WDC|KINGSTON|Samsung|Extreme|My Book|SNV3S/ {print $1}')
  if [ "${#cands[@]}" -eq 0 ]; then
    echo "No SD card found in a USB reader."
    echo "Plug the card into a USB SD reader and re-run (or pass /dev/sdX)."; exit 1
  elif [ "${#cands[@]}" -gt 1 ]; then
    echo "Multiple candidate devices — pass one explicitly:"; printf '  /dev/%s\n' "${cands[@]}"; exit 1
  fi
  DEV="/dev/${cands[0]}"
fi
[ -b "$DEV" ] || { echo "$DEV is not a block device."; exit 1; }

echo "=== card device: $DEV ==="
lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINT "$DEV" | sed 's/^/  /'
size_b=$(blockdev --getsize64 "$DEV" 2>/dev/null || cat /sys/block/$(basename "$DEV")/size 2>/dev/null | awk '{print $1*512}')
awk "BEGIN{printf \"  size: %.1f GB\n\", $size_b/1e9}"
echo "  usb: $(udevadm info -q property -n "$DEV" 2>/dev/null | sed -n 's/^ID_VENDOR=//p;s/^ID_MODEL=//p' | paste -sd' / ' -)"

# --- baseline dmesg error count, then read tests -----------------------------
errs0=$(dmesg 2>/dev/null | grep -icE "I/O error.*$(basename "$DEV")|blk_update_request.*$(basename "$DEV")|critical medium|unrecovered read")

echo "=== cold sequential read speed (first 1 GB) ==="
sync 2>/dev/null; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
dd if="$DEV" of=/dev/null bs=1M count=1024 2>&1 | grep -iE "copied|MB/s" | sed 's/^/  /'

if [ -n "$FULL" ]; then
  echo "=== FULL read-only surface scan (badblocks) — this takes a while ==="
  if command -v badblocks >/dev/null; then
    sudo badblocks -sv -b 4096 "$DEV" 2>&1 | tail -6 | sed 's/^/  /'
  else
    echo "  badblocks not installed — full read via dd instead:"
    sudo dd if="$DEV" of=/dev/null bs=4M 2>&1 | grep -iE "copied|error|MB/s" | sed 's/^/  /'
  fi
else
  echo "=== quick sample reads (start / middle / end) — surfaces gross failures fast ==="
  mid=$(( size_b/2/1048576 )); endo=$(( size_b/1048576 - 512 ))
  for off in 0 "$mid" "$endo"; do
    r=$(dd if="$DEV" of=/dev/null bs=1M count=512 skip="$off" 2>&1 | grep -oE "[0-9.]+ [MG]B/s" | tail -1)
    echo "  @ ${off}MB: ${r:-READ FAILED}"
  done
fi

# --- verdict -----------------------------------------------------------------
errs1=$(dmesg 2>/dev/null | grep -icE "I/O error.*$(basename "$DEV")|blk_update_request.*$(basename "$DEV")|critical medium|unrecovered read")
new_errs=$(( errs1 - errs0 ))
echo "=== I/O errors during this scan: $new_errs ==="
dmesg 2>/dev/null | grep -iE "I/O error.*$(basename "$DEV")|blk_update_request.*$(basename "$DEV")|critical medium|unrecovered read" | tail -4 | sed 's/^/  /'
echo "----------------------------------------------------------------"
if [ "$new_errs" -gt 0 ]; then
  echo "  VERDICT: SUSPECT ⚠️  — $new_errs read I/O errors. This card is failing; retire it."
else
  echo "  VERDICT: no read errors seen$([ -z "$FULL" ] && echo ' (quick scan — run --full for a definitive whole-card scan)')."
fi