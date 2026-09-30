#!/usr/bin/env python3
"""
camera_fingerprint — does this DECXIN emit a JPEG the Rockchip hardware decoder will take?

Run it on a LAPTOP with the camera plugged in. Nothing is written to any board and the
camera is only read from, so it is safe to run on a unit that is about to ship.

WHY THIS EXISTS
---------------
A DECXIN can read a flawless 60 fps on a laptop and still lose ~40% of its frames on a
NanoPi R76S. The two machines do not decode the same way:

    laptop      libjpeg-turbo / ffmpeg    SOFTWARE — lenient, warns and recovers
    NanoPi      Rockchip MPP hal_jpegd_rkv  HARDWARE — rigid, aborts the frame

So a camera whose bitstream is technically out of spec but practically decodable looks
perfect on a laptop and fails in the field. Measured on camera 77.1: 1200/1200 frames
decoded in software (with 1200 warnings), while two separate R76S boards logged
`hal_jpegd_rkv: decode result: failed, irq 0x0000214d` and recorded ~34 fps instead of 60.

Worse, the usual laptop check cannot see it at all: `v4l2-ctl --stream-to` writes raw
bytes without ever decoding, so it happily reports 60.1 fps on a camera that is broken
for its actual target hardware. This script reads the MARKER STRUCTURE instead, which is
what the hardware parser chokes on.

WHAT IT FLAGS
-------------
  stub APP1     An APP1 segment too short to hold an identifier (no "Exif\\0\\0"). Seen on
                camera 77.1 as seglen=4, payload 00 00, on every single frame. ffmpeg logs
                "unable to decode APP fields" and carries on; the hardware parser does not.

  restart mkrs  A DRI segment plus RST0-7 markers inside the scan. Hardware JPEG parsers
                resynchronise at RST markers and that path is where non-deterministic
                failures live — which fits an INTERMITTENT ~40% loss better than a
                per-frame-identical anomaly does (a constant defect would fail 100%).

  size spread   Reported for completeness. It was the first theory and it is WRONG for
                77.1: 99% of its frames sit in a 320-322 KB band, which cannot produce a
                40% failure rate. Kept so the next investigation does not re-chase it.

USAGE
    ./camera_fingerprint.py                     # auto-detect the DECXIN, capture, report
    ./camera_fingerprint.py --frames 200        # longer capture
    ./camera_fingerprint.py --device /dev/video2
    ./camera_fingerprint.py --file cap.mjpg     # re-analyse an existing capture
    ./camera_fingerprint.py --keep              # keep the .mjpg for later diffing

Every run appends one row to camera_fingerprints.csv next to this script, so docking
cameras one after another builds the fleet table on its own.
"""
import argparse
import csv
import glob
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "camera_fingerprints.csv")

DECXIN_VID, DECXIN_PID = "1bcf", "2d50"

# APP1 payloads that are legitimate. Anything shorter than the shortest of these cannot
# be carrying an identifier at all, which is the defect we are hunting.
KNOWN_APP1 = (b"Exif\x00\x00", b"http://ns.adobe.com/xap/1.0/\x00")

C_RED, C_YEL, C_GRN, C_DIM, C_BLD, C_OFF = (
    "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m")
if not sys.stdout.isatty():
    C_RED = C_YEL = C_GRN = C_DIM = C_BLD = C_OFF = ""


# ---------------------------------------------------------------- USB identity

def _descriptor(usb_dir):
    def rd(name):
        try:
            with open(os.path.join(usb_dir, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    product = rd("product")
    m = re.search(r"(\d{1,4})(?:\.(\d{1,3}))?\s*$", product)
    cam_id = (m.group(1) + ("." + m.group(2) if m.group(2) else "")) if m else "?"
    return {
        "usb_path": os.path.basename(usb_dir),
        "product": product,
        "camera_id": cam_id,
        "revised": bool(m and m.group(2)),   # the ".N" — flash rewritten twice
        "bcd_device": rd("bcdDevice"),
        "serial": rd("serial"),
        "link_speed_mbps": rd("speed"),
        "max_power": rd("bMaxPower"),
    }


def usb_identity(dev=None):
    """Descriptor fields for the camera behind `dev`, or the first DECXIN if dev is None.

    Resolving THROUGH the video node matters: with two DECXINs docked at once, "the first
    match on the bus" attributes one camera's bitstream to the other's serial number,
    which is exactly the kind of mix-up that would invalidate a fleet comparison. So walk
    up from /sys/class/video4linux/<node> until a directory carries idVendor.

    The product string is the point of interest: it is read out of the camera's own SPI
    flash, so "DECXIN  Camera 77.1" is physical evidence that the unit was written a
    SECOND time to record a lens revision, while a bare "DECXIN  Camera 170" was not.
    Only ~21 of 127 fleet cameras carry a .N suffix, and the suffix is the best available
    proxy for "this unit's flash has been rewritten more than once".
    """
    if dev:
        node = os.path.basename(dev)
        p = os.path.realpath(f"/sys/class/video4linux/{node}")
        for _ in range(8):
            p = os.path.dirname(p)
            if not p or p == "/":
                break
            if os.path.exists(os.path.join(p, "idVendor")):
                return _descriptor(p)
        return {}

    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                if f.read().strip() != DECXIN_VID:
                    continue
            with open(os.path.join(d, "idProduct")) as f:
                if f.read().strip() != DECXIN_PID:
                    continue
        except OSError:
            continue
        return _descriptor(d)
    return {}


def list_decxin_nodes():
    """[(dev, identity)] for every MJPEG-capable DECXIN node currently attached."""
    found = []
    for dev in sorted(glob.glob("/dev/video*"),
                      key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        ident = usb_identity(dev)
        if not ident or ident.get("usb_path") in {i["usb_path"] for _, i in found}:
            continue
        try:
            out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "MJPG" in out:
            found.append((dev, ident))
    return found


def find_capture_node(preferred=None):
    """The /dev/videoN that can actually hand us MJPEG.

    A DECXIN exposes two nodes and only the first is a capture device, so picking
    /dev/video0 blindly works until it doesn't. This asks each node whether it enumerates
    MJPG and takes the first that says yes.
    """
    if preferred:
        return preferred
    for dev in sorted(glob.glob("/dev/video*"),
                      key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        try:
            out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "MJPG" in out:
            return dev
    return None


def holder_of(dev):
    """Which process is holding the node — one process at a time can stream from it, so a
    forgotten camrig/preview window is the usual reason a capture returns nothing.

    Guard on existence first: fuser's "does not exist" complaint arrives on stderr just
    like a real holder does, so an unplugged camera was being reported as "in use".
    """
    if not shutil.which("fuser") or not os.path.exists(dev):
        return ""
    r = subprocess.run(["fuser", "-v", dev], capture_output=True, text=True)
    out = (r.stderr or r.stdout).strip()
    return "" if "does not exist" in out else out


# ---------------------------------------------------------------- capture

def capture(dev, frames, out_path, width, height, fps):
    subprocess.run(["v4l2-ctl", "-d", dev,
                    f"--set-fmt-video=width={width},height={height},pixelformat=MJPG",
                    f"--set-parm={fps}"], capture_output=True, text=True, timeout=20)
    r = subprocess.run(["v4l2-ctl", "-d", dev, "--stream-mmap",
                        f"--stream-count={frames}", f"--stream-to={out_path}"],
                       capture_output=True, text=True, timeout=max(60, frames // 2 + 30))
    return r.returncode == 0, (r.stderr or r.stdout)


# ---------------------------------------------------------------- JPEG parsing

# Markers that carry no length field and so must not be length-skipped.
STANDALONE = {0x01} | set(range(0xD0, 0xD8))


def parse_frame(f):
    """Marker inventory for one JPEG, up to and including the scan."""
    info = {"apps": [], "dri": None, "rst": 0, "sof": None, "dqt": 0, "dht": 0,
             "truncated": False}
    i = 2
    while i < len(f) - 1:
        if f[i] != 0xFF:
            info["truncated"] = True
            break
        m = f[i + 1]
        if m == 0xD9:
            break
        if m in STANDALONE or m == 0xFF:
            i += 2
            continue
        if i + 4 > len(f):
            info["truncated"] = True
            break
        seg = (f[i + 2] << 8) | f[i + 3]
        body = f[i + 4:i + 2 + seg]

        if 0xE0 <= m <= 0xEF:
            info["apps"].append({"n": m - 0xE0, "seglen": seg, "payload": body[:32]})
        elif m == 0xDD:
            info["dri"] = (body[0] << 8) | body[1] if len(body) >= 2 else 0
        elif m == 0xDB:
            info["dqt"] += 1
        elif m == 0xC4:
            info["dht"] += 1
        elif m in (0xC0, 0xC1, 0xC2):
            if len(body) >= 6:
                ncomp = body[5]
                comps = [(body[6 + c * 3], body[7 + c * 3] >> 4, body[7 + c * 3] & 0xF)
                          for c in range(ncomp) if 7 + c * 3 < len(body)]
                info["sof"] = {"prec": body[0],
                                "h": (body[1] << 8) | body[2],
                                "w": (body[3] << 8) | body[4],
                                "comps": comps}
        elif m == 0xDA:
            # Entropy data follows. A literal 0xFF in compressed data is byte-stuffed as
            # FF 00, so an FF D0-D7 pair is unambiguously a real restart marker.
            scan = f[i + 2 + seg:]
            end = scan.rfind(b"\xff\xd9")
            if end != -1:
                scan = scan[:end]
            info["rst"] = sum(scan.count(bytes([0xFF, 0xD0 + k])) for k in range(8))
            break
        i += 2 + seg
    return info


def split_frames(data):
    starts = [m.start() for m in re.finditer(b"\xff\xd8\xff", data)]
    return [(starts[k], (starts[k + 1] if k + 1 < len(starts) else len(data)))
            for k in range(len(starts))]


def software_decode_check(path):
    """What a lenient SOFTWARE decoder thinks. A camera that trips warnings here is one a
    rigid hardware parser is liable to reject outright."""
    if not shutil.which("ffmpeg"):
        return None
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "mjpeg", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    # ffmpeg collapses runs of one message into "Last message repeated N times". That
    # summary line is BOOKKEEPING, not an error: counting it as one inflated every
    # camera's error total by exactly 1 and made both a known-good and a known-bad unit
    # read as "real bitstream corruption". Attribute the repeats to whatever message
    # preceded them and drop the summary line itself.
    lines, repeated, prev = [], 0, ""
    for raw in r.stderr.splitlines():
        l = raw.strip()
        if not l:
            continue
        m = re.search(r"[Ll]ast message repeated (\d+) times", l)
        if m:
            repeated += int(m.group(1))
            if "APP fields" in prev:
                lines.extend([prev] * int(m.group(1)))
            continue
        lines.append(l)
        prev = l
    app_warn = sum(1 for l in lines if "APP fields" in l)
    other = sum(1 for l in lines if "APP fields" not in l)
    return {"app_field_errors": app_warn, "other_errors": other,
            "total_lines": len(lines), "repeated": repeated}


# ---------------------------------------------------------------- report

def analyse(data):
    spans = split_frames(data)
    if not spans:
        return None
    frames = [parse_frame(data[a:b]) for a, b in spans]
    sizes = [b - a for a, b in spans]

    stub = []
    for fr in frames:
        bad = False
        for a in fr["apps"]:
            if a["n"] != 1:
                continue
            if a["seglen"] <= 4 or not any(a["payload"].startswith(k) for k in KNOWN_APP1):
                bad = True
        stub.append(bad)

    has_app1 = [any(a["n"] == 1 for a in fr["apps"]) for fr in frames]
    app1_ex = next((a for fr in frames for a in fr["apps"] if a["n"] == 1), None)
    dris = [fr["dri"] for fr in frames if fr["dri"] is not None]
    rsts = [fr["rst"] for fr in frames]

    return {
        "n_frames": len(frames),
        "stub_app1_frames": sum(stub),
        "app1_frames": sum(has_app1),
        "app1_example": app1_ex,
        "dri": statistics.mode(dris) if dris else None,
        "dri_frames": len(dris),
        "rst_median": int(statistics.median(rsts)) if rsts else 0,
        "sizes": sizes,
        "sof": next((fr["sof"] for fr in frames if fr["sof"]), None),
        "truncated": sum(1 for fr in frames if fr["truncated"]),
        "app_markers": sorted({a["n"] for fr in frames for a in fr["apps"]}),
    }


def sampling_name(comps):
    if not comps:
        return "?"
    hv = [(h, v) for _, h, v in comps]
    return {((2, 2), (1, 1), (1, 1)): "4:2:0",
            ((2, 1), (1, 1), (1, 1)): "4:2:2",
            ((1, 1), (1, 1), (1, 1)): "4:4:4"}.get(tuple(hv), "-".join(f"{h}x{v}" for h, v in hv))


def report(ident, a, decode, dev):
    def hdr(t):
        print(f"\n{C_BLD}{t}{C_OFF}\n" + "-" * len(t))

    hdr("CAMERA")
    cid = ident.get("camera_id", "?")
    rev = ident.get("revised")
    print(f"  product        {ident.get('product','?')}")
    print(f"  camera_id      {cid}" + (
        f"   {C_YEL}<-- .N revision: flash written a SECOND time{C_OFF}" if rev
        else f"   {C_DIM}(bare — flash written once){C_OFF}"))
    print(f"  firmware       bcdDevice {ident.get('bcd_device','?')}   serial {ident.get('serial','?')}")
    print(f"  link           {ident.get('link_speed_mbps','?')} Mbps   {ident.get('max_power','?')}   node {dev}")

    hdr("BITSTREAM")
    sof = a["sof"] or {}
    print(f"  geometry       {sof.get('w','?')}x{sof.get('h','?')}  "
          f"{sampling_name(sof.get('comps'))}  {a['n_frames']} frames analysed")
    print(f"  APP markers    {', '.join('APP%d' % n for n in a['app_markers']) or 'none'}")
    sz = sorted(a["sizes"])
    p = lambda q: sz[min(len(sz) - 1, q * len(sz) // 100)] / 1024
    print(f"  frame size     p10 {p(10):.0f} KB   median {p(50):.0f} KB   p90 {p(90):.0f} KB   "
          f"max {sz[-1]/1024:.0f} KB")
    if a["truncated"]:
        print(f"  {C_YEL}truncated      {a['truncated']} frame(s) failed to parse cleanly{C_OFF}")

    # ---- Deviation from the measured DECXIN baseline -------------------------
    # MEASURED 2026-08-26 on three units: 77.1 (loses ~40% of frames on two separate
    # R76S boards), 170 (records 0 dropped) and 190. All three are byte-identical here:
    # a stub APP1 (seglen=4, payload 00 00) on 100% of frames, DRI=250, ~149 RST/frame,
    # 4:2:2 at 4000x1200. ffmpeg warns once per frame on every one of them and decodes
    # them all anyway.
    #
    # So these traits are the DECXIN BASELINE, not a defect, and an earlier version of
    # this script was wrong to call them one — it returned SUSPECT for every camera
    # including the known-good unit. A fingerprint that flags 100% of the fleet carries
    # no information. What is reported now is DEVIATION from the baseline; the marker
    # structure demonstrably does NOT separate a good camera from a bad one, so a clean
    # result here does NOT clear a unit for field use.
    hdr("BASELINE COMPARISON")
    BASE = {"stub_app1_pct": 100, "dri": 250, "rst": 149, "sampling": "4:2:2"}
    dev_notes, findings = [], []

    stub_pct = round(100 * a["stub_app1_frames"] / a["n_frames"])
    def cmp_line(label, got, exp, tol=0):
        same = abs(got - exp) <= tol if isinstance(got, (int, float)) else got == exp
        tag = f"{C_GRN}[baseline]{C_OFF}" if same else f"{C_YEL}[DEVIATES]{C_OFF}"
        print(f"  {tag} {label:<22} {got}" + ("" if same else f"   {C_YEL}(fleet: {exp}){C_OFF}"))
        if not same:
            dev_notes.append(f"{label}={got} vs {exp}")
        return same

    cmp_line("stub APP1 frames", f"{stub_pct}%", f"{BASE['stub_app1_pct']}%")
    cmp_line("DRI interval", a["dri"] or 0, BASE["dri"])
    cmp_line("RST per frame", a["rst_median"], BASE["rst"], tol=8)
    cmp_line("sampling", sampling_name((a["sof"] or {}).get("comps")), BASE["sampling"])
    if a["truncated"]:
        print(f"  {C_RED}[DEVIATES]{C_OFF} unparseable frames    {a['truncated']}"
              f"   {C_YEL}(fleet: 0){C_OFF}")
        dev_notes.append(f"truncated={a['truncated']}")
        findings.append("truncated")

    if decode:
        n, tot = decode["app_field_errors"], a["n_frames"]
        extra = decode["other_errors"]
        print(f"  {C_GRN}[baseline]{C_OFF} sw APP warnings        {n}/{tot}"
              f"   {C_DIM}(1/frame is normal for DECXIN){C_OFF}")
        if extra > 0:
            print(f"  {C_RED}[DEVIATES]{C_OFF} other sw decode errors {extra}"
                  f"   {C_YEL}(fleet: 0) — real bitstream corruption{C_OFF}")
            dev_notes.append(f"decode_errors={extra}")
            findings.append("decode_errors")

    print(f"\n  {C_DIM}size band p10-p90 {p(90)-p(10):.0f} KB, median {p(50):.0f} KB — "
          f"scene-dependent, NOT comparable across sessions.{C_OFF}")
    print(f"  {C_DIM}77.1 (bad) ran the SMALLEST frames of the three measured, so frame "
          f"size does not predict failure.{C_OFF}")

    hdr("VERDICT")
    if findings:
        print(f"  {C_RED}{C_BLD}DEVIATES FROM FLEET{C_OFF} — {', '.join(dev_notes)}")
        print(f"  {C_DIM}Real bitstream damage. Worth chasing.{C_OFF}")
    elif dev_notes:
        print(f"  {C_YEL}{C_BLD}MINOR DEVIATION{C_OFF} — {', '.join(dev_notes)}")
    else:
        print(f"  {C_GRN}{C_BLD}MATCHES FLEET BASELINE{C_OFF}")
        print(f"  {C_YEL}This does NOT clear the unit.{C_OFF} Camera 77.1 matches this "
              f"baseline exactly and still")
        print(f"  loses ~40% of its frames on an R76S. Whatever separates a good unit "
              f"from a bad one is")
        print(f"  {C_BLD}not visible in the marker structure{C_OFF} — confirm on a real "
              f"board before shipping.")
    return findings


def append_csv(ident, a, decode, findings):
    row = {
        "camera_id": ident.get("camera_id", ""),
        "product": ident.get("product", ""),
        "revised": int(bool(ident.get("revised"))),
        "bcd_device": ident.get("bcd_device", ""),
        "frames": a["n_frames"],
        "stub_app1_frames": a["stub_app1_frames"],
        "app1_frames": a["app1_frames"],
        "dri": a["dri"] or "",
        "rst_per_frame": a["rst_median"],
        "median_kb": round(sorted(a["sizes"])[len(a["sizes"]) // 2] / 1024, 1),
        "sw_app_warnings": (decode or {}).get("app_field_errors", ""),
        "sw_other_errors": (decode or {}).get("other_errors", ""),
        # p10-p90 spread: the ONE number that differed between the measured units
        # (77.1 = 2 KB, 170 = 72 KB, 190 = 87 KB). Confounded by scene content, so it is
        # recorded rather than judged — compare only captures of the SAME scene.
        "size_spread_kb": round((lambda s: s[min(len(s)-1, 90*len(s)//100)]
                                  - s[min(len(s)-1, 10*len(s)//100)])(sorted(a["sizes"])) / 1024, 1),
        "verdict": "DEVIATES" if findings else "BASELINE",
    }
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"\n{C_DIM}appended to {CSV_PATH}{C_OFF}")


def main():
    ap = argparse.ArgumentParser(description="Fingerprint a DECXIN's JPEG bitstream.")
    ap.add_argument("--device")
    ap.add_argument("--frames", type=int, default=90,
                    help="frames to capture (default 90; ~1.6s of ramp is normal)")
    ap.add_argument("--width", type=int, default=4000)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--file", help="analyse an existing .mjpg instead of capturing")
    ap.add_argument("--all", action="store_true",
                    help="fingerprint every DECXIN currently docked, one after another")
    ap.add_argument("--keep", action="store_true", help="keep the capture for diffing")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args()

    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f"no such file: {args.file}")
        data = open(args.file, "rb").read()
        a = analyse(data)
        if not a:
            sys.exit("no JPEG frames found in the capture")
        ident = {"product": f"(from file {os.path.basename(args.file)})", "camera_id": ""}
        findings = report(ident, a, software_decode_check(args.file), "-")
        if not args.no_csv:
            append_csv(ident, a, software_decode_check(args.file), findings)
        return

    if not shutil.which("v4l2-ctl"):
        sys.exit("v4l2-ctl not found — install v4l-utils")

    if args.device:
        targets = [(args.device, usb_identity(args.device))]
    else:
        docked = list_decxin_nodes()
        if not docked:
            dev = find_capture_node()
            if not dev:
                sys.exit("no MJPEG-capable /dev/video* found — is the camera plugged in?")
            targets = [(dev, {})]
        elif args.all or len(docked) == 1:
            targets = docked
        else:
            print(f"{C_YEL}{len(docked)} DECXINs docked:{C_OFF}")
            for d, i in docked:
                print(f"    {d}  {i.get('product','?')}")
            print(f"{C_DIM}fingerprinting all of them; use --device to pick one{C_OFF}")
            targets = docked

    for n, (dev, ident) in enumerate(targets):
        if len(targets) > 1:
            print(f"\n{C_BLD}{'='*68}\n== [{n+1}/{len(targets)}] "
                  f"{ident.get('product','?')}  ({dev})\n{'='*68}{C_OFF}")
        if not os.path.exists(dev):
            print(f"{C_RED}{dev} does not exist — camera unplugged?{C_OFF}")
            continue
        held = holder_of(dev)
        if held:
            print(f"{C_YEL}note: {dev} is in use — close it if the capture stalls{C_OFF}")
            print(f"{C_DIM}{held}{C_OFF}")

        tmp = tempfile.NamedTemporaryFile(prefix="camfp_", suffix=".mjpg", delete=False)
        tmp.close()
        path = tmp.name
        print(f"capturing {args.frames} frames from {dev} "
              f"({args.width}x{args.height}@{args.fps})…")
        ok, err = capture(dev, args.frames, path, args.width, args.height, args.fps)
        data = open(path, "rb").read()
        if not data:
            print(f"{C_RED}captured nothing from {dev}{C_OFF}\n{err.strip()}")
            os.unlink(path)
            continue
        if not ok:
            print(f"{C_YEL}v4l2-ctl exited non-zero; analysing what arrived{C_OFF}")

        a = analyse(data)
        if not a:
            print(f"{C_RED}no JPEG frames found in the capture{C_OFF}")
            os.unlink(path)
            continue
        decode = software_decode_check(path)
        findings = report(ident, a, decode, dev)
        if not args.no_csv:
            append_csv(ident, a, decode, findings)

        if args.keep:
            dest = os.path.join(HERE, f"cam_{ident.get('camera_id','unknown')}.mjpg")
            shutil.move(path, dest)
            print(f"{C_DIM}capture kept at {dest}{C_OFF}")
        else:
            os.unlink(path)


if __name__ == "__main__":
    main()
