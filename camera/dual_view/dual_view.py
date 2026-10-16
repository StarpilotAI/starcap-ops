#!/usr/bin/env python3
"""Live viewer + recorder for DECXIN cameras in ONE window: the stereo 168 on top, every mono below it, side by side.

    python3 dual_view.py                    # every connected DECXIN; CAM_SKIP_PORTS=2-9 leaves a USB port out

r starts / stops recording ALL cameras at once, full res, 60 fps; q or Esc quits. Camera settings are left as they are.
Low CPU: each camera is read on its own thread as its own MJPEG bytes (CONVERT_RGB off). Recording pipes those bytes
unchanged into ffmpeg (-c copy, no re-encode): ~/Videos/<stamp>_<camera>.avi, each with a .csv of CLOCK_MONOTONIC
stamps (ns) per frame so the videos line up. The window decodes only the newest frame of each, ~15x a second, at 1/4.

From Youssef Mahran (Slack DM, 2026-10-08). Setup steps are in README.md.
"""
import os, subprocess, sys, threading, time
from pathlib import Path
import cv2
import numpy as np

# Sizes by the camera's USB name. Two "DECXIN Camera M" units share one name AND one serial number, so the cameras are
# told apart by USB port; the stereo 168 is always first (the top row), the monos follow in port order.
SIZES = {"DECXIN Camera 168": (4000, 1200), "DECXIN Camera M": (2080, 1200), "DCXIN Camera": (1920, 1200)}


def discover():
    """[(key, name, dev, size, decode flag)]: every UVC capture node (index 0) whose name is a known DECXIN."""
    out = []
    for v in sorted(Path("/sys/class/video4linux").glob("video*")):
        if (v / "index").read_text().strip() != "0":
            continue
        name = (v / "name").read_text().split(":")[0].strip()
        if name not in SIZES:
            continue
        port = next((part.split(":")[0] for part in reversed(str((v / "device").resolve()).split("/")) if "-" in part and ":" in part), v.name)
        if port in os.environ.get("CAM_SKIP_PORTS", "").split(","):
            continue  # e.g. CAM_SKIP_PORTS=2-9: a camera that keeps resetting
        key = "168" if "168" in name else f"mono_{port}"
        out.append((key, name, f"/dev/{v.name}", SIZES[name], cv2.IMREAD_REDUCED_COLOR_4))
    return sorted(out, key=lambda c: (c[0] != "168", c[0]))
SHOW_HZ, WIDTH = 15.0, 1200  # the window's width; each camera is scaled to it


class Cam(threading.Thread):
    def __init__(self, key, name, dev, size, flag):
        super().__init__(daemon=True)
        self.key, self.name, self.size, self.flag = key, name, size, flag
        self.dev = dev
        self.latest = None; self.lock = threading.Lock(); self.stop = False
        self.n, self.t0, self.fps = 0, time.monotonic(), 0.0
        self.rec = None; self.csv = None; self.n_rec = 0; self.path = None
        self.last_frame = 0.0

    def run(self):
        if self.dev is None:
            return
        cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0]); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        cap.set(cv2.CAP_PROP_FPS, 60); cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        while not self.stop:
            ok, buf = cap.read()
            if not ok or buf is None:
                time.sleep(0.02); continue
            ts = time.monotonic_ns(); data = buf.tobytes()
            self.last_frame = time.monotonic()
            self.n += 1
            if time.monotonic() - self.t0 >= 1.0:
                self.fps, self.n, self.t0 = self.n / (time.monotonic() - self.t0), 0, time.monotonic()
            with self.lock:
                self.latest = data
                if self.rec is not None:
                    try:
                        self.rec.stdin.write(data); self.csv.write(f"{ts}\n"); self.n_rec += 1
                    except BrokenPipeError:
                        self.rec = None
        cap.release()

    def start_rec(self, stamp):
        if self.dev is None:
            return
        with self.lock:
            self.path = Path.home() / "Videos" / f"{stamp}_{self.key}.avi"; self.path.parent.mkdir(exist_ok=True)
            self.rec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "mjpeg", "-framerate", "60", "-i", "-",
                                         "-c", "copy", str(self.path)], stdin=subprocess.PIPE)
            self.csv = open(self.path.with_suffix(".csv"), "w"); self.csv.write("t_monotonic_ns\n"); self.n_rec = 0

    def stop_rec(self):
        with self.lock:
            rec, csv, self.rec = self.rec, self.csv, None
        if rec is not None:
            rec.stdin.close(); rec.wait(); csv.close()
            print(f"saved {self.path}: {self.n_rec} frames", flush=True)

    def view(self, width=WIDTH):
        with self.lock:
            data = self.latest
        h = int(width * self.size[1] / self.size[0])
        if data is None or time.monotonic() - self.last_frame > 1.0:
            img = np.zeros((h, width, 3), np.uint8)
            cv2.putText(img, f"{self.name}: {'not connected' if self.dev is None else 'no frames'}", (20, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
            return img
        img = cv2.imdecode(np.frombuffer(data, np.uint8), self.flag)
        img = cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA)
        label = f"{self.key}  {self.size[0]}x{self.size[1]}  {self.fps:.1f} fps"
        if self.rec is not None:
            label += f"   REC {self.n_rec} frames"
        cv2.putText(img, label, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        return img


def main():
    cams = [Cam(*c) for c in discover()]
    if not cams:
        sys.exit("no DECXIN camera found (known names: " + ", ".join(SIZES) + ")")
    print('cameras:', ', '.join(f'{c.key} {c.dev} {c.size[0]}x{c.size[1]}' for c in cams), flush=True)
    for c in cams:
        c.start()
    win = "DECXIN 168 (top) + monos (bottom) - r record all, q quit"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    recording, t_rec = False, 0.0
    t_log = time.monotonic()
    while True:
        # the 168 across the top; the monos side by side below it, at one height
        monos = [c.view(WIDTH // max(len(cams) - 1, 1)) for c in cams[1:]] or [np.zeros((1, WIDTH, 3), np.uint8)]
        hh = min(m.shape[0] for m in monos)
        frame = np.vstack([cams[0].view(), np.hstack([m[:hh] for m in monos])])
        if recording:
            cv2.circle(frame, (WIDTH - 25, 25), 12, (0, 0, 255), -1)
            cv2.putText(frame, f"REC {time.monotonic() - t_rec:.0f} s", (WIDTH - 170, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        if time.monotonic() - t_log > 5:
            t_log = time.monotonic()
            print(' | '.join(f"{c.key} {c.dev or '-'} {c.fps:.1f} fps" + (f' rec {c.n_rec}' if c.rec else '') for c in cams), flush=True)
        cv2.imshow(win, frame)
        k = cv2.waitKey(int(1000 / SHOW_HZ)) & 0xFF
        if k == ord("r"):
            if not recording:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                for c in cams:
                    c.start_rec(stamp)
                recording, t_rec = True, time.monotonic(); print(f"recording {stamp}", flush=True)
            else:
                for c in cams:
                    c.stop_rec()
                recording = False
        try:
            closed = cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1
        except cv2.error:
            closed = True
        if k in (ord("q"), 27) or closed:
            break
    for c in cams:
        c.stop_rec(); c.stop = True
    time.sleep(0.2); cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
