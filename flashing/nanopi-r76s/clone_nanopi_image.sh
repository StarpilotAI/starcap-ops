#!/usr/bin/env bash
# clone_nanopi_image.sh — pull a FLASHABLE image off a docked, running
# FriendlyElec RK3576 NanoPi R76S and land it on this laptop.
#
# Usage: ./clone_nanopi_image.sh [10.77.0.149] [--user pi] [--capture-only]
#                                [--offline] [--jobs N] [--verify]
#                                [--rescue-to DIR | --drop-recordings]
#                                [--out DIR] [--yes] [--dry-run]
#
# Probed against the actual board on 2026-08-01 — the layout below is measured,
# not assumed, because three obvious assumptions are all WRONG on this board.
#
# WHAT THIS BOARD ACTUALLY LOOKS LIKE
# -----------------------------------
#   mmcblk2   58.2G  eMMC, THE BOOT DISK — this is what gets imaged
#     p1..p7         uboot/misc/dtbo/resource/kernel/boot/recovery (FriendlyElec)
#     p8     5.5G    ext4 "rootfs"    <- root=/dev/mmcblk2p8, overlay LOWER
#     p9    52.6G    ext4 "userdata"  <- data=/dev/mmcblk2p9, overlay UPPER+WORK
#   mmcblk0  119.2G  vfat SD card at /media/sdcard — RECORDING STORAGE, not imaged
#
# Trap 1: `/` IS AN OVERLAY, NOT A PARTITION.
#   /proc/mounts: overlay / overlay rw,lowerdir=/root,upperdir=/data/root,...
#   So `findmnt -no SOURCE /` returns the string "overlay" and `lsblk -no PKNAME`
#   on it returns NOTHING. Any script that derives the disk that way ends up
#   pointing at /dev/overlay and either dies or images the wrong thing. The disk
#   is resolved here from /proc/cmdline's root= instead, which names the real
#   partition (/dev/mmcblk2p8) even when / is stacked on top of it.
#
# Trap 2: fsfreeze CANNOT freeze this root.
#   overlayfs implements no freeze_fs op, so `fsfreeze -f /` fails EOPNOTSUPP,
#   and the two ext4 layers underneath are not independently mounted anywhere we
#   can reach them. There is no way to quiesce this filesystem from userspace
#   while it is running. A live capture here is therefore CRASH-CONSISTENT only
#   — the same state a board would be in after a power cut, which the ext4
#   journal on p8/p9 replays on first boot. That is fine for a golden master; it
#   is NOT a substitute for --offline if you want a provably clean one.
#
# Trap 3: the recordings are ON THE DISK BEING IMAGED.
#   app.py:109-122 picks the recordings path by testing whether the root device
#   string contains "mmcblk1". Here it contains "overlay", so ROOT_ON_EMMC is
#   False and the recorder writes to $REPO/recordings/stereo — i.e. into the
#   overlay upper, i.e. into mmcblk2p9, i.e. INSIDE THE IMAGE. Measured: 78 runs
#   / 22G there, plus a separate 13 runs / 68G on the SD card that are NOT
#   imaged. A naive capture bakes 22G of someone else's takes into every board
#   you flash, so this script refuses to proceed until you say what to do with
#   them (--rescue-to or --drop-recordings).
#
# WHY A RAW COPY IS A TWIN, NOT A TEMPLATE
# ----------------------------------------
# uploader.py:123 generates the StarCap device_id ONCE as a random `pi-<hex>`
# and persists it to ~/.config/starcap/device_id. It is NOT derived from the SoC
# serial, so a byte copy carries the source board's id (currently pi-5029ae18d9)
# onto every clone. Two boards sharing an id breaks peer discovery (/api/peers
# keys on it) and makes uploads unattributable. Same story for /etc/ssh/ssh_host_*
# and /etc/machine-id. --sanitize (the default) clears all three and re-arms
# stereocam-firstboot.service + stereocam-resizefs.service, which are both
# present on this board, so each flashed clone self-assigns on first boot.
#
# MODES
#   (default)        sanitize the board, then capture. The SOURCE BOARD LOSES ITS
#                    IDENTITY and re-assigns on its next boot. Sanitizing in place
#                    is deliberate: the running kernel applies overlay delete
#                    semantics (whiteouts) correctly, which hand-editing the two
#                    layers inside a captured image does not.
#   --capture-only   pure read-only byte copy. THE BOARD IS NOT TOUCHED AT ALL.
#                    The image is a twin — fix identity yourself after flashing.
#   --offline        board is SD-booted (build_nanopi_sd.sh) so the eMMC is idle;
#                    cold read, the only provably clean capture.
#
# PARALLEL CAPTURE (--jobs, default 4)
# ------------------------------------
# One `dd | ssh` stream is one cipher stream on one core, and that — not the
# eMMC, not the 1Gb wire — is the ceiling. So the disk is cut into N contiguous
# byte ranges, each read over its OWN SSH connection concurrently.
#
# The parts rejoin with plain `cat`, which is only valid because of a specific
# property of both container formats used: gzip and zstd streams CONCATENATE —
# decompressing joined members yields the joined payload. Parts are numbered
# %03d so the glob expands in offset order; get that wrong and the image is
# silently scrambled rather than obviously broken. Ranges are byte-exact
# (iflag=skip_bytes,count_bytes) so a disk that is not a whole number of 4 MiB
# blocks still round-trips exactly. Verified byte-exact on a deliberately
# misaligned test image before this script was handed over.
#
# NOTE: this board has NO zstd (checked) — it falls back to gzip -1, and the
# 22G of already-compressed H.264 in the recordings will not shrink, so
# --drop-recordings makes by far the biggest difference to capture time.
set -euo pipefail

BOARD="10.77.0.149"
SSH_USER="pi"
MODE="sanitize"          # sanitize | capture-only | offline
AUTO_YES=0
DRY_RUN=0
JOBS=4
VERIFY=0
RESCUE_TO=""
DROP_REC=0
OUT_DIR="$HOME/Desktop"
PASS="${NANOPI_PASS:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --user)             SSH_USER="$2"; shift ;;
    --password)         PASS="$2"; shift ;;
    --capture-only)     MODE="capture-only" ;;
    --offline)          MODE="offline" ;;
    --jobs)             JOBS="$2"; shift ;;
    --verify)           VERIFY=1 ;;
    --rescue-to)        RESCUE_TO="$2"; shift ;;
    --drop-recordings)  DROP_REC=1 ;;
    --out)              OUT_DIR="$2"; shift ;;
    --yes)              AUTO_YES=1 ;;
    --dry-run)          DRY_RUN=1 ;;
    -h|--help)          awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    -*)                 echo "unknown option: $1" >&2; exit 1 ;;
    *)                  BOARD="$1" ;;
  esac
  shift
done

case "$JOBS" in ''|*[!0-9]*) echo "--jobs must be a positive integer" >&2; exit 1 ;; esac
[ "$JOBS" -ge 1 ] || { echo "--jobs must be >= 1" >&2; exit 1; }

say()  { printf '\n\033[1;36m• %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# The password is never stored in this file. It comes from --password, from
# $NANOPI_PASS, or it is prompted for.
if [ -z "$PASS" ]; then
  read -rsp "password for $SSH_USER@$BOARD: " PASS; echo
fi
[ -n "$PASS" ] || die "no password given"
command -v sshpass >/dev/null || die "sshpass not installed (apt install sshpass)"

export SSHPASS="$PASS"
# Host-key pinning is switched OFF deliberately, not out of laziness. This
# script's whole job is to DESTROY and regenerate the board's SSH host keys, so
# every run legitimately presents a different key and a pinned known_hosts entry
# turns into a hard "cannot SSH" failure on the very next run (seen 2026-08-01).
# Safe here only because 10.77.0.0/24 is the direct-attached bench subnet.
SSH="sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -o LogLevel=ERROR -o ConnectTimeout=8 \
     -o PreferredAuthentications=password -o PubkeyAuthentication=no"
rsh() { $SSH "$SSH_USER@$BOARD" "$@"; }
# This board has NO passwordless sudo (checked), so every privileged call feeds
# the password to `sudo -S` on stdin. dd reads via if=, never stdin, so the two
# never collide.
SUDO="printf '%s\n' \"\$P\" | sudo -S -p ''"

# ---------------------------------------------------------------------------
# 1. Reach the board
# ---------------------------------------------------------------------------
say "connecting to $SSH_USER@$BOARD"
rsh true 2>/dev/null || die "cannot SSH to $SSH_USER@$BOARD — wrong user or password?"
# NOTE: `export`, not a bare assignment. Every privileged block below is piped
# into a CHILD `bash -s`, and an unexported var does not cross that boundary —
# sudo then silently gets no password and the whole block no-ops under `set -u`.
rsh "export P='$PASS'; $SUDO true" >/dev/null 2>&1 || die "sudo refused for '$SSH_USER' — wrong password?"

# ---------------------------------------------------------------------------
# 2. Resolve the disk from cmdline (see Trap 1)
# ---------------------------------------------------------------------------
say "resolving storage layout"
eval "$(rsh 'bash -s' <<'PROBE'
set -e
root_part=$(sed -n 's/.*[ ]root=\([^ ]*\).*/\1/p' /proc/cmdline)
[ -n "$root_part" ] || root_part=$(findmnt -no SOURCE /)
disk=$(lsblk -no PKNAME "$root_part" 2>/dev/null || true)
[ -n "$disk" ] || disk=$(basename "$root_part")
repo="$HOME/orangepi-recorder"
# Same rule app.py uses, so this agrees with the recorder rather than guessing.
if [ "${root_part#*mmcblk1}" != "$root_part" ]; then rec=/media/sdcard/stereo
else rec="$repo/recordings/stereo"; fi
probe="$rec"; [ -d "$probe" ] || probe=$(dirname "$rec")
rec_src=$(findmnt -no SOURCE -T "$probe" 2>/dev/null || echo none)
echo "ROOT_PART='$root_part' OS_DISK='/dev/$disk'"
echo "OS_SIZE='$(lsblk -bdno SIZE "/dev/$disk" 2>/dev/null || echo 0)'"
echo "REC_DIR='$rec' REC_SRC='$rec_src'"
echo "REC_RUNS='$(ls -1 "$rec" 2>/dev/null | wc -l)'"
echo "REC_BYTES='$(du -sb "$rec" 2>/dev/null | cut -f1 || echo 0)'"
echo "ROOTFS='$(findmnt -no FSTYPE /)' HOSTN='$(hostname)'"
echo "DEVID='$(cat "$HOME/.config/starcap/device_id" 2>/dev/null || echo none)'"
echo "HAS_ZSTD='$(command -v zstd >/dev/null && echo 1 || echo 0)'"
PROBE
)"

[ "${OS_SIZE:-0}" -gt 0 ] || die "could not size $OS_DISK — refusing to image a disk I cannot measure."
HUMAN=$(numfmt --to=iec "$OS_SIZE")
REC_HUMAN=$(numfmt --to=iec "${REC_BYTES:-0}")

echo "  board      : $HOSTN  ($DEVID)"
echo "  OS disk    : $OS_DISK  ($HUMAN)   <- imaged"
echo "  root       : $ROOT_PART  via $ROOTFS"
echo "  recordings : $REC_DIR  ($REC_RUNS runs, $REC_HUMAN) on $REC_SRC"

# Is the recordings dir physically inside the image? On the overlay it is.
REC_ON_IMAGE=0
case "$REC_SRC" in
  overlay|"$OS_DISK"*) REC_ON_IMAGE=1 ;;
esac
[ "$REC_ON_IMAGE" = 1 ] && warn "those $REC_RUNS runs sit INSIDE the image (overlay upper -> $OS_DISK)."

# ---------------------------------------------------------------------------
# 3. Recordings interlock
# ---------------------------------------------------------------------------
if [ "$REC_ON_IMAGE" = 1 ] && [ "${REC_RUNS:-0}" -gt 0 ] && [ "$MODE" != "capture-only" ]; then
  if [ -n "$RESCUE_TO" ]; then
    say "rescuing $REC_RUNS run(s) ($REC_HUMAN) to $RESCUE_TO"
    mkdir -p "$RESCUE_TO"
    [ "$DRY_RUN" = 1 ] || sshpass -e rsync -a --info=progress2 \
      -e "sshpass -e ssh -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password -o PubkeyAuthentication=no" \
      "$SSH_USER@$BOARD:$REC_DIR/" "$RESCUE_TO/"
  elif [ "$DROP_REC" != 1 ]; then
    die "$REC_RUNS run(s) ($REC_HUMAN) live on the disk being imaged.
  Baking them into a golden master ships someone else's takes to every board.
  Choose one:
    pull them off first : $0 $BOARD --rescue-to ~/Desktop/rescued_runs
    delete them         : $0 $BOARD --drop-recordings
    image as-is, board untouched, twin identity :
                          $0 $BOARD --capture-only"
  fi
fi

TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUT_DIR/nanopi-r76s-$([ "$MODE" = capture-only ] && echo backup || echo golden)-$TS.img"
[ "${HAS_ZSTD:-0}" = 1 ] && OUT="$OUT.zst" || OUT="$OUT.gz"

# ---------------------------------------------------------------------------
# 4. Confirm
# ---------------------------------------------------------------------------
echo
echo "  mode: $MODE"
case "$MODE" in
  capture-only)
    echo "  - board is NOT modified in any way (read-only)"
    echo "  - image keeps device_id $DEVID, SSH host keys, machine-id"
    echo "    => flashing it makes a TWIN; fix identity by hand afterward" ;;
  sanitize)
    echo "  - stop recorder, clear device_id/SSH host keys/machine-id/logs"
    [ "$DROP_REC" = 1 ] && echo "  - DELETE $REC_RUNS run(s) ($REC_HUMAN) from the board"
    echo "  - zero free space, then capture"
    echo "  - THE SOURCE BOARD LOSES ITS IDENTITY (re-assigns on next boot)"
    echo "  - crash-consistent only: overlayfs cannot be frozen (see header)" ;;
  offline)
    echo "  - cold read of an idle eMMC (board must be SD-booted)" ;;
esac
echo "  - $JOBS parallel stream(s) -> $OUT"
echo
if [ "$DRY_RUN" = 1 ]; then say "dry run — nothing done"; exit 0; fi
if [ "$AUTO_YES" != 1 ]; then
  read -rp "  proceed? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || die "aborted"
fi

# ---------------------------------------------------------------------------
# 5/6. Sanitize + capture
# ---------------------------------------------------------------------------
# THE ORDERING TRAP THAT COST A RUN (2026-08-01)
# ----------------------------------------------
# The sanitize has to `rm -f /etc/ssh/ssh_host_*`, because stereocam-firstboot.
# service only regenerates them when they are ABSENT:
#     ConditionFileNotEmpty=|!/etc/ssh/ssh_host_ed25519_key
# Leave them in the image and no clone ever replaces them — every board flashed
# from it shares one host key.
#
# But deleting them KILLS SSHD FOR NEW CONNECTIONS. make-image.sh gets away with
# it because its very next step is `poweroff` and the card is pulled by hand.
# Here the capture still has to cross the network afterwards, and every new
# connection dies with "kex_exchange_identification: Connection reset by peer".
# Established sessions survive (sshd already holds the keys); only new ones fail
# — which is exactly what N parallel dd workers need to open.
#
# So in sanitize mode the transport CANNOT be SSH. The whole sequence — sanitize,
# zero, serve — is written to the board and launched DETACHED while SSH still
# works, and the image comes back over plain netcat on $BASE_PORT.. instead.
# That also removes the per-stream cipher cost, so it is faster, and it is only
# acceptable because 10.77.0.0/24 is the direct-attached bench subnet.
# capture-only/offline leave the host keys alone, so those keep using SSH.
if [ "${HAS_ZSTD:-0}" = 1 ]; then
  [ "$JOBS" -gt 1 ] && COMP="zstd -3" || COMP="zstd -T0 -3"
else
  COMP="gzip -1"
fi
BASE_PORT=9700

FREE=$(df -B1 --output=avail "$OUT_DIR" | tail -1)
[ "$FREE" -gt "$OS_SIZE" ] || warn "only $(numfmt --to=iec "$FREE") free in $OUT_DIR vs a $HUMAN disk — compression should cover it, but watch it."

if [ "$MODE" = "offline" ]; then
  if rsh "findmnt -no SOURCE / | grep -q mmcblk2"; then
    die "--offline given but / is still on the eMMC. SD-boot the board first (build_nanopi_sd.sh)."
  fi
fi

# Byte-exact ranges, 4 MiB-aligned. Shared by both transports.
PARTS=$(mktemp -d "${TMPDIR:-/tmp}/nanopi-clone-XXXXXX")
trap 'rm -rf "$PARTS"' EXIT
ALIGN=$((4*1024*1024))
CHUNK=$(( (OS_SIZE + JOBS - 1) / JOBS ))
CHUNK=$(( (CHUNK + ALIGN - 1) / ALIGN * ALIGN ))
OFFS=""; LENS=""; NPART=0; off=0
while [ "$off" -lt "$OS_SIZE" ]; do
  len=$CHUNK
  [ $((off + len)) -gt "$OS_SIZE" ] && len=$((OS_SIZE - off))
  OFFS="$OFFS $off"; LENS="$LENS $len"
  off=$((off + len)); NPART=$((NPART + 1))
done

if [ "$MODE" = "sanitize" ]; then
  command -v nc >/dev/null || die "netcat needed on this laptop for the sanitize transport"
  # Which netcat is on the board? traditional wants -l -p PORT, OpenBSD -l PORT.
  NCL=$(rsh 'nc -h 2>&1 | grep -q -- "-p port" && echo "nc -l -p" || echo "nc -l"')

  say "staging the detached sanitize+serve job on $HOSTN"
  { cat <<SERVEHDR
#!/bin/bash
set -u
P='$PASS'; REC='$REC_DIR'; DROP=$DROP_REC; DISK='$OS_DISK'
SERVEHDR
    cat <<'SERVEBODY'
S() { printf '%s\n' "$P" | sudo -S -p '' "$@"; }
S systemctl stop stereocam 2>/dev/null || true
[ "$DROP" = 1 ] && rm -rf "${REC:?}/"* 2>/dev/null
rm -f "$HOME/.config/starcap/device_id" 2>/dev/null || true
S journalctl --rotate >/dev/null 2>&1 || true
S journalctl --vacuum-time=1s >/dev/null 2>&1 || true
S rm -rf /var/log/journal /var/log/*.gz /var/log/*.[0-9] 2>/dev/null || true
cat /dev/null > "$HOME/.bash_history" 2>/dev/null || true
S apt-get clean 2>/dev/null || true
S truncate -s 0 /etc/machine-id 2>/dev/null || true
S rm -f /var/lib/dbus/machine-id 2>/dev/null || true
S sh -c 'rm -f /var/lib/dhcp/* /var/lib/NetworkManager/*.lease*' 2>/dev/null || true
S systemctl enable stereocam-firstboot.service >/dev/null 2>&1 || true
S systemctl enable stereocam-resizefs.service  >/dev/null 2>&1 || true
S rm -f /var/lib/stereocam/resized 2>/dev/null || true
# Host keys go LAST in the sanitize: from here on sshd accepts nothing new.
S sh -c 'rm -f /etc/ssh/ssh_host_*'
sync
echo "SANITIZED" > /tmp/starcap-serve.stage
# Zeroing AFTER the deletes, so the freed 22G of recordings compresses away too.
S sh -c 'dd if=/dev/zero of=/EMPTY bs=4M 2>/dev/null; rm -f /EMPTY; sync'
echo "ZEROED" > /tmp/starcap-serve.stage
SERVEBODY
    i=0; set -- $OFFS; o_list="$*"; set -- $LENS; l_list="$*"
    i=0
    for o in $o_list; do
      l=$(echo "$l_list" | cut -d' ' -f$((i+1)))
      echo "S dd if=\$DISK bs=4M skip=$o count=$l iflag=skip_bytes,count_bytes status=none | $COMP | $NCL $((BASE_PORT+i)) &"
      i=$((i+1))
    done
    echo 'echo "SERVING" > /tmp/starcap-serve.stage'
    echo 'wait'
    echo 'echo "SERVED" > /tmp/starcap-serve.stage'
    # Reboot restores sshd (firstboot regenerates keys) and the recorder's identity.
    echo 'sleep 5; S reboot'
  } > "$PARTS/serve.sh"

  rsh 'cat > /tmp/starcap-serve.sh && chmod 700 /tmp/starcap-serve.sh' < "$PARTS/serve.sh"
  rsh 'setsid nohup bash /tmp/starcap-serve.sh >/tmp/starcap-serve.log 2>&1 </dev/null & echo launched'
  warn "SSH to the board dies from here — it comes back after the automatic reboot."

  say "waiting for $HOSTN to sanitize + zero, then serve on $BASE_PORT..$((BASE_PORT+NPART-1))"
  t=0
  until timeout 2 bash -c "exec 3<>/dev/tcp/$BOARD/$BASE_PORT" 2>/dev/null; do
    sleep 10; t=$((t+10))
    [ $((t % 60)) = 0 ] && echo "    still preparing… ${t}s"
    [ "$t" -gt 3600 ] && die "board never started serving (check /tmp/starcap-serve.log after it reboots)"
  done

  say "pulling $NPART part(s) over netcat"
  pids=""; i=0
  for o in $OFFS; do
    part=$(printf '%s/part.%03d' "$PARTS" "$i")
    nc "$BOARD" $((BASE_PORT+i)) > "$part" &
    pids="$pids $!"; i=$((i+1))
  done
  fail=0; for p in $pids; do wait "$p" || fail=1; done
  [ "$fail" = 0 ] || die "a netcat worker failed — parts kept in $PARTS"
else
  say "reading $OS_DISK over $JOBS SSH stream(s)"
  pids=""; i=0
  for o in $OFFS; do
    l=$(echo $LENS | cut -d' ' -f$((i+1)))
    part=$(printf '%s/part.%03d' "$PARTS" "$i")
    $SSH "$SSH_USER@$BOARD" \
      "printf '%s\n' '$PASS' | sudo -S -p '' dd if=$OS_DISK bs=4M skip=$o count=$l iflag=skip_bytes,count_bytes status=none 2>/dev/null | $COMP" \
      > "$part" &
    pids="$pids $!"; i=$((i+1))
  done
  fail=0; for p in $pids; do wait "$p" || fail=1; done
  [ "$fail" = 0 ] || die "a capture worker failed — parts kept in $PARTS"
fi

say "joining $NPART part(s) in offset order"
for f in "$PARTS"/part.*; do [ -s "$f" ] || die "empty part $f — refusing to join a short image"; done
cat "$PARTS"/part.* > "$OUT"
rm -rf "$PARTS"; trap - EXIT

[ -s "$OUT" ] || die "capture produced an empty file"

if [ "$VERIFY" = 1 ]; then
  say "verifying the image expands to exactly $HUMAN"
  case "$OUT" in *.zst) GOT=$(zstd -dc "$OUT" | wc -c) ;; *) GOT=$(gunzip -c "$OUT" | wc -c) ;; esac
  [ "$GOT" = "$OS_SIZE" ] || die "size mismatch: $GOT vs $OS_SIZE — DO NOT FLASH THIS."
  echo "  ok — $GOT bytes, exact"
fi

say "done"; ls -lh "$OUT"
cat <<EOF

  wrote: $OUT

  decompress:  $(case "$OUT" in *.zst) echo "zstd -d '$OUT'";; *) echo "gunzip -k '$OUT'";; esac)

  This is a whole-disk image of $OS_DISK (GPT + uboot + all 9 partitions), so it
  restores with a plain dd to another R76S eMMC, or:
    SD card :  sudo ~/starcap-ops/flashing/nanopi-r76s/flash_sd.sh /dev/sdX <image>
    eMMC    :  sudo ~/starcap-ops/flashing/nanopi-r76s/flash_nanopi_emmc.sh <image dir>
               (maskrom; a 471 err=-7 is the A-to-A cable, re-seat and retry)
EOF
[ "$MODE" = "capture-only" ] && cat <<EOF
  WARNING: this image is a TWIN. It still carries device_id $DEVID, the SSH host
  keys and the machine-id. Flash it to a second board and BOTH will claim that
  id — peer discovery and upload attribution break. Clear them on the clone
  before it joins the fleet.
EOF
[ "$MODE" = "sanitize" ] && cat <<EOF
  $HOSTN has given up its own identity too — boot it once, let it re-assign,
  then re-check it against starcap-ops/inventory/vin-tracker/pi_emmc_speeds.csv.
EOF
exit 0
