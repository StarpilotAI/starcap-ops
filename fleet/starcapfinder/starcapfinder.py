#!/usr/bin/env python3
"""
StarCap Finder — double-click app that lists every StarCap recorder reachable
from THIS laptop (ethernet first) and opens the one you click.

    ./run.sh                    -> http://127.0.0.1:8090
    ./run.sh --port 9100        -> different port
    ./run.sh --no-browser       -> don't auto-open a browser

Why this exists: each recorder already serves a fleet page at /service, but you
can only reach it if you ALREADY know a recorder's IP. This runs the same
discovery from the laptop, so a bare ethernet cable is enough to find them.

How it finds boards (no upload server, no cloud, no internet):
  1. UDP roll-call — broadcast "STARCAP_PEER_DISCOVER v1" on udp/18793; every
     recorder answers with its identity (stereocam/app.py PeerResponder). ~1 s.
  2. Subnet sweep — fallback that probes :8080/api/version across the local /24s
     for boards that miss the broadcast. Runs at startup if the roll-call comes
     back empty, and on demand from the Rescan button.

Live per-device stats are fetched HERE (server side) rather than in the browser:
the recorder's API sends no CORS headers, so a page served from localhost cannot
read it directly. Preview thumbnails are plain <img> streams, which need no CORS.

The 🎵 button plays مزمار عبد السلام on a board's buzzer — see the SING section
below. It borrows opm-name's identify-buzz trick over SSH, so NO recorder
firmware change is involved and nothing has to be deployed to the fleet.

Config via env:
    FINDER_PORT     local web UI port      (default 8090)
    DEVICE_PORT     recorder dashboard port (default 8080)
    PEER_PORT       discovery port          (default 18793)
    FINDER_SUBNETS  comma list of /24 prefixes to sweep, e.g. "10.77.0,192.168.8"
                    (default: auto-detected from this machine's interfaces)
    PI_USER/PI_PASS recorder SSH login for 🎵     (default orangepi/orangepi)
    MOZMAR_TEMPO_MS base note length in ms, the one tempo knob (default 200 —
                    raise it to play the tune slower, lower it to speed it up)

The WB button turns a board's shading correction on or off. It reads which
StarCap answered and which camera body is plugged into it, SSHes in, sets
Environment=WB_ENABLE= in /etc/systemd/system/stereocam.service, restarts
stereocam, then waits for /api/status to say what white balance ACTUALLY came
up as. No recorder firmware change either. Read the WB section before pressing
it — correction on costs the board its frame-timing headroom.

Stdlib only — no pip install. The 🎵 and WB buttons additionally shell out to
`sshpass` and `ssh` (already required by opm-lan / the copy station).
"""
import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_PORT    = int(os.environ.get("FINDER_PORT", "8090"))
DEVICE_PORT = int(os.environ.get("DEVICE_PORT", "8080"))
PEER_PORT   = int(os.environ.get("PEER_PORT", "18793"))
PEER_MAGIC  = b"STARCAP_PEER_DISCOVER v1"

ROLLCALL_EVERY_S = 2.5    # cheap broadcast, keeps the list live
STALE_S          = 8.0    # no answer for this long -> dim the card
DROP_S           = 30.0   # ...and this long -> remove it entirely

# --- 🎵 SING: SSH login + buzzer pin, same defaults as the fleet ---------------
SSH_USER = os.environ.get("PI_USER", "orangepi")
SSH_PASS = os.environ.get("PI_PASS", "orangepi")
# Pin 15 = GPIO0_D4 = gpiochip0 line 28 — the values stereocam/app.py's
# BUZZER_CHIP/BUZZER_LINE fall back to, and the ones opm-name's identify-buzz uses.
BUZZER_CHIP = int(os.environ.get("BUZZER_CHIP", "0"))
BUZZER_LINE = int(os.environ.get("BUZZER_LINE", "28"))
BUZZER_ACTIVE_LOW = os.environ.get("BUZZER_ACTIVE_LOW", "0") == "1"
# Two images, two logins: the Orange Pi 5 build is orangepi/orangepi, the NanoPi
# R76S build is pi/12345, and /api/status does not say which — the R76S answers
# orangepi/orangepi with a plain "Permission denied", so a single hardcoded login
# reports every R76S as a board fault. opm-lan's ssh.accounts probes the same two
# in the same order; PI_USER/PI_PASS just puts a different pair at the front.
SSH_LOGINS = [(SSH_USER, SSH_PASS)]
for _u, _p in (("orangepi", "orangepi"), ("pi", "12345")):
    if (_u, _p) not in SSH_LOGINS:
        SSH_LOGINS.append((_u, _p))

LOCK    = threading.Lock()
DEVICES = {}              # device_id -> merged identity + live status + seen
SWEEP   = {"running": False, "ts": 0.0}
SINGING = {}              # device_id -> {"state": singing|ok|error, "msg", "ts"}
LOGINS  = {}              # ip -> (user, password) that actually worked
WBWORK  = {}              # device_id -> {"state": working|ok|error, "msg", "ts"}


# ----------------------------------------------------------------------------
# Local network shape — which interfaces exist, and which are wired
# ----------------------------------------------------------------------------
_IFACES = {"ts": 0.0, "val": []}
_IFACE_TTL_S = 5.0


def local_ifaces():
    """-> [{iface, ip, network, broadcast, wired}] for every usable IPv4 address.

    Cached for a few seconds: this is consulted for every device on every scan
    cycle, and shelling out to `ip` once per device per tick is pure waste.
    """
    now = time.monotonic()
    if now - _IFACES["ts"] < _IFACE_TTL_S:
        return _IFACES["val"]

    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        txt = ""
    for line in txt.splitlines():
        p = line.split()
        if len(p) < 4 or p[2] != "inet":
            continue
        iface, cidr = p[1], p[3]
        ip = cidr.split("/")[0]
        if ip.startswith(("127.", "169.254.")):
            continue
        brd = p[p.index("brd") + 1] if "brd" in p else None
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        # A wireless interface has a /sys/class/net/<if>/wireless directory;
        # everything else on a laptop LAN is effectively the wired port.
        out.append({"iface": iface, "ip": ip, "network": net,
                    "broadcast": brd or str(net.broadcast_address),
                    "wired": not os.path.isdir(f"/sys/class/net/{iface}/wireless")})

    _IFACES.update(ts=now, val=out)
    return out


def iface_for_ip(ip, ifaces):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for i in ifaces:
        if addr in i["network"]:
            return i
    return None


def broadcast_targets(ifaces):
    """Per-interface broadcast addresses.

    255.255.255.255 alone is not enough: the kernel routes it out the DEFAULT
    route only (wifi, on this laptop), so recorders on the wired capture LAN
    would never hear the roll-call. Hitting each interface's own broadcast
    address reaches all of them.
    """
    t = [i["broadcast"] for i in ifaces if i["broadcast"]]
    t.append("255.255.255.255")
    return list(dict.fromkeys(t))


def local_subnets():
    if os.environ.get("FINDER_SUBNETS"):
        return [s.strip() for s in os.environ["FINDER_SUBNETS"].split(",") if s.strip()]
    return sorted({i["ip"].rsplit(".", 1)[0] for i in local_ifaces()
                   if not i["ip"].startswith(("127.", "169.254."))})


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
def roll_call(timeout=1.5):
    """Broadcast the peer magic and collect every recorder that answers."""
    ifaces = local_ifaces()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    found = {}
    try:
        for b in broadcast_targets(ifaces):
            try:
                s.sendto(PEER_MAGIC, (b, PEER_PORT))
            except OSError:
                pass            # interface down / no route — other targets still go
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
                d = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if d.get("device_id"):
                d.setdefault("ip", addr[0])
                found[d["device_id"]] = d
    finally:
        s.close()
    return found


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def probe(ip):
    """Identify a recorder by its dashboard API. -> identity dict or None."""
    try:
        v = _get_json(f"http://{ip}:{DEVICE_PORT}/api/version", timeout=2.5)
    except Exception:
        return None
    if v.get("service") != "stereocam" or not v.get("device_id"):
        return None
    v["ip"] = ip
    v.setdefault("port", DEVICE_PORT)
    return v


def sweep():
    """Probe every host on the local /24s. Slower than the roll-call, but finds
    boards whose broadcast reply got dropped (some switches/APs filter them)."""
    with LOCK:
        if SWEEP["running"]:
            return
        SWEEP["running"] = True
    try:
        targets = [f"{sub}.{h}" for sub in local_subnets() for h in range(1, 255)]
        with ThreadPoolExecutor(max_workers=128) as ex:
            for res in ex.map(probe, targets):
                if res:
                    merge(res)
    finally:
        with LOCK:
            SWEEP["running"] = False
            SWEEP["ts"] = time.time()


# ----------------------------------------------------------------------------
# Live status enrichment — done here, not in the browser (no CORS on the device)
# ----------------------------------------------------------------------------
def enrich(dev):
    try:
        st = _get_json(f"http://{dev['ip']}:{dev.get('port', DEVICE_PORT)}/api/status", timeout=2.5)
    except Exception:
        return dev
    cam, sysd, up = st.get("camera", {}), st.get("system", {}), st.get("upload", {}) or {}
    dev.update({
        "name":        st.get("name") or dev.get("name"),
        "version":     st.get("version") or dev.get("version"),
        "recording":   bool(cam.get("recording")),
        "fps":         cam.get("capture_fps"),
        "fps_target":  cam.get("fps_target"),
        "duration":    cam.get("duration"),
        "camera_error": cam.get("error"),
        "soc_temp":    sysd.get("soc_temp"),
        "uptime":      sysd.get("uptime"),
        "disk_used":   sysd.get("disk_used", dev.get("disk_used")),
        "disk_total":  sysd.get("disk_total", dev.get("disk_total")),
        "disk_free":   sysd.get("disk_free", dev.get("disk_free")),
        "pending":     up.get("pending_runs"),
        "ethernet":    st.get("ethernet"),
        "record_gate": st.get("record_gate"),
        # panel.buzzer = "the Buzzer object constructed" -> whether 🎵 can work
        # at all on this board (false/absent on the NanoPi R76S). See has_buzzer().
        "buzzer":      (st.get("panel") or {}).get("buzzer"),
        # The two facts the WB button reports on: which camera body is fitted,
        # and whether shading correction is actually running. wb_correct.mode is
        # the authority — "WB_ENABLE=1 in the unit" only means it was asked for.
        "camera_number": cam.get("camera_number"),
        "camera_id":     cam.get("camera_id"),
        "wb_mode":       (st.get("wb_correct") or {}).get("mode"),
        "wb_reason":     (st.get("wb_correct") or {}).get("reason"),
        "wb_cal_error":  (st.get("wb_correct") or {}).get("cal_error"),
        "wb_kernel":     (st.get("wb_correct") or {}).get("kernel"),
    })
    return dev


def merge(identity):
    """Fold a discovery result into DEVICES, tagging how we can reach it."""
    ifaces = local_ifaces()
    i = iface_for_ip(identity.get("ip", ""), ifaces)
    identity["iface"] = i["iface"] if i else None
    identity["wired"] = bool(i and i["wired"])
    identity["seen"]  = time.time()
    with LOCK:
        prev = DEVICES.get(identity["device_id"], {})
        prev.update(identity)
        DEVICES[identity["device_id"]] = prev


def scanner_loop():
    first = True
    while True:
        found = roll_call()
        for d in found.values():
            merge(d)

        # Nothing answered the very first roll-call? Fall back to a sweep once —
        # a fresh board behind a switch that eats broadcasts still shows up.
        if first and not found:
            threading.Thread(target=sweep, daemon=True).start()
        first = False

        with LOCK:
            live = [d for d in DEVICES.values() if time.time() - d["seen"] < STALE_S]
        if live:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for d in ex.map(enrich, live):
                    merge(d)

        with LOCK:
            for did in [k for k, v in DEVICES.items() if time.time() - v["seen"] > DROP_S]:
                del DEVICES[did]

        time.sleep(ROLLCALL_EVERY_S)


def device_list():
    now = time.time()
    with LOCK:
        devs = [dict(d) for d in DEVICES.values()]
    for d in devs:
        d["age"]     = round(now - d["seen"], 1)
        d["online"]  = d["age"] < STALE_S
        d["can_sing"] = has_buzzer(d)
        d["sing"]     = sing_state(d["device_id"])
        d["wb"]       = wb_state(d["device_id"])
    # Wired boards first (that's the capture LAN), then starcap<N> in number
    # order — so the list doesn't reshuffle as boards answer in random order.
    def key(d):
        name = (d.get("name") or d.get("hostname") or "").lower()
        m = re.search(r"(\d+)\s*$", name)
        return (0 if d.get("wired") else 1, 0 if m else 1,
                int(m.group(1)) if m else 0, name)
    return sorted(devs, key=key)


# ----------------------------------------------------------------------------
# SING — play مزمار عبد السلام on a board's buzzer
#
# No recorder firmware involved, by design. This is opm-name's identify-buzz
# trick (starcap-ops/opm-name/opmname.py:_IDENTIFY_SCRIPT) with a melody instead
# of two beeps: SSH in, stop the `stereocam` service (which releases the GPIO
# line and lets the app finalize any in-flight take through its normal SIGTERM
# path), drive the pin with the very same lgpio calls webui/buzzer.py uses, then
# start the service again. The board is briefly offline; nothing is deployed.
#
# One improvement over the original: no `set -e`. If the tune itself dies, the
# restart still runs and the exit code is propagated afterwards — a crashed
# serenade must never leave a field recorder with stereocam down.
# ----------------------------------------------------------------------------
_Q = int(os.environ.get("MOZMAR_TEMPO_MS", "200"))     # base note (~an eighth)

# Maqam Hijaz on D, in the mizmar's own high register — which is also where a
# small piezo is loudest. EB5 is deliberately sharp of a tempered E♭ (622 Hz):
# the mizmar leans that second upward, and since we hand raw Hz to tx_pwm
# nothing forces equal temperament on us. That stretch is most of what keeps it
# sounding Egyptian instead of like a Nokia ringtone.
D5, EB5, F5S, G5, A5, BB5, C6, D6 = 587, 634, 740, 784, 880, 932, 1047, 1175

# (freq_hz, duration_ms); freq 0 = rest. Same frame format as webui/buzzer.py's
# JINGLES, so anything tuned here can be pasted into the firmware later.
#
# THIS IS A BEST-EFFORT TRANSCRIPTION — tell me what's wrong by phrase and note
# ("phrase 2, third note too high", "phrase 3 drags") and I'll edit the frames.
MOZMAR = [
    # ── phrase 1 — the call: climb the Hijaz tetrachord, land on A and sit there
    (D5, _Q // 2), (EB5, _Q // 2), (F5S, _Q), (G5, _Q),
    (A5, _Q * 2), (0, _Q // 3),

    # ── phrase 2 — the answer: turn off the top, fall back home to D
    (BB5, _Q // 2), (A5, _Q // 2), (G5, _Q), (F5S, _Q * 2), (0, _Q // 4),
    (EB5, _Q // 2), (D5, _Q * 2), (0, _Q // 2),

    # ── phrase 3 — the big one: same shape taken up to the octave, with the
    #    two-note trill the mizmar always puts on the top note
    (A5, _Q // 2), (BB5, _Q // 2), (C6, _Q), (D6, _Q * 2),
    (C6, _Q // 4), (D6, _Q // 4), (C6, _Q // 4), (D6, _Q + _Q // 2), (0, _Q // 3),

    # ── phrase 4 — the drop: run all the way down the maqam and hold the tonic
    (C6, _Q // 2), (BB5, _Q // 2), (A5, _Q // 2), (G5, _Q // 2),
    (F5S, _Q), (EB5, _Q // 2), (D5, _Q * 3), (0, _Q // 2),

    # ── tag — the little "khalas" flick everyone claps to at the end
    (D5, _Q // 3), (F5S, _Q // 3), (A5, _Q // 3), (D6, _Q * 2),
]


def _ssh(ip, script, timeout, login=None):
    user, pw = login or (SSH_USER, SSH_PASS)
    return subprocess.run(
        ["sshpass", "-p", pw, "ssh",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=8",
         f"{user}@{ip}", script],
        capture_output=True, text=True, timeout=timeout)


def ssh_login(ip):
    """-> the (user, password) this board accepts, or None if neither does.

    One cheap `id -un` per candidate, cached per IP for the life of the process:
    the answer is a property of the board's image and never changes under us."""
    with LOCK:
        hit = LOGINS.get(ip)
    if hit:
        return hit
    for login in SSH_LOGINS:
        try:
            r = _ssh(ip, "id -un", timeout=14, login=login)
        except (OSError, subprocess.SubprocessError):
            return None                      # no sshpass, or the host is gone
        if r.returncode == 0:
            with LOCK:
                LOGINS[ip] = login
            return login
    return None


_SING_SCRIPT = r'''
echo '__PW__' | sudo -S systemctl stop stereocam 2>/dev/null
rc=0
python3 - <<'PY' || rc=$?
import lgpio, time
CHIP, LINE, SILENT = __CHIP__, __LINE__, __SILENT__
FRAMES = __FRAMES__
h = lgpio.gpiochip_open(CHIP)
lgpio.gpio_claim_output(h, LINE, SILENT)

def tone(freq):
    # tx_pwm can go silent by RETURNING <0 instead of raising once a channel is
    # wedged — the exact failure buzzer.py heals. Free + re-claim the line and
    # retry once, so a wedged board can't report a serenade nobody heard.
    rc = lgpio.tx_pwm(h, LINE, freq, 50)
    if isinstance(rc, int) and rc < 0:
        lgpio.gpio_free(h, LINE)
        lgpio.gpio_claim_output(h, LINE, SILENT)
        lgpio.tx_pwm(h, LINE, freq, 50)

try:
    for freq, ms in FRAMES:
        if freq > 0:
            tone(freq)
        else:
            lgpio.tx_pwm(h, LINE, 0, 0)
            lgpio.gpio_write(h, LINE, SILENT)
        time.sleep(ms / 1000.0)
finally:
    lgpio.tx_pwm(h, LINE, 0, 0)
    lgpio.gpio_write(h, LINE, SILENT)
    lgpio.gpio_free(h, LINE)
    lgpio.gpiochip_close(h)
PY
echo '__PW__' | sudo -S systemctl start stereocam 2>/dev/null
exit $rc
'''


def song_ms(frames=MOZMAR):
    return sum(ms for _, ms in frames)


def has_buzzer(d):
    """/api/status' panel.buzzer is the authority — it's literally "did the
    Buzzer object construct". Fall back to the board name for boards whose
    status predates that field: the NanoPi R76S has no buzzer hardware at all
    (and no lgpio), so 🎵 is an Orange Pi 5 feature only."""
    b = d.get("buzzer")
    if b is not None:
        return bool(b)
    return "orange" in (d.get("board") or "").lower()


def _login_error(ip):
    return (f"no SSH login worked for {ip} — tried "
            + ", ".join(u for u, _ in SSH_LOGINS))


def _set_wb_or_sing_login_error(dev_id, ip):
    _set_sing(dev_id, "error", _login_error(ip))
    return {"ok": False, "error": _login_error(ip)}


def _set_sing(dev_id, state, msg=""):
    with LOCK:
        SINGING[dev_id] = {"state": state, "msg": msg, "ts": time.time()}


def sing_state(dev_id):
    with LOCK:
        s = SINGING.get(dev_id)
    # Let a finished result linger briefly so the card can show it, then clear.
    if s and s["state"] != "singing" and time.time() - s["ts"] > 6:
        with LOCK:
            SINGING.pop(dev_id, None)
        return None
    return s


def sing(dev_id):
    """Serenade one discovered board. Runs the guards inline (so the click gets a
    real answer: no buzzer / recording / offline) then plays in the background —
    the song outlasts any sane HTTP wait, so the card polls SINGING for progress."""
    with LOCK:
        d = dict(DEVICES.get(dev_id) or {})
        busy = (SINGING.get(dev_id) or {}).get("state") == "singing"
    # A refusal is parked in SINGING like any other outcome, so the card keeps
    # showing WHY for a few seconds instead of the next poll wiping it.
    def refuse(msg):
        _set_sing(dev_id, "error", msg)
        return {"ok": False, "error": msg}

    # Only ever SSH to a board we discovered ourselves — never to an address
    # handed in by the page, so this endpoint can't be used as an SSH relay.
    if not d.get("ip"):
        return refuse("unknown device")
    if busy:
        return {"ok": False, "error": "already singing"}   # leave the state alone
    if not has_buzzer(d):
        return refuse(f"{d.get('board') or 'this board'} has no buzzer")

    # Re-check recording LIVE. The card's copy can be a poll stale, and stopping
    # stereocam mid-take interrupts a real shoot — never do that for a song.
    ip, port = d["ip"], d.get("port", DEVICE_PORT)
    try:
        st = _get_json(f"http://{ip}:{port}/api/status", timeout=3)
        if (st.get("camera") or {}).get("recording"):
            return refuse("recording — not interrupting a take")
    except Exception as e:
        return refuse(f"no answer from {ip} ({type(e).__name__})")

    _set_sing(dev_id, "singing")
    threading.Thread(target=_sing_run, args=(dev_id, ip), daemon=True).start()
    return {"ok": True, "started": True, "ms": song_ms()}


def _sing_run(dev_id, ip):
    login = ssh_login(ip)
    if not login:
        return _set_wb_or_sing_login_error(dev_id, ip)
    script = (_SING_SCRIPT
              .replace("__PW__", login[1])
              .replace("__CHIP__", str(BUZZER_CHIP))
              .replace("__LINE__", str(BUZZER_LINE))
              .replace("__SILENT__", "1" if BUZZER_ACTIVE_LOW else "0")
              .replace("__FRAMES__", repr(MOZMAR)))
    # Song + the service stop/start round trip, with room to spare.
    timeout = song_ms() / 1000.0 + 30
    try:
        r = _ssh(ip, script, timeout=timeout, login=login)
    except FileNotFoundError:
        res = {"ok": False, "error": "sshpass not installed on this laptop"}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "error": "SSH timed out"}
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        res = ({"ok": True} if r.returncode == 0
               else {"ok": False, "error": (r.stderr or "failed").strip()[:200]})
    _set_sing(dev_id, "ok" if res["ok"] else "error", res.get("error", ""))
    return res


# ----------------------------------------------------------------------------
# WB — shading correction on / off (Environment=WB_ENABLE in the unit)
#
# BEFORE PRESSING IT: white balance defaults OFF fleet-wide, and that is a FRAME
# TIMING decision, not a colour one. stereocam/app.py's own bench numbers —
# R76S + DECXIN, 3-minute takes, N extra busy cores added mid-take:
#
#                       WB ON                        WB OFF
#   idle        60.10 fps, 0.00% out-of-band   60.08 fps, 0.03%
#   +1 core     50.64 fps, 15.2%  <-- FAILS    60.10 fps, 0.00%
#   +4 cores    43.72 fps, 29.0%               60.03 fps, 0.11%
#
# WB on its own is not the fault — idle, the full kernel is 74% CPU and drops
# nothing. What it removes is HEADROOM: with it on, one competing core costs a
# third of the frames and the p99 interval spec fails. So this is a toggle, it
# asks before switching on, and it always reports the state it left the board in.
#
# What it does NOT touch: the camera-side 4000 K UVC white-balance PIN. That is a
# control write, costs no per-frame work and applies either way — takes are still
# shot at a fixed colour temperature, they are simply not shading-corrected on
# the board. Uncorrected footage is recoverable in post; wrongly corrected is not.
#
# STICKY ACROSS UPDATES: the repo's stereocam.service does NOT name WB_ENABLE,
# and post_update.sh re-injects every Environment= line the installed unit
# already had. An ON set here therefore survives every future OTA until it is
# switched back off — which is why OFF DELETES the line rather than writing =0,
# putting the board back on whatever the repo template defaults to.
# ----------------------------------------------------------------------------
# Exit codes start at 12: sshpass owns 1-7 (5 = password rejected, 6/7 = host
# key), and a collision would report a board fault for a login problem.
_WB_SCRIPT = r'''
UNIT=/etc/systemd/system/stereocam.service
S(){ echo '__PW__' | sudo -S -p '' "$@"; }
[ -f "$UNIT" ] || exit 12
if [ "__WANT__" = "off" ]; then
    S sed -i '/^Environment=WB_ENABLE=/d' "$UNIT"
    grep -q '^Environment=WB_ENABLE=' "$UNIT" && exit 13
else
    if grep -q '^Environment=WB_ENABLE=' "$UNIT"; then
        S sed -i 's/^Environment=WB_ENABLE=.*/Environment=WB_ENABLE=1/' "$UNIT"
    elif grep -q '^Environment=AUTOSTART=' "$UNIT"; then
        S sed -i '/^Environment=AUTOSTART=/a Environment=WB_ENABLE=1' "$UNIT"
    else
        S sed -i '0,/^\[Service\]/s//[Service]\nEnvironment=WB_ENABLE=1/' "$UNIT"
    fi
    grep -q '^Environment=WB_ENABLE=1$' "$UNIT" || exit 13
fi
S systemctl daemon-reload || exit 14
S systemctl restart stereocam || exit 15
'''

_WB_EXIT = {5:  "SSH password rejected by the board",
            6:  "SSH host key unknown", 7: "SSH host key changed",
            12: "no stereocam.service on that board",
            13: "the unit edit did not stick (sudo password wrong?)",
            14: "systemctl daemon-reload failed",
            15: "stereocam would not restart",
            22: "no orangepi-recorder checkout in that user's home",
            23: "apt could not install the gstreamer dev headers",
            24: "wbcorrect failed to compile (see .wbcorrect.err on the board)",
            25: "stereocam would not restart after the rebuild"}


def _set_wb(dev_id, state, msg=""):
    with LOCK:
        WBWORK[dev_id] = {"state": state, "msg": msg, "ts": time.time()}


def wb_state(dev_id):
    # Longer linger than the 🎵 result: "on, but uncalibrated" is an answer worth
    # reading, not just a chime you already heard.
    with LOCK:
        s = WBWORK.get(dev_id)
    if s and s["state"] != "working" and time.time() - s["ts"] > 45:
        with LOCK:
            WBWORK.pop(dev_id, None)
        return None
    return s


def _sample_fps(ip, port, seconds=8):
    """-> "60.1" / "56.2, 3.5 dropped/s", or None if the board went quiet.

    capture_fps is a 2-second rolling average and reads optimistically for a
    while after a restart, so wait a beat and carry the DROPPED delta with it:
    a board losing frames steadily still shows a healthy-looking instantaneous
    fps, and that is the number that gives it away."""
    def rd():
        try:
            c = _get_json(f"http://{ip}:{port}/api/status", timeout=3).get("camera") or {}
            return c.get("capture_fps"), c.get("dropped")
        except Exception:
            return None, None
    time.sleep(5)
    f0, d0 = rd()
    time.sleep(seconds)
    f1, d1 = rd()
    if f1 is None:
        return None
    out = f"{float(f1):.1f} fps"
    if d0 is not None and d1 is not None and d1 > d0:
        out += f", {round((d1 - d0) / float(seconds), 1)} dropped/s"
    return out


def wb_set(dev_id, want_on):
    """Flip WB_ENABLE on one discovered board, then say what actually came up.

    One guard, same as sing(): only ever SSH to a board we discovered ourselves,
    never an address handed in by the page. Unlike sing() it does NOT refuse
    mid-take — this is a bench tool, and `systemctl restart` sends SIGTERM, which
    the recorder handles through its normal finalize path, so a take in flight
    ends cleanly rather than being corrupted."""
    with LOCK:
        d = dict(DEVICES.get(dev_id) or {})
        busy = (WBWORK.get(dev_id) or {}).get("state") == "working"

    def refuse(msg):
        _set_wb(dev_id, "error", msg)
        return {"ok": False, "error": msg}

    if not d.get("ip"):
        return refuse("unknown device")
    if busy:
        return {"ok": False, "error": "already working"}      # leave the state alone

    # Re-read live rather than trusting the card's copy — this is also where the
    # board tells us WHO it is: its starcap name and the camera body plugged in.
    ip, port = d["ip"], d.get("port", DEVICE_PORT)
    try:
        st = _get_json(f"http://{ip}:{port}/api/status", timeout=3)
    except Exception as e:
        return refuse(f"no answer from {ip} ({type(e).__name__})")

    who  = st.get("name") or d.get("name") or ip
    camd = st.get("camera") or {}
    cam  = camd.get("camera_id") or camd.get("camera_number")
    label = f"{who} · cam #{cam}" if cam is not None else who

    _set_wb(dev_id, "working",
            f"{label} — {'turning WB on' if want_on else 'turning WB off'}…")
    threading.Thread(target=_wb_run, args=(dev_id, ip, port, want_on, label),
                     daemon=True).start()
    return {"ok": True, "started": True, "target": label}


def _wb_verify(dev_id, ip, port, want_on, label):
    """Read the outcome back off the board and park the verdict.

    The only place a WB claim is allowed to come from: a clean exit code
    means the commands ran, not that correction is running correctly."""
    # The unit is edited and the service restarted — but that only means WB was
    # ASKED for. On a camera with no baked .wbc the board comes up "uncalibrated"
    # and records uncorrected at full rate, which is indistinguishable from here
    # unless it is read back. So wait for stereocam's HTTP to answer again and
    # report wb_correct.mode, never our own intent.
    deadline, mode = time.time() + 60, None
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            st = _get_json(f"http://{ip}:{port}/api/status", timeout=3)
        except Exception:
            continue                                   # still restarting
        wbc  = st.get("wb_correct") or {}
        mode = wbc.get("mode")
        if not mode:
            continue
        if want_on and mode != "off":
            # Three separate claims, checked in order, because any one of them can
            # be false while the others hold: the mode says correction is running,
            # the KERNEL says it is running the right code, and the fps says frames
            # are still coming out the other side.
            if mode != "native" or wbc.get("degraded"):
                why = wbc.get("reason") or wbc.get("cal_error") or "not correcting"
                return _set_wb(dev_id, "error", f"{label}: WB enabled but {mode} — {why}")
            kern = wbc.get("kernel") or "full"
            fps = _sample_fps(ip, port)
            # "native" is NOT the same as "correct". A board that cannot compile the
            # plugin falls back to the binary committed in the repo, which can be
            # older than the source running beside it — worth about 4 fps and a
            # dropped frame every third of a second. Reporting that as plain "WB is
            # on" is the lie this check exists to stop.
            if "prebuilt" in kern or "stale" in kern:
                err = (wbc.get("build_error") or "").split(";")[0].strip()
                return _set_wb(dev_id, "warn",
                    f"{label}: WB on, but the kernel is {kern} — a STALE PREBUILT"
                    + (f" ({fps})" if fps else "") + ". "
                    + (err + ". " if err else "")
                    + "Fix: apt install libgstreamer-plugins-base1.0-dev, "
                      "then sudo bash stereocam/post_update.sh and restart.")
            return _set_wb(dev_id, "ok",
                f"{label}: WB ON — native, {kern} kernel"
                + (f" — {fps}" if fps else " (no fps reading)"))
        if not want_on and mode == "off":
            fps = _sample_fps(ip, port)
            return _set_wb(dev_id, "ok", f"{label}: WB OFF"
                           + (f" — {fps}" if fps else "") + ", headroom back")
    return _set_wb(dev_id, "error",
                   f"{label}: restarted, but status still reads "
                   f"{mode or 'nothing'} after 60 s")


def _wb_run(dev_id, ip, port, want_on, label):
    login = ssh_login(ip)
    if not login:
        return _set_wb(dev_id, "error", f"{label}: {_login_error(ip)}")
    # The sudo password is whatever got us in — pi/12345 on an R76S, not the
    # orangepi default, and the unit edit needs root.
    script = (_WB_SCRIPT.replace("__PW__", login[1])
                        .replace("__WANT__", "on" if want_on else "off"))
    try:
        r = _ssh(ip, script, timeout=90, login=login)
    except FileNotFoundError:
        return _set_wb(dev_id, "error", "sshpass not installed on this laptop")
    except subprocess.TimeoutExpired:
        return _set_wb(dev_id, "error", f"{label}: SSH timed out")
    except Exception as e:
        return _set_wb(dev_id, "error", f"{label}: {type(e).__name__}: {e}")
    if r.returncode != 0:
        why = _WB_EXIT.get(r.returncode)
        if not why:
            tail = (r.stderr or "").strip().splitlines()
            why = tail[-1][:160] if tail else f"failed (exit {r.returncode})"
        return _set_wb(dev_id, "error", f"{label}: {why}")

    return _wb_verify(dev_id, ip, port, want_on, label)


# ----------------------------------------------------------------------------
# REBUILD — compile wbcorrect natively on a board that fell back to the prebuilt
#
# Boards ship without the gstreamer dev headers, so post_update.sh cannot compile
# vision/gstwbcorrect.c and app.py loads the binary committed in the repo instead.
# That binary can be OLDER than the source running beside it (the v1.38.0
# fixed-band kernel against v1.5x+ sources), which costs ~4 fps and drops a frame
# roughly every third of a second — while /api/status still says "native", because
# native it is. Only `kernel` gives it away, which is why this action exists and
# why it is offered ONLY on a board reporting a stale kernel.
#
# Measured, starcap94 + starcap101, both R76S v1.64.0, take rolling:
#   stale prebuilt -> 55.0-59.1 fps, ~3.5 dropped/s, continuous
#   native build   -> 60.1 fps, 0 dropped over 75 s
# The .so goes 35360 -> 75576 bytes and the stamp full-prebuilt-stale -> full.
#
# This is a per-board repair. The fleet-wide fix is regenerating prebuilt/ with
# vision/build_prebuilt.sh on aarch64 and committing it.
# ----------------------------------------------------------------------------
_WB_BUILD_SCRIPT = r'''
APP_DIR="$HOME/orangepi-recorder/stereocam"
[ -f "$APP_DIR/post_update.sh" ] || exit 22
S(){ echo '__PW__' | sudo -S -p '' "$@"; }
S env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev >/tmp/wbbuild.apt 2>&1 || {
    tail -3 /tmp/wbbuild.apt; exit 23; }
pkg-config --exists gstreamer-video-1.0 || { echo "headers still absent after apt"; exit 23; }
# post_update.sh does a lot more than this build and can exit non-zero for
# reasons that have nothing to do with wbcorrect, so judge it by its own output
# and by the compiler's error file, never by its exit code.
S bash "$APP_DIR/post_update.sh" >/tmp/wbbuild.log 2>&1 || true
if ! grep -qi 'rebuilt libgstwbcorrect' /tmp/wbbuild.log; then
    [ -s "$APP_DIR/vision/.wbcorrect.err" ] && { head -3 "$APP_DIR/vision/.wbcorrect.err"; exit 24; }
fi
S systemctl restart stereocam || exit 25
'''


def wb_rebuild(dev_id):
    """Repair a board whose wbcorrect is a stale prebuilt, then re-verify it.

    Same guards and the same verification as wb_set(): the claim at the end is
    read back off the board, never assumed from a clean exit code."""
    with LOCK:
        d = dict(DEVICES.get(dev_id) or {})
        busy = (WBWORK.get(dev_id) or {}).get("state") == "working"

    def refuse(msg):
        _set_wb(dev_id, "error", msg)
        return {"ok": False, "error": msg}

    if not d.get("ip"):
        return refuse("unknown device")
    if busy:
        return {"ok": False, "error": "already working"}

    ip, port = d["ip"], d.get("port", DEVICE_PORT)
    try:
        st = _get_json(f"http://{ip}:{port}/api/status", timeout=3)
    except Exception as e:
        return refuse(f"no answer from {ip} ({type(e).__name__})")
    wbc = st.get("wb_correct") or {}
    kern = wbc.get("kernel") or ""
    if "prebuilt" not in kern and "stale" not in kern:
        return refuse(f"nothing to rebuild — kernel already reads {kern or 'unknown'}")

    who = st.get("name") or d.get("name") or ip
    camd = st.get("camera") or {}
    cam = camd.get("camera_id") or camd.get("camera_number")
    label = f"{who} · cam #{cam}" if cam is not None else who

    _set_wb(dev_id, "working", f"{label} — installing headers and rebuilding wbcorrect…")
    threading.Thread(target=_wb_build_run, args=(dev_id, ip, port, label), daemon=True).start()
    return {"ok": True, "started": True, "target": label}


def _wb_build_run(dev_id, ip, port, label):
    login = ssh_login(ip)
    if not login:
        return _set_wb(dev_id, "error", f"{label}: {_login_error(ip)}")
    script = _WB_BUILD_SCRIPT.replace("__PW__", login[1])
    try:
        # apt over a board's ethernet is the slow part; the compile is seconds.
        r = _ssh(ip, script, timeout=600, login=login)
    except FileNotFoundError:
        return _set_wb(dev_id, "error", "sshpass not installed on this laptop")
    except subprocess.TimeoutExpired:
        return _set_wb(dev_id, "error", f"{label}: rebuild timed out (apt too slow?)")
    except Exception as e:
        return _set_wb(dev_id, "error", f"{label}: {type(e).__name__}: {e}")
    if r.returncode != 0:
        why = _WB_EXIT.get(r.returncode)
        if not why:
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            why = tail[-1][:160] if tail else f"failed (exit {r.returncode})"
        return _set_wb(dev_id, "error", f"{label}: {why}")
    return _wb_verify(dev_id, ip, port, True, label)


# ----------------------------------------------------------------------------
# Web UI
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StarCap Finder</title>
<style>
 :root{--bg:#0a0e14;--panel:#0f1620;--panel2:#131c28;--line:#1e2b3a;--txt:#c8d6e5;--dim:#6b7d90;
       --accent:#21d4fd;--accent2:#2dd4bf;--ok:#39d98a;--warn:#ffcf5c;--rec:#ff4d6d;
       --mono:'JetBrains Mono','SF Mono',ui-monospace,Consolas,monospace}
 *{box-sizing:border-box}body{margin:0;background:radial-gradient(1100px 480px at 80% -10%,#10202e,transparent 60%),var(--bg);
   color:var(--txt);font-family:var(--mono);font-size:13px;line-height:1.5}
 header{display:flex;align-items:center;gap:14px;padding:13px 22px;border-bottom:1px solid var(--line);
   background:linear-gradient(180deg,#0d141d,#0a0e14);position:sticky;top:0;z-index:5}
 .logo{font-weight:700;letter-spacing:3px;color:#fff;font-size:15px}.logo b{color:var(--accent)}
 .tag{color:var(--dim);font-size:11px;letter-spacing:1px}.spacer{flex:1}
 button{font-family:var(--mono);border:1px solid var(--line);background:var(--panel2);color:var(--txt);
   padding:7px 14px;border-radius:8px;cursor:pointer;font-size:12px}
 button:hover{border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
 main{padding:20px;max-width:1280px;margin:0 auto}
 .note{color:var(--dim);font-size:11px;margin:0 0 14px}
 .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;
   cursor:pointer;transition:border-color .12s,transform .12s}
 .card:hover{border-color:var(--accent);transform:translateY(-2px)}
 .card.stale{opacity:.45}
 .card .prev{width:100%;aspect-ratio:10/3;background:#05080c;display:block;object-fit:cover}
 .card .ph{width:100%;aspect-ratio:10/3;background:var(--panel2);display:grid;place-items:center;color:var(--dim);font-size:11px}
 .body{padding:12px 14px}
 .row1{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .name{color:#fff;font-weight:600;font-size:14px}
 .badge{font-size:10px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
 .badge.rec{border-color:var(--rec);color:var(--rec)}
 .badge.eth{border-color:var(--accent2);color:var(--accent2)}
 .badge.warn{border-color:var(--warn);color:var(--warn)}
 .meta{color:var(--dim);font-size:11px;margin-top:3px}
 .kv{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--dim)}
 .kv b{color:#fff;font-weight:600}
 .kv .bad{color:var(--rec)}
 .kv .good{color:var(--ok)}
 .bar{height:6px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:9px}
 .bar>span{display:block;height:100%}
 .disk{color:var(--dim);font-size:10px;margin-top:4px}
 .open{display:inline-block;font-size:11px;border:1px solid var(--line);
   border-radius:7px;padding:6px 12px;color:var(--accent);text-decoration:none}
 .open:hover{border-color:var(--accent)}
 .acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
 .sing{font-size:11px;padding:6px 11px;border-radius:7px}
 .sing:hover:not(:disabled){border-color:var(--accent2);color:var(--accent2)}
 .sing.busy{border-color:var(--accent2);color:var(--accent2);animation:pulse 1s ease-in-out infinite}
 .sing.ok{border-color:var(--ok);color:var(--ok)}
 .sing.err{border-color:var(--rec);color:var(--rec)}
 .wbb{font-size:11px;padding:6px 11px;border-radius:7px}
 .wbb:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
 .wbb.on{border-color:var(--ok);color:var(--ok)}
 .wbb.warn{border-color:var(--warn);color:var(--warn)}
 .wbb.busy{border-color:var(--accent);color:var(--accent);animation:pulse 1s ease-in-out infinite}
 .wbb.err{border-color:var(--rec);color:var(--rec)}
 .wbfix{font-size:11px;padding:6px 11px;border-radius:7px;border-color:var(--warn);color:var(--warn)}
 .wbfix:hover:not(:disabled){border-color:var(--warn);color:var(--txt);background:rgba(255,207,92,.12)}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
 .offline{display:none;margin:0 0 14px;padding:10px 14px;border-radius:10px;font-size:12px;
   border:1px solid var(--rec);color:var(--rec);background:rgba(255,77,109,.08)}
 .offline b{color:var(--txt)}
 .empty{color:var(--dim);text-align:center;padding:46px 24px;border:1px solid var(--line);border-radius:12px}
 .empty h3{color:var(--txt);font-size:13px;margin:0 0 10px;font-weight:600}
 .empty ul{text-align:left;max-width:460px;margin:0 auto;font-size:11px;line-height:1.9}
</style></head><body>
<header>
 <div class="logo"><b>◇</b> STAR<span style="color:var(--accent)">CAP</span> FINDER</div>
 <div class="tag" id="count">scanning…</div><div class="spacer"></div>
 <div class="tag" id="sweepnote"></div>
 <button id="rescan" onclick="rescan()">⟳ Rescan network</button>
</header>
<main>
 <p class="note">Recorders answering a UDP roll-call on this laptop's networks. Click a card to open its dashboard —
   or hit <b>🎵</b> to play مزمار عبد السلام on its buzzer (Orange Pi 5 only; pauses the recorder for a few seconds, never mid-take).
   <b>WB</b> shows each board's shading correction and toggles it — it restarts stereocam, and correction ON costs the frame-timing headroom that holds 60 fps.</p>
 <div class="offline" id="offline"></div>
 <div class="grid" id="grid"></div>
 <div class="empty" id="empty" style="display:none">
   <h3>No StarCaps answering yet…</h3>
   <ul>
     <li>· ethernet cable seated at both ends?</li>
     <li>· board powered and past boot (~30 s to answer)?</li>
     <li>· hit <b>Rescan network</b> to force a full subnet sweep</li>
   </ul>
 </div>
</main>
<script>
const $=s=>document.querySelector(s);
const fb=b=>{if(b==null)return'—';if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];
  const i=Math.floor(Math.log(b)/Math.log(1024));return (b/Math.pow(1024,i)).toFixed(i?1:0)+' '+u[i]};
const fmtDur=t=>{t=Math.floor(t||0);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;
  return (h?h+'h ':'')+(m?m+'m ':'')+s+'s'};
const cards={};   // device_id -> element (kept so live previews don't restart every poll)

function makeCard(d){
  const base=`http://${d.ip}:${d.port||8080}`;
  const el=document.createElement('div'); el.className='card';
  // Anything interactive inside the card handles its own click — otherwise
  // hitting 🎵 would also navigate away to the dashboard.
  el.onclick=e=>{ if(!e.target.closest('a,button')) location.href=base+'/'; };
  el.innerHTML=`<img class="prev" src="${base}/preview_small.mjpg"
      onerror="this.outerHTML='<div class=ph>no preview</div>'">
    <div class="body">
      <div class="row1"><span class="name nm"></span>
        <span class="badge rec rb" style="display:none">● REC</span>
        <span class="badge eth eb" style="display:none">ethernet</span>
        <span class="badge warn wb" style="display:none"></span></div>
      <div class="meta mt"></div>
      <div class="kv kvs"></div>
      <div class="dk"></div>
      <div class="acts">
        <a class="open" href="${base}/" target="_blank" rel="noopener">Open dashboard ↗</a>
        <button class="sing" onclick="sing('${d.device_id}',this)">🎵 Sing</button>
        <button class="wbb" onclick="wbToggle('${d.device_id}',this)">WB …</button>
        <button class="wbfix" hidden onclick="wbRebuild('${d.device_id}',this)">Rebuild WB</button>
      </div>
    </div>`;
  return el;
}

function fill(el,d){
  el.classList.toggle('stale',!d.online);
  el.querySelector('.nm').textContent=d.name||d.hostname||d.device_id;
  el.querySelector('.rb').style.display=d.recording?'inline-block':'none';
  el.querySelector('.eb').style.display=d.wired?'inline-block':'none';
  const wb=el.querySelector('.wb');
  const warn = d.camera_error ? 'cam error' : (!d.online ? 'not answering' : '');
  wb.style.display=warn?'inline-block':'none'; wb.textContent=warn;
  el.querySelector('.mt').textContent=
    `${d.ip}:${d.port||8080} · v${d.version||'?'} · ${d.board||'?'}`
    +`${d.camera_id!=null?' · cam #'+d.camera_id:''}${d.iface?' · '+d.iface:''}`;

  const slow = d.fps!=null && d.fps_target && d.fps < d.fps_target*0.95;
  const hot  = d.soc_temp>80;
  const kv=[];
  kv.push(`fps <b class="${slow?'bad':''}">${d.fps!=null?d.fps.toFixed(0):'—'}</b>`);
  kv.push(`temp <b class="${hot?'bad':''}">${d.soc_temp!=null?d.soc_temp.toFixed(0)+'°':'—'}</b>`);
  kv.push(`up <b>${d.uptime!=null?fmtDur(d.uptime):'—'}</b>`);
  if(d.wb_mode) kv.push(`wb <b class="${d.wb_mode==='native'?'good':d.wb_mode==='off'?'':'bad'}">${d.wb_mode}</b>`);
  if(d.recording) kv.push(`rec <b>${fmtDur(d.duration)}</b>`);
  if(d.pending)   kv.push(`pending <b>${d.pending}</b>`);
  el.querySelector('.kvs').innerHTML=kv.join('');

  const dk=el.querySelector('.dk');
  if(d.disk_total){
    const pct=Math.min(100,Math.round((d.disk_used/d.disk_total)*100));
    const col=pct>=90?'var(--rec)':pct>=75?'var(--warn)':'var(--accent2)';
    dk.innerHTML=`<div class="bar"><span style="width:${pct}%;background:${col}"></span></div>
      <div class="disk">disk ${fb(d.disk_free)} free of ${fb(d.disk_total)} · ${pct}% used</div>`;
  } else dk.innerHTML=`<div class="disk">disk —</div>`;

  // 🎵 state is the SERVER's (the song outlasts the POST), so a reload — or a
  // second browser window — still shows a board mid-serenade.
  const sb=el.querySelector('.sing'), s=d.sing;
  const set=(cls,txt,tip,off)=>{sb.className='sing'+(cls?' '+cls:'');
    sb.textContent=txt; sb.title=tip||''; sb.disabled=!!off};
  if(!d.can_sing)                 set('', '🎵 no buzzer', (d.board||'this board')+' has no buzzer hardware', true);
  else if(s&&s.state==='singing') set('busy','🎵 singing…','مزمار عبد السلام', true);
  else if(s&&s.state==='error')   set('err', '🎵 failed', s.msg, !d.online);
  else if(s&&s.state==='ok')      set('ok',  '🎵 ✓',      'مزمار عبد السلام');
  else if(d.recording)            set('', '🎵 Sing', 'recording — not interrupting a take', true);
  else                            set('', '🎵 Sing', d.online?'مزمار عبد السلام':'not answering', !d.online);

  // WB. The LABEL is the live mode straight from /api/status (off / native /
  // uncalibrated / python-probe); the CLICK always asks for the opposite of it.
  // A work result outranks the label for a few seconds so the answer is readable.
  const wbb=el.querySelector('.wbb'), w=d.wb, mode=d.wb_mode;
  const setw=(cls,txt,tip,off)=>{wbb.className='wbb'+(cls?' '+cls:'');
    wbb.textContent=txt; wbb.title=tip||''; wbb.disabled=!!off};
  wbb.dataset.on = (mode && mode!=='off') ? '0' : '1';
  const blocked = !d.online;
  const why = !d.online ? 'not answering'
            : d.recording ? 'restarts stereocam — the take in flight ends (finalized cleanly)' : '';
  if(w&&w.state==='working')    setw('busy','WB …',        w.msg, true);
  else if(w&&w.state==='error') setw('err', 'WB failed',   w.msg, !d.online);
  else if(w&&w.state==='warn')  setw('warn','WB ⚠',        w.msg);
  else if(w&&w.state==='ok')    setw('on',  'WB ✓',        w.msg);
  else if(mode==null)           setw('',    'WB —',        'no status from this board', true);
  else if(mode==='off')         setw('',    'Turn WB on',  why||'correction is off — turning it on costs frame-timing headroom', blocked);
  else if(mode==='native')      setw('on',  'Turn WB off', why||('correcting now, '+(d.wb_kernel||'full')+' kernel'), blocked);
  else                          setw('warn','Turn WB off', why||(d.wb_reason||d.wb_cal_error||'enabled but not correcting'), blocked);

  // The repair only exists as an option when the board is actually reporting the
  // fault, so it can never be pressed on a board whose kernel is already native.
  const fixb=el.querySelector('.wbfix'), stale=/prebuilt|stale/.test(d.wb_kernel||'');
  fixb.hidden = !stale;
  fixb.disabled = !d.online || (w&&w.state==='working');
  fixb.title = stale ? 'kernel is '+d.wb_kernel+' — install the gstreamer dev headers, '
                     + 'recompile wbcorrect on this board and restart (a few minutes)' : '';
}

async function tick(){
  // The finder SERVER (this page's own backend) dying used to be invisible: the
  // fetch threw, we returned, and every card sat frozen on its last poll — a board
  // that started recording after the server died still showed no ● REC. Silent
  // stale data is worse than no data, so say so and grey the cards out.
  let r;
  try{ r=await(await fetch('/api/devices')).json(); }
  catch(e){
    $('#offline').style.display='block';
    $('#offline').innerHTML='<b>Finder server not responding.</b> '
      +'Showing the last known state — badges (● REC, fps, disk) are frozen and may be wrong. '
      +'Restart it with <code>systemctl --user restart starcapfinder</code>.';
    $('#count').textContent='disconnected';
    for(const id in cards) cards[id].classList.add('stale');
    return;
  }
  $('#offline').style.display='none';
  const devs=r.devices||[], grid=$('#grid'), seen=new Set();
  const n=devs.filter(d=>d.online).length;
  $('#count').textContent=`${n} recorder${n!==1?'s':''} found`;
  $('#empty').style.display=devs.length?'none':'block';
  $('#sweepnote').textContent=r.sweeping?'sweeping subnets…':'';
  $('#rescan').disabled=!!r.sweeping;
  for(const d of devs){ seen.add(d.device_id);
    let el=cards[d.device_id];
    if(!el){ el=makeCard(d); cards[d.device_id]=el; }
    // Re-append in rank order so the grid stays sorted without rebuilding
    // the <img> streams (which would restart every preview each poll).
    grid.appendChild(el);
    fill(el,d);
  }
  for(const id in cards){ if(!seen.has(id)){ cards[id].remove(); delete cards[id]; } }
}
async function sing(id,btn){
  btn.disabled=true; btn.className='sing busy'; btn.textContent='🎵 singing…';
  try{
    const r=await(await fetch('/api/sing',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device_id:id})})).json();
    if(!r.ok){ btn.className='sing err'; btn.textContent='🎵 failed'; btn.title=r.error||'failed'; }
  }catch(e){ btn.className='sing err'; btn.textContent='🎵 failed'; btn.title=String(e); }
  tick();          // hand the button over to the server-side state
}
async function wbToggle(id,btn){
  const on = btn.dataset.on==='1';
  // Only the ON direction asks. Turning correction OFF gives frames back — it is
  // the direction that can cost a take that needs the confirmation.
  if(on && !confirm('Turn white balance ON for this board?\n\n'
     +'· stereocam restarts (a few seconds offline; a take in flight ends, finalized cleanly)\n'
     +'· correction eats the frame-timing headroom: one busy core then costs ~15% of frames\n'
     +'· the setting survives every future update until you turn it off here')) return;
  btn.disabled=true; btn.className='wbb busy'; btn.textContent='WB …';
  try{
    const r=await(await fetch('/api/wb',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device_id:id,on})})).json();
    if(!r.ok){ btn.className='wbb err'; btn.textContent='WB !'; btn.title=r.error||'failed'; }
  }catch(e){ btn.className='wbb err'; btn.textContent='WB !'; btn.title=String(e); }
  tick();          // hand the button over to the server-side state
}
async function wbRebuild(id,btn){
  if(!confirm('Rebuild wbcorrect on this board?\n\n'
     +'· installs the gstreamer dev headers over the board\'s network\n'
     +'· recompiles the plugin and restarts stereocam (a take in flight ends)\n'
     +'· takes a few minutes, mostly apt')) return;
  btn.disabled=true; btn.textContent='Rebuilding…';
  try{
    const r=await(await fetch('/api/wb/rebuild',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device_id:id})})).json();
    if(!r.ok){ btn.textContent='Rebuild WB'; btn.title=r.error||'failed'; }
  }catch(e){ btn.textContent='Rebuild WB'; btn.title=String(e); }
  tick();
}
async function rescan(){ $('#rescan').disabled=true;
  try{ await fetch('/api/rescan',{method:'POST'}); }catch(e){} setTimeout(tick,300); }
tick(); setInterval(tick,2000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/devices"):
            with LOCK:
                sweeping = SWEEP["running"]
            self._send(200, json.dumps({"devices": device_list(), "sweeping": sweeping,
                                        "subnets": local_subnets()}).encode(),
                       "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/api/rescan"):
            threading.Thread(target=sweep, daemon=True).start()
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path.startswith("/api/wb/rebuild"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or "{}")
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
            res = wb_rebuild(str(body.get("device_id") or ""))
            self._send(200 if res["ok"] else 409,
                       json.dumps(res).encode(), "application/json")
        elif self.path.startswith("/api/wb"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or "{}")
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
            res = wb_set(str(body.get("device_id") or ""), bool(body.get("on")))
            self._send(200 if res["ok"] else 409,
                       json.dumps(res).encode(), "application/json")
        elif self.path.startswith("/api/sing"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or "{}")
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
            res = sing(str(body.get("device_id") or ""))
            self._send(200 if res["ok"] else 409,
                       json.dumps(res).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass          # quiet — this runs windowless from the desktop launcher


def already_running(port):
    """True if OUR finder already owns the port (double-click twice -> just
    focus the existing one instead of dying on 'address in use')."""
    try:
        _get_json(f"http://127.0.0.1:{port}/api/devices", timeout=1.5)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Find StarCap recorders on this laptop's networks")
    ap.add_argument("--port", type=int, default=WEB_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}"

    if already_running(a.port):
        print(f"[finder] already running at {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return

    threading.Thread(target=scanner_loop, daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"[finder] {url}   (subnets: {', '.join(local_subnets()) or 'none'})")
    if not a.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[finder] bye")


if __name__ == "__main__":
    main()
