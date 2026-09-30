#!/usr/bin/env bash
# flash_opi_sd.sh — write the Orange Pi 5 StarCap image to SD card(s), then PROVE it landed.
#
# Finds the image next to itself and finds the cards itself, so the common case is just:
#
#     ./flash_opi_sd.sh              # one card  -> propose it, confirm, write, verify
#     ./flash_opi_sd.sh --all        # every card found -> write all in parallel, verify each
#     ./flash_opi_sd.sh --list       # show what it can see, touch nothing
#     ./flash_opi_sd.sh --verify-only            # check the card(s) already written
#     ./flash_opi_sd.sh <image> /dev/sdb /dev/sdc   # spell it out
#
# Options: --all  --list  --verify-only  --dry-run  --yes  --max-gb N  -h
#
# WHY IT WILL NOT PICK FOR YOU WHEN THERE IS MORE THAN ONE CANDIDATE
# "SD cards are always /dev/sd-something" is true and also not enough: so is every USB
# stick, so is every USB hard drive, and most of them report removable=1 exactly like a
# card reader does. On a machine whose internal disk is SATA rather than NVMe, so is
# /dev/sda. So detection is deliberately narrow — removable, whole disk, usb/mmc
# transport, non-zero size, under a size ceiling, no system mount, not the disk holding /
# — every candidate is printed with its size, model and CURRENT CONTENTS before anything
# happens, and when more than one qualifies it REFUSES to choose: you pass --all or name
# them. Erasing the wrong disk is not recoverable, so ambiguity is a stop, not a guess.
#
# Why this exists rather than reusing flash_sd.sh: that script is NanoPi R76S shaped and
# mis-verifies an Orange Pi image three ways — it looks for the recorder at
# `root/home/pi/orangepi-recorder` (R76S overlay-upper path, user `pi`) when an Orange Pi
# image has /home/orangepi/orangepi-recorder; it expands with sgdisk assuming GPT plus a
# trailing partition when this image is MBR with ONE; and it insists on labels
# `rootfs`+`userdata` when the Orange Pi label is `opi_root`.
#
# It does NOT expand the filesystem, and does not need to: stereocam-resizefs.service runs
# growpart + resize2fs on first boot, gated on /var/lib/stereocam/resized, which the image
# ships cleared. That the flag IS cleared is one of the checks — a card that boots without
# re-expanding gets 16 GB of a 128 GB card, silently, while recording "works".
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YEL=""; DIM=""; BLD=""; OFF=""; }
ok(){   printf '  %s[ok]%s   %s\n'  "$GRN" "$OFF" "$*"; }
bad(){  printf '  %s[FAIL]%s %s\n'  "$RED" "$OFF" "$*"; FAILED=$((FAILED+1)); }
warn(){ printf '  %s[warn]%s %s\n'  "$YEL" "$OFF" "$*"; }
say(){  printf '\n%s== %s ==%s\n'   "$BLD" "$*" "$OFF"; }
die(){  printf '%serror:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
gb(){   awk -v b="$1" 'BEGIN{printf "%.1f GB", b/1e9}'; }

DRY=0; VERIFY_ONLY=0; ALL=0; LIST=0; ASSUME_YES=0; MAX_AUTO_GB=512
IMG=""; DEVS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     DRY=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    --all)         ALL=1 ;;
    --list)        LIST=1 ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --max-gb)      shift; MAX_AUTO_GB="${1:-512}" ;;
    -h|--help)     sed -n '2,33p' "$0"; exit 0 ;;
    /dev/*)        DEVS+=("$1") ;;
    -*)            die "unknown option: $1" ;;
    *)             IMG="$1" ;;
  esac
  shift
done

# ---------------------------------------------------------------- find the image
# Newest matching file sitting next to this script.
find_image(){
  local newest="" f
  for f in "$SCRIPT_DIR"/*.img.gz "$SCRIPT_DIR"/*.img.zst "$SCRIPT_DIR"/*.img.xz "$SCRIPT_DIR"/*.img; do
    [ -f "$f" ] || continue
    if [ -z "$newest" ] || [ "$f" -nt "$newest" ]; then newest="$f"; fi
  done
  printf '%s' "$newest"
}

# ---------------------------------------------------------------- find the cards
disk_of_root(){          # the physical disk backing / — never a candidate
  local src; src="$(findmnt -no SOURCE / 2>/dev/null)"
  [ -n "$src" ] && lsblk -no PKNAME "$src" 2>/dev/null | head -1 | tr -d ' '
}
ROOT_DISK="$(disk_of_root)"

card_candidates(){
  local d n sz tran type mp skip
  for d in /sys/block/*; do
    n="$(basename "$d")"
    case "$n" in loop*|ram*|dm-*|zram*|md*|sr*|nbd*) continue;; esac
    [ -n "$ROOT_DISK" ] && [ "$n" = "$ROOT_DISK" ] && continue
    [ "$(cat "$d/removable" 2>/dev/null)" = "1" ] || continue
    sz=$(( $(cat "$d/size" 2>/dev/null || echo 0) * 512 ))
    [ "$sz" -gt 0 ] || continue                       # an empty reader slot reports 0
    type="$(lsblk -ndro TYPE "/dev/$n" 2>/dev/null)"; [ "$type" = "disk" ] || continue
    tran="$(lsblk -ndro TRAN "/dev/$n" 2>/dev/null)"
    case "$tran" in usb|mmc) ;; *) continue;; esac    # a card is on usb (reader) or mmc (slot)
    skip=0
    for mp in $(lsblk -nro MOUNTPOINT "/dev/$n" 2>/dev/null); do
      case "$mp" in /|/home|/boot|/boot/efi|/var|/usr) skip=1;; esac
    done
    [ "$skip" = "1" ] && continue
    printf '%s\n' "/dev/$n"
  done
}

describe(){              # size, model and what is on it NOW, so a wrong pick is visible
  local dev="$1" b n
  b="$(basename "$dev")"
  n=$(( $(cat "/sys/block/$b/size" 2>/dev/null || echo 0) * 512 ))
  printf '  %-12s %-10s %-18s %s\n' "$dev" "$(gb $n)" \
    "$(cat "/sys/block/$b/device/model" 2>/dev/null | tr -s ' ' | cut -c1-18)" \
    "$(lsblk -nro FSTYPE,LABEL,SIZE "$dev" 2>/dev/null | tail -n +2 | awk 'NF{printf "%s | ", $0}' | cut -c1-56)"
}

mapfile -t FOUND < <(card_candidates)

if [ "$LIST" = "1" ]; then
  say "removable card candidates"
  if [ "${#FOUND[@]}" -eq 0 ]; then echo "  none found"
  else for d in "${FOUND[@]}"; do describe "$d"; done; fi
  IMGF="$(find_image)"
  say "image"
  [ -n "$IMGF" ] && echo "  $IMGF  ($(gb $(stat -c%s "$IMGF")))" || echo "  none found in $SCRIPT_DIR"
  exit 0
fi

# ---------------------------------------------------------------- decide targets
if [ "${#DEVS[@]}" -eq 0 ]; then
  [ "${#FOUND[@]}" -eq 0 ] && die "no removable card found. Plug one in, or name it: $0 <image> /dev/sdX  (see --list)"
  # The ceiling applies to AUTO-selection only. A 2 TB USB drive is almost certainly not
  # the card you meant, and is also the one you would most regret.
  KEPT=()
  for d in "${FOUND[@]}"; do
    n=$(( $(cat "/sys/block/$(basename "$d")/size") * 512 ))
    if [ "$n" -gt $(( MAX_AUTO_GB * 1000000000 )) ]; then
      warn "$d is $(gb $n) — over the ${MAX_AUTO_GB} GB auto limit, not auto-selected (name it explicitly to use it)"
    else KEPT+=("$d"); fi
  done
  [ "${#KEPT[@]}" -eq 0 ] && die "no candidate under ${MAX_AUTO_GB} GB. Name the device explicitly if you are sure."
  if [ "$ALL" = "1" ] || [ "${#KEPT[@]}" -eq 1 ]; then
    DEVS=("${KEPT[@]}")
  else
    say "more than one card found — refusing to choose"
    for d in "${KEPT[@]}"; do describe "$d"; done
    die "pass --all to write all of them, or name the one you want"
  fi
fi

for d in "${DEVS[@]}"; do
  [ -b "$d" ] || die "$d is not a block device"
  t="$(lsblk -ndro TYPE "$d" 2>/dev/null)"
  [ "$t" = "disk" ] || die "$d is a '${t:-unknown}', not a whole disk — pass /dev/sdb, not /dev/sdb1"
  b="$(basename "$d")"; [ -e "/sys/block/$b" ] || die "$d has no /sys/block entry"
  [ -n "$ROOT_DISK" ] && [ "$b" = "$ROOT_DISK" ] && die "$d is the disk holding / — refusing"
  [ "$(cat "/sys/block/$b/removable" 2>/dev/null)" = "1" ] \
    || die "$d is not REMOVABLE — refusing (this is how an internal drive gets erased)"
  # A zero-size device is an EMPTY reader slot, not a card. Auto-detection already skips
  # these; catching it here too means naming one explicitly gives a sentence that says what
  # is wrong, instead of a dd failure part-way through a batch.
  [ "$(( $(cat "/sys/block/$b/size" 2>/dev/null || echo 0) * 512 ))" -gt 0 ] \
    || die "$d reports 0 bytes — that is an empty card-reader slot, not a card"
  for mp in $(lsblk -nro MOUNTPOINT "$d" 2>/dev/null); do
    case "$mp" in /|/home|/boot|/boot/efi|/var|/usr) die "$d has a partition mounted at $mp — that is a SYSTEM disk";; esac
  done
done

say "target$([ "${#DEVS[@]}" -gt 1 ] && echo s) (${#DEVS[@]})"
for d in "${DEVS[@]}"; do describe "$d"; done

# ---------------------------------------------------------------- verify one device
verify_dev(){
  local DEV="$1"
  FAILED=0
  say "verifying $DEV"

  # Raw reads: plain dd when the device is readable, sudo only when it is not. sudo's
  # cached credential does NOT survive into a child process in every environment, so an
  # unconditional `sudo dd` here can return "sudo: a password is required" instead of
  # sector data and report every signature as absent. A verifier that cries "will NOT
  # boot" over a permission problem teaches people to ignore it.
  local RD=""
  if dd if="$DEV" bs=512 count=1 status=none of=/dev/null 2>/dev/null; then RD=""
  elif sudo -n true 2>/dev/null; then RD="sudo -n"
  else
    RD="sudo"
    $RD dd if="$DEV" bs=512 count=1 status=none of=/dev/null 2>/dev/null \
      || { bad "cannot read $DEV (permission) — run with sudo, or: sudo chmod a+r $DEV"; return 1; }
  fi
  sect(){ $RD dd if="$DEV" bs=512 skip="$1" count=1 status=none 2>/dev/null; }

  # The bootloader lives OUTSIDE any partition — which is exactly why a filesystem-level
  # copy gives a card that mounts perfectly on a laptop and never boots.
  [ "$(sect 64 | head -c4)" = "RKNS" ] && ok "idbloader present (RKNS @ sector 64)" \
    || bad "no RKNS at sector 64 — this card will NOT boot"
  sect 16384 | head -c4 | od -An -tx1 | grep -q 'd0 0d fe ed' \
    && ok "u-boot FIT present (d00dfeed @ sector 16384)" || bad "no u-boot FIT at sector 16384"

  local LABEL; LABEL="$(lsblk -nro LABEL "$DEV" 2>/dev/null | grep -v '^$' | head -1)"
  [ "$LABEL" = "opi_root" ] && ok "rootfs label: opi_root" \
    || bad "unexpected rootfs label: '${LABEL:-none}' (expected opi_root)"

  local PART MNT UDISKS UOUT
  PART="$(lsblk -nro NAME "$DEV" 2>/dev/null | tail -n +2 | head -1)"
  MNT=""; UDISKS=0
  # udisksctl first: it mounts removable media without a password on a normal desktop,
  # and plain sudo cannot be relied on from inside this script (see the note above).
  if command -v udisksctl >/dev/null 2>&1 && [ -n "$PART" ]; then
    UOUT="$(udisksctl mount -b "/dev/$PART" --no-user-interaction 2>/dev/null)"
    MNT="$(printf '%s' "$UOUT" | sed -n 's/.* at \(.*\)\.$/\1/p')"
    [ -z "$MNT" ] && MNT="$(lsblk -nro MOUNTPOINT "/dev/$PART" 2>/dev/null | head -1)"
    [ -n "$MNT" ] && UDISKS=1
  fi
  if [ -z "$MNT" ] && [ -n "$PART" ]; then
    MNT="$(mktemp -d)"
    $RD mount -o ro "/dev/$PART" "$MNT" 2>/dev/null || { rmdir "$MNT" 2>/dev/null; MNT=""; }
  fi

  if [ -n "$MNT" ]; then
    local R="$MNT/home/orangepi/orangepi-recorder" FDT ENVL W
    [ -f "$R/stereocam/VERSION" ] && ok "recorder present, VERSION $(cat "$R/stereocam/VERSION")" \
      || bad "no recorder at /home/orangepi/orangepi-recorder"

    FDT="$(grep -oP '^fdtfile=\K.*' "$MNT/boot/orangepiEnv.txt" 2>/dev/null)"
    case "$FDT" in
      *orangepi-5*) ok "dtb: $FDT" ;;
      "")           bad "no fdtfile in /boot/orangepiEnv.txt" ;;
      *)            bad "dtb is '$FDT' — not an Orange Pi 5 device tree" ;;
    esac

    # Unset is not off (updater.py defaults to on/main), but an implicit pin is one
    # tools/pin_to_branch.sh away from UPDATE_ENABLE=0. Both must be explicit.
    ENVL="$(cat "$MNT/etc/systemd/system/stereocam.service.d/"*.conf "$MNT/etc/systemd/system/stereocam.service" 2>/dev/null | grep -h '^Environment=')"
    grep -q 'UPDATE_BRANCH=main' <<<"$ENVL" && ok "UPDATE_BRANCH=main (explicit)" || bad "UPDATE_BRANCH=main not set explicitly"
    grep -q 'UPDATE_ENABLE=1'   <<<"$ENVL" && ok "UPDATE_ENABLE=1 (explicit)"   || bad "UPDATE_ENABLE=1 not set explicitly"

    [ -e "$MNT/var/lib/stereocam/resized" ] \
      && bad "/var/lib/stereocam/resized EXISTS — this clone will NOT grow to fill the card" \
      || ok "resize armed (resized flag cleared)"

    # -L, not -e. systemd enable-links are ABSOLUTE (-> /etc/systemd/system/...), so inside
    # a mounted image they resolve against the HOST's /etc and a correctly-enabled unit
    # reads as missing. This check failed every good card until it was fixed.
    W="$MNT/etc/systemd/system/multi-user.target.wants"
    { [ -L "$W/stereocam-resizefs.service" ] || [ -e "$W/stereocam-resizefs.service" ]; } \
      && ok "stereocam-resizefs enabled" || bad "stereocam-resizefs not enabled"
    { [ -L "$W/stereocam-firstboot.service" ] || [ -e "$W/stereocam-firstboot.service" ]; } \
      && ok "stereocam-firstboot enabled (ssh keys + machine-id per clone)" \
      || bad "stereocam-firstboot not enabled"

    # A template must carry NO identity, or every card is a twin: two boards claiming one
    # name and one device_id breaks /api/peers and makes uploads unattributable.
    [ -e "$MNT/home/orangepi/.config/starcap/device_name" ] \
      && bad "device_name present ($(cat "$MNT/home/orangepi/.config/starcap/device_name" 2>/dev/null)) — clones will impersonate it" \
      || ok "device_name absent (clone boots nameless, opm-name assigns it)"
    [ -e "$MNT/home/orangepi/.config/starcap/device_id" ] \
      && bad "device_id present — clones will share it" || ok "device_id absent (regenerated per board)"
    [ "$(ls "$MNT/etc/ssh/" 2>/dev/null | grep -c '^ssh_host_')" = "0" ] \
      && ok "ssh host keys absent (regenerated on first boot)" || bad "ssh host keys present — clones would share them"
    [ -s "$MNT/etc/machine-id" ] && bad "machine-id is populated — clones would share it" || ok "machine-id blank"
    [ -d "$R/.git" ] && bad ".git in the recorder tree (fossil; can pin UPDATE_BRANCH to a retired branch)" \
                     || ok "no .git (clean plain tree the updater can fast-forward)"

    # cv2 decides whether white balance works AT ALL: export_wbc.py bakes camera_cal.wbc
    # with cv2.GaussianBlur, and without it the recorder reports wb_correct=uncalibrated
    # while the dashboard still shows the camera as calibrated. Silent; in the field it
    # shows up only as wrong colour.
    ls -d "$MNT/usr/lib/python3/dist-packages/cv2"* >/dev/null 2>&1 \
      && ok "python3-opencv present (clones can bake camera_cal.wbc)" \
      || bad "python3-opencv MISSING — white balance will not bake on any clone"

    [ -f "$MNT/usr/include/gstreamer-1.0/gst/gst.h" ] \
      && ok "gstreamer dev headers present (native WB survives OTA)" \
      || warn "gstreamer dev headers missing — wbcorrect falls back to the stale prebuilt after the first OTA"

    if [ "$UDISKS" = "1" ]; then udisksctl unmount -b "/dev/$PART" --no-user-interaction >/dev/null 2>&1
    else $RD umount "$MNT" 2>/dev/null; rmdir "$MNT" 2>/dev/null; fi
  else
    bad "could not mount ${PART:+/dev/$PART} to verify contents (tried udisksctl and mount)"
  fi
  return "$FAILED"
}

# ---------------------------------------------------------------- write
if [ "$VERIFY_ONLY" = "0" ]; then
  [ -n "$IMG" ] || IMG="$(find_image)"
  [ -n "$IMG" ] || die "no image given and none found in $SCRIPT_DIR"
  [ -f "$IMG" ] || die "image not found: $IMG"
  echo; echo "image:  $IMG  ($(gb $(stat -c%s "$IMG")))"

  case "$IMG" in
    *.gz)  DECOMP=(gzip -dc) ;;
    *.zst) DECOMP=(zstd -dc) ;;
    *.xz)  DECOMP=(xz -dc) ;;
    *)     DECOMP=(cat) ;;
  esac
  # Deliberately NO "does it fit" test from gzip -l: that field is the uncompressed size
  # MODULO 4 GiB, so for anything larger it reports a small number that would read as
  # reassurance. dd failing on a genuinely too-small card is the honest signal.

  printf '\n%sthis ERASES %s completely.%s type %serase%s to continue: ' \
    "$RED" "$(printf '%s ' "${DEVS[@]}")" "$OFF" "$BLD" "$OFF"
  if [ "$DRY" = "1" ]; then echo "(dry-run, skipped)"
  elif [ "$ASSUME_YES" = "1" ]; then echo "(--yes)"
  else read -r reply; [ "$reply" = "erase" ] || die "aborted"; fi

  # Unmount everything on the targets, or the write races the page cache.
  for d in "${DEVS[@]}"; do
    for p in $(lsblk -nro NAME "$d" 2>/dev/null | tail -n +2); do
      if mount | grep -q "^/dev/$p "; then
        [ "$DRY" = "1" ] && echo "  would unmount /dev/$p" \
          || { udisksctl unmount -b "/dev/$p" --no-user-interaction >/dev/null 2>&1 || sudo umount "/dev/$p" 2>/dev/null; }
      fi
    done
  done

  if [ "$DRY" = "1" ]; then
    say "dry-run"; for d in "${DEVS[@]}"; do echo "  would write $IMG -> $d"; done
    echo; echo "dry-run complete"; exit 0
  fi

  if [ "${#DEVS[@]}" -eq 1 ]; then
    say "writing -> ${DEVS[0]}"
    "${DECOMP[@]}" "$IMG" | sudo dd of="${DEVS[0]}" bs=4M conv=fsync status=progress || die "write failed"
  else
    # Several cards: decompress ONCE to scratch, then write them concurrently with
    # independent exit codes. Piping through `tee` into process substitutions would also
    # work but discards each dd's status, and on a batch "which card failed" is the only
    # thing you actually need to know.
    TMPRAW="$(mktemp -p "${TMPDIR:-/var/tmp}" opiimg.XXXXXX)"
    STATUS_DIR="$(mktemp -d)"
    trap 'rm -f "$TMPRAW"; rm -rf "$STATUS_DIR"' EXIT

    # The scratch copy is the FULL RAW image (~17 GB), and ${TMPDIR:-/var/tmp} is normally
    # on /. A root filesystem with less free than that dies mid-decompress with nothing but
    # "gzip: stdout: No space left on device" — after minutes of looking like it worked.
    # Say so BEFORE spending those minutes. (One card is immune: that path streams gzip
    # straight into dd and never lands a scratch copy at all.)
    SCRATCH_DIR="$(dirname "$TMPRAW")"
    AVAIL_KB="$(df -kP "$SCRATCH_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ "${AVAIL_KB:-0}" -lt 20971520 ]; then
      warn "only $(gb $(( ${AVAIL_KB:-0} * 1024 ))) free on $SCRATCH_DIR — the raw image needs ~17 GB"
      printf '         if this dies with "No space left on device", point scratch elsewhere:\n'
      printf '           sudo TMPDIR=%s %s --all\n' "$SCRIPT_DIR" "$0"
    fi

    say "decompressing once -> $TMPRAW"
    "${DECOMP[@]}" "$IMG" > "$TMPRAW" 2>"$STATUS_DIR/decomp.err" &
    DPID=$!
    DT0=$(date +%s)
    while kill -0 "$DPID" 2>/dev/null; do
      DSZ="$(stat -c%s "$TMPRAW" 2>/dev/null || echo 0)"
      DEL=$(( $(date +%s) - DT0 )); [ "$DEL" -lt 1 ] && DEL=1
      printf '\r  %s written to scratch  (%s MB/s)\033[K' \
        "$(gb "$DSZ")" "$(awk -v b="$DSZ" -v t="$DEL" 'BEGIN{printf "%.0f", b/t/1048576}')"
      sleep 1
    done
    if ! wait "$DPID"; then
      printf '\r\033[K'
      tr '\r' '\n' < "$STATUS_DIR/decomp.err" 2>/dev/null | grep -v '^$' | tail -3 | sed 's/^/      /'
      if grep -qi "no space left" "$STATUS_DIR/decomp.err" 2>/dev/null; then
        printf '  %sout of scratch space in %s.%s Rerun with TMPDIR on a filesystem with 20+ GB:\n' \
          "$RED" "$SCRATCH_DIR" "$OFF"
        printf '    sudo TMPDIR=%s %s --all\n' "$SCRIPT_DIR" "$0"
      fi
      die "decompress failed"
    fi
    RAWBYTES="$(stat -c%s "$TMPRAW")"
    printf '\r  %s raw\033[K\n' "$(gb "$RAWBYTES")"

    say "writing to ${#DEVS[@]} cards in parallel"
    # Pre-authenticate ONCE, before any writer starts. Several `sudo dd` processes
    # launching together would otherwise each try to prompt and race for the terminal,
    # and the progress repaint below would scribble straight over the prompt. Caching
    # the credential here means none of them ask. No-op when already root.
    sudo -v 2>/dev/null || sudo true || die "need root to write to the card(s)"
    PIDS=()
    declare -A BASE_SECT
    for d in "${DEVS[@]}"; do
      b="$(basename "$d")"
      BASE_SECT["$d"]="$(awk '{print $7}' "/sys/block/$b/stat" 2>/dev/null || echo 0)"
      BASE_SECT["$d"]="${BASE_SECT["$d"]:-0}"
      : > "$STATUS_DIR/$b.err"
      rm -f "$STATUS_DIR/$b.rc"   # a stale rc would render as "done" on the first frame
      ( sudo dd if="$TMPRAW" of="$d" bs=4M conv=fsync status=progress 2>"$STATUS_DIR/$b.err"
        echo "$?" > "$STATUS_DIR/$b.rc" ) &
      PIDS+=("$!")
      echo "  started $d"
    done

    # ---- live per-card progress -------------------------------------------------
    # dd's own status=progress cannot be shown directly for several cards at once: every
    # writer would rewrite the same terminal line and you'd get shredded output. So each
    # dd's stderr goes to its own file and we render ONE refreshed line per card from it.
    #
    # Rate is computed from OUR deltas, not from dd's running average, and that difference
    # matters: a bad card writes fast into its small SLC cache and then collapses to raw
    # flash speed. dd's average still looks respectable long after the card has fallen to
    # ~1 MB/s, while the instant rate shows it immediately. A card that needs 5 hours is
    # not worth waiting for, so it gets flagged SLOW after 3 consecutive slow samples
    # rather than discovered at the end.
    # Bytes the DEVICE has actually taken, from /sys/block/<dev>/stat field 7 (write
    # sectors completed), measured against a baseline captured before the writers start.
    #
    # NOT dd's status=progress: dd counts bytes handed to the PAGE CACHE. A card slower
    # than the cache fills instantly, so dd prints nothing for its first second, then
    # blocks in conv=fsync emitting no records at all while the card grinds through the
    # backlog — the line would read 0% for a minute and then jump to done, or sit at 100%
    # while minutes of flushing remain. Device counters track what the card really did,
    # which is also what exposes a card that has collapsed to ~1 MB/s.
    #
    # The counter is cumulative since the device appeared, hence the baseline: without it a
    # card flashed twice in one session reads as "32 GB of 16 GB".
    _dev_written(){   # $1 = /dev/sdX, $2 = baseline sectors -> bytes taken by the device
      local b sect
      b="$(basename "$1")"
      sect="$(awk '{print $7}' "/sys/block/$b/stat" 2>/dev/null)"
      if [ -z "${sect:-}" ]; then printf '0'; return; fi
      awk -v s="$sect" -v base="$2" 'BEGIN{ d=(s-base)*512; if(d<0)d=0; printf "%d", d }'
    }
    NLINES="${#DEVS[@]}"
    declare -A PREV_B PREV_T SLOWHITS
    NOW="$(date +%s)"
    for d in "${DEVS[@]}"; do PREV_B["$d"]=0; PREV_T["$d"]="$NOW"; SLOWHITS["$d"]=0; done
    ISTTY=0; [ -t 1 ] && ISTTY=1
    if [ "$ISTTY" = "1" ]; then for _i in $(seq 1 "$NLINES"); do echo; done; fi
    TICK=0
    while :; do
      RUNNING=0
      for p in "${PIDS[@]}"; do if kill -0 "$p" 2>/dev/null; then RUNNING=1; fi; done
      TICK=$(( TICK + 1 ))
      # On a terminal, repaint in place every tick. Piped to a file or a pager there is no
      # cursor to move, so emit a plain block only every 10th tick (~30 s) instead of
      # scrolling hundreds of duplicate frames into the log.
      SHOW=0
      if [ "$ISTTY" = "1" ]; then SHOW=1; printf '\033[%dA' "$NLINES"
      elif [ $(( TICK % 10 )) = 1 ] || [ "$RUNNING" = "0" ]; then SHOW=1; fi
      NOW="$(date +%s)"
      for d in "${DEVS[@]}"; do
        b="$(basename "$d")"
        CUR="$(_dev_written "$d" "${BASE_SECT["$d"]}")"
        DTS=$(( NOW - ${PREV_T["$d"]} )); [ "$DTS" -lt 1 ] && DTS=1
        RATE="$(awk -v a="${PREV_B["$d"]}" -v c="$CUR" -v t="$DTS" \
                 'BEGIN{r=(c-a)/t/1048576; if(r<0)r=0; printf "%.1f", r}')"
        if [ "$(awk -v r="$RATE" 'BEGIN{print (r+0>0 && r+0<5)?1:0}')" = "1" ]; then
          SLOWHITS["$d"]=$(( ${SLOWHITS["$d"]} + 1 ))
        else
          SLOWHITS["$d"]=0
        fi
        FLAG=""
        if [ "${SLOWHITS["$d"]}" -ge 3 ]; then FLAG="  ${YEL}SLOW — suspect card${OFF}"; fi
        if [ "$SHOW" = "1" ]; then
          if [ -f "$STATUS_DIR/$b.rc" ]; then
            if [ "$(cat "$STATUS_DIR/$b.rc")" = "0" ]; then
              printf '  %-8s %sdone%s      %s\033[K\n' "$b" "$GRN" "$OFF" "$(gb "$CUR")"
            else
              printf '  %-8s %sFAILED%s\033[K\n' "$b" "$RED" "$OFF"
            fi
          else
            printf '  %-8s %3s%%  %9s/%-9s %6s MB/s  ETA %s%s\033[K\n' \
              "$b" \
              "$(awk -v c="$CUR" -v t="$RAWBYTES" 'BEGIN{printf "%.0f", (t>0? c*100/t : 0)}')" \
              "$(gb "$CUR")" "$(gb "$RAWBYTES")" "$RATE" \
              "$(awk -v c="$CUR" -v t="$RAWBYTES" -v r="$RATE" \
                   'BEGIN{ if (r+0 < 0.05) print "  --"; else printf "%.0fm", (t-c)/1048576/(r+0)/60 }')" \
              "$FLAG"
          fi
        fi
        PREV_B["$d"]="$CUR"; PREV_T["$d"]="$NOW"
      done
      if [ "$RUNNING" = "0" ]; then break; fi
      sleep 3
    done
    wait "${PIDS[@]}" 2>/dev/null

    for d in "${DEVS[@]}"; do
      b="$(basename "$d")"
      rc="$(cat "$STATUS_DIR/$b.rc" 2>/dev/null || echo 1)"
      if [ "$rc" = "0" ]; then printf '  %swritten%s %s\n' "$GRN" "$OFF" "$d"
      else printf '  %sWRITE FAILED%s %s\n' "$RED" "$OFF" "$d"
           # dd's progress records share the same stderr, so strip them: the real error is
           # at the END, and `sed -n 1,3p` would have shown three progress lines instead.
           tr '\r' '\n' < "$STATUS_DIR/$b.err" 2>/dev/null \
             | grep -vE 'bytes \(.*copied|^[0-9]+\+[0-9]+ records|^[[:space:]]*$' | tail -3 | sed 's/^/      /'; fi
    done
  fi

  sync
  # Make the kernel re-read the table, else the label/partition checks below can still be
  # looking at the PREVIOUS card's geometry and pass for the wrong reason.
  for d in "${DEVS[@]}"; do
    sudo blockdev --rereadpt "$d" 2>/dev/null || sudo partprobe "$d" 2>/dev/null || true
  done
  sleep 2
fi

# ---------------------------------------------------------------- verify all
TOTAL_FAIL=0; PASSED=0
for d in "${DEVS[@]}"; do
  if verify_dev "$d"; then PASSED=$(( PASSED + 1 )); else TOTAL_FAIL=$(( TOTAL_FAIL + 1 )); fi
done

say "result"
if [ "$TOTAL_FAIL" -eq 0 ]; then
  printf '  %s%sPASS%s — %d card(s) safe to boot. Each comes up nameless; name it with opm-name.\n' \
    "$GRN" "$BLD" "$OFF" "$PASSED"
  exit 0
else
  printf '  %s%s%d of %d card(s) FAILED%s — do not ship a card that failed.\n' \
    "$RED" "$BLD" "$TOTAL_FAIL" "${#DEVS[@]}" "$OFF"
  exit 1
fi
