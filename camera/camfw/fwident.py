#!/usr/bin/env python3
"""Identify which DECXIN firmware build a docked camera is running.

There is no firmware version string anywhere on these cameras -- the USB
serial reads "01.00.000" on every unit regardless of build. So the build is
identified by what it *exposes* instead.

Two independent classes of evidence:

  DESCRIPTOR  (instant, no capture)   bcdDevice, the USB alt-setting
              bandwidth ladder, and the v4l2 control RANGES, all of which
              differ between the builds.

  TIMING      (needs ~10 s of frames) the frame length the sensor actually
              runs at -- 16626/16627 dithered on the bad build, a flat
              16640 on the good one.

TIMING IS THE AUTHORITY. The descriptor markers are a fast pre-check: they
are only trustworthy if they are rewritten by a reflash, which is exactly
what `--verify` is for. Never grade a camera on descriptors alone unless
that has been confirmed on a camera you personally reflashed.

    ./fwident.py                 # grade every docked camera (descriptors only)
    ./fwident.py --measure       # ...and confirm each one from its frame timing
    ./fwident.py --measure --push  # ...and record it on the dashboard's /cameras page
    ./fwident.py --fleet         # what the dashboard currently holds for the whole fleet

The camera NUMBER comes from the vendor-burned USB product string ("DECXIN Camera
78.1" -> 78.1), the same name the recorder reports as ground truth. Nothing here
gates a check-out: a camera on the old build dispatches exactly as before.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

VID = "1bcf"
CFG = Path.home() / ".config" / "starcap"

# Descriptor markers, as measured on camera 26 (bad) and camera 78.1 (good)
# on 2026-09-14. `ladder` is the bytes-per-microframe of alt settings 1..6,
# which is where the two builds diverge most visibly.
PROFILES = {
    "dither-16626": {
        "bcdDevice": "9234",
        "ladder": [192, 384, 512, 640, 800, 944],
        "gain": (118, 138), "gamma": (118, 138), "brightness": (0, 255),
        "nominal_us": [16626, 16627], "fps": 60.147, "exposure_us": (4990, 5000),
    },
    "static-16640": {
        "bcdDevice": "0234",
        "ladder": [944, 2048, 3072, 5120, 6144, 9216],
        "gain": (0, 255), "gamma": (0, 255), "brightness": (0, 1),
        "nominal_us": [16640], "fps": 60.096, "exposure_us": (6000, 8400),
    },
}
VERDICT = {"dither-16626": "BAD  — produces the frame-timing jitter, fails p99",
           "static-16640": "GOOD — no jitter events, passes p99"}


def docked():
    """Every DECXIN, with its capture node (sysfs index 0; 1 is metadata)."""
    out = []
    for s in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            if open(f"{s}/idVendor").read().strip() != VID:
                continue
        except OSError:
            continue
        rd = lambda f: (open(f"{s}/{f}").read().strip()
                        if os.path.exists(f"{s}/{f}") else "")
        node = None
        for v in sorted(glob.glob("/sys/class/video4linux/video*")):
            if os.path.realpath(f"{v}/device").startswith(os.path.realpath(s)) \
                    and open(f"{v}/index").read().strip() == "0":
                node = "/dev/" + os.path.basename(v)
                break
        out.append({"bus_id": os.path.basename(s), "product": rd("product"),
                    "bcdDevice": rd("bcdDevice"), "serial": rd("serial"),
                    "speed": rd("speed"), "device": node})
    return out


def ladder(bus_id):
    """Bytes per 125 us microframe for streaming alt settings 1..6."""
    try:
        txt = subprocess.run(["lsusb", "-v", "-s", bus_id.split("-")[0] + ":"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    alts, cur = {}, None
    for ln in txt.splitlines():
        s = ln.strip()
        m = re.match(r"bAlternateSetting\s+(\d+)", s)
        if m:
            cur = int(m.group(1)); alts.setdefault(cur, {})
        if cur is None:
            continue
        for pat, key in ((r"wMaxPacketSize\s+0x[0-9a-f]+\s+\S+\s+(\d+) bytes", "mps"),
                         (r"bMaxBurst\s+(\d+)", "burst"), (r"Mult\s+(\d+)", "mult")):
            m = re.match(pat, s)
            if m and key not in alts[cur]:
                alts[cur][key] = int(m.group(1))
    out = []
    for a in range(1, 7):
        x = alts.get(a, {})
        out.append(x["mps"] * (x.get("burst", 0) + 1) * (x.get("mult", 0) + 1)
                   if "mps" in x else 0)
    return out


def ctrl_ranges(dev):
    if not dev:
        return {}
    txt = subprocess.run(["v4l2-ctl", "-d", dev, "-L"],
                         capture_output=True, text=True).stdout
    out = {}
    for ln in txt.splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\(\w+\)\s*:\s*min=(-?\d+)\s+max=(-?\d+)", ln)
        if m:
            out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def grade_descriptors(cam):
    """Score each profile by how many independent markers it matches.

    The alt-setting ladder is only comparable over a SuperSpeed link. It is built from
    the SuperSpeed endpoint companion descriptors (bMaxBurst, Mult), which a USB 2.0
    enumeration does not carry at all — the device then advertises an entirely different,
    much smaller set of alt settings. Counting that as a failed marker reported a
    correctly-flashed camera as 4/5 and invited exactly the question "same firmware, why
    4/5?". So on a non-SuperSpeed link the ladder is dropped from the denominator and the
    reason is stated, rather than being silently scored as a disagreement.
    """
    ss = str(cam.get("speed") or "") == "5000"
    lad, rng = (ladder(cam["bus_id"]) if ss else []), ctrl_ranges(cam["device"])
    scores = {}
    for name, p in PROFILES.items():
        hits, total, why = 0, 0, []
        total += 1
        if cam["bcdDevice"] == p["bcdDevice"]:
            hits += 1; why.append(f"bcdDevice={cam['bcdDevice']}")
        if lad:
            total += 1
            if lad == p["ladder"]:
                hits += 1; why.append(f"alt ladder {lad[:3]}…")
        for k in ("gain", "gamma", "brightness"):
            if k in rng:
                total += 1
                if rng[k] == p[k]:
                    hits += 1; why.append(f"{k} {rng[k][0]}–{rng[k][1]}")
        scores[name] = (hits, total, why)
    best = max(scores, key=lambda n: scores[n][0])
    h, t, why = scores[best]
    if not ss:
        why.append("ladder skipped — USB 2.0 link")
    return (best if h else None), h, t, why, lad, rng


def measure(dev, secs=11):
    """The authority: what frame length is the sensor actually running?"""
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "camsignature.py"),
                        dev, str(secs)], capture_output=True, text=True, timeout=180)
    txt = r.stdout
    m = re.search(r"frame interval : \{([^}]*)\}", txt)
    if not m:
        return None, txt.strip()
    vals = sorted(int(x) for x in re.findall(r"(\d+):", m.group(1)))
    fam = "dither-16626" if len(vals) > 1 or 16626 in vals else \
          "static-16640" if vals == [16640] else f"unknown {vals}"
    j = re.search(r"jitter events\s*:\s*(\d+)", txt)
    return fam, (f"intervals {vals}, "
                 f"{j.group(1) if j else '?'} jitter events in {secs}s")


def camera_number(product):
    """The camera id burned into the USB product string, e.g. 'DECXIN  Camera 78.1'
    -> '78.1'. Returns None rather than guessing: filing a grading against the wrong
    number is worse than not filing one."""
    m = re.search(r"Camera\s+(\d+(?:\.\d+)?)\s*$", (product or "").strip())
    return m.group(1) if m else None


def camera_body(camera):
    """The BODY the firmware belongs to: "78.1" -> "78".

    A re-lensed camera is renamed on the USB descriptor ("78" -> "78.1"), but the
    revision names the GLASS and firmware lives in the body. The dashboard files
    calibrations per lens revision and everything else — custody, fingerprint — on the
    bare number, so a grading posted as "78.1" would be stored under an id the /cameras
    overview never looks up, and silently never appear. Post the body."""
    return (camera or "").split(".")[0]


def _cfg(name, env):
    v = os.environ.get(env, "").strip()
    if v:
        return v
    p = CFG / name
    if p.is_file():
        return p.read_text().strip()
    return ""


def dash(method, path, body=None):
    url = (_cfg("dashboard_url", "DASHBOARD_URL") or "https://starcap.dev").rstrip("/")
    pw = _cfg("dashboard_password", "STARCAP_PASSWORD")
    if not pw:
        raise SystemExit("no dashboard password — set STARCAP_PASSWORD or put it in "
                         "~/.config/starcap/dashboard_password")
    req = urllib.request.Request(url + path, method=method)
    req.add_header("X-Starcap-Password", pw)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def show_fleet():
    r = dash("GET", "/api/ingest/camera-firmware")
    if not r.get("ok"):
        sys.exit(f"dashboard: {r.get('error')}")
    c, n = r["counts"], r["numbers"]
    print(f"\nfleet firmware — latest build is {r['latest']}\n")
    print(f"  on latest ({c['latest']:>3}) : {', '.join(n['latest']) or '—'}")
    print(f"  on old    ({c['old']:>3}) : {', '.join(n['old']) or '—'}")
    if c["unknown"]:
        print(f"  unknown   ({c['unknown']:>3}) : {', '.join(n['unknown'])}")
    print(f"\n  {c['graded']} cameras graded in all.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", metavar="CAMERA",
                    help="grade just this one — a camera number (26), a node "
                         "(/dev/video2) or a USB bus id (2-2). Default: every docked camera")
    ap.add_argument("--measure", action="store_true",
                    help="also confirm each camera from its frame timing (~11 s each)")
    ap.add_argument("--push", action="store_true",
                    help="record each grading on the dashboard's /cameras page")
    ap.add_argument("--fleet", action="store_true",
                    help="print what the dashboard holds for the whole fleet, then exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out", metavar="PATH",
                    help="also write the results as JSON here, so a front end can read "
                         "what a streamed run concluded without running it a second time")
    a = ap.parse_args()

    if a.fleet:
        show_fleet(); return

    cams = docked()
    if not cams:
        sys.exit("no DECXIN cameras docked")

    if a.only:
        want = a.only.strip()
        # Accept whichever handle the caller happens to have: the camera number is what
        # a person reads off the sticker, the node is what a script holds, and the bus id
        # is the only one that stays unique when two cameras share a burned name.
        cams = [c for c in cams
                if want in (c["bus_id"], c["device"], camera_number(c["product"]))
                or want == (c["product"] or "").strip()]
        if not cams:
            sys.exit(f"no docked camera matches {a.only!r} — "
                     f"try a camera number, /dev/videoN, or a USB bus id")

    results = []
    for c in cams:
        guess, hits, total, why, lad, rng = grade_descriptors(c)
        rec = {**c, "camera": camera_number(c["product"]),
               "descriptor_build": guess, "markers": f"{hits}/{total}",
               "matched": why, "ladder": lad,
               "ranges": {k: list(v) for k, v in rng.items()
                          if k in ("gain", "gamma", "brightness")}}
        if a.measure and c["device"]:
            fam, detail = measure(c["device"])
            rec["measured_build"] = fam
            rec["measured_detail"] = detail
            rec["agrees"] = (fam == guess)
        results.append(rec)

    # PUSH ONLY WHAT WAS MEASURED, unless explicitly told otherwise. A descriptor-only
    # grading is a guess about a build the camera may have been reflashed away from, and
    # the dashboard record is what the fleet then acts on.
    if a.push:
        for r in results:
            if not r["camera"]:
                r["push"] = f"no camera number in USB name {r['product']!r} — not pushed"
                continue
            build = r.get("measured_build") or r["descriptor_build"]
            if not build:
                r["push"] = "unrecognised — not pushed"
                continue
            method = "timing" if r.get("measured_build") else "descriptor"
            jit = None
            m = re.search(r"(\d+) jitter events", r.get("measured_detail") or "")
            secs = re.search(r"in (\d+)s", r.get("measured_detail") or "")
            if m and secs and int(secs.group(1)):
                jit = round(int(m.group(1)) / int(secs.group(1)), 3)
            body_id = camera_body(r["camera"])
            res = dash("POST", "/api/ingest/camera-firmware", {
                "camera": body_id, "build": build, "method": method,
                "jitter_per_s": jit, "usb_name": r["product"],
                "markers": {"bcdDevice": r["bcdDevice"],
                            "gain": r["ranges"].get("gain")},
                "by": os.uname().nodename,
            })
            r["push"] = ("recorded" if res.get("ok")
                         else f"FAILED — {res.get('error')}")
            if res.get("ok") and body_id != r["camera"]:
                r["push"] += (f" under camera {body_id} — {r['camera']} names the lens, "
                              f"firmware belongs to the body")
            if res.get("ok") and not res.get("known"):
                r["push"] += " (build not in the dashboard's registry — shows as unknown)"

    if a.json_out:
        try:
            with open(a.json_out, "w") as f:
                json.dump(results, f, indent=2)
        except OSError as e:
            print(f"[could not write {a.json_out}: {e}]")

    if a.json:
        print(json.dumps(results, indent=2)); return

    for r in results:
        print(f"\n{r['product']}   ({r['bus_id']}, {r['device']})"
              + (f"   -> camera {r['camera']}" if r["camera"] else "   -> NO CAMERA NUMBER"))
        if str(r.get("speed") or "") != "5000":
            print(f"  ** USB {r.get('speed')} Mbps — this camera is NOT on a USB 3 link. "
                  f"It cannot stream 4000x1200 at 60 fps here; move it to a blue port "
                  f"or replace the cable before measuring.")
        print(f"  descriptors say : {r['descriptor_build'] or 'UNRECOGNISED'}"
              f"   [{r['markers']} markers: {', '.join(r['matched']) or 'none'}]")
        if "measured_build" in r:
            print(f"  frame timing say: {r['measured_build']}   [{r['measured_detail']}]")
            if not r["agrees"]:
                print("  ** DISAGREE — trust the timing. The descriptors are stale, "
                      "which means a reflash does not rewrite them.")
        b = r.get("measured_build") or r["descriptor_build"]
        print(f"  => {VERDICT.get(b, 'unknown build — grade it by hand')}")
        if "push" in r:
            print(f"  dashboard       : {r['push']}")
    if not a.measure:
        print("\n(descriptors only — add --measure to confirm from the frame timing)")
    if a.push:
        show_fleet()


if __name__ == "__main__":
    main()
