#!/usr/bin/env python3
"""Which docked starcaps have a camera that jitters.

Plug the dock switch into this laptop's ethernet (the starcap-dock profile shares
10.77.0.1/24 over enp2s0), and this sweeps every recorder on the cable and grades
the camera attached to each one.

IT NEVER OPENS THE CAMERA. The firmware build is read from two sysfs files the
kernel fills in at enumeration:

    /sys/bus/usb/devices/<x>/bcdDevice     9234 = old build, 0234 = latest
    /sys/bus/usb/devices/<x>/descriptors   raw USB blob -> alt-setting ladder

Both are world-readable, need no v4l2 and no root, and — the point — claim nothing.
A recorder mid-take is safe to grade. Measuring the frame timing instead would mean
fighting the recorder for /dev/video0 and 11 s per board, which does not scale to 48.

The SSH layer is opm-lan's, imported rather than reimplemented: it already knows
that Orange Pi boards answer orangepi/orangepi and NanoPi boards pi/12345, probes
once per IP and remembers which answered.

    ./dockscan.py              # sweep and print the table
    ./dockscan.py --push       # ...and file each verdict on the dashboard
"""
import argparse
import concurrent.futures
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time

# opm-lan (discovery + the fleet ssh logins) lives in the starcap-dashboard
# repo. STARCAP_DASHBOARD overrides where that checkout is.
OPMLAN = os.path.join(os.environ.get("STARCAP_DASHBOARD",
                                     os.path.expanduser("~/starcap-dashboard")), "opm-lan")
sys.path.insert(0, OPMLAN)
import opmlan                                                       # noqa: E402

FWIDENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fwident.py")

# Descriptor markers, measured on known units 2026-09-14. Keep in step with the
# dashboard's lib/firmware-core.ts.
PROFILES = {
    "dither-16626": {"bcd": "9234", "ladder": [192, 384, 512, 640, 800, 944],
                     "verdict": "JITTERS", "ok": False},
    "static-16640": {"bcd": "0234", "ladder": [944, 2048, 3072, 5120, 6144, 9216],
                     "verdict": "clean", "ok": True},
}

# Runs ON the recorder. Pure sysfs: finds every DECXIN and parses its own descriptor
# blob down to six numbers, so 65 KB per camera never crosses the wire.
REMOTE = r'''
import glob, json, os, struct

def ladder(path):
    try:
        b = open(path, "rb").read()
    except OSError:
        return []
    i, out, cur = 0, {}, None
    while i + 1 < len(b):
        ln, typ = b[i], b[i + 1]
        if ln < 2:
            break
        d = b[i:i + ln]
        if typ == 0x04 and ln >= 9:
            cur = d[3] if (d[5] == 0x0E and d[6] == 0x02) else None
        elif typ == 0x05 and ln >= 7 and cur is not None:
            out[cur] = {"mps": struct.unpack_from("<H", d, 4)[0] & 0x7FF, "burst": 0, "mult": 0}
        elif typ == 0x30 and ln >= 6 and cur in out:
            out[cur]["burst"] = d[2]
            out[cur]["mult"] = d[3] & 0x03
        i += ln
    return [out[a]["mps"] * (out[a]["burst"] + 1) * (out[a]["mult"] + 1)
            if a in out else 0 for a in range(1, 7)]

def rd(p, b=""):
    try:
        return open(p).read().strip()
    except OSError:
        return b

cams = []
for s in sorted(glob.glob("/sys/bus/usb/devices/*")):
    if rd(s + "/idVendor") != "1bcf":
        continue
    cams.append({"bus": os.path.basename(s), "product": rd(s + "/product"),
                 "bcd": rd(s + "/bcdDevice"), "ladder": ladder(s + "/descriptors"),
                 "speed": rd(s + "/speed")})

home = os.path.expanduser("~")
print(json.dumps({
    "name": rd(home + "/.config/starcap/device_name"),
    "device_id": rd(home + "/.config/starcap/device_id"),
    "version": (rd(home + "/orangepi-recorder/stereocam/VERSION") or "").split("\n")[0],
    "hostname": os.uname().nodename,
    "cameras": cams,
}))
'''


def grade(cam):
    """Which build, and how many markers agreed.

    The ladder is only comparable over a SuperSpeed link — it is built from the
    SuperSpeed endpoint companion descriptors, which a USB 2.0 enumeration does not
    carry. On such a link it is dropped from the score rather than counted as a
    disagreement, which would otherwise report a correctly-flashed camera as 1/2.
    """
    ss = str(cam.get("speed") or "") == "5000"
    best, hits = None, 0
    for name, p in PROFILES.items():
        n = (cam.get("bcd") == p["bcd"]) + (ss and cam.get("ladder") == p["ladder"])
        if n > hits:
            best, hits = name, n
    return (best if hits else None), hits


def camera_number(product):
    m = re.search(r"Camera\s+(\d+(?:\.\d+)?)\s*$", (product or "").strip())
    return m.group(1) if m else None


def _oneline(s, n=70):
    """SSH puts warnings and errors on several lines; the table needs one.

    The known-hosts warning is dropped, not shortened: UserKnownHostsFile=/dev/null
    makes it fire on EVERY connection, so keeping it means the one line the operator
    reads is a notice that is always true and never the reason anything failed."""
    lines = [ln.strip() for ln in (s or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if "Permanently added" not in ln and not ln.startswith("Warning:")]
    return re.sub(r"\s+", " ", " ".join(lines))[:n] or "no answer"


# Not every address on a dock segment is a recorder — the managed switch has a
# management interface on it. Identifying that by the SSH error text alone proved
# unreliable: the same switch refused with "no matching host key type" on one sweep
# and "Connection closed by" on the next, so one of them slipped through as a
# starcap that failed to answer.
#
# So a refusal is only the TRIGGER. What settles it is a positive signal — an open
# telnet port, which no recorder has — and the verdict is then remembered against the
# device's MAC, which is stable while its DHCP address is not.
_SSH_REFUSED = ("no matching host key type", "no matching key exchange",
                "protocol version mismatch", "connection closed by",
                "connection reset by")
IGNORE_FILE = os.path.expanduser("~/.config/starcap/dockscan-ignore.json")


def _mac_for(ip):
    try:
        out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:                                           # noqa: BLE001
        return None
    m = re.search(r"lladdr\s+([0-9a-f:]{17})", out)
    return m.group(1) if m else None


def _load_ignore():
    try:
        with open(IGNORE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remember_ignore(mac, ip, why):
    d = _load_ignore()
    d[mac] = {"ip": ip, "why": why, "when": int(time.time())}
    try:
        os.makedirs(os.path.dirname(IGNORE_FILE), exist_ok=True)
        with open(IGNORE_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def _is_network_gear(ip):
    """Telnet open = infrastructure. No recorder runs telnetd; every managed switch
    does. Only asked after SSH has already refused, so it costs nothing on a sweep
    where every board answers."""
    try:
        with socket.create_connection((ip, 23), timeout=2):
            return True
    except OSError:
        return False


def probe(cfg, ip, retry=True):
    # A device already known to be network gear is skipped before SSH, which also
    # saves the 25 s timeout it would otherwise burn on every sweep.
    mac = _mac_for(ip)
    known = _load_ignore()
    if mac and mac in known:
        return {"ip": ip, "not_recorder": True,
                "error": f"known network gear ({known[mac].get('why', '')})"}
    try:
        r = opmlan._ssh(cfg, ip, "python3 -", timeout=25, stdin=REMOTE)
    except Exception as e:                                          # noqa: BLE001
        return {"ip": ip, "error": _oneline(str(e))}
    if r.returncode != 0 or not (r.stdout or "").strip():
        raw = (r.stderr or "no answer")
        err = _oneline(raw)
        # Classify on the RAW text: _oneline truncates, and the cut lands mid-phrase
        # ("...no matching host key typ"), so matching the short form silently fails.
        if any(k in raw.lower() for k in _SSH_REFUSED) and _is_network_gear(ip):
            if mac:
                _remember_ignore(mac, ip, "telnet open, ssh refused")
            return {"ip": ip, "not_recorder": True, "error": "network gear (telnet open)"}
        # A board that is still booting answers TCP and then times out. One retry
        # costs 25 s on a genuinely dead address and saves a re-run on a slow one.
        if retry and ("timed out" in err.lower() or "closed by remote" in err.lower()):
            return probe(cfg, ip, retry=False)
        return {"ip": ip, "error": err}
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return {"ip": ip, "error": "unparseable reply: " + _oneline(r.stdout, 50)}
    d["ip"] = ip
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true",
                    help="file each verdict on the dashboard's /cameras page")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()

    cfg = opmlan.load_config()
    ips, iface = opmlan.discover(cfg)
    if not ips:
        _, subnet = opmlan.wired_iface_subnet(cfg)
        sys.exit(f"nothing answered on {iface or 'any wired interface'} "
                 f"({subnet or 'no subnet'}) — is the dock switch plugged into this "
                 f"laptop, and is the dock profile up? (station/setup_dock_lan.sh --check)")
    # stderr, not stdout: --json must emit nothing but JSON, or every consumer has to
    # strip a banner off the front before parsing.
    print(f"{len(ips)} host(s) answering on {iface}\n",
          file=sys.stderr if a.json else sys.stdout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(lambda ip: probe(cfg, ip), ips))

    # Grade before emitting, so --json is self-describing: a consumer should not have
    # to re-implement the marker comparison to know what a row means.
    for r in rows:
        for c in r.get("cameras") or []:
            b, hits = grade(c)
            c["build"], c["markers"] = b, hits
            c["camera"] = camera_number(c.get("product"))

    if a.json:
        print(json.dumps(rows, indent=2)); return

    old, clean, unknown, nocam, bad, skipped = [], [], [], [], [], []
    print(f"{'starcap':<14}{'ip':<14}{'camera':<9}{'build':<15}{'verdict'}")
    print("-" * 66)
    for r in sorted(rows, key=lambda r: r.get("name") or r["ip"]):
        who = r.get("name") or r["ip"]
        if r.get("not_recorder"):
            skipped.append(r["ip"])
            continue
        if r.get("error"):
            bad.append(who)
            print(f"{who:<14}{r['ip']:<14}{'—':<9}{'—':<15}no answer: {r['error']}")
            continue
        if not r.get("cameras"):
            nocam.append(who)
            print(f"{who:<14}{r['ip']:<14}{'—':<9}{'—':<15}NO CAMERA on this board")
            continue
        for c in r["cameras"]:
            num = c.get("camera") or "?"
            build, hits = c["build"], c["markers"]
            mark = PROFILES[build]["verdict"] if build else "UNRECOGNISED"
            (clean if build == "static-16640" else old if build == "dither-16626"
             else unknown).append(f"{who}/{num}")
            ss = str(c.get("speed") or "") == "5000"
            note = "" if ss else "  ** USB 2.0 LINK — cannot stream 60 fps"
            if ss and hits != 2:
                note = f"  [only {hits}/2 markers]"
            print(f"{who:<14}{r['ip']:<14}{num:<9}{build or '—':<15}{mark}{note}")

    print(f"\n  {len(clean):>3} clean      {', '.join(clean) or '—'}")
    print(f"  {len(old):>3} JITTER     {', '.join(old) or '—'}")
    if unknown:
        print(f"  {len(unknown):>3} unknown    {', '.join(unknown)}")
    if nocam:
        print(f"  {len(nocam):>3} no camera  {', '.join(nocam)}")
    if bad:
        print(f"  {len(bad):>3} no answer  {', '.join(bad)}")
    if skipped:
        print(f"  {len(skipped):>3} not a recorder (ignored)  {', '.join(skipped)}")

    if a.push:
        spec = importlib.util.spec_from_file_location("fwident", FWIDENT)
        m = importlib.util.module_from_spec(spec)
        saved, sys.argv = sys.argv, ["fwident"]
        try:
            spec.loader.exec_module(m)
        finally:
            sys.argv = saved
        print()
        for r in rows:
            for c in r.get("cameras") or []:
                num = camera_number(c["product"])
                build, hits = grade(c)
                if not num or not build:
                    continue
                res = m.dash("POST", "/api/ingest/camera-firmware", {
                    "camera": m.camera_body(num), "build": build,
                    # Read off the dock, never measured — the dashboard shows this
                    # as unconfirmed, and a later bench measurement overwrites it.
                    "method": "descriptor",
                    "usb_name": c["product"], "markers": {"bcdDevice": c["bcd"]},
                    "by": f"dock/{r.get('name') or r['ip']}",
                })
                print(f"  camera {num}: "
                      + ("recorded" if res.get("ok") else f"FAILED — {res.get('error')}"))


if __name__ == "__main__":
    main()
