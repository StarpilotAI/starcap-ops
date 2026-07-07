#!/usr/bin/env bash
# fix_ugreen_uas.sh
# ================================================================
# Stops a flaky USB SSD enclosure (UGREEN / Realtek RTL9210, JMicron, etc.) from
# hanging, resetting and throwing I/O errors under UAS — the "takes 2 minutes to
# mount / won't open" problem. It adds a kernel "ignore UAS" quirk for that exact
# bridge chip, so the drive falls back to plain USB mass-storage (stable).
#
# SAFE: it only reads device info, then SHOWS you the drive and ASKS before changing
# anything. It never writes to the SSD's data. The change is a system config file
# (/etc/modprobe.d) + an initramfs rebuild — both need sudo, so it'll ask for your
# password when it applies.
#
# Usage:  ./fix_ugreen_uas.sh      (plug the SSD in first)
# ================================================================
CONF=/etc/modprobe.d/ugreen-uas.conf

# Read a block device's USB bridge id + names by walking sysfs up to the USB device.
# (This works for UAS devices too, where `udevadm` doesn't expose the numeric id.)
usbinfo() {                      # arg: sdX ; sets U_VID U_PID U_MFR U_PROD
  U_VID=""; U_PID=""; U_MFR=""; U_PROD=""
  local p; p=$(readlink -f "/sys/block/$1/device" 2>/dev/null) || return 1
  while [ -n "$p" ] && [ "$p" != "/" ]; do
    if [ -r "$p/idVendor" ] && [ -r "$p/idProduct" ]; then
      U_VID=$(cat "$p/idVendor" 2>/dev/null)
      U_PID=$(cat "$p/idProduct" 2>/dev/null)
      [ -r "$p/manufacturer" ] && U_MFR=$(cat "$p/manufacturer" 2>/dev/null)
      [ -r "$p/product" ]      && U_PROD=$(cat "$p/product" 2>/dev/null)
      return 0
    fi
    p=$(dirname "$p")
  done
  return 1
}

usb_driver() {                  # arg: sdX -> prints uas | usb-storage | ?
  udevadm info -q property -n "/dev/$1" 2>/dev/null \
    | sed -n 's/^ID_USB_DRIVER=//p' | head -1
}

echo "Scanning USB drives…"
echo
mapfile -t DISKS < <(lsblk -dno NAME,TRAN 2>/dev/null | awk '$2=="usb"{print $1}')
[ "${#DISKS[@]}" -eq 0 ] && { echo "No USB drives detected. Plug in the SSD and run again."; exit 1; }

declare -a VID PID DESC
n=0
for d in "${DISKS[@]}"; do
  size=$(lsblk -dno SIZE "/dev/$d" 2>/dev/null | tr -d ' ')
  [ "$size" = "0B" ] && continue          # skip empty card-reader slots
  usbinfo "$d"
  drv=$(usb_driver "$d")
  label=$(lsblk -rno LABEL "/dev/$d" 2>/dev/null | grep -m1 . || true)
  mnt=$(lsblk -rno MOUNTPOINT "/dev/$d" 2>/dev/null | grep -m1 . || true)
  name=$(printf '%s %s' "$U_MFR" "$U_PROD" | sed 's/^ *//;s/ *$//')
  n=$((n+1)); VID[$n]="$U_VID"; PID[$n]="$U_PID"
  hint=""
  printf '%s' "$name" | grep -qi ugreen && hint="  <-- UGREEN"
  [ "$drv" = "uas" ] && hint="$hint  (on UAS — the hang/reset cause)"
  DESC[$n]="/dev/$d | ${name:-?} | ${U_VID:-??}:${U_PID:-??} | ${size:-?} | driver=${drv:-?} | label=${label:-none} | ${mnt:-not mounted}"
  printf "  [%d] %s%s\n" "$n" "${DESC[$n]}" "$hint"
done
[ "$n" -eq 0 ] && { echo "No non-empty USB drives found."; exit 1; }

echo
read -rp "Which drive do you want to fix?  [1-$n, or q to quit] " pick
{ [ "$pick" = q ] || [ "$pick" = Q ]; } && { echo "Quit — nothing changed."; exit 0; }
case "$pick" in ''|*[!0-9]*) echo "Not a number — nothing changed."; exit 1;; esac
{ [ "$pick" -ge 1 ] && [ "$pick" -le "$n" ]; } 2>/dev/null || { echo "Out of range — nothing changed."; exit 1; }

vid="${VID[$pick]}"; pid="${PID[$pick]}"
[ -z "$vid" ] || [ -z "$pid" ] && { echo "Couldn't read this drive's USB bridge id — nothing changed."; exit 1; }

echo
echo "You selected:"
echo "    ${DESC[$pick]}"
echo
echo "This tells the kernel to STOP using UAS for bridge ${vid}:${pid} (fixes the slow"
echo "mount / reset / I/O-error problem). It will, with sudo:"
echo "   • write the quirk to $CONF"
echo "   • rebuild the initramfs so it sticks across reboots"
echo "   • it does NOT touch any data on the SSD."
echo
read -rp ">> Is THIS the SSD you want to fix, and apply now?  [y/N] " ans
case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted — nothing changed."; exit 0;; esac

QUIRK="${vid}:${pid}:u"
existing=""
[ -f "$CONF" ] && existing=$(grep -oE 'quirks=[^[:space:]]+' "$CONF" 2>/dev/null | head -1 | cut -d= -f2)
if printf ',%s,' "$existing" | grep -q ",$QUIRK,"; then
  echo "(${vid}:${pid} already in $CONF — re-applying.)"; merged="$existing"
else
  merged="${existing:+$existing,}$QUIRK"
fi

echo
echo "Applying (enter your password if asked)…"
echo "options usb-storage quirks=$merged" | sudo tee "$CONF" >/dev/null || { echo "failed to write $CONF"; exit 1; }
echo "  wrote: options usb-storage quirks=$merged"
sudo update-initramfs -u || echo "  (update-initramfs warning — config still saved)"
echo "$merged" | sudo tee /sys/module/usb_storage/parameters/quirks >/dev/null 2>&1 || true

echo
echo "To apply RIGHT NOW without rebooting: unplug the SSD, wait ~3s, plug it back in."
read -rp "Done? Press Enter to check (or Ctrl-C — it'll apply on next reboot anyway)… " _
sleep 2
newdrv=""
for d in $(lsblk -dno NAME,TRAN 2>/dev/null | awk '$2=="usb"{print $1}'); do
  usbinfo "$d"
  [ "$U_VID" = "$vid" ] && [ "$U_PID" = "$pid" ] && newdrv=$(usb_driver "$d")
done
echo
if [ "$newdrv" = "usb-storage" ]; then
  echo "DONE ✅ — ${vid}:${pid} is now on usb-storage (UAS off). Mounts fast now, no resets."
elif [ -z "$newdrv" ]; then
  echo "Didn't see it after replug — plug it back in. Fix is saved; also applies on reboot."
else
  echo "Still on '$newdrv'. Fix is saved; a full REBOOT will guarantee it."
fi
