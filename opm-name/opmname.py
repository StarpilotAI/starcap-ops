"""
opm-name engine — bulk rename fleet recorders as they dock over ethernet.

Reuses opm-lan's LAN discovery when it's installed (same wired-subnet
auto-detect, same ping-sweep) so it behaves identically to opm-lan for "what's
currently docked", and falls back to its own equivalent sweep when it isn't (the
standalone/zip install). The fleet is mixed hardware — Orange Pi 5 and NanoPi
R76S recorders, different logins and different identify hardware — so every SSH
here goes through _ssh(), which works out per-board which login answers.

RENAMING GOES THROUGH THE DASHBOARD, and has to. Read the board's cpu_id over SSH,
then POST it to the dashboard's station endpoint, /api/ingest/starcap-name, which
writes the name through the same `bothWrites("machines", …)` pair the dashboard's
own rename button uses: authoritative in Postgres, mirrored into Redis. cpu_id is
the key because it survives a reflash, and because that endpoint needs nothing
else — this tool's whole job is naming a board that has just been flashed and has
never reached the internet, which the device_id-keyed route cannot do. The
recorder then self-syncs its local ~/.config/starcap/device_name from the reply to
its next heartbeat (~1s cadence), so one call renames it "on the device and on the
dashboard" in one step; nothing needs to be SSHed onto the board to set a name.

WHAT NOT TO GO BACK TO. This used to write the name itself — one `HSET
starcap:names <cpu_id>` into Upstash, which really was the whole rename before the
migration (same as the old tools/name_pi.sh). Names live in Postgres now: the
dashboard's `devices` read is flipped to SQL and `machines` writes are
SQL-authoritative, so that HSET lands in a store nothing reads. It failed
SILENTLY and in both directions — /devices showed the board unnamed, the heartbeat
reply (built from the same flipped table) never carried the name so the recorder
kept its old one, and the operator had to name it a second time by hand. Nothing
back-syncs Redis→SQL and drift-watch only compares row COUNTS, so re-naming an
already-named board was invisible even to the monitor. Any future "just write it
directly" shortcut has the same shape: if the dashboard is not the writer, the
fleet and the dashboard disagree and nobody is told.
"""
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# opm-lan lives in the starcap-dashboard repo, NOT here — this repo's copy was a
# submodule that got deleted for drifting behind the dashboard's (see CLAUDE.md:
# "Don't re-add it"). Try the sibling first anyway so a vendored/submodule layout
# still works, then the real home, then $OPM_LAN_DIR for a non-standard checkout.
# Absent entirely is fine (a laptop with no copy-station checkout) — see _discover().
_OPM_LAN_CANDIDATES = [
    Path(os.environ["OPM_LAN_DIR"]) if os.environ.get("OPM_LAN_DIR") else None,
    SCRIPT_DIR.parent / "opm-lan",
    Path.home() / "starcap-dashboard" / "opm-lan",
]
_OPM_LAN_DIR = next(
    (d for d in _OPM_LAN_CANDIDATES if d and (d / "opmlan.py").is_file()), None
)
if _OPM_LAN_DIR is not None:
    sys.path.insert(0, str(_OPM_LAN_DIR))
    import opmlan  # noqa: E402  (reuse discover() — identical LAN behavior to opm-lan)
else:
    opmlan = None   # standalone: _discover() below stands in — see its comment

# --------------------------------------------------------------------------- #
# fleet logins
# --------------------------------------------------------------------------- #
# The fleet is mixed hardware and each board ships with its own stock login:
#     Orange Pi 5 recorders   orangepi / orangepi
#     NanoPi R76S recorders   pi       / 12345
# (on both, that password is also the board's sudo password — no passwordless
# sudo anywhere.) Which one is on the other end of an IP isn't knowable until we
# talk to it, so _ssh() tries them in turn and remembers per-IP which one
# answered. A board holds its DHCP lease for the whole dock session, so that
# costs exactly one rejected auth the first time a NanoPi shows up, and nothing
# afterwards.
#
# Overrides: PI_USER + PI_PASS pin a login (tried first, the built-ins still act
# as fallback); PI_LOGINS="user:pass,user:pass" replaces the list outright.
_DEFAULT_LOGINS = [("orangepi", "orangepi"), ("pi", "12345")]


def _fleet_logins():
    raw = os.environ.get("PI_LOGINS", "").strip()
    if raw:
        pairs = [(u.strip(), p) for u, _, p in (e.partition(":") for e in raw.split(",")) if u.strip()]
        if pairs:
            return pairs
    logins = list(_DEFAULT_LOGINS)
    user, password = os.environ.get("PI_USER"), os.environ.get("PI_PASS")
    if user and password:
        logins = [(user, password)] + [l for l in logins if l != (user, password)]
    return logins


LOGINS = _fleet_logins()

_LOGIN_CACHE = {}                    # ip -> (user, password) that last authenticated
_LOGIN_LOCK = threading.Lock()       # the webui probes from a scanner thread + request threads

# Which dashboard to name boards on, and with what station password. In order: the
# environment, opm-lan's own station config (a copy station already reports footage
# and dock arrivals with exactly this url+password, so there is nothing new to
# configure there), then the dashboard checkout's .env.local, then
# ~/.config/opm-name/env — which is what the standalone installer writes, since a
# laptop that only has opm-name has neither of the first two.
#
# THE DEFAULT HOST IS starcap.dev, NOT projects.starcap.dev. The projects host is
# the read-only SQL dashboard and refuses every write (dashboard lib/hostMode.ts),
# while `/api/*` on starcap.dev is deliberately never redirected precisely because
# machines post to it. `starupload` is the station secret opm-lan ships with, which
# the dashboard's ingestAuthGuard accepts.
_DEFAULT_DASHBOARD_URL = "https://starcap.dev"
_DEFAULT_STATION_PASSWORD = "starupload"

_ENV_FILES = [
    Path.home() / "starcap-dashboard" / "dashboard" / ".env.local",
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opm-name" / "env",
]


def _env_file_value(key):
    """First non-empty `KEY=value` for `key` across _ENV_FILES, or None."""
    pat = re.compile(r'^\s*(?:export\s+)?%s=(.*)$' % re.escape(key))
    for env_file in _ENV_FILES:
        if not env_file.is_file():
            continue
        try:
            lines = env_file.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            m = pat.match(line)
            if m:
                v = m.group(1).strip().strip('"').strip("'")
                if v:
                    return v
    return None


def _load_dashboard_target():
    """(url, password) for the dashboard's station API — see the note above."""
    url = (os.environ.get("STARCAP_DASHBOARD_URL") or os.environ.get("OPM_DASHBOARD_URL") or "").strip()
    pw = (os.environ.get("STARCAP_PASSWORD") or os.environ.get("OPM_UPLOAD_PASSWORD") or "").strip()
    if opmlan is not None and not (url and pw):
        try:
            an = (opmlan.load_config() or {}).get("analysis") or {}
            url = url or str(an.get("dashboard_url") or "").strip()
            pw = pw or str(an.get("password") or "").strip()
        except Exception:                # noqa: BLE001 — an unreadable station config is not fatal
            pass
    url = url or _env_file_value("STARCAP_DASHBOARD_URL") or ""
    pw = pw or _env_file_value("STARCAP_PASSWORD") or ""
    return (url or _DEFAULT_DASHBOARD_URL).rstrip("/"), (pw or _DEFAULT_STATION_PASSWORD)


DASHBOARD_URL, DASHBOARD_PASSWORD = _load_dashboard_target()


def login_for(ip):
    """The (user, password) this IP last authenticated with, or None."""
    with _LOGIN_LOCK:
        return _LOGIN_CACHE.get(ip)


def _auth_failed(r):
    # sshpass exits 5 on a rejected password; ssh's own message lands on stderr
    return r.returncode == 5 or "Permission denied" in (r.stderr or "")


def _run_ssh(ip, user, password, script, timeout):
    try:
        return subprocess.run(
            ["sshpass", "-p", password, "ssh",
             "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
             "-o", f"ConnectTimeout={min(timeout, 8)}",
             f"{user}@{ip}", script],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # ConnectTimeout covers a dead host, but a link that stalls AFTER the
        # handshake would raise out of here — and from the scanner thread that
        # throws away the whole pass. Report it like any other failed probe.
        return subprocess.CompletedProcess(args=[], returncode=255, stdout="",
                                           stderr=f"timed out after {timeout}s")


def _ssh(ip, script, timeout=10):
    """Run `script` on the board as whichever fleet login it answers on.

    Only a rejected PASSWORD moves on to the next login — a refused or timed-out
    connection stops right there, so an IP with nothing listening on 22 costs the
    same single attempt it always did (the scan sweeps a whole /24)."""
    cached = login_for(ip)
    order = ([cached] if cached else []) + [l for l in LOGINS if l != cached]
    authed = r = None
    for user, password in order:
        r = _run_ssh(ip, user, password, script, timeout)
        if _auth_failed(r):
            continue                        # wrong login for this board — try the next
        if r.returncode == 255:
            break                           # not an auth problem (refused/host key/timeout)
        authed = (user, password)           # we got a shell; the exit code is the script's
        break
    with _LOGIN_LOCK:
        if authed:
            _LOGIN_CACHE[ip] = authed
        elif cached and r is not None and _auth_failed(r):
            _LOGIN_CACHE.pop(ip, None)      # a different board took over this IP
    return r


def _resolve_login(ip, timeout=8):
    """Establish (and cache) this board's login before building a script that has
    to embed its sudo password. Returns (user, password) or None."""
    if login_for(ip) is None:
        _ssh(ip, "true", timeout=timeout)
    return login_for(ip)


# "Which physical board is this row?" — and the answer is different hardware on
# each half of the fleet, so the BOARD decides, using the same rule app.py's
# PANEL=auto uses: an Orange Pi has the breakout panel (buzzer on pin 15 =
# gpiochip0 line 28, the BUZZER_CHIP/LINE/ACTIVE_LOW defaults — see
# fix_boot_buzzer.sh); a NanoPi R76S has no buzzer at all, just the three
# onboard status LEDs panel_sysfs.py drives (wan/sys/lan), so it flashes all
# three together instead — a pattern no normal recorder state uses.
#
# Zero orangepi-recorder code changes either way: stop the systemd service
# (which releases the GPIO line / the LEDs and gracefully finalizes any
# in-flight take — the app already handles SIGTERM that way, same as a normal
# `systemctl restart`), drive the hardware directly, restart the service. A few
# seconds of the board being briefly offline, nothing more. The LED path needs
# no sudo of its own: /sys/class/leds/*/brightness is group-writable to `video`,
# which the recorder user is in (the installer's 60-stereocam-leds.rules).
_IDENTIFY_SCRIPT = r'''
set -e
SUDO_PASS=@PW@
model=$(cat /proc/device-tree/model 2>/dev/null | tr -d "\0" | tr 'A-Z' 'a-z')
leds=1
for l in wan_led sys_led lan_led; do [ -d "/sys/class/leds/$l" ] || leds=0; done
python3 -c 'import lgpio' 2>/dev/null && lgpio=1 || lgpio=0
case "$model" in
  *"orange pi"*) mode=buzzer ;;
  *) if [ "$leds" = 1 ]; then mode=leds; elif [ "$lgpio" = 1 ]; then mode=buzzer; else mode=none; fi ;;
esac
if [ "$mode" = none ]; then
  echo "board has neither a buzzer (no lgpio) nor the onboard status LEDs" >&2
  exit 3
fi
echo "$SUDO_PASS" | sudo -S systemctl stop stereocam 2>/dev/null
if [ "$mode" = buzzer ]; then
python3 - <<'PY'
import lgpio, time
CHIP, LINE, SILENT = 0, 28, 0   # active-low=0 default -> idle level is LOW
h = lgpio.gpiochip_open(CHIP)
lgpio.gpio_claim_output(h, LINE, SILENT)
for _ in range(2):
    lgpio.tx_pwm(h, LINE, 523, 50)   # C5 tone, matches the recorder's own "start" jingle
    time.sleep(0.15)
    lgpio.tx_pwm(h, LINE, 0, 0)
    lgpio.gpio_write(h, LINE, SILENT)
    time.sleep(0.13)
lgpio.gpio_free(h, LINE)
lgpio.gpiochip_close(h)
PY
else
python3 - <<'PY'
import os, re, time

BASE = "/sys/class/leds"

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""

def write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except OSError:
        pass

# Save each LED's trigger + level first: stopping the recorder hands them back
# to whatever the kernel had them on (sys_led is a heartbeat by default), which
# would fight our blinking and stay wrong after we're done.
saved = []
for name in ("wan_led", "sys_led", "lan_led"):
    d = os.path.join(BASE, name)
    m = re.search(r"\[(.+?)\]", read(os.path.join(d, "trigger")))
    saved.append((d, m.group(1) if m else "none", read(os.path.join(d, "brightness")) or "0",
                  read(os.path.join(d, "max_brightness")) or "1"))
    write(os.path.join(d, "trigger"), "none")
try:
    for _ in range(6):                       # ~2s of all-three flashing
        for d, _t, _b, mx in saved:
            write(os.path.join(d, "brightness"), mx)
        time.sleep(0.18)
        for d, _t, _b, _mx in saved:
            write(os.path.join(d, "brightness"), 0)
        time.sleep(0.18)
finally:
    for d, trig, level, _mx in saved:
        write(os.path.join(d, "trigger"), trig)
        write(os.path.join(d, "brightness"), level)
PY
fi
echo "$SUDO_PASS" | sudo -S systemctl start stereocam 2>/dev/null
echo "MODE=$mode"
'''


def identify_buzz(ip, timeout=25):
    """Make the physical board announce itself so whoever's renaming it can match
    it to its row in the UI — a double beep on boards with a buzzer, a flash of
    all three status LEDs on the ones without (NanoPi R76S). See
    _IDENTIFY_SCRIPT's comment; no orangepi-recorder code is involved, just a
    temporary stop/signal/restart. Reports which one it did as `mode`."""
    login = _resolve_login(ip)
    if not login:
        return {"ok": False, "error": "couldn't log in with any known fleet login"}
    r = _ssh(ip, _IDENTIFY_SCRIPT.replace("@PW@", shlex.quote(login[1])), timeout=timeout)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "failed").strip()[:200]}
    mode = (re.search(r"^MODE=(\w+)$", r.stdout, re.M) or [None, "buzzer"])[1]
    return {"ok": True, "mode": mode}


# Every directory a recording can live in, as ONE list shared by the tally
# (identify_for_rename) and the delete (clear_recordings) — they MUST agree, and
# they didn't: the tally used to read only the `stereo` leaves, so a board on
# multivideo firmware reported the few stale stereo dirs it had left (starcap90:
# "5 sessions · 6.9 GB") while 101 GB of multivideo sat uncounted, and 🗑️ then
# deleted only that visible 6.9 GB and left the card 98% full. Two axes:
#   leaf     — `stereo` (pre-1.38 firmware) vs `multivideo` (app.py's MULTIVIDEO=True,
#              which sets _REC_LEAF); a board that flipped mid-life has runs in both.
#   location — the local repo dir (SD-booted boards) vs /media/sdcard (eMMC-booted
#              boards redirect recording storage to the inserted SD card).
# All four are probed unconditionally; whichever don't exist are skipped.
_REC_LEAVES = ("stereo", "multivideo")
_REC_DIRS_BUILTIN = tuple(f'{base}/{leaf}'
                          for base in ('$HOME/orangepi-recorder/recordings', '/media/sdcard')
                          for leaf in _REC_LEAVES)

# ...and rather than let that hardcoded list be the only answer, ADOPT opm-lan's.
# opm-lan already learned this lesson (its identify() probes both leaves and its
# copy_paths list both locations) — a second hand-maintained list here is exactly
# how the two drifted apart in the first place, so read its config when we have it
# and the next leaf someone adds there lands here for free.
#
# Deliberately a UNION with the built-in list, not a replacement: opm-name also
# ships as a standalone zip (make_zip.sh / install.sh) onto laptops with no
# copy-station checkout, where `opmlan` is None and the built-ins are all there is.
#
# And deliberately FILTERED: copy_paths is not a list of recording dirs, it's a list
# of things worth copying — it also carries `~/.config/starcap` (device_name/device_id).
# _REC_DIRS feeds clear_recordings(), so admitting that path would make 🗑️ delete a
# board's identity config. Only accept an entry that is actually a recording location:
# under a `/recordings/` dir, or under the SD-card mount.
_HOME_RE = re.compile(r"^/home/[^/]+/")


def _looks_like_rec_dir(p):
    return "/recordings/" in p or p.startswith("/media/sdcard/")


@functools.lru_cache(maxsize=1)
def _rec_dirs():
    """Every dir a recording can live in: the built-in floor, plus opm-lan's
    recording copy_paths. Cached — the scanner calls this per board every 4s and
    the answer only changes when opm-lan's config does (a restart picks it up).

    Remote paths are re-rooted to `$HOME/...`: copy_paths is written for one board
    kind (`/home/orangepi/...`) but the fleet is mixed, and a NanoPi's home is
    /home/pi. The scripts run ON the board, so $HOME is always its own."""
    dirs = list(_REC_DIRS_BUILTIN)
    try:
        paths = (opmlan.load_config() or {}).get("copy_paths") or [] if opmlan else []
    except Exception as e:                                  # noqa: BLE001
        print(f"[rec-dirs] couldn't read opm-lan's copy_paths ({e}) — using built-ins")
        paths = []
    for p in paths:
        p = (p or "").rstrip("/")
        if not p or not _looks_like_rec_dir(p):
            continue
        p = _HOME_RE.sub("$HOME/", p)
        if p not in dirs:
            dirs.append(p)
    return tuple(dirs)


def _rec_dirs_sh():
    """The shared dir list as a shell word-list, for interpolating into a remote
    script. Quoted so $HOME still expands but nothing else can."""
    return " ".join('"' + d + '"' for d in _rec_dirs())


_CPU_ID_SCRIPT = r'''
(cd ~/orangepi-recorder/stereocam 2>/dev/null && python3 -c "import uploader; print(uploader.cpu_id())" 2>/dev/null) && exit 0
v=$(tr -d '\0' < /proc/device-tree/serial-number 2>/dev/null || true)
if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
v=$(grep -i '^serial' /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | tr -d ' \t')
if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
echo ""
'''


def identify_for_rename(ip, timeout=25):
    """Cheap identity probe for the rename table: cpu_id (the rename key) +
    current device_name (for display), plus a recordings tally (sessions +
    bytes) summed over EVERY location clear_recordings() wipes (_REC_DIRS — both
    leaves × both locations) — so the UI can show what the 🗑️ button would
    actually delete. The du is bounded with `timeout` (same trick as opm-lan's
    identify()) so a huge recordings tree can't blow the SSH timeout and make the
    board flap offline; a size that couldn't be measured in time comes back as
    rec_bytes=None / rec_bytes_known=False, NOT as 0."""
    script = (
        f'echo "CPUID=$({_CPU_ID_SCRIPT})"; '
        f'echo "NAME=$(cat ~/.config/starcap/device_name 2>/dev/null)"; '
        f'echo "HOST=$(hostname)"; '
        f'echo "VER=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null | head -1)"; '
        # what hardware this is, and how to make it announce itself later — same
        # rule app.py's PANEL=auto uses (see _IDENTIFY_SCRIPT)
        'echo "MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d "\\0")"; '
        'l=1; for d in wan_led sys_led lan_led; do [ -d "/sys/class/leds/$d" ] || l=0; done; '
        'echo "LEDS=$l"; '
        # wifi: does this board have a radio at all, and is it usable? A NanoPi
        # R76S carries an on-board SDIO RTL8822CS (wifi+BT combo); an Orange Pi 5
        # has none unless a USB dongle is fitted. Read from sysfs, /proc and one
        # cheap nmcli property query — never a scan: `nmcli dev wifi list
        # --rescan yes` costs ~10s (measured) and this probe shares its SSH
        # timeout with the recordings du below, so a scan would make boards flap
        # offline in the table.
        #
        # An ANTENNA cannot be detected in software; only the module, and whether
        # it associates. WIFIDBM is the usable proxy — a module with no antenna
        # fitted still associates, just at a signal far worse than the distance
        # to the AP should give.
        #
        # Both phy80211 and wireless/ are tested: mac80211 drivers expose the
        # first, Realtek's vendor/staging drivers only ever guaranteed the second.
        'w=""; for n in /sys/class/net/*; do '
        'if [ -e "$n/phy80211" ] || [ -d "$n/wireless" ]; then w=$(basename "$n"); break; fi; '
        'done; '
        'echo "WIFIDEV=$w"; '
        'if [ -n "$w" ]; then '
        'echo "WIFIDRV=$(basename "$(readlink -f /sys/class/net/$w/device/driver 2>/dev/null)" 2>/dev/null)"; '
        'echo "WIFISTATE=$(cat /sys/class/net/$w/operstate 2>/dev/null)"; '
        # carrier is the association test, and it is the ONLY one that works here:
        # `iw` is NOT installed on these images (checked), so anything parsing
        # `iw dev link` silently reported every connected board as merely "ready".
        'echo "WIFICARR=$(cat /sys/class/net/$w/carrier 2>/dev/null)"; '
        # SSID from NetworkManager, which IS present. Prints "--" when down.
        'echo "WIFISSID=$(nmcli -t -f GENERAL.CONNECTION dev show "$w" 2>/dev/null '
        '| sed -n \'s/^GENERAL.CONNECTION://p\' | head -1)"; '
        # /proc/net/wireless columns are: iface status link level noise ...
        # `link` is a 0-100 quality. `level` is dBm on mac80211 but a POSITIVE
        # vendor scale on rtl88x2cs — measured 36 on a board showing 90/100 link —
        # so it is passed through raw and only read as dBm when it is negative.
        'echo "WIFIQ=$(awk -v i="$w:" \'$1==i{gsub(/[.]/,"",$3); print $3; exit}\' '
        '/proc/net/wireless 2>/dev/null)"; '
        'echo "WIFILVL=$(awk -v i="$w:" \'$1==i{gsub(/[.]/,"",$4); print $4; exit}\' '
        '/proc/net/wireless 2>/dev/null)"; '
        # rfkill is its own failure mode and looks identical to "no hardware" from
        # the netdev alone: a soft/hard block leaves the interface present and dead.
        'b=0; for r in /sys/class/rfkill/*; do '
        '[ "$(cat $r/type 2>/dev/null)" = wlan ] || continue; '
        '[ "$(cat $r/soft 2>/dev/null)" = 1 ] && b=1; '
        '[ "$(cat $r/hard 2>/dev/null)" = 1 ] && b=2; '
        'done; '
        'echo "WIFIRF=$b"; '
        'fi; '
        # modem: is a cellular modem fitted, and is it carrying traffic? Same
        # question as wifi, but almost nothing that SOUNDS right actually works
        # here. Measured on starcap30 (Orange Pi 5) with a ZTE 19d2:1405 fitted:
        #
        #   /sys/class/wwan      empty        mmcli            not installed
        #   /dev/cdc-wdm*        none         ModemManager     inactive
        #   /dev/ttyUSB*         none         qmi_wwan/option  not in /proc/modules
        #
        # ...because that dongle runs in ROUTER mode: it NATs internally and hands
        # the board a plain USB ethernet interface (cdc_ether, its own DHCP on
        # 192.168.0.x). cdc_ether is built INTO these kernels, not a module, so
        # even the /proc/modules check reads "no". Anything written against
        # ModemManager reports "no modem" on a board that has one — the same trap
        # as `iw` not being installed cost the wifi probe above.
        #
        # So: walk each netdev up to its USB parent and read the VENDOR id. The
        # USB id is the discriminator, NOT the driver — an ordinary USB-to-RJ45
        # adapter is cdc_ether too, and calling that a modem would be worse than
        # missing one. Modems in QMI/MBIM mode expose no netdev until they are
        # connected, so the wwan/cdc-wdm/ttyUSB paths are still checked as a
        # fallback; this board simply is not in that mode.
        'md=""; mdrv=""; mid=""; mven=""; mprod=""; '
        'for n in /sys/class/net/*; do '
        'i=$(basename "$n"); [ "$i" = lo ] && continue; '
        'd=$(readlink -f "$n/device" 2>/dev/null); '
        # climb to the nearest ancestor carrying idVendor — that IS the USB device
        'v=""; while [ -n "$d" ] && [ "$d" != / ]; do '
        '[ -f "$d/idVendor" ] && { v=$(cat "$d/idVendor"); break; }; d=$(dirname "$d"); done; '
        '[ -n "$v" ] || continue; '
        'case "$v" in '
        # ZTE, Quectel, Huawei, SIMCom, Qualcomm, Sierra, Fibocom, Alcatel/TCT,
        # Telit, u-blox — the vendors that actually ship the dongles/M.2 cards
        # these boards get fitted with.
        '19d2|2c7c|12d1|1e0e|05c6|1199|2cb7|1bbb|1bc7|1546) ;; '
        '*) continue ;; esac; '
        'md=$i; mid="$v:$(cat "$d/idProduct" 2>/dev/null)"; '
        'mven=$(cat "$d/manufacturer" 2>/dev/null); mprod=$(cat "$d/product" 2>/dev/null); '
        'mdrv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null); '
        'echo "MODEMCARR=$(cat "$n/carrier" 2>/dev/null)"; '
        'echo "MODEMSTATE=$(cat "$n/operstate" 2>/dev/null)"; '
        'break; done; '
        # QMI/MBIM fallback: a control node or a wwan port means a modem is present
        # even with no netdev up yet. Only consulted when the scan above found none,
        # so a router-mode dongle is never double-reported.
        'if [ -z "$md" ]; then '
        'for c in /sys/class/wwan/* /dev/cdc-wdm*; do [ -e "$c" ] || continue; '
        'md="$(basename "$c")"; mdrv="wwan"; break; done; fi; '
        'echo "MODEMDEV=$md"; echo "MODEMDRV=$mdrv"; echo "MODEMID=$mid"; '
        'echo "MODEMVEN=$mven"; echo "MODEMPROD=$mprod"; '
        # Is it actually a way OUT, or just fitted? The modem's equivalent of the
        # SSID: a default route on its interface is proof it carries traffic. It is
        # normally the BACKUP route (higher metric) while the board is docked on
        # ethernet, so presence is what matters here, not which one wins.
        '[ -n "$md" ] && echo "MODEMROUTE=$(ip route show default 2>/dev/null '
        '| grep -c " dev $md ")" || echo "MODEMROUTE=0"; '
        # ...and does it actually REACH anything? A route is not internet. A
        # router-mode dongle brings its LAN side up and hands out a DHCP lease with
        # a default route whether or not it has a SIM, credit, or any signal at all,
        # so "has route" is satisfied by a dongle that is completely dead to the
        # network. Only a packet that comes back proves otherwise.
        #
        # Every test here is BOUND TO THE MODEM DEVICE (ping -I <ifname>, curl
        # --interface = SO_BINDTODEVICE), which forces egress out that device
        # regardless of the routing table. Without that the test is worthless while
        # the board is docked: eth0 has internet and metric 100, so an unbound probe
        # succeeds through ethernet and reports a dead modem as working.
        #
        # Two hops, because they fail differently and the difference is the repair:
        #   gateway reachable, internet not -> dongle alive, no cellular service
        #                                      (SIM/credit/coverage)
        #   gateway unreachable             -> the dongle itself is not answering
        #
        # Targets are RAW IPs so DNS is never in the path — a modem with working
        # data but broken DNS must not read as offline. Budgeted tight (<=3 s each,
        # hard-capped by `timeout`) and only ever run on a board that HAS a modem,
        # so the boards that do not have one pay nothing. Empty MODEMNET means
        # "could not test", which is not the same answer as "no internet".
        'if [ -n "$md" ]; then '
        'gw=$(ip route show default dev "$md" 2>/dev/null | awk \'{print $3; exit}\'); '
        'echo "MODEMGW=$gw"; '
        # Reachability FIRST, and the gateway only if it fails. A working modem
        # then costs ~0.2 s (measured) and never pays for the gateway hop at all,
        # because reaching the far side already proves the near side.
        # ping is a SUBSTITUTE for curl, never a second opinion: running both on
        # failure cost 7.1 s (3.1 + 4.0, measured), and this budget is shared with
        # the 10 s du below inside one 25 s SSH timeout — overrun it and boards
        # flap offline in the table, which is the bug this ordering avoids.
        'n=""; '
        'if command -v curl >/dev/null 2>&1; then '
        'timeout 4 curl -s --interface "$md" --max-time 3 -o /dev/null http://1.1.1.1/ '
        '>/dev/null 2>&1 && n=1 || n=0; '
        # Only when curl is absent. TCP is likelier to be allowed than ICMP (some
        # carriers drop ICMP outright), so curl's verdict is the trusted one.
        'else timeout 4 ping -I "$md" -c 1 -W 3 1.1.1.1 >/dev/null 2>&1 && n=1 || n=0; fi; '
        'echo "MODEMNET=$n"; '
        # Only meaningful once we know there is no internet: it separates "the
        # dongle answers but has no service (SIM/credit/coverage)" from "the
        # dongle itself is dead". A local gateway replies in milliseconds or not
        # at all, so 1 s is generous.
        'g=0; '
        'if [ "$n" != "1" ] && [ -n "$gw" ]; then '
        'timeout 2 ping -I "$md" -c 1 -W 1 "$gw" >/dev/null 2>&1 && g=1; '
        'else [ "$n" = "1" ] && g=1; fi; '
        'echo "MODEMGWOK=$g"; '
        'fi; '
        # boot medium: the mmc block device the rootfs lives on tells us —
        # /sys/block/<dev>/device/type is "MMC" for eMMC, "SD" for an SD card.
        # The NanoPi image boots an OVERLAY root (lowerdir=/root over
        # upperdir=/data/root), so findmnt just says "overlay" and there's no
        # block device to ask — fall back to the kernel's own root=, then to
        # Rockchip's storagemedia= (which names the medium outright).
        'rd=$(findmnt -n -o SOURCE / 2>/dev/null); '
        'case "$rd" in /dev/*) ;; *) rd=$(tr " " "\\n" < /proc/cmdline | sed -n "s/^root=//p" | head -1) ;; esac; '
        'bd=$(basename "$rd" 2>/dev/null | sed "s/p[0-9]*$//"); '
        'bt=$(cat /sys/block/$bd/device/type 2>/dev/null); '
        '[ -n "$bt" ] || bt=$(tr " " "\\n" < /proc/cmdline | sed -n "s/^storagemedia=//p" | head -1); '
        'echo "BOOT=$bt"; '
        # recordings tally over every location clear_recordings() wipes (_REC_DIRS).
        # Collect the ones that exist into $@ first, then measure with ONE find and
        # ONE du: a per-dir `timeout 8 du` would let four dirs spend 32s and blow the
        # SSH timeout, making the board flap offline in the table.
        'set --; '
        f'for rec in {_rec_dirs_sh()}; do '
        '[ -d "$rec" ] && set -- "$@" "$rec"; '
        'done; '
        'if [ $# -eq 0 ]; then echo "RECB=0"; echo "RECN=0"; else '
        'echo "RECN=$(find "$@" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"; '
        # A multivideo session runs to ~100 GB across hundreds of files, so this du
        # CAN be killed. Report that as an empty RECB ("never measured") rather than
        # 0 — "unmeasured" and "this board is empty" must not look identical, or the
        # UI draws a full card as an empty one. du exits non-zero for other reasons
        # (a dir vanishing mid-walk), so key off timeout's own 124 and nothing else.
        #
        # printf "%.0f", NOT print: mawk (Ubuntu's default awk) renders an integral
        # value as an integer only up to INT_MAX and falls back to OFMT="%.6g" above
        # it, so `print s+0` emitted "3.02693e+10" for a 29 GB board. That is not
        # .isdigit(), so to_int() below scored it 0 while RECB stayed non-empty —
        # rec_bytes=0 with rec_bytes_known=True, i.e. the UI drew a full card as
        # "0 B" with full confidence. Every board over 2 GiB read empty, which is
        # precisely the set that has footage on it. %.0f keeps it in decimal, and a
        # double is exact to 2^53 (~9 PB), far past any card. Empty input still
        # prints 0, so "empty RECB == the du was killed" still holds.
        'duout=$(timeout 10 du -sb "$@" 2>/dev/null); '
        'if [ $? -eq 124 ]; then echo "RECB="; '
        'else echo "RECB=$(printf %s "$duout" | awk \'{s+=$1} END{printf "%.0f", s}\')"; fi; fi'
    )
    r = _ssh(ip, script, timeout=timeout)
    if r.returncode != 0 or "CPUID=" not in r.stdout:
        return {"ip": ip, "ok": False, "error": (r.stderr or "unreachable").strip()[:120]}
    grab = lambda k: (re.search(rf"^{k}=(.*)$", r.stdout, re.M) or [None, ""])[1].strip()
    cpu_id = grab("CPUID")
    if not cpu_id:
        return {"ip": ip, "ok": False, "error": "couldn't read a stable hardware id (not an RK3588 board?)"}
    to_int = lambda s: int(s) if str(s).isdigit() else 0
    boot = {"MMC": "emmc", "EMMC": "emmc", "SD": "sd", "SDMMC": "sd"}.get(grab("BOOT").upper(), "unknown")
    model = grab("MODEL")
    # how this board can be told to announce itself — the buzzer is breakout-panel
    # hardware, so a NanoPi (no buzzer at all) flashes its status LEDs instead
    identify = ("buzzer" if "orange pi" in model.lower()
                else "leds" if grab("LEDS") == "1" else "buzzer")
    # One word for "can this board do wifi", because that is what you scan a table
    # for. Ordered by what would stop you using it: no radio at all, then a radio
    # that is switched off (rfkill — present but dead, which looks identical to
    # absent if you only check for the interface), then link state.
    wifi_dev, wifi_rf = grab("WIFIDEV"), grab("WIFIRF")
    wifi_ssid = grab("WIFISSID")
    if wifi_ssid in ("--", ""):                       # nmcli prints "--" when down
        wifi_ssid = ""
    _num = lambda v: int(v) if re.fullmatch(r"-?\d+", v or "") else None
    lvl, qual = _num(grab("WIFILVL")), _num(grab("WIFIQ"))
    if not wifi_dev:
        wifi = "none"
    elif wifi_rf == "2":
        wifi = "hard-blocked"
    elif wifi_rf == "1":
        wifi = "blocked"
    elif grab("WIFICARR") == "1":
        wifi = "connected"
    elif grab("WIFISTATE") in ("up", "dormant"):
        wifi = "ready"
    else:
        wifi = "down"
    # Same shape as `wifi`: one word for "can this board get a cellular link".
    # "none" is a real answer, not a failure — most boards have no modem fitted,
    # and the table needs to show that as a fact rather than a blank.
    modem_dev = grab("MODEMDEV")
    if not modem_dev:
        modem = "none"
    elif grab("MODEMDRV") == "wwan":
        # Found only via a control node: the hardware is there, but with no netdev
        # there is nothing to ask about carrier, so do not claim it is working.
        modem = "present"
    elif grab("MODEMCARR") == "1":
        modem = "online"
    elif grab("MODEMSTATE") in ("up", "dormant"):
        modem = "ready"
    else:
        modem = "down"
    return {"ip": ip, "ok": True, "cpu_id": cpu_id, "name": grab("NAME"), "hostname": grab("HOST"),
            "version": grab("VER"), "boot": boot, "model": model, "identify": identify,
            # wifi capability: `wifi` is the verdict, the rest is for the tooltip.
            # wifi_dbm is the only clue about an ANTENNA being fitted (see the probe).
            "wifi": wifi, "wifi_dev": wifi_dev, "wifi_driver": grab("WIFIDRV"),
            "wifi_ssid": wifi_ssid,
            # dBm only when the driver actually reports dBm (negative); otherwise
            # quality is the honest number. Never present them as the same thing.
            "wifi_dbm": (lvl if lvl is not None and lvl < 0 else None),
            "wifi_quality": qual,
            # modem capability: `modem` is the verdict, the rest is the tooltip.
            # modem_route is the "is it actually a way out" signal — see the probe.
            "modem": modem, "modem_dev": modem_dev, "modem_driver": grab("MODEMDRV"),
            "modem_usb": grab("MODEMID"), "modem_vendor": grab("MODEMVEN"),
            "modem_product": grab("MODEMPROD"),
            "modem_route": grab("MODEMROUTE") not in ("", "0"),
            # Reachability, tested THROUGH the modem device (see the probe).
            # True/False/None where None means "could not test" — a modem we
            # failed to probe must not be reported as one with no internet.
            "modem_internet": {"1": True, "0": False}.get(grab("MODEMNET")),
            "modem_gw": grab("MODEMGW"),
            "modem_gw_ok": grab("MODEMGWOK") == "1",
            "user": (login_for(ip) or ("", ""))[0],
            # empty RECB = the du was killed before it finished. Pass that through as
            # None + known=False so the UI can say "size unknown" instead of "0 B" —
            # on a multivideo board the tree being too big to measure is exactly the
            # case where the number matters most.
            "rec_bytes": (to_int(grab("RECB")) if grab("RECB") else None),
            "rec_bytes_known": bool(grab("RECB")),
            "rec_runs": to_int(grab("RECN"))}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
# When opm-lan is installed we call ITS discover() so "what's currently docked"
# is decided by exactly the code opm-lan copies with. The fallback below is for
# the standalone install (the zip that ships opm-name on its own to a laptop
# with no copy-station checkout): same two steps — pick the wired interface,
# ping-sweep its /24 — deliberately kept small, because renaming only needs a
# list of live IPs, not opm-lan's inventory/copy machinery.
def _wired_iface_subnet(cfg):
    """(iface, subnet_cidr) for the cable the recorders dock on. Honors a forced
    lan.iface/scan_subnet; else prefers a NetworkManager 'Shared' 10.42.0.x link,
    then any wired interface."""
    forced_if = (cfg.get("lan") or {}).get("iface") or ""
    forced_net = (cfg.get("lan") or {}).get("scan_subnet") or ""
    if forced_if and forced_net:
        return forced_if, forced_net
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:                                  # noqa: BLE001
        return (forced_if or None), (forced_net or None)
    best = None
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        iface, ip, bits = m.group(1), m.group(2), m.group(3)
        if iface == "lo" or iface.startswith(("wl", "docker", "veth", "br-")):
            continue
        if forced_if and iface != forced_if:
            continue
        net = ".".join(ip.split(".")[:3]) + ".0/" + bits
        if ip.startswith("10.42.0."):
            return iface, (forced_net or net)
        best = best or (iface, net)
    if best:
        return best[0], (forced_net or best[1])
    return (forced_if or None), (forced_net or None)


def _discover(cfg):
    """Ping-sweep the wired subnet; return (live_ips, iface)."""
    if opmlan is not None:
        return opmlan.discover(cfg)
    iface, subnet = _wired_iface_subnet(cfg)
    if not subnet:
        return [], iface
    base = subnet.split("/")[0].rsplit(".", 1)[0]
    selfips = set(re.findall(r"\d+\.\d+\.\d+\.\d+",
                  subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True).stdout))
    procs = []
    for i in range(2, 255):
        ip = f"{base}.{i}"
        if ip in selfips:
            continue
        args = ["ping", "-c1", "-W1"] + (["-I", iface] if iface else []) + [ip]
        procs.append((ip, subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
    live, deadline = [], time.time() + 3
    for ip, p in procs:
        try:
            if p.wait(timeout=max(0, deadline - time.time())) == 0:
                live.append(ip)
        except subprocess.TimeoutExpired:
            p.kill()
    return live, iface


def discover_for_rename(cfg, timeout=25):
    """Ping-sweep (wired subnet auto-detected) + identify every live IP."""
    ips, iface = _discover(cfg)
    return [identify_for_rename(ip, timeout=timeout) for ip in ips], iface


def clear_recordings(ip, timeout=180):
    """Wipe recordings off a board — dummy field-readiness test takes, or a
    filled-up card that needs reclaiming. Empties every location in _REC_DIRS
    (both leaves × both locations — it doesn't know in advance which this board
    uses); the ones that don't apply are a harmless no-op.

    The timeout is generous because this now really does delete multivideo trees:
    unlinking a ~100 GB / 400-file session off an SD card takes far longer than
    clearing a handful of stereo test takes, and a too-short timeout would report
    a failure over a delete that was actually still running."""
    script = (
        f'for rec in {_rec_dirs_sh()}; do '
        'rm -rf "$rec"/*/ 2>/dev/null; '
        'done; '
        'true'
    )
    r = _ssh(ip, script, timeout=timeout)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "failed").strip()[:200]}
    return {"ok": True}


_NAME_API = "/api/ingest/starcap-name"


def _refusal(e, path):
    """An HTTP refusal as one line an operator can act on.

    A route the dashboard HAS answers in JSON (`{"error": …}`), and that message is the
    useful one — pass it through as written. A route it does NOT have answers with the
    Next 404 PAGE: an entire HTML document, which as an error string is useless and long
    enough to bury the only thing it actually means — that this dashboard is running a
    build from before station renaming existed. So HTML is never quoted back; the
    diagnosis is."""
    body = (e.read() or b"").decode(errors="replace").strip()
    msg = ""
    if not body.startswith("<"):
        try:
            msg = str((json.loads(body) or {}).get("error") or "").strip()
        except ValueError:
            msg = body
    if not msg:
        if e.code == 404:
            msg = (f"no {path} on this dashboard — it is running a build from before "
                   "station renaming; deploy the dashboard, then rename again")
        else:
            msg = str(e.reason or "refused, with no message")
    return f"dashboard {e.code}: {msg[:220]}"


def _dashboard_call(method, path, body=None, timeout=15):
    """One call to the dashboard's station API, as parsed JSON.

    Raises RuntimeError carrying the dashboard's OWN message on anything but a 2xx, so a
    refusal the operator can act on ("unauthorized", "read_only") reaches them verbatim
    instead of arriving as a blank failure. urllib rather than curl (which the Redis call
    used): the reply has to be parsed either way, and a bench laptop is not guaranteed to
    have curl installed."""
    data = None
    headers = {"x-starcap-password": DASHBOARD_PASSWORD}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(DASHBOARD_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = (r.read() or b"{}").decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(_refusal(e, path)) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {DASHBOARD_URL} — {e.reason}") from None
    try:
        return json.loads(raw or "{}") or {}
    except ValueError:
        raise RuntimeError(f"dashboard sent non-JSON: {raw.strip()[:120]}") from None


def load_authoritative_names():
    """Every name the dashboard currently hands out, as {cpu_id: name}.

    Straight off the table /devices renders and the heartbeat pushes down, so it — not
    board_registry.json — decides whether a name is already taken. The local registry
    only knows boards THIS laptop has scanned, which is exactly how two boards ended up
    both called starcap127.
    """
    names = (_dashboard_call("GET", _NAME_API) or {}).get("names") or {}
    out = {}
    for cpu_id, entry in names.items():
        nm = entry.get("name") if isinstance(entry, dict) else entry
        if nm and str(nm).strip():
            out[cpu_id] = str(nm).strip()
    return out


def name_survey(prefix="starcap"):
    """How far the `prefix` series has been handed out, so naming a new board is not
    a guess. Reads the SAME authoritative table check_rename() does.

    highest  biggest N currently assigned          next  highest + 1, the safe default
    gaps     numbers BELOW highest nobody holds — free to reuse, not an error
    strays   assigned names outside the series (WALEEDTEST1, unknown, ...), so a typo
             that lands outside the numbering is visible instead of silently ignored
    """
    names = load_authoritative_names()
    pat = re.compile(r"^%s(\d+)$" % re.escape(prefix), re.I)
    nums, strays = set(), []
    for v in names.values():
        m = pat.match(v.strip())
        if m:
            nums.add(int(m.group(1)))
        else:
            strays.append(v.strip())
    if not nums:
        return {"count": 0, "highest": None, "next": prefix + "1", "gaps": [],
                "strays": sorted(strays), "total": len(names)}
    lo, hi = min(nums), max(nums)
    return {"count": len(nums), "highest": hi, "next": "%s%d" % (prefix, hi + 1),
            "gaps": [i for i in range(lo, hi) if i not in nums],
            "strays": sorted(strays), "total": len(names)}


def check_rename(name, cpu_id):
    """Look before writing. Returns {"taken_by": [cpu_id...], "current_name": str|None}.

    taken_by    other boards already holding this name. Empty when it is free, and
                empty when the holder IS this board (re-typing a board's own name is
                a no-op, not a collision).
    current_name what this board is called today, so the caller can confirm before
                overwriting a name someone deliberately set.
    """
    want = (name or "").strip().lower()
    names = load_authoritative_names()
    return {"taken_by": [c for c, n in names.items() if n.lower() == want and c != cpu_id],
            "current_name": names.get(cpu_id)}


def set_authoritative_name(cpu_id, name):
    """Rename this headset — on the dashboard now, and on the board itself on its next
    heartbeat (~1s), when it self-corrects its local device_name from the reply.

    Returns the dashboard's answer, which is worth reading rather than discarding:
    `assigned_name` is the name as actually STORED (it is normalised server-side),
    `previous_name` is what this board was called, and `taken_from` lists other boards
    that held this name and have just lost it. That last one is not decoration —
    `starcaps.name` is unique, so the dashboard resolves a clash by taking the name off
    the other board, and that is the one consequence the operator cannot see from the
    bench. An empty `name` clears the entry, returning the board to unnamed."""
    return _dashboard_call("POST", _NAME_API, {"cpu_id": cpu_id, "name": name})
