#!/usr/bin/env bash
# vin_check.sh — automates the full per-board data-gathering routine for
# pi_emmc_speeds.csv: auto-detects the docked board (or takes an IP), SSHes in
# to read its eMMC identity (or confirms no usable eMMC), benchmarks read/write
# speed when applicable, reads its cpu_id, checks SD-card storage, then
# interactively asks just the two things that are actual judgment calls (USB
# Modem, Field Ready + reason) before appending a row to the CSV.
#
# Everything else is read straight off the board, same checks used by hand:
#   - root device (mmcblk1 = eMMC-booted, mmcblk0 = SD-booted / no usable eMMC)
#   - eMMC size via df (model/manufacturer were dropped — the manufacturer
#     lookup by manfid was never actually confirmed reliable, see git history)
#   - SD storage mount state (/media/sdcard)
#   - read/write speed via dd with O_DIRECT (bypasses page cache) — only run on
#     eMMC-booted boards, since that's the only case where local storage speed
#     is actually relevant (SD-booted boards' storage is just the SD card)
#   - cpu_id via orangepi-recorder's own uploader.cpu_id()
#
# Usage:
#   ./vin_check.sh <vin-name>            # auto-detects the one docked board
#   ./vin_check.sh <vin-name> <ip>       # target a specific IP directly
#
# Env overrides: SUBNET (10.77.0), SSH_USER (orangepi), SSH_PASS (orangepi)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="$SCRIPT_DIR/pi_emmc_speeds.csv"
SUBNET="${SUBNET:-10.77.0}"
SSH_USER="${SSH_USER:-orangepi}"
SSH_PASS="${SSH_PASS:-orangepi}"

VIN="${1:?usage: $0 <vin-name> [ip]}"
IP="${2:-}"

SSHOPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8)
# < /dev/null: without this, ssh eats stdin meant for this script's later
# `read -p` prompts (confirmed live — the modem/field-ready prompts got
# silently skipped otherwise, since ssh had already consumed the piped input).
SSH() { SSHPASS="$SSH_PASS" sshpass -e ssh "${SSHOPTS[@]}" "${SSH_USER}@${IP}" "$@" < /dev/null; }

if [ -z "$IP" ]; then
  echo "== scanning ${SUBNET}.0/24 for a docked board =="
  for i in $(seq 2 254); do ( ping -c1 -W1 "${SUBNET}.${i}" >/dev/null 2>&1 && echo "${SUBNET}.${i}" ) & done | sort -t. -k4 -n > /tmp/_vin_scan
  wait 2>/dev/null
  mapfile -t IPS < /tmp/_vin_scan
  case "${#IPS[@]}" in
    0) echo "no live host found on ${SUBNET}.0/24 — dock the board or pass its IP explicitly"; exit 1 ;;
    1) IP="${IPS[0]}" ;;
    *) echo "multiple live hosts found: ${IPS[*]} — pass the right IP explicitly: $0 $VIN <ip>"; exit 1 ;;
  esac
fi
echo "   using IP: $IP"

ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$IP" >/dev/null 2>&1 || true
SSH true 2>/dev/null || { echo "can't SSH into $IP as ${SSH_USER} — is it a booted orangepi-recorder board?"; exit 1; }

echo "== reading identity =="
ROOT_DEV="$(SSH "findmnt -no SOURCE /")"
CPU_ID="$(SSH "cd ~/orangepi-recorder/stereocam 2>/dev/null && python3 -c 'import uploader; print(uploader.cpu_id())'")"
echo "   root device: $ROOT_DEV"
echo "   cpu_id:      $CPU_ID"

if echo "$ROOT_DEV" | grep -q mmcblk1; then
  BOOTED_EMMC=Yes
  EMMC_SIZE_KB="$(SSH "df -k / | tail -1 | awk '{print \$2}'")"
  EMMC_SIZE="$(( (EMMC_SIZE_KB + 500000) / 1000000 ))GB"
  SD_MOUNTED="$(SSH "mountpoint -q /media/sdcard && echo Yes || echo No")"

  echo "== benchmarking read/write speed (1GB, O_DIRECT) =="
  WRITE_LINE="$(SSH "dd if=/dev/zero of=/home/orangepi/speedtest.bin bs=1M count=1024 oflag=direct" 2>&1)"
  READ_LINE="$(SSH "dd if=/home/orangepi/speedtest.bin of=/dev/null bs=1M iflag=direct" 2>&1)"
  SSH "rm -f /home/orangepi/speedtest.bin"
  WRITE_MBPS="$(echo "$WRITE_LINE" | grep -oP '[\d.]+(?= MB/s)' | tail -1)"
  READ_MBPS="$(echo "$READ_LINE" | grep -oP '[\d.]+(?= MB/s)' | tail -1)"
else
  BOOTED_EMMC=No
  EMMC_SIZE="N/A"
  WRITE_MBPS="N/A"
  READ_MBPS="N/A"
  SD_MOUNTED="N/A"
fi

echo
echo "===== $VIN summary ====="
echo "  eMMC size:       $EMMC_SIZE"
echo "  Booted eMMC:     $BOOTED_EMMC"
echo "  SD storage:      $SD_MOUNTED"
echo "  Write / Read:    ${WRITE_MBPS} / ${READ_MBPS} MB/s"
echo "  cpu_id:          $CPU_ID"
echo

read -r -p "Does this board have a USB modem? [y/N]: " HAS_MODEM
[[ "$HAS_MODEM" =~ ^[Yy] ]] && MODEM=Yes || MODEM=No

read -r -p "Is this board field ready? [y/N]: " IS_READY
if [[ "$IS_READY" =~ ^[Yy] ]]; then
  READY=Yes
  REASON=""
else
  READY=No
  read -r -p "Reason not field ready: " REASON
fi

# CSV-escape one field: quote + double-up any embedded quotes if it has a comma or quote
csv_escape() {
  local s="$1"
  if [[ "$s" == *,* || "$s" == *\"* ]]; then
    printf '"%s"' "${s//\"/\"\"}"
  else
    printf '%s' "$s"
  fi
}

ROW="$(csv_escape "$VIN"),$(csv_escape "$EMMC_SIZE"),$(csv_escape "$WRITE_MBPS"),$(csv_escape "$READ_MBPS"),$(csv_escape "$BOOTED_EMMC"),$(csv_escape "$SD_MOUNTED"),$(csv_escape "$READY"),$(csv_escape "$MODEM"),$(csv_escape "$CPU_ID"),Unassigned,$(csv_escape "$REASON")"

echo "$ROW" >> "$CSV"
echo
echo "== appended to $CSV =="
tail -1 "$CSV"
echo
python3 - "$CSV" <<'PY'
import csv, sys
with open(sys.argv[1], newline="") as f:
    rows = list(csv.reader(f))
header, data = rows[0], rows[1:]
def count_yes(col_name):
    col = header.index(col_name) if col_name in header else None
    return sum(1 for r in data if col is not None and len(r) > col and r[col].strip().lower() == "yes")
ready = count_yes("Field Ready")
emmc = count_yes("Booted from eMMC")
sd = len(data) - emmc
print(f"{len(data)} devices tracked so far · {ready} field ready · {emmc} from eMMC · {sd} from SD")
PY
echo "(csv_viewer.py, if running, will pick this up on its next 1.5s poll — no restart needed)"
