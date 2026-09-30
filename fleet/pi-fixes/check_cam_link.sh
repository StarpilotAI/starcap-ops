#!/bin/bash
# Is this camera's SuperSpeed link healthy on THIS board?
# Run on any recorder. No usbutils needed.
echo "== host =="
echo "  model : $(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
echo "  kernel: $(uname -r)"
echo "== camera link =="
found=0
for x in /sys/bus/usb/devices/*/; do
  [ -f "$x/idVendor" ] || continue
  [ "$(cat $x/idVendor)" = "1bcf" ] || continue
  found=1
  echo "  $(cat $x/product 2>/dev/null)"
  echo "  bus=$(basename $x)  speed=$(cat $x/speed) Mbps  devnum=$(cat $x/devnum)"
done
[ $found = 0 ] && echo "  NOT ON THE BUS"
echo "  node  : $(ls /dev/video0 2>/dev/null || echo NONE)"
echo "== storm indicators (this boot) =="
D=$(sudo dmesg 2>/dev/null)
printf "  not-accepting-address : %s\n" "$(echo "$D" | grep -c 'not accepting address')"
printf "  Cannot enable         : %s\n" "$(echo "$D" | grep -c 'Cannot enable')"
printf "  highest devnum        : %s\n" "$(echo "$D" | grep -oE 'device number [0-9]+' | grep -oE '[0-9]+' | sort -n | tail -1)"
echo "== verdict =="
sp=$(for x in /sys/bus/usb/devices/*/; do [ -f "$x/idVendor" ] && [ "$(cat $x/idVendor)" = 1bcf ] && cat $x/speed; done)
if [ "$sp" = "5000" ] && [ -e /dev/video0 ]; then
  echo "  SUPERSPEED OK -- measuring 60fps for 20s..."
  timeout 28 gst-launch-1.0 -v v4l2src device=/dev/video0 io-mode=2 \
    ! image/jpeg,width=4000,height=1200,framerate=60/1 \
    ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false 2>&1 \
    | grep -oE 'rendered: [0-9]+, dropped: [0-9]+, current: [0-9.]+, average: [0-9.]+' | tail -2
elif [ "$sp" = "480" ]; then
  echo "  FELL BACK TO USB2 (480) -- SuperSpeed transmitter too weak for THIS host"
else
  echo "  NO LINK -- storming or absent"
fi
