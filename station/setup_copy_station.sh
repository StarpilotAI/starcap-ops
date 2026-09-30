#!/usr/bin/env bash
# setup_copy_station.sh — turn this Ubuntu machine into a StarCap copy-station.
#
# Reproduces the environment described in CLAUDE.md:
#   1. installs opm-lan's runtime deps (sshpass, rsync, ssh, ffmpeg, PyYAML,
#      opencv/numpy for contact sheets)
#   2. shares the wired NIC on 10.77.0.1/24 (NOT the recorder-hotspot default
#      10.42.0.1/24, which would collide with a docked pi's own subnet), as a
#      NetworkManager profile named `opm-lan`
#   3. raises the ARP neighbor-table thresholds so a full-subnet ping-sweep
#      doesn't evict live recorders (persisted via /etc/sysctl.d)
#   4. writes the `opm-lan` desktop launcher with THIS machine's SSD path baked in
#      as OPM_LAN_DESTINATION — so the per-machine value lives outside the repo and
#      git can never wipe it or conflict with it (config.yaml stays pristine)
#
# Needs sudo for: apt install, nmcli connection modify, sysctl/sysctl.d.
# Safe to re-run — every step is idempotent.
#
# Usage:  sudo ./setup_copy_station.sh [wired-iface] [profile-name]
#   wired-iface  optional; auto-detected if omitted (first non-wifi,
#                non-loopback interface reported by `ip -br link`).
#   profile-name optional; defaults to `opm-lan` (or $OPM_LAN_CONN_NAME). Every
#                copy station uses the same name so `nmcli connection up opm-lan`
#                is the same command on every machine.
#   $OPM_LAN_DEST  the SSD mount path to bake into the launcher. If unset, it is
#                auto-detected — but only when exactly ONE drive is mounted under
#                /media/<user>; with several it refuses to guess.
#
#   e.g.  sudo OPM_LAN_DEST=/media/sam/STARSSD8 ./setup_copy_station.sh
#
# The copiers themselves live in the separate `starcap-dashboard` repo — clone it
# too; this script looks for it next to this repo and in $HOME.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This needs root (apt install, nmcli, sysctl). Re-run:"
  echo "    sudo $0 $*"
  exit 1
fi

# Resolve the real user (for messages / file ownership), since we're running as root.
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== StarCap copy-station setup =="
echo "repo: $REPO_DIR"
echo

# ---------------------------------------------------------------------------
# 1. Packages
# ---------------------------------------------------------------------------
echo "-- [1/4] installing packages --"
apt-get update -qq
apt-get install -y \
  sshpass \
  openssh-client \
  rsync \
  ffmpeg \
  python3 \
  python3-yaml \
  python3-opencv \
  python3-numpy

echo "packages OK: sshpass, ssh, rsync, ffmpeg, python3(+yaml,opencv,numpy)"
echo

# ---------------------------------------------------------------------------
# 2. Wired NIC → shared, 10.77.0.1/24
# ---------------------------------------------------------------------------
echo "-- [2/4] wired NIC sharing (10.77.0.1/24) --"

WIRED_IFACE="${1:-}"
if [ -z "$WIRED_IFACE" ]; then
  # first interface that's ethernet-looking (not lo, not wl*/wifi), per `ip -br link`
  WIRED_IFACE="$(ip -br link show | awk '{print $1}' | grep -Ev '^(lo|wl|wwan|docker|veth|virbr|br-)' | head -n1)"
fi

if [ -z "$WIRED_IFACE" ]; then
  echo "  ⚠ couldn't auto-detect a wired interface. Skipping network setup."
  echo "    Re-run with the interface name, e.g.: sudo $0 enp2s0"
else
  echo "  wired interface: $WIRED_IFACE"

  # Name the profile `opm-lan` on every copy station, so the docs and the recovery
  # command (`nmcli connection up opm-lan`) are identical everywhere. If a profile is
  # already bound to this NIC under a distro default like "Wired connection 1", RENAME
  # it rather than adding a second profile for the same device — two profiles on one
  # NIC race over which one autoconnects.
  CONN_NAME="${2:-${OPM_LAN_CONN_NAME:-opm-lan}}"

  EXISTING="$(nmcli -t -f NAME,DEVICE connection show | awk -F: -v d="$WIRED_IFACE" '$2==d{print $1; exit}')"
  if [ -z "$EXISTING" ]; then
    # no connection bound to this device yet — fall back to any ethernet-type profile
    EXISTING="$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
  fi

  if [ -z "$EXISTING" ]; then
    echo "  no NetworkManager profile found for $WIRED_IFACE — creating '$CONN_NAME'"
    nmcli connection add type ethernet ifname "$WIRED_IFACE" con-name "$CONN_NAME"
  elif [ "$EXISTING" != "$CONN_NAME" ]; then
    echo "  renaming existing profile '$EXISTING' → '$CONN_NAME'"
    nmcli connection modify "$EXISTING" connection.id "$CONN_NAME"
  fi

  echo "  connection profile: $CONN_NAME"
  # Pin it to this NIC, so the profile can't drift onto another interface later.
  nmcli connection modify "$CONN_NAME" connection.interface-name "$WIRED_IFACE" \
                                       ipv4.method shared ipv4.addresses 10.77.0.1/24
  nmcli connection down "$CONN_NAME" >/dev/null 2>&1 || true
  nmcli connection up "$CONN_NAME" >/dev/null 2>&1 || echo "  (bring-up failed — plug in a cable/dock a recorder, then: nmcli connection up $CONN_NAME)"

  echo "  verify later with: ip -br addr show $WIRED_IFACE   (want 10.77.0.1/24)"
fi
echo

# ---------------------------------------------------------------------------
# 3. ARP cache thresholds
# ---------------------------------------------------------------------------
# opm-lan discovers recorders by ping-sweeping the whole /24 every scan. The
# default ARP table caps at gc_thresh1=128; a full sweep floods it to ~253, so the
# kernel's garbage collector EVICTS live recorders to make room — which is why pis
# flicker in and out of opm-lan ("shows up then disappears"). This raises the
# limits so a full-subnet sweep fits with headroom, applies it now, and persists
# it across reboots via /etc/sysctl.d.
echo "-- [3/4] ARP cache thresholds --"
ARP_CONF=/etc/sysctl.d/99-arp-cache.conf
cat > "$ARP_CONF" <<'EOF'
# opm-lan ping-sweeps the whole /24; the default ARP table (gc_thresh1=128)
# overflows to ~253 and the GC evicts live recorders, making pis flicker in and out.
# Enlarge it so a full-subnet sweep fits with headroom.
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 2048
net.ipv4.neigh.default.gc_thresh3 = 4096
EOF
sysctl -p "$ARP_CONF" >/dev/null
echo "  wrote $ARP_CONF and applied it"
echo

# opm-lan lives in the starcap-dashboard repo (not this one) — find it so the steps
# below use real paths instead of a guess.
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
DASH_DIR=""
for cand in "$(dirname "$REPO_DIR")/starcap-dashboard" "$REAL_HOME/starcap-dashboard"; do
  if [ -d "$cand/opm-lan" ]; then DASH_DIR="$cand"; break; fi
done

find "$(dirname "$REPO_DIR")" -path "*/.git" -prune -o -name "*.sh" -exec chmod +x {} + 2>/dev/null || true
if [ -n "$DASH_DIR" ]; then
  chmod +x "$DASH_DIR"/opm-lan/start_opm_lan.sh 2>/dev/null || true
  chown -R "$REAL_USER" "$DASH_DIR/opm-lan" 2>/dev/null || true   # we ran as root
fi

# ---------------------------------------------------------------------------
# 4. opm-lan desktop launcher, with THIS machine's SSD path
# ---------------------------------------------------------------------------
# The per-machine destination goes in the LAUNCHER as OPM_LAN_DESTINATION, never in
# config.yaml. config.yaml is tracked in git, so editing it means a permanently dirty
# tree, an aborted `git pull --ff-only` on launch, and a value that opm's "Update
# available" button can erase with `git reset --hard`. An env var in the launcher
# lives outside the repo, so git can never touch it — opmlan.py already reads
# OPM_LAN_DESTINATION ahead of config.yaml for exactly this reason.
echo "-- [4/4] opm-lan desktop launcher --"

if [ -z "$DASH_DIR" ]; then
  echo "  skipped — starcap-dashboard isn't cloned yet (see next steps below)"
else
  # Destination: an explicit OPM_LAN_DEST wins; otherwise auto-detect but ONLY if
  # exactly one drive is mounted under /media/<user>. Several means we'd be guessing,
  # and guessing wrong here sends footage to the wrong disk.
  DEST="${OPM_LAN_DEST:-}"
  MOUNTS=()
  if [ -z "$DEST" ] && [ -d "/media/$REAL_USER" ]; then
    while IFS= read -r m; do MOUNTS+=("$m"); done \
      < <(find "/media/$REAL_USER" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    if [ "${#MOUNTS[@]}" -eq 1 ]; then
      DEST="${MOUNTS[0]}"
      echo "  auto-detected the only mounted drive: $DEST"
    elif [ "${#MOUNTS[@]}" -gt 1 ]; then
      echo "  ⚠ ${#MOUNTS[@]} drives mounted — not guessing which is the target. Re-run as:"
      echo "      sudo OPM_LAN_DEST='${MOUNTS[0]}' $0"
    fi
  fi

  APPS="$REAL_HOME/.local/share/applications"
  # -H is load-bearing: plain `sudo -u <user>` keeps ROOT's $HOME, so xdg-user-dir
  # would report root's Desktop (or another user's) instead of this user's. Also
  # require the answer to live under $REAL_HOME before trusting it.
  DESKTOP_DIR="$(sudo -u "$REAL_USER" -H xdg-user-dir DESKTOP 2>/dev/null || true)"
  case "${DESKTOP_DIR:-}" in
    "$REAL_HOME"/*) : ;;                       # plausible — keep it
    *) DESKTOP_DIR="$REAL_HOME/Desktop" ;;     # empty, root's, or anything odd
  esac
  mkdir -p "$APPS" "$DESKTOP_DIR"

  ENVPREFIX=""
  [ -n "$DEST" ] && ENVPREFIX="env OPM_LAN_DESTINATION='$DEST' "

  # Terminal=true on purpose: the launcher git-pulls first and prints any ⚠ there.
  cat > "$APPS/opm-lan.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=opm-lan (Ethernet copier)
Comment=Pull recordings off docked Orange Pi recorders over ethernet
Exec=bash -c "cd '$DASH_DIR/opm-lan' && ${ENVPREFIX}./start_opm_lan.sh; echo; read -p 'press enter to close...'"
Icon=network-wired
Terminal=true
Categories=Utility;
EOF
  cp "$APPS/opm-lan.desktop" "$DESKTOP_DIR/opm-lan.desktop"
  chmod +x "$DESKTOP_DIR/opm-lan.desktop"
  # Don't let a chown hiccup abort the run (set -e) after the files are already
  # written — that would leave root-owned launchers the user can't edit.
  chown "$REAL_USER" "$APPS/opm-lan.desktop" "$DESKTOP_DIR/opm-lan.desktop" \
    || echo "  ⚠ chown failed — fix with: sudo chown $REAL_USER '$DESKTOP_DIR/opm-lan.desktop'"
  # Must run AS THE USER: root's gio metadata is a separate store, so trusting it as
  # root leaves GNOME still refusing to launch the icon.
  sudo -u "$REAL_USER" gio set "$DESKTOP_DIR/opm-lan.desktop" metadata::trusted true 2>/dev/null || true
  sudo -u "$REAL_USER" update-desktop-database "$APPS" 2>/dev/null || true

  echo "  wrote $DESKTOP_DIR/opm-lan.desktop (and the app grid entry)"
  if [ -n "$DEST" ]; then
    echo "  destination baked in : OPM_LAN_DESTINATION=$DEST"
    echo "  config.yaml is left untouched — git can never clobber this value"
  else
    echo "  ⚠ no destination set. Plug the SSD in and re-run, or edit the Exec= line:"
    echo "      env OPM_LAN_DESTINATION=/media/$REAL_USER/<DriveLabel> ./start_opm_lan.sh"
  fi
fi
echo

# ---------------------------------------------------------------------------
# Wrap up
# ---------------------------------------------------------------------------

echo "== Done =="
echo "  wired profile : $CONN_NAME  ($WIRED_IFACE, shared, 10.77.0.1/24)"
echo "                  bring it up by hand with: nmcli connection up $CONN_NAME"
echo
echo "Next steps:"
if [ -z "$DASH_DIR" ]; then
  echo "  FIRST — clone the copiers repo (opm-lan lives there, not in this one):"
  echo "       git clone https://github.com/StarpilotAI/starcap-dashboard.git $REAL_HOME/starcap-dashboard"
  echo "     then re-run this script so it can finish wiring things up."
  DASH_DIR="$REAL_HOME/starcap-dashboard"
fi
echo "  1. Edit $DASH_DIR/opm-lan/config.yaml → set 'destination:' to your SSD's mount path"
echo "     (e.g. /media/$REAL_USER/<DriveLabel>)."
echo "  2. cd $DASH_DIR/opm-lan && ./start_opm_lan.sh   → http://localhost:8770"
echo "     (that launcher git-pulls the repo first, so it always starts on current code)"
echo "  3. Dock a recorder over ethernet — it should appear in the UI."
