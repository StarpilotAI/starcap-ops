#!/usr/bin/env python3
"""
dock_check — is this docked board actually current, auto-updating, and on main?

Run it whenever an Orange Pi 5 or a NanoPi R76S is docked — meaning plugged into THIS
laptop's ethernet. The dock link is auto-detected from the laptop's wired interfaces,
and boards reachable only over WiFi are ignored: they answer the UDP roll-call too (it
broadcasts out every interface), but nobody docked them, and restarting one would stop
a take on a board in someone's hands. `--any-link` opts into including them.

It answers the three questions that decide whether a board is safe to send back to the
field, and then (on confirmation) fixes them:

  1. LATEST      stereocam/VERSION on the board == stereocam/VERSION on origin/main
  2. AUTOUPDATE  UPDATE_ENABLE=1 explicitly, a token exists, GitHub is reachable
  3. ON MAIN     UPDATE_BRANCH=main explicitly, and NO .git in the tree

Why each check is not as obvious as it looks:

* `fleet_branch_check.py` prints VERSION but compares it to nothing, so a board
  frozen on 1.37.2 reads as a pass. Latest is resolved here from `origin/main`
  after a fetch — never from the local working tree, which is routinely behind
  (a stale tree is how the v1.39.3 mdns fix got lost on starcap15).

* updater.py:49 is `ENABLED = os.environ.get("UPDATE_ENABLE","1") not in (...)`,
  so UNSET means ON. "Not configured" is not "off" — but it is still drift, and
  `tools/pin_to_branch.sh` writes UPDATE_ENABLE=0 BY DEFAULT, so any board that
  went through it is frozen until someone notices.

* systemd merges stereocam.service.d/*.conf in LEXICAL order and the last
  Environment= for a variable wins. A leftover 20-no-autoupdate.conf or
  20-pin-commit.conf silently overrides a correct pin, so this tool reads the
  EFFECTIVE environment (systemctl show) and also inventories the drop-ins.

* SelfUpdater.maybe_update() returns immediately when there is no update_token,
  and never fires without a trusted link. Autoupdate "on" with a missing token or
  no route to api.github.com is a no-op that otherwise reports as healthy.

* A .git in the tree is a fossil: the updater only ever extracts tarballs, so git
  reports a version that is not running, and any `git checkout .` reverts the board.

How the fix gets the board current: it does NOT copy code. It repairs the config
and restarts stereocam, and the board updates ITSELF from GitHub — uploader.py:1110
calls `maybe_update(force=not was_online)` with was_online starting False, so the
first loop pass on the docked ethernet link forces a check. That is the same path
production uses. Hand-deploying a tree stays in `tools/pin_to_branch.sh`, and this
tool prints that command when the board has no route to GitHub.

Usage:
    ./dock_check.py                     # find ethernet-docked boards, report, offer to fix
    ./dock_check.py --check-only        # read-only
    ./dock_check.py --yes               # fix without the prompt
    ./dock_check.py --host 10.77.0.66   # skip discovery (repeatable)
    ./dock_check.py --expect 1.49.1     # trust this version instead of fetching
    ./dock_check.py --any-link          # include boards on WiFi (NOT docked)
"""
import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PEER_MAGIC = b"STARCAP_PEER_DISCOVER v1"
PEER_PORT = 18793
# The dock link is AUTO-DETECTED from this laptop's wired interfaces (see
# wired_links()); this is only the documented default the copy station is set up with,
# used in help text and as the shape --subnet expects. See CLAUDE.md → Networking.
DOCK_SUBNET = "10.77.0"
REPO_DIR = "~/orangepi-recorder"         # on the board
LOCAL_REPO = os.path.expanduser("~/orangepi-recorder")

# Mixed hardware, one login each; the login password is also the sudo password.
# Key auth is tried first for both users so a keyed board never needs sshpass.
LOGINS = (("orangepi", None), ("pi", None), ("orangepi", "orangepi"), ("pi", "12345"))
SUDO_PASS = {"orangepi": "orangepi", "pi": "12345"}

DROPIN_DIR = "/etc/systemd/system/stereocam.service.d"
# Drop-ins written by tools/pin_to_branch.sh. Each one pins a commit and/or turns
# the updater OFF, and each sorts after nothing that would undo it — so they must
# be removed, not merely overridden.
BLOCKING_DROPINS = ("20-no-autoupdate.conf", "20-pin-commit.conf", "20-branch-pin.conf")
# Same filename fleet_branch_check.py --fix writes, deliberately: two tools that
# agree on one file converge, two tools with two files layer two pins.
PIN_DROPIN = "20-pin-main.conf"

OK, WARN, BAD, DIM = "\033[32m", "\033[33m", "\033[31m", "\033[2m"
OFF, BOLD = "\033[0m", "\033[1m"

HAVE_SSHPASS = True                      # probed in main(); gates the password logins

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            # ERROR silences "Warning: Permanently added …", which otherwise has to be
            # grepped out — and `grep -v` returns 1 when it filters every line, which is
            # how a silent-but-successful remote command starts looking like a failure.
            "-o", "LogLevel=ERROR"]


# --------------------------------------------------------------------------- #
# latest version (the yardstick)
# --------------------------------------------------------------------------- #
def git(repo, *args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


def latest_version(repo, expect=None):
    """The version every board should be running -> (version, provenance, stale).

    Read from `origin/main` AFTER a fetch. The local working tree is never used:
    it is frequently behind origin/main, and a yardstick that is behind marks a
    current board as 'ahead' and an out-of-date board as fine.
    """
    if expect:
        return expect, "given with --expect", False
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None, f"{repo} is not a git clone — pass --repo or --expect", False
    rc, _, err = git(repo, "fetch", "origin", "main", "--quiet")
    stale = rc != 0
    rc2, ver, err2 = git(repo, "show", "origin/main:stereocam/VERSION")
    if rc2 != 0 or not ver:
        return None, f"could not read origin/main:stereocam/VERSION ({err2 or err})", stale
    src = "origin/main (fetched)" if not stale else \
          f"origin/main — FETCH FAILED, may be stale ({err.splitlines()[0] if err else 'no network?'})"
    return ver.strip(), src, stale


def _semver(v):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except (ValueError, AttributeError):
        return None


def is_newer(remote, local):
    """updater.py's exact rule, so this tool and the board never disagree."""
    rs, ls = _semver(remote), _semver(local)
    if rs is None or ls is None:
        return remote != local and bool(remote)
    return rs > ls


# --------------------------------------------------------------------------- #
# discovery — DOCKED means on this laptop's ethernet, nothing else
# --------------------------------------------------------------------------- #
def _is_wireless(iface):
    return (os.path.exists(f"/sys/class/net/{iface}/wireless")
            or os.path.exists(f"/sys/class/net/{iface}/phy80211"))


def _is_physical(iface):
    # docker0/virbr0/veth/tun have no backing device; a USB-ethernet dongle does.
    return os.path.exists(f"/sys/class/net/{iface}/device")


def wired_links():
    """Every UP wired interface with an IPv4 -> [(iface, ip, network)].

    This is the definition of "docked": a board on the laptop's ethernet. A board
    sitting on the office WiFi is NOT docked, but it DOES answer the UDP roll-call
    (the broadcast goes out every interface), so without this filter the tool
    reports and offers to restart boards nobody plugged in.
    """
    links = []
    try:
        r = subprocess.run(["ip", "-o", "-4", "addr", "show", "up"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return links
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface, cidr = parts[1], parts[3]
        if iface == "lo" or _is_wireless(iface) or not _is_physical(iface):
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        links.append((iface, cidr.split("/")[0], net))
    return links


def wired_status():
    """Why no dock link is up — the operstate of every wired NIC, so the answer is
    'enp2s0 is DOWN' rather than a silent empty result."""
    out = []
    try:
        for iface in sorted(os.listdir("/sys/class/net")):
            if iface == "lo" or _is_wireless(iface) or not _is_physical(iface):
                continue
            try:
                with open(f"/sys/class/net/{iface}/operstate") as f:
                    out.append((iface, f.read().strip()))
            except OSError:
                out.append((iface, "unknown"))
    except OSError:
        pass
    return out


def local_broadcasts():
    """Every interface broadcast address — a board on the dock cable never hears a
    broadcast sent only out the wifi interface."""
    outs = set()
    try:
        r = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if "brd" in parts:
                b = parts[parts.index("brd") + 1]
                if not b.startswith(("127.", "169.254.")):
                    outs.add(b)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    outs.add("255.255.255.255")
    return sorted(outs)


def roll_call(timeout=2.0, links=None):
    """The recorder's own UDP roll-call (what starcapfinder uses). Only boards whose
    recorder process is alive answer — which is why the sweep below also runs.

    `links` (from wired_links()) restricts responders to the ethernet dock. Pass
    None to accept any responder — WiFi boards included, which are not docked.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    found = {}
    try:
        for b in local_broadcasts():
            try:
                s.sendto(PEER_MAGIC, (b, PEER_PORT))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
                d = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if not d.get("device_id"):
                continue
            if links is not None and not on_wired(addr[0], links):
                continue          # answered, but over WiFi — not docked
            found[addr[0]] = {"ip": addr[0], "via": "roll-call",
                              "name": d.get("name") or d.get("hostname") or addr[0]}
    finally:
        s.close()
    return found


def on_wired(ip, links):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in net and str(a) != own for _, own, net in links)


def ping_sweep(links, workers=64, max_hosts=1024):
    """Ping every address on the wired dock link(s). Not redundant with the roll-call:
    a board that is crash-looping or has stereocam stopped CANNOT answer the roll-call,
    and those are exactly the boards that can never self-update out of that state.

    Only wired networks are swept, and only small ones — a docked board is always on a
    /24-ish link, so a wide network means we are pointed at the wrong interface."""
    targets = []
    for iface, own, net in links:
        hosts = list(net.hosts())
        if len(hosts) > max_hosts:
            print(f"{WARN}  skipping the sweep of {iface} ({net}, {len(hosts)} addresses "
                  f"— too wide for a dock link){OFF}")
            continue
        targets += [str(h) for h in hosts if str(h) != own]

    def alive(ip):
        r = subprocess.run(["ping", "-c1", "-W1", ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ip if r.returncode == 0 else None

    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        hits = [h for h in ex.map(alive, targets) if h]
    return {ip: {"ip": ip, "via": "sweep", "name": ip} for ip in hits}


# --------------------------------------------------------------------------- #
# ssh
# --------------------------------------------------------------------------- #
def ssh(host, login, cmd, timeout=30):
    user, password = login
    argv = (["sshpass", "-p", password] if password else []) + ["ssh"]
    if not password:
        argv += ["-o", "BatchMode=yes"]          # never prompt; fail fast instead
    argv += SSH_OPTS + [f"{user}@{host}", cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return 255, "", "ssh failed or timed out"


def find_login(host):
    """Which of the fleet's logins does this board answer on? Orange Pi 5 images use
    orangepi/orangepi, NanoPi R76S images use pi/12345 — assuming one is how
    fleet_branch_check.py ends up calling half the fleet UNREACHABLE."""
    for login in LOGINS:
        if login[1] and not HAVE_SSHPASS:
            continue
        rc, _, _ = ssh(host, login, "true", timeout=14)
        if rc == 0:
            return login
    return None


PROBE = r"""
echo "MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\000')"
echo "ACTIVE=$(systemctl is-active stereocam 2>/dev/null)"
echo "NRESTARTS=$(systemctl show stereocam -p NRestarts --value 2>/dev/null)"
if command -v curl >/dev/null 2>&1; then
  echo "GITHUB=$(curl -s -m 8 -o /dev/null -w '%{http_code}' https://api.github.com/ 2>/dev/null)"
else
  echo "GITHUB=nocurl"
fi
systemctl show stereocam -p Environment --value 2>/dev/null | tr ' ' '\n' \
  | sed -n 's/^UPDATE_\(BRANCH\|ENABLE\)=/EFF_UPDATE_\1=/p'
for f in /etc/systemd/system/stereocam.service.d/*.conf; do
  [ -f "$f" ] || continue
  b=$(basename "$f")
  echo "DROPIN=$b"
  grep -hE '^Environment=UPDATE_' "$f" 2>/dev/null | while IFS= read -r l; do
    echo "DROPINENV=$b|${l#Environment=}"
  done
done
echo "LASTREC=$(journalctl -u stereocam --no-pager -n 400 2>/dev/null \
  | grep -oE '\[rec\] (START|STOP)' | tail -1)"
cd ~/orangepi-recorder 2>/dev/null || { echo "NOREPO=1"; exit 0; }
echo "VERSION=$(cat stereocam/VERSION 2>/dev/null)"
if [ -s stereocam/update_token ]; then echo "TOKEN=yes"; else echo "TOKEN=no"; fi
if [ -d .git ]; then
  echo "GIT=yes"
  echo "BRANCH=$(git branch --show-current 2>/dev/null)"
  echo "DIRTY=$(git status --short 2>/dev/null | wc -l)"
else
  echo "GIT=no"
fi
echo "WBMODE=$(cat stereocam/vision/.wbcorrect.mode 2>/dev/null)"
if pkg-config --exists gstreamer-video-1.0 2>/dev/null; then
  echo "WBHDR=yes"
else
  echo "WBHDR=no"
fi
"""


def parse_kv(out):
    info, dropins, dropin_env = {}, [], []
    for line in out.splitlines():
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "DROPIN":
            dropins.append(v)
        elif k == "DROPINENV":
            name, _, env = v.partition("|")
            dropin_env.append((name, env))
        elif k:
            info[k] = v
    info["DROPINS"] = dropins
    info["DROPIN_ENV"] = dropin_env
    return info


def probe(board):
    host = board["ip"]
    login = find_login(host)
    if login is None:
        return {**board, "reachable": False,
                "error": "no ssh (tried key auth, orangepi/orangepi, pi/12345)"}
    rc, out, err = ssh(host, login, PROBE, timeout=45)
    if rc != 0:
        return {**board, "reachable": False, "login": login,
                "error": f"probe failed: {err or f'rc={rc}'}"}
    return {**board, "reachable": True, "login": login, **parse_kv(out)}


def model_short(info):
    m = (info.get("MODEL") or "").strip()
    if not m:
        return "?"
    if re.search(r"nanopi|r76s|rk3576", m, re.I):
        return "NanoPi R76S"
    if re.search(r"orange\s*pi|rk3588", m, re.I):
        return "Orange Pi 5"
    return m[:18]


# --------------------------------------------------------------------------- #
# assessment
# --------------------------------------------------------------------------- #
def eff(info, var):
    """The EFFECTIVE value systemd will hand the process, or None if unset. Reading
    the merged environment rather than a file is the whole point: a later drop-in
    silently wins over an earlier one."""
    return info.get(f"EFF_UPDATE_{var}")


def can_self_update(info):
    """Could this board pull an update if we restarted it right now?
    -> (bool, why not). Offering a restart to a board that cannot reach GitHub or
    has no token is a promise the tool cannot keep — and it would stop a take for
    nothing, then wait out the full poll before admitting it."""
    if info.get("TOKEN") == "no":
        return False, "no update_token on the board"
    if info.get("GITHUB") in ("000", "", None):
        return False, "no route to api.github.com from the board"
    if info.get("ACTIVE") not in ("active", "activating"):
        return False, f"stereocam is {info.get('ACTIVE') or 'not running'}"
    return True, ""


def env_functionally_wrong(info):
    """Is the RUNNING process's updater behaviour actually wrong — as opposed to
    merely unset? Unset means main + enabled by updater.py's defaults, which works;
    that deserves an explicit pin but not a restart that stops a recording."""
    en, br = eff(info, "ENABLE"), eff(info, "BRANCH")
    return ((en is not None and en not in ("1", "true", "yes"))
            or (br is not None and br != "main"))


def needs_restart(info, actions):
    """A restart is the update trigger and the only way a changed Environment=
    reaches the running process. Skip it when nothing about the live process is
    wrong — writing the drop-in is enough, and it applies on the next start.

    'wb' always needs one: post_update.sh installs the rebuilt .so with temp+mv
    precisely so it does NOT disturb the running recorder, which means the live
    process keeps the old inode mmapped and stays slow until it restarts."""
    return ("update" in actions or "dropin" in actions or "wb" in actions
            or env_functionally_wrong(info))


def assess(info, latest):
    """-> (label, colour, [(issue text, action key)]).

    Action keys drive the fix: 'git', 'dropin', 'env', 'update'. Issues with no
    action key are things this tool cannot repair over SSH.
    """
    if not info.get("reachable"):
        return "UNKNOWN", DIM, [(info.get("error", "unreachable"), None)]
    if info.get("NOREPO"):
        return "UNKNOWN", BAD, [(f"no {REPO_DIR} on this host — not a working recorder", None)]

    issues, blocked = [], False

    # --- 1. latest? -------------------------------------------------------- #
    ver = info.get("VERSION") or ""
    if not latest:
        # Unverifiable is not a pass. Certifying a board against a yardstick you
        # never obtained is the same mistake as certifying one you never reached.
        issues.append(("latest version unknown — cannot judge this board's version", None))
        blocked = True
    elif ver == latest:
        pass
    elif is_newer(latest, ver):
        able, why = can_self_update(info)
        issues.append((f"running {ver or '?'}, main is at {latest}",
                       "update" if able else None))
        if not able:
            issues.append((f"cannot self-update ({why}) — hand-deploy instead:\n"
                           f"                  tools/pin_to_branch.sh --host {info['ip']} "
                           f"--branch main --updates on", None))
    else:
        # Not a pass and not "implicit": the version is unexplained, and the updater
        # only ever moves forward (is_newer), so this tool cannot resolve it either.
        issues.append((f"running {ver}, AHEAD of origin/main ({latest}) — your local clone "
                       f"is probably stale, or this board is on a feature branch", None))
        blocked = True

    # --- 2. autoupdate on? ------------------------------------------------- #
    en = eff(info, "ENABLE")
    if en in ("0", "false", "no"):
        issues.append(("UPDATE_ENABLE=0 — the self-updater is OFF "
                       "(pin_to_branch.sh's default)", "env"))
        blocked = True
    elif en is None:
        issues.append(("UPDATE_ENABLE unset — updates run by updater.py's default, "
                       "not by intent", "env"))
    elif en not in ("1", "true", "yes"):
        issues.append((f"UPDATE_ENABLE={en} — unrecognised, treat as drift", "env"))

    if info.get("TOKEN") == "no":
        # maybe_update() returns before any network call without a token, so this
        # board would report "autoupdate on" and never update once.
        issues.append(("stereocam/update_token missing or empty — autoupdate can "
                       "never run", None))
        blocked = True

    gh = info.get("GITHUB")
    if gh == "nocurl":
        issues.append(("no curl on the board — GitHub reachability unverified", None))
    elif gh in ("000", "", None):
        issues.append(("cannot reach api.github.com from the board — no route to "
                       "update through this dock", None))
        blocked = True

    # --- 3. on main? ------------------------------------------------------- #
    br = eff(info, "BRANCH")
    if br is None:
        issues.append(("UPDATE_BRANCH unset — tracks main only by updater.py's "
                       "default", "env"))
    elif br != "main":
        issues.append((f"UPDATE_BRANCH={br} — pinned off main", "env"))
        blocked = True

    if info.get("GIT") == "yes":
        issues.append((f".git present on '{info.get('BRANCH') or '(detached)'}' "
                       f"({info.get('DIRTY', '?')} modified) — a fossil that misreports "
                       f"the running version; a git op here reverts the board", "git"))
        blocked = True

    bad_dropins = [d for d in info.get("DROPINS", []) if d in BLOCKING_DROPINS]
    for d in bad_dropins:
        envs = [e for n, e in info.get("DROPIN_ENV", []) if n == d]
        issues.append((f"{d} present ({', '.join(envs) or 'no UPDATE_ vars'}) — drop-ins "
                       f"merge in lexical order, so this can override the pin", "dropin"))
        blocked = True

    # --- 4. white-balance kernel matches the source it shipped with? -------- #
    # Not an update-plumbing question like 1-3, but the same "safe to send back to
    # the field?" one: a board here records at 56.4 fps and drops 6.6% of frames
    # (measured on starcap95, 2026-08-17, 220 of 3322) where a correct one holds
    # 60.10 and drops ZERO. The deficit only shows up while a take is running —
    # idle, the stale binary coasts at 60.1 because nothing competes with it — so
    # it survives every casual dashboard glance.
    #
    # Cause: post_update.sh rebuilds wbcorrect from source on every OTA, but only
    # if the board can compile. It is deliberately offline (no apt), so a unit
    # imaged before install.sh started apt-installing the gstreamer dev headers
    # falls back to stereocam/prebuilt/ forever. That prebuilt is currently STALE
    # against the tree — PR #23 landed wb_kernel.c's chunk-claiming workers and
    # never regenerated it — so those boards run the old fixed-band kernel, whose
    # every frame waits on the slow A55s.
    #
    # This is the one repair that needs the DOCK rather than an update: apt needs
    # a network and an operator, which is exactly what being docked means. And it
    # fixes the class, not the instance — a local build always beats the prebuilt,
    # so once the headers are on, the stale binary stops mattering on this board
    # whatever the repo does. (Regenerating the prebuilt is the fleet-wide fix and
    # has to happen on the floor image; build_prebuilt.sh refuses anywhere else.)
    wbmode = info.get("WBMODE") or ""
    if wbmode.endswith("-prebuilt-stale"):
        how = ("no gstreamer dev headers, so it cannot compile its own"
               if info.get("WBHDR") == "no" else
               "headers are present but post_update.sh has not rebuilt it yet")
        issues.append((f"wbcorrect is '{wbmode}' — running a white-balance kernel older "
                       f"than its own source ({how}); costs ~4 fps and ~6.6% of frames "
                       f"while recording", "wb"))
        blocked = True
    elif not wbmode and info.get("WBHDR") == "no":
        # No mode file AND no way to build one: post_update.sh either found no
        # prebuilt for this arch or the prebuilt was refused by this GStreamer.
        # Either way the recorder is on the python probe (~45 fps, not 60).
        issues.append(("no wbcorrect plugin and no dev headers to build one — white "
                       "balance is on the slow python probe (~45 fps, not 60)", "wb"))
        blocked = True

    # --- service health ---------------------------------------------------- #
    if info.get("ACTIVE") not in ("active", "activating"):
        issues.append((f"stereocam is {info.get('ACTIVE') or 'unknown'} — a stopped or "
                       f"crash-looping board cannot self-update out of it", None))
        blocked = True
    else:
        try:
            n = int(info.get("NRESTARTS") or 0)
        except ValueError:
            n = 0
        if n > 3:
            issues.append((f"NRestarts={n} — restarting; check the journal before "
                           f"trusting this board", None))

    if not issues:
        return "READY", OK, []
    if blocked or any(a == "update" for _, a in issues):
        return "NOT READY", BAD, issues
    return "IMPLICIT", WARN, issues       # works today, but by default rather than by intent


def fixable(issues):
    return {a for _, a in issues if a}


# --------------------------------------------------------------------------- #
# fixing
# --------------------------------------------------------------------------- #
def build_fix(info, actions, stamp):
    """One remote script. Everything it changes is config: a fossil .git, the
    drop-ins, and the unit environment. It never writes recorder code — the board
    pulls that itself once the config is right."""
    pw = SUDO_PASS.get(info["login"][0], "orangepi")
    s = [f"SUDO() {{ printf '%s\\n' '{pw}' | sudo -S -p '' \"$@\"; }}",
         f"cd {REPO_DIR} 2>/dev/null || exit 9"]

    if "git" in actions:
        # Backed up first, and LEFT on the board: starcap28's .git held 17 branches,
        # some possibly never pushed. Deleting that unrecoverably is not this tool's call.
        s.append(f'B=$HOME/git-metadata-backup-{stamp}.tar.gz\n'
                 f'if [ -d .git ]; then tar czf "$B" .git 2>/dev/null && rm -rf .git '
                 f'&& echo "DID=removed .git (backup left on the board: $B)"; fi')
    if "dropin" in actions:
        for d in BLOCKING_DROPINS:
            p = f"{DROPIN_DIR}/{d}"
            s.append(f'if [ -f "{p}" ]; then '
                     f'echo "DID=removed {d} — it carried: '
                     f'$(grep -hE \'^Environment=UPDATE_\' "{p}" | tr \'\\n\' \' \')"; '
                     f'SUDO rm -f "{p}"; fi')
    if "env" in actions or "dropin" in actions:
        # A drop-in, never a rewrite of stereocam.service: per-device Environment=
        # lines there (SUDO_PASS, WB_CHROMA_ONLY, AUTOSTART…) must survive untouched.
        #
        # Staged as the normal user, then copied with sudo. NOT piped into `sudo tee`:
        # SUDO() feeds the password in on its own stdin, which replaces the pipe
        # carrying the file content — tee would then write an EMPTY drop-in and every
        # command would still report success.
        s.append(
            f"T=$(mktemp)\n"
            f"printf '%s\\n' "
            f"'# Written by starcap-ops/fleet/dock-check/dock_check.py on {stamp}.' "
            f"'# updater.py defaults UPDATE_ENABLE to 1, so unset is NOT off — both are' "
            f"'# set explicitly here so the intent is visible and greppable.' "
            f"'[Service]' 'Environment=UPDATE_BRANCH=main' 'Environment=UPDATE_ENABLE=1' "
            f'> "$T"\n'
            f'SUDO mkdir -p {DROPIN_DIR} && SUDO cp "$T" {DROPIN_DIR}/{PIN_DROPIN} '
            f'&& SUDO chmod 644 {DROPIN_DIR}/{PIN_DROPIN} '
            f'&& echo "DID=wrote {PIN_DROPIN} (UPDATE_BRANCH=main, UPDATE_ENABLE=1)"\n'
            f'rm -f "$T"')
        s.append('SUDO systemctl daemon-reload && echo "DID=daemon-reload"')

    if "wb" in actions:
        # apt, deliberately — and ONLY here. post_update.sh cannot do this (offline
        # by contract) and neither can an OTA, so the dock is the only place it can
        # happen: a wired link, and an operator who chose to plug this board in.
        # Purely additive; measured on two boards, apt reported "0 upgraded, 20
        # newly installed, 0 to remove", so it cannot drag the recorder's own
        # GStreamer runtime forward underneath it.
        s.append(
            'if ! pkg-config --exists gstreamer-video-1.0 2>/dev/null; then\n'
            '  if SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y '
            'libgstreamer-plugins-base1.0-dev >/tmp/wbhdr.log 2>&1; then\n'
            '    echo "DID=installed the gstreamer dev headers"\n'
            '  else\n'
            '    echo "DID=FAILED to install dev headers ('
            '$(tail -1 /tmp/wbhdr.log)) — no apt mirror through this dock?"\n'
            '  fi\n'
            'fi')
        # post_update.sh is the only thing that should ever write this .so: it picks
        # the kernel variant for the SoC, does the temp+mv the mmapped-in recorder
        # needs, and refuses a binary this GStreamer would reject. Calling gcc by
        # hand here would be a second, drifting copy of all three decisions.
        # It works out APP_USER from the tree's owner, so nothing to pass in.
        s.append(
            'SUDO bash stereocam/post_update.sh >/tmp/wbpu.log 2>&1 || true\n'
            'M=$(cat stereocam/vision/.wbcorrect.mode 2>/dev/null)\n'
            # Report the mode we actually ended on, never "done". A board with no
            # apt route reaches here having changed nothing, and silently calling
            # that a repair is how a 6.6%-dropping board gets sent back to the field.
            'case "$M" in\n'
            '  "") echo "DID=wbcorrect STILL missing after rebuild — see /tmp/wbpu.log '
            'on the board" ;;\n'
            '  *-prebuilt-stale) echo "DID=wbcorrect STILL stale ($M) — see /tmp/wbpu.log '
            'on the board" ;;\n'
            '  *) echo "DID=rebuilt wbcorrect from source (kernel now $M)" ;;\n'
            'esac')

    if needs_restart(info, actions):
        # The restart IS the update trigger: uploader.py starts with was_online=False,
        # so its first pass on the docked ethernet link calls maybe_update(force=True).
        s.append('SUDO systemctl restart stereocam && echo "DID=restarted stereocam '
                 '(forces an immediate update check)"')
    else:
        s.append('echo "DID=no restart needed — the running process already behaves '
                 'correctly; the pin applies on its next start"')
    return "\n".join(s)


def wait_for_version(host, login, latest, seconds, quiet=False):
    """Poll VERSION while the board updates itself. Returns the last version seen."""
    deadline = time.monotonic() + seconds
    seen, drew = None, False
    try:
        while time.monotonic() < deadline:
            rc, out, _ = ssh(host, login, f"cat {REPO_DIR}/stereocam/VERSION 2>/dev/null",
                             timeout=20)
            if rc == 0 and out:
                seen = out.strip()
                if seen == latest:
                    return seen
            if not quiet:
                left = int(deadline - time.monotonic())
                print(f"\r    {DIM}at {seen or '?'}, waiting for {latest} … "
                      f"{left}s left{OFF}   ", end="", flush=True)
                drew = True
            time.sleep(5)
        return seen
    finally:
        if drew:
            print()          # close the \r progress line however we leave the loop


def update_log(host, login):
    rc, out, _ = ssh(host, login,
                     "journalctl -u stereocam --no-pager -n 300 2>/dev/null "
                     "| grep -iE '\\[update\\]|updater' | tail -12", timeout=25)
    return out if rc == 0 else ""


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def print_table(rows, latest):
    print(f"\n{BOLD}{'board':<16}{'model':<14}{'version':<20}{'state'}{OFF}")
    print("-" * 74)
    for info, label, col, issues in rows:
        ver = info.get("VERSION") or "—"
        if latest and ver not in ("—", latest):
            ver = f"{ver} → {latest}"
        elif latest and ver == latest:
            ver = f"{ver} (latest)"
        print(f"{str(info.get('name'))[:15]:<16}{model_short(info):<14}{ver[:19]:<20}"
              f"{col}{label}{OFF}")
        for text, action in issues:
            mark = "·" if action else "!"
            print(f"{'':<16}{DIM}{mark} {text}{OFF}")


def main():
    ap = argparse.ArgumentParser(
        description="Make a docked StarCap board current, auto-updating, and on main")
    ap.add_argument("--host", action="append", default=[],
                    help="explicit board IP (repeatable); skips discovery")
    ap.add_argument("--check-only", action="store_true", help="report, change nothing")
    ap.add_argument("--yes", action="store_true", help="fix without the confirmation prompt")
    ap.add_argument("--expect", help="treat this as the latest version (skips the fetch)")
    ap.add_argument("--repo", default=LOCAL_REPO, help=f"local recorder clone (default {LOCAL_REPO})")
    ap.add_argument("--timeout", type=float, default=2.0, help="roll-call seconds")
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip the ping sweep (roll-call only — misses stopped boards)")
    ap.add_argument("--subnet", default=None,
                    help="force a dock /24 (first three octets, e.g. 10.77.0) instead of "
                         "auto-detecting this laptop's wired link")
    ap.add_argument("--any-link", action="store_true",
                    help="also accept boards reachable over WiFi — they are NOT docked")
    ap.add_argument("--wait", type=int, default=240,
                    help="seconds to wait for a board to self-update (default 240)")
    a = ap.parse_args()

    global HAVE_SSHPASS
    HAVE_SSHPASS = subprocess.run(["which", "sshpass"], capture_output=True).returncode == 0
    if not HAVE_SSHPASS:
        print(f"{WARN}sshpass not installed — only key-auth boards can be checked "
              f"(apt install sshpass){OFF}")

    # ---- the yardstick ---------------------------------------------------- #
    latest, src, stale = latest_version(a.repo, a.expect)
    if latest:
        print(f"latest recorder version: {BOLD}{latest}{OFF}  {DIM}({src}){OFF}")
    else:
        print(f"{BAD}cannot determine the latest version: {src}{OFF}")
        print(f"{DIM}  version checking is disabled for this run; pass --expect X.Y.Z{OFF}")
    if stale:
        print(f"{WARN}  the fetch failed, so this yardstick may be behind — a board "
              f"could read 'latest' while main has moved on.{OFF}")

    # ---- discovery -------------------------------------------------------- #
    if a.host:
        boards = {h: {"ip": h, "via": "--host", "name": h} for h in a.host}
    else:
        links = wired_links()
        if a.subnet:                    # explicit override, e.g. a dock behind a switch
            links = [(iface, own, net) for iface, own, net in links
                     if str(net).startswith(a.subnet)] or \
                    [("(--subnet)", f"{a.subnet}.1",
                      ipaddress.ip_network(f"{a.subnet}.0/24"))]
        if not links and not a.any_link:
            # Be specific about WHY nothing is docked. An empty result that looks the
            # same whether a board is broken or the cable is out is a bad answer.
            print(f"\n{BAD}No wired dock link is up — nothing is docked over ethernet.{OFF}")
            for iface, state in wired_status():
                print(f"  {iface}: {state}")
            print(f"{DIM}  Plug the board in, or use --any-link to also check boards "
                  f"reachable over WiFi (they are not docked), or --host for an "
                  f"explicit IP.{OFF}")
            return 1
        for iface, own, net in links:
            print(f"dock link: {BOLD}{iface}{OFF} {own} on {net}")
        print(f"{DIM}roll-call on udp/{PEER_PORT}"
              f"{'' if a.any_link else ' (ethernet only)'}…{OFF}")
        boards = roll_call(a.timeout, links=None if a.any_link else links)
        if not a.no_sweep and links:
            print(f"{DIM}sweeping the dock link for boards too broken to answer…{OFF}")
            for ip, b in ping_sweep(links).items():
                boards.setdefault(ip, b)
    if not boards:
        print("\nNothing answered on the dock link. Is the board powered and its "
              "ethernet plugged into this laptop?")
        return 1

    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(probe, boards.values()))

    # A swept IP that answers no login is almost certainly not a recorder (another
    # laptop, a switch). Reporting those as UNREACHABLE buries the real boards in
    # noise. Anything from the roll-call or --host is always reported.
    def is_noise(i):
        return i.get("via") == "sweep" and not i.get("reachable")
    quiet_drops = [i for i in infos if is_noise(i)]
    infos = [i for i in infos if not is_noise(i)]
    if not infos:
        print("\nNo StarCap answered SSH. Only docked, powered boards can be checked.")
        return 1

    rows = []
    for i in sorted(infos, key=lambda x: str(x.get("name"))):
        label, col, issues = assess(i, latest)
        rows.append((i, label, col, issues))
    print_table(rows, latest)
    if quiet_drops:
        print(f"{DIM}({len(quiet_drops)} other host(s) on the subnet answered ping but no "
              f"recorder login — ignored){OFF}")

    todo = [(i, iss) for i, lab, _, iss in rows if fixable(iss)]
    unknown = [i for i, lab, _, _ in rows if lab == "UNKNOWN"]
    not_ready = [i for i, lab, _, _ in rows if lab != "READY"]

    print()
    if unknown:
        # A board we could not inspect is UNKNOWN, never a pass — counting it as one
        # is how a checker quietly certifies a board it never looked at.
        print(f"{BAD}{len(unknown)} board(s) COULD NOT BE CHECKED — not a pass.{OFF}")
    if not todo:
        # "Nothing to fix" is NOT "everything is fine": a board that is broken in a
        # way this tool cannot repair (no token, no route, crash-looping) also has no
        # fixable actions, and reporting exit 0 there would certify it.
        if not not_ready:
            print(f"{OK}All {len(rows)} board(s) are on the latest main and "
                  f"auto-updating.{OFF}")
            return 0
        print(f"{BAD}{len(not_ready)} board(s) are not ready and nothing here is fixable "
              f"over SSH — see the '!' lines above.{OFF}")
        return 1

    # ---- what will change ------------------------------------------------- #
    print(f"{BOLD}Fixable now:{OFF}")
    for i, iss in todo:
        acts = fixable(iss)
        plan = []
        if "git" in acts:
            plan.append("back up + remove .git")
        if "dropin" in acts:
            plan.append("remove the blocking drop-in(s)")
        if "env" in acts or "dropin" in acts:
            plan.append(f"write {PIN_DROPIN} (UPDATE_BRANCH=main, UPDATE_ENABLE=1)")
        if needs_restart(i, acts):
            plan.append("restart stereocam" + (" → board self-updates from GitHub"
                                               if "update" in acts else ""))
        else:
            plan.append("no restart (nothing live is wrong; the pin applies next start)")
        print(f"  {str(i.get('name'))[:15]:<16}{', '.join(plan)}")
        if needs_restart(i, acts) and (i.get("LASTREC") or "").endswith("START"):
            print(f"  {'':<16}{WARN}⚠ a take looks like it is RECORDING — the restart will "
                  f"finalize and stop it{OFF}")
    print(f"{DIM}  recordings/, ~/.config/starcap/, and stereocam.service itself are not "
          f"touched; no code is copied to the board.{OFF}")

    if a.check_only:
        print("\n--check-only: nothing was changed. Re-run without it to fix.")
        return 1
    if not a.yes:
        try:
            if input("\nType 'fix' to apply: ").strip() != "fix":
                print("aborted — nothing changed.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted — nothing changed.")
            return 1

    # ---- fix -------------------------------------------------------------- #
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Boards that are not ready and that this tool cannot repair. They must still
    # count against the final exit code — the re-verify below only re-probes the
    # boards we touched, so without this a broken board rides along on someone
    # else's success.
    stuck = [i for i, lab, _, iss in rows if lab != "READY" and not fixable(iss)]
    for i, iss in todo:
        name, host, login = i.get("name"), i["ip"], i["login"]
        print(f"\n{BOLD}{name}{OFF} ({host})")
        acts = fixable(iss)
        # 'wb' pulls ~20 packages over apt and then compiles two C files, which is
        # minutes rather than seconds; everything else here is local config edits.
        rc, out, err = ssh(host, login, build_fix(i, acts, stamp),
                           timeout=600 if "wb" in acts else 120)
        for line in out.splitlines():
            if line.startswith("DID="):
                print(f"    {line[4:]}")
        if rc != 0:
            print(f"    {BAD}fix failed: {err or out or f'rc={rc}'}{OFF}")
            continue

        # Assert the EFFECTIVE environment, not the file we wrote. A drop-in that
        # sorts later still wins, so a run can otherwise report success while the
        # updater state is the opposite of what was asked for.
        rc, out, _ = ssh(host, login,
                         "systemctl show stereocam -p Environment --value | tr ' ' '\\n' "
                         "| grep -E '^UPDATE_'", timeout=25)
        effs = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if effs.get("UPDATE_BRANCH") != "main" or effs.get("UPDATE_ENABLE") != "1":
            print(f"    {BAD}effective env is still {effs or 'empty'} — another drop-in "
                  f"is winning; check {DROPIN_DIR}/{OFF}")
            continue
        print("    asserted: UPDATE_BRANCH=main, UPDATE_ENABLE=1 (effective)")

        if not latest:
            print(f"    {WARN}version not verified (latest unknown){OFF}")
            continue
        if "update" not in acts:
            print(f"    {OK}config fixed; already on {i.get('VERSION')}{OFF}")
            continue
        got = wait_for_version(host, login, latest, a.wait)
        if got == latest:
            print(f"    {OK}now on {got}{OFF}")
        else:
            print(f"    {BAD}still on {got or '?'} after {a.wait}s — it did not self-update.{OFF}")
            log = update_log(host, login)
            if log:
                print(f"{DIM}" + "\n".join("      " + l for l in log.splitlines()) + f"{OFF}")
            print("    Most likely: no route to GitHub from this dock, or a dead token.")
            print("    Hand-deploy fallback (from a FRESHLY FETCHED clone):")
            print(f"      tools/pin_to_branch.sh --host {host} --branch main --updates on")

    # ---- re-verify -------------------------------------------------------- #
    print(f"\n{BOLD}Re-checking…{OFF}")
    with ThreadPoolExecutor(max_workers=8) as ex:
        again = list(ex.map(probe, [{"ip": i["ip"], "via": i.get("via"),
                                     "name": i.get("name")} for i, _ in todo]))
    rows2 = []
    for i in sorted(again, key=lambda x: str(x.get("name"))):
        label, col, issues = assess(i, latest)
        rows2.append((i, label, col, issues))
    print_table(rows2, latest)

    failed = [i for i, lab, _, _ in rows2 if lab != "READY"]
    print()
    if failed or stuck:
        print(f"{BAD}{len(failed) + len(stuck)} board(s) still not ready.{OFF}")
        if stuck:
            print(f"  never fixable over SSH: "
                  f"{', '.join(str(i.get('name')) for i in stuck)}")
        return 1
    print(f"{OK}All {len(rows2)} board(s) are on {latest}, auto-updating, "
          f"pinned to main.{OFF}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted.")
        sys.exit(130)
