#!/usr/bin/env python3
"""camview.py - native 1:1 viewer for DECXIN cameras, as a small desktop app.

    python3 camview.py                  # every DECXIN, live; plug/unplug at will
    python3 camview.py --dev /dev/video2  # only that node
    python3 camview.py --solo           # open on the first camera instead of all of them
    python3 camview.py --fullscreen     # start full screen (the old default)
    python3 camview.py --take           # if camrig holds a camera, ask it to let go

THE APP

A sidebar lists every DECXIN that is plugged in, live, with its fps and USB link speed. Click
"All cameras" to see them all at once, or click one camera to view it alone. Cameras come and
go without a restart: pull one out and its tile says so (the others keep streaming), plug one
in and it shows up within a couple of seconds. A camera that drops off and comes back keeps
the zoom, pan and eye you had on it.

That last part is why this was rebuilt. The old viewer looked for cameras once, at launch, so
an unplugged camera's tile froze on its last frame, a new camera never showed up, and the only
way out was q and reopen.

WHY THIS EXISTS AT ALL

The browser preview cannot show you the sensor's pixels honestly. It downscales on the server,
re-encodes to JPEG on top of the camera's own MJPEG, and then the browser FITS a 4000 px frame
into a ~1200 px window. Even with the first two fixed, 60 fps of 4000x1200 at q100 is ~100 MB/s
through an HTTP stream the browser must decode per frame, so it drops frames, and dropped
frames look like a bad camera.

This path decodes once and draws. At 1:1 the frame is CROPPED, never resampled: one sensor
pixel is one screen pixel (device pixels, so this holds on a HiDPI panel too).

KEYS AND MOUSE

    scroll          zoom in/out around the pointer (fit -> 1:1 -> 2:1 -> 3:1 -> 4:1)
    double-click    fit <-> 1:1 at that spot
    drag            pan (when zoomed)
    1 / 2 / 3 / 4   zoom 1:1 .. 4:1          f  fit         r  reset the view
    e               both eyes -> left -> right
    TAB / Shift-TAB all cameras -> camera 1 -> camera 2 -> ...   a  all cameras
    s               save the full-resolution frame as a PNG (~/Videos/camrig)
    v               start / stop recording the full frame (~/Videos/camrig)
    d               camera controls back to driver defaults
    p               stop this camera's stream / resume it (frees it for another app)
    ± / w / F11     full screen on / off (move the pointer to the top edge for the toolbar)
    F9              show / hide the sidebar
    q               quit        Esc  leave full screen, or quit

WHY 60 FPS NEEDS A DECODE POOL ON A LAPTOP

A NanoPi decodes MJPEG in hardware (the Rockchip JPEG block), so 60 fps costs it nothing. This
laptop decodes in software, and one 4000x1200 frame takes 14.5 ms on one core - a single thread
tops out at ~60 fps with zero headroom, so reading AND decoding on the same thread (what the old
viewer did) fell short of 60 on any hiccup. So each camera's capture thread only pulls the raw
MJPEG off the driver, a pool of --decoders threads (default 3: ~170 frames/s measured) decodes
them in parallel, and a publisher puts them back IN ORDER for the screen and the recorder. If
the pool ever falls behind, frames are dropped and counted, never queued without bound.

The rate is ASKED for (--fps, default 60) because left unset the camera keeps whatever interval
it was last given, which was 30. Each tile shows the measured rate, not an assumed one.

ONE PROCESS AT A TIME owns a /dev/video* node. If camrig has a camera, its tile says so and
offers "Take it", which asks camrig to let go (--take does that automatically). The other way
round, "Stop stream" (p, the stop button in the toolbar or on the camera's sidebar row) closes
the camera completely so another app can open it, without quitting CamView; "Resume" takes it
back. A stopped camera stays stopped across a replug, so CamView never grabs it back from the
app you gave it to.

On exit, and for every camera that leaves while it is open, controls go back to driver
defaults: these units go to the field after the bench, and anything left pinned here would
follow them into service and quietly degrade their footage.
"""
from __future__ import annotations

import argparse
import atexit
import collections
import itertools
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# A pulled or wedged camera must not park a capture thread in OpenCV's default 10 s select().
# Read once, when the first capture is made, so it has to be set before cv2 is imported.
# (CAP_PROP_READ_TIMEOUT_MSEC exists in 4.6 too but the V4L backend ignores it.)
os.environ.setdefault("OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT", "1")

import cv2  # noqa: E402
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

# ---------------------------------------------------------------------------- old-distro shims
# Ubuntu 22.04 ships GTK 4.6, libadwaita 1.1 and GLib 2.72; the widgets below are newer
# (ToolbarView / OverlaySplitView 1.4, AlertDialog 1.5, Toast button-clicked 1.2,
# append_scaled_texture 4.10, ApplicationFlags.DEFAULT_FLAGS 2.74). Each gets a plain-GTK
# stand-in with the same few methods CamView calls. CAMVIEW_COMPAT=1 forces the stand-ins
# so they can be tested on a new system.
_COMPAT = os.environ.get("CAMVIEW_COMPAT") == "1"


def _adw_at_least(minor):
    return not _COMPAT and (Adw.get_major_version(), Adw.get_minor_version()) >= (1, minor)


APP_FLAGS = (Gio.ApplicationFlags.FLAGS_NONE if _COMPAT else
             getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE))
HAS_SCALED_TEXTURE = not _COMPAT and hasattr(Gtk.Snapshot, "append_scaled_texture")
TOAST_HAS_BUTTON_SIGNAL = _adw_at_least(2)


class _ToolbarViewCompat(Gtk.Box):
    """Adw.ToolbarView stand-in: header bars on top (hideable), content below."""

    def __init__(self, **_kw):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._rev = Gtk.Revealer(child=self._bars, reveal_child=True,
                                 transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.append(self._rev)
        self._content = None

    def add_top_bar(self, w):
        self._bars.append(w)

    def set_content(self, w):
        if self._content is not None:
            self.remove(self._content)
        self._content = w
        w.set_vexpand(True)
        self.append(w)

    def set_reveal_top_bars(self, v):
        self._rev.set_reveal_child(v)

    def get_reveal_top_bars(self):
        return self._rev.get_reveal_child()


class _SplitViewCompat(Gtk.Box):
    """Adw.OverlaySplitView stand-in: a fixed-width sidebar that can be hidden."""

    show_sidebar = GObject.Property(type=bool, default=True)

    def __init__(self, sidebar, content, min_sidebar_width=230, max_sidebar_width=290, **_kw):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar.set_size_request(min(260, max_sidebar_width), -1)
        sidebar.set_vexpand(True)
        self._side = Gtk.Box()
        self._side.append(sidebar)
        self._side.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        content.set_hexpand(True)
        self.append(self._side)
        self.append(content)
        self.connect("notify::show-sidebar",
                     lambda *_: self._side.set_visible(self.show_sidebar))

    def get_show_sidebar(self):
        return self.show_sidebar

    def set_show_sidebar(self, v):
        self.show_sidebar = bool(v)


ToolbarView = Adw.ToolbarView if _adw_at_least(4) else _ToolbarViewCompat
SplitView = Adw.OverlaySplitView if _adw_at_least(4) else _SplitViewCompat


def info_dialog(parent, heading, body_markup):
    """Adw.AlertDialog (1.5) -> Adw.MessageDialog (1.2) -> Gtk.MessageDialog."""
    if _adw_at_least(5):
        d = Adw.AlertDialog(heading=heading, body=body_markup, body_use_markup=True)
        d.add_response("ok", "Close")
        d.present(parent)
    elif _adw_at_least(2):
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body_markup,
                              body_use_markup=True)
        d.add_response("ok", "Close")
        d.present()
    else:
        d = Gtk.MessageDialog(transient_for=parent, modal=True, text=heading,
                              secondary_text=body_markup, secondary_use_markup=True,
                              buttons=Gtk.ButtonsType.CLOSE)
        d.connect("response", lambda dlg, *_: dlg.destroy())
        d.present()


def load_css(provider, css):
    # GTK >= 4.9 takes (str, length); 4.6 takes the bytes alone.
    try:
        provider.load_from_data(css, len(css))
    except TypeError:
        provider.load_from_data(css.encode())

HERE =os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import camrig                      # measured eye seam, ffmpeg writer, 3-pass control apply
except Exception as _e:                # the viewer still works without it, just less well
    camrig = None
    print(f"  camrig.py not importable ({_e}): eyes split at the midpoint, no recording")

APP_ID = os.environ.get("CAMVIEW_APP_ID", "ai.starpilot.CamView")   # override = test copy
SYS = "/sys/class/video4linux"
SAVE_DIR = os.path.expanduser("~/Videos/camrig")
ZOOMS = (0.0, 1.0, 2.0, 3.0, 4.0)      # 0 = fit
BG = 14                                # stage grey, matches the CSS below
GONE_GRACE_S = 6.0                     # how long a pulled camera's tile says "disconnected"


# ---------------------------------------------------------------------------- camera helpers

def crop_eye(frame, eye):
    """One eye, using camrig's measured seam; halves only if camrig is not importable.

    Splitting down the middle is wrong on a DECXIN: the frame is a 158 px data strip then two
    1921 px eyes, so the boundary is at 2079. A midpoint split put the IMU strip inside the
    left eye and a slice of the left eye down the edge of the right one.
    """
    if eye == "full" or frame is None:
        return frame
    if camrig is not None:
        try:
            return camrig.crop_eye(frame, eye)
        except Exception:
            pass
    half = frame.shape[1] // 2
    return frame[:, :half] if eye == "left" else frame[:, half:]


# The fleet reports 'DECXIN Camera 69', 'DECXIN  Camera 95' (two spaces) and 'DECXIN  Camera
# 84.1' (a suffix). A pattern assuming one space and a bare integer matches none of the last
# two, which looks exactly like a camera that was never given a number.
_NAME_RE = re.compile(r"^(DECXIN\s+Camera(?:\s+([\d.]*\d))?)\s*:?", re.I)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def scan_nodes() -> dict[tuple[str, str], str]:
    """{(/dev/videoN, usb device path): sysfs name} for every CAPTURE node right now.

    Read from sysfs, not v4l2-ctl: it is a few small file reads, cheap enough to do every
    second, which is what lets cameras appear and vanish without a restart. index 0 is the
    capture node; a DECXIN's index-1 twin is metadata and never streams.
    """
    out = {}
    try:
        names = os.listdir(SYS)
    except OSError:
        return out
    for n in names:
        if not n.startswith("video"):
            continue
        p = os.path.join(SYS, n)
        if _read(p + "/index") != "0":
            continue
        out[("/dev/" + n, os.path.realpath(p + "/device"))] = _read(p + "/name")
    return out


def does_4000x1200(dev: str) -> bool:
    """For a camera that does not call itself DECXIN: can it do the stereo mode at all?"""
    try:
        fmt = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=4).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "4000x1200" in fmt


def reset_controls(dev: str) -> tuple[bool, str]:
    """Every writable control back to its driver default, via camrig's 3-pass apply.

    A naive loop is wrong in a way that is easy to miss: exposure_time_absolute is READ-ONLY
    while auto_exposure owns it, so arbitrary order fails with EACCES on exactly the values
    that matter. camrig unlocks the gating auto modes, writes everything, then sets the gates.
    """
    if camrig is None:
        return False, "camrig.py not importable"
    try:
        vals = {c["name"]: c["default"] for c in camrig.get_controls(dev)
                if c.get("writable") and c.get("default") is not None}
        if not vals:
            return False, "no writable controls found"
        errors, _ = camrig.apply_values(dev, vals)
        return (not errors), (f"{len(vals)} controls reset" if not errors
                              else f"{len(errors)} failed")
    except Exception as e:                       # never let a reset take the viewer down
        return False, str(e)[:60]


def ask_camrig_to_let_go(dev: str) -> bool:
    """camrig holds the node while its preview is open; ask it to close politely."""
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:8088/api/close", method="POST",
            data=json.dumps({"dev": dev}).encode(),
            headers={"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        time.sleep(1.5)
        return True
    except Exception:
        return False


_FUSER_RE = re.compile(r"(\d+)\s+\S+\s+(\S+)\s*$")


def holders(dev: str) -> list[str]:
    """Other processes holding the node. fuser lists THIS process too once it has the node
    open, and counting ourselves would call every camera busy."""
    try:
        out = subprocess.run(["fuser", "-v", dev], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    me, names = os.getpid(), []
    for line in ((out.stdout or "") + (out.stderr or "")).splitlines():
        m = _FUSER_RE.search(line)
        if m and int(m.group(1)) != me:
            names.append(m.group(2))
    return sorted(set(names))


class QueuedWriter:
    """Feeds a writer from its own thread, so capture never waits on the encoder.

    Writing straight from the capture thread held it up for every 14 MB raw frame going down
    the ffmpeg pipe, and recording fell to ~50 fps although libx264 ultrafast does ~177 fps at
    4000x1200 here. The queue is short on purpose (8 frames is ~115 MB); if the encoder falls
    behind, frames are DROPPED and counted rather than memory growing without bound.
    """

    def __init__(self, inner, depth=8):
        import queue
        self.inner, self.err, self.dropped = inner, "", 0
        self.q = queue.Queue(maxsize=depth)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            f = self.q.get()
            if f is None:
                return
            self.inner.write(f)

    def isOpened(self):
        return self.inner.isOpened()

    def write(self, img):
        try:
            self.q.put_nowait(img)
        except Exception:
            self.dropped += 1

    def release(self):
        self.q.put(None)                         # drain what is queued, then stop
        self.t.join(timeout=30)
        self.inner.release()
        self.err = self.inner.err


# ---------------------------------------------------------------------------- one camera

class ViewState:
    """Zoom / pan / eye for one camera. Kept by camera name, so a camera that drops off the
    bus and comes back is shown the way you left it."""

    def __init__(self, eye="full", zoom=0.0):
        self.eye, self.zoom = eye, zoom
        self.ox = self.oy = 0.0
        self.recentre = True
        self.anchor = None                       # (img_x, img_y, dev_x, dev_y) to zoom about
        self.released = False                    # stream stopped so another app can have it


class Stream:
    """One camera. Its capture thread owns the VideoCapture outright - opens it, reads it,
    reopens it after a stall, and releases it - so nothing on the UI side can ever block on
    a read from a camera that has just been pulled out."""

    _ids = itertools.count(1)

    def __init__(self, dev: str, devpath: str, opts):
        self.id = next(self._ids)
        self.dev, self.devpath, self.opts = dev, devpath, opts
        self.node = os.path.basename(dev)
        self.state, self.detail = "opening", "starting the stream"
        self.frame, self.seq, self.fps = None, 0, 0.0
        self.dropped = 0                         # raw frames the decode pool had no room for
        self.req_fps = opts.fps
        self._pending: queue.Queue = queue.Queue(maxsize=max(1, opts.decoders) + 1)
        self._pool = None
        self.lock = threading.Lock()             # guards self.rec
        self.rec, self.rec_path, self.rec_n, self.rec_t0 = None, "", 0, 0.0
        self.stop = threading.Event()
        self.released = threading.Event()        # set = closed on purpose, for another app
        self.events: collections.deque = collections.deque()   # (kind, text) -> toasts
        self.gone_at = None
        self.opened_once = False
        self.take_asked = False
        self.refresh_identity()
        self.thread = threading.Thread(target=self._run, name=f"cap:{self.node}", daemon=True)

    # identity -------------------------------------------------------------------------
    def refresh_identity(self):
        sysname = _read(f"{SYS}/{self.node}/name")
        m = _NAME_RE.match(sysname)
        num = m.group(2) if m and m.group(2) else None
        # name is the FILE stem and stays what it always was ('starcap138'); the title says
        # what the camera itself reports, because a camera number is not a board number.
        self.name = ("starcap" + num) if num else self.node
        self.title = f"Camera {num}" if num else (sysname.split(":")[0].strip() or self.node)
        usbdev = os.path.dirname(self.devpath)
        self.port = os.path.basename(self.devpath).split(":")[0]
        try:
            self.speed = int(_read(usbdev + "/speed") or 0)
        except ValueError:
            self.speed = 0

    def present(self) -> bool:
        return os.path.realpath(f"{SYS}/{self.node}/device") == self.devpath

    def speed_text(self) -> str:
        if not self.speed:
            return ""
        return "USB 2.0" if self.speed <= 480 else f"USB {self.speed / 1000:g} Gb/s"

    # lifecycle ------------------------------------------------------------------------
    def start(self):
        self.thread.start()

    def vanish(self):
        """The node is gone from sysfs. Say so NOW; the thread notices within a read."""
        if self.state != "gone":
            self.state, self.detail = "gone", "unplugged"
            self.gone_at = time.monotonic()
            self.fps = 0.0
            self._event("warn", f"{self.title} disconnected")
        self.stop.set()
        self._finish_rec()           # now, not when the blocked read finally returns

    def close(self):
        self.stop.set()

    def release_cam(self):
        '''Stop streaming and close the node so another app can open it. The capture thread
        does the closing (it owns the handle), within one read.'''
        self.released.set()
        with self.lock:
            if self.rec is not None:
                self._end_rec("stream stopped")

    def resume(self):
        self.take_asked = False
        self.released.clear()
        if self.state == "released":
            self.state, self.detail = "opening", "starting the stream"

    def join(self, timeout):
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout)

    def _event(self, kind, text):
        self.events.append((kind, text))

    # capture --------------------------------------------------------------------------
    def _open(self):
        cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        o = self.opts
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, o.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, o.height)
        # Ask for the rate explicitly: unset, the camera keeps whatever interval it was last
        # given - measured on camera 175 that was 30 fps, though it offers 60 at 4000x1200.
        cap.set(cv2.CAP_PROP_FPS, o.fps)
        self.req_fps = cap.get(cv2.CAP_PROP_FPS) or o.fps
        # Two buffers, not one: with one the driver has nothing to fill while a frame is
        # handled, so capture and USB transfer cannot overlap (31 fps against 60, measured).
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        # Hand back the raw MJPEG and let the pool decode it (see the docstring). A backend
        # that ignores this just returns decoded frames, which _decode passes through.
        if o.decoders > 0:
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        return cap

    @staticmethod
    def _decode(raw):
        if raw.ndim == 3:                        # already decoded by the backend
            return raw
        return cv2.imdecode(raw.reshape(-1), cv2.IMREAD_COLOR)

    def _publish_loop(self):
        """Takes decoded frames back IN capture order, so the recorder never gets a frame
        out of sequence even though several are decoded at once."""
        t0, n = time.monotonic(), 0
        while True:
            try:
                item = self._pending.get(timeout=0.5)
            except queue.Empty:
                item = False
            now = time.monotonic()
            if item is None:
                return
            if item is not False:
                try:
                    f = item.result()
                except Exception:
                    f = None
                if f is not None:
                    self._publish(f)
                    n += 1
            if now - t0 >= 1.0:
                self.fps, t0, n = n / (now - t0), now, 0

    def _publish(self, f):
        self.frame = f
        self.seq += 1
        self.last_frame_t = time.monotonic()
        if self.state != "live":
            self.state, self.detail, self.take_asked = "live", "", False
        with self.lock:
            if self.rec is not None:
                self.rec.write(f)                # FULL frame, never the zoomed view
                self.rec_n += 1
                if not self.rec.isOpened():
                    self._end_rec("recording stopped: ffmpeg exited")

    def _run(self):
        n = max(0, self.opts.decoders)
        self._pool = ThreadPoolExecutor(max(1, n), thread_name_prefix=f"dec:{self.node}")
        pub = threading.Thread(target=self._publish_loop, name=f"pub:{self.node}", daemon=True)
        pub.start()
        try:
            self._capture()
        except Exception as e:
            self.state, self.detail = "error", str(e)[:80]
            self._event("error", f"{self.title}: capture failed - {e}")
        finally:
            try:
                self._pending.put(None, timeout=5)
            except queue.Full:
                pass
            pub.join(5)
            self._pool.shutdown(wait=False, cancel_futures=True)
            self.fps = 0.0
            self._finish_rec()
            if self.opened_once and self.state != "gone" and self.present():
                ok, msg = reset_controls(self.dev)
                print(f"  {self.dev}: controls -> driver defaults ({msg})")

    def _busy_wait(self, who):
        if who and self.opts.take and not self.take_asked:
            self.take_asked = True
            self.detail = "asking camrig to let go"
            if ask_camrig_to_let_go(self.dev):
                return
        if who:
            self.state, self.detail = "busy", "in use by " + ", ".join(who)
        else:
            self.state, self.detail = "error", "the camera will not open"
        for _ in range(10):                     # 2 s, but a Stop click ends the wait
            if self.stop.is_set() or self.released.is_set():
                return
            self.stop.wait(0.2)

    def _capture(self):
        while not self.stop.is_set():
            if not self.present():
                self.vanish()
                return
            if self.released.is_set():
                if self.state != "released":
                    self.state, self.detail = "released", "free for other apps"
                self.stop.wait(0.2)
                continue
            cap = self._open()
            if cap is None:
                self._busy_wait(holders(self.dev))
                continue
            self.refresh_identity()
            self.opened_once = True
            if self.state != "live":
                self.state, self.detail = "opening", "waiting for the first frame"
            last_ok = time.monotonic()
            reason = None
            while not self.stop.is_set() and not self.released.is_set():
                got, f = cap.read()
                now = time.monotonic()
                if got and f is not None:
                    last_ok = now
                    if self._pending.full():
                        self.dropped += 1          # decode is behind: drop, never pile up
                    else:
                        self._pending.put(self._pool.submit(self._decode, f))
                else:
                    if not self.present():
                        reason = "gone"
                        break
                    stalled = now - last_ok
                    if stalled > 1.0:
                        if self.frame is None or self.state == "opening":
                            who = holders(self.dev)
                            if who:
                                reason = ("busy", who)
                                break
                        self.state, self.detail = "nosignal", "no frames coming in"
                    if stalled > 4.0 or not cap.isOpened():
                        reason = "reopen"           # a stalled stream: start it again
                        break
                    self.stop.wait(0.05)
            cap.release()
            if reason == "gone" or not self.present():
                self.vanish()
                return
            if self.released.is_set() and not self.stop.is_set():
                # Hand it over clean, the same as on exit: the next app should not inherit
                # whatever was last set here.
                self.fps = 0.0
                self.state, self.detail = "released", "free for other apps"
                ok, msg = reset_controls(self.dev)
                print(f"  {self.dev}: stopped, controls -> driver defaults ({msg})")
                self._event("info", f"{self.title} stopped - free for other apps")
                continue
            if isinstance(reason, tuple):
                self._busy_wait(reason[1])
            elif reason == "reopen":
                self.stop.wait(0.5)

    # recording / snapshots ------------------------------------------------------------
    def recording(self) -> bool:
        return self.rec is not None

    def toggle_rec(self):
        if self.rec is not None:
            with self.lock:
                self._end_rec(None)
            return
        if self.state != "live":
            self._event("warn", f"{self.title} is not streaming, nothing to record")
            return
        if camrig is None:
            self._event("error", "cannot record: camrig.py is not importable")
            return
        h, w = self.frame.shape[:2]
        os.makedirs(SAVE_DIR, exist_ok=True)
        p = os.path.join(SAVE_DIR, f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        # Tag the file with the rate actually coming in; a fixed 30 on a 60 fps stream plays
        # back at half speed.
        wr = QueuedWriter(camrig.FfmpegWriter(p, w, h, round(self.fps or self.req_fps)))
        if not wr.isOpened():
            self._event("error", f"cannot record: {wr.inner.err}")
            return
        with self.lock:
            self.rec_path, self.rec_n, self.rec_t0 = p, 0, time.monotonic()
            self.rec = wr
        self._event("rec", f"Recording {self.title}")

    def _end_rec(self, why):
        """Detach the writer (caller holds self.lock) and finalise it off-thread: ffmpeg
        writing the moov atom can take seconds, and a half-written mp4 will not play."""
        w, self.rec = self.rec, None
        if w is None:
            return
        path, n = self.rec_path, self.rec_n

        def fin():
            w.release()
            txt = f"{os.path.basename(path)} saved ({n - w.dropped} frames"
            txt += f", {w.dropped} DROPPED)" if w.dropped else ")"
            self._event("saved" if not why else "warn", (why + " - " if why else "") + txt)
            print(f"  saved {path}")
        threading.Thread(target=fin, daemon=False).start()

    def _finish_rec(self):
        with self.lock:
            if self.rec is not None:
                self._end_rec("camera disconnected" if self.state == "gone" else None)

    def snapshot(self):
        f = self.frame
        if f is None:
            return
        os.makedirs(SAVE_DIR, exist_ok=True)
        p = os.path.join(SAVE_DIR, f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}.png")

        def save():
            ok = cv2.imwrite(p, f)
            self._event("saved" if ok else "error",
                        ("Saved " if ok else "Could not save ") + os.path.basename(p))
        threading.Thread(target=save, daemon=False).start()

    def defaults(self):
        def go():
            ok, msg = reset_controls(self.dev)
            self._event("info" if ok else "error",
                        f"{self.title}: " + ("controls reset - " if ok else "reset FAILED - ") + msg)
        threading.Thread(target=go, daemon=True).start()

    def take(self):
        def go():
            self.detail = "asking camrig to let go"
            if not ask_camrig_to_let_go(self.dev):
                self._event("warn", "camrig did not answer - close the camera there")
        threading.Thread(target=go, daemon=True).start()


# ---------------------------------------------------------------------------- drawing

class VideoArea(Gtk.Widget):
    """Draws one texture over its whole allocation. It asks for no size of its own, so the
    frames it shows can never push the layout around."""

    __gtype_name__ = "CamViewVideoArea"

    def __init__(self):
        super().__init__()
        self.tex = None
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_measure(self, orientation, for_size):
        return 0, 0, -1, -1

    def do_snapshot(self, snap):
        if self.tex is None:
            return
        r = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
        # NEAREST: the texture is rendered at exactly the device size, so this is 1:1 anyway;
        # on the odd frame where the allocation just changed it stays sharp instead of smeared.
        if HAS_SCALED_TEXTURE:
            snap.append_scaled_texture(self.tex, Gsk.ScalingFilter.NEAREST, r)
        else:                              # GTK < 4.10: 1:1 anyway, so linear is fine
            snap.append_texture(self.tex, r)

    def set_texture(self, tex):
        self.tex = tex
        self.queue_draw()


class Tile(Gtk.Overlay):
    """One camera on screen: the video, a small info pill, a REC pill, and a status card
    over a dimmed last frame whenever the stream is not live."""

    def __init__(self, app: "CamView", stream: Stream):
        super().__init__()
        self.app, self.s = app, stream
        self.vs = app.view_state(stream)
        self.add_css_class("tile")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.area = VideoArea()
        self.set_child(self.area)
        self._buf = None
        self._last = (None, None)
        self.dirty = True
        self.geom = None
        self._texts = {}

        self.info = self._pill(Gtk.Align.START, Gtk.Align.START, "pill")
        self.recpill = self._pill(Gtk.Align.END, Gtk.Align.START, "pill", "rec")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.card_icon = Gtk.Image(pixel_size=40)
        self.card_title = Gtk.Label(css_classes=["title-3"])
        self.card_detail = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        self.card_btn = Gtk.Button(label="Take it", halign=Gtk.Align.CENTER,
                                   css_classes=["pill", "suggested-action"], margin_top=6)
        self.card_btn.connect("clicked", lambda *_: (
            self.app.set_released(self.s, False) if self.s.state == "released" else self.s.take()))
        for w in (self.card_icon, self.card_title, self.card_detail, self.card_btn):
            card.append(w)
        self.veil = Gtk.Box(css_classes=["veil"], hexpand=True, vexpand=True)
        self.veil.append(card)
        card.set_hexpand(True)
        self.add_overlay(self.veil)

        self.outline = Gtk.Box(css_classes=["active-outline"], can_target=False)
        self.add_overlay(self.outline)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_ptr", (x, y)))
        self.add_controller(motion)
        self._ptr = None
        self.add_tick_callback(self._tick)

    def _pill(self, h, v, *classes):
        lab = Gtk.Label(css_classes=list(classes), halign=h, valign=v, can_target=False,
                        margin_start=10, margin_end=10, margin_top=10, visible=False)
        self.add_overlay(lab)
        return lab

    def _set(self, widget, attr, value):
        """Touch a widget only when the value actually changed; this runs every frame."""
        key = (id(widget), attr)
        if self._texts.get(key) != value:
            self._texts[key] = value
            getattr(widget, "set_" + attr)(value)

    # per-frame ------------------------------------------------------------------------
    def _tick(self, _w, _clock):
        s, vs = self.s, self.vs
        self._sync_overlays()
        f = s.frame
        sf = self.area.get_scale_factor()
        W, H = int(self.area.get_width() * sf), int(self.area.get_height() * sf)
        if f is None or W < 4 or H < 4:
            return GLib.SOURCE_CONTINUE
        if not self.dirty and self._last == (s.seq, (W, H)):
            return GLib.SOURCE_CONTINUE
        self._last, self.dirty = (s.seq, (W, H)), False
        buf = self._render(f, W, H)
        tex = Gdk.MemoryTexture.new(W, H, Gdk.MemoryFormat.B8G8R8,
                                    GLib.Bytes.new_take(buf.tobytes()), W * 3)
        self.area.set_texture(tex)
        return GLib.SOURCE_CONTINUE

    def _render(self, frame, W, H):
        vs = self.vs
        img = crop_eye(frame, vs.eye)
        ih, iw = img.shape[:2]
        if self._buf is None or self._buf.shape[:2] != (H, W):
            self._buf = np.empty((H, W, 3), np.uint8)
        buf = self._buf
        buf.fill(BG)
        if vs.zoom <= 0:
            sc = min(W / iw, H / ih)
            vw, vh = max(1, int(iw * sc)), max(1, int(ih * sc))
            x0, y0 = (W - vw) // 2, (H - vh) // 2
            buf[y0:y0 + vh, x0:x0 + vw] = cv2.resize(
                img, (vw, vh), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)
            self.geom = (0.0, sc, x0, y0, 0, 0, iw, ih)
            return buf
        # Crop THEN scale, so at 1:1 nothing is resampled - the pixels on screen are the
        # sensor's. Scaling the whole frame and cropping after would resample everything.
        z = vs.zoom
        zi = int(z)
        cw, ch = min(iw, math.ceil(W / z)), min(ih, math.ceil(H / z))
        vw, vh = min(W, cw * zi), min(H, ch * zi)
        x0, y0 = (W - vw) // 2, (H - vh) // 2
        if vs.anchor is not None:
            px, py, cx, cy = vs.anchor
            vs.ox, vs.oy = px - (cx - x0) / z, py - (cy - y0) / z
            vs.anchor, vs.recentre = None, False
        elif vs.recentre:
            vs.ox, vs.oy = (iw - cw) / 2, (ih - ch) / 2
            vs.recentre = False
        vs.ox = float(np.clip(vs.ox, 0, iw - cw))
        vs.oy = float(np.clip(vs.oy, 0, ih - ch))
        ox, oy = int(vs.ox), int(vs.oy)
        crop = img[oy:oy + ch, ox:ox + cw]
        view = crop if zi == 1 else cv2.resize(crop, (cw * zi, ch * zi),
                                               interpolation=cv2.INTER_NEAREST)
        buf[y0:y0 + vh, x0:x0 + vw] = view[:vh, :vw]
        self.geom = (z, 1.0, x0, y0, ox, oy, iw, ih)
        return buf

    def _sync_overlays(self):
        s, vs, app = self.s, self.vs, self.app
        many = len(app.shown) > 1
        zoom = "fit" if vs.zoom <= 0 else f"{vs.zoom:g}:1"
        eye = {"full": "both", "left": "left eye", "right": "right eye"}[vs.eye]
        parts = ([s.title] if many else []) + [zoom, eye, f"{s.fps:.1f} fps"]
        if s.speed and s.speed <= 480:
            parts.append("USB 2.0")
        self._set(self.info, "label", "   ·   ".join(parts))
        self._set(self.info, "visible", s.frame is not None and s.state == "live")
        rec = s.recording()
        self._set(self.recpill, "visible", rec)
        if rec:
            self._set(self.recpill, "label",
                      f"●  REC  {time.monotonic() - s.rec_t0:5.1f} s  ·  {s.rec_n}")
        self._set(self.outline, "visible", many and app.active is s)
        live = s.state == "live"
        self._set(self.veil, "visible", not live)
        if not live:
            look = {
                "opening": ("camera-web-symbolic", "Opening"),
                "nosignal": ("dialog-warning-symbolic", "No signal"),
                "busy": ("changes-prevent-symbolic", "In use"),
                "error": ("dialog-error-symbolic", "Will not open"),
                "gone": ("media-eject-symbolic", "Disconnected"),
                "released": ("media-playback-pause-symbolic", "Stream stopped"),
            }.get(s.state, ("camera-web-symbolic", s.state))
            self._set(self.card_icon, "from_icon_name", look[0])
            self._set(self.card_title, "label", f"{s.title} - {look[1]}" if many else look[1])
            detail = s.detail
            if s.state == "gone":
                detail = "plug it back in and it reconnects on its own"
            elif s.state == "released":
                detail = "CamView has let go of it - another app can open it now"
            self._set(self.card_detail, "label", detail)
            self._set(self.card_btn, "visible", s.state in ("busy", "released"))
            self._set(self.card_btn, "label", "Resume" if s.state == "released" else "Take it")
            self._set(self.veil, "css_classes",
                      ["veil", "solid"] if s.frame is None else ["veil"])

    # input ----------------------------------------------------------------------------
    def _dev_xy(self, x, y):
        sf = self.area.get_scale_factor()
        return x * sf, y * sf

    def image_point(self, cx, cy):
        """Image coordinates under a device-pixel point of this tile, from the last render."""
        if self.geom is None:
            return None
        z, sc, x0, y0, ox, oy, iw, ih = self.geom
        if z <= 0:
            return (cx - x0) / sc, (cy - y0) / sc
        return ox + (cx - x0) / z, oy + (cy - y0) / z

    def zoom_to(self, z, at=None):
        """Change zoom keeping the image point under `at` (device px) where it is; the tile
        centre when no point is given."""
        vs = self.vs
        if at is None:
            sf = self.area.get_scale_factor()
            at = (self.area.get_width() * sf / 2, self.area.get_height() * sf / 2)
        p = self.image_point(*at)
        vs.zoom = z
        if z > 0 and p is not None:
            vs.anchor = (p[0], p[1], at[0], at[1])
        else:
            vs.recentre = True
        self.dirty = True
        self.app.sync_header()

    def _on_click(self, gesture, n_press, x, y):
        self.app.set_active(self.s)
        if n_press == 2:
            self.zoom_to(1.0 if self.vs.zoom <= 0 else 0.0, self._dev_xy(x, y))

    def _on_drag_begin(self, _g, _x, _y):
        self._drag0 = (self.vs.ox, self.vs.oy)

    def _on_drag_update(self, _g, dx, dy):
        if self.vs.zoom <= 0:
            return
        sf = self.area.get_scale_factor()
        self.vs.ox = self._drag0[0] - dx * sf / self.vs.zoom
        self.vs.oy = self._drag0[1] - dy * sf / self.vs.zoom
        self.dirty = True

    def _on_scroll(self, _c, _dx, dy):
        if dy == 0:
            return False
        self.app.set_active(self.s)
        i = ZOOMS.index(self.vs.zoom) if self.vs.zoom in ZOOMS else 0
        i = max(0, min(len(ZOOMS) - 1, i + (1 if dy < 0 else -1)))
        if ZOOMS[i] != self.vs.zoom:
            at = self._dev_xy(*self._ptr) if self._ptr else None
            self.zoom_to(ZOOMS[i], at)
        return True


# ---------------------------------------------------------------------------- the app

CSS = b"""
.stage, .tile { background-color: rgb(14,14,14); }
.veil { background-color: alpha(black, 0.62); }
.veil.solid { background-color: rgb(14,14,14); }
.pill { background-color: alpha(black, 0.6); color: white; border-radius: 999px;
        padding: 4px 12px; font-size: 0.86em; font-feature-settings: "tnum"; }
.pill.rec { background-color: alpha(#c01c28, 0.9); font-weight: bold; }
.active-outline { border: 2px solid @accent_bg_color; }
.dot { font-size: 0.75em; }
.dot.live { color: #33d17a; }
.dot.warn { color: #f6d32d; }
.dot.bad  { color: #e66100; }
.dot.off  { color: alpha(currentColor, 0.25); }
.recdot { color: #e01b24; font-size: 0.75em; }
.camrow-sub { font-size: 0.82em; font-feature-settings: "tnum"; }
.sidebar-hint { font-size: 0.82em; }
.recbtn.on { background-color: #c01c28; color: white; }
"""

SHORTCUTS = """<tt>scroll          </tt>zoom around the pointer
<tt>double-click    </tt>fit / 1:1 at that spot
<tt>drag            </tt>pan when zoomed
<tt>1 2 3 4         </tt>zoom 1:1 .. 4:1
<tt>f  r            </tt>fit · reset view
<tt>e               </tt>both eyes / left / right
<tt>Tab  a          </tt>next camera · all cameras
<tt>s  v            </tt>snapshot · record
<tt>d               </tt>camera controls to defaults
<tt>p               </tt>stop stream (free it for other apps) / resume
<tt>±  w  F11       </tt>full screen
<tt>F9              </tt>sidebar
<tt>q  Esc          </tt>quit · leave full screen"""


class CamRow(Gtk.ListBoxRow):
    def __init__(self, stream: Stream | None, app=None):
        super().__init__()
        self.s, self.app = stream, app
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
        if stream is None:
            box.append(Gtk.Image(icon_name="view-grid-symbolic"))
        else:
            self.dot = Gtk.Label(label="●", css_classes=["dot", "off"], valign=Gtk.Align.CENTER)
            box.append(self.dot)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.sub = Gtk.Label(xalign=0, ellipsize=3, css_classes=["dim-label", "camrow-sub"])
        txt.append(self.title)
        txt.append(self.sub)
        box.append(txt)
        self.rec = Gtk.Label(label="● REC", css_classes=["recdot"], visible=False)
        box.append(self.rec)
        if stream is not None:
            self.toggle = Gtk.Button(css_classes=["flat", "circular"], valign=Gtk.Align.CENTER,
                                     focus_on_click=False)
            self.toggle.connect("clicked", lambda *_: app.set_released(
                stream, not stream.released.is_set()))
            box.append(self.toggle)
        self.set_child(box)
        self._cache = {}

    def sort_key(self):
        return ("",) if self.s is None else ("~", self.s.port, self.s.id)

    def _set(self, w, attr, v):
        k = (id(w), attr)
        if self._cache.get(k) != v:
            self._cache[k] = v
            getattr(w, "set_" + attr)(v)

    def update(self, n_live=0):
        s = self.s
        if s is None:
            self._set(self.title, "label", "All cameras")
            self._set(self.sub, "label", f"{n_live} live" if n_live else "none live")
            return
        self._set(self.title, "label", s.title)
        bits = []
        if s.state == "live":
            bits.append(f"{s.fps:.1f} fps")
        else:
            bits.append({"opening": "opening", "nosignal": "no signal", "busy": "in use",
                         "error": "will not open", "gone": "disconnected",
                         "released": "stopped"}.get(s.state, s.state))
        if s.speed_text():
            bits.append(s.speed_text())
        bits.append(f"port {s.port}")
        self._set(self.sub, "label", "  ·  ".join(bits))
        cls = {"live": "live", "opening": "warn", "nosignal": "warn", "busy": "bad",
               "error": "bad"}.get(s.state, "off")
        if s.state == "live" and s.speed and s.speed <= 480:
            cls = "warn"                      # 30 fps on a USB 2.0 link, not a bad camera
        self._set(self.dot, "css_classes", ["dot", cls])
        self._set(self.rec, "visible", s.recording())
        stopped = s.released.is_set()
        self._set(self.toggle, "icon_name",
                  "media-playback-start-symbolic" if stopped else "media-playback-stop-symbolic")
        self._set(self.toggle, "tooltip_text", "Resume the stream" if stopped
                  else "Stop the stream - frees the camera for another app")
        self._set(self.toggle, "visible", s.state != "gone")


class CamView(Adw.Application):
    def __init__(self, opts):
        super().__init__(application_id=APP_ID, flags=APP_FLAGS)
        self.opts = opts
        self.streams: list[Stream] = []
        self.by_key: dict[tuple[str, str], Stream] = {}
        self.tiles: dict[int, Tile] = {}
        self.rows: dict[int, CamRow] = {}
        self.views: dict[str, ViewState] = {}
        self.not_decxin: set = set()
        self.checking: set = set()
        self.solo: str | None = None          # camera NAME shown alone, or None for all
        self.active: Stream | None = None
        self.shown: list[Stream] = []
        self._grid_shape = None
        self._syncing = False
        self.win = None
        self.closing = False
        self.restart = False
        self._fs_hint_shown = False
        self._code_mtime = self._mtime()

    @staticmethod
    def _mtime():
        try:
            return os.path.getmtime(os.path.abspath(__file__))
        except OSError:
            return 0.0

    # setup ----------------------------------------------------------------------------
    def do_activate(self):
        if self.win is not None:              # a second launch (the hotkey): just raise it
            self.win.present()
            # ...which would silently keep OLD code on screen after an update: raising the
            # running window is all a relaunch can do, so offer the restart it cannot.
            if self._mtime() != self._code_mtime:
                t = Adw.Toast(title="CamView was updated - restart to load the new version",
                              timeout=0)
                if TOAST_HAS_BUTTON_SIGNAL:
                    t.set_button_label("Restart")
                    t.connect("button-clicked", lambda *_: self.restart_app())
                self.toasts.add_toast(t)
            return
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        load_css(css, CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self._actions()
        self.hold()
        self.win.present()
        if self.opts.fullscreen:
            self.win.fullscreen()
        else:
            self.win.maximize()
        self.poll_devices()
        GLib.timeout_add(1000, self.poll_devices)
        GLib.timeout_add(250, self._housekeeping)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.shutdown)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.shutdown)

    def _build(self):
        win = self.win = Adw.ApplicationWindow(application=self, title="CamView")
        win.set_default_size(1400, 820)
        win.connect("close-request", lambda *_: (self.shutdown(), True)[1])
        win.connect("notify::fullscreened", self._on_fullscreen)

        # sidebar
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.list.connect("row-selected", self._on_row)
        # By USB port, so a camera that drops off and comes back lands where it was.
        self.list.set_sort_func(lambda a, b: (a.sort_key() > b.sort_key())
                                - (a.sort_key() < b.sort_key()))
        self.all_row = CamRow(None)
        self.list.append(self.all_row)
        side_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        side_body.append(sc)
        side_body.append(Gtk.Label(
            label="Plug in a DECXIN (blue USB 3 port)\nand it shows up here.",
            css_classes=["dim-label", "sidebar-hint"], justify=Gtk.Justification.CENTER,
            margin_bottom=14, margin_top=8))
        side_head = Adw.HeaderBar(show_end_title_buttons=False)
        side_head.set_title_widget(Adw.WindowTitle(title="CamView", subtitle="1:1 native"))
        side = ToolbarView()
        side.add_top_bar(side_head)
        side.set_content(side_body)

        # content header
        head = self.head = Adw.HeaderBar(show_start_title_buttons=False)
        self.side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True,
                                         tooltip_text="Sidebar (F9)", focus_on_click=False)
        head.pack_start(self.side_btn)
        self.eye_btns = self._toggle_group(head, (("full", "Both"), ("left", "Left"),
                                                  ("right", "Right")), self._on_eye_btn)
        self.zoom_btns = self._toggle_group(head, ((0.0, "Fit"), (1.0, "1:1"), (2.0, "2:1"),
                                                   (3.0, "3:1")), self._on_zoom_btn)
        self.title = Adw.WindowTitle(title="CamView")
        head.set_title_widget(self.title)

        menu = Gio.Menu()
        menu.append("Reset view", "app.reset-view")
        menu.append("Camera controls to defaults", "app.defaults")
        menu.append("Stop all streams (free every camera)", "app.stop-all")
        menu.append("Resume all streams", "app.resume-all")
        menu.append("Open recordings folder", "app.folder")
        menu.append("Keyboard shortcuts", "app.shortcuts")
        menu.append("Quit", "app.quit")
        head.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                     focus_on_click=False))
        fs = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Full screen (±)",
                        focus_on_click=False)
        fs.connect("clicked", lambda *_: self.toggle_fullscreen())
        head.pack_end(fs)
        self.rec_lbl = Gtk.Label(label="REC")
        rb = Gtk.Box(spacing=6)
        rb.append(Gtk.Image(icon_name="media-record-symbolic"))
        rb.append(self.rec_lbl)
        self.rec_btn = Gtk.Button(child=rb, tooltip_text="Record the full frame (v)",
                                  css_classes=["recbtn"], focus_on_click=False)
        self.rec_btn.connect("clicked", lambda *_: self.active and self.active.toggle_rec())
        head.pack_end(self.rec_btn)
        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic", focus_on_click=False,
                                   tooltip_text="Stop the stream - frees the camera for another app (p)")
        self.stop_btn.connect("clicked", lambda *_: self.active and self.set_released(
            self.active, not self.active.released.is_set()))
        head.pack_end(self.stop_btn)
        snap = Gtk.Button(icon_name="camera-photo-symbolic", focus_on_click=False,
                          tooltip_text="Save a full-resolution PNG (s)")
        snap.connect("clicked", lambda *_: self.active and self.active.snapshot())
        head.pack_end(snap)

        # stage
        self.grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True,
                             row_spacing=2, column_spacing=2, css_classes=["stage"])
        empty = Adw.StatusPage(icon_name="camera-web-symbolic", title="No camera",
                               description="Plug a DECXIN into a blue USB 3 port.\n"
                                           "It shows up here on its own - no restart.")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(empty, "empty")
        self.stack.add_named(self.grid, "stage")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.content = (ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT)
                        if ToolbarView is Adw.ToolbarView else ToolbarView())
        self.content.add_top_bar(head)
        self.content.set_content(self.toasts)

        self.split = SplitView(sidebar=side, content=self.content,
                                          min_sidebar_width=230, max_sidebar_width=290)
        self.split.bind_property("show-sidebar", self.side_btn, "active",
                                 gi.repository.GObject.BindingFlags.BIDIRECTIONAL
                                 | gi.repository.GObject.BindingFlags.SYNC_CREATE)
        win.set_content(self.split)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        win.add_controller(motion)
        self.list.select_row(self.all_row)

    def _toggle_group(self, head, items, cb):
        box = Gtk.Box(css_classes=["linked"])
        btns, first = {}, None
        for val, label in items:
            b = Gtk.ToggleButton(label=label, focus_on_click=False)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", cb, val)
            box.append(b)
            btns[val] = b
        head.pack_start(box)
        return btns

    def _actions(self):
        for name, fn in (("reset-view", self.reset_view),
                         ("defaults", lambda: self.active and self.active.defaults()),
                         ("folder", self.open_folder),
                         ("restart", self.restart_app),
                         ("stop-all", lambda: [self.set_released(s, True) for s in self.streams
                                               if s.state != "gone"]),
                         ("resume-all", lambda: [self.set_released(s, False)
                                                 for s in self.streams]),
                         ("shortcuts", self.show_shortcuts),
                         ("quit", self.shutdown)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, f=fn: f())
            self.add_action(a)

    # devices --------------------------------------------------------------------------
    def view_state(self, s: Stream) -> ViewState:
        if s.name not in self.views:
            self.views[s.name] = ViewState(self.opts.eye, self.opts.zoom)
        return self.views[s.name]

    def poll_devices(self):
        if self.closing:
            return GLib.SOURCE_REMOVE
        now = scan_nodes()
        if self.opts.dev:
            now = {k: v for k, v in now.items() if k[0] == self.opts.dev}
        for key, s in list(self.by_key.items()):
            if key not in now:
                s.vanish()
                del self.by_key[key]
        for key, name in now.items():
            if key in self.by_key or key in self.not_decxin:
                continue
            if "DECXIN" in name.upper() or self.opts.dev:
                self._add_stream(key)
            elif key not in self.checking:
                # Not a DECXIN by name: ask the hardware, off the UI thread (v4l2-ctl).
                self.checking.add(key)

                def check(k=key):
                    ok = does_4000x1200(k[0])
                    GLib.idle_add(self._checked, k, ok)
                threading.Thread(target=check, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _checked(self, key, ok):
        self.checking.discard(key)
        if self.closing:
            return GLib.SOURCE_REMOVE
        if ok and key in scan_nodes() and key not in self.by_key:
            self._add_stream(key)
        elif not ok:
            self.not_decxin.add(key)
        return GLib.SOURCE_REMOVE

    def _add_stream(self, key):
        s = Stream(key[0], key[1], self.opts)
        # The same camera back on the bus: drop its "disconnected" ghost straight away.
        for old in [o for o in self.streams if o.state == "gone" and o.name == s.name]:
            self._remove_stream(old)
        self.by_key[key] = s
        self.streams.append(s)
        self.streams.sort(key=lambda x: (x.port, x.id))
        if self.view_state(s).released:
            s.released.set()                   # stopped before a replug stays stopped
        row = CamRow(s, self)
        self.rows[s.id] = row
        self.list.append(row)
        self.list.invalidate_sort()
        self.tiles[s.id] = Tile(self, s)
        s.start()
        if self.opts.solo and self.solo is None and len(self.streams) == 1:
            self.solo = s.name
            self._select_row_for(s)
        print(f"  + {s.dev}  {s.title}  port {s.port}  {s.speed_text()}")
        self.relayout()

    def _remove_stream(self, s):
        if s in self.streams:
            self.streams.remove(s)
        row = self.rows.pop(s.id, None)
        if row is not None:
            if self.list.get_selected_row() is row:
                self._syncing = True
                self.list.select_row(self.all_row)
                self._syncing = False
            self.list.remove(row)
        t = self.tiles.pop(s.id, None)
        if t is not None and t.get_parent() is not None:
            self.grid.remove(t)
        if self.active is s:
            self.active = None
        self.relayout()

    # layout ---------------------------------------------------------------------------
    def relayout(self):
        if self.solo is not None:
            pick = [s for s in self.streams if s.name == self.solo]
            live = [s for s in pick if s.state != "gone"]
            shown = live[:1] or pick[:1]
            if not shown:
                self.solo = None                # that camera is gone for good: back to all
                self._syncing = True
                self.list.select_row(self.all_row)
                self._syncing = False
        if self.solo is None:
            shown = list(self.streams)
        if self.active not in shown:
            self.active = shown[0] if shown else None

        W, H = max(1, self.grid.get_width()), max(1, self.grid.get_height())
        aspect = 3.33
        if shown:
            vs = self.view_state(shown[0])
            aspect = 3.33 if vs.eye == "full" else 1.6
        cols = self._best_cols(len(shown), W, H, aspect)
        shape = (tuple(s.id for s in shown), cols)
        if shape != self._grid_shape:
            self._grid_shape = shape
            for t in list(self.tiles.values()):
                if t.get_parent() is self.grid:
                    self.grid.remove(t)
            for i, s in enumerate(shown):
                t = self.tiles[s.id]
                self.grid.attach(t, i % cols, i // cols, 1, 1)
                t.dirty = True
        self.shown = shown
        self.stack.set_visible_child_name("stage" if shown else "empty")
        self.sync_header()

    @staticmethod
    def _best_cols(n, W, H, aspect):
        """Columns that make the frames biggest. These frames are wide (3.3:1 for both eyes),
        so two cameras usually stack; two single eyes (1.6:1) usually sit side by side."""
        if n <= 1:
            return 1
        best, best_c = -1.0, 1
        for c in range(1, n + 1):
            r = math.ceil(n / c)
            tw, th = W / c, H / r
            w = min(tw, th * aspect)
            if w > best + 1e-6:
                best, best_c = w, c
        return best_c

    def _housekeeping(self):
        if self.closing:
            return GLib.SOURCE_REMOVE
        now = time.monotonic()
        changed = False
        for s in list(self.streams):
            while s.events:
                kind, text = s.events.popleft()
                self.toast(text, kind)
            if s.state == "gone" and s.gone_at and now - s.gone_at > GONE_GRACE_S:
                self._remove_stream(s)
                changed = True
        n_live = 0
        for s in self.streams:
            row = self.rows.get(s.id)
            if row:
                row.update()
            n_live += s.state == "live"
        self.all_row.update(n_live)
        if not changed:
            self.relayout()                  # cheap when nothing moved; also tracks resizes
        return GLib.SOURCE_CONTINUE

    # header ---------------------------------------------------------------------------
    def sync_header(self):
        s = self.active
        self._syncing = True
        try:
            if s is None:
                self.title.set_title("CamView")
                self.title.set_subtitle("no camera")
            else:
                vs = self.view_state(s)
                self.eye_btns[vs.eye].set_active(True)
                zb = self.zoom_btns.get(vs.zoom)
                if zb is not None:
                    zb.set_active(True)
                else:                          # 4:1 has no button
                    for b in self.zoom_btns.values():
                        b.set_active(False)
                self.title.set_title(s.title if len(self.shown) <= 1
                                     else f"All cameras  ·  {s.title}")
                h, w = s.frame.shape[:2] if s.frame is not None else (0, 0)
                sub = [f"{w}×{h}"] if w else []
                sub += ([f"{s.fps:.1f} fps"] if s.state == "live" else
                        [{"released": "stopped", "nosignal": "no signal", "busy": "in use",
                          "gone": "disconnected"}.get(s.state, s.state)])
                if s.speed_text():
                    sub.append(s.speed_text())
                self.title.set_subtitle("  ·  ".join(sub))
            stopped = bool(s and s.released.is_set())
            self.stop_btn.set_icon_name("media-playback-start-symbolic" if stopped
                                        else "media-playback-stop-symbolic")
            self.stop_btn.set_tooltip_text("Resume the stream (p)" if stopped else
                                           "Stop the stream - frees the camera for another app (p)")
            self.stop_btn.set_sensitive(s is not None and s.state != "gone")
            rec = bool(s and s.recording())
            self.rec_lbl.set_label(f"{time.monotonic() - s.rec_t0:.0f} s" if rec else "REC")
            if rec:
                self.rec_btn.add_css_class("on")
            else:
                self.rec_btn.remove_css_class("on")
        finally:
            self._syncing = False

    def _on_eye_btn(self, btn, val):
        if self._syncing or not btn.get_active() or self.active is None:
            return
        self.set_eye(val)

    def _on_zoom_btn(self, btn, val):
        if self._syncing or not btn.get_active() or self.active is None:
            return
        t = self.tiles.get(self.active.id)
        if t:
            t.zoom_to(val)

    # actions --------------------------------------------------------------------------
    def set_active(self, s):
        if self.active is not s:
            self.active = s
            self.sync_header()

    def set_released(self, s, on):
        '''Stop (on=True) or resume one camera's stream. Remembered by camera name, so a
        stopped camera stays stopped across a replug instead of being grabbed back.'''
        self.view_state(s).released = on
        if on:
            s.release_cam()
        else:
            s.resume()
        self.sync_header()

    def set_eye(self, eye):
        s = self.active
        if s is None:
            return
        vs = self.view_state(s)
        vs.eye, vs.recentre, vs.anchor = eye, True, None
        self.tiles[s.id].dirty = True
        self.relayout()

    def reset_view(self):
        s = self.active
        if s is None:
            return
        vs = self.view_state(s)
        vs.zoom, vs.ox, vs.oy, vs.recentre, vs.anchor = 0.0, 0, 0, True, None
        self.tiles[s.id].dirty = True
        self.sync_header()

    def _select_row_for(self, s):
        self._syncing = True
        row = self.rows.get(s.id) if s else self.all_row
        self.list.select_row(row)
        self._syncing = False

    def _on_row(self, _lb, row):
        if self._syncing or row is None:
            return
        if row.s is None:
            self.solo = None
        else:
            self.solo = row.s.name
            self.active = row.s
        self.relayout()

    def cycle(self, step):
        """Tab: all cameras -> camera 1 -> camera 2 -> ... -> all cameras."""
        order = [None] + [s for s in self.streams if s.state != "gone"]
        cur = next((s for s in order[1:] if self.solo == s.name), None)
        i = (order.index(cur) + step) % len(order)
        nxt = order[i]
        self.solo = nxt.name if nxt else None
        if nxt:
            self.active = nxt
        self._select_row_for(nxt)
        self.relayout()

    def toggle_fullscreen(self):
        if self.win.is_fullscreen():
            self.win.unfullscreen()
        else:
            self.win.fullscreen()

    def _on_fullscreen(self, *_):
        fs = self.win.is_fullscreen()
        if fs:
            self._side_before = self.split.get_show_sidebar()
            self.split.set_show_sidebar(False)
            self.content.set_reveal_top_bars(False)
            if not self._fs_hint_shown:
                self._fs_hint_shown = True
                self.toast("Full screen  ·  ± or Esc to leave  ·  pointer to the top edge for the toolbar")
        else:
            self.content.set_reveal_top_bars(True)
            self.split.set_show_sidebar(getattr(self, "_side_before", True))

    def _on_motion(self, _c, _x, y):
        if not self.win.is_fullscreen():
            return
        show = self.content.get_reveal_top_bars()
        if not show and y < 6:
            self.content.set_reveal_top_bars(True)
        elif show and y > 90:
            self.content.set_reveal_top_bars(False)

    def toast(self, text, kind="info"):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=3)
        if kind == "saved":
            t.set_button_label("Show")
            if TOAST_HAS_BUTTON_SIGNAL:
                t.connect("button-clicked", lambda *_: self.open_folder())
            else:                              # libadwaita < 1.2: buttons fire an action
                t.set_action_name("app.folder")
        self.toasts.add_toast(t)

    def open_folder(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(SAVE_DIR, None), None)

    def show_shortcuts(self):
        info_dialog(self.win, "Keyboard shortcuts", SHORTCUTS)

    def _on_key(self, _c, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        k = Gdk.keyval_to_lower(keyval)
        s = self.active
        t = self.tiles.get(s.id) if s else None
        K = Gdk
        if k == K.KEY_q:
            self.shutdown()
        elif k == K.KEY_Escape:
            if self.win.is_fullscreen():
                self.win.unfullscreen()
            else:
                self.shutdown()
        elif k in (K.KEY_Tab, K.KEY_ISO_Left_Tab):
            self.cycle(-1 if (k == K.KEY_ISO_Left_Tab or state & K.ModifierType.SHIFT_MASK) else 1)
        elif k == K.KEY_a:
            self.solo = None
            self._select_row_for(None)
            self.relayout()
        elif k in (K.KEY_plusminus, K.KEY_w, K.KEY_F11):
            self.toggle_fullscreen()
        elif k == K.KEY_F9:
            self.split.set_show_sidebar(not self.split.get_show_sidebar())
        elif t is None:
            return False
        elif k in (K.KEY_1, K.KEY_2, K.KEY_3, K.KEY_4):
            t.zoom_to(float(k - K.KEY_0))
        elif k == K.KEY_f:
            t.zoom_to(0.0)
        elif k == K.KEY_r:
            self.reset_view()
        elif k == K.KEY_e:
            vs = self.view_state(s)
            self.set_eye({"full": "left", "left": "right", "right": "full"}[vs.eye])
        elif k == K.KEY_s:
            s.snapshot()
        elif k == K.KEY_v:
            s.toggle_rec()
        elif k == K.KEY_d:
            s.defaults()
        elif k == K.KEY_p:
            self.set_released(s, not s.released.is_set())
        else:
            return False
        return True

    # exit -----------------------------------------------------------------------------
    def shutdown(self, *_):
        """Hide at once, then stop every stream - finishing recordings and putting controls
        back to driver defaults - before the process goes."""
        if self.closing:
            return GLib.SOURCE_REMOVE
        self.closing = True
        if self.win is not None:
            self.win.set_visible(False)
        streams = list(self.streams)

        def work():
            for s in streams:
                s.close()
            for s in streams:
                s.join(15)
            GLib.idle_add(self._really_quit)
        threading.Thread(target=work, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def restart_app(self):
        self.restart = True
        self.shutdown()

    def _really_quit(self):
        self.release()
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", help="only this node, e.g. /dev/video2")
    ap.add_argument("--width", type=int, default=4000)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--fps", type=float, default=60.0,
                    help="frame rate to ask the camera for (it offers 60 and 30 at 4000x1200)")
    ap.add_argument("--decoders", type=int, default=3,
                    help="MJPEG decode threads per camera (0 = let OpenCV decode inline)")
    ap.add_argument("--eye", choices=("full", "left", "right"), default="full")
    ap.add_argument("--zoom", type=float, default=0.0, choices=ZOOMS,
                    help="0 = fit (default), 1 = sensor pixels, 2..4 = magnified")
    ap.add_argument("--take", action="store_true",
                    help="if camrig holds a camera, ask it to let go")
    ap.add_argument("--solo", action="store_true", help="open on one camera, not all of them")
    ap.add_argument("--fullscreen", action="store_true", help="start full screen")
    ap.add_argument("--windowed", action="store_true", help=argparse.SUPPRESS)  # old default
    a = ap.parse_args(argv)

    app = CamView(a)

    # Defaults on EVERY exit path, including an exception out of the main loop: a camera left
    # pinned here goes to the field carrying bench settings, and nothing downstream says so.
    def _last_resort():
        for s in app.streams:
            s.close()
        for s in app.streams:
            s.join(10)
    atexit.register(_last_resort)
    # GApplication must not see our flags; a second launch just raises the first window.
    rc = app.run([sys.argv[0]])
    if app.restart:
        _last_resort()                         # streams are already stopped; this is a no-op
        atexit.unregister(_last_resort)
        os.execv(sys.executable, [sys.executable, "-s", os.path.abspath(__file__)]
                 + (sys.argv[1:] if argv is None else list(argv)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
