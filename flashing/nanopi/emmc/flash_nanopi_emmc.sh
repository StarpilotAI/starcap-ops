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
#   --no-verify  skip the read-back check after writing (see VERIFY below).
#                Only for chasing a problem; a board flashed this way is unproven.
#   --ports      no root needed: list every Rockchip board on USB with its port,
#                controller and speed, and flag boards that share one link
#                (several behind the same hub). Touches no board.
#   --port P     flash only the board on USB port P (a name from --ports, e.g.
#                1-2). Without it (or --all), the script refuses to run while
#                more than one board is plugged in.
#   --all        flash EVERY board on USB at once from this one terminal: one
#                confirmation, then a background `--port P --yes` run per
#                board, a live status line each, and PASSED/FAILED per board at
#                the end. Each board's full output goes to logs/<time>/port-P.log.
#   --update     with --all: also keep each board's ethernet in this laptop's
#                dock port. As each flashed board boots onto the dock LAN it
#                gets fleet/dock-check/dock_check.py --yes (pinned to main,
#                updates on, self-updated to latest main), and the run is only
#                done once every board is on latest main.
#
# Several boards at once: put each on its OWN laptop port (boards behind one
# hub split a single 480 Mbit/s link, so they gain nothing), then
#     ./flash_nanopi_emmc.sh --ports            # check: no SHARED LINK warning
#     sudo ./flash_nanopi_emmc.sh --all         # all of them, one terminal
#     sudo ./flash_nanopi_emmc.sh --all --update  # ...and update each to latest main over the dock LAN
# or by hand, one terminal per board:
#     sudo ./flash_nanopi_emmc.sh --port 1-1    # terminal 1
#     sudo ./flash_nanopi_emmc.sh --port 1-2    # terminal 2, and so on
# make_launcher.sh puts two desktop icons: one runs --all, one --all --update.
# Each port gets its own upgrade_tool log under ~/upgrade_tool/port-<P>/.
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
VERIFY=1
PORT=""
LIST_PORTS=0
ALL=0
UPDATE=0
PASS_ARGS=()   # every option except --all, handed on to each --all child run
while [ $# -gt 0 ]; do
  case "$1" in --all|--update) ;; *) PASS_ARGS+=("$1") ;; esac
  case "$1" in
    --yes) AUTO_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --probe) PROBE=1 ;;
    --unsparse) UNSPARSE=1 ;;
    --no-verify) VERIFY=0 ;;
    --tool) shift; TOOL="${1:-}"; PASS_ARGS+=("${1:-}") ;;
    --tool=*) TOOL="${1#*=}" ;;
    --loader) shift; RAM_LOADER="${1:-}"; PASS_ARGS+=("${1:-}") ;;
    --loader=*) RAM_LOADER="${1#*=}" ;;
    --flash-loader) shift; FLASH_LOADER="${1:-}"; PASS_ARGS+=("${1:-}") ;;
    --flash-loader=*) FLASH_LOADER="${1#*=}" ;;
    --port) shift; PORT="${1:-}"; PASS_ARGS+=("${1:-}") ;;
    --port=*) PORT="${1#*=}" ;;
    --ports) LIST_PORTS=1 ;;
    --all) ALL=1 ;;
    --update) UPDATE=1 ;;
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

# ---------------------------------------------------------------------------
# Which USB port each board is on, and whether boards share a link
# ---------------------------------------------------------------------------
# Every Rockchip device (vendor 2207) on USB, one per line:
#   <port> <busnum> <devnum> <pid> <speed Mbit/s> <controller PCI address>
# <port> is the kernel's name for the physical port: 1-2 is root port 2 of
# bus 1 (straight into the laptop), 1-2.3 is port 3 of a hub sitting on 1-2.
USB_SYSFS="${USB_SYSFS:-/sys/bus/usb/devices}"   # overridable only for testing
rk_devices() {
  local d
  for d in "$USB_SYSFS"/*; do
    [ -f "$d/idVendor" ] && [ "$(cat "$d/idVendor")" = 2207 ] || continue
    printf '%s %s %s %s %s %s\n' "$(basename "$d")" "$(cat "$d/busnum")" "$(cat "$d/devnum")" \
      "$(cat "$d/idProduct")" "$(cat "$d/speed")" \
      "$(readlink -f "$d" | sed -E 's#.*/([^/]+)/usb[0-9]+/.*#\1#')"
  done
}
# upgrade_tool's LocationID for whatever board is on port $1 right now.
# v2.44 builds it from libusb_get_port_numbers: the bus number, then one hex
# digit per port along the path, printed with %x. So 1-1 -> 11, 1-2 -> 12,
# 3-1.4 -> 314. (Older tools used (bus << 8) + device address; seen wrong on
# real boards 2026-10-08.)
port_locid() {
  local d="$USB_SYSFS/$1" loc p
  [ -f "$d/idVendor" ] && [ "$(cat "$d/idVendor")" = 2207 ] || return 1
  loc="$(cat "$d/busnum")"
  local IFS=.
  for p in ${1#*-}; do loc=$(( loc * 16 + p )); done
  printf '%x\n' "$loc"
}
# The report behind --ports, also printed at the start of every --port run.
# Two boards share bandwidth only when they hang off the same laptop port
# (the part of the port name before the first "."), i.e. behind the same hub.
# Two boards on different laptop ports of the same controller do NOT: each
# root port has its own 480 Mbit/s link and the controller has plenty spare.
show_ports() {
  local rows n via root sharing="" hubbed=0 slow=""
  rows="$(rk_devices)"
  if [ -z "$rows" ]; then echo "   no Rockchip boards (vendor 2207) on USB right now."; return 0; fi
  n="$(wc -l <<<"$rows")"
  printf '   %-10s %-12s %-6s %-14s %s\n' port usb-id speed controller "connected"
  while read -r name bus dev pid speed pci; do
    root="${name%%.*}"
    if [ "$root" = "$name" ]; then via="straight into the laptop"
    else via="through hub on laptop port $root ($(cat "$USB_SYSFS/$root/product" 2>/dev/null || echo hub))"; hubbed=1; fi
    [ "$name" = "$PORT" ] && via="$via   <- this run"
    printf '   %-10s %-12s %-6s %-14s %s\n' "$name" "2207:$pid" "${speed}M" "$pci" "$via"
    [ "${speed%%.*}" -lt 480 ] && slow="$slow $name"
  done <<<"$rows"
  sharing="$(cut -d' ' -f1 <<<"$rows" | sed 's/\..*//' | sort | uniq -c | awk '$1>1{print $2" ("$1" boards)"}' | tr '\n' ' ')"
  echo
  if [ -n "$sharing" ]; then
    echo "   ⚠ SHARED LINK: these laptop ports each carry several boards through a hub: $sharing"
    echo "     Those boards split one 480 Mbit/s link, so flashing them at once is about"
    echo "     as slow as one after another. Move them onto separate laptop ports."
  elif [ "$n" -gt 1 ]; then
    echo "   ✓ $n boards, each on its own laptop port: none share a link, so they can"
    echo "     all flash at once at full speed."
  fi
  [ "$hubbed" -eq 1 ] && [ -z "$sharing" ] && echo "   ⚠ a board is behind a hub. That works, but a hub is the usual cause of err=-1 link failures."
  [ -n "$slow" ] && echo "   ⚠ running below USB 2 high speed (480M):$slow (bad cable or port, a flash will crawl or fail)"
  [ "$n" -gt 1 ] && [ -z "$PORT" ] && echo "   Flash them all from here with: sudo $0 --all   (or one per terminal: --port <port>)"
  return 0
}

if [ "$LIST_PORTS" -eq 1 ]; then
  echo "== Rockchip boards on USB =="
  show_ports
  exit 0
fi
if [ -n "$PORT" ]; then
  [ "$TOOL" = upgrade_tool ] || { echo "--port only works with upgrade_tool (rkdeveloptool can't pick a device)"; exit 1; }
  [[ "$PORT" =~ ^[0-9]+-[0-9.]+$ ]] || { echo "--port wants a USB port name like 1-2 or 3-1.4 (see --ports), got: $PORT"; exit 1; }
fi
# each port gets its own HOME for upgrade_tool, so each board gets its own log
UT_HOME="${HOME:-/root}"
[ -z "$PORT" ] || UT_HOME="${HOME:-/root}/upgrade_tool/port-$PORT"

if [ "$DRY_RUN" -ne 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "This needs root: no udev rule ships for the RK3576's USB id (2207:350e),"
  echo "so its maskrom/loader device node is root-only. Re-run:"
  echo "    sudo $0 $*"
  exit 1
fi

# ---------------------------------------------------------------------------
# --all: every board on USB, at once, from this one terminal
# ---------------------------------------------------------------------------
# One confirmation here, then this same script runs `--port P --yes` per board
# in the background (exactly what you'd type in one terminal per board), with
# its output in logs/<time>/port-P.log. This loop just redraws one status line
# per board until they've all finished, then says which passed.
#
# --update adds a second leg: keep the boards' ethernet in this laptop's dock
# port, and as each flashed board boots and shows up on the dock LAN, run
# fleet/dock-check/dock_check.py --yes on it (pin to main, updates on, restart
# stereocam so it self-updates from GitHub, wait until it IS on latest main).
# A board is done only when that passes. Which LAN board is which USB port
# isn't knowable (the image is identical), so a LAN board counts as "ours" when
# it's a NanoPi that booted after this run started.
[ "$UPDATE" -eq 0 ] || [ "$ALL" -eq 1 ] || { echo "--update goes with --all (sudo $0 --all --update)"; exit 1; }
if [ "$ALL" -eq 1 ]; then
  [ -z "$PORT" ] || { echo "--all and --port don't mix: --all already flashes every port"; exit 1; }
  [ "$TOOL" = upgrade_tool ] || { echo "--all only works with upgrade_tool (rkdeveloptool can't pick a device)"; exit 1; }
  echo "== Rockchip boards on USB =="
  show_ports
  mapfile -t PORTS < <(rk_devices | cut -d' ' -f1 | sort -V)
  [ "${#PORTS[@]}" -gt 0 ] || { echo "!! no board in maskrom. Plug them in (A-to-A cable, maskrom) and re-run."; exit 1; }

  if [ "$UPDATE" -eq 1 ]; then
    # dock_check runs as whoever ran sudo: it needs their ~/orangepi-recorder
    # clone (and git credentials) to learn what "latest main" is.
    DOCK_CHECK="$(cd "$SCRIPT_DIR" && pwd -P)/../../../fleet/dock-check/dock_check.py"
    [ -f "$DOCK_CHECK" ] || { echo "!! --update needs $DOCK_CHECK"; exit 1; }
    as_user() { if [ -n "${SUDO_USER:-}" ]; then sudo -u "$SUDO_USER" -H "$@"; else "$@"; fi; }
    command -v sshpass >/dev/null || { echo "!! --update needs sshpass (apt install sshpass)"; exit 1; }
    # the dock link: a wired interface (not WiFi) holding an IPv4 address
    DOCK_NET=""
    while read -r ifc cidr; do
      [ -e "/sys/class/net/$ifc/wireless" ] && continue
      case "$ifc" in en*|eth*) DOCK_NET="${cidr%.*/*}"; DOCK_OWN="${cidr%/*}"; break ;; esac
    done < <(ip -4 -o addr show up | awk '{print $2, $4}')
    if [ -z "$DOCK_NET" ]; then
      echo "!! --update: no wired dock link is up. Plug the boards' ethernet into this"
      echo "   laptop's port, then: nmcli connection up starcap-dock"
      echo "   (first time on this laptop: sudo $(cd "$SCRIPT_DIR" && pwd -P)/../../../station/setup_dock_lan.sh)"
      exit 1
    fi
    echo
    echo "== --update: what is latest main? =="
    LATEST="$(as_user python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import dock_check as d
v, src, stale = d.latest_version(d.LOCAL_REPO)
print(v or "", file=sys.stdout); print(src, file=sys.stderr)' "$(dirname "$DOCK_CHECK")" 2>"$SCRIPT_DIR/.latest.err" || true)"
    echo "   $LATEST  ($(cat "$SCRIPT_DIR/.latest.err" 2>/dev/null))"; rm -f "$SCRIPT_DIR/.latest.err"
    [ -n "$LATEST" ] || { echo "!! couldn't work out the latest recorder version; nothing was flashed."; exit 1; }
    echo "   dock LAN: $DOCK_NET.0/24. Boards get updated to $LATEST as they come up there."
  fi

  if [ "$AUTO_YES" -ne 1 ] && [ "$DRY_RUN" -ne 1 ] && [ "$PROBE" -ne 1 ]; then
    echo
    echo "!! About to OVERWRITE the ENTIRE eMMC of ${#PORTS[@]} board(s), on port(s): ${PORTS[*]} !!"
    read -r -p "Type 'flash' to continue: " CONFIRM
    [ "$CONFIRM" = "flash" ] || { echo "aborted — nothing written."; exit 1; }
  fi
  LOG_DIR="$SCRIPT_DIR/logs/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$LOG_DIR"
  declare -A PIDS
  START=$SECONDS
  for p in "${PORTS[@]}"; do
    "$0" "${PASS_ARGS[@]}" --port "$p" --yes </dev/null >"$LOG_DIR/port-$p.log" 2>&1 &
    PIDS[$p]=$!
  done
  # the last line a log printed, with \r progress bars split up and colours off
  last_line() { { tr '\r' '\n' <"$1" | sed 's/\x1b\[[0-9;]*m//g' | grep -v '^[[:space:]]*$' || true; } | tail -n 1 | cut -c1-90; }

  # --update state, one entry per LAN board picked up
  declare -A UPIDS UDONE
  LAN_IPS=()
  UPDATE_DEADLINE=""      # set once every flash has finished
  UPDATE_WAIT=600         # how long a passed board gets to show up on the LAN
  NEXT_SWEEP=0
  pi_ssh() { sshpass -p 12345 ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
               -o ConnectTimeout=4 -o LogLevel=ERROR -o BatchMode=no "pi@$1" "$2" 2>/dev/null; }
  # NanoPis on the dock LAN that booted after this run started and aren't picked up yet
  sweep_lan() {
    local i ip up age pings=(); age=$(( SECONDS - START ))
    # wait on the pings only: a bare `wait` would also block on the flashes
    for i in $(seq 2 254); do ping -c1 -W1 "$DOCK_NET.$i" >/dev/null 2>&1 & pings+=($!); done
    wait "${pings[@]}" 2>/dev/null || true
    for ip in $(ip neigh show | awk -v n="$DOCK_NET." 'index($1,n)==1 && $0 ~ /lladdr/ {print $1}'); do
      [ "$ip" = "$DOCK_OWN" ] && continue
      [ -n "${UPIDS[$ip]:-}" ] && continue
      up="$(pi_ssh "$ip" 'cut -d. -f1 /proc/uptime')" || continue
      [[ "$up" =~ ^[0-9]+$ ]] && [ "$up" -lt "$age" ] || continue
      LAN_IPS+=("$ip")
      as_user python3 "$DOCK_CHECK" --host "$ip" --yes --expect "$LATEST" \
        </dev/null >"$LOG_DIR/update-$ip.log" 2>&1 &
      UPIDS[$ip]=$!
    done
  }
  n_passed() { local p n=0; for p in "${PORTS[@]}"; do grep -q 'VERIFY PASSED' "$LOG_DIR/port-$p.log" && n=$((n + 1)); done; echo "$n"; }

  while :; do
    running=0
    for p in "${PORTS[@]}"; do kill -0 "${PIDS[$p]}" 2>/dev/null && running=1; done
    updating=0
    if [ "$UPDATE" -eq 1 ] && [ "$DRY_RUN" -ne 1 ] && [ "$PROBE" -ne 1 ]; then
      for ip in "${LAN_IPS[@]}"; do kill -0 "${UPIDS[$ip]}" 2>/dev/null && updating=1; done
      [ "$running" -eq 1 ] || [ -n "$UPDATE_DEADLINE" ] || UPDATE_DEADLINE=$(( SECONDS + UPDATE_WAIT ))
      if [ "$SECONDS" -ge "$NEXT_SWEEP" ] && { [ -z "$UPDATE_DEADLINE" ] || [ "$SECONDS" -lt "$UPDATE_DEADLINE" ]; } \
         && [ "${#LAN_IPS[@]}" -lt "$(n_passed)" -o "$running" -eq 1 ]; then
        sweep_lan; NEXT_SWEEP=$(( SECONDS + 10 ))
        for ip in "${LAN_IPS[@]}"; do kill -0 "${UPIDS[$ip]}" 2>/dev/null && updating=1; done
      fi
      waiting_lan=0
      if [ "$running" -eq 0 ] && [ "${#LAN_IPS[@]}" -lt "$(n_passed)" ] && [ "$SECONDS" -lt "$UPDATE_DEADLINE" ]; then waiting_lan=1; fi
    else
      waiting_lan=0
    fi
    printf '\033[H\033[J'
    echo "== flashing ${#PORTS[@]} board(s) — $(( (SECONDS-START)/60 ))m$(( (SECONDS-START)%60 ))s   logs: $LOG_DIR"
    for p in "${PORTS[@]}"; do
      log="$LOG_DIR/port-$p.log"
      if kill -0 "${PIDS[$p]}" 2>/dev/null; then state="running"; else state="finished"; fi
      stage="$({ grep '^== ' "$log" || true; } | tail -n 1 | sed 's/^== //; s/ ==$//')"
      printf '\n  port %-8s %-9s %s\n             %s\n' "$p" "$state" "$stage" "$(last_line "$log")"
    done
    if [ "$UPDATE" -eq 1 ] && [ "$DRY_RUN" -ne 1 ] && [ "$PROBE" -ne 1 ]; then
      echo
      echo "== updating to $LATEST on the dock LAN ($DOCK_NET.0/24) =="
      for ip in "${LAN_IPS[@]}"; do
        if kill -0 "${UPIDS[$ip]}" 2>/dev/null; then state="updating"; else state="finished"; fi
        printf '  %-14s %-9s %s\n' "$ip" "$state" "$(last_line "$LOG_DIR/update-$ip.log")"
      done
      [ "$waiting_lan" -eq 1 ] && echo "  waiting for $(( $(n_passed) - ${#LAN_IPS[@]} )) more flashed board(s) to boot onto the LAN ($(( UPDATE_DEADLINE - SECONDS ))s left)"
      [ "${#LAN_IPS[@]}" -eq 0 ] && [ "$waiting_lan" -eq 0 ] && [ "$running" -eq 1 ] && echo "  (boards show up here once they're flashed and booted)"
    fi
    [ "$running" -eq 1 ] || [ "$updating" -eq 1 ] || [ "$waiting_lan" -eq 1 ] || break
    sleep 2
  done
  echo
  echo "== result =="
  FAILED=0
  for p in "${PORTS[@]}"; do
    rc=0; wait "${PIDS[$p]}" || rc=$?
    log="$LOG_DIR/port-$p.log"
    if [ "$rc" -ne 0 ]; then verdict="FAILED (exit $rc)"; FAILED=1
    elif grep -q 'VERIFY PASSED' "$log"; then verdict="PASSED"
    elif [ "$DRY_RUN" -eq 1 ] || [ "$PROBE" -eq 1 ]; then verdict="ok (nothing written)"
    else verdict="flashed, NOT verified"; fi
    printf '   port %-8s %s\n' "$p" "$verdict"
  done
  if [ "$UPDATE" -eq 1 ] && [ "$DRY_RUN" -ne 1 ] && [ "$PROBE" -ne 1 ]; then
    for ip in "${LAN_IPS[@]}"; do
      rc=0; wait "${UPIDS[$ip]}" || rc=$?
      ver="$(pi_ssh "$ip" 'cat ~/orangepi-recorder/stereocam/VERSION' || true)"
      if [ "$rc" -eq 0 ]; then printf '   lan  %-14s UPDATED: on %s, auto-updating, pinned to main\n' "$ip" "$ver"
      else printf '   lan  %-14s NOT UPDATED (on %s) — see %s\n' "$ip" "${ver:-?}" "$LOG_DIR/update-$ip.log"; FAILED=1; fi
    done
    missing=$(( $(n_passed) - ${#LAN_IPS[@]} ))
    if [ "$missing" -gt 0 ]; then
      echo "   !! $missing flashed board(s) never showed up on the dock LAN within ${UPDATE_WAIT}s:"
      echo "      is each board's ethernet in this laptop's dock port (or a switch on it)?"
      echo "      Once it's up: fleet/dock-check/dock_check.py"
      FAILED=1
    fi
  fi
  if [ "$FAILED" -eq 1 ]; then
    for p in "${PORTS[@]}"; do
      grep -q 'VERIFY PASSED' "$LOG_DIR/port-$p.log" && continue
      echo; echo "-- port $p, last lines (full log: $LOG_DIR/port-$p.log) --"
      { tr '\r' '\n' <"$LOG_DIR/port-$p.log" | grep -v '^[[:space:]]*$' || true; } | tail -n 12 | sed 's/^/   /'
    done
  elif [ "$DRY_RUN" -ne 1 ] && [ "$PROBE" -ne 1 ]; then
    echo
    if [ "$UPDATE" -eq 1 ]; then
      echo "Done: every board is flashed, booted and on $LATEST. Unplug the USB cables."
    else
      echo "Unplug the cables and power-cycle each board: they boot from eMMC now."
    fi
  fi
  # logs belong to whoever ran sudo, not root
  [ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER:" "$SCRIPT_DIR/logs" 2>/dev/null || true
  exit "$FAILED"
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

# With --port, every upgrade_tool call is aimed at that one board with
# `-s <LocationID>` (manual §1.11). The LocationID comes from the physical
# port (see port_locid), so it survives the board re-enumerating after `db` or
# `rd 3`; it's still looked up before every call, which also waits for the
# board to come back on that port. Each port also gets its own HOME, so the tool's log
# for this board (~/upgrade_tool/log) isn't mixed with the other boards'.
ut() {
  if [ -z "$PORT" ]; then "$UT_BIN" "$@"; return; fi
  local loc=""
  for _ in $(seq 1 15); do loc="$(port_locid "$PORT")" && break; sleep 1; done
  [ -n "$loc" ] || { echo "!! no Rockchip board on USB port $PORT any more"; return 1; }
  HOME="$UT_HOME" "$UT_BIN" -s "$loc" "$@"
}
t_list() {
  if [ "$TOOL" != upgrade_tool ]; then rkdeveloptool list; return; fi
  if [ -z "$PORT" ]; then "$UT_BIN" ld; return; fi
  # `ld` lists every board on the machine: keep only this port's line
  local loc
  loc="$(port_locid "$PORT")" || return 0
  HOME="$UT_HOME" "$UT_BIN" ld | grep -i "LocationID=${loc}[[:space:]]" || true
}
t_db()         { if [ "$TOOL" = upgrade_tool ]; then ut db "$RAM_LOADER"; else rkdeveloptool boot "$RAM_LOADER"; fi; }
t_chip_info()  { if [ "$TOOL" = upgrade_tool ]; then ut rci;             else rkdeveloptool read-chip-info; fi; }
t_flash_info() { if [ "$TOOL" = upgrade_tool ]; then ut rfi 2>&1;        else rkdeveloptool read-flash-info 2>&1; fi; }
t_partitions() { if [ "$TOOL" = upgrade_tool ]; then ut pl;              else rkdeveloptool list-partitions; fi; }
t_reset()      { if [ "$TOOL" = upgrade_tool ]; then ut rd;              else rkdeveloptool reset; fi; }
t_gpt()        {
  if [ "$TOOL" = upgrade_tool ]; then ut di -p "$IMAGE_DIR/parameter.txt"
  else rkdeveloptool write-partition-table "$IMAGE_DIR/parameter.txt"; fi
}
t_write_part() {  # <partition-name> <start-sector> <file>
  if [ "$TOOL" = upgrade_tool ]; then ut di "$(ut_switch "$1")" "$3"
  else rkdeveloptool write "$2" "$3"; fi
}
# upgrade_tool's console output is terse ("Read capability Fail!"); its log has
# the actual libusb error codes, and lives in ~/upgrade_tool/log — which is
# /root/... under sudo, so it's easy to miss entirely.
show_ut_log_errors() {
  [ "$TOOL" = upgrade_tool ] || return 0
  local log
  log="$(ls -t "$UT_HOME"/upgrade_tool/log/Log*.txt 2>/dev/null | head -1)"
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
    ut ul "$FLASH_LOADER" -noreset
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
if [ -n "$PORT" ]; then
  echo "   (only the one on USB port $PORT; tool log: $UT_HOME/upgrade_tool/log)"
  mkdir -p "$UT_HOME"
  show_ports
  for _ in $(seq 1 20); do port_locid "$PORT" >/dev/null && break; sleep 1; done
  port_locid "$PORT" >/dev/null || { echo "!! nothing Rockchip on USB port $PORT. Run: $0 --ports"; exit 1; }
  # Prove the port -> LocationID mapping against what upgrade_tool itself lists
  # before anything is aimed with -s. If this ever trips, nothing was written.
  LOC="$(port_locid "$PORT")"
  ALL="$(HOME="$UT_HOME" "$UT_BIN" ld 2>&1 || true)"
  if ! grep -qi "LocationID=${LOC}[[:space:]]" <<<"$ALL"; then
    echo "!! the board on port $PORT should be LocationID=$LOC, but upgrade_tool lists:"
    sed 's/^/     /' <<<"$ALL"
    echo "   So this script's port -> LocationID mapping is wrong for this tool version."
    echo "   Nothing was written. Flash one board at a time (no --port) until it's fixed."
    exit 1
  fi
elif [ "$TOOL" = upgrade_tool ] && [ "$(rk_devices | wc -l)" -gt 1 ]; then
  echo "!! more than one Rockchip board is plugged in, and without --port there's no"
  echo "   telling which one each step would hit. Pick one:"
  show_ports
  exit 1
fi
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
    if [ "$TOOL" = upgrade_tool ]; then ut "$ut_cmd" 2>&1 | grep -v '^Using ' || true
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
  if [ "$TOOL" = upgrade_tool ]; then ut rd 3 >/dev/null 2>&1 || true
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
cleanup() { [ -z "$UNSPARSE_TMP" ] || rm -rf -- "$UNSPARSE_TMP"; }   # must return 0: under set -e its status becomes the exit code
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

# ---------------------------------------------------------------------------
# VERIFY: read the eMMC back and compare it with the images
# ---------------------------------------------------------------------------
# "di ... 100%" and "Download IDB Success" only prove the tool handed the bytes
# over, so while the loader is still running, read the board back with `rl`
# (the board is not reset until this passes):
#   * the GPT header at sector 1 must say "EFI PART"
#   * every image region below sector 65536 is read back and compared byte
#     for byte: uboot, misc, dtbo, resource
#
# Nothing at or past sector 65536 (32 MiB) can be checked this way: the
# package's USB loader (nanopi_m5_usbplug) returns 0xCC for every sector from
# 65536 on, whatever the eMMC holds. Proven 2026-10-08: two boards both read
# 0xCC there after a flash, even in a read that started below the line and
# ran across it, yet the one that was rebooted came up on the flashed OS. So
# kernel, boot, rootfs and userdata are reported "not checkable over USB", not
# failed; the first boot is their check. (The earlier "this eMMC keeps only
# its first 32 MiB" diagnosis was this same loader limit.)
# --no-verify skips the read-back.
VERIFY_FULL_MAX=$((64 * 1024 * 1024))
RL_MAX_SECTOR=65536   # the USB loader reads nothing real from here on (see above)
VERIFY_SAMPLES=16
VERIFY_SAMPLE_BYTES=$((1024 * 1024))

# Print one "<eMMC sector> <file offset> <bytes>" line per region to check, for
# image $1 written at partition sector $2. Every region starts on a sector edge.
verify_plan() {
  python3 - "$1" "$2" "$VERIFY_FULL_MAX" "$VERIFY_SAMPLES" "$VERIFY_SAMPLE_BYTES" <<'PY'
import os, struct, sys
path, part, full_max, n, chunk = sys.argv[1], int(sys.argv[2]), *map(int, sys.argv[3:])
regions = []  # (byte offset in partition, byte offset in file, length)
with open(path, "rb") as f:
    hdr = f.read(28)
    if struct.unpack_from("<I", hdr)[0] == 0xED26FF3A:
        _, _, _, fh, ch, blk, _, nchunks, _ = struct.unpack("<IHHHHIIII", hdr)
        f.seek(fh)
        out = 0
        for _ in range(nchunks):
            ctype, _, csz, tot = struct.unpack("<HHII", f.read(12))
            f.seek(ch - 12, 1)
            data = f.tell()
            if ctype == 0xCAC1:                      # raw: real bytes we can compare
                regions.append((out, data, csz * blk))
            f.seek(data + tot - ch)
            out += csz * blk if ctype != 0xCAC4 else 0
    else:
        regions.append((0, 0, os.path.getsize(path)))
total = sum(r[2] for r in regions)
if total <= full_max:
    picks = [(o, d, l) for o, d, l in regions]
else:
    # n evenly spaced points through the compared bytes, first and last included
    picks, marks = [], [round(i * (total - 1) / (n - 1)) for i in range(n)]
    seen, acc = set(), 0
    for o, d, l in regions:
        for m in marks:
            if acc <= m < acc + l:
                k = (m - acc) // 512 * 512
                k = min(k, max(0, (l - chunk) // 512 * 512))
                if (o + k) not in seen:
                    seen.add(o + k)
                    picks.append((o + k, d + k, min(chunk, l - k)))
        acc += l
for o, d, l in picks:
    if o % 512:
        sys.exit(f"region at partition byte {o} is not sector aligned")
    print(part + o // 512, d, l)
PY
}

verify_emmc() {
  local vdir fails=0 checked=0 bytes=0
  vdir="$(mktemp -d)"
  echo
  echo "== verifying: reading the eMMC back =="
  if ut rl 0 2 "$vdir/gpt.bin" >/dev/null 2>&1 \
     && [ "$(dd if="$vdir/gpt.bin" bs=1 skip=512 count=8 2>/dev/null)" = "EFI PART" ]; then
    echo "   gpt        ok"
  else
    echo "   gpt        FAIL: sector 1 does not hold a GPT header"
    fails=$((fails + 1))
  fi
  for i in "${!W_NAME[@]}"; do
    local name="${W_NAME[$i]}" file="${W_FILE[$i]}" plan sec off len n bad=0 nreg=0 skipped=0 got first=""
    plan="$(verify_plan "$file" "${W_OFF[$i]}")" || { echo "   $name: couldn't plan the check"; fails=$((fails + 1)); continue; }
    while read -r sec off len; do
      [ -n "$sec" ] || continue
      n=$(( (len + 511) / 512 ))
      if [ $(( sec + n )) -gt "$RL_MAX_SECTOR" ]; then skipped=$((skipped + 1)); continue; fi
      rm -f "$vdir/rb.bin"
      ut rl "$sec" "$n" "$vdir/rb.bin" >/dev/null 2>&1 || true
      got="$(stat -c%s "$vdir/rb.bin" 2>/dev/null || echo 0)"
      if [ "$got" -lt "$len" ]; then
        [ -n "$first" ] || first="sector $sec could not be read (got $got of $len bytes)"
        bad=$((bad + 1))
      elif ! cmp -s -n "$len" "$vdir/rb.bin" \
             <(dd if="$file" iflag=skip_bytes,count_bytes skip="$off" count="$len" bs=1M 2>/dev/null); then
        [ -n "$first" ] || first="sector $sec holds $(od -An -tx1 -N8 "$vdir/rb.bin" | tr -d ' \n')..."
        bad=$((bad + 1))
      fi
      nreg=$((nreg + 1)); bytes=$((bytes + len))
    done <<<"$plan"
    if [ "$nreg" -eq 0 ] && [ "$skipped" -gt 0 ]; then
      printf '   %-10s not checkable over USB (past 32 MiB; the first boot proves it)\n' "$name"
      continue
    fi
    checked=$((checked + 1))
    if [ "$bad" -eq 0 ]; then
      printf '   %-10s ok  (%d region%s)\n' "$name" "$nreg" "$([ "$nreg" = 1 ] || echo s)"
    else
      printf '   %-10s FAIL  %d of %d region%s wrong; first: %s\n' "$name" "$bad" "$nreg" "$([ "$nreg" = 1 ] || echo s)" "$first"
      fails=$((fails + 1))
    fi
  done
  rm -rf -- "$vdir"
  echo "   read back $(human "$bytes") across $checked partitions"
  return "$fails"
}

if [ "$VERIFY" -eq 0 ]; then
  echo
  echo "   ⚠ --no-verify: nothing was read back, so this flash is unproven."
elif [ "$TOOL" != upgrade_tool ]; then
  echo
  echo "   ⚠ read-back verify needs upgrade_tool; skipped with --tool $TOOL."
elif ! verify_emmc; then
  cat <<EOF

!! VERIFY FAILED: the eMMC does not hold what was just written to it.
   The board was NOT rebooted; it is still in loader mode.

   Re-seat the cable and flash once more. If the same partitions fail again,
   set the board aside: its eMMC is not keeping writes.
EOF
  exit 1
else
  echo "   ✓ VERIFY PASSED: everything below 32 MiB matches; kernel/rootfs/userdata are proven by the first boot."
fi

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

To flash the next board: power-cycle it into maskrom the same way and re-run${PORT:+
(with --port $PORT if it goes on the same USB port)}.
EOF
