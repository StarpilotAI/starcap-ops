#!/usr/bin/env bash
# vnc_into_pi.sh — VNC into a recorder's live desktop over ethernet.
#
# Installs x11vnc on the pi (if needed), starts it sharing the REAL desktop on
# :0 (not a fresh virtual one), and opens it in TigerVNC on this PC. Blocks until
# the TigerVNC window is closed, then cleans up everything it changed on the pi
# (kills x11vnc, un-forces the HDMI connector) so it never affects a later launch
# or the pi's normal behavior.
#
# Meant to be launched in the background (e.g. by opm-lan's "VNC" button) — one
# instance per IP; a second launch for the same IP that's still open is a no-op.
#
# Handles both states we've seen on the fleet:
#   - a real logged-in desktop session (uses the login user's own ~/.Xauthority)
#   - nobody logged in yet, just the display manager's greeter (uses the X server's
#     own auth file instead, via sudo on the pi)
#
# Login: the fleet is mixed hardware with a different stock login per board type
# (Orange Pi 5 = orangepi/orangepi, NanoPi R76S = pi/12345), so it tries each in
# turn and uses whichever authenticates. PI_USER + PI_PASS skip the probe — which
# is how opm-name calls it, since its scan already knows.
#
# Known root cause of the "solid black desktop, just a moving cursor" symptom on
# this fleet: the Mali/Panfork GPU's compositor only initializes real scanout once
# the kernel DRM subsystem sees the HDMI connector as physically connected (real
# EDID hotplug). With no monitor ever plugged in, GNOME Shell renders black.
# Confirmed by plugging in a real monitor once — the desktop then rendered fine,
# even over VNC. This script forces the HDMI connector's debugfs `force` file to
# "on" (the standard software HDMI-dummy-plug trick) before starting x11vnc, so a
# never-had-a-monitor pi gets the same fix without physical hardware — then sets
# it back to "off" on exit.
#
# Usage:  ./vnc_into_pi.sh <pi-ip>          e.g. ./vnc_into_pi.sh 10.77.0.12
set -euo pipefail

IP="${1:?usage: $0 <pi-ip>}"
FLEET_LOGINS=("orangepi:orangepi" "pi:12345")     # user:password, tried in order

LOCK_DIR="${TMPDIR:-/tmp}/vnc_into_pi"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/$IP.pid"

if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE")" 2>/dev/null; then
  echo "already open for $IP (pid $(cat "$LOCK_FILE")) — not launching a second one."
  exit 0
fi
echo $$ > "$LOCK_FILE"
cleanup_lock() { rm -f "$LOCK_FILE"; }
trap cleanup_lock EXIT

for bin in sshpass ssh xtigervncviewer; do
  command -v "$bin" >/dev/null || { echo "missing: $bin"; exit 1; }
done

ssh_pi() { SSHPASS="$SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$SSH_USER@$IP" "$@"; }

echo "== clearing any stale SSH host key for $IP (SD-card swaps change it) =="
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$IP" >/dev/null 2>&1 || true

# Which login does this board answer on? Skipped entirely when the caller
# already knows (opm-name passes PI_USER/PI_PASS from its scan).
SSH_USER="${PI_USER:-}"
SSH_PASS="${PI_PASS:-}"
if [ -z "$SSH_USER" ] || [ -z "$SSH_PASS" ]; then
  echo "== working out the login for $IP =="
  for login in "${FLEET_LOGINS[@]}"; do
    SSH_USER="${login%%:*}"; SSH_PASS="${login#*:}"
    if ssh_pi true >/dev/null 2>&1; then break; fi
    SSH_USER=""; SSH_PASS=""
  done
  [ -n "$SSH_USER" ] || { echo "couldn't log in to $IP with any known fleet login"; exit 1; }
fi
echo "   login: $SSH_USER"
VNC_PASS="$SSH_PASS"       # ≤8 chars, as classic VNC auth requires — both fleet passwords are

echo "== setting up x11vnc on $IP =="
ssh_pi "VNC_PASS='$VNC_PASS' SUDO_PASS='$SSH_PASS' bash -s" <<'REMOTE'
set -e
command -v x11vnc >/dev/null || { echo "$SUDO_PASS" | sudo -S apt-get update -qq && echo "$SUDO_PASS" | sudo -S apt-get install -y x11vnc; }

echo "forcing HDMI DRM connector to report 'connected' (software HDMI dummy plug)..."
# HDMI only, deliberately — also forcing DP-* made Xorg build a combined extended
# layout across both fake outputs (e.g. 3288x1080), which crashed x11vnc's resize
# handling (signal 11) on this fleet's RK3588 boards.
echo "$SUDO_PASS" | sudo -S bash -c '
  for f in /sys/kernel/debug/dri/*/HDMI-*/force; do
    [ -e "$f" ] || continue
    echo on > "$f" 2>/dev/null && echo "  forced: $f"
  done
  for s in /sys/class/drm/card*-HDMI-*/status; do
    [ -e "$s" ] && cat "$s" >/dev/null 2>&1
  done
' || echo "  (no forceable connectors found — not fatal, continuing)"

mkdir -p ~/.vnc
x11vnc -storepasswd "$VNC_PASS" ~/.vnc/passwd >/dev/null

# stop any previous instance so restarting is always clean
echo "$SUDO_PASS" | sudo -S pkill x11vnc >/dev/null 2>&1 || true
sleep 1

if [ -s ~/.Xauthority ]; then
  echo "using logged-in desktop session (~/.Xauthority)"
  nohup x11vnc -display :0 -auth ~/.Xauthority -forever -shared -rfbauth ~/.vnc/passwd \
    -rfbport 5900 -xcomposite -bg -o ~/x11vnc.log < /dev/null > /dev/null 2>&1 &
  disown
else
  # No ~/.Xauthority — either nobody's logged in (greeter only) or the display
  # manager keeps the cookie for them. Ask the running X server itself where its
  # auth file is instead of guessing a path: the fleet is not all LightDM
  # (/var/run/lightdm/root/:0) — the NanoPi image runs SDDM, whose cookie is a
  # random /run/sddm/xauth_XXXXXX. Reading it needs root either way.
  XAUTH=$(pgrep -af Xorg 2>/dev/null | grep -o -- "-auth [^ ]*" | head -1 | cut -d' ' -f2)
  [ -n "$XAUTH" ] || XAUTH=/var/run/lightdm/root/:0
  echo "no ~/.Xauthority — using the X server's own auth file ($XAUTH)"
  echo "$SUDO_PASS" | sudo -S bash -c \
    "nohup x11vnc -display :0 -auth '$XAUTH' -forever -shared -rfbauth /home/$USER/.vnc/passwd -rfbport 5900 -xcomposite -bg -o /home/$USER/x11vnc.log < /dev/null > /dev/null 2>&1 &"
fi
ok=0
for _ in $(seq 1 10); do
  pgrep -a x11vnc >/dev/null && { ok=1; break; }
  sleep 0.5
done
[ "$ok" = 1 ] || { echo "x11vnc failed to start — check ~/x11vnc.log on the pi"; exit 1; }
pgrep -a x11vnc
REMOTE

cleanup_pi() {
  echo "== closing VNC — cleaning up on $IP =="
  ssh_pi "SUDO_PASS='$SSH_PASS' bash -s" <<'REMOTE' 2>/dev/null || true
set -e
echo "$SUDO_PASS" | sudo -S pkill x11vnc >/dev/null 2>&1 || true
echo "$SUDO_PASS" | sudo -S bash -c '
  for f in /sys/kernel/debug/dri/*/HDMI-*/force; do
    [ -e "$f" ] || continue
    echo off > "$f" 2>/dev/null
  done
' || true
REMOTE
}
trap 'cleanup_pi; cleanup_lock' EXIT

echo "== launching TigerVNC (close its window to end the session) =="
echo "   host:     $IP:5900"
echo "   password: $VNC_PASS"
# TigerVNC has no scale-to-fit — it renders the remote screen at native pixel size,
# so a window smaller than the pi's resolution gets scrollbars instead of a shrunk
# image. -FullScreen uses the whole local display instead, so it fits without
# scrolling as long as the local screen is at least as big as the pi's (1920x1080).
# Press F8 for TigerVNC's menu (has a "Full screen" toggle) to get back out.
xtigervncviewer -FullScreen "$IP:5900" || true
echo "== TigerVNC closed =="
