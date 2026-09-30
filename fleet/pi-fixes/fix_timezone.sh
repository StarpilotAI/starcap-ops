#!/usr/bin/env bash
# fix_timezone.sh — set the fleet's system timezone to Africa/Cairo.
#
# The base orangepi-build image ships with Asia/Shanghai (China Standard Time)
# as its default timezone — baked in from the vendor, never corrected for this
# fleet's actual deployment region. This makes every local-time-based
# timestamp (recording folder names, device_info.json's started_at, the event
# log) come out ~5 hours AHEAD of true local time. This is NOT an NTP-sync
# issue — NTP correctly disciplines the underlying UTC clock (confirmed
# "System clock synchronized: yes" on affected boards); it's purely the
# timezone-conversion-to-local being wrong. Confirmed present on BOTH an
# SD-booted board and an eMMC-booted board — it's fleet-wide, not eMMC-specific.
#
# Idempotent, takes effect immediately (no reboot needed for a timezone change,
# unlike the boot-buzzer fix).
#
# Usage:
#   ./fix_timezone.sh 10.77.0.59              # one pi by IP
#   ./fix_timezone.sh 10.77.0.59 10.77.0.72   # several
#   ./fix_timezone.sh --scan                  # every live pi on 10.77.0.0/24
#
# Env overrides: PI_USER (orangepi), PI_PASS (orangepi), SUBNET (10.77.0),
#                TZ_NAME (Africa/Cairo)
set -u

PI_USER="${PI_USER:-orangepi}"
PI_PASS="${PI_PASS:-orangepi}"
SUBNET="${SUBNET:-10.77.0}"
TZ_NAME="${TZ_NAME:-Africa/Cairo}"
IPS=()

for a in "$@"; do
  case "$a" in
    --scan)    SCAN=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *)         IPS+=("$a") ;;
  esac
done

command -v sshpass >/dev/null || { echo "need sshpass (sudo apt install sshpass)"; exit 1; }

if [ "${SCAN:-0}" = "1" ]; then
  echo "scanning ${SUBNET}.0/24 for live pis…"
  for i in $(seq 2 254); do ( ping -c1 -W1 "${SUBNET}.${i}" >/dev/null 2>&1 && echo "${SUBNET}.${i}" ) & done | sort -t. -k4 -n > /tmp/_tz_scan
  wait 2>/dev/null
  mapfile -t IPS < /tmp/_tz_scan
  echo "found: ${IPS[*]:-none}"
fi

[ "${#IPS[@]}" -gt 0 ] || { echo "no targets. pass pi IPs or --scan"; exit 1; }

SSH="sshpass -p ${PI_PASS} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"

fix_one() {
  local ip="$1"
  echo "===== ${ip} ====="
  if ! $SSH "${PI_USER}@${ip}" true 2>/dev/null; then
    echo "  ✗ unreachable — skipped"; return 1
  fi
  $SSH "${PI_USER}@${ip}" "PI_PASS='${PI_PASS}' TZ_NAME='${TZ_NAME}' bash -s" <<'REMOTE' 2>&1 | sed 's/^/  /'
set -u
S(){ printf '%s\n' "$PI_PASS" | sudo -S "$@" 2>/dev/null; }

CURRENT=$(cat /etc/timezone 2>/dev/null)
if [ "$CURRENT" = "$TZ_NAME" ]; then
  echo "already $TZ_NAME — no change"
else
  echo "was: $CURRENT -> setting: $TZ_NAME"
  S timedatectl set-timezone "$TZ_NAME"
fi
timedatectl 2>/dev/null | head -3

VER=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null || echo '?')
NAME=$(cat ~/.config/starcap/device_name 2>/dev/null || hostname)
echo "device: ${NAME}  fw ${VER}"
REMOTE
  local rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] && echo "  ✓ ${ip} done" || echo "  ✗ ${ip} failed (rc=$rc)"
}

for ip in "${IPS[@]}"; do fix_one "$ip"; done
echo "----- all done. Timezone change is immediate — no reboot needed. -----"
