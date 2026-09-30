#!/usr/bin/env python3
"""focusbarrel.py - live barrel-focus bench for a hand-screwed lens.

    ./run.sh                      -> http://127.0.0.1:8150
    ./run.sh --device /dev/video2
    ./run.sh --no-lock            -> leave the camera's auto modes alone
    ./run.sh --host 0.0.0.0       -> reach it from a phone while both hands are on the lens

WHAT THIS IS FOR

You are turning the lens barrel by hand and you want to stop at the sharpest
point. There is no jig and no printed chart: you point the camera at whatever is
in front of it - a box with print on it works well - and the tool tells you live
whether you are getting sharper or softer, which way to keep turning, and when
you are back at the best point it has seen.

WHY THERE IS NO ABSOLUTE "SHARP ENOUGH" NUMBER, AND WHY YOU MUST SWEEP

Sharpness measured off an arbitrary scene has no absolute meaning. The same lens
at the same focus reads wildly different numbers on different scenes, so any
fixed threshold ("green above 2000") would be a lie dressed up as a measurement.
Everything here is therefore RELATIVE to the best reading seen since the last
reset, which has one consequence you cannot design away:

    the tool cannot know where the peak is until you have turned PAST it.

At startup the current reading is trivially also the best reading, so a naive
tool would show green immediately and mean nothing by it. Instead this one stays
grey and says SWEEP until the readings have spanned at least SPAN_MIN (a real
rise and fall), and only then does it arm the green verdict. Sweep past the peak
in both directions, then come back to it.

THE OTHER TRAP: A ROOM IS A BAD TARGET

A general room scene has content at every distance, so something is always in
focus and the metric barely moves - measured on this camera it swung only ~1.4x
across the entire focus range, and not even monotonically. A weak sweep span is
exactly what that failure looks like, so the span is shown in the UI as the
honesty check: if it stays under about 1.2x, the target is not good enough and
no amount of squinting at the number will fix it. Aim the ROI at printed text or
a barcode on ONE surface at ONE distance and let it fill the box.

WHAT IS PINNED, AND WHY IT MATTERS MORE THAN THE METRIC

Every auto knob on this camera moves the sharpness reading on its own. Auto
exposure re-meters as the barrel turns and changes contrast; the ISP `sharpness`
control (0-255) is a digital edge enhancer, so leaving it free means measuring
the ISP rather than the glass. So on startup the tool pins exposure, white
balance, gain, gamma, backlight compensation and sharpness for the rest of the
session. Writing those needs a three-pass apply, because a dependent control is
read-only while its auto mode owns it - see apply_values().

The exposure it pins is metered off the IMAGE, not read back from the camera,
because on this camera reading it does not work: exposure_time_absolute returns
whatever was last WRITTEN to it and never what the auto mode is doing. Measured
directly - with that control reading 1 the whole time, the ROI sat at luma 2.0
in Manual Mode and 98.8 in Aperture Priority. Anything that "meters" by letting
auto settle and then reading the control is really just locking the register's
stale contents. See Bench.meter_exposure().

STEREO

On a side-by-side stereo camera the buffer is [data strip][right lens][left
lens] and there is ONE v4l2 control set for both eyes, so nothing here can be
tuned per eye. Both eyes are measured at the same ROI and tracked separately,
which is what you want: you are screwing one barrel while the other eye sits
still as a control. Do not read the L/R ratio as a verdict on the glass - the
eyes are offset by the stereo baseline and so frame slightly different content,
which alone can move the ratio further than a real defect would.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8150

# [data strip][right lens][left lens] - the strip is camera metadata, not image.
DATA_STRIP_WIDTH = 160
# Wider than this aspect and the frame is two eyes side by side. A 16:9 webcam
# is 1.78, this stereo camera is 3.33, so the gap is wide and unambiguous.
STEREO_ASPECT = 2.5

# --- verdict tuning -------------------------------------------------------
EMA_ALPHA = 0.30        # smoothing on the displayed metric
SPAN_MIN = 1.20         # best/lowest must exceed this before green is armed
MIN_BAND = 0.03         # "at the peak" means within this fraction of best...
MAX_BAND = 0.08         # ...but never looser than this, whatever the noise says
NOISE_K = 2.0           # ...or within this many sigma of measured noise, whichever is looser
# Past this much frame-to-frame jitter the band would be pinned at MAX_BAND
# anyway, which is the point where the verdict has stopped meaning anything. So
# rather than flicker green on noise, say outright that no call can be made.
# Reference points measured on this bench: a well-lit ROI at luma 116 jitters
# 0.6%; the same rig at luma 43 jitters 4-12% and invented a 1.5x sweep with
# nothing moving at all.
NOISE_UNUSABLE = MAX_BAND / NOISE_K
TREND_WIN = 0.5         # seconds per half of the rising/falling comparison
TREND_MIN = 0.015       # relative change below this counts as "holding still"
# A reading only counts toward best/lowest once it has been HELD this long.
# Peak-hold on the instantaneous value is wrong when the signal is noisy: the
# max latches onto the top of the noise and the min onto the bottom, so `best`
# becomes unreachable and `span` reports a sweep that never happened. Measured
# on a dark scene with nothing moving - +/-7% jitter alone produced span 2.4x
# and pinned the readout at 60-70% of a peak no barrel position could reach.
BEST_HOLD_S = 2.0
HIST_DECIMATE = 0.1     # seconds between trace points
HIST_SECONDS = 60.0

# Measured on this camera: exposure at or below 100 (10.0 ms) streams 30 fps,
# anything from 120 (12.0 ms) up halves it to 15. Frame SIZE barely moves across
# that step (~310 KB either way), so this is the sensor changing its frame
# interval, not the USB throughput ceiling that limits these cameras elsewhere.
# Nothing here breaks at 15 fps, it just feels half as responsive under your
# fingers, so the drop is reported rather than silently accepted.
FPS_KNEE_EXPOSURE = 100

# Reads that fail for this long mean the camera is gone, not busy. Swapping
# cameras mid-session is the normal workflow on this bench, so that has to be
# recovered from rather than spun on.
CAM_FAIL_SECONDS = 2.0

# --- scene guards ---------------------------------------------------------
# Absolute bounds are legitimate here in a way they are not for the sharpness
# verdict: these are properties of the 8-bit sensor output, not of the scene.
LUMA_LO, LUMA_HI = 35.0, 215.0
CLIP_MAX_PCT = 2.0      # pixels at >=250 destroy the gradients they sit on
STD_MIN = 8.0           # below this the ROI is a blank surface, not a target


def _run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


# ------------------------------------------------------------ v4l2 plumbing

def list_capture_devices():
    """[{path, label, port}] for real capture nodes only.

    A UVC camera also publishes metadata nodes, and with two cameras attached
    the capture and metadata nodes interleave (video0+video1 capture,
    video2+video3 metadata) - so /dev/videoN order proves nothing and every
    node has to be asked whether it can actually capture.
    """
    rc, out, _ = _run(["v4l2-ctl", "--list-devices"])
    seen, label = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            label = line.split("(")[0].strip().rstrip(":")
        elif line.strip().startswith("/dev/video"):
            seen.append((line.strip(), label))
    if not seen:
        import glob
        seen = [(p, "camera") for p in sorted(glob.glob("/dev/video*"))]

    devs = []
    for path, label in seen:
        rc, info, _ = _run(["v4l2-ctl", "-d", path, "--info"])
        caps = info.split("Device Caps")[-1] if "Device Caps" in info else ""
        if "Video Capture" not in caps:
            continue
        label = label or path
        half = (label or "").split(":")
        if len(half) == 2:
            a, b = half[0].strip(), half[1].strip()
            if a.startswith(b) or b.startswith(a):
                label = a if len(a) >= len(b) else b
        bm = re.search(r"Bus info\s*:\s*(\S+)", info)
        port = re.sub(r"^usb-(?:[0-9a-f]{4}:)?", "", bm.group(1)) if bm else ""
        devs.append({"path": path, "label": label, "port": port})
    return devs


def device_holders(dev):
    """Process names already holding the node. Only one process can own a
    /dev/video* node, and the failure is confusing from the inside: control
    reads keep working fine while every frame grab returns busy."""
    rc, out, err = _run(["fuser", "-v", dev])
    pids = re.findall(r"\b(\d+)\b", (out or "") + " " + (err or ""))
    names = []
    for pid in dict.fromkeys(pids):
        try:
            with open("/proc/%s/comm" % pid) as f:
                names.append("%s(%s)" % (f.read().strip(), pid))
        except OSError:
            pass
    return names


_CTRL_RE = re.compile(
    r"^\s*(?P<name>\w+)\s+0x[0-9a-fA-F]+\s+\((?P<type>[\w-]+)\)\s*:\s*(?P<rest>.*)$")


def get_controls(dev):
    rc, out, _ = _run(["v4l2-ctl", "-d", dev, "--list-ctrls"])
    ctrls = {}
    for line in out.splitlines():
        m = _CTRL_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")

        def num(k):
            mm = re.search(r"\b%s=(-?\d+)" % k, rest)
            return int(mm.group(1)) if mm else None

        ctrls[m.group("name")] = {
            "type": m.group("type"),
            "min": num("min"), "max": num("max"),
            "default": num("default"), "value": num("value"),
            "inactive": "inactive" in rest,
        }
    return ctrls


def set_control(dev, name, value):
    rc, out, err = _run(["v4l2-ctl", "-d", dev, "--set-ctrl", "%s=%s" % (name, value)])
    msg = (err or out).strip()
    return rc == 0 and "error" not in msg.lower(), msg


# dependent control -> (gating auto control, value that UNLOCKS the dependent)
GATE_MAP = {
    "exposure_time_absolute":    ("auto_exposure", 1),
    "exposure_absolute":         ("exposure_auto", 1),
    "focus_absolute":            ("focus_automatic_continuous", 0),
    "white_balance_temperature": ("white_balance_automatic", 0),
}


def apply_values(dev, values):
    """Apply many controls at once, in three passes.

    exposure_time_absolute is read-only while auto_exposure owns it, so a naive
    loop fails with EACCES on exactly the values that matter. Unlock the gating
    auto modes first, write everything, then set the gates to what was actually
    asked for.
    """
    ctrls = get_controls(dev)
    vals = {k: v for k, v in values.items() if k in ctrls and v is not None}

    unlock = {}
    for dep, (gate, need) in GATE_MAP.items():
        if dep in vals and gate in ctrls:
            unlock[gate] = need

    problems = []

    def write(name, val):
        ok, msg = set_control(dev, name, val)
        if not ok:
            line = msg.splitlines()[-1].strip() if msg else "failed"
            if "permission denied" not in line.lower():
                problems.append("%s: %s" % (name, line))

    for gate, need in unlock.items():
        write(gate, need)
    for name, val in vals.items():
        if name not in unlock:
            write(name, val)
    for gate in unlock:
        if gate in vals:
            write(gate, vals[gate])
    return problems


# Pinned to whatever the driver calls default: these are the ISP knobs that move
# a sharpness reading without the glass moving at all. `sharpness` is the one
# that matters most - it is a digital edge enhancer, so left free it is the
# thing being measured.
PIN_TO_DEFAULT = ["brightness", "contrast", "saturation", "hue", "gamma", "gain",
                  "sharpness", "backlight_compensation", "zoom_absolute",
                  "region_of_interest_auto_ctrls"]


# Closed-loop metering aims for this ROI luma - comfortably inside the
# LUMA_LO/LUMA_HI guards with headroom at both ends.
TARGET_LUMA = 120.0
DARK_LUMA = 30.0        # below this the ROI risks crushing the detail into black
LUMA_TOL = 6.0
METER_STEPS = 9
# Metering that lands just past the frame-rate knee costs HALF the preview rate
# for a few percent of brightness, which is a bad trade nobody should have to
# think about. Within this ratio of the knee, drop to the knee instead - but
# only if the ROI is still bright enough afterwards, which is measured, not
# assumed.
SNAP_RATIO = 1.6
SNAP_MIN_LUMA = TARGET_LUMA - 30.0


def flicker_quantum(dev):
    """Exposure step that integrates a whole number of mains light cycles.

    Mains-powered lighting ripples at TWICE the supply frequency (the lamp
    brightens on both halves of the cycle), so 50 Hz mains flickers at 100 Hz =
    a 10.0 ms period. A shutter that is not a whole multiple of that catches a
    different slice of the ripple every frame, and the resulting contrast wobble
    lands directly on the sharpness metric.

    Measured on this bench, same camera, same scene, back to back: a 48.5 ms
    shutter gave 11.5% metric jitter with luma/metric correlation -0.71, while
    40.0 ms - exactly four cycles - gave 2.8% and correlation 0.06. Same light,
    same lens: a 4x improvement for free. This matters far more than it sounds,
    because in a normal office you cannot fix it by adding light.
    """
    c = get_controls(dev)
    hz = 50
    m = c.get("power_line_frequency")
    if m and m.get("value") == 2:      # menu: 0 disabled, 1 = 50 Hz, 2 = 60 Hz
        hz = 60
    return 10000.0 / (2 * hz), hz      # in 100us units: 50 Hz -> 100, 60 Hz -> 83.3


def pin_isp(dev, wb_k=None):
    """Freeze everything that can move a sharpness reading without the glass
    moving: the ISP knobs, white balance, focus, and auto exposure itself.

    Exposure VALUE is not set here - it cannot be chosen without looking at the
    image, so Bench.meter_exposure() does it. This function's job is to put the
    camera in manual so that metering has something to write to.
    """
    c = get_controls(dev)
    notes = []

    want = {}
    for name in PIN_TO_DEFAULT:
        if name in c and c[name]["default"] is not None:
            want[name] = c[name]["default"]

    if "auto_exposure" in c:
        want["auto_exposure"] = 1          # 1 = Manual Mode

    if "white_balance_temperature" in c:
        # Pinned to a fixed temperature rather than to "whatever auto chose",
        # because on this camera auto's choice is not readable (see
        # Bench.meter_exposure). 4650 K is the same setpoint the white-balance
        # bench pins to, so the two tools at least agree.
        k = int(wb_k or 4650)
        lo, hi = c["white_balance_temperature"]["min"], c["white_balance_temperature"]["max"]
        if lo is not None and hi is not None:
            k = max(lo, min(hi, k))
        want["white_balance_automatic"] = 0
        want["white_balance_temperature"] = k

    # Fixed-focus glass, but the control still exists and an auto mode that does
    # nothing visible can still tick the image. Pin it shut.
    if "focus_absolute" in c:
        want["focus_automatic_continuous"] = 0
        want["focus_absolute"] = c["focus_absolute"]["default"] or 0

    problems = apply_values(dev, want)
    return problems + notes


# ------------------------------------------------------------ measurement

def tenengrad(roi):
    """Sobel gradient energy - the classic autofocus metric, monotonic through
    focus and cheap. Returned raw: with exposure locked there is nothing to
    normalise against, and dividing by the ROI variance would cancel part of the
    signal, since a sharper image legitimately has a higher variance."""
    gx = cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3)
    mag2 = gx * gx + gy * gy
    return float(np.mean(mag2)), mag2


def scene_stats(roi):
    """Is this ROI even measurable? Nothing here judges focus - these are the
    conditions under which the focus number means anything at all."""
    mean = float(roi.mean())
    std = float(roi.std())
    clip = float(np.count_nonzero(roi >= 250)) * 100.0 / roi.size
    msg = ""
    if mean < LUMA_LO:
        msg = "too dark - add light"
    elif mean > LUMA_HI:
        msg = "too bright - the ROI is washed out"
    elif clip > CLIP_MAX_PCT:
        msg = "%.0f%% of the ROI is clipped white - move off the glare" % clip
    elif std < STD_MIN:
        msg = "no detail in the ROI - aim at printed text, not a blank surface"
    return {"mean": round(mean, 1), "std": round(std, 1),
            "clip": round(clip, 2), "ok": msg == "", "msg": msg}


class FocusTrack:
    """Peak-hold tracker for one eye.

    Holds the highest smoothed reading since reset and answers three questions:
    how close are we to it, has the sweep been wide enough for it to mean
    anything, and which way is the number currently moving.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.ema = None
        self.best = 0.0
        self.best_t = 0.0
        self.lo = None
        self.peak_thr = None      # gradient magnitude at best, for the peaking overlay
        self.hist = deque()       # (t, smoothed) decimated for the trace
        self._last_hist = 0.0
        self.recent = deque(maxlen=40)   # raw values, for the live noise estimate
        self.hold = deque()       # (t, smoothed) over the last BEST_HOLD_S
        self.held_v = None        # median of that window - the decision variable
        self._t0 = time.time()

    def update(self, raw, mag2, now):
        self.recent.append(raw)
        self.ema = raw if self.ema is None else (1 - EMA_ALPHA) * self.ema + EMA_ALPHA * raw
        v = self.ema

        # best and lowest both come from the MEDIAN of the last second, not from
        # the instantaneous value. Smoothing alone was not enough: an EMA still
        # rides the noise, so max() latched the top of it and min() the bottom.
        # A real focus change holds for far longer than a second; noise does not
        # survive a median.
        self.hold.append((now, v))
        while self.hold and now - self.hold[0][0] > BEST_HOLD_S:
            self.hold.popleft()
        if now - self._t0 >= BEST_HOLD_S and len(self.hold) >= 5:
            held = float(np.median([x for _, x in self.hold]))
            self.held_v = held
            if held > self.best:
                self.best, self.best_t = held, now
                if mag2 is not None:
                    self.peak_thr = float(np.percentile(np.sqrt(mag2), 98.0))
            self.lo = held if self.lo is None else min(self.lo, held)

        if now - self._last_hist >= HIST_DECIMATE:
            self._last_hist = now
            self.hist.append((now, v))
            while self.hist and now - self.hist[0][0] > HIST_SECONDS:
                self.hist.popleft()

    @property
    def noise(self):
        """Frame-to-frame measurement noise: the MAD of SECOND differences.

        Three estimators, each fixing the previous one's blind spot:

        - The window's standard deviation is dominated by the real signal while
          the barrel turns, so the band widened to swallow everything and the
          tool called green 11% off the best.
        - First differences cancel the level but not the slope, and are wrecked
          by a single step: when something in the room moved once, a series that
          was otherwise steady to 0.3% was scored as 13% noise, which tripped the
          "too noisy to call" gate while nothing was actually wrong.
        - Second differences cancel level AND slope, so a steadily turning barrel
          reads as quiet; taking the MAD rather than the std makes it shrug off
          the occasional step too.

        Validated against synthetic cases (true noise in brackets): quiet [0.5%]
        -> 0.32%, noisy [7%] -> 6.69%, ramp -> 0.32%, single step -> 0.32%.
        1.4826*MAD is the std of a Gaussian; /sqrt(6) converts a second
        difference back to a single sample.
        """
        if len(self.recent) < 10:
            return 0.0
        a = np.fromiter(self.recent, dtype=np.float64)
        m = float(a.mean())
        if m <= 0:
            return 0.0
        d2 = np.diff(a, 2)
        mad = float(np.median(np.abs(d2 - np.median(d2))))
        return float(1.4826 * mad / np.sqrt(6.0) / m)

    def trend(self, now):
        """'up' / 'down' / 'flat' from the last two TREND_WIN windows - this is
        the bit that tells you which way to keep turning."""
        if len(self.hist) < 4:
            return "flat"
        a = [v for t, v in self.hist if now - t <= TREND_WIN]
        b = [v for t, v in self.hist if TREND_WIN < now - t <= 2 * TREND_WIN]
        if len(a) < 2 or len(b) < 2:
            return "flat"
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        if mb <= 0:
            return "flat"
        d = (ma - mb) / mb
        return "up" if d > TREND_MIN else ("down" if d < -TREND_MIN else "flat")

    def verdict(self, now, scene_ok):
        # Compare like with like. `best` is the max of the 2 s median, so scoring
        # the raw EMA against it lets a rising reading overshoot its own peak and
        # report "109% of best", which is meaningless. The median is the decision
        # variable; the EMA stays the live number on the trace.
        v = self.held_v if self.held_v is not None else (self.ema or 0.0)
        best = self.best or 0.0
        span = (best / self.lo) if (self.lo and self.lo > 0) else 1.0
        # Capped as well as floored: however noisy the scene gets, "within 8% of
        # best" is the loosest claim worth making, and without a ceiling a bad
        # patch of noise silently turns the verdict into a rubber stamp.
        band = min(MAX_BAND, max(MIN_BAND, NOISE_K * self.noise))
        pct = (v / best) if best > 0 else 0.0
        trend = self.trend(now)

        if not scene_ok:
            state, hint = "scene", "fix the framing before trusting any number"
        elif self.noise > NOISE_UNUSABLE:
            state = "noisy"
            hint = ("readings jitter %.0f%% frame to frame - as much as the focus change "
                    "you are looking for. No peak can be called until there is more light "
                    "on the target." % (self.noise * 100))
        elif span < SPAN_MIN:
            state = "sweep"
            hint = ("keep screwing - I have only seen a %.2fx spread, too flat to "
                    "call a peak yet" % span)
        elif pct >= 1.0 - band:
            state, hint = "peak", "this is the best point seen - stop here"
        else:
            state = "off"
            if trend == "up":
                hint = "getting sharper - keep turning the same way"
            elif trend == "down":
                hint = "getting softer - turn back the other way"
            else:
                hint = "%.0f%% of best and not moving - turn the barrel" % (pct * 100)

        return {"s": round(v, 1), "live": round(self.ema or 0.0, 1),
                "best": round(best, 1), "pct": round(pct, 4),
                "span": round(span, 3), "band": round(band, 4),
                "noise": round(self.noise, 4), "state": state, "trend": trend,
                "hint": hint, "age": round(now - self.best_t, 1) if self.best_t else 0.0}


# ------------------------------------------------------------ capture

class Bench:
    """Owns the capture, the measurement and the two view encodings.

    One grab per frame feeds everything: the metric runs on the NATIVE-resolution
    ROI (downscaling first would throw away the exact high frequencies the whole
    tool is about), while the aiming view is decoded straight to quarter size by
    the JPEG decoder, which is nearly free.
    """

    def __init__(self, device, roi_size=0.30, want_fps=60):
        self.device = device
        self.roi = {"cx": 0.5, "cy": 0.5, "size": roi_size}
        self.want_fps = want_fps
        self.eye = "left"
        self.peaking = False

        self.cap = None
        self.label = ""
        self.running = False
        self.err = ""
        self.fps = 0.0
        self.stereo = False
        self.size = (0, 0)
        self.lock_notes = []
        self.lock_got = {}
        self.locked = False
        self._metering = False
        self.max_exposure = 500
        self.default_max_exposure = 500
        self.wb_k = None
        self.exposure = None
        self.flicker_hz = None

        self.tracks = {"left": FocusTrack(), "right": FocusTrack()}
        self.stats = {"left": None, "right": None}
        self.roi_px = {}

        self._retry_at = 0.0
        self.last_frame_t = 0.0
        self.cond = threading.Condition()
        self.roi_jpeg = None
        self.full_jpeg = None
        self.seq = 0

    # -- lifecycle
    def open(self):
        holders = device_holders(self.device)
        if holders:
            self.err = ("%s is already held by %s - close it first (camrig, camview and "
                        "the lenscheck bench all take the node exclusively)"
                        % (self.device, ", ".join(holders)))
            return False
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.err = "cannot open %s" % self.device
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4000)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1200)
        cap.set(cv2.CAP_PROP_FPS, self.want_fps)
        # Raw mode: hand back the MJPEG buffer instead of a converted BGR frame,
        # so we choose what to decode and at what size.
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, buf = cap.read()
        if not ok or buf is None:
            cap.release()
            self.err = "%s opened but delivered no frames" % self.device
            return False
        self.cap = cap
        return True

    def _wait_frames(self, n=3, timeout=3.0):
        with self.cond:
            start = self.seq
            return self.cond.wait_for(lambda: self.seq >= start + n, timeout=timeout)

    def meter_exposure(self, target=TARGET_LUMA):
        """Binary-search the shutter until the selected eye's ROI hits `target`.

        This is metered off the IMAGE because on this camera it cannot be metered
        any other way: exposure_time_absolute reads back whatever was last
        WRITTEN to it and never what the auto mode is actually doing. Measured
        directly - with the control reading 1 throughout, the ROI sat at luma 2.0
        in Manual and at 98.8 in Aperture Priority. So the old "let auto settle,
        then read the control and lock that" would have locked the register's
        stale contents, with no relationship to the room. Luma rises
        monotonically with exposure, so a binary search converges in a few steps.
        """
        lo, hi = 1, max(1, int(self.max_exposure))
        best = None                    # (exposure, luma) closest to target so far
        for _ in range(METER_STEPS):
            mid = (lo + hi) // 2
            ok, _msg = set_control(self.device, "exposure_time_absolute", mid)
            if not ok or not self._wait_frames(3):
                break
            st = self.stats.get(self.eye) or {}
            m = st.get("mean")
            if m is None:
                break
            if best is None or abs(m - target) < abs(best[1] - target):
                best = (mid, m)
            if abs(m - target) <= LUMA_TOL:
                break
            if m < target:
                lo = mid + 1
            else:
                hi = mid - 1
            if lo > hi:
                break
        if best is not None and best[0] != int(self.exposure or -1):
            set_control(self.device, "exposure_time_absolute", best[0])
            self._wait_frames(2)
        return best

    def lock_camera(self, max_exposure=None, wb_k=None):
        """Pin the ISP, then meter the shutter against live frames."""
        if self._metering:
            return
        self._metering = True
        try:
            self._lock_camera(max_exposure, wb_k)
        finally:
            self._metering = False

    def _lock_camera(self, max_exposure=None, wb_k=None):
        if max_exposure is not None:
            self.max_exposure = max_exposure
        notes = pin_isp(self.device, wb_k=self.wb_k)

        got = self.meter_exposure()

        # Snap to whole mains-flicker cycles BEFORE anything else looks at the
        # exposure - this is the single biggest lever on measurement noise in a
        # room lit by mains, and it costs nothing.
        q, hz = flicker_quantum(self.device)
        self.flicker_hz = hz
        if got:
            k = int(round(got[0] / q))
            if k < 1:
                # Below one whole cycle there is no flicker-free option at all.
                # Rounding UP to one cycle is not a compromise, it is a blown
                # frame: a scene metering at 4.0 ms would get 10.0 ms, i.e. 2.5x
                # the light. That is how a run once locked at luma 214 with the
                # highlights clipped, which destroys the measurement far more
                # thoroughly than flicker does.
                notes.append("scene is too bright for a flicker-free shutter: it meters at "
                             "%.1f ms and the shortest whole %d Hz light cycle is %.1f ms. "
                             "Keeping the correct exposure and living with the ripple - "
                             "expect a jumpier reading, and shade the target if you can."
                             % (got[0] / 10.0, hz, q / 10.0))
            else:
                snapped = int(round(k * q))
                if snapped != got[0]:
                    before = got
                    set_control(self.device, "exposure_time_absolute", snapped)
                    self._wait_frames(4)
                    st = self.stats.get(self.eye) or {}
                    m, clip = st.get("mean"), st.get("clip", 0.0)
                    # Verify rather than assume: rounding up is only free if the
                    # frame can absorb the extra light.
                    if m is not None and (m > TARGET_LUMA + 40 or clip > CLIP_MAX_PCT) and k > 1:
                        snapped = int(round((k - 1) * q))
                        set_control(self.device, "exposure_time_absolute", snapped)
                        self._wait_frames(4)
                        st = self.stats.get(self.eye) or {}
                        m, clip = st.get("mean"), st.get("clip", 0.0)
                        k -= 1
                    if m is not None and (m > TARGET_LUMA + 40 or clip > CLIP_MAX_PCT):
                        set_control(self.device, "exposure_time_absolute", before[0])
                        self._wait_frames(3)
                        got = before
                        notes.append("could not align the shutter to the %.1f ms light ripple "
                                     "without overexposing, so the metered %.1f ms is kept. "
                                     "Expect a jumpier reading."
                                     % (q / 10.0, before[0] / 10.0))
                    else:
                        got = (snapped, m if m is not None else before[1])
                        notes.append("shutter snapped to %.1f ms = %d x the %.1f ms ripple of "
                                     "%d Hz mains, so each frame integrates whole light cycles. "
                                     "Measured 4x less jitter than an unaligned shutter."
                                     % (snapped / 10.0, k, q / 10.0, hz))

        if got and FPS_KNEE_EXPOSURE < got[0] <= FPS_KNEE_EXPOSURE * SNAP_RATIO:
            was = got
            set_control(self.device, "exposure_time_absolute", FPS_KNEE_EXPOSURE)
            self._wait_frames(4)
            m = (self.stats.get(self.eye) or {}).get("mean")
            if m is not None and m >= SNAP_MIN_LUMA:
                got = (FPS_KNEE_EXPOSURE, m)
                notes.append("shutter trimmed %.1f -> %.1f ms to hold 30 fps instead of 15; "
                             "ROI luma %.0f, still well inside the usable band."
                             % (was[0] / 10.0, FPS_KNEE_EXPOSURE / 10.0, m))
            else:
                set_control(self.device, "exposure_time_absolute", was[0])
                self._wait_frames(2)

        if got is None:
            notes.append("could not meter the exposure - the ROI luma never came back. "
                         "Readings will still work but are not exposure-stable.")
        else:
            exp, luma = got
            self.exposure = exp
            # Only a genuinely crushed ROI is worth complaining about. This used
            # to fire at luma 46 telling the operator to add light "because a dim
            # ROI measures noise as much as focus" - which turned out to be
            # false: with the shutter aligned to the mains ripple the same luma-46
            # ROI measures 0.2% jitter, better than a bright one with an unaligned
            # shutter. Noise is measured directly and warned about on its own, so
            # darkness does not need to stand in as a proxy for it.
            if luma < DARK_LUMA:
                # Keyed off the LUMA IT REACHED, not off hitting the cap exactly.
                # The search converges to the closest value it tried, so a dark
                # room lands on something like 497 against a 500 cap and an
                # `exp >= cap` test misses - which is how a scene at luma 56 once
                # went out with no warning at all.
                notes.append("ROI is dim: luma %.0f at the %.1f ms shutter, against a target "
                             "of %.0f. Watch that the print you are focusing on has not gone "
                             "into the black - check the peak band in Numbers to see whether "
                             "it is actually costing you precision."
                             % (luma, exp / 10.0, TARGET_LUMA))
            elif luma > TARGET_LUMA + 25 and exp <= 1:
                notes.append("scene is very bright: even at the shortest shutter the ROI is "
                             "at luma %.0f. Aim off the glare." % luma)
            if exp > FPS_KNEE_EXPOSURE:
                notes.append("exposure %.1f ms is past the %.1f ms point where this camera "
                             "halves to 15 fps. Flicker-free shutters come in %.1f ms steps, so "
                             "%.1f ms is the ONLY one that also holds 30 fps - and in this light "
                             "that would underexpose. 15 fps with a clean reading beats 30 fps "
                             "with a flickering one."
                             % (exp / 10.0, FPS_KNEE_EXPOSURE / 10.0,
                                q / 10.0, FPS_KNEE_EXPOSURE / 10.0))

        after = get_controls(self.device)
        self.lock_got = {k: after[k]["value"]
                         for k in ("exposure_time_absolute", "white_balance_temperature",
                                   "sharpness", "gain", "gamma") if k in after}
        if got:
            self.lock_got["metered_luma"] = round(got[1])
        self.lock_notes = notes
        self.locked = True
        self.reset()

    def reset(self):
        for t in self.tracks.values():
            t.reset()

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()

    # -- geometry
    def _split(self, gray):
        """(left, right) eye views. Buffer layout is [strip][right][left], so the
        SECOND half is the physically-left lens."""
        h, w = gray.shape
        if w < h * STEREO_ASPECT:
            return {"left": gray}
        lw = (w - DATA_STRIP_WIDTH) // 2
        right = gray[:, DATA_STRIP_WIDTH:DATA_STRIP_WIDTH + lw]
        left = gray[:, DATA_STRIP_WIDTH + lw:]
        return {"left": left, "right": right}

    def _roi_box(self, eye_img):
        h, w = eye_img.shape
        side = max(64, int(min(w, h) * self.roi["size"]))
        cx, cy = int(self.roi["cx"] * w), int(self.roi["cy"] * h)
        x = max(0, min(w - side, cx - side // 2))
        y = max(0, min(h - side, cy - side // 2))
        return x, y, side

    # -- main loop
    def _loop(self):
        tick, count = time.time(), 0
        first_fail = None
        while self.running:
            try:
                # A failed reconnect leaves no capture at all. Dereferencing it
                # anyway used to raise straight out of this thread and kill it,
                # while the HTTP server carried on serving the last numbers it
                # had - which from the outside is exactly what "the app froze"
                # looks like. Retry instead, with a backoff so it does not spin.
                if self.cap is None:
                    if time.time() >= self._retry_at:
                        self._reconnect()
                    else:
                        time.sleep(0.1)
                    continue

                ok, buf = self.cap.read()
                if not ok or buf is None:
                    now = time.time()
                    first_fail = first_fail or now
                    if now - first_fail >= CAM_FAIL_SECONDS:
                        self._reconnect()
                        first_fail = None
                    else:
                        time.sleep(0.02)
                    continue
                first_fail = None
                raw = buf.reshape(-1)
                gray = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                now = self.last_frame_t = time.time()
                self.size = (gray.shape[1], gray.shape[0])
                eyes = self._split(gray)
                self.stereo = len(eyes) > 1

                sel = self.eye if self.eye in eyes else next(iter(eyes))
                roi_vis = None
                for name, img in eyes.items():
                    x, y, side = self._roi_box(img)
                    roi = img[y:y + side, x:x + side]
                    st = scene_stats(roi)
                    s, mag2 = tenengrad(roi)
                    self.stats[name] = st
                    # A measurement taken through a bad frame must not set the peak
                    # the rest of the session is judged against.
                    if st["ok"]:
                        self.tracks[name].update(s, mag2, now)
                    if name == sel:
                        self.roi_px = {"x": x, "y": y, "side": side,
                                       "w": img.shape[1], "h": img.shape[0]}
                        roi_vis = self._render_roi(roi, mag2, self.tracks[name])

                full = cv2.imdecode(raw, cv2.IMREAD_REDUCED_COLOR_4)
                full_vis = self._render_full(full, eyes, sel)

                with self.cond:
                    self.roi_jpeg = roi_vis
                    self.full_jpeg = full_vis
                    self.seq += 1
                    self.cond.notify_all()

                count += 1
                if now - tick >= 1.0:
                    self.fps = count / (now - tick)
                    tick, count = now, 0
            except Exception as e:
                # Nothing in here is worth losing the capture thread over.
                self.err = "capture error (%s) - recovering" % e
                try:
                    if self.cap is not None:
                        self.cap.release()
                except Exception:
                    pass
                self.cap = None
                self._retry_at = time.time() + 1.0
                continue

    def _reconnect(self):
        """Re-find and re-open the camera after it stopped delivering frames.

        Unplugging a camera does not make reads return an error forever in a
        useful way: the node is removed from /dev but the open fd survives as
        "(deleted)", so the loop would spin on a corpse while the replacement
        camera enumerates on a DIFFERENT node - and, because the dead fd is still
        held, the next launch of this tool would find the port taken and die.
        Swapping cameras is the whole point of this bench, so re-discover
        instead.
        """
        self.err = "camera stopped delivering frames - looking for it again..."
        try:
            self.cap.release()
        except Exception:
            pass
        self.cap = None

        devs = list_capture_devices()
        want = next((d for d in devs if d["path"] == self.device), None)
        pick = want or (devs[0] if devs else None)
        if pick is None:
            self.err = "no capture device found - waiting for one to appear"
            self._retry_at = time.time() + 1.5
            return
        self.device = pick["path"]
        self.label = pick["label"]
        if not self.open():
            # self.err already says why (busy, or opened but no frames)
            self._retry_at = time.time() + 1.5
            return
        self.err = ""
        # A different camera means a different lens: every peak held against the
        # old one is meaningless, and the new one needs its own exposure. Locking
        # waits on frames from THIS thread, so it cannot run here.
        self.reset()
        if self.locked:
            threading.Thread(target=self.lock_camera, daemon=True).start()

    def _render_roi(self, roi, mag2, track):
        """The ROI at native pixels - this is the view you actually judge focus
        on. Luma only: it is what the metric sees, and colour adds nothing to a
        focus decision."""
        if self.peaking and mag2 is not None:
            vis = cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)
            # Threshold held from the sharpest frame so far, so the amount of
            # highlight grows as you approach focus instead of always painting a
            # fixed 2% of the pixels whatever the focus is doing.
            thr = track.peak_thr or float(np.percentile(np.sqrt(mag2), 98.0))
            vis[np.sqrt(mag2) >= thr] = (0, 90, 255)
            out = vis
        else:
            out = roi
        ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return jpg.tobytes() if ok else None

    def _render_full(self, full, eyes, sel):
        """Quarter-size aiming view with the ROI drawn on it. Click it to move
        the ROI."""
        if full is None:
            return None
        h, w = full.shape[:2]
        if self.roi_px:
            sx = w / float(self.size[0])
            sy = h / float(self.size[1])
            off = 0
            if self.stereo:
                lw = (self.size[0] - DATA_STRIP_WIDTH) // 2
                off = DATA_STRIP_WIDTH + (lw if sel == "left" else 0)
            x = int((off + self.roi_px["x"]) * sx)
            y = int(self.roi_px["y"] * sy)
            s = int(self.roi_px["side"] * sx)
            cv2.rectangle(full, (x, y), (x + s, y + s), (60, 230, 60), 2)
        ok, jpg = cv2.imencode(".jpg", full, [cv2.IMWRITE_JPEG_QUALITY, 72])
        return jpg.tobytes() if ok else None

    # -- state for the UI
    def state(self):
        now = time.time()
        eyes = {}
        for name, tr in self.tracks.items():
            st = self.stats.get(name)
            if st is None:
                continue
            v = tr.verdict(now, st["ok"])
            v["scene"] = st
            eyes[name] = v
        sel = self.eye if self.eye in eyes else (next(iter(eyes)) if eyes else None)
        trace = []
        if sel:
            tr = self.tracks[sel]
            best = tr.best or 1.0
            trace = [[round(now - t, 2), round(v / best, 4)] for t, v in tr.hist]
        return {
            "ok": self.err == "", "err": self.err, "fps": round(self.fps, 1),
            "since_frame": round(now - self.last_frame_t, 1) if self.last_frame_t else None,
            "device": self.device, "stereo": self.stereo,
            "size": {"w": self.size[0], "h": self.size[1]},
            "eye": sel, "eyes": eyes, "trace": trace,
            "roi": dict(self.roi, px=self.roi_px),
            "peaking": self.peaking, "locked": self.locked,
            "max_exposure": self.max_exposure, "fps_knee": FPS_KNEE_EXPOSURE,
            "default_max_exposure": self.default_max_exposure,
            "exposure": self.exposure, "flicker_hz": self.flicker_hz,
            "lock_notes": self.lock_notes, "lock_got": self.lock_got,
        }


# ------------------------------------------------------------ http

class Handler(BaseHTTPRequestHandler):
    bench: Bench = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        b = self.bench

        if u.path == "/":
            try:
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                return self._send(500, "ui.html missing: %s" % e, "text/plain")

        if u.path == "/state":
            return self._send(200, json.dumps(b.state()))

        if u.path == "/api/eye":
            if q.get("e") in ("left", "right"):
                b.eye = q["e"]
            return self._send(200, json.dumps({"eye": b.eye}))

        if u.path == "/api/roi":
            for k in ("cx", "cy", "size"):
                if k in q:
                    try:
                        b.roi[k] = max(0.02, min(0.98, float(q[k])))
                    except ValueError:
                        pass
            # Moving the ROI changes what is being measured, so every peak held
            # against the old framing is now meaningless - and the exposure was
            # metered ON the old ROI, so the lock notes would go on describing a
            # patch of the scene nobody is looking at any more.
            b.reset()
            if b.locked:
                threading.Thread(target=b.lock_camera, daemon=True).start()
            return self._send(200, json.dumps({"roi": b.roi}))

        if u.path == "/api/peaking":
            b.peaking = q.get("on") == "1"
            return self._send(200, json.dumps({"peaking": b.peaking}))

        if u.path == "/api/reset":
            b.reset()
            return self._send(200, json.dumps({"reset": True}))

        if u.path == "/api/remeter":
            try:
                cap = int(q["max"]) if "max" in q else None
            except ValueError:
                cap = None
            threading.Thread(target=b.lock_camera, kwargs={"max_exposure": cap},
                             daemon=True).start()
            return self._send(200, json.dumps({"remetering": True}))

        if u.path in ("/roi.mjpg", "/full.mjpg"):
            return self._stream("roi" if u.path == "/roi.mjpg" else "full")

        return self._send(404, "not found", "text/plain")

    def _stream(self, which):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        b = self.bench
        last = -1
        idle = 0
        try:
            while True:
                with b.cond:
                    fresh = b.cond.wait_for(lambda: b.seq != last, timeout=5.0)
                    last = b.seq
                    buf = b.roi_jpeg if which == "roi" else b.full_jpeg
                if not fresh:
                    # No new frame. Re-sending the last one keeps the stream
                    # alive AND makes a vanished client show up as a write
                    # error - without this these threads never write again, so
                    # they never notice the browser has gone and simply
                    # accumulate (36 of them were alive on one frozen run).
                    idle += 1
                    if buf is None or idle > 6:
                        break
                elif not buf:
                    continue
                else:
                    idle = 0
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(buf)).encode() +
                                 b"\r\n\r\n" + buf + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def already_serving(host, port):
    """True if a focusbarrel is already answering on this port."""
    h = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with urllib.request.urlopen("http://%s:%d/state" % (h, port), timeout=2) as r:
            json.loads(r.read() or b"{}")
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", default=None, help="/dev/videoN (default: first capture node)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--roi", type=float, default=0.30, help="ROI side as a fraction of the eye (default 0.30)")
    ap.add_argument("--no-lock", action="store_true", help="do not meter/lock the camera's auto modes")
    ap.add_argument("--max-exposure", type=int, default=500,
                    help="shutter cap in 100us units (default 500 = 50 ms)")
    ap.add_argument("--wb-k", type=int, default=None, help="force a white balance temperature")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    url = "http://%s:%d" % ("127.0.0.1" if a.host in ("0.0.0.0", "") else a.host, a.port)

    def open_browser():
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # Claim the port before touching the camera. The camera open and the
    # meter/lock have side effects on hardware another instance may be using, so
    # discovering the port clash afterwards is both silent and destructive.
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if already_serving(a.host, a.port):
            print("focusbarrel is already running on %s - opening it" % url, flush=True)
            if not a.no_browser:
                open_browser()
            return
        sys.exit("error: port %d is in use by something that is not focusbarrel. "
                 "Free it, or run with --port <other>." % a.port)

    devs = list_capture_devices()
    dev = a.device
    if not dev:
        if not devs:
            sys.exit("error: no capture device found (check: v4l2-ctl --list-devices)")
        dev = devs[0]["path"]
        label = devs[0]["label"]
        print("camera: %s  (%s)" % (label, dev), flush=True)
        if len(devs) > 1:
            print("other capture nodes: %s"
                  % ", ".join("%s %s" % (d["path"], d["label"]) for d in devs[1:]))
            print("(two identical cameras are indistinguishable by name - pick by "
                  "--device, or by USB port: %s)"
                  % ", ".join("%s=%s" % (d["path"], d["port"]) for d in devs))

    bench = Bench(dev, roi_size=a.roi)
    bench.max_exposure = bench.default_max_exposure = a.max_exposure
    bench.wb_k = a.wb_k
    if not bench.open():
        sys.exit("error: " + bench.err)
    bench.start()

    if a.no_lock:
        print("camera left on auto - readings will drift as the ISP re-meters", flush=True)
    else:
        print("metering the scene, then locking exposure/WB/gain/sharpness...", flush=True)
        bench.lock_camera()
        for n in bench.lock_notes:
            print("  note: %s" % n, flush=True)
        print("  locked: %s" % ", ".join("%s=%s" % kv for kv in sorted(bench.lock_got.items())))

    Handler.bench = bench
    print("\nfocusbarrel on %s   (ctrl-C to stop)" % url, flush=True)
    if not a.no_browser:
        open_browser()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        bench.stop()


if __name__ == "__main__":
    main()
