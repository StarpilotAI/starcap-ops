#!/usr/bin/env python3
"""camview.py - native 1:1 viewer for a DECXIN, when the browser is in the way.

    python3 camview.py                 # every DECXIN found, tiled
    python3 camview.py --dev /dev/video1        # just one
    python3 camview.py --eye left
    python3 camview.py --solo          # one camera even if several are connected
    python3 camview.py --windowed      # a normal window instead of full screen

It opens FULL SCREEN and stays there. Every key that changes what is shown - e, r, f, 1/2/3,
TAB - changes the view inside the screen; none of them gives the window back. The one key that
does is ±, which toggles full screen off and on again.

WHY THIS EXISTS

The browser preview cannot show you the sensor's pixels honestly, and it took a while to see
why. Three things degrade it, and only the first is obvious:

  1. the stream is downscaled server-side  (fixable, and was fixed)
  2. it is re-encoded to JPEG on top of the camera's own MJPEG  (fixable, q100)
  3. the browser then FITS it to the window - a 4000 px frame in a 1200 px window is
     displayed at 29%, and no amount of stream quality survives that

Even with 1 and 2 solved, a 4000x1200 frame at q100 is ~1.8 MB, so 60 fps is ~100 MB/s
through an HTTP multipart stream that the browser must JPEG-decode per frame. It drops
frames, and dropped frames look like a bad camera.

This path has none of that: decode once, blit to a window. No HTTP, no re-encode, no
resampling unless you ask for it.

    drag        pan
    1 / 2 / 3   zoom 1:1, 2:1, 3:1
    f           fit to window
    r           reset the VIEW - back to fit, centred (or click VIEW, top right)
    d           reset the CAMERA's controls to driver defaults (or click DEFAULTS)
    e           cycle both eyes -> left -> right
    ± or w      full screen on / off
    s           save the current full-resolution frame as a PNG, named for the camera
    v           start / stop recording (or click REC)
    TAB         next camera, when more than one is connected
    q or Esc    quit

MULTIPLE CAMERAS

With several DECXINs plugged in they are all opened and tiled, the way RealSense Viewer shows
its streams. Each keeps its own zoom and pan, so you can sit at 1:1 on one camera's corner
while another is fitted. TAB moves the keyboard focus between them; the active one is outlined.

Each camera is read and decoded on its own thread, so drawing the window never slows capture.
The rate is ASKED for (--fps, default 60): left unset, the camera runs at whatever interval it
was last given, which was 30 - that, not decode speed, is what used to cap this at 30. The
decode is ~17.5 ms a frame on this laptop, so one camera holds 60; several cameras share the
CPU. The viewer shows each stream's own capture rate so you can see what you are actually
getting rather than assuming.

ONE PROCESS AT A TIME owns a /dev/video* node. If camrig has the camera open, this cannot
have it - close it in camrig first, or pass --take to have this ask camrig to let go.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

WIN = "camview"

# The key that toggles full screen, as cv2.waitKey reports it here AFTER the & 0xFF mask. It
# is a bare number rather than ord(...) because the key is not an ASCII one: highgui truncates
# X keysyms to 8 bits, so this physical key (keycode 126, plusminus) arrives as 177. Captured
# by pressing it rather than guessed - it repeated 15 times out of 15, and collides with none
# of the letter bindings. To move it to another key, capture that key's code the same way.
FULLSCREEN_KEY = 177


def _crop_eye(frame, eye):
    """One eye, using camrig's measured seam; falls back to halves if camrig is not importable."""
    if eye == "full" or frame is None:
        return frame
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import camrig
        return camrig.crop_eye(frame, eye)
    except Exception:
        half = frame.shape[1] // 2
        return frame[:, :half] if eye == "left" else frame[:, half:]


_NAME_RE = re.compile(r"^(DECXIN\s+Camera(?:\s+([\d.]*\d))?)\s*:", re.I)


def camera_name(dev: str) -> str:
    """What this camera calls itself, as a filename stem: 'starcap91', or the node if it has
    no number set.

    Whitespace is flexible and the number may be dotted. Both were learned the hard way on the
    bench: the fleet reports 'DECXIN Camera 69', 'DECXIN  Camera 95' with two spaces, and
    'DECXIN  Camera 84.1' with a suffix, and a pattern that assumes one space and a bare
    integer silently matches NONE of the last two - which looks exactly like a camera that was
    never given a number.
    """
    try:
        out = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True,
                             text=True, timeout=5).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return os.path.basename(dev)
    header, nodes = None, []
    for line in out.splitlines() + [""]:
        if line.startswith(("\t", " ")) and line.strip().startswith("/dev/video"):
            nodes.append(line.strip())
            continue
        if header is not None and dev in nodes:
            m = _NAME_RE.match(header)
            if m and m.group(2):
                return "starcap" + m.group(2)
            if m:
                return os.path.basename(dev)      # a DECXIN with no number set
        header, nodes = line.strip(), []
    return os.path.basename(dev)


def reset_controls(dev: str) -> tuple[bool, str]:
    """Put every writable control back to its driver default.

    Reuses camrig's apply_values rather than looping over v4l2-ctl here, because the naive
    loop is wrong in a way that is easy to miss: a control like exposure_time_absolute is
    READ-ONLY while auto_exposure owns it, so writing them in arbitrary order fails with
    EACCES on exactly the values that matter. camrig already does the three-pass dance -
    unlock the gating auto modes, write everything, then set the gates to what was asked for.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import camrig
        vals = {c["name"]: c["default"] for c in camrig.get_controls(dev)
                if c.get("writable") and c.get("default") is not None}
        if not vals:
            return False, "no writable controls found"
        errors, _ = camrig.apply_values(dev, vals)
        return (not errors), (f"{len(vals)} controls reset"
                              if not errors else f"{len(errors)} failed")
    except Exception as e:                      # never let a reset take the viewer down
        return False, str(e)[:60]


def find_devs() -> list[str]:
    """Every node that can actually do 4000x1200, one per physical camera.

    A DECXIN exposes two nodes (capture + metadata) and only the first streams, so taking every
    matching node would open the same camera twice and fight itself for the bus. Nodes are
    grouped by the USB bus path from --list-devices, and one is kept per group.
    """
    try:
        out = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True,
                             text=True, timeout=6).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    groups, cur = [], []
    for line in out.splitlines() + [""]:
        if line.startswith(("\t", " ")) and line.strip().startswith("/dev/video"):
            cur.append(line.strip())
        else:
            if cur:
                groups.append(cur)
            cur = []
    devs = []
    for g in groups:
        for p in g:
            try:
                fmt = subprocess.run(["v4l2-ctl", "-d", p, "--list-formats-ext"],
                                     capture_output=True, text=True, timeout=4).stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if "4000x1200" in fmt:
                devs.append(p)
                break                     # one node per physical camera
    return devs


def find_dev(pref: str | None) -> str:
    if pref:
        return pref
    d = find_devs()
    return d[0] if d else "/dev/video0"


def ask_camrig_to_let_go(dev: str) -> bool:
    """camrig holds the node while its preview is open; ask it to close politely."""
    try:
        import urllib.request
        import json as _j
        req = urllib.request.Request(
            "http://localhost:8088/api/close", method="POST",
            data=_j.dumps({"dev": dev}).encode(),
            headers={"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def holders(dev: str) -> list[str]:
    try:
        out = subprocess.run(["fuser", "-v", dev], capture_output=True, text=True,
                             timeout=5)
        txt = (out.stdout or "") + (out.stderr or "")
        return [l.split()[-1] for l in txt.splitlines()[1:] if l.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


class QueuedWriter:
    """Feeds a writer from its own thread, so capture never waits on the encoder.

    Writing straight from the capture thread held it up for every 14 MB raw frame going down
    the ffmpeg pipe, and recording fell to ~50 fps although libx264 ultrafast does ~177 fps at
    4000x1200 here. The queue is short on purpose (8 frames is ~115 MB); if the encoder ever
    does fall behind, frames are DROPPED and counted rather than memory growing without bound.
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
        self.q.put(None)                     # drain what is queued, then stop
        self.t.join(timeout=30)
        self.inner.release()
        self.err = self.inner.err


class Stream:
    """One camera: its capture, its own zoom/pan, its own fps."""

    def __init__(self, dev, width, height, eye, zoom, fps=60.0):
        self.dev, self.name = dev, camera_name(dev)
        self.cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        # Ask for the rate explicitly. Unset, the camera keeps whatever interval it was last
        # given - measured on camera 175 that was 30 fps, though it offers 60 at 4000x1200.
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.req_fps = self.cap.get(cv2.CAP_PROP_FPS) or fps
        # Two buffers, not one: with a single buffer the driver has nothing to fill while this
        # loop handles the current frame, so capture and USB transfer cannot overlap. Measured
        # in camrig on the same camera: 31 fps with one buffer against 60 with two.
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        self.frame = None
        self.eye, self.zoom = eye, zoom
        self.ox = self.oy = 0
        self.recentre = True
        self.rec = None
        self.rec_path, self.rec_n, self.rec_t0 = "", 0, 0.0
        self.t0, self.n, self.fps = time.monotonic(), 0, 0.0
        self.err = ""
        # Capture runs on its own thread: a 4000x1200 decode is ~17.5 ms, and sharing a loop
        # with the scaling and imshow would pull 60 fps down into the 40s.
        self.lock = threading.Lock()          # guards self.rec against toggle_rec/release
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running and self.ok():
            self.read_one()

    def ok(self):
        return self.cap is not None and self.cap.isOpened()

    def grab(self):
        """The newest frame. Reading happens on the capture thread once start() has run."""
        return self.frame

    def read_one(self):
        if not self.ok():
            return None
        got, f = self.cap.read()
        if not got or f is None:
            return self.frame
        self.frame = f
        self.n += 1
        now = time.monotonic()
        if now - self.t0 >= 1.0:
            self.fps, self.t0, self.n = self.n / (now - self.t0), now, 0
        with self.lock:
            if self.rec is not None:
                self.rec.write(f)             # FULL frame, never the zoomed view
                self.rec_n += 1
                if not self.rec.isOpened():
                    self.err = "recording stopped: " + (self.rec.inner.err or "ffmpeg exited")
                    self.rec = None
        return f

    def toggle_rec(self):
        if self.rec is not None:
            with self.lock:
                w, self.rec = self.rec, None
            w.release()
            self.err = f"saved {os.path.basename(self.rec_path)} ({self.rec_n - w.dropped} frames"
            self.err += f", {w.dropped} DROPPED)" if w.dropped else ")"
            return
        if self.frame is None:
            return
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import camrig
        except Exception as e:
            self.err = f"cannot record: {e}"
            return
        h, w_ = self.frame.shape[:2]
        d = os.path.expanduser("~/Videos/camrig")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        # Tag the file with the rate actually coming in; a fixed 30 on a 60 fps stream plays
        # back at half speed.
        wr = QueuedWriter(camrig.FfmpegWriter(p, w_, h, round(self.fps or self.req_fps)))
        if not wr.isOpened():
            self.err = f"cannot record: {wr.err}"
            return
        self.rec_path, self.rec_n, self.rec_t0 = p, 0, time.monotonic()
        with self.lock:
            self.rec = wr
        self.err = f"recording -> {os.path.basename(p)}"

    def snapshot(self):
        if self.frame is None:
            return
        d = os.path.expanduser("~/Videos/camrig")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        cv2.imwrite(p, self.frame)
        self.err = "saved " + os.path.basename(p)
        print(f"  saved {p}")

    def render(self, vw, vh):
        """The tile for this camera, at its own zoom, sized to vw x vh."""
        f = self.frame
        if f is None:
            t = np.zeros((vh, vw, 3), np.uint8)
            cv2.putText(t, f"{self.name}: no frame", (14, 34), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (90, 90, 200), 2, cv2.LINE_AA)
            return t
        # camrig.crop_eye measures the seam. Splitting down the middle is wrong on a DECXIN:
        # the frame is a 158 px data strip then two 1921 px eyes, so the boundary is at 2079.
        # A midpoint split put the IMU strip inside the left eye and a slice of the left eye
        # down the edge of the right one.
        img = _crop_eye(f, self.eye)
        z = self.zoom
        if z <= 0:
            sc = min(vw / img.shape[1], vh / img.shape[0])
            view = cv2.resize(img, (max(1, int(img.shape[1] * sc)),
                                    max(1, int(img.shape[0] * sc))),
                              interpolation=cv2.INTER_AREA)
        else:
            # Crop THEN scale, so at 1:1 nothing is resampled - the pixels on screen are the
            # sensor's. Scaling the whole frame and cropping after would resample everything
            # and defeat the point of the window.
            cw, ch = int(vw / z), int(vh / z)
            if self.recentre:
                self.ox = max(0, (img.shape[1] - cw) // 2)
                self.oy = max(0, (img.shape[0] - ch) // 2)
                self.recentre = False
            self.ox = int(np.clip(self.ox, 0, max(0, img.shape[1] - cw)))
            self.oy = int(np.clip(self.oy, 0, max(0, img.shape[0] - ch)))
            crop = img[self.oy:self.oy + ch, self.ox:self.ox + cw]
            view = crop if z == 1 else cv2.resize(
                crop, (max(1, int(crop.shape[1] * z)), max(1, int(crop.shape[0] * z))),
                interpolation=cv2.INTER_NEAREST)
        # CENTRE the view in the tile. Left-aligning it meant a single eye - 1.6:1 against a
        # tile that is nearer 3:1 - was drawn hard left with a black slab filling the rest,
        # which reads as "pressing E blanked the other eye" rather than as letterboxing.
        pad = np.zeros((vh, vw, 3), np.uint8)
        h, w_ = min(vh, view.shape[0]), min(vw, view.shape[1])
        y0, x0 = (vh - h) // 2, (vw - w_) // 2
        pad[y0:y0 + h, x0:x0 + w_] = view[:h, :w_]
        return pad

    def release(self, to_defaults=True):
        """Close down and leave the CAMERA on driver defaults.

        These units go to the field after being on the bench, so anything left pinned here
        follows them into service and quietly degrades their footage. Nothing would report it.
        The reset happens after the capture handle is released, because some controls are
        refused while a stream is open.
        """
        self.running = False
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)       # never release the cap under a read
        self.thread = None
        with self.lock:
            rec, self.rec = self.rec, None
        if rec is not None:
            rec.release()                     # a half-written mp4 has no moov atom
            print(f"  saved {self.rec_path}")
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        if to_defaults:
            ok, msg = reset_controls(self.dev)
            print(f"  {self.dev}: controls -> driver defaults ({msg})")


def screen_size():
    """The screen, or None if it cannot be told.

    Full screen needs the SCREEN's size, and getWindowImageRect cannot give it: with
    WINDOW_KEEPRATIO it reports the letterboxed IMAGE rect. Sizing the canvas from that is
    what would leave a 1.6:1 single eye fitted inside the 3.3:1 both-eyes canvas - small and
    centred on a big black screen, which is the opposite of what full screen is for.
    """
    try:
        out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True,
                             timeout=3).stdout
        m = (re.search(r"\bconnected primary (\d+)x(\d+)\+", out)
             or re.search(r"\bconnected\b.*?(\d+)x(\d+)\+", out))
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def window_size(scr, n):
    """A normal window that fits THIS screen: 85% of it, at the stacked 3.3:1 aspect."""
    sw, sh = scr if scr else (1600, 900)
    w = int(sw * 0.85)
    return w, max(220, min(int(sh * 0.85), int(w / 3.33) * n))


def ui_scale(vh):
    """HUD size for the screen: 1.0 up to ~1200 px tall, bigger on hi-res panels."""
    return max(1.0, min(2.5, vh / 1200))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev")
    ap.add_argument("--width", type=int, default=4000)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--fps", type=float, default=60.0,
                    help="frame rate to ask the camera for (it offers 60 and 30 at 4000x1200)")
    ap.add_argument("--eye", choices=("full", "left", "right"), default="full")
    ap.add_argument("--zoom", type=float, default=0.0,
                    help="0 = fit to window (default), 1 = sensor pixels, 2 = 2:1")
    ap.add_argument("--take", action="store_true", help="ask camrig to release the camera")
    ap.add_argument("--solo", action="store_true", help="one camera even if several are found")
    ap.add_argument("--windowed", action="store_true",
                    help="a normal window instead of full screen")
    a = ap.parse_args(argv)

    devs = [a.dev] if a.dev else find_devs()
    if not devs:
        print("  no camera doing 4000x1200 found")
        return 2
    if a.solo:
        devs = devs[:1]
    if a.take:
        for d in devs:
            ask_camrig_to_let_go(d)

    streams = []
    for d in devs:
        st = Stream(d, a.width, a.height, a.eye, a.zoom, a.fps)
        if not st.ok():
            h = holders(d)
            print(f"  skipping {d}" + (f" - held by {', '.join(h)}" if h else " - will not open"))
            st.release()
            continue
        st.read_one()
        if st.frame is None:
            print(f"  skipping {d} - opened but delivered no frame")
            st.release()
            continue
        streams.append(st)
        st.start()
        print(f"  {d}  {st.name}  {st.frame.shape[1]}x{st.frame.shape[0]}"
              f"  @ {st.req_fps:g} fps asked")
    if not streams:
        print("  nothing could be opened. close it in camrig first, or pass --take")
        return 2
    print("  drag=pan  1/2/3=zoom  f=fit  r=reset  d=defaults  e=eye  s=save  v=rec  "
          "TAB=next  w=full screen  q=quit")

    # A signal or an exception must not skip the reset, so it is registered the moment the
    # streams exist rather than left to the end of the loop.
    import atexit
    import signal as _sig

    def _cleanup(*_):
        for s in streams:
            try:
                s.release()
            except Exception:
                pass
        del streams[:]
    atexit.register(_cleanup)
    for _s in (_sig.SIGINT, _sig.SIGTERM):
        try:
            _sig.signal(_s, lambda *_a: (_cleanup(), sys.exit(128)))
        except (ValueError, OSError):
            pass

    # Full screen is the whole point: the browser's crime was fitting a 4000 px frame into a
    # small window, and a 1600x900 window commits the same one. Keep the intent in a variable
    # rather than reading the property back - the property is exactly what gets lost, so it
    # cannot also be the record of what was asked for.
    #
    # Ask for the screen BEFORE the window exists. A slow call between setting the flag and
    # the first imshow is what loses the full-screen transition, and xrandr is a subprocess.
    full = not a.windowed
    scr = screen_size()

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    if full:
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    else:
        cv2.resizeWindow(WIN, *window_size(scr, len(streams)))

    def keep_fullscreen(wx, wy, vw, vh, _last=[0.0]):
        """Put the window back to full screen when it is not - judged by GEOMETRY.

        WND_PROP_FULLSCREEN cannot be trusted here. Qt reports it as 1 while the window is
        still a 532x402 box, because the flag is recorded before the window manager has acted
        on it and any slow call in between loses the transition - 3 starts in 8 on this
        laptop. And re-setting a flag that already reads 1 is a no-op, so the recovery has to
        be a REAL transition: leave full screen, then re-enter.
        """
        now = time.monotonic()
        if now - _last[0] < 0.5:                  # at most twice a second, so no flicker
            return
        if scr:
            # KEEPRATIO centres the image, so its margins give back the window it sits in.
            if 2 * wx + vw >= scr[0] - 8 and 2 * wy + vh >= scr[1] - 8:
                return
        elif cv2.getWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN) == cv2.WINDOW_FULLSCREEN:
            return                                # no xrandr: the flag is all there is to go on
        _last[0] = now
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    sel = {"i": 0, "drag": False, "sx": 0, "sy": 0, "btn": {}, "tiles": []}

    def on_mouse(ev, x, y, flags, _):
        if ev == cv2.EVENT_LBUTTONDOWN:
            for key, (bx, by, bw, bh, idx) in sel["btn"].items():
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    s = streams[idx]
                    if key[0] == "view":
                        s.zoom, s.ox, s.oy, s.recentre = 0.0, 0, 0, True
                    elif key[0] == "def":
                        ok, msg = reset_controls(s.dev)
                        s.err = ("controls reset - " + msg) if ok else ("reset FAILED - " + msg)
                    elif key[0] == "rec":
                        s.toggle_rec()
                    sel["i"] = idx
                    return
            for idx, (tx, ty, tw, th) in enumerate(sel["tiles"]):
                if tx <= x <= tx + tw and ty <= y <= ty + th:
                    sel["i"] = idx
                    break
            sel.update(drag=True, sx=x, sy=y)
        elif ev == cv2.EVENT_LBUTTONUP:
            sel["drag"] = False
        elif ev == cv2.EVENT_MOUSEMOVE and sel["drag"]:
            s = streams[sel["i"]]
            s.ox -= x - sel["sx"]
            s.oy -= y - sel["sy"]
            sel.update(sx=x, sy=y)
    cv2.setMouseCallback(WIN, on_mouse)

    while True:
        for s in streams:
            s.grab()
        try:
            wx, wy, vw, vh = cv2.getWindowImageRect(WIN)
        except cv2.error:
            wx, wy, vw, vh = 0, 0, 1600, 900
        if full:
            keep_fullscreen(wx, wy, vw, vh)
            if scr is None and vw >= 640 and vh >= 480:
                # No xrandr: the letterbox margins give the screen back. Only believe it once
                # the window is plausibly screen-sized, or a start that came up small would be
                # recorded as the screen size for good.
                scr = (2 * wx + vw, 2 * wy + vh)
            if scr:
                # Draw onto the WHOLE screen and let render() fit inside it, so one eye fills
                # the screen the way both eyes do. Sizing the canvas from the rect above would
                # fit a 1.6:1 single eye inside the 3.3:1 both-eyes canvas instead - small and
                # centred, which is the opposite of what full screen is for.
                vw, vh = scr
        vw, vh = max(vw, 200), max(vh, 200)
        n = len(streams)
        th = max(120, vh // n)                  # stacked rows: these frames are 3.3:1
        u = ui_scale(scr[1] if scr else vh)
        fs, lw = 0.6 * u, max(1, round(u))
        canvas = np.zeros((th * n, vw, 3), np.uint8)
        sel["btn"], sel["tiles"] = {}, []
        for idx, s in enumerate(streams):
            tile = s.render(vw, th)
            active = (idx == sel["i"])
            label = (f"{s.name}   {s.dev}   "
                     f"{'fit' if s.zoom <= 0 else f'{s.zoom:g}:1'}   {s.eye}   "
                     f"{s.fps:.1f} fps")
            # Solid strip, not outlined text on the image. At 0.6 scale over a busy frame the
            # outline-then-fill pass smears and the zoom level becomes unreadable - which is
            # the one field you actually need when deciding whether you are looking at real
            # pixels or a resampled view.
            (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, lw)
            cv2.rectangle(tile, (0, 0), (tw + int(24 * u), tht + int(18 * u)), (18, 18, 20), -1)
            cv2.putText(tile, label, (int(12 * u), tht + int(6 * u)), cv2.FONT_HERSHEY_SIMPLEX,
                        fs, (255, 255, 255) if active else (160, 160, 160), lw, cv2.LINE_AA)
            bw, bh = int(88 * u), int(28 * u)
            for k, (txt, warm) in enumerate((("VIEW", False), ("DEFAULTS", True),
                                             ("STOP" if s.rec else "REC", bool(s.rec)))):
                bx = vw - (3 - k) * (bw + int(8 * u)) - 4
                by = int(6 * u)
                cv2.rectangle(tile, (bx, by), (bx + bw, by + bh),
                              (30, 26, 24) if warm else (40, 40, 40), -1)
                cv2.rectangle(tile, (bx, by), (bx + bw, by + bh),
                              (110, 170, 245) if warm else (200, 200, 200), 1)
                cv2.putText(tile, txt, (bx + int(10 * u), by + int(20 * u)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5 * u,
                            (150, 200, 255) if warm else (235, 235, 235), lw, cv2.LINE_AA)
                sel["btn"][(["view", "def", "rec"][k], idx)] = (bx, by + idx * th, bw, bh, idx)
            if s.rec is not None:
                el = time.monotonic() - s.rec_t0
                cv2.circle(tile, (int(22 * u), int(52 * u)), int(8 * u), (60, 60, 235), -1)
                cv2.putText(tile, f"REC {el:5.1f}s  {s.rec_n}", (int(38 * u), int(58 * u)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55 * u, (90, 90, 255), lw, cv2.LINE_AA)
            if s.err:
                cv2.putText(tile, s.err, (int(12 * u), th - int(14 * u)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55 * u, (0, 0, 0), 3 * lw, cv2.LINE_AA)
                cv2.putText(tile, s.err, (int(12 * u), th - int(14 * u)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55 * u, (150, 220, 150), lw, cv2.LINE_AA)
            if active and n > 1:
                cv2.rectangle(tile, (1, 1), (vw - 2, th - 2), (90, 170, 250), 2)
            canvas[idx * th:(idx + 1) * th] = tile
            sel["tiles"].append((0, idx * th, vw, th))
        cv2.imshow(WIN, canvas)

        k = cv2.waitKey(1) & 0xFF
        cur = streams[sel["i"]]
        if k in (ord("q"), 27):
            break
        elif k == 9:                            # TAB
            sel["i"] = (sel["i"] + 1) % n
        elif k == ord("f"):
            cur.zoom = 0.0
        elif k == ord("r"):
            cur.zoom, cur.ox, cur.oy, cur.recentre = 0.0, 0, 0, True
        elif k in (ord("1"), ord("2"), ord("3")):
            cur.zoom, cur.recentre = float(chr(k)), True
        elif k == ord("e"):
            cur.eye = {"full": "left", "left": "right", "right": "full"}[cur.eye]
            cur.recentre = True
            # Reshape the window to the new aspect, so one eye FILLS it instead of sitting in
            # a letterbox. A single eye is ~1.6:1 against a both-eyes tile nearer 3.3:1, so
            # without this, switching eye just adds dead space either side and looks like the
            # other eye was blanked.
            # ...but only when there IS a window to reshape. In full screen the screen is
            # already the frame, and resizeWindow here is exactly what used to drop out of it.
            ref = streams[sel["i"]].frame
            if ref is not None and not full:
                shown = _crop_eye(ref, cur.eye)
                ih, iw = shown.shape[:2]
                try:
                    _, _, cw_, _ = cv2.getWindowImageRect(WIN)
                except cv2.error:
                    cw_ = 1600
                mw = window_size(scr, 1)[0]
                cw_ = max(600, min(cw_, mw))
                wh = int(cw_ * ih / iw) * len(streams)
                max_h = int(scr[1] * 0.9) if scr else 1150
                cv2.resizeWindow(WIN, cw_, max(220, min(wh, max_h)))
        elif k in (FULLSCREEN_KEY, ord("w")):
            full = not full
            if full:
                # A real transition, for the same reason keep_fullscreen forces one: setting a
                # flag that already reads 1 does nothing, and Qt often still reads 1 here.
                cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
                cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
            else:
                cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(WIN, *window_size(scr, len(streams)))
        elif k == ord("s"):
            cur.snapshot()
        elif k == ord("v"):
            cur.toggle_rec()
        elif k == ord("d"):
            ok, msg = reset_controls(cur.dev)
            cur.err = ("controls reset - " + msg) if ok else ("reset FAILED - " + msg)

    # Defaults on EVERY exit path, including a crash or a Ctrl-C - a camera left pinned here
    # goes to the field carrying bench settings, and nothing downstream would say so.
    for s in streams:
        try:
            s.release()
        except Exception as e:
            print(f"  {s.dev}: reset failed: {e}")
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
