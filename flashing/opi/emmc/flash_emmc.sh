#!/usr/bin/env bash
# flash_emmc.sh — write an image to an Orange Pi 5B eMMC over USB, then PROVE it landed.
#
#     ./flash_emmc.sh --probe        # read-only: what board / how big is the eMMC?
#     ./flash_emmc.sh                # newest .img beside this script
#     ./flash_emmc.sh some.img       # a specific image
#     ./flash_emmc.sh --yes some.img # no confirmation prompt
#
# NO SUDO NEEDED on this laptop: the packaged udev rule puts the board's USB node
# in group `rkdeveloptool` and the desktop session adds an ACL for the logged-in
# user. This script therefore does NOT demand uid 0 — it tests whether it can
# actually talk to the device, which is the thing that matters.
#
# ---------------------------------------------------------------------------
# GETTING THE BOARD INTO A MODE THIS CAN TALK TO
#
#   Board RUNNING an OS  -> no button needed:  adb reboot loader
#                           (lsusb shows 2207:0006 when it is running Android)
#   Board off / wedged   -> MaskRom button:
#       unplug DC *and* USB-C, wait 10s, hold MaskRom, apply DC while holding,
#       hold 2-3s more, release, then plug USB-C into the laptop.
#
# `lsusb` should then show 2207:350b. Both "Maskrom" and "Loader" are writable.
#
# ---------------------------------------------------------------------------
# FIVE TRAPS THIS SCRIPT HANDLES, ALL OF WHICH COST A DAY TO FIND
#
# 1. THE BOARD IS BACK-POWERED OVER USB-C.  Pulling DC while the USB cable is in
#    does NOT switch it off — the laptop keeps it alive, so a "cold power-cycle"
#    isn't cold and a failed boot proves nothing. Watch the USB device NUMBER in
#    `lsusb`: a genuine reset always increments it. Same number = never reset.
#
# 2. A VENDOR LOADER READS 0xCC PAST 32 MiB.  Two different things answer in
#    "Loader" mode: rk3588_spl_loader.bin (full range), and the board's OWN
#    loader — what `adb reboot loader` gives you — which serves only the first
#    32 MiB and returns 0xCC filler for every address beyond. The write is fine;
#    the READ lies. Verification then reports MISMATCH from sector 65536 (=32 MiB)
#    and it reads exactly like the notorious "this eMMC only kept 32 MiB" fault.
#    This script probes for it and swaps to the good loader via reboot-maskrom
#    (`boot` is maskrom-only: a vendor loader answers "does not support").
#
# 3. A SURVIVING BACKUP GPT MAKES A PERFECT FLASH UNBOOTABLE.  A factory board
#    has Android on it = GPT, whose BACKUP copy lives in the LAST sector of the
#    disk. An image smaller than the eMMC never overwrites it. If your image is
#    MBR, U-Boot sees an invalid primary, reads the last LBA, and boots ANDROID's
#    partition table ("*** Using Backup GPT ***"). Linux is immune (it needs a
#    0xEE protective MBR first); U-Boot is not, and U-Boot boots the board.
#    -> zeroed below, then re-read to confirm.
#
# 4. `rkdeveloptool boot` TWICE HANGS FOREVER.  Downloading a loader onto a board
#    that already has one live makes it re-enumerate mid-transfer and strands the
#    USB handle. Checked before downloading.
#
# 5. `pkill -f rkdeveloptool` KILLS YOUR OWN SHELL — the pattern matches the
#    command line that contains it. Use `pkill -x`.
#
# AND: `rkdeveloptool reset` is NOT a boot test. It frequently re-enters maskrom,
# which looks exactly like a failed flash. Judge a flash on a COLD power-cycle
# with the USB-C unplugged. This script deliberately does not reset.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOADER="$HERE/rk3588_spl_loader.bin"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YEL=""; BLD=""; OFF=""; }
say(){ printf '\n%s== %s ==%s\n' "$BLD" "$*" "$OFF"; }
ok(){  printf '  %s[ok]%s   %s\n' "$GRN" "$OFF" "$*"; }
warn(){ printf '  %s[warn]%s %s\n' "$YEL" "$OFF" "$*"; }
die(){ printf '%serror:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

IMAGE=""; AUTO_YES=0; PROBE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)  AUTO_YES=1 ;;
    --probe)   PROBE=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*)        die "unknown option: $1" ;;
    *)         IMAGE="$1" ;;
  esac
  shift
done

command -v rkdeveloptool >/dev/null || die "missing rkdeveloptool — sudo apt-get install -y rkdeveloptool"
[ -f "$LOADER" ] || die "missing loader: $LOADER"

# ---------------------------------------------------------------- find the board
say "looking for the board (2207:350b)"
for _ in $(seq 1 20); do
  rkdeveloptool list 2>/dev/null | grep -qiE 'maskrom|loader' && break
  sleep 1
done
rkdeveloptool list 2>/dev/null | grep -qiE 'maskrom|loader' || {
  cat <<'EOF'
  No board in maskrom or loader mode after 20s.

  If the board is RUNNING you do not need the MaskRom button:
      adb devices && adb reboot loader

  Otherwise: unplug DC *and* USB-C, wait 10s, hold MaskRom, apply DC while
  holding, hold 2-3s more, release, then plug USB-C into this machine.
EOF
  exit 1
}
rkdeveloptool list | sed 's/^/  /'

# ---------------------------------------------------------------- loader (trap 4)
say "loader"
if rkdeveloptool read-flash-info 2>/dev/null | grep -q 'Flash Size'; then
  ok "a loader is already live — not re-downloading (that would hang; see trap 4)"
else
  rkdeveloptool boot "$LOADER" >/dev/null 2>&1 || die "loader failed to start"
  sleep 3
  ok "downloaded $(basename "$LOADER") to RAM (nothing written to flash)"
fi

# ---------------------------------------------------------------- trap 2
loader_full_range(){
  local t; t="$(mktemp)"; local rc=1
  if rkdeveloptool read 65536 4096 "$t" >/dev/null 2>&1; then
    python3 - "$t" <<'PY' && rc=0 || rc=1
import sys
b=open(sys.argv[1],'rb').read()
sys.exit(1 if (b and b==b'\xcc'*len(b)) else 0)
PY
  fi
  rm -f "$t"; return $rc
}
say "checking the loader can reach the whole eMMC"
if loader_full_range; then
  ok "reads past 32 MiB return real data"
else
  warn "this loader returns 0xCC past 32 MiB (vendor loader) — swapping it out"
  rkdeveloptool reboot-maskrom >/dev/null 2>&1 || die "could not reboot into maskrom"
  for _ in $(seq 1 15); do rkdeveloptool list 2>/dev/null | grep -qi maskrom && break; sleep 1; done
  rkdeveloptool boot "$LOADER" >/dev/null 2>&1 || die "could not download $LOADER"
  sleep 3
  loader_full_range && ok "now on $(basename "$LOADER") — full range OK" \
    || die "still cannot read past 32 MiB — this one may be a real eMMC fault"
fi

# ---------------------------------------------------------------- the eMMC
say "board"
rkdeveloptool read-chip-info 2>/dev/null | sed 's/^/  /'
INFO="$(rkdeveloptool read-flash-info 2>/dev/null)"; echo "$INFO" | sed 's/^/  /'
FLASH_MB="$(echo "$INFO" | grep -oP 'Flash Size:\s*\K[0-9]+(?= ?MB)' | head -1)"
FLASH_SECTORS="$(echo "$INFO" | grep -oiE '[0-9]+ *Sectors' | grep -oE '[0-9]+' | head -1)"
if [ -n "${FLASH_MB:-}" ] && [ "$FLASH_MB" -lt 1000 ]; then
  printf '\n  %s%s MB of flash — that is SPI-NOR, not eMMC.%s This board is SD-card-only.\n\n' "$RED" "$FLASH_MB" "$OFF"
  exit 1
fi
[ -n "${FLASH_MB:-}" ] && ok "eMMC ~$(( (FLASH_MB + 512) / 1024 )) GB (${FLASH_MB} MB)"

if [ "$PROBE" = "1" ]; then
  say "probe only — nothing written"
  echo "  board left in loader mode, ready to flash."
  exit 0
fi

# ---------------------------------------------------------------- the image
[ -n "$IMAGE" ] || IMAGE="$(ls -t "$HERE"/*.img 2>/dev/null | head -1)"
[ -n "$IMAGE" ] && [ -f "$IMAGE" ] || die "no image given and no *.img beside this script"
IMG_BYTES="$(stat -c%s "$IMAGE")"; IMG_MB=$((IMG_BYTES / 1024 / 1024))
say "image"
echo "  $(basename "$IMAGE")  ($IMG_MB MB)"
if [ -f "$IMAGE.sha256" ]; then
  ( cd "$(dirname "$IMAGE")" && sha256sum -c "$(basename "$IMAGE").sha256" >/dev/null 2>&1 ) \
    && ok "checksum verified" || die "image checksum MISMATCH"
fi
[ -n "${FLASH_MB:-}" ] && { [ "$IMG_MB" -le "$FLASH_MB" ] || die "image ${IMG_MB} MB > eMMC ${FLASH_MB} MB"; ok "fits"; }

if [ "$AUTO_YES" -ne 1 ]; then
  printf '\n%sthis ERASES the entire eMMC.%s type %sflash%s to continue: ' "$RED" "$OFF" "$BLD" "$OFF"
  read -r C; [ "$C" = "flash" ] || die "aborted — nothing written."
fi

# ---------------------------------------------------------------- write
say "writing (do NOT touch the cables until this finishes)"
rkdeveloptool write 0 "$IMAGE" 2>&1 | tr '\r' '\n' | tail -1
rkdeveloptool list 2>/dev/null | grep -qiE 'maskrom|loader' \
  || die "the board left the USB bus during the write — re-seat the cable, put it back in maskrom, and rerun"

# ---------------------------------------------------------------- trap 3
say "erasing any older backup GPT from the tail of the eMMC"
if [ -n "${FLASH_SECTORS:-}" ]; then
  LAST=$((FLASH_SECTORS - 1))
  # Only if the image does not itself reach the end of the disk.
  if [ "$((IMG_BYTES / 512))" -lt "$FLASH_SECTORS" ]; then
    Z="$(mktemp)"; dd if=/dev/zero of="$Z" bs=512 count=2048 status=none
    rkdeveloptool write $((FLASH_SECTORS - 2048)) "$Z" >/dev/null 2>&1 \
      && ok "zeroed the last 1 MiB" || warn "could not zero the tail"
    rm -f "$Z"
    C="$(mktemp)"
    if rkdeveloptool read "$LAST" 512 "$C" >/dev/null 2>&1; then
      if head -c8 "$C" | grep -q 'EFI PART'; then
        rm -f "$C"; die "a GPT signature REMAINS at the last sector — U-Boot would boot it instead"
      fi
      ok "no GPT signature at the last sector"
    fi
    rm -f "$C"
  else
    ok "image covers the whole eMMC — no stale tail to erase"
  fi
else
  warn "eMMC sector count unknown — could not locate the tail"
fi

# ---------------------------------------------------------------- read back
# A Rockchip eMMC can ACK every write and keep only the first 32 MiB, so a write
# that "succeeded" is not evidence. Regions that are ZERO in the image prove
# nothing (unwritten flash reads as zero too) and are skipped, not counted.
say "verifying: reading the eMMC back and byte-comparing"
TMPI="$(mktemp)"; TMPD="$(mktemp)"; trap 'rm -f "$TMPI" "$TMPD"' EXIT
PASS=0; FAIL=0; SKIP=0
MAXS=$((IMG_BYTES / 512))
for s in 0 1 64 16384 32768 1000000 5000000 10000000 15000000 20000000 25000000 $((MAXS - 1)); do
  [ "$s" -lt "$MAXS" ] || continue
  dd if="$IMAGE" bs=512 skip="$s" count=8 status=none > "$TMPI"
  n=$(stat -c%s "$TMPI"); [ "$n" -gt 0 ] || continue
  if python3 -c "import sys;sys.exit(0 if open('$TMPI','rb').read()==b'\x00'*$n else 1)"; then
    SKIP=$((SKIP+1)); continue
  fi
  rkdeveloptool read "$s" "$n" "$TMPD" >/dev/null 2>&1
  if cmp -s "$TMPI" "$TMPD"; then PASS=$((PASS+1)); printf '   sector %-10s %sOK%s\n' "$s" "$GRN" "$OFF"
  else FAIL=$((FAIL+1)); printf '   sector %-10s %s** MISMATCH **%s\n' "$s" "$RED" "$OFF"; fi
done
printf '   PASS=%s FAIL=%s skipped(zero-in-image)=%s\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] || die "read-back MISMATCH — the eMMC did not take the full write"

say "done — deliberately NOT resetting the board"
cat <<EOF
  Leaving it in loader mode keeps it WRITABLE: if the boot looks wrong, just
  rerun this script. \`rkdeveloptool reset\` is not a boot test — it commonly
  re-enters maskrom, which reads exactly like a failed flash.

  ${BLD}To test it properly:${OFF}
    1. unplug the USB-C cable FROM THE LAPTOP   (it back-powers the board — trap 1)
    2. unplug DC power, wait 10 seconds
    3. HDMI connected, SD card slot EMPTY
    4. DC power back in, leave USB-C out

  The SD slot must be empty: BootROM order is SPI -> eMMC -> SD and the first
  valid bootloader wins, so a bootable card means you are testing the card.
EOF
