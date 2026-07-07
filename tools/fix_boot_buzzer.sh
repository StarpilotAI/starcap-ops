#!/usr/bin/env bash
# fix_boot_buzzer.sh — silence the boot-time buzzer BEEEEP on the fleet.
#
# The buzzer (header pin 15 = GPIO0_D4 = gpiochip0 line 28) is wired ACTIVE-HIGH:
# the pin powers up HIGH, so the buzzer screams from power-on until the recorder
# service finally claims the line low (~25 s in). This patch adds two register
# writes to the very top of the U-Boot boot script so the pad is driven LOW in
# U-Boot, before Linux even starts — cutting the beep to the ~2-3 s SPL/U-Boot
# floor. It holds no kernel/gpio descriptor, so `stereocam` still reclaims the
# line normally for the record/stop beeps.
#
# GPIO0 controller base = 0xfd8a0000 (Orange Pi 5 / RK3588S). D4 = bit 28, which
# lives in the *_H registers (bits 16-31), local bit 12:
#   DDR_H (0x000c): 0x10001000  -> D4 = output   (mask bit28 | value bit12)
#   DR_H  (0x0004): 0x10000000  -> D4 = 0 (low)  (mask bit28 | value 0)
#
# Idempotent: re-running is a no-op if already patched. Takes effect on the
# pi's NEXT reboot (does not reboot for you unless you pass --reboot).
#
# Usage:
#   ./fix_boot_buzzer.sh 10.77.0.59              # one pi by IP
#   ./fix_boot_buzzer.sh 10.77.0.59 10.77.0.72   # several
#   ./fix_boot_buzzer.sh --scan                  # every live pi on 10.77.0.0/24
#   ./fix_boot_buzzer.sh --reboot 10.77.0.59     # patch then reboot it
#
# Env overrides: PI_USER (orangepi), PI_PASS (orangepi), SUBNET (10.77.0).
set -u

PI_USER="${PI_USER:-orangepi}"
PI_PASS="${PI_PASS:-orangepi}"
SUBNET="${SUBNET:-10.77.0}"
DO_REBOOT=0
IPS=()

for a in "$@"; do
  case "$a" in
    --reboot) DO_REBOOT=1 ;;
    --scan)   SCAN=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *)        IPS+=("$a") ;;
  esac
done

command -v sshpass >/dev/null || { echo "need sshpass (sudo apt install sshpass)"; exit 1; }

if [ "${SCAN:-0}" = "1" ]; then
  echo "scanning ${SUBNET}.0/24 for live pis…"
  for i in $(seq 2 254); do ( ping -c1 -W1 "${SUBNET}.${i}" >/dev/null 2>&1 && echo "${SUBNET}.${i}" ) & done | sort -t. -k4 -n > /tmp/_buzz_scan
  wait 2>/dev/null
  mapfile -t IPS < /tmp/_buzz_scan
  echo "found: ${IPS[*]:-none}"
fi

[ "${#IPS[@]}" -gt 0 ] || { echo "no targets. pass pi IPs or --scan"; exit 1; }

SSH="sshpass -p ${PI_PASS} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"

patch_one() {
  local ip="$1"
  echo "===== ${ip} ====="
  if ! $SSH "${PI_USER}@${ip}" true 2>/dev/null; then
    echo "  ✗ unreachable — skipped"; return 1
  fi
  $SSH "${PI_USER}@${ip}" "PI_PASS='${PI_PASS}' DO_REBOOT='${DO_REBOOT}' bash -s" <<'REMOTE' 2>&1 | sed 's/^/  /'
set -u
S(){ printf '%s\n' "$PI_PASS" | sudo -S "$@" 2>/dev/null; }

# safety: only touch the expected board + a boot.cmd that has the booti anchor
if ! grep -q 'rk3588s-orangepi-5' /boot/orangepiEnv.txt 2>/dev/null; then
  echo "! not an Orange Pi 5 (rk3588s) fdtfile — refusing to patch"; exit 2
fi
if ! grep -q '^booti ${kernel_addr_r}' /boot/boot.cmd 2>/dev/null; then
  echo "! /boot/boot.cmd missing expected 'booti' line — refusing to patch"; exit 2
fi

if grep -q 'buzzer: force GPIO0_D4 low' /boot/boot.cmd; then
  echo "already patched — no change"
else
  S cp -n /boot/boot.cmd /boot/boot.cmd.pre-buzzer
  S cp -n /boot/boot.scr /boot/boot.scr.pre-buzzer
  python3 - <<'PY'
p="/boot/boot.cmd"
s=open(p).read()
block=("\n# buzzer: force GPIO0_D4 low (header pin 15, active-high) at the very start of\n"
       "# the boot script -- before kernel/dtb are even loaded -- so the buzzer is\n"
       "# silent through boot. No kernel/gpio claim; the recorder reclaims the line.\n"
       "mw.l 0xfd8a000c 0x10001000   # GPIO0 SWPORT_DDR_H: D4 (bit28) = output\n"
       "mw.l 0xfd8a0004 0x10000000   # GPIO0 SWPORT_DR_H:  D4 (bit28) = 0 (low)\n\n")
anchor='setenv earlycon "off"\n'
if anchor in s:
    s=s.replace(anchor, anchor+block, 1)
else:                                   # fallback: just before booti
    s=s.replace("booti ${kernel_addr_r}", block.strip("\n")+"\n\nbooti ${kernel_addr_r}", 1)
open("/tmp/boot.cmd.new","w").write(s)
PY
  S cp /tmp/boot.cmd.new /boot/boot.cmd
  if S mkimage -C none -A arm -T script -d /boot/boot.cmd /boot/boot.scr >/dev/null 2>&1; then
    echo "patched + recompiled boot.scr  (takes effect next reboot)"
  else
    echo "! mkimage failed — restoring backup"; S cp /boot/boot.cmd.pre-buzzer /boot/boot.cmd; exit 3
  fi
fi

VER=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null || echo '?')
NAME=$(cat ~/.config/starcap/device_name 2>/dev/null || hostname)
echo "device: ${NAME}  fw ${VER}"

if [ "${DO_REBOOT}" = "1" ]; then echo "rebooting…"; S reboot; fi
REMOTE
  local rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] && echo "  ✓ ${ip} done" || echo "  ✗ ${ip} failed (rc=$rc)"
}

for ip in "${IPS[@]}"; do patch_one "$ip"; done
echo "----- all done. Fix applies on each pi's next reboot. -----"
