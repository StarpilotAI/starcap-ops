"""
opm-name engine — bulk rename fleet recorders as they dock over ethernet.

Reuses opm-lan's LAN discovery when it's installed (same wired-subnet
auto-detect, same ping-sweep) so it behaves identically to opm-lan for "what's
currently docked", and falls back to its own equivalent sweep when it isn't (the
standalone/zip install). The fleet is mixed hardware — Orange Pi 5 and NanoPi
R76S recorders, different logins and different identify hardware — so every SSH
here goes through _ssh(), which works out per-board which login answers. Renaming
itself is the same mechanism as tools/name_pi.sh: read the board's cpu_id over
SSH, then HSET the name straight into the same Upstash Redis `starcap:names`
hash the dashboard's setName()/API route uses. That's the dashboard's single
source of truth for names (keyed by cpu_id, stable across reflashes) — the
recorder itself self-syncs its local ~/.config/starcap/device_name from there
on its next heartbeat (~1s cadence), so writing to Redis is sufficient to
rename it "on the device and on the dashboard" in one step; nothing needs to be
SSHed onto the board to set a name directly.
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

# Where the dashboard's Redis credentials come from, in order: the environment,
# the dashboard checkout's own .env.local (the copy station), then
# ~/.config/opm-name/env — which is what the standalone installer writes, since a
# laptop that only has opm-name has no dashboard checkout to read them out of.
_ENV_FILES = [
    Path.home() / "starcap-dashboard" / "dashboard" / ".env.local",
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opm-name" / "env",
]


def _load_redis_creds():
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and tok:
        return url, tok
    for env_file in _ENV_FILES:
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            m = re.match(r'^\s*(?:export\s+)?UPSTASH_REDIS_REST_URL=(.*)$', line)
            if m: url = url or m.group(1).strip().strip('"').strip("'")
            m = re.match(r'^\s*(?:export\s+)?UPSTASH_REDIS_REST_TOKEN=(.*)$', line)
            if m: tok = tok or m.group(1).strip().strip('"').strip("'")
        if url and tok:
            break
    return url, tok


REDIS_URL, REDIS_TOKEN = _load_redis_creds()


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


def _redis_call(*args):
    if not (REDIS_URL and REDIS_TOKEN):
        raise RuntimeError("missing UPSTASH_REDIS_REST_URL/TOKEN — set env vars or "
                            "create ~/starcap-dashboard/dashboard/.env.local")
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST", REDIS_URL,
         "-H", f"Authorization: Bearer {REDIS_TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(list(args))],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "curl failed")
    return r.stdout


def set_authoritative_name(cpu_id, name):
    """Same write name_pi.sh does: HSET into starcap:names, keyed by cpu_id.
    The board picks this up on its next heartbeat (~1s) and self-corrects its
    own local device_name to match — this one write is the whole rename."""
    now_ms = int(time.time() * 1000)
    entry = json.dumps({"name": name, "updated_at": now_ms})
    return _redis_call("HSET", "starcap:names", cpu_id, entry)
