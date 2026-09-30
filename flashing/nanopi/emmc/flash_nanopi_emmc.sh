#!/usr/bin/env bash
# flash_nanopi_emmc.sh — flash a FriendlyElec RK3576 NanoPi's eMMC over
# USB/maskrom, from one of their `rk3576-usb-*` image packages, so the board
# boots that OS straight from eMMC. This is the Linux stand-in for running the
# package's own RKDevTool.exe on Windows: same loader, same offsets, same files.
#
# Companion to clone_sd_to_emmc.sh (same job, but for the RK3588 Orange Pi 5/5B
# recorders). Five things differ on the NanoPi, all handled below:
#
#   1. rkdeveloptool CANNOT DO THIS SOC — use upgrade_tool (see below).
#   2. Different SoC → different RAM loader. clone_sd_to_emmc.sh boots
#      assets/rk3588_spl_loader_*.bin; that will not run on an RK3576. The
#      image package ships the right one: MiniLoaderAll.bin (DDR init v1.13 +
#      U-Boot SPL, both built for rk3576).
#   3. Not one image — nine. The golden Orange Pi image is a whole-disk blob
#      written at sector 0. A FriendlyElec `-usb-` package is per-partition
#      images (uboot, misc, dtbo, resource, kernel, boot, rootfs, userdata)
#      that each have to land in the partition declared in parameter.txt. This
#      script PARSES parameter.txt's mtdparts CMDLINE instead of hardcoding
#      anything, so a newer image revision with a different layout still
#      flashes correctly — and it checks every image fits its partition.
#   4. The partition table has to be written explicitly. The blob image carried
#      its own; this one only describes it (parameter.txt, TYPE: GPT). Its last
#      entry, `userdata:grow`, gets sized to fill whatever eMMC is really there.
#   5. rootfs.img and userdata.img are Android sparse images (magic 0x3aff26ed),
#      not raw filesystems — rootfs.img is 5.2 GB on disk, 5.5 GiB expanded.
#      Both tools expand sparse chunks as they upload, so they're written as-is.
#      `--unsparse` forces an explicit simg2img pass first, if ever needed.
#
# ---------------------------------------------------------------------------
# WHY NOT rkdeveloptool (this cost an afternoon — don't repeat it)
# ---------------------------------------------------------------------------
# Ubuntu's `rkdeveloptool` package is a 2021 pine64 snapshot (`rkdeveloptool -v`
# says 1.0.0; the only SoCs named in the binary are RK281X/RK292X). Point it at
# an RK3576 and it prints "Downloading bootloader..." and HANGS FOREVER — the
# board stays in maskrom, and since libusb is waiting on a transfer with no
# timeout, only Ctrl-C gets you out. Nothing is written, so it's harmless, just
# a dead end. The maskrom download-boot protocol changed for this SoC
# generation; Rockchip's own upgrade_tool changelog dates it:
#     v2.28 (2023/12) "add usb transfer 4m handshake with device"
#     v2.28 (2023/12) "support to usb2 reconnection when failure of switching usb3"
#     v2.29 (2024/02) "support crc off for downloading 471 and 472"
# 471/472 are the DDR-init and loader blobs — precisely the step that hangs.
# Neither upstream rockchip-linux/rkdeveloptool nor Radxa's fork mentions RK3576
# at all, and Rockchip's rkbin copy of upgrade_tool is from 2019 (v1.43).
#
# So this script drives Rockchip's own **upgrade_tool v2.44** (Dec 2024), the
# tool RK3576 vendors document for Linux hosts, kept in-repo at
# assets/upgrade_tool_v2.44_for_linux/ (out of the official RK3576 rkr5 SDK; its
# command-line manual is in there too). It is closed-source, statically linked,
# x86-64. The rkdeveloptool path below is kept only for RK3588 boards and has to
# be asked for explicitly with `--tool rkdeveloptool`.
#
# With upgrade_tool the vendor flow is used verbatim (per its manual §1.2-1.4):
#     db <loader>            download boot into RAM (writes nothing)
#     di -p parameter.txt    write the partition table (must precede images)
#     di -<part> <image>     write one partition, by name
#     ul <loader> -noreset   generate the IDBlock and write the bootloader
# That last one is why this script doesn't hand-place idbloader.img at sector 64
# the way clone_sd_to_emmc.sh splices bootloaders — `ul` does it the vendor way.
#
# THIS PERMANENTLY OVERWRITES THE BOARD'S ENTIRE EMMC (Android or anything else
# already on it).
#
# Requires the board in maskrom mode, cable DIRECT to this machine (no hub) into
# the board's upgrade/OTG port. Per FriendlyElec's own wiki, on the NanoPi R76S
# that is a **USB-A port, so you need an A-to-A cable** (the M5 uses USB-C), and:
#   1. remove the SD card and any other USB peripherals from the board
#   2. hold the "Mask" key
#   3. power the board on FROM ITS OWN SUPPLY (don't rely on bus power — DRAM
#      training is the first heavy load and a browning-out board fails it)
#   4. keep holding until the status LED has been on for AT LEAST 3s, then release
# `lsusb` should then show a Rockchip device (vendor 2207) — 2207:350e is an
# RK3576. Always power-cycle back into maskrom before a retry: a board with a
# blank eMMC sits in maskrom by itself, so seeing "Mode=Maskrom" does NOT prove
# the chip was freshly reset.
#
# Usage:  sudo ./flash_nanopi_emmc.sh [image_dir] [--yes] [--dry-run]
#                                     [--tool upgrade_tool|rkdeveloptool] [--unsparse]
#   image_dir    a FriendlyElec rk3576-usb-* directory (the one holding
#                parameter.txt + MiniLoaderAll.bin). Default: the newest such
#                directory under nanopiIMAGE/.
#   --yes        skip the confirmation prompt
#   --dry-run    parse the package, print the write plan, touch no hardware
#   --probe       read-only: find the board, then read secure-mode/capability/
#                 chip-id/flash-info and stop. For diagnosing repeated
#                 download-boot failures without writing anything.
#   --tool       flashing backend; default upgrade_tool (see above)
#   --loader F   use F as the RAM loader for the download-boot step instead of
#                the package's MiniLoaderAll.bin. Only affects bringing DDR up
#                over USB — the bootloader written to eMMC still comes from the
#                package. Use when download-boot fails with "please check ddr":
#                the package's loader carries a board-specific DDR blob (this
#                one is rk3576_ddr_lp4_1866, LPDDR4 @1866 only, paired with
#                nanopi_m5_usbplug — see `upgrade_tool EXF <loader> <dir>`),
#                whereas rk3576_spl_loader_v1.12.108.bin (not shipped) is Rockchip's
#                generic build (rkbin RKBOOT/RK3576MINIALL.ini) whose DDR blob
#                covers LPDDR4-2112 *and* LPDDR5-2736.
#   --flash-loader F
#                use F for the bootloader WRITTEN to eMMC (default: the
#                package's own). The IDBlock carries a DDR blob too, so a board
#                that only comes up over USB with --loader will also only BOOT
#                with the same file here — the script warns if you override one
#                and not the other.
#   --unsparse   expand sparse images with simg2img before writing (needs
#                android-sdk-libsparse-utils and ~6 GB free in TMPDIR)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UT_DEFAULT="$SCRIPT_DIR/assets/upgrade_tool_v2.44_for_linux/upgrade_tool"
IMAGE_DIR=""
TOOL=upgrade_tool
RAM_LOADER=""
FLASH_LOADER=""
AUTO_YES=0
DRY_RUN=0
PROBE=0
UNSPARSE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) AUTO_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --probe) PROBE=1 ;;
    --unsparse) UNSPARSE=1 ;;
    --tool) shift; TOOL="${1:-}" ;;
    --tool=*) TOOL="${1#*=}" ;;
    --loader) shift; RAM_LOADER="${1:-}" ;;
    --loader=*) RAM_LOADER="${1#*=}" ;;
    --flash-loader) shift; FLASH_LOADER="${1:-}" ;;
    --flash-loader=*) FLASH_LOADER="${1#*=}" ;;
    -h|--help) awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    -*) echo "unknown option: $1"; exit 1 ;;
    *) IMAGE_DIR="$1" ;;
  esac
  shift
done
case "$TOOL" in
  upgrade_tool|rkdeveloptool) : ;;
  *) echo "--tool must be upgrade_tool or rkdeveloptool (got: $TOOL)"; exit 1 ;;
esac

if [ "$DRY_RUN" -ne 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "This needs root: no udev rule ships for the RK3576's USB id (2207:350e),"
  echo "so its maskrom/loader device node is root-only. Re-run:"
  echo "    sudo $0 $*"
  exit 1
fi

# ---------------------------------------------------------------------------
# Backend — one set of verbs, two implementations
# ---------------------------------------------------------------------------
UT_BIN=""
if [ "$TOOL" = upgrade_tool ]; then
  UT_BIN="${UPGRADE_TOOL:-}"
  [ -n "$UT_BIN" ] || { [ -x "$UT_DEFAULT" ] && UT_BIN="$UT_DEFAULT"; }
  [ -n "$UT_BIN" ] || UT_BIN="$(command -v upgrade_tool || true)"
  if [ -z "$UT_BIN" ] && [ "$DRY_RUN" -ne 1 ]; then
    echo "missing upgrade_tool. Expected it at:"
    echo "    $UT_DEFAULT"
    echo "It's Rockchip's own flashing tool, out of the RK3576 rkr5 SDK"
    echo "(tools/linux/Linux_Upgrade_Tool/upgrade_tool_v2.44_for_linux.zip)."
    echo "Set UPGRADE_TOOL=/path/to/upgrade_tool to point somewhere else."
    exit 1
  fi
elif [ "$DRY_RUN" -ne 1 ]; then
  command -v rkdeveloptool >/dev/null || { echo "missing rkdeveloptool — install with: sudo apt-get install -y rkdeveloptool"; exit 1; }
  rkdeveloptool -h 2>&1 | grep -q 'write-partition-table' || {
    echo "this rkdeveloptool lacks the long-form commands (boot / write /"
    echo "write-partition-table). Install Ubuntu's package: sudo apt-get install -y rkdeveloptool"
    exit 1
  }
fi

# upgrade_tool addresses partitions by name; these abbreviations are the ones it
# defines (manual §1.4), anything else is passed as -<partition-name>.
# (-re for resource comes from the tool's own `DI <-p|-b|-k|-s|-r|-m|-u|-t|-re image>`
# usage line, and FriendlyElec's R76S wiki uses exactly `di -re resource.img`.)
ut_switch() {
  case "$1" in
    boot) echo -b ;; kernel) echo -k ;; recovery) echo -r ;; system) echo -s ;;
    uboot) echo -u ;; misc) echo -m ;; trust) echo -t ;; resource) echo -re ;;
    *) echo "-$1" ;;
  esac
}

t_list()       { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" ld;              else rkdeveloptool list; fi; }
t_db()         { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" db "$RAM_LOADER"; else rkdeveloptool boot "$RAM_LOADER"; fi; }
t_chip_info()  { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" rci;             else rkdeveloptool read-chip-info; fi; }
t_flash_info() { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" rfi 2>&1;        else rkdeveloptool read-flash-info 2>&1; fi; }
t_partitions() { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" pl;              else rkdeveloptool list-partitions; fi; }
t_reset()      { if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" rd;              else rkdeveloptool reset; fi; }
t_gpt()        {
  if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" di -p "$IMAGE_DIR/parameter.txt"
  else rkdeveloptool write-partition-table "$IMAGE_DIR/parameter.txt"; fi
}
t_write_part() {  # <partition-name> <start-sector> <file>
  if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" di "$(ut_switch "$1")" "$3"
  else rkdeveloptool write "$2" "$3"; fi
}
# upgrade_tool's console output is terse ("Read capability Fail!"); its log has
# the actual libusb error codes, and lives in ~/upgrade_tool/log — which is
# /root/... under sudo, so it's easy to miss entirely.
show_ut_log_errors() {
  [ "$TOOL" = upgrade_tool ] || return 0
  local log
  log="$(ls -t "${HOME:-/root}"/upgrade_tool/log/Log*.txt 2>/dev/null | head -1)"
  [ -n "$log" ] || return 0
  echo
  echo "-- error codes from the tool's own log ($log) --"
  grep -E 'Error|ERROR' "$log" | tail -6 | sed 's/^/   /'
  echo "   (err=-1 is LIBUSB_ERROR_IO — the transfer failed on the link, i.e."
  echo "    cable/port/connector, not DDR and not power.)"
}

t_write_loader() {
  if [ "$TOOL" = upgrade_tool ]; then
    # generates the IDBlock from the loader and writes it to storage (manual §1.3)
    "$UT_BIN" ul "$FLASH_LOADER" -noreset
  elif [ -f "$IMAGE_DIR/idbloader.img" ]; then
    # rkdeveloptool has no equivalent: place the RKNS idblock by hand, at the
    # sector the boot ROM reads it from.
    rkdeveloptool write 64 "$IMAGE_DIR/idbloader.img"
  fi
}

if [ "$UNSPARSE" -eq 1 ]; then
  command -v simg2img >/dev/null || { echo "--unsparse needs simg2img — install with: sudo apt-get install -y android-sdk-libsparse-utils"; exit 1; }
fi

# ---------------------------------------------------------------------------
# Locate the image package
# ---------------------------------------------------------------------------
if [ -z "$IMAGE_DIR" ]; then
  # newest package under nanopiIMAGE/ — one directory per build date
  IMAGE_DIR="$(find "$SCRIPT_DIR/nanopiIMAGE" -maxdepth 2 -name parameter.txt -printf '%T@ %h\n' 2>/dev/null \
                 | sort -rn | head -1 | cut -d' ' -f2-)"
fi
if [ -z "$IMAGE_DIR" ]; then
  echo "no image package found under $SCRIPT_DIR/nanopiIMAGE"
  echo "unzip a FriendlyElec rk3576-usb-*.zip in there, or pass the directory as an argument."
  exit 1
fi
IMAGE_DIR="${IMAGE_DIR%/}"
[ -f "$IMAGE_DIR/parameter.txt" ] || { echo "not an RKDevTool image package (no parameter.txt): $IMAGE_DIR"; exit 1; }
LOADER="$IMAGE_DIR/MiniLoaderAll.bin"
[ -f "$LOADER" ] || { echo "missing the RK3576 RAM loader: $LOADER"; exit 1; }
# The RAM loader (download-boot) is the package's own unless --loader overrides
# it; what gets WRITTEN to eMMC is always the package's, either way.
[ -n "$RAM_LOADER" ] || RAM_LOADER="$LOADER"
[ -f "$RAM_LOADER" ] || { echo "--loader file not found: $RAM_LOADER"; exit 1; }
[ -n "$FLASH_LOADER" ] || FLASH_LOADER="$LOADER"
[ -f "$FLASH_LOADER" ] || { echo "--flash-loader file not found: $FLASH_LOADER"; exit 1; }

TITLE=""; BOARD=""
if [ -f "$IMAGE_DIR/info.conf" ]; then
  TITLE="$(sed -n 's/^title=//p' "$IMAGE_DIR/info.conf" | head -1)"
  BOARD="$(sed -n 's/^require-board=//p' "$IMAGE_DIR/info.conf" | head -1)"
fi
echo "== image package: $IMAGE_DIR =="
[ -n "$TITLE" ] && echo "   ${TITLE}${BOARD:+  (board: $BOARD)}"
echo "   backend: $TOOL${UT_BIN:+  ($UT_BIN)}"
[ "$RAM_LOADER" = "$LOADER" ] || echo "   RAM loader override:  $RAM_LOADER"
[ "$FLASH_LOADER" = "$LOADER" ] || echo "   eMMC bootloader override: $FLASH_LOADER"
# What DDR-init/usbplug blobs does the RAM loader actually carry? Both are built
# per board, and a "please check ddr" download-boot failure is usually the DDR
# one not matching the board's memory — worth seeing before you flash.
if [ "$TOOL" = upgrade_tool ] && [ -n "$UT_BIN" ]; then
  LOADER_PARTS="$(mktemp -d)"
  if "$UT_BIN" EXF "$RAM_LOADER" "$LOADER_PARTS" >/dev/null 2>&1; then
    PARTS="$(cd "$LOADER_PARTS" && ls 2>/dev/null | grep -vE '^(UsbHead|FlashHead|FlashBoost|FlashData|FlashBoot)\.bin$' | sed 's/\.bin$//' | tr '\n' ' ')"
    [ -n "$PARTS" ] && echo "   loader carries: $PARTS"
  fi
  rm -rf -- "$LOADER_PARTS"
fi
if [ -n "$BOARD" ] && [ "$BOARD" != "rk3576" ]; then
  echo "!! this package says require-board=$BOARD, not rk3576 — wrong script for it."
  exit 1
fi

# ---------------------------------------------------------------------------
# Partition layout, straight out of parameter.txt
# ---------------------------------------------------------------------------
# Rockchip parameter.txt describes the table in an mtdparts CMDLINE, in units of
# 512-byte sectors:  size@offset(name)  — with "-" size and ":grow" on the last
# partition meaning "take the rest of the flash".
CMDLINE="$(grep -m1 '^CMDLINE:' "$IMAGE_DIR/parameter.txt" || true)"
[ -n "$CMDLINE" ] || { echo "parameter.txt has no CMDLINE line — can't work out the partition layout"; exit 1; }
MTD="${CMDLINE#*mtdparts=}"
MTD="${MTD%%[[:space:]]*}"
MTD="${MTD#*:}"                     # drop the "rk29xxnand:" device prefix

# expanded payload size of a file: for an Android sparse image that's
# blk_sz * total_blks out of its header, otherwise just the file size.
is_sparse() { [ "$(od -An -tx4 -N4 -- "$1" | tr -d ' \n')" = "ed26ff3a" ]; }
payload_bytes() {
  local blk cnt
  if is_sparse "$1"; then
    read -r blk cnt < <(od -An -tu4 -j12 -N8 -- "$1")
    echo $((blk * cnt))
  else
    stat -c%s -- "$1"
  fi
}
human() { if [ "$1" -ge 1048576 ]; then echo "$(($1 / 1048576)) MB"; else echo "$(($1 / 1024)) KB"; fi; }

W_NAME=(); W_FILE=(); W_OFF=(); W_BYTES=()
SKIPPED=""
NEED_BYTES=0
IFS=',' read -ra RAW_PARTS <<<"$MTD"
for part in "${RAW_PARTS[@]}"; do
  [[ "$part" =~ ^(-|0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)\(([^\)]+)\)$ ]] || {
    echo "can't parse this mtdparts entry from parameter.txt: $part"; exit 1; }
  P_SIZE="${BASH_REMATCH[1]}"; P_OFF=$((BASH_REMATCH[2])); P_NAME="${BASH_REMATCH[3]%%:*}"
  P_SECTORS=-1
  [ "$P_SIZE" = "-" ] || P_SECTORS=$((P_SIZE))

  FILE="$IMAGE_DIR/$P_NAME.img"
  if [ ! -f "$FILE" ]; then
    # e.g. `recovery` is declared in the table but ships no image — same as
    # RKDevTool's config.cfg, which has no row for it either.
    SKIPPED="$SKIPPED $P_NAME"
    continue
  fi

  BYTES="$(payload_bytes "$FILE")"
  if [ "$P_SECTORS" -ge 0 ] && [ "$BYTES" -gt $((P_SECTORS * 512)) ]; then
    echo "!! $P_NAME.img needs $((BYTES / 1024 / 1024)) MB but the $P_NAME partition is only $((P_SECTORS / 2048)) MB — aborting."
    exit 1
  fi
  W_NAME+=("$P_NAME"); W_FILE+=("$FILE"); W_OFF+=("$P_OFF"); W_BYTES+=("$BYTES")
  END=$((P_OFF * 512 + BYTES))
  [ "$END" -gt "$NEED_BYTES" ] && NEED_BYTES="$END"
done
[ "${#W_NAME[@]}" -gt 0 ] || { echo "parameter.txt lists partitions but none of their .img files are in $IMAGE_DIR"; exit 1; }

echo
printf '   %-10s %10s %9s  %s\n' partition sector size file
for i in "${!W_NAME[@]}"; do
  printf '   %-10s %10s %9s  %s%s\n' "${W_NAME[$i]}" "${W_OFF[$i]}" \
    "$(human "${W_BYTES[$i]}")" "$(basename "${W_FILE[$i]}")" \
    "$(is_sparse "${W_FILE[$i]}" && echo '  (sparse)')"
done
if [ "$TOOL" = upgrade_tool ]; then
  printf '   %-10s %10s %9s  %s\n' bootloader '(idblock)' \
    "$(human "$(stat -c%s "$LOADER")")" "MiniLoaderAll.bin  (via ul)"
elif [ -f "$IMAGE_DIR/idbloader.img" ]; then
  printf '   %-10s %10s %9s  %s\n' bootloader 64 \
    "$(human "$(stat -c%s "$IMAGE_DIR/idbloader.img")")" idbloader.img
fi
[ -n "$SKIPPED" ] && echo "   declared in parameter.txt but no image shipped (skipping):$SKIPPED"
NEED_MB=$((NEED_BYTES / 1024 / 1024))
echo "   total eMMC needed: $NEED_MB MB"

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "--dry-run: nothing written, no device touched."
  exit 0
fi

# ---------------------------------------------------------------------------
# Find the board
# ---------------------------------------------------------------------------
echo
echo "== looking for a board in maskrom mode =="
MODE=""
for _ in $(seq 1 20); do
  LIST="$(t_list 2>/dev/null || true)"
  if grep -qi maskrom <<<"$LIST"; then MODE=maskrom; break; fi
  if grep -qi loader  <<<"$LIST"; then MODE=loader;  break; fi
  sleep 1
done
if [ -z "$MODE" ]; then
  cat <<'EOF'
No board found after 20s. Checklist:
  - cable plugged DIRECTLY into this machine, no USB hub in between
  - cable is a real DATA cable (some are charge-only)
  - plugged into the board's USB-A upgrade port with an A-to-A cable (the M5 uses USB-C)
  - fully unplugged power first, held the MASKROM button, powered up WHILE
    holding it, released ~2-3s after
Run `lsusb` — a Rockchip device (vendor 2207) should appear once it's right.
EOF
  exit 1
fi
echo "   found ($MODE mode):"
echo "$LIST"

# Which chip is really on the other end of the cable?
USB_ID="$(sed -n 's/.*Pid=0x\([0-9a-f]*\).*/\1/p' <<<"$LIST" | head -1)"
case "$USB_ID" in
  350e|'') : ;;                    # 350e = RK3576; empty = couldn't read it, carry on
  350a|350b)
    echo "!! attached Rockchip device is 2207:$USB_ID — an RK3568/RK3588, not an RK3576 NanoPi."
    echo "   (Orange Pi 5/5B recorders are RK3588 → use clone_sd_to_emmc.sh for those.)"
    exit 1 ;;
  *)
    echo "   ⚠ attached Rockchip USB id is 2207:$USB_ID (expected 350e for RK3576) — continuing, but check the board." ;;
esac
if [ "$TOOL" = rkdeveloptool ] && [ "$USB_ID" = 350e ]; then
  echo "   ⚠ rkdeveloptool on an RK3576 hangs at \"Downloading bootloader...\" forever"
  echo "     (see this script's header). You asked for it with --tool rkdeveloptool;"
  echo "     Ctrl-C and drop the flag to use upgrade_tool instead."
fi

if [ "$PROBE" -eq 1 ]; then
  # Read-only interrogation of the chip, for when download-boot keeps failing.
  # Most of these need a loader running, so in maskrom mode expect several to
  # error — what matters is secure-mode (a locked chip rejects any unsigned
  # loader, which looks exactly like a DDR failure) and the capability word.
  for probe in TD:test-device RSM:read-secure-mode RCB:read-capability RCI:read-chip-info RID:read-flash-id RFI:read-flash-info; do
    ut_cmd="${probe%%:*}"; rk_cmd="${probe#*:}"
    echo "-- ${ut_cmd} / ${rk_cmd} --"
    if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" "$ut_cmd" 2>&1 | grep -v '^Using ' || true
    else rkdeveloptool "$rk_cmd" 2>&1 || true; fi
  done
  show_ut_log_errors
  echo
  echo "--probe: read-only, nothing written."
  exit 0
fi

if [ "$MODE" = loader ]; then
  # Something bootable is already on the eMMC and answered in rockusb mode. Bounce
  # it to maskrom so the loader doing the writing is the one from THIS package.
  echo "== board answered in loader mode — bouncing it to maskrom =="
  if [ "$TOOL" = upgrade_tool ]; then "$UT_BIN" rd 3 >/dev/null 2>&1 || true
  else rkdeveloptool reboot-maskrom >/dev/null 2>&1 || true; fi
  for _ in $(seq 1 10); do
    if t_list 2>/dev/null | grep -qi maskrom; then MODE=maskrom; break; fi
    sleep 1
  done
  [ "$MODE" = maskrom ] || echo "   ⚠ still in loader mode — using the loader already running on the board"
fi

if [ "$MODE" = maskrom ]; then
  echo "== loading the RK3576 DDR-init + SPL into RAM (writes nothing) =="
  if ! t_db; then
    # upgrade_tool keeps its own log (~/upgrade_tool/log/, so /root/... under
    # sudo) and it says FAR more than the console does. The error code there is
    # what tells you which of the causes below you actually have.
    show_ut_log_errors
    cat <<EOF

Download-boot failed, so nothing was written. Read the error code above first,
because it splits the causes cleanly:

  * "vendor=0x471 failed,err=-1 ... index(0)" — err=-1 is LIBUSB_ERROR_IO and
    index(0) means the FIRST chunk never landed: no DDR code ever ran, so this
    is NOT a DDR/memory/power problem. It's the link itself (-EPROTO/-EILSEQ,
    i.e. bit-stuff/CRC errors). Enumeration still works because the kernel
    retries small control transfers, which is why the board looks fine in
    \`lsusb\`. Swap the data cable first (on the R76S it's USB A-to-A, and cheap
    ones fail exactly like this), then try a different port on this machine.
    If reads like RCB/RCI also fail with err=-1, it's the same story — check
    with: sudo $0 --probe
  * a timeout, or a failure at a LATER index — then the chip took the code and
    stopped answering, which is where the list below applies.

  1. The chip needs a CLEAN power-cycle per attempt (the tool says so too), and
     the Mask key held LONGER than you'd think — FriendlyElec's wiki says to
     keep holding until the status LED has been on for at least 3s. Unplug the
     board's power AND the cable, wait ~5s, pull the SD card and any USB
     peripherals, then re-enter maskrom and retry. A board with a blank eMMC
     sits in maskrom by itself, so "Mode=Maskrom" does NOT prove a fresh reset.
  2. Underpowered board. DRAM training is the first heavy load: power the board
     from its own supply, not off the data cable (this maskrom device asks the
     bus for only 400mA, and an R76S has two 2.5G PHYs to feed).
  3. Wrong DDR blob for this board's memory. The package's loader carries a
     board-specific one — check what it actually targets with:
         $UT_BIN EXF "$LOADER" /tmp/loader-parts
     If it names a different board than the one on your desk, you need that
     board's image package. Rockchip's generic loader (rk3576_spl_loader
     v1.12.108) was already tried against err=-7 on 2026-08-01 and was not the
     fault, so it is not shipped; pass another loader with --loader to try one.
EOF
    exit 1
  fi
  for _ in $(seq 1 10); do
    t_list 2>/dev/null | grep -qi loader && break
    sleep 1
  done
  sleep 1
fi

echo "== reading chip + eMMC info =="
t_chip_info || true
FLASH_INFO="$(t_flash_info)"
echo "$FLASH_INFO"
# rkdeveloptool prints "Flash Size: 30528 MB", upgrade_tool "Flash Size: 29820MB,value=0x..."
EMMC_MB="$(grep -oP 'Flash Size:\s*\K[0-9]+(?=\s*MB)' <<<"$FLASH_INFO" | head -1 || true)"
if [ -n "${EMMC_MB:-}" ]; then
  if [ "$NEED_MB" -gt "$EMMC_MB" ]; then
    echo "!! this image needs $NEED_MB MB but the board's eMMC is only $EMMC_MB MB — aborting."
    exit 1
  fi
  echo "   fits: $NEED_MB MB of image onto $EMMC_MB MB of eMMC"
else
  echo "   ⚠ couldn't read the eMMC size — is there actually eMMC on this board?"
fi

if [ "$AUTO_YES" -ne 1 ]; then
  echo
  echo "!! About to OVERWRITE this board's ENTIRE eMMC with ${TITLE:-this image} (everything on it gone for good) !!"
  read -r -p "Type 'flash' to continue: " CONFIRM
  [ "$CONFIRM" = "flash" ] || { echo "aborted — nothing written."; exit 1; }
fi

# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
echo
echo "== writing the GPT partition table (from parameter.txt) =="
t_gpt

UNSPARSE_TMP=""
cleanup() { [ -n "$UNSPARSE_TMP" ] && rm -rf -- "$UNSPARSE_TMP"; }
trap cleanup EXIT
[ "$UNSPARSE" -eq 1 ] && UNSPARSE_TMP="$(mktemp -d)"

for i in "${!W_NAME[@]}"; do
  FILE="${W_FILE[$i]}"
  RAW=""
  if [ "$UNSPARSE" -eq 1 ] && is_sparse "$FILE"; then
    RAW="$UNSPARSE_TMP/${W_NAME[$i]}.raw"
    echo "-- expanding ${W_NAME[$i]} with simg2img ($(human "${W_BYTES[$i]}")) --"
    simg2img "$FILE" "$RAW"
    FILE="$RAW"
  fi
  echo "-- ${W_NAME[$i]} → sector ${W_OFF[$i]} ($(human "${W_BYTES[$i]}")) --"
  t_write_part "${W_NAME[$i]}" "${W_OFF[$i]}" "$FILE"
  [ -n "$RAW" ] && rm -f -- "$RAW"   # reclaim the space before the next one
done

# Bootloader last on purpose: if a write above dies half-way, the board stays
# unbootable (and obviously in need of a reflash) rather than booting into a
# half-written rootfs.
if [ "$RAM_LOADER" != "$LOADER" ] && [ "$FLASH_LOADER" = "$LOADER" ]; then
  echo "   ⚠ this board needed $(basename "$RAM_LOADER") to come up over USB, but the"
  echo "     bootloader about to be written is the package's $(basename "$LOADER")."
  echo "     The IDBlock carries a DDR blob too — if the package's doesn't suit this"
  echo "     board's memory it'll flash fine and then not boot. In that case re-run"
  echo "     with: --flash-loader $RAM_LOADER"
fi
echo "-- bootloader IDBlock ($(basename "$FLASH_LOADER")) --"
t_write_loader

echo "== partition table now on the board =="
t_partitions || true

echo "== rebooting the board =="
t_reset || true

cat <<EOF

Done. Unplug the A-to-A data cable and power-cycle the board — it now boots
${TITLE:-the flashed OS} from eMMC, no SD card involved. The last partition
(userdata) was sized to fill the rest of the eMMC by the GPT write, and this
first boot does the image's own first-boot work (filesystem resize, SSH host
keys, etc.), so give it a couple of minutes before expecting a desktop or a
network answer.

To flash the next board: power-cycle it into maskrom the same way and re-run.
EOF
