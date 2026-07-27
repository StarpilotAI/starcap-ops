#!/usr/bin/env bash
# uplink-manager — hardware-adaptive hotspot/uplink manager for the cloned OPi5 image.
#
# Designed to be flashed onto a FLEET of Orange Pi 5 boards that differ in hardware.
# Nothing is hardcoded to an interface name (USB-dongle names are MAC-derived and vary
# per board); every device is discovered by TYPE each cycle:
#     USB-bus WiFi      -> the dongle      -> preferred HOTSPOT (AP) radio
#     non-USB WiFi      -> onboard AP6275P -> WiFi-client uplink; OR the hotspot radio
#                                            on a board that has NO dongle
#     connected ethernet-> 4G modem / LAN  -> wired uplink (preferred when it has internet)
#
# RULE (both modes): when the USB modem is giving us internet -> broadcast the hotspot and
# DO NOT connect to WiFi. When it isn't -> actively scan and connect to the best known WiFi
# (like the old recorder watchdog), so the device gets back online.
#
# DUAL-radio board (dongle + onboard): the dongle holds the hotspot permanently. The onboard
#   is a WiFi-client uplink ONLY while the modem has no internet; while on the modem it is
#   disconnected (hotspot only). The dead modem's route is demoted so WiFi wins when it's up.
# SINGLE-radio board (dongle only, or onboard only): the one radio time-shares — hotspot
#   while the modem has internet, WiFi-client when it doesn't (hotspot offline during that).
#
# Runs as root. Install as a systemd service. Tunables overridable via Environment=.
set -u

HOTSPOT_CON="${HOTSPOT_CON:-hotspot}"
POLL="${POLL:-20}"                     # seconds between checks (frequent, like the old watchdog)
UP_THRESHOLD="${UP_THRESHOLD:-2}"      # consecutive modem-internet-OK cycles before "on modem"
DOWN_THRESHOLD="${DOWN_THRESHOLD:-3}"  # consecutive modem-internet-FAIL cycles before using WiFi
METRIC_GOOD="${METRIC_GOOD:-100}"      # wired route-metric when it has internet (beats wifi 600)
METRIC_BAD="${METRIC_BAD:-1000}"       # wired route-metric when it has no internet (loses to wifi)
PING_HOSTS=(1.1.1.1 8.8.8.8)

log(){ echo "[uplink] $*"; }
HOTSPOT_SSID="$(nmcli -g 802-11-wireless.ssid connection show "$HOTSPOT_CON" 2>/dev/null)"

# ---- device discovery (by type, never by name) ----
wifi_ifaces(){ local d; for d in /sys/class/net/*/phy80211; do [ -e "$d" ] && basename "$(dirname "$d")"; done; }
is_usb(){ readlink -f "/sys/class/net/$1/device" 2>/dev/null | grep -q '/usb'; }
dongle_iface(){ local w; for w in $(wifi_ifaces); do is_usb "$w" && { echo "$w"; return; }; done; }
onboard_iface(){ local w; for w in $(wifi_ifaces); do is_usb "$w" || { echo "$w"; return; }; done; }
wired_ifaces(){ nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null \
                 | awk -F: '$2=="ethernet" && $3=="connected"{print $1}'; }

# ---- connectivity probes ----
ping_via(){ local h; for h in "${PING_HOSTS[@]}"; do ping -I "$1" -c1 -W3 "$h" >/dev/null 2>&1 && return 0; done; return 1; }
ping_any(){ local h; for h in "${PING_HOSTS[@]}"; do ping -c1 -W3 "$h" >/dev/null 2>&1 && return 0; done; return 1; }
wired_has_internet(){ local i; for i in $(wired_ifaces); do ping_via "$i" && return 0; done; return 1; }

active_con_on(){ nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$1" '$2==d{print $1}'; }
visible_ssids_on(){ nmcli -t -f SSID device wifi list ifname "$1" 2>/dev/null | sed 's/\\:/:/g' | sort -u; }

# client-WiFi candidates, highest autoconnect-priority first, excluding the hotspot
# itself and any profile sharing the hotspot's SSID
candidates(){
  nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show 2>/dev/null \
    | awk -F: '$2=="802-11-wireless"{print $3":"$1}' | sort -t: -k1,1 -nr | cut -d: -f2- \
    | while read -r n; do
        [ "$n" = "$HOTSPOT_CON" ] && continue
        [ "$(nmcli -g 802-11-wireless.ssid connection show "$n" 2>/dev/null)" = "$HOTSPOT_SSID" ] && continue
        echo "$n"
      done
}

ensure_hotspot_on(){ # $1 = hotspot iface
  [ -z "$1" ] && return 1
  [ "$(active_con_on "$1")" = "$HOTSPOT_CON" ] && return 0
  log "starting hotspot on $1 (SSID '$HOTSPOT_SSID')"
  nmcli connection up "$HOTSPOT_CON" ifname "$1" >/dev/null 2>&1
}

join_best_client_on(){ # $1 = iface ; 0 if connected to a client with internet
  local cur n s vis
  cur="$(active_con_on "$1")"
  [ -n "$cur" ] && [ "$cur" != "$HOTSPOT_CON" ] && ping_any && return 0
  nmcli device wifi rescan ifname "$1" >/dev/null 2>&1; sleep 4
  vis="$(visible_ssids_on "$1")"
  while read -r n; do
    [ -z "$n" ] && continue
    s="$(nmcli -g 802-11-wireless.ssid connection show "$n" 2>/dev/null)"
    grep -qxF "$s" <<<"$vis" || continue          # only profiles whose SSID is in range
    log "joining upstream WiFi '$n' on $1"
    if nmcli -w 20 connection up "$n" ifname "$1" >/dev/null 2>&1; then
      sleep 3; ping_any && { log "online via $n"; return 0; }
    fi
  done <<<"$(candidates)"
  return 1
}

disconnect_client_on(){ # $1 = iface — drop any non-hotspot (client) connection on it
  local cur; cur="$(active_con_on "$1")"
  if [ -n "$cur" ] && [ "$cur" != "$HOTSPOT_CON" ]; then
    nmcli connection down "$cur" >/dev/null 2>&1
    log "modem uplink healthy — stopped WiFi client '$cur' on $1 (hotspot only)"
  fi
}

# make each wired uplink preferred ONLY while it truly has internet, so the kernel
# fails over to WiFi when the modem is up-but-dead.
adjust_wired_pref(){
  local i con want cur
  for i in $(wired_ifaces); do
    con="$(active_con_on "$i")"; [ -z "$con" ] && continue
    if ping_via "$i"; then want="$METRIC_GOOD"; else want="$METRIC_BAD"; fi
    cur="$(nmcli -g ipv4.route-metric connection show "$con" 2>/dev/null)"
    if [ "$cur" != "$want" ]; then
      nmcli connection modify "$con" ipv4.route-metric "$want" >/dev/null 2>&1
      nmcli device reapply "$i" >/dev/null 2>&1
      log "wired $i ($con) route-metric -> $want"
    fi
  done
}

up=0; down=0
log "started (hotspot SSID='$HOTSPOT_SSID')"
last_state=""
while true; do
  D="$(dongle_iface)"; O="$(onboard_iface)"
  # the hotspot rides the USB dongle when present, else the onboard radio.
  if [ -n "$D" ]; then HOTSPOT_DEV="$D"; CLIENT_DEV="$O"; else HOTSPOT_DEV="$O"; CLIENT_DEV=""; fi
  # dual mode needs TWO radios (one for the AP, one for the client uplink); else single.
  if [ -n "$HOTSPOT_DEV" ] && [ -n "$CLIENT_DEV" ]; then MODE=dual; else MODE=single; fi
  state="$MODE|$HOTSPOT_DEV|$CLIENT_DEV"
  [ "$state" != "$last_state" ] && { log "mode=$MODE hotspot-dev='${HOTSPOT_DEV:-none}' client-dev='${CLIENT_DEV:-none}'"; last_state="$state"; }

  if [ -z "$HOTSPOT_DEV" ]; then sleep "$POLL"; continue; fi   # no WiFi radio at all

  # modem-internet health, with hysteresis (shared by both modes)
  if wired_has_internet; then up=$((up+1)); down=0; else down=$((down+1)); up=0; fi
  on_modem=0;  [ "$up"   -ge "$UP_THRESHOLD" ]   && on_modem=1     # modem internet is stable
  no_modem=0;  [ "$down" -ge "$DOWN_THRESHOLD" ] && no_modem=1     # modem internet is gone

  if [ "$MODE" = dual ]; then
    # dongle always broadcasts the hotspot; the onboard radio connects to WiFi ONLY while
    # the modem isn't giving us internet — on the modem it's disconnected (per request).
    ensure_hotspot_on "$HOTSPOT_DEV"
    adjust_wired_pref
    if [ "$no_modem" = 1 ]; then
      join_best_client_on "$CLIENT_DEV" || log "no known upstream WiFi in range on $CLIENT_DEV"
    elif [ "$on_modem" = 1 ]; then
      disconnect_client_on "$CLIENT_DEV"
    fi
  else
    # single radio: hotspot while the modem has internet, WiFi-client when it doesn't.
    if [ "$on_modem" = 1 ]; then
      [ "$(active_con_on "$HOTSPOT_DEV")" != "$HOTSPOT_CON" ] && ensure_hotspot_on "$HOTSPOT_DEV"
    elif [ "$no_modem" = 1 ]; then
      join_best_client_on "$HOTSPOT_DEV" || { log "no upstream WiFi — keeping hotspot up (local only)"; ensure_hotspot_on "$HOTSPOT_DEV"; }
    fi
  fi
  sleep "$POLL"
done
