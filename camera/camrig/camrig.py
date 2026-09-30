#!/usr/bin/env python3
"""
camrig - browser control panel for UVC cameras.

Live MJPEG preview + every v4l2 control as a slider/number box, for up to
N cameras at once. Built for the DECXIN stereo cam (4000x1200 side-by-side)
but device-agnostic: controls are discovered from the hardware, not hardcoded.

    python3 camrig.py            # then open http://localhost:8088
    python3 camrig.py --port 9000 --no-browser
"""

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
except ImportError:
    sys.exit("camrig needs OpenCV: pip install opencv-python  (or apt install python3-opencv)")
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PRESET_FILE = os.path.join(HERE, "presets.json")
SNAP_DIR = os.path.expanduser("~/Desktop")
REC_DIR = os.path.expanduser("~/Videos/camrig")

# Controls whose payload we cannot express as a slider/toggle/dropdown.
UNSUPPORTED_TYPES = {"unknown"}
# Ratio above which a frame is assumed to be two eyes side-by-side.
STEREO_RATIO = 2.2

# Recording formats. H.264 mp4 is the default: a quarter the size of MJPG and
# the only option here that plays in a browser as well as in a video player.
# The fourcc is 'avc1' rather than 'H264' because the latter makes ffmpeg print
# a "tag not supported, falling back to avc1" warning on every single recording.
# MJPG stays available as one JPEG per frame - no interframe smear on the fine
# detail you are usually tuning for, at roughly 300 MB/min at 4000x1200.
CODECS = {
    "avc1": {"ext": ".mp4", "label": "MP4 H.264 · plays anywhere"},
    "MJPG": {"ext": ".avi", "label": "MJPG avi · every frame intact"},
}
DEFAULT_CODEC = "avc1"
# Stop a recording rather than run the filesystem down to its last byte.
MIN_FREE_MB = 400


# ---------------------------------------------------------------- v4l2 shim

def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def device_holders(dev):
    """Names of *other* processes holding an fd on dev, so we can explain EBUSY.

    Our own pid is filtered out: once camrig opens a camera it shows up in fuser
    too, and reporting that back would claim the device is blocked by something
    when it is simply in use by this very server.
    """
    rc, out, _ = _run(["fuser", dev])
    names, me = [], str(os.getpid())
    for pid in out.split():
        pid = pid.strip()
        if pid == me:
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                names.append("%s (pid %s)" % (f.read().strip(), pid))
        except OSError:
            pass
    return names


def list_devices(quick=False):
    """Every /dev/videoN that can actually capture video (skips metadata nodes).

    quick=True skips format enumeration and the busy-holder lookup. It exists so
    the UI can cheaply poll for a camera being plugged in or pulled out without
    spawning a v4l2-ctl per format list every few seconds.
    """
    devs = []
    rc, out, _ = _run(["v4l2-ctl", "--list-devices"])
    label = ""
    seen = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")):
            label = line.split("(")[0].strip().rstrip(":")
        elif line.strip().startswith("/dev/video"):
            seen.append((line.strip(), label))
    if not seen:  # fall back to globbing if --list-devices came up empty
        import glob
        seen = [(p, "camera") for p in sorted(glob.glob("/dev/video*"))]

    for path, label in seen:
        rc, info, _ = _run(["v4l2-ctl", "-d", path, "--info"])
        caps = info.split("Device Caps")[-1] if "Device Caps" in info else ""
        if "Video Capture" not in caps:
            continue  # metadata-only node
        # v4l2 repeats the card name ("DECXIN Camera: DECXIN Camera") - collapse it
        label = label or path
        half = label.split(":")
        if len(half) == 2 and half[0].strip() == half[1].strip():
            label = half[0].strip()
        # Two identical cameras are indistinguishable by name, so carry the USB
        # port through: it is the only thing that maps a panel to real hardware.
        bm = re.search(r"Bus info\s*:\s*(\S+)", info)
        bus = bm.group(1) if bm else ""
        # usb-0000:66:00.3-1 -> 66:00.3-1  (drop the usb- prefix and PCI domain)
        port = re.sub(r"^usb-(?:[0-9a-f]{4}:)?", "", bus)
        d = {"path": path, "label": label, "bus": bus, "port": port}
        if not quick:
            d["formats"] = list_formats(path)
            d["busy_by"] = device_holders(path)
        devs.append(d)
    return devs


def list_formats(dev):
    """[{fourcc, width, height, fps:[...]}, ...] from --list-formats-ext."""
    rc, out, _ = _run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    modes, fourcc, cur = [], None, None
    for line in out.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            fourcc = m.group(1)
            continue
        m = re.search(r"Size:\s*Discrete\s+(\d+)x(\d+)", line)
        if m and fourcc:
            cur = {"fourcc": fourcc, "width": int(m.group(1)),
                   "height": int(m.group(2)), "fps": []}
            modes.append(cur)
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and cur:
            f = float(m.group(1))
            if f not in cur["fps"]:
                cur["fps"].append(f)
    for m in modes:
        m["fps"].sort(reverse=True)
    return modes


_CTRL_RE = re.compile(
    r"^\s*(?P<name>\w+)\s+(?P<id>0x[0-9a-fA-F]+)\s+\((?P<type>[\w-]+)\)\s*:\s*(?P<rest>.*)$")
_MENU_RE = re.compile(r"^\s+(?P<val>-?\d+)\s*:\s*(?P<label>.+)$")


def get_controls(dev):
    """Parse --list-ctrls-menus into structured, UI-renderable control dicts."""
    rc, out, err = _run(["v4l2-ctl", "-d", dev, "--list-ctrls-menus"])
    controls, group, last = [], "Controls", None
    for line in out.splitlines():
        if not line.strip():
            continue
        m = _CTRL_RE.match(line)
        if m:
            rest = m.group("rest")
            flags = []
            fm = re.search(r"flags=([^\n]*)$", rest)
            if fm:
                for tok in fm.group(1).split(","):
                    tok = tok.strip()
                    if tok and not tok.startswith("0x"):
                        flags.append(tok)
                rest = rest[:fm.start()]
            kv = dict(re.findall(r"(\w+)=(-?(?:0x)?[0-9a-fA-F]+)", rest))

            def num(key):
                v = kv.get(key)
                if v is None:
                    return None
                try:
                    return int(v, 16) if v.startswith("0x") else int(v)
                except ValueError:
                    return None

            ctype = m.group("type")
            ctrl = {
                "name": m.group("name"),
                "id": m.group("id"),
                "type": ctype,
                "group": group,
                "min": num("min"),
                "max": num("max"),
                "step": num("step") or 1,
                "default": num("default"),
                "value": num("value"),
                "flags": flags,
                "menu": [],
                "writable": ctype not in UNSUPPORTED_TYPES
                            and "read-only" not in flags
                            and "has-payload" not in flags,
            }
            if ctype == "bool":
                ctrl["min"], ctrl["max"], ctrl["step"] = 0, 1, 1
            if ctrl["min"] is None and ctrl["max"] is not None:
                ctrl["min"] = 0          # bitmask controls report max but no min
            if ctrl["min"] is None or ctrl["max"] is None:
                ctrl["writable"] = False  # no range to render a slider from
            controls.append(ctrl)
            last = ctrl if ctype == "menu" else None
            continue
        mm = _MENU_RE.match(line)
        if mm and last is not None:
            last["menu"].append({"value": int(mm.group("val")),
                                 "label": mm.group("label").strip()})
            continue
        if not line.startswith(("\t", " ")) and line.strip().endswith("Controls"):
            group = line.strip()
    return controls


def set_control(dev, name, value):
    rc, out, err = _run(["v4l2-ctl", "-d", dev, "--set-ctrl", "%s=%s" % (name, value)])
    msg = (err or out).strip()
    return rc == 0 and "error" not in msg.lower(), msg


# dependent control -> (gating auto control, value that UNLOCKS the dependent).
# Mirrors GATES in ui.html.
GATE_MAP = {
    "exposure_time_absolute":    ("auto_exposure", 1),
    "exposure_absolute":         ("exposure_auto", 1),
    "focus_absolute":            ("focus_automatic_continuous", 0),
    "white_balance_temperature": ("white_balance_automatic", 0),
    "iso_sensitivity":           ("iso_sensitivity_auto", 0),
}


def apply_values(dev, values):
    """Apply many controls at once, in three passes.

    A control like exposure_time_absolute is read-only while auto_exposure owns
    it, so a naive loop fails with EACCES on exactly the values that matter.
    Unlock the gating auto modes first, write everything, then set the gates to
    whatever the caller actually asked for. That makes both directions work:
    going manual, and resetting to defaults (where the defaults re-enable auto).
    """
    ctrls = {c["name"]: c for c in get_controls(dev)}
    vals = {k: v for k, v in values.items()
            if k in ctrls and ctrls[k]["writable"] and v is not None}

    unlock = {}
    for dep, (gate, need) in GATE_MAP.items():
        if dep in vals and gate in ctrls:
            unlock[gate] = need

    errors, warnings = [], []

    def write(name, val):
        ok, msg = set_control(dev, name, val)
        if ok:
            return
        line = msg.splitlines()[-1].strip() if msg else "failed"
        # EACCES means an auto mode owns the control - expected, not a failure.
        bucket = warnings if "permission denied" in line.lower() else errors
        bucket.append("%s: %s" % (name, line))

    for gate, need in unlock.items():
        write(gate, need)
    for name, val in vals.items():
        if name not in unlock:
            write(name, val)
    for gate in unlock:
        if gate in vals:
            write(gate, vals[gate])
    return errors, warnings


# ------------------------------------------------------------ frame grabber

class Camera:
    """Owns one cv2 capture in a background thread; hands out latest frame."""

    def __init__(self, path):
        self.path = path
        self.cap = None
        self.thread = None
        self.running = False
        self.lock = threading.Condition()
        self.frame = None            # BGR frame, only in fallback (non-raw) mode
        self.jpeg = None             # latest compressed buffer, in raw mode
        self.raw = False             # True once the driver agrees to hand us JPEG
        self.seq = 0
        self._dec = {}               # (seq, flag) -> decoded image, one per frame
        self._declock = threading.Lock()
        self.error = ""
        self.width = self.height = 0
        self.fourcc = ""
        self.fps_target = 0.0
        self.fps_actual = 0.0
        self.stereo = False
        self.metrics = {}
        self.rec = None          # Recorder, if one is running or just finished
        self._times = []

    # -- lifecycle ---------------------------------------------------------
    def open(self, width=None, height=None, fps=None, fourcc="MJPG"):
        self.close()
        cap = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        if not cap.isOpened():
            holders = device_holders(self.path)
            self.error = ("device busy - held by " + ", ".join(holders)) if holders \
                else "could not open device"
            cap.release()
            return False
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        if fps:
            cap.set(cv2.CAP_PROP_FPS, float(fps))
        # BUFFERSIZE=1 leaves the driver nothing to fill while we handle the
        # current frame, so capture and USB transfer cannot overlap: measured
        # 31 fps against 60.1 fps with two buffers, on the same camera and the
        # same code path. The cost is one extra frame of latency - 16.7 ms at
        # 60 fps - which is invisible for a tuning panel.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        # Ask for the compressed buffer rather than a decoded BGR frame. On an
        # MJPEG camera the driver then hands us the JPEG bytes untouched, so the
        # capture loop does no decoding at all and only consumers that actually
        # need pixels pay for it - at 1/2 or 1/4 scale where that is enough.
        # Drivers that ignore this, and non-MJPEG cameras, return a normal 3-D
        # frame; we detect that below and keep the original behaviour.
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            self.error = "opened but returned no frames"
            return False

        # A raw buffer arrives as 1-D/2-D uint8 starting with the JPEG SOI
        # marker; a decoded frame is 3-D. Verify it really decodes before
        # committing, so a half-supported driver cannot leave us blind.
        self.raw = frame.ndim <= 2 and bytes(frame.flatten()[:2]) == b"\xff\xd8"
        probe = cv2.imdecode(frame, cv2.IMREAD_COLOR) if self.raw else frame
        if probe is None:
            cap.release()
            self.error = "raw MJPEG buffer did not decode"
            return False

        self.cap = cap
        self.error = ""
        self.height, self.width = probe.shape[:2]
        self.stereo = self.width / max(self.height, 1) > STEREO_RATIO
        self.fps_target = cap.get(cv2.CAP_PROP_FPS) or 0.0
        raw = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.fourcc = "".join(chr((raw >> (8 * i)) & 0xFF) for i in range(4)).strip() or "?"
        if self.raw:
            self.jpeg, self.frame = frame, None
        else:
            self.jpeg, self.frame = None, frame
        self.seq = 1
        self._dec = {}
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def close(self):
        # Finalize any recording before the frames stop coming, so re-opening at
        # a new mode cannot leave a half-written file behind.
        if self.rec is not None and self.rec.running:
            self.rec.stop()
        self.running = False
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self.thread = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        with self.lock:
            self.frame = None
            self.jpeg = None
            self.lock.notify_all()
        with self._declock:
            self._dec = {}
        self._times = []
        self.fps_actual = 0.0
        self.metrics = {}

    # -- capture loop ------------------------------------------------------
    def _loop(self):
        misses, n = 0, 0
        while self.running:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                misses += 1
                if misses > 30:
                    self.error = "stream stalled (camera unplugged?)"
                    self.running = False
                    break
                time.sleep(0.02)
                continue
            misses = 0
            now = time.time()
            self._times.append(now)
            if len(self._times) > 30:
                self._times.pop(0)
            if len(self._times) > 1:
                span = self._times[-1] - self._times[0]
                self.fps_actual = (len(self._times) - 1) / span if span > 0 else 0.0
            n += 1
            if n % 5 == 0:
                self._measure(frame)
            with self.lock:
                if self.raw:
                    self.jpeg = frame
                else:
                    self.frame = frame
                self.seq += 1
                self.lock.notify_all()

    def _measure(self, frame):
        """Cheap exposure/focus readouts so you can see if a tweak helped.

        In raw mode this decodes at 1/4 scale: libjpeg skips DCT coefficients
        rather than decoding and throwing pixels away, which measured free
        against no decode at all. Absolute sharpness numbers shift slightly
        versus decoding full-res first, but only the left/right ratio is ever
        compared, and that is unaffected.
        """
        if self.raw:
            frame = cv2.imdecode(frame, cv2.IMREAD_REDUCED_COLOR_4)
            if frame is None:
                return
        small = cv2.resize(frame, (320, max(1, int(320 * frame.shape[0] / frame.shape[1]))),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        m = {
            "brightness": round(float(gray.mean()), 1),
            "contrast": round(float(gray.std()), 1),
            "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
            "clipped": round(float((gray >= 254).mean() * 100), 2),
        }
        if self.stereo:
            half = gray.shape[1] // 2
            ls = float(cv2.Laplacian(gray[:, :half], cv2.CV_64F).var())
            rs = float(cv2.Laplacian(gray[:, half:], cv2.CV_64F).var())
            m["left_sharpness"] = round(ls, 1)
            m["right_sharpness"] = round(rs, 1)
            m["lr_ratio"] = round(ls / rs, 3) if rs > 0.01 else None
        self.metrics = m

    # -- frame access ------------------------------------------------------
    # Reduction flags, largest image first. libjpeg can emit 1/2, 1/4 or 1/8
    # straight out of the DCT, so a consumer that only needs 900 px never pays
    # to decode 4.8 Mpixel.
    _REDUCE = ((1, cv2.IMREAD_COLOR),
               (2, cv2.IMREAD_REDUCED_COLOR_2),
               (4, cv2.IMREAD_REDUCED_COLOR_4),
               (8, cv2.IMREAD_REDUCED_COLOR_8))

    def _flag_for(self, min_width):
        """Smallest decode that still yields at least min_width pixels across."""
        if not min_width or not self.width:
            return 1, cv2.IMREAD_COLOR
        best = (1, cv2.IMREAD_COLOR)
        for div, flag in self._REDUCE:
            if self.width // div >= min_width:
                best = (div, flag)
        return best

    def _decode(self, buf, seq, min_width=0):
        """Decode the current frame, at most once per (frame, scale) however
        many consumers ask - stream, recorder and snapshot share the result."""
        if not self.raw:
            return buf
        div, flag = self._flag_for(min_width)
        key = (seq, div)
        with self._declock:
            hit = self._dec.get(key)
            if hit is not None:
                return hit
        img = cv2.imdecode(buf, flag)       # outside the lock: this is the slow part
        if img is None:
            return None
        with self._declock:
            if len(self._dec) > 8:          # only the newest frames matter
                self._dec.clear()
            self._dec[key] = img
        return img

    def wait_frame(self, last_seq, timeout=5.0, min_width=0):
        """Latest frame as BGR. min_width lets a caller that is going to shrink
        the image anyway (the preview) get a cheap reduced decode instead."""
        with self.lock:
            if self.seq == last_seq:
                self.lock.wait(timeout)
            buf = self.jpeg if self.raw else self.frame
            seq = self.seq
        if buf is None:
            return None, seq
        return self._decode(buf, seq, min_width), seq

    def snapshot(self):
        with self.lock:
            buf = self.jpeg if self.raw else self.frame
            seq = self.seq
        if buf is None:
            return None
        img = self._decode(buf, seq)        # snapshots always want full resolution
        return None if img is None else img.copy()

    def status(self):
        return {
            "path": self.path, "open": self.running, "error": self.error,
            "width": self.width, "height": self.height, "fourcc": self.fourcc,
            "fps_target": round(self.fps_target, 1), "fps_actual": round(self.fps_actual, 1),
            "stereo": self.stereo, "metrics": self.metrics,
            "raw_mjpeg": self.raw,
            "rec": self.rec.status() if self.rec is not None else None,
        }


CAMS = {}
CAMS_LOCK = threading.Lock()


def get_cam(path):
    with CAMS_LOCK:
        if path not in CAMS:
            CAMS[path] = Camera(path)
        return CAMS[path]


_SEAM_CACHE = {}


def find_seam(frame):
    """(strip_px, seam_x) for a stereo frame, measured rather than assumed.

    A DECXIN frame is NOT two equal halves. It is a narrow data strip, then the left eye, then
    the right eye - on a 4000 px frame that is 158 / 1921 / 1921, so the boundary sits at 2079,
    not at 2000. Splitting down the middle put the IMU strip inside the "left eye" and the last
    79 px of the left eye inside the "right eye", which is visible as a sliver of the wrong
    view down the edge of each crop, and was being recorded that way too.

    The seam is found where adjacent columns stop resembling each other: within one eye a
    column looks much like its neighbour, but across the boundary they are different images.
    The strip then follows from the eyes being equal width: strip = 2*seam - width.
    """
    key = (frame.shape[1], frame.shape[0])
    hit = _SEAM_CACHE.get(key)
    if hit is not None:
        return hit
    g = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    g = g.astype(np.float32)
    w = g.shape[1]
    best = None
    for x in range(int(w * 0.46), int(w * 0.56)):
        a_, b_ = g[:, x - 1], g[:, x]
        sa, sb = a_.std(), b_.std()
        if sa < 1e-3 or sb < 1e-3:
            continue
        c = float(((a_ - a_.mean()) * (b_ - b_.mean())).mean() / (sa * sb))
        if best is None or c < best[0]:
            best = (c, x)
    if best is None:
        out = (0, w // 2)
    else:
        seam = best[1]
        strip = 2 * seam - w
        # a strip outside this range means the scan latched onto something else; fall back to
        # halves rather than return a crop that is confidently wrong
        out = (strip, seam) if 0 <= strip <= w * 0.12 else (0, w // 2)
    _SEAM_CACHE[key] = out
    return out


def crop_eye(frame, eye):
    if eye in ("left", "right") and frame is not None:
        strip, seam = find_seam(frame)
        ew = min(seam - strip, frame.shape[1] - seam)
        return (frame[:, strip:strip + ew] if eye == "left"
                else frame[:, seam:seam + ew])
    return frame


# --------------------------------------------------------------- recording

def free_mb(path):
    """Free megabytes on the filesystem holding path (or its nearest parent)."""
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        st = os.statvfs(path or "/")
        return st.f_bavail * st.f_frsize / 1e6
    except OSError:
        return float("inf")


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class FfmpegWriter:
    """H.264 via the ffmpeg BINARY, for when OpenCV's own writer refuses.

    OpenCV 5.0's FFMPEG backend resolves H.264 to the *hardware* encoder
    h264_v4l2m2m and, when there is no mem2mem device, fails outright:

        [h264_v4l2m2m] Could not find a valid device
        [h264_v4l2m2m] can't configure encoder
        VIDEOIO/FFMPEG: Failed to initialize VideoWriter

    It never falls back to software libx264, and neither
    VIDEOWRITER_PROP_HW_ACCELERATION=NONE nor
    OPENCV_FFMPEG_WRITER_OPTIONS=video_codec;libx264 changes its mind - both were
    tried and both still fail. Measured on this laptop: avc1/H264/h264/X264 all
    refuse at 4000x1200 and at 1920x1080, while mp4v and MJPG open fine, and the
    system ffmpeg has libx264 available the whole time.

    So the frames go down a pipe to ffmpeg instead. This does NOT contradict the
    note on Recorder about not handing recording to ffmpeg: that is about
    CAPTURE, which stays here because only one process can hold a /dev/video*
    node. ffmpeg is only the encoder, and it reads raw frames from stdin.

    Same three methods the cv2 writer exposes, so the writer thread cannot tell
    the difference.
    """

    def __init__(self, path, w, h, fps):
        self.path, self.proc, self.err = path, None, ""
        if not shutil.which("ffmpeg"):
            self.err = "ffmpeg is not installed"
            return
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
               "-r", f"{fps}", "-i", "-", "-an",
               # ultrafast because this has to keep up with a 4000x1200 live
               # stream in real time; the point of the recording is to look at
               # detail, so quality is protected with a low CRF instead.
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
               "-pix_fmt", "yuv420p", path]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            self.err = str(e)
            self.proc = None

    def isOpened(self):
        return self.proc is not None and self.proc.poll() is None

    def write(self, img):
        if self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(img.tobytes())
        except (BrokenPipeError, OSError) as e:
            self.err = self.err or f"ffmpeg stopped accepting frames: {e}"
            self.proc = None

    def release(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=20)          # let it write the moov atom
        except subprocess.TimeoutExpired:
            p.kill()
        if p.returncode not in (0, None) and not self.err:
            err = (p.stderr.read().decode("utf-8", "replace").strip()
                   if p.stderr else "")
            self.err = f"ffmpeg exited {p.returncode}: {err[:200]}"


class Recorder:
    """Writes one Camera's frames to a video file, on a wall-clock timeline.

    One frame goes out per slot at a fixed rate, always the newest frame there
    is. If capture hiccups the previous frame is repeated rather than the file
    quietly getting shorter, so 30 s of recording is a 30 s file that stays in
    step with what you were watching.

    Full sensor resolution, independent of the preview's downscale - the only
    thing it takes from the UI is which eye to keep.

    Recording cannot be handed off to ffmpeg: only one process can hold a
    /dev/video* node and camrig is already holding it, so frames have to come
    from the capture thread that is already running.
    """

    def __init__(self, cam, eye="full", codec=DEFAULT_CODEC, fps=None):
        self.cam = cam
        self.eye = eye if eye in ("left", "right") else "full"
        self.codec = codec if codec in CODECS else DEFAULT_CODEC
        # The camera has been streaming, so its measured rate is the honest
        # target; asking for the mode's nominal fps would stretch the timeline.
        want = fps or cam.fps_actual or cam.fps_target or 15.0
        self.fps = round(min(max(float(want), 1.0), 120.0), 2)
        self.path = ""
        self.writer = None
        self.thread = None
        self.running = False
        self.error = ""
        self.shape = None
        self.frames = self.dups = self.skipped = 0
        self.size = 0
        self.t0 = 0.0
        self.t_end = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        frame = self.cam.snapshot()
        if frame is None:
            return False, "no frames from the camera yet"
        img = crop_eye(frame, self.eye)
        h, w = img.shape[:2]
        free = free_mb(REC_DIR)
        if free < MIN_FREE_MB:
            return False, "only %.0f MB free - need at least %d MB" % (free, MIN_FREE_MB)
        try:
            os.makedirs(REC_DIR, exist_ok=True)
        except OSError as e:
            return False, "cannot create %s: %s" % (REC_DIR, e)

        suffix = "" if self.eye == "full" else "-" + self.eye
        name = "camrig-%s%s-%s%s" % (os.path.basename(self.cam.path), suffix,
                                     time.strftime("%Y%m%d-%H%M%S"),
                                     CODECS[self.codec]["ext"])
        self.path = os.path.join(REC_DIR, name)
        writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*self.codec),
                                 self.fps, (w, h))
        self.via_ffmpeg = False
        if not writer.isOpened():
            # A refused encoder still leaves an empty file behind - don't litter
            # the recordings folder with 0-byte stubs.
            writer.release()
            if file_size(self.path) == 0:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            # OpenCV refusing H.264 is the NORMAL case on a machine with no
            # mem2mem encoder, not a broken install - see FfmpegWriter. Pipe to
            # the ffmpeg binary rather than telling the operator to pick another
            # codec, because H.264 is the only option here that plays in a
            # browser as well as a player.
            if self.codec in ("avc1", "H264", "h264", "X264"):
                fw = FfmpegWriter(self.path, w, h, self.fps)
                if fw.isOpened():
                    writer, self.via_ffmpeg = fw, True
                else:
                    return False, ("no %s encoder available for %dx%d, and the ffmpeg "
                                   "fallback could not start: %s"
                                   % (self.codec, w, h, fw.err or "unknown"))
            else:
                return False, "no %s encoder available for %dx%d" % (self.codec, w, h)

        self.writer = writer
        self.shape = img.shape
        self.t0 = time.monotonic()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True, ""

    def stop(self):
        """Signal the writer thread and wait for it to finalize the file."""
        self.running = False
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=6.0)
        if not self.t_end:                       # thread never got going
            self.t_end = time.monotonic()
            self._release()
        return self.status()

    def _release(self):
        if self.writer is not None:
            self.writer.release()               # writes the index/moov atom
            self.writer = None
        self.size = file_size(self.path)

    # -- writer thread -----------------------------------------------------
    def _loop(self):
        interval = 1.0 / self.fps
        last_seq, since_check = -1, 0
        try:
            while self.running and self.cam.running:
                due = self.t0 + self.frames * interval
                now = time.monotonic()
                if now < due:
                    time.sleep(min(due - now, 0.02))
                    continue
                # Returns at once if a frame we have not written yet is waiting,
                # otherwise gives capture a fraction of a slot to deliver one
                # before we pad with a repeat. Without that grace a slot landing
                # just before a frame costs a duplicate *and* skips the frame
                # that arrived a millisecond later - measured 15 of each per 8 s
                # on a DECXIN, versus 0 and 1 with it.
                frame, seq = self.cam.wait_frame(last_seq, timeout=interval * 0.5)
                if frame is None:
                    time.sleep(0.01)
                    continue
                img = crop_eye(frame, self.eye)
                if img.shape != self.shape:
                    self.error = "frame size changed mid-recording (%dx%d to %dx%d)" % (
                        self.shape[1], self.shape[0], img.shape[1], img.shape[0])
                    break
                self.writer.write(img)
                self.frames += 1
                if seq == last_seq:
                    self.dups += 1              # capture stalled: held the frame
                elif last_seq >= 0:
                    self.skipped += max(0, seq - last_seq - 1)
                last_seq = seq

                since_check += 1
                if since_check >= 30:
                    since_check = 0
                    self.size = file_size(self.path)
                    if free_mb(REC_DIR) < MIN_FREE_MB:
                        self.error = "disk nearly full (under %d MB free)" % MIN_FREE_MB
                        break
            else:
                if not self.cam.running and not self.error:
                    self.error = self.cam.error or "camera closed"
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.running = False
            self.t_end = time.monotonic()
            self._release()

    # -- readout -----------------------------------------------------------
    def elapsed(self):
        return max(0.0, (self.t_end or time.monotonic()) - self.t0)

    def status(self):
        el = self.elapsed()
        # Frames written / header fps is how long the file claims to be. Short
        # of wall clock means the encoder could not keep up and playback runs
        # fast - worth saying out loud instead of shipping a silently sped-up clip.
        drift = 0.0
        if el > 1.0:
            drift = max(0.0, (el - self.frames / self.fps) / el)
        return {
            "active": self.running,
            "path": self.path,
            "file": os.path.basename(self.path),
            "eye": self.eye,
            "codec": self.codec,
            "fps": self.fps,
            "frames": self.frames,
            "dups": self.dups,
            "skipped": self.skipped,
            "seconds": round(el, 1),
            "bytes": self.size if not self.running else file_size(self.path),
            "drift": round(drift, 3),
            "error": self.error,
        }


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "camrig"

    def log_message(self, fmt, *a):
        pass  # keep the console clean; errors still surface in the UI

    # -- helpers -----------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        r = u.path
        try:
            if r == "/":
                return self._file(os.path.join(HERE, "ui.html"), "text/html; charset=utf-8")
            if r == "/api/devices":
                return self._json({"devices": list_devices(quick=q.get("quick") == "1")})
            if r == "/api/controls":
                dev = q.get("dev", "")
                return self._json({"controls": get_controls(dev),
                                   "status": get_cam(dev).status()})
            if r == "/api/status":
                devs = [d for d in q.get("devs", "").split(",") if d]
                return self._json({"status": {d: get_cam(d).status() for d in devs}})
            if r == "/api/presets":
                return self._json({"presets": load_presets()})
            if r == "/api/config":
                return self._json({
                    "recdir": REC_DIR, "snapdir": SNAP_DIR,
                    "default_codec": DEFAULT_CODEC,
                    "codecs": [{"id": k, "label": v["label"]} for k, v in CODECS.items()],
                })
            if r == "/stream":
                return self.stream(q)
            if r == "/snapshot":
                return self.snapshot(q)
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # never let one bad request kill the server
            try:
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        r, dev = u.path, body.get("dev", "")
        try:
            if r == "/api/open":
                cam = get_cam(dev)
                ok = cam.open(body.get("width"), body.get("height"),
                              body.get("fps"), body.get("fourcc") or "MJPG")
                return self._json({"ok": ok, "status": cam.status(),
                                   "controls": get_controls(dev)})
            if r == "/api/close":
                get_cam(dev).close()
                return self._json({"ok": True, "status": get_cam(dev).status()})
            if r == "/api/set":
                ok, msg = set_control(dev, body["name"], body["value"])
                return self._json({"ok": ok, "message": msg,
                                   "controls": get_controls(dev)})
            if r == "/api/apply":
                errors, warnings = apply_values(dev, body.get("values", {}))
                return self._json({"ok": not errors, "errors": errors,
                                   "warnings": warnings, "controls": get_controls(dev)})
            if r == "/api/reset":
                vals = {c["name"]: c["default"] for c in get_controls(dev)
                        if c["writable"] and c["default"] is not None}
                errors, warnings = apply_values(dev, vals)
                return self._json({"ok": not errors, "errors": errors,
                                   "warnings": warnings, "controls": get_controls(dev)})
            if r == "/api/record/start":
                cam = get_cam(dev)
                if not cam.running:
                    return self._json({"error": "open the camera first"}, 409)
                if cam.rec is not None and cam.rec.running:
                    return self._json({"error": "already recording"}, 409)
                rec = Recorder(cam, eye=body.get("eye", "full"),
                               codec=body.get("codec", DEFAULT_CODEC))
                ok, msg = rec.start()
                if not ok:
                    return self._json({"error": "cannot record: " + msg}, 500)
                cam.rec = rec
                return self._json({"ok": True, "rec": rec.status(),
                                   "status": cam.status()})
            if r == "/api/record/stop":
                cam = get_cam(dev)
                if cam.rec is None:
                    return self._json({"error": "not recording"}, 409)
                st = cam.rec.stop()
                return self._json({"ok": True, "rec": st, "status": cam.status()})
            if r == "/api/reveal":
                # Local tool: pop the recordings folder open in the file manager.
                if not os.path.isdir(REC_DIR):
                    try:
                        os.makedirs(REC_DIR, exist_ok=True)
                    except OSError as e:
                        return self._json({"error": str(e)}, 500)
                rc, out, err = _run(["xdg-open", REC_DIR], timeout=5)
                if rc != 0:
                    return self._json({"error": (err or "xdg-open failed").strip(),
                                       "path": REC_DIR}, 500)
                return self._json({"ok": True, "path": REC_DIR})
            if r == "/api/preset/save":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json({"error": "preset needs a name"}, 400)
                p = load_presets()
                p[name] = {c["name"]: c["value"] for c in get_controls(dev)
                           if c["writable"] and c["value"] is not None}
                save_presets(p)
                return self._json({"ok": True, "presets": p})
            if r == "/api/preset/delete":
                p = load_presets()
                p.pop(body.get("name", ""), None)
                save_presets(p)
                return self._json({"ok": True, "presets": p})
            self.send_error(404)
        except KeyError as e:
            self._json({"error": "missing field %s" % e}, 400)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # -- media -------------------------------------------------------------
    def stream(self, q):
        dev = q.get("dev", "")
        eye = q.get("eye", "full")
        width = int(q.get("w", 900))
        quality = int(q.get("q", 72))
        cap_fps = float(q.get("fps", 15))
        cam = get_cam(dev)
        if not cam.running:
            self.send_error(409, "camera not open")
            return

        boundary = "camrigframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=%s" % boundary)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()

        seq, interval, last = 0, 1.0 / max(cap_fps, 0.5), 0.0
        try:
            while cam.running:
                # The preview is about to be shrunk to `width` anyway, so ask for
                # a reduced decode. An eye crop halves the width first, so it
                # needs twice as many pixels to start with.
                #
                # width=0 means NATIVE: no downscale and no reduced decode, so the preview
                # is the sensor's own pixels. Worth having because the default 900 px at q72
                # is a quarter-scale decode re-compressed on top of the camera's own MJPEG,
                # and it looks materially softer than a recording of the same moment - which
                # reads as the camera being soft when it is only the preview that is.
                need = 0 if width == 0 else width * (2 if eye in ("left", "right") else 1)
                if width == 0:
                    need = cam.width or 0
                frame, seq = cam.wait_frame(seq, timeout=5.0, min_width=need)
                if frame is None:
                    continue
                now = time.time()
                if now - last < interval:
                    continue
                last = now
                img = crop_eye(frame, eye)
                if width and img.shape[1] > width:
                    h = max(1, int(width * img.shape[0] / img.shape[1]))
                    img = cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", img,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if not ok:
                    continue
                data = buf.tobytes()
                self.wfile.write(b"--%s\r\n" % boundary.encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(data))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def snapshot(self, q):
        dev = q.get("dev", "")
        eye = q.get("eye", "full")
        cam = get_cam(dev)
        frame = cam.snapshot()
        if frame is None:
            return self._json({"error": "no frame available"}, 409)
        img = crop_eye(frame, eye)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = os.path.basename(dev)
        suffix = "" if eye == "full" else "-" + eye
        name = "camrig-%s%s-%s.jpg" % (tag, suffix, stamp)
        path = os.path.join(SNAP_DIR, name)
        try:
            cv2.imwrite(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "path": path,
                           "size": "%dx%d" % (img.shape[1], img.shape[0])})


def load_presets():
    try:
        with open(PRESET_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_presets(p):
    with open(PRESET_FILE, "w") as f:
        json.dump(p, f, indent=2)


def already_serving(host, port):
    """True if a camrig is already on this port - so double-clicking the desktop
    icon a second time just focuses the tab instead of dying on EADDRINUSE."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/devices" % (host, port), timeout=2) as r:
            return b"devices" in r.read()
    except Exception:
        return False


def main():
    global REC_DIR
    ap = argparse.ArgumentParser(description="browser control panel for UVC cameras")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--rec-dir", default=REC_DIR,
                    help="folder Record writes video files to (default %(default)s)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    REC_DIR = os.path.expanduser(a.rec_dir)

    reach = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
    if already_serving(reach, a.port):
        print("camrig is already running on port %d - opening the browser" % a.port)
        if not a.no_browser:
            webbrowser.open("http://%s:%d" % (reach, a.port))
        return

    devs = list_devices()
    print("camrig - http://%s:%d" % (a.host, a.port))
    if devs:
        for d in devs:
            busy = "  [BUSY: %s]" % ", ".join(d["busy_by"]) if d["busy_by"] else ""
            modes = ", ".join(sorted({"%dx%d" % (m["width"], m["height"])
                                      for m in d["formats"]})) or "?"
            print("  %-14s %-28s %s%s" % (d["path"], d["label"][:28], modes, busy))
    else:
        print("  no capture devices found")
    print("recordings -> %s   snapshots -> %s" % (REC_DIR, SNAP_DIR))
    print("Ctrl-C to stop")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    # A plain kill (or closing the terminal) would otherwise leave a recording
    # in progress without its index - unplayable. Route both signals into the
    # same shutdown path as Ctrl-C so every open file gets finalized.
    def bail(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, bail)
    if not a.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open("http://%s:%d" % (a.host, a.port))).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        for c in list(CAMS.values()):
            c.close()
        srv.server_close()


if __name__ == "__main__":
    main()
