#!/bin/bash
# Read-only fingerprint of a StarCap recorder board. Run against a GOOD board and a
# BAD one, then `diff` the two outputs — whatever differs is your suspect list.
#   ./board_fingerprint.sh 10.77.0.143 > good.txt
#   ./board_fingerprint.sh 10.77.0.99  > bad.txt
#   diff -u good.txt bad.txt
IP="${1:?usage: board_fingerprint.sh <board-ip>}"
PW="${PI_PASS:-12345}"
sh_() { sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=8 pi@"$IP" "$@" 2>/dev/null; }

echo "########## IDENTITY ##########"
curl -s -m 8 "http://$IP:8080/api/status" | python3 -c "
import sys,json;d=json.load(sys.stdin);c=d['camera'];w=d['wb_correct']
print('version        ', d['version']);print('device_id      ', d['device_id'])
print('camera_number  ', c['camera_number']);print('card           ', c['card'])
print('geometry       ', c['width'],'x',c['height'],'@',c['fps_target'],'target')
print('bitrate        ', c['bitrate'])
print('wb_mode        ', w['mode'],'/',w['kernel'],' degraded=',w['degraded'])
print('wb_cal/pin_k   ', w['cal_temp_k'],'/',w['pin_temp_k'],' chroma=',w['chroma_trim'])
print('record_gate    ', d.get('record_gate'),' autostart=',d.get('autostart'))
"
echo; echo "########## CAMERA HARDWARE ##########"
sh_ 'for d in /sys/bus/usb/devices/*/; do p=$(cat $d/product 2>/dev/null); case "$p" in *DECXIN*|*Cam*)
  echo "usb path      $(basename $d)"
  echo "idVendor      $(cat $d/idVendor)"; echo "idProduct     $(cat $d/idProduct)"
  echo "bcdDevice     $(cat $d/bcdDevice)   <-- CAMERA FIRMWARE VERSION"
  echo "serial        $(cat $d/serial 2>/dev/null)"
  echo "link speed    $(cat $d/speed) Mbps"; echo "bMaxPower     $(cat $d/bMaxPower)"
  echo "manufacturer  $(cat $d/manufacturer 2>/dev/null)";; esac; done'
echo; echo "########## V4L2 FORMATS + NEGOTIATED RATE ##########"
sh_ 'v4l2-ctl -d /dev/video0 --list-formats-ext 2>/dev/null | grep -E "\[|Size|Interval" | head -20; echo "-- get-parm --"; v4l2-ctl -d /dev/video0 --get-parm 2>/dev/null | grep -i "frames per"'
echo; echo "########## UVC CONTROLS (every value) ##########"
sh_ 'v4l2-ctl -d /dev/video0 --list-ctrls 2>/dev/null'
echo; echo "########## PLATFORM ##########"
sh_ 'echo "model         $(tr -d "\0" < /proc/device-tree/model)"
echo "kernel        $(uname -r)"; echo "compatible    $(tr -d "\0" < /proc/device-tree/compatible | tr "\0" " ")"
echo "uptime_s      $(cut -d. -f1 /proc/uptime)"
echo "-- cpufreq --"; for p in /sys/devices/system/cpu/cpufreq/policy*; do
  echo "  $(basename $p) cpus=$(cat $p/affected_cpus) gov=$(cat $p/scaling_governor) max=$(( $(cat $p/scaling_max_freq)/1000 ))MHz"; done
echo "-- devfreq (DDR/GPU/NPU) --"; for d in /sys/class/devfreq/*/; do
  echo "  $(basename $d) gov=$(cat $d/governor 2>/dev/null) cur=$(( $(cat $d/cur_freq 2>/dev/null||echo 0)/1000000 ))MHz min=$(( $(cat $d/min_freq 2>/dev/null||echo 0)/1000000 ))MHz"; done
echo "-- thermal throttle steps applied --"; for c in /sys/class/thermal/cooling_device*; do
  echo "  $(cat $c/type) cur=$(cat $c/cur_state)/$(cat $c/max_state)"; done'
echo; echo "########## RECORDER BUILD ##########"
sh_ 'cd ~/orangepi-recorder 2>/dev/null || exit
echo "VERSION       $(cat stereocam/VERSION)"
echo "-- vision prebuilts --"; ls -la stereocam/vision/*.so 2>/dev/null | awk "{print \"  \",\$9,\$5\"b\",\$6,\$7,\$8}"
echo "wbcorrect kernel mode  $(cat stereocam/vision/.wbcorrect.mode 2>/dev/null || echo "(none)")"
echo "wbcorrect build error  $(test -f stereocam/vision/.wbcorrect.err && echo PRESENT || echo none)"
echo "wbc bake mode          $(cat ~/.config/starcap/camera_cal.wbc.mode 2>/dev/null || echo "(no .wbc)")"
echo "wbc size               $(stat -c %s ~/.config/starcap/camera_cal.wbc 2>/dev/null || echo MISSING)"
echo "cal owner              $(cat ~/.config/starcap/camera_cal.camera 2>/dev/null)"
echo "-- gstreamer / mpp --"; echo "  gst $(gst-inspect-1.0 --version 2>/dev/null | head -1 | awk "{print \$NF}")"
echo "-- git fossil? --"; test -d .git && echo "  .git PRESENT (lies about version - see memory)" || echo "  no .git (correct)"'
echo; echo "########## SYSTEMD UNIT ENV ##########"
sh_ 'ls /etc/systemd/system/stereocam.service.d/ 2>/dev/null | sed "s/^/  drop-in: /"
systemctl show stereocam -p Environment | tr " " "\n" | grep -v "^Environment=$" | sed "s/^/  /"
echo "  NRestarts=$(systemctl show stereocam -p NRestarts --value)"'
echo; echo "########## STORAGE ##########"
sh_ 'echo "root  $(findmnt -no SOURCE,FSTYPE /)"; echo "recdir $(findmnt -no SOURCE,FSTYPE,OPTIONS ~/orangepi-recorder/recordings 2>/dev/null || echo "(on root)")"
df -h / | tail -1 | sed "s/^/  /"'
echo; echo "########## USB / UVC KERNEL MESSAGES ##########"
sh_ 'echo '"$PW"' | sudo -S -p "" dmesg 2>/dev/null | grep -iE "uvc|xhci|usb 2-|dwc3" | grep -viE "registered|Manufacturer|SerialNumber" | tail -12'
echo; echo "########## MEASURED CAPTURE RATE (recorder running, 45s) ##########"
python3 - "$IP" <<'PY'
import json,sys,time,urllib.request
ip=sys.argv[1]
def st(): return json.load(urllib.request.urlopen(f"http://{ip}:8080/api/status",timeout=5))
for _ in range(60):
    d=st()
    if d["camera"]["recording"] and (d["camera"]["duration"] or 0)>10: break
a=st(); t0=time.monotonic()
while time.monotonic()-t0<45: pass
b=st(); dt=time.monotonic()-t0
df=b["camera"]["total_frames"]-a["camera"]["total_frames"]
print("  TRUE capture fps  %.3f   (%d frames / %.1f s)"%(df/dt,df,dt))
print("  dropped delta     %d"%(b["camera"]["dropped"]-a["camera"]["dropped"]))
print("  imu per frame     %.3f"%((b["camera"]["imu_live"]["total_samples"]-a["camera"]["imu_live"]["total_samples"])/max(df,1)))
print("  cpu %.0f%%  load %.2f  soc %sC"%(b["system"]["cpu_percent"],b["system"]["load_avg"][0],b["system"]["soc_temp"]))
PY
