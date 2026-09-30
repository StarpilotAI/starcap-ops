#!/usr/bin/env python3
"""Firmware jitter bench — does THIS firmware produce the frame-timing fault?

The fault: two frames arrive at the sensor's readout floor (9984 us, zero
exposure wait) and one long frame puts the stream back on the nominal beat. It is
triggered by the camera's auto-exposure loop reacting to changing light, and
whether that trigger does damage is decided by the CAMERA FIRMWARE - proven by
re-flashing one unit and watching its whole signature flip.

Each run is two captures on one camera:

    CONTROL   ~30 s, screen held at a steady grey   (AE idle)
    RAMP      ~75 s, screen ramped black<->white    (AE forced to work)

and the verdict is the jitter rate in each, plus the p99 frame interval against
the customer line: 99th percentile within +/-10 ms of nominal.

    ./fwbench.py run --camera 4.1 --fw 114 --reps 2
    ./fwbench.py status
    ./fwbench.py report -o ~/Desktop/firmware_jitter_report.md

Nothing is inferred from a filename or a firmware number. The firmware FAMILY is read back
out of the camera's own frame timing, and the run is flagged if that disagrees
with the --fw number you gave it: that is how a flash that silently did not take
gets caught before it costs you a whole afternoon.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from imu_strip import StripDecoder                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "fwbench", "results")
LIGHTRAMP = os.path.join(HERE, "lightramp.py")
W, H, STRIP = 4000, 1200, 160
# Crop a little scene ALONGSIDE the strip: columns 0-160 carry the timestamps,
# 160-PROBE_W are real image. That makes "did the light ramp actually reach this
# camera?" a measurement rather than an assumption, for ~60% more bandwidth on an
# already tiny crop. Checking the camera's exposure instead does NOT work - on the
# failing firmware AE corrects with GAIN and the exposure never moves at all.
PROBE_W = 256
SPEC_MS = 10.0
EARLY_MARGIN_US = 3000
CONTROL_S, RAMP_S = 30, 78

# The two builds seen so far, by the only thing that reliably tells them apart.
# A unit's nominal frame interval is a property of its firmware: one build holds
# a single value, the other dithers between two.
FAMILIES = {16640: "static-16640", 16626: "dither-16626", 16627: "dither-16626"}


def family_of(sc):
    """Name the build from the measured frame timing, never from a filename."""
    base = FAMILIES.get(sc["nominal_us"])
    if not base:
        return f"unknown-{sc['nominal_us']}"
    return base


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, **kw)


# ---------------------------------------------------------------- capture
def _ramp_env():
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":1")
    return env


def capture(dev, seconds, out, ramp=False):
    """Pull the strip + a scene probe for `seconds`. Raises if the capture is
    short, because a truncated RAMP is the dangerous direction: lightramp holds a
    steady grey for its first 15 s, so a capture cut off early contains almost no
    stimulus, ramp jitter collapses to control, and the run reads as a confident
    PASS attributable to the firmware."""
    dec = StripDecoder(W)
    rows = dec.rows_needed()
    n = int(seconds * 61)
    cmd = ["ffmpeg", "-v", "error", "-f", "v4l2", "-input_format", "mjpeg",
           "-video_size", f"{W}x{H}", "-i", dev, "-frames:v", str(n),
           "-vf", f"crop={PROBE_W}:{rows}:0:0", "-pix_fmt", "nv12",
           "-f", "rawvideo", "-y", out]
    hold = None
    try:
        if not ramp:
            # The control needs a KNOWN steady screen, not whatever the desktop
            # happens to show. It must be up and SETTLED before the capture opens:
            # letting the desktop->grey step land inside the capture puts a large
            # brightness transient in the baseline, which is itself a stimulus.
            # Measured: starting it late took the control from 0.03 to 0.17
            # events/s and its luma swing from 0.9 to 17.6.
            hold = subprocess.Popen([sys.executable, LIGHTRAMP, "--hold", "128"],
                                    env=_ramp_env(), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            time.sleep(7.0)                  # window up, AND the camera's AE settled on it
            if hold.poll() is not None:
                raise RuntimeError("could not put the steady grey on screen "
                                   "— check DISPLAY")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        if ramp:
            time.sleep(3)
            # A failed ramp returns in milliseconds (no DISPLAY under ssh/tmux),
            # after which the capture runs on against a static screen and reads as
            # a PASS. The return code is the only cheap way to see that.
            r = subprocess.run([sys.executable, LIGHTRAMP], env=_ramp_env(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                proc.kill()
                raise RuntimeError("light ramp failed to start "
                                   f"({(r.stderr or b'').decode()[:160].strip()}) "
                                   "— check DISPLAY")
        try:
            rc = proc.wait(timeout=seconds * 3 + 60)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
            raise RuntimeError("ffmpeg hung — it would hold the camera for the "
                               "rest of the session, so it was killed")
    finally:
        if hold is not None:
            hold.terminate()
            try: hold.wait(timeout=5)
            except Exception: hold.kill()                    # noqa: BLE001
    got = os.path.getsize(out) // (PROBE_W * rows * 3 // 2) if os.path.exists(out) else 0
    if rc != 0 or got < 0.97 * n:
        raise RuntimeError(f"capture incomplete: ffmpeg rc={rc}, {got}/{n} frames")
    return rows


def decode(path, rows):
    """-> (exposure_start_us array, exposure_us list, duplicate count, luma list)."""
    fsz = PROBE_W * rows * 3 // 2
    dec = StripDecoder(W)
    raw = np.memmap(path, dtype=np.uint8, mode="r")
    starts, expos, luma = [], [], []
    for i in range(len(raw) // fsz):
        y = np.asarray(raw[i * fsz:i * fsz + PROBE_W * rows]).reshape(rows, PROBE_W)
        # Compact the strip columns before decoding, exactly as the recorder does.
        # decode() returns bare None when the bit-error filter rejects a frame —
        # MJPEG is lossy and cells near the threshold do flip — and unpacking that
        # would kill the run after all the physical work was already done.
        res = dec.decode(np.ascontiguousarray(y[:, :STRIP]).reshape(-1), STRIP)
        if not res:
            continue
        e, _ = res
        if e:
            starts.append(e[0])
            expos.append((e[1] - e[0]) & 0xFFFFFFFF)
            luma.append(float(y[:, STRIP:].mean()))
    es = np.array(starts, dtype=np.int64)
    if len(es) < 2:
        return es, expos, 0, luma
    # exposure_start_us is a u32 counting from plug-in, so it wraps every 71.6
    # minutes — well inside a session that measures one camera across a reflash.
    # Unwrapped, the wrap frame yields a hugely negative interval, which the
    # dedup and the >0 filter quietly swallow, leaving a NEGATIVE duration and a
    # negative jitter rate that the report prints and averages without complaint.
    es = es + (np.cumsum(np.concatenate(
        ([0], (np.diff(es) < -(1 << 31)).astype(np.int64)))) << 32)
    dup = int((np.diff(es) == 0).sum())
    # A duplicate stamp is a ZERO interval, which reads as a frame that arrived
    # infinitely early - i.e. it manufactures the exact fault being measured.
    # ~80 of them follow every device open. Drop them, report the count.
    keep = np.concatenate(([True], np.diff(es) > 0))
    # expos/luma stay index-aligned with es - they are per-frame, so they must be
    # filtered by the SAME mask or every later lookup is off by the dropped count.
    expos = [e for e, k in zip(expos, keep) if k]
    luma = [l for l, k in zip(luma, keep) if k]
    return es[keep], expos, dup, luma


def _slow_range(luma, win=30):
    """Peak-to-peak of luma after a ~0.5 s moving average (60 fps)."""
    a = np.asarray(luma, dtype=float)
    if len(a) < win * 2:
        return float(a.max() - a.min()) if len(a) else 0.0
    k = np.ones(win) / win
    sm = np.convolve(a, k, mode="valid")
    return float(sm.max() - sm.min())


def score(es, expos, dup, luma):
    d = np.diff(es)
    d = d[d > 0]
    g = d[(d > 1000) & (d < 100000)]
    if len(g) < 60:
        return {"error": f"only {len(g)} usable intervals"}
    mode = int(np.bincount(g).argmax())
    if not 15000 <= mode <= 18000:
        return {"error": f"nominal came out {mode} us — not a 60 fps stream; "
                         f"faults may outnumber good frames"}
    lo, hi = mode - SPEC_MS * 1000, mode + SPEC_MS * 1000
    # The nominal is a SET, not one number: one firmware build holds a single
    # frame length, the other DITHERS between two. Matching against mode+/-1 calls
    # every dithered frame an anomaly, which both invents drops and reports a
    # dithering build as static — and the interval column is the very thing used
    # to prove the two builds differ.
    counts = Counter(g.tolist())
    floor = 0.01 * len(g)
    nominal_set = {v for v, n in counts.items()
                   if n >= floor and abs(v - mode) <= 4} or {mode}
    ok = np.isin(d, list(nominal_set))
    ev, i = [], 0
    while i < len(d):
        if not ok[i]:
            j = i
            while j < len(d) and not ok[j]:
                j += 1
            ev.append(d[i:j]); i = j
        else:
            i += 1
    jit = [s for s in ev if any(x < mode - EARLY_MARGIN_US for x in s)]
    dur = float((es[-1] - es[0]) / 1e6)
    p99 = float(np.percentile(d, 99))
    ivs = Counter(g[np.isin(g, list(nominal_set))].tolist())
    return {
        "frames": int(len(es)), "duration_s": round(dur, 1),
        "nominal_us": mode, "fps": round(1e6 / mode, 3),
        "nominal_set": sorted(int(v) for v in nominal_set),
        "interval_values": {str(k): int(v) for k, v in ivs.most_common(4)},
        "exposure_values": {str(k): int(v) for k, v in Counter(expos).most_common(4)},
        "jitter": len(jit), "jitter_per_s": round(len(jit) / dur, 3) if dur else 0.0,
        "drops": len(ev) - len(jit),
        "p99_us": round(p99), "spec_lo": round(lo), "spec_hi": round(hi),
        "pass": bool(lo <= p99 <= hi),
        "oob_pct": round(100.0 * float(((d > hi) | (d < lo)).sum()) / len(d), 3),
        # An absolute count, not a fraction: the duplicate burst after a device
        # open is a fixed ~81 frames, so a fraction makes the 30 s control fail
        # and the 78 s ramp pass on the same open, for no physical reason.
        "dup_stamps": dup, "trustworthy": bool(dup <= 250),
        "luma_min": round(float(np.min(luma)), 1) if luma else None,
        "luma_max": round(float(np.max(luma)), 1) if luma else None,
        "luma_range": round(float(np.max(luma) - np.min(luma)), 1) if luma else None,
        # The SLOW swing is what says a light ramp happened. Raw frame-to-frame
        # range does not: an exposure unaligned to the mains half-period aliases
        # 100 Hz room ripple into per-frame luma, which put a dead-static control
        # screen at a range of 16 against the ramp's 34 — close enough to make a
        # "did the stimulus land?" test on raw range unreliable. A half-second
        # moving average removes the ripple and keeps the ramp.
        "luma_slow_range": round(float(_slow_range(luma)), 1) if luma else None,
        "examples": [[int(x) for x in s] for s in jit[:4]],
    }


# ---------------------------------------------------------------- preflight
def preflight(dev):
    """Refuse to burn 2 minutes on a camera that cannot deliver a clean frame."""
    if not os.path.exists(dev):
        return f"{dev} does not exist"
    # 300 frames, and the LAST reading. v4l2-ctl reports every 30 frames and a
    # freshly opened DECXIN climbs from a slow start (measured: 2.8 -> 20 -> 33 ->
    # 40 -> 44 -> 60.2 on one open), so a short capture reports a rate the camera
    # is not actually running at and would refuse a perfectly healthy unit.
    # Measure the format the experiment uses. Without this v4l2-ctl streams
    # whatever G_FMT happens to be (often 640x480 YUYV on a fresh plug), and a
    # camera that sails the gate at low res can still be marginal at 4000x1200 —
    # which is the exact case the gate exists to catch, since low fps here is a
    # bytes/sec ceiling.
    sh("v4l2-ctl", "-d", dev,
       "--set-fmt-video=width=4000,height=1200,pixelformat=MJPG")
    r = sh("timeout", "45", "v4l2-ctl", "-d", dev,
           "--stream-mmap", "--stream-count=300", "--stream-to=/dev/null")
    out = (r.stdout or b"").decode() + (r.stderr or b"").decode()
    fps = [float(x) for x in __import__("re").findall(r"([\d.]+) fps", out)]
    if not fps:
        return ("camera enumerates but delivered no frames. This is the wedged "
                "state - unplug it, wait 5 s, plug it back in.")
    best = fps[-1]
    if best < 55:
        return (f"only {best:.1f} fps - not a 60 fps stream. Check nothing else "
                f"is on the same USB controller (a phone will do it), and that "
                f"the firmware flash completed.")
    return None


def run_once(dev, camera, fw, rep, keep):
    tag = f"cam{camera}_fw{fw}_rep{rep}"
    print(f"\n=== {tag} ===")
    err = preflight(dev)
    if err:
        print(f"  ABORT: {err}")
        return None
    tmp = os.environ.get("TMPDIR", "/tmp")
    cpath = os.path.join(tmp, f"{tag}_ctrl.raw")
    rpath = os.path.join(tmp, f"{tag}_ramp.raw")

    try:
        print(f"  control: {CONTROL_S}s, screen held at steady grey")
        rows = capture(dev, CONTROL_S, cpath)
        c = score(*decode(cpath, rows))
        if "error" in c:
            print(f"  ABORT (control): {c['error']}")
            return None                       # don't spend 78 s after a bad control

        print(f"  ramp:    {RAMP_S}s, SCREEN WILL FLASH black/white")
        rows = capture(dev, RAMP_S, rpath, ramp=True)
        r = score(*decode(rpath, rows))
        if "error" in r:
            print(f"  ABORT (ramp): {r['error']}")
            return None
    except RuntimeError as e:
        print(f"  ABORT: {e}")
        return None
    finally:
        # Cleanup last: on an error the raw capture is the only evidence of two
        # minutes of physical work.
        if keep:
            print(f"  raw kept: {cpath} {rpath}")
    if not keep:
        for p in (cpath, rpath):
            try: os.remove(p)
            except OSError: pass

    fam = family_of(r)
    # The stimulus is proven to have landed only if the SCENE the camera saw
    # actually swung during the ramp, and swung much harder than while the screen
    # was held steady. Without this, a camera pointed away from the screen, a
    # blanked display or a sleeping monitor all look exactly like "this firmware
    # is fine" - a false PASS, which is the expensive direction to be wrong in.
    # A real black<->white ramp moves the scene a LOT: measured across 16 runs on
    # four cameras the ramp's slow swing was 39-82, while a camera that could not
    # see the screen at all read 0.8. So the absolute size is the real evidence,
    # and the ratio is only a secondary guard.
    #
    # The ratio alone was too strict and was rejecting good runs: the CONTROL's own
    # slow drift varied 4.4-19.6 between runs (AE still settling, room light), so a
    # run with a 55-unit ramp swing could still fail a 3x test purely because its
    # control happened to drift. Three of sixteen runs were flagged that way, all
    # with ramp swings of 39-56.
    rs, cs = r["luma_slow_range"], c["luma_slow_range"]
    stim = bool(rs is not None and cs is not None
                and rs >= 20.0 and rs > 1.8 * max(cs, 0.5))
    rec = {
        "tag": tag, "camera": camera, "firmware_label": fw, "rep": rep,
        "device": dev, "when": datetime.now().isoformat(timespec="seconds"),
        "family": fam, "stimulus_reached_camera": bool(stim),
        "control": c, "ramp": r,
    }
    os.makedirs(RESULTS, exist_ok=True)
    # Timestamped: re-running a camera+fw+rep must never destroy the earlier
    # measurement, and mistyping --fw must not overwrite the other build's data
    # under this build's numbers.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(RESULTS, f"{tag}_{stamp}.json"), "w") as f:
        json.dump(rec, f, indent=1)

    print(f"  family={fam}  control={c['jitter_per_s']}/s  ramp={r['jitter_per_s']}/s  "
          f"p99={r['p99_us']}us {'PASS' if r['pass'] else 'FAIL'}")
    print(f"  scene luma swing (slow): control {c['luma_slow_range']}  "
          f"ramp {r['luma_slow_range']}")
    if not stim:
        print("  !! the scene barely changed during the ramp — is the camera "
              "pointed at the screen? This run cannot support a PASS.")
    if not r["trustworthy"]:
        print(f"  !! {r['dup_stamps']} duplicate timestamps — numbers not trustworthy.")
    return rec


def cmd_run(a):
    dev = a.device
    if not dev:
        import camselect
        dev = camselect.choose(require_stereo=True)
        if not dev:
            sys.exit("no camera")
    seen = []
    for rep in range(1, a.reps + 1):
        rec = run_once(dev, a.camera, a.fw, rep, a.keep_raw)
        if not rec:
            continue
        seen.append(rec)
        # Does the hardware agree with the label you typed? Every other camera
        # already measured under this label voted on what this build looks like.
        # A flash that silently did not take is otherwise invisible: both reps
        # agree with each other perfectly, and the mislabelled numbers go on to
        # dilute or invert the headline claim.
        prior = {p["family"] for p in load_all()
                 if str(p["firmware_label"]) == str(a.fw)
                 and p["tag"] != rec["tag"]}
        if prior and rec["family"] not in prior:
            print(f"  !! STOP: --fw {a.fw} has measured as {prior} before, but this "
                  f"camera reads {rec['family']}. The flash may not have taken.")
    fams = {r["family"] for r in seen}
    if len(fams) > 1:
        print(f"\n!! reps disagree on firmware family: {fams} — re-run.")
    print(f"\n{len(seen)}/{a.reps} runs stored in {RESULTS}")


def load_all():
    out = []
    if os.path.isdir(RESULTS):
        for f in sorted(os.listdir(RESULTS)):
            if f.endswith(".json"):
                try: out.append(json.load(open(os.path.join(RESULTS, f))))
                except Exception: pass                        # noqa: BLE001
    return out


def cmd_probe(a):
    """Aim check, before a 40-minute session rather than after it.

    The scene probe is a narrow band at the left edge of the left eye, so a camera
    aimed slightly off flags every single run. Ten seconds here is cheaper than
    discovering it twenty runs in."""
    dev = a.device or "/dev/video0"
    err = preflight(dev)
    if err:
        sys.exit(f"camera not ready: {err}")
    tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fwbench_probe.raw")
    print("watch the screen — it will flash for ~12 s")
    try:
        rows = capture(dev, 12, tmp, ramp=True)
    except RuntimeError as e:
        sys.exit(str(e))
    es, expos, dup, luma = decode(tmp, rows)
    try: os.remove(tmp)
    except OSError: pass
    if not luma:
        sys.exit("strip never decoded — is this the right camera?")
    swing = _slow_range(luma)
    print(f"  scene luma {min(luma):.1f}–{max(luma):.1f}  "
          f"(slow swing {swing:.1f}, raw {max(luma)-min(luma):.1f})")
    print(f"  strip decoded on {len(es)} frames, {dup} duplicate stamps")
    if swing < 8:
        print("\n  TOO LOW. The screen is not reaching the camera's probe band.")
        print("  Aim the camera so the laptop screen fills more of the frame,")
        print("  especially toward the LEFT of the left eye, and re-probe.")
        sys.exit(1)
    print("\n  Good — the stimulus reaches this camera. Safe to start the session.")


def cmd_status(_a):
    recs = load_all()
    if not recs:
        print(f"nothing yet in {RESULTS}"); return
    print(f"{'run':30} {'family':16} {'ctrl/s':>7} {'ramp/s':>7} {'p99':>7}  verdict")
    for r in recs:
        print(f"{r['tag']:30} {r['family']:16} {r['control']['jitter_per_s']:7.2f} "
              f"{r['ramp']['jitter_per_s']:7.2f} {r['ramp']['p99_us']:7d}  "
              f"{'PASS' if r['ramp']['pass'] else 'FAIL'}"
              f"{'' if r['stimulus_reached_camera'] else '   [stimulus?]'}")
    cams = sorted({r["camera"] for r in recs})
    fws = sorted({str(r["firmware_label"]) for r in recs})
    print(f"\n{len(recs)} runs · {len(cams)} cameras {cams} · firmwares {fws}")
    for c in cams:
        for fw in fws:
            n = sum(1 for r in recs if r["camera"] == c and str(r["firmware_label"]) == fw)
            if n < 2:
                print(f"  missing: camera {c} fw {fw} has {n}/2 reps")


def cmd_report(a):
    recs = load_all()
    if not recs:
        sys.exit("no runs to report")
    fws = sorted({str(r["firmware_label"]) for r in recs})
    cams = sorted({str(r["camera"]) for r in recs})
    L = []
    L.append("# Frame-timing jitter vs camera firmware")
    L.append(f"\n_{datetime.now():%Y-%m-%d}_ · {len(recs)} runs · "
             f"{len(cams)} cameras · firmware builds {', '.join(fws)}\n")
    L.append("## What was measured\n")
    L.append("The fault is a frame-timing glitch: two frames arrive at the sensor's")
    L.append("readout floor (9984 us, no exposure wait) and one long frame puts the")
    L.append("stream back on the nominal beat. Only the long catch-up frame breaks the")
    L.append("customer's timing line — *99th percentile frame-to-frame interval within")
    L.append("±10 ms of nominal* — so each event costs exactly one out-of-spec interval.\n")
    L.append("It is triggered by the camera's own auto-exposure loop reacting to changing")
    L.append("light. Each camera was therefore measured twice:\n")
    L.append("| phase | duration | what the screen does | what AE does |")
    L.append("|---|---|---|---|")
    L.append(f"| control | {CONTROL_S} s | held steady | idle |")
    L.append(f"| ramp | {RAMP_S} s | ramped black↔white | forced to correct continuously |\n")
    L.append("Timestamps are the camera's own, decoded from the IMU strip it embeds in")
    L.append("every frame — not host arrival times — so the numbers describe the sensor,")
    L.append("not the PC or the recorder.\n")
    L.append("## Results\n")
    L.append("| camera | firmware number | measured build | rep | frame interval | "
             "control | ramp | p99 | verdict |")
    L.append("|---|---|---|---:|---|---:|---:|---:|---:|")
    for r in recs:
        iv = " / ".join(str(v) for v in r["ramp"].get("nominal_set")
                        or list(r["ramp"]["interval_values"])[:2])
        flag = "" if r["stimulus_reached_camera"] and r["ramp"]["trustworthy"] else " ⚠"
        L.append(f"| {r['camera']}{flag} | {r['firmware_label']} | `{r['family']}` | "
                 f"{r['rep']} | {iv} µs | "
                 f"{r['control']['jitter_per_s']:.2f}/s | **{r['ramp']['jitter_per_s']:.2f}/s** | "
                 f"{r['ramp']['p99_us']} µs | {'PASS' if r['ramp']['pass'] else '**FAIL**'} |")
    L.append("\n`measured build` is read back from the camera's own frame timing, "
             "not from the flashed filename — so a flash that did not take is visible "
             "here as a firmware number that disagrees with its neighbours. ⚠ marks a run "
             "excluded from the summary below.")
    # Runs the report itself calls untrustworthy must not also be the runs that
    # decide its conclusion.
    usable = [r for r in recs
              if r["stimulus_reached_camera"] and r["ramp"]["trustworthy"]]
    L.append("\n## Summary by firmware\n")
    L.append(f"_Computed over the {len(usable)} of {len(recs)} runs that passed both "
             f"validity checks (the light ramp demonstrably reached the camera, and "
             f"the timestamps were clean)._\n")
    L.append("| firmware number | measured build | runs | cameras | mean ramp jitter | runs failing p99 |")
    L.append("|---|---|---:|---:|---:|---:|")
    for fw in fws:
        sub = [r for r in usable if str(r["firmware_label"]) == fw]
        if not sub:
            L.append(f"| {fw} | — | 0 | 0 | — | no usable runs |")
            continue
        mean = sum(r["ramp"]["jitter_per_s"] for r in sub) / len(sub)
        fail = sum(1 for r in sub if not r["ramp"]["pass"])
        builds = ", ".join(sorted({r["family"] for r in sub}))
        L.append(f"| {fw} | `{builds}` | {len(sub)} | {len({r['camera'] for r in sub})} | "
                 f"{mean:.2f}/s | {fail}/{len(sub)} |")
    bad = [r for r in usable if not r["ramp"]["pass"]]
    good = [r for r in usable if r["ramp"]["pass"]]
    L.append("\n## Reading\n")
    # A label counts as failing only if EVERY usable run under it failed. Judging
    # per-rep puts a label with one disagreeing rep on both sides of the sentence.
    verdict = {}
    for fw in fws:
        sub = [r for r in usable if str(r["firmware_label"]) == fw]
        if sub:
            verdict[fw] = ("fails" if all(not r["ramp"]["pass"] for r in sub)
                           else "passes" if all(r["ramp"]["pass"] for r in sub)
                           else "mixed")
    bfw = [k for k, v in verdict.items() if v == "fails"]
    gfw = [k for k, v in verdict.items() if v == "passes"]
    mixed = [k for k, v in verdict.items() if v == "mixed"]
    if bfw and gfw:
        shared = sorted({r["camera"] for r in usable
                         if str(r["firmware_label"]) in bfw}
                        & {r["camera"] for r in usable
                           if str(r["firmware_label"]) in gfw})
        L.append(f"Firmware {', '.join(bfw)} fails the timing line. "
                 f"Firmware {', '.join(gfw)} passes it.")
        if shared:
            L.append(f"\nBoth were measured on the same physical cameras "
                     f"({', '.join(shared)}), on the same rig, with the same stimulus.")
            L.append("Nothing changed between those runs except the firmware, so the")
            L.append("firmware is the cause — not the recorder, the board, the operator")
            L.append("or the lighting.")
        else:
            L.append("\nNOTE: no camera was measured on BOTH builds, so camera-to-camera")
            L.append("variation is not yet excluded. Flash one unit both ways to close that.")
        fams_bad = sorted({r["family"] for r in usable
                           if str(r["firmware_label"]) in bfw})
        fams_good = sorted({r["family"] for r in usable
                            if str(r["firmware_label"]) in gfw})
        L.append(f"\nThe two builds are also distinguishable from the frame timing "
                 f"alone: the failing one measures `{', '.join(fams_bad)}` and the "
                 f"passing one `{', '.join(fams_good)}`, which is a ten-second test "
                 f"for grading the rest of the fleet.")
    elif mixed:
        L.append(f"Firmware {', '.join(mixed)} gave BOTH passing and failing runs. "
                 f"Repeat those before concluding anything — a mixed result usually "
                 f"means a flash did not take, or a run the checks above flagged.")
    else:
        L.append("Not enough contrast yet: every usable run landed on the same side of")
        L.append("the line. Collect both firmware builds before drawing a conclusion.")
    caveats = [r for r in recs if not r["stimulus_reached_camera"]
               or not r["ramp"]["trustworthy"]]
    if caveats:
        L.append("\n## Runs to treat with caution\n")
        for r in caveats:
            why = []
            if not r["stimulus_reached_camera"]:
                why.append(f"the scene barely changed during the ramp "
                           f"(slow swing {r['ramp']['luma_slow_range']} vs "
                           f"{r['control']['luma_slow_range']} in the control), so "
                           f"the light ramp may not have reached the camera")
            if not r["ramp"]["trustworthy"]:
                why.append(f"{r['ramp']['dup_stamps']} duplicate timestamps")
            L.append(f"- `{r['tag']}` — {'; '.join(why)}")
    L.append("\n## Method notes\n")
    L.append("- Jitter event = a run of intervals containing one at least 3 ms under")
    L.append("  nominal; dropped frames (an exact multiple of nominal) are counted")
    L.append("  separately and are a different fault.")
    L.append("- Duplicate timestamps are discarded before scoring: a repeated stamp is a")
    L.append("  zero interval, which would otherwise read as a frame arriving infinitely")
    L.append("  early and manufacture the very fault being measured.")
    L.append("- The firmware family is read back from the measured frame interval, never")
    L.append("  from the flashed filename, so a flash that did not take is visible.")
    txt = "\n".join(L) + "\n"
    out = a.out or os.path.join(HERE, "firmware_jitter_report.md")
    with open(out, "w") as f:
        f.write(txt)
    print(txt)
    print(f"\n[written to {out}]")


p = argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
sub = p.add_subparsers(dest="cmd", required=True)
r = sub.add_parser("run", help="measure one camera on one firmware")
r.add_argument("--camera", required=True, help="unit number, e.g. 4.1")
r.add_argument("--fw", required=True, help="firmware number, e.g. 114")
r.add_argument("--reps", type=int, default=2)
r.add_argument("--device", default=None, help="/dev/videoN (default: ask)")
r.add_argument("--keep-raw", action="store_true")
r.set_defaults(func=cmd_run)
b = sub.add_parser("probe", help="check the camera can see the screen, before starting")
b.add_argument("--device", default=None)
b.set_defaults(func=cmd_probe)
s = sub.add_parser("status", help="what has been collected so far")
s.set_defaults(func=cmd_status)
g = sub.add_parser("report", help="write the markdown report")
g.add_argument("-o", "--out", default=None)
g.set_defaults(func=cmd_report)
a = p.parse_args()
a.func(a)
