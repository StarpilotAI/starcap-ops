#!/usr/bin/env bash
# sdcard_probe.sh [IP|device_name]  — probe a StarCap pi's SD card health over the LAN.
# ============================================================================
# Reads the card's identity, negotiated bus speed, kernel I/O/CRC error counts,
# filesystem errors, and sequential read speed — the signals that tell a healthy
# card (like starcap24's replacement) from a failing one (periodic stalls that
# freeze the pi and look like a network flap). Saves a profile per pi so cards
# can be compared over time. Auto-detects the live pi on eno1 if no arg given.
#
# Usage:  ./sdcard_probe.sh            # probe whatever pi is on the ethernet now
#         ./sdcard_probe.sh 10.42.0.24 # probe a specific pi
# ============================================================================
IFACE=eno1
PROFILES="$HOME/Desktop/starcap-tools/sdcard_profiles"
PASS=orangepi
SSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
mkdir -p "$PROFILES"

# --- resolve target IP -------------------------------------------------------
IP="$1"
if [ -z "$IP" ]; then
  echo "No IP given — scanning $IFACE for a live pi…"
  IP=$( ( for i in $(seq 2 254); do ping -c1 -W1 -I "$IFACE" 10.42.0.$i >/dev/null 2>&1 && echo 10.42.0.$i & done; wait ) 2>/dev/null | sort -u | head -1 )
  [ -z "$IP" ] && { echo "No pi found on $IFACE. Plug one in or pass an IP."; exit 1; }
  echo "  found $IP"
fi

# --- run the probe on the pi -------------------------------------------------
RAW=$(timeout 60 $SSH orangepi@"$IP" 'bash -s' 2>/dev/null <<'EOF'
S(){ echo orangepi | sudo -S "$@" 2>/dev/null; }
name=$(cat ~/.config/starcap/device_name 2>/dev/null)
root_src=$(findmnt -no SOURCE / 2>/dev/null)
disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | head -1); [ -z "$disk" ] && disk=$(echo "$root_src" | sed 's#/dev/##; s/p[0-9]*$//')
D=/sys/block/$disk/device
sz=$(cat /sys/block/$disk/size 2>/dev/null)
echo "device_name=$name"
echo "disk=$disk"
awk "BEGIN{printf \"size_GB=%.1f\n\", $sz*512/1e9}"
for f in type name manfid oemid hwrev fwrev serial date; do [ -r "$D/$f" ] && printf "%s=%s\n" "card_$f" "$(cat $D/$f)"; done
echo "cid=$(cat $D/cid 2>/dev/null)"
echo "bus_mode=$(S dmesg | grep -iE "$disk|mmc[0-9]: new" | grep -iE "new .*card" | tail -1 | sed 's/.*mmc[0-9]: //')"
echo "mmc_io_errors=$(S dmesg | grep -iE "mmc[0-9].*(error|timeout| crc|reset|retry|failed)" | wc -l)"
echo "fs_errors=$(S dmesg | grep -iE "EXT4-fs error|remount.*read-only|Buffer I/O error|blk_update_request|I/O error.*$disk" | wc -l)"
echo "recent_errors:"
S dmesg | grep -iE "mmc[0-9].*(error|timeout| crc|reset|failed)|EXT4-fs error|read-only|Buffer I/O error" | tail -4 | sed 's/^/  /'
rb=$(echo orangepi | sudo -S sh -c "sync; echo 3 >/proc/sys/vm/drop_caches; dd if=/dev/$disk of=/dev/null bs=1M count=800 2>&1" 2>/dev/null | grep -oE "[0-9.]+ [MG]B/s" | tail -1)
echo "read_speed=${rb:-?}"
echo "device_id=$(cat ~/.config/starcap/device_id 2>/dev/null)"
echo "fw_version=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null)"
ethc=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | grep ':eth0' | cut -d: -f1 | head -1)
echo "eth_conn=$ethc"
echo "eth_method=$(nmcli -g ipv4.method connection show "$ethc" 2>/dev/null)"
echo "eth_route_metric=$(nmcli -g ipv4.route-metric connection show "$ethc" 2>/dev/null)"
echo "eth_autoconnect=$(nmcli -g connection.autoconnect connection show "$ethc" 2>/dev/null)"
echo "eth_autoconnect_prio=$(nmcli -g connection.autoconnect-priority connection show "$ethc" 2>/dev/null)"
echo "wired_profiles:"
nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show 2>/dev/null | grep "802-3-ethernet" | sed 's/^/  /'
host=$(basename "$(dirname "$(readlink -f /sys/block/$disk/device)")")
ios=$(echo orangepi | sudo -S sh -c "mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug 2>/dev/null; cat /sys/kernel/debug/$host/ios 2>/dev/null" 2>/dev/null)
echo "mmc_host=$host"
echo "signal_voltage=$(echo "$ios" | sed -n 's/.*signal voltage:[[:space:]]*//p')"
echo "timing_spec=$(echo "$ios" | sed -n 's/.*timing spec:[[:space:]]*//p')"
echo "bus_clock=$(echo "$ios" | sed -n 's/^clock:[[:space:]]*//p')"
echo "bus_width=$(echo "$ios" | sed -n 's/.*bus width:[[:space:]]*//p')"
echo "voltage_switch_fail=$(S dmesg | grep -iE "1.8V signalling|signal voltage.*fail|voltage switch.*fail|failed to switch" | wc -l)"
EOF
)
[ -z "$RAW" ] && { echo "Probe failed — pi at $IP unreachable or SSH refused."; exit 1; }

# --- interpret ---------------------------------------------------------------
get(){ echo "$RAW" | sed -n "s/^$1=//p" | head -1; }
name=$(get device_name); [ -z "$name" ] && name="unknown"
manfid=$(get card_manfid)
case "$manfid" in
  0x000003|0x03) brand="SanDisk";; 0x00001b|0x1b) brand="Samsung";;
  0x000027|0x27) brand="Phison";;  0x000028|0x28) brand="Lexar";;
  0x000074|0x74) brand="Transcend/PNY";; 0x00009f|0x9f) brand="Kingston";;
  "") brand="?";; *) brand="unknown ($manfid) — verify, counterfeit risk";;
esac
ioerr=$(get mmc_io_errors); fserr=$(get fs_errors); bus=$(get bus_mode); rd=$(get read_speed)
verdict="GOOD ✅"
[ "${ioerr:-0}" -gt 0 ] 2>/dev/null && verdict="SUSPECT ⚠️  ($ioerr mmc I/O errors)"
[ "${fserr:-0}" -gt 0 ] 2>/dev/null && verdict="SUSPECT ⚠️  ($fserr filesystem errors)"
vsf=$(get voltage_switch_fail); [ "${vsf:-0}" -gt 0 ] 2>/dev/null && verdict="SUSPECT ⚠️  ($vsf failed 1.8V voltage switches)"
case "$brand" in unknown*) [ "$verdict" = "GOOD ✅" ] && verdict="CAUTION — $brand";; esac

# --- print + save ------------------------------------------------------------
out="$PROFILES/${name}.txt"
{
  echo "# SD card profile — $name @ $IP"
  echo "brand=$brand"
  echo "$RAW"
  echo "verdict=$verdict"
} > "$out"

echo
echo "======================= SD CARD: $name ($IP) ======================="
echo "  brand      : $brand"
echo "  card       : $(get card_name)  $(get size_GB) GB   made $(get card_date)"
echo "  bus mode   : $bus"
echo "  read speed : $rd"
echo "  mmc errors : $ioerr        fs errors: $fserr"
[ "${ioerr:-0}" -gt 0 ] || [ "${fserr:-0}" -gt 0 ] && echo "$RAW" | sed -n '/recent_errors:/,/read_speed/p' | grep '^  '
echo "  signaling  : $(get signal_voltage)  timing: $(get timing_spec)  clock: $(get bus_clock)  volt-switch-fails: $(get voltage_switch_fail)"
echo "  --------------------------------------------------------"
echo "  ETHERNET (firmware-driven — should match across all pis):"
echo "    firmware : $(get fw_version)      device_id: $(get device_id)"
echo "    eth conn : $(get eth_conn)   method=$(get eth_method)  route-metric=$(get eth_route_metric)  autoconnect=$(get eth_autoconnect)/prio$(get eth_autoconnect_prio)"
echo "  --------------------------------------------------------"
echo "  VERDICT    : $verdict"
echo "  saved      : $out"
echo "======================================================================"