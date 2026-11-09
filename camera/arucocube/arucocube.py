#!/usr/bin/env python3
"""arucocube - check an ArUco cube's markers for print defects with the docked DECXIN.

    ./run.sh                    -> http://127.0.0.1:8786
    ./run.sh --dev /dev/video21 --expected 0-5 --dict 4X4_50

Wear the camera, turn the cube in front of it, and watch every face fill in.

WHAT IT MEASURES (per marker ID, accumulated over every usable view, both eyes)

  * cell defects  - every marker is warped flat onto its own grid and each cell is compared
                    with what the dictionary says it should be. A cell's score is how far it
                    sits toward the wrong colour (0 = perfect, 1 = fully inverted), taking the
                    worse of its mean and the fraction of its pixels on the wrong side, so a
                    small ink void or spot inside one cell still shows.
  * persistence   - glare, motion blur and shadows move when your head moves; a printing
                    defect does not. A cell only becomes a DEFECT when it is wrong in a large
                    share of views, and the UI says whether both eyes agree.
  * quiet zone    - the white ring outside the black border. If it is dark or dirty the
                    detector loses the marker at angles.
  * edge bow      - the outer black border is traced with subpixel crossings and fitted with
                    a line; the residual (as % of marker side) is a curled/bubbled sticker or
                    a bent face. Only measured when the marker is near the image centre, so
                    lens distortion does not masquerade as bow.
  * duplicates    - the same ID twice in one eye's frame = a face printed twice.
  * missing faces - with --expected (or the UI box), IDs never seen are listed.

Views are thrown away (and counted, so you can see why) when the marker is too small, too
oblique, too blurred or too low-contrast to judge cells honestly.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CELL = 24                     # canonical px per marker cell in the flattened view
MARGIN = int(CELL * 0.22)     # cell edge pixels ignored when scoring a cell (blur lives there)

# gates for a view to count
MIN_PX_PER_CELL = 5.0
MAX_OBLIQUE = 0.5             # shortest side / longest side
MAX_BLUR_CELLS = 0.40         # edge transition width, in cells
MIN_CONTRAST = 25             # white - black, grey levels
CENTRE_ZONE = 0.55            # bow only when marker centre within this frac of half-height

FLIP = 0.5                    # cell score that counts as a bit error in one view
WEAK = 0.25
PERSIST_RATE = 0.30           # share of views a cell must be bad in to be a defect
MIN_VIEWS = 12                # valid views before a verdict
STRIP = (230, 860)            # top-left of each eye masked: DECXIN timestamp strip (w, h)

# auto-detect tries these. APRILTAG_16h5/25h9 are left out on purpose: they fire on plain
# clutter (16h5 "won" auto with no marker in view); pick them from the menu if needed.
FAMILIES = ["4X4", "5X5", "6X6", "7X7", "ARUCO_ORIGINAL", "APRILTAG_36h11", "ARUCO_MIP_36h12"]
DICT_CHOICES = ["auto"] + [f"{f}_{n}" for f in ["4X4", "5X5", "6X6", "7X7"]
                           for n in (50, 100, 250, 1000)] + FAMILIES[4:] + [
                           "APRILTAG_25h9", "APRILTAG_16h5"]


def get_dict(name):
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, "DICT_" + name))


def make_detector(name):
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    p.errorCorrectionRate = 1.0   # a damaged face must still be FOUND to be judged; at the
    #                               default 0.6 one flipped bit on a 4X4_50 hides the marker
    return cv2.aruco.ArucoDetector(get_dict(name), p)


def narrow(family, max_id):
    if family in ("4X4", "5X5", "6X6", "7X7"):
        for n in (50, 100, 250, 1000):
            if max_id < n:
                return f"{family}_{n}"
    return family


def parse_ids(text):
    ids = set()
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            ids.update(range(int(m[1]), int(m[2]) + 1))
        elif part.isdigit():
            ids.add(int(part))
    return sorted(ids)


# ---------------------------------------------------------------- camera

def find_decxins():
    out = []
    for p in sorted(glob.glob("/sys/class/video4linux/video*"),
                    key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        try:
            name = open(f"{p}/name").read().strip()
            idx = open(f"{p}/index").read().strip()
        except OSError:
            continue
        if "DECXIN" in name.upper() and idx == "0":   # index 1 = metadata node, no frames
            out.append(("/dev/" + os.path.basename(p), name))
    return out


def holders(dev):
    try:
        r = subprocess.run(["fuser", "-v", dev], capture_output=True, text=True, timeout=5)
        lines = ((r.stdout or "") + (r.stderr or "")).splitlines()[1:]
        return [l.split()[-1] for l in lines if l.strip()]
    except Exception:
        return []


class Camera:
    def __init__(self, dev, fps):
        self.dev, self.fps = dev, fps
        self.cap = None
        self.frame = None
        self.seq = 0
        self.status = "starting"
        self.rate = 0.0
        self.lock = threading.Lock()
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _open(self):
        dev = self.dev
        if not dev:
            cams = find_decxins()
            if not cams:
                self.status = "no DECXIN docked"
                return False
            dev = cams[0][0]
        # OpenCV 5 refuses V4L2 "by name"; the node number works on every version
        cap = cv2.VideoCapture(int(re.search(r"(\d+)$", dev)[1]), cv2.CAP_V4L2)
        if not cap.isOpened():
            h = holders(dev)
            self.status = f"{dev} busy - held by {', '.join(h)}" if h else f"{dev} will not open"
            self.busy_dev = dev
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4000)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1200)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
        ok, f = cap.read()
        if not ok or f is None:
            h = holders(dev)
            self.status = f"{dev} gives no frames" + (f" (held by {', '.join(h)})" if h else "")
            self.busy_dev = dev
            cap.release()
            return False
        self.cap, self.open_dev = cap, dev
        self.status = f"live {dev} {f.shape[1]}x{f.shape[0]}"
        return True

    def _run(self):
        t0, n = time.time(), 0
        while not self.stop:
            if self.cap is None:
                if not self._open():
                    time.sleep(2)
                    continue
            ok, f = self.cap.read()
            if not ok or f is None:
                self.status = "lost frames - reopening"
                self.cap.release()
                self.cap = None
                time.sleep(1)
                continue
            with self.lock:
                self.frame, self.seq = f, self.seq + 1
            n += 1
            if time.time() - t0 >= 1:
                self.rate, t0, n = n / (time.time() - t0), time.time(), 0

    def latest(self):
        with self.lock:
            return self.seq, self.frame


# ---------------------------------------------------------------- per-view analysis

class Ideal:
    """Expected colour of every cell for one dictionary/id, quiet ring included."""
    cache = {}

    @classmethod
    def get(cls, dname, mid):
        k = (dname, mid)
        if k not in cls.cache:
            d = get_dict(dname)
            g = d.markerSize + 2
            img = cv2.aruco.generateImageMarker(d, mid, g, borderBits=1)
            e = np.ones((g + 2, g + 2), np.uint8)
            e[1:-1, 1:-1] = (img > 127).astype(np.uint8)
            cls.cache[k] = e
        return cls.cache[k]


def analyse(gray, corners, dname, mid, eye_h):
    """Score one marker in one eye. Returns a dict; 'valid' False carries a reason."""
    c = corners.reshape(4, 2).astype(np.float32)
    E = Ideal.get(dname, mid)
    n = E.shape[0]                    # cells incl. quiet ring
    g = n - 2                         # marker incl. black border
    S = n * CELL
    sides = np.linalg.norm(c - np.roll(c, -1, axis=0), axis=1)
    ppc = float(sides.min() / g)
    obl = float(sides.min() / max(sides.max(), 1e-6))
    centre = c.mean(axis=0)
    r = {"ppc": ppc, "corners": c, "centre": centre, "valid": False}
    if ppc < MIN_PX_PER_CELL:
        r["why"] = "small"
        return r
    if obl < MAX_OBLIQUE:
        r["why"] = "oblique"
        return r
    dst = np.float32([[CELL, CELL], [S - CELL, CELL], [S - CELL, S - CELL], [CELL, S - CELL]])
    H = cv2.getPerspectiveTransform(c, dst)
    w = cv2.warpPerspective(gray, H, (S, S), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
    cells = w.reshape(n, CELL, n, CELL).transpose(0, 2, 1, 3)[:, :, MARGIN:CELL - MARGIN,
                                                             MARGIN:CELL - MARGIN]
    means = cells.mean(axis=(2, 3))
    inner = np.zeros_like(E, bool)
    inner[1:-1, 1:-1] = True
    black = means[(E == 0) & inner]
    white = means[(E == 1) & inner]
    if white.size == 0 or black.size == 0:
        r["why"] = "degenerate"
        return r
    b, wt = float(np.median(black)), float(np.median(white))
    contrast = wt - b
    r["contrast"] = contrast
    if contrast < MIN_CONTRAST:
        r["why"] = "low contrast"
        return r
    thr = (b + wt) / 2
    norm = np.clip((w - b) / contrast, 0, 1)

    # blur: steepest contrast-normalised edge inside the marker -> transition width
    gy, gx = np.gradient(norm[CELL:S - CELL, CELL:S - CELL])
    p99 = float(np.percentile(np.hypot(gx, gy), 99))
    blur = 1.0 / max(p99 * CELL, 1e-6)
    r["blur"] = blur
    if blur > MAX_BLUR_CELLS:
        r["why"] = "blurred"
        return r

    err = np.where(E == 1, (wt - means) / contrast, (means - b) / contrast)
    wrong = np.where(E[:, :, None, None] == 1, cells < thr, cells > thr).mean(axis=(2, 3))
    score = np.clip(np.maximum(err, wrong), 0, 1)

    # edge bow: trace the quiet-ring -> black-border step on each side
    bows = []
    eye_mid = np.array([gray.shape[1] / 2, eye_h / 2])
    if np.hypot(*(centre - eye_mid)) < CENTRE_ZONE * eye_h / 2:
        for k in range(4):
            img = np.rot90(norm, k)      # bring side k to the left
            x0, x1 = int(CELL * 0.3), int(CELL * 1.7)
            ys = np.arange(2 * CELL, S - 2 * CELL)
            prof = img[ys, x0:x1]
            prof = cv2.blur(prof, (1, 3))
            if prof[:, :CELL // 4].mean() < 0.7:     # quiet zone not white enough here
                continue
            xs = []
            for row in prof:
                below = np.nonzero(row < 0.5)[0]
                if below.size == 0 or below[0] == 0:
                    xs.append(np.nan)
                    continue
                i = below[0]
                a, bb = row[i - 1], row[i]
                xs.append(x0 + i - 1 + (a - 0.5) / max(a - bb, 1e-6))
            xs = np.array(xs)
            ok = ~np.isnan(xs)
            if ok.sum() < len(xs) * 0.8:
                continue
            fit = np.polyfit(ys[ok], xs[ok], 1)
            res = xs[ok] - np.polyval(fit, ys[ok])
            bows.append(float(np.abs(res).max() / (g * CELL) * 100))
    r.update(valid=True, score=score, flips=int((score[1:-1, 1:-1] >= FLIP).sum()),
             weak=int(((score[1:-1, 1:-1] >= WEAK) & (score[1:-1, 1:-1] < FLIP)).sum()),
             quiet_bad=int((score[~inner] >= FLIP).sum()),
             bow=max(bows) if bows else None, norm=norm)
    return r


def _inner_quads(gray, quad):
    """Dark 4-sided shapes inside a rejected quad. The detector keeps the OUTER of two
    nested contours, so on a white-margined face the candidate it rejects is the quiet
    zone, and the marker's own black square has to be found again inside it."""
    q = quad.reshape(4, 2)
    x0, y0 = np.floor(q.min(axis=0)).astype(int) - 3
    x1, y1 = np.ceil(q.max(axis=0)).astype(int) + 3
    x0, y0 = max(x0, 0), max(y0, 0)
    roi = gray[y0:y1, x0:x1]
    if min(roi.shape) < 24:
        return []
    _, bw = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    area = cv2.contourArea(q.astype(np.float32))
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if not 0.15 * area < a < 0.95 * area:
            continue
        p = cv2.approxPolyDP(c, 0.04 * cv2.arcLength(c, True), True)
        if len(p) != 4 or not cv2.isContourConvex(p):
            continue
        p = p.reshape(4, 2).astype(np.float32)
        cv2.cornerSubPix(roi, p, (5, 5), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
        out.append((p + [x0, y0]).astype(np.float32))
    return out


def _match(gray, q, dname, candidates):
    g = get_dict(dname).markerSize + 2
    C = 8
    S = g * C
    dst = np.float32([[0, 0], [S, 0], [S, S], [0, S]])
    limit = max(2, int((g - 2) ** 2 * 0.2))
    best = None
    for order in (q, q[::-1]):
        for k in range(4):
            c = np.ascontiguousarray(np.roll(order, -k, axis=0))
            w = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(c, dst), (S, S))
            m = w.reshape(g, C, g, C)[:, 2:-2, :, 2:-2].mean(axis=(1, 3))
            lo, hi = np.percentile(m, 10), np.percentile(m, 90)
            if hi - lo < MIN_CONTRAST:
                return None
            bits = m > (lo + hi) / 2
            ring = np.ones_like(bits, bool)
            ring[1:-1, 1:-1] = False
            if bits[ring].mean() > 0.15:          # border must be (nearly) all black
                return None
            for mid in candidates:
                E = Ideal.get(dname, mid)[1:-1, 1:-1].astype(bool)
                dist = int((E[1:-1, 1:-1] != bits[1:-1, 1:-1]).sum())
                if dist <= limit and (best is None or dist < best[2]):
                    best = (mid, c, dist)
    return best


def recover(gray, quad, dname, candidates):
    """A quad the detector rejected: does it read as one of `candidates` with a few bad
    bits? Only quads with a solid black border are considered, and only IDs we expect (or
    have already seen), so a random dark rectangle cannot become a marker. Returns
    (id, corners in canonical order, bit distance) or None."""
    if not candidates:
        return None
    q = quad.reshape(4, 2).astype(np.float32)
    for cand in [q] + _inner_quads(gray, q):
        hit = _match(gray, cand, dname, candidates)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- accumulation

class MarkerStats:
    def __init__(self, mid, n):
        self.id = mid
        self.n = n
        self.views = {"L": 0, "R": 0}
        self.total = {"L": 0, "R": 0}
        self.rejects = {}
        self.score_sum = {e: np.zeros((n, n)) for e in "LR"}
        self.bad_count = {e: np.zeros((n, n)) for e in "LR"}
        self.img_sum = np.zeros((n * CELL, n * CELL), np.float64)
        self.bows = []
        self.contrasts = []
        self.max_flips = 0
        self.last = None
        self.last_seen = 0.0
        self.dups = 0
        self.recovered = 0          # views the detector rejected and we matched by bits

    def add(self, eye, r):
        self.total[eye] += 1
        self.last_seen = time.time()
        if not r["valid"]:
            self.rejects[r["why"]] = self.rejects.get(r["why"], 0) + 1
            return
        self.views[eye] += 1
        self.score_sum[eye] += r["score"]
        self.bad_count[eye] += r["score"] >= WEAK
        self.img_sum += r["norm"]
        if r["bow"] is not None:
            self.bows.append(r["bow"])
            self.bows = self.bows[-400:]
        self.contrasts.append(r["contrast"])
        self.contrasts = self.contrasts[-400:]
        self.max_flips = max(self.max_flips, r["flips"])
        self.last = {"eye": eye, "flips": r["flips"], "weak": r["weak"]}

    def combined(self):
        nv = self.views["L"] + self.views["R"]
        if nv == 0:
            return nv, None, None
        mean = (self.score_sum["L"] + self.score_sum["R"]) / nv
        rate = (self.bad_count["L"] + self.bad_count["R"]) / nv
        return nv, mean, rate

    def eye_mean(self, eye):
        v = self.views[eye]
        return self.score_sum[eye] / v if v else None

    def summary(self):
        nv, mean, rate = self.combined()
        n = self.n
        inner = np.zeros((n, n), bool)
        inner[1:-1, 1:-1] = True
        d = {"id": self.id, "views": self.views, "seen": self.total, "rejects": self.rejects,
             "ago": round(time.time() - self.last_seen, 1), "dups": self.dups,
             "recovered": self.recovered,
             "max_flips_one_view": self.max_flips, "last": self.last,
             "contrast": round(float(np.median(self.contrasts)), 1) if self.contrasts else None,
             "bow_pct": round(float(np.median(self.bows)), 2) if self.bows else None,
             "bow_n": len(self.bows), "defects": [], "reasons": []}
        if mean is None:
            d["verdict"] = "NO GOOD VIEW"
            d["reasons"].append("never seen sharp/large/flat enough - bring it closer, hold still")
            return d
        mL, mR = self.eye_mean("L"), self.eye_mean("R")
        for (y, x) in zip(*np.nonzero((rate >= PERSIST_RATE) & (mean >= WEAK))):
            both = (mL is not None and mR is not None and self.views["L"] >= 4
                    and self.views["R"] >= 4 and mL[y, x] >= WEAK and mR[y, x] >= WEAK)
            kind = "quiet zone" if not inner[y, x] else (
                "border" if (y in (1, n - 2) or x in (1, n - 2)) else "bit")
            d["defects"].append({"row": int(y - 1), "col": int(x - 1), "kind": kind,
                                 "score": round(float(mean[y, x]), 2),
                                 "rate": round(float(rate[y, x]), 2), "both_eyes": bool(both),
                                 "flip": bool(mean[y, x] >= FLIP)})
        d["worst_cell"] = round(float(mean[inner].max()), 2)
        flips = [c for c in d["defects"] if c["flip"] and c["kind"] != "quiet zone"]
        weak = [c for c in d["defects"] if not c["flip"] and c["kind"] != "quiet zone"]
        quiet = [c for c in d["defects"] if c["kind"] == "quiet zone"]
        bad = suspect = False
        if flips:
            bad = True
            d["reasons"].append(f"{len(flips)} cell(s) read as the WRONG colour in most views")
        if weak:
            suspect = True
            d["reasons"].append(f"{len(weak)} cell(s) persistently off-colour (spot/void/fade)")
        if len(quiet) >= 2:
            suspect = True
            d["reasons"].append(f"white margin dirty/missing on {len(quiet)} cell(s)")
        if d["bow_pct"] is not None and len(self.bows) >= 5 and d["bow_pct"] > 1.0:
            suspect = True
            d["reasons"].append(f"border bows {d['bow_pct']}% of side - curled/bubbled/bent")
        if self.recovered >= 3 and self.recovered >= 0.2 * sum(self.total.values()):
            bad = True
            d["reasons"].append(f"the ArUco detector REJECTED it in {self.recovered} views -"
                                " only matched by its bits; too damaged to rely on")
        if self.dups:
            bad = True
            d["reasons"].append(f"ID seen twice in one frame {self.dups}x - duplicate face")
        if bad:
            d["verdict"] = "DEFECT"
        elif suspect:
            d["verdict"] = "SUSPECT"
        elif nv < MIN_VIEWS:
            d["verdict"] = "NEED VIEWS"
            d["reasons"].append(f"{nv}/{MIN_VIEWS} usable views so far")
        else:
            d["verdict"] = "GOOD"
        return d

    def composite(self):
        """ideal | average of all usable views | persistent-defect heatmap"""
        n = self.n
        P = 220
        nv, mean, rate = self.combined()
        E = self.ideal
        ideal = cv2.resize((E * 255).astype(np.uint8), (P, P), interpolation=cv2.INTER_NEAREST)
        ideal = cv2.cvtColor(ideal, cv2.COLOR_GRAY2BGR)
        if nv:
            avg = (self.img_sum / nv * 255).clip(0, 255).astype(np.uint8)
            avg = cv2.cvtColor(cv2.resize(avg, (P, P), interpolation=cv2.INTER_AREA),
                               cv2.COLOR_GRAY2BGR)
            heat = np.zeros((n, n, 3), np.uint8)
            m = np.clip(mean / FLIP, 0, 1)
            heat[..., 1] = (200 * (1 - m)).astype(np.uint8) + 30   # green -> red (BGR)
            heat[..., 2] = (230 * m).astype(np.uint8) + 25
            heat[..., 0] = 40
            heat = cv2.resize(heat, (P, P), interpolation=cv2.INTER_NEAREST)
            cs = P / n
            for y, x in zip(*np.nonzero((rate >= PERSIST_RATE) & (mean >= WEAK))):
                col = (0, 0, 255) if mean[y, x] >= FLIP else (0, 200, 255)
                cv2.rectangle(heat, (int(x * cs) + 2, int(y * cs) + 2),
                              (int((x + 1) * cs) - 2, int((y + 1) * cs) - 2), col, 2)
                cv2.rectangle(avg, (int(x * cs) + 1, int(y * cs) + 1),
                              (int((x + 1) * cs) - 1, int((y + 1) * cs) - 1), col, 2)
            for i in range(n + 1):
                v = int(i * cs)
                cv2.line(heat, (v, 0), (v, P), (25, 25, 25), 1)
                cv2.line(heat, (0, v), (P, v), (25, 25, 25), 1)
        else:
            avg = np.full((P, P, 3), 40, np.uint8)
            heat = np.full((P, P, 3), 40, np.uint8)
        gap = np.full((P, 8, 3), 255, np.uint8)
        return np.hstack([ideal, gap, avg, gap, heat])


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self, cam, dict_name, expected):
        self.cam = cam
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(2)
        self.jpeg = None
        self.jpeg_seq = 0
        self.proc_rate = 0.0
        self.expected = expected
        self.error = None
        self.set_dict(dict_name)
        threading.Thread(target=self._run, daemon=True).start()

    def set_dict(self, name):
        with self.lock:
            self.dict_req = name
            self.dict = None if name == "auto" else name
            self.detector = make_detector(self.dict) if self.dict else None
            self.sniff = {}
            self.markers = {}

    def reset(self):
        with self.lock:
            self.markers = {}

    # auto: try every family on the left eye until one clearly wins
    def _sniff(self, eyes):
        """Only IDs found in BOTH eyes of the same frame count - a real marker is in
        both views, a false positive on clutter almost never is."""
        for fam in FAMILIES:
            name = fam + "_1000" if fam in ("4X4", "5X5", "6X6", "7X7") else fam
            det = make_detector(name)
            found = []
            for g in eyes:
                _, ids, _ = det.detectMarkers(g)
                found.append(set() if ids is None else set(ids.ravel().tolist()))
            both = found[0] & found[1]
            if both:
                s = self.sniff.setdefault(fam, [0, 0])
                s[0] += 1
                s[1] = max(s[1], max(both))
        if self.sniff and max(v[0] for v in self.sniff.values()) >= 6:
            fam, (cnt, mx) = max(self.sniff.items(), key=lambda kv: kv[1][0])
            with self.lock:
                self.dict = narrow(fam, mx)
                self.detector = make_detector(self.dict)

    def _run(self):
        last_seq, t0, n = -1, time.time(), 0
        while True:
            seq, frame = self.cam.latest()
            if frame is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            try:
                self._process(frame)
                self.error = None
            except Exception as e:          # one bad frame must not stop the analysis
                import traceback
                self.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                time.sleep(0.2)
            n += 1
            if time.time() - t0 >= 1:
                self.proc_rate, t0, n = n / (time.time() - t0), time.time(), 0

    def _process(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, W = gray.shape
        eyes = [gray[:, :W // 2].copy(), gray[:, W // 2:].copy()]
        for e in eyes:      # the DECXIN's in-frame timestamp strip reads as a pile of markers
            e[:STRIP[1], :STRIP[0]] = 128
        if self.detector is None:
            self._sniff([cv2.resize(e, None, fx=0.5, fy=0.5) for e in eyes])
            self._publish(frame, [], [])
            return
        det, dname = self.detector, self.dict
        results = list(self.pool.map(lambda e: det.detectMarkers(e), eyes))
        nmax = len(get_dict(dname).bytesList)
        gmin = (get_dict(dname).markerSize + 2) * MIN_PX_PER_CELL
        found = []                                   # per eye: [(id, corners, recovered)]
        for eye_i, (corners, ids, rejected) in enumerate(results):
            ids = [] if ids is None else ids.ravel().tolist()
            hits = [(m, c, False) for m, c in zip(ids, corners)]
            # recovery only ever looks for IDs the user said are on the cube: matching
            # against IDs we found ourselves let clutter reinforce its own false IDs
            cands = [i for i in self.expected if i < nmax and i not in ids]
            if cands:
                rej = sorted((q for q in rejected
                              if cv2.contourArea(q.reshape(4, 2)) >= gmin * gmin),
                             key=lambda q: -cv2.contourArea(q.reshape(4, 2)))[:25]
                got = set()
                for rq in rej:
                    hit = recover(eyes[eye_i], rq, dname, cands)
                    if hit and hit[0] not in got:
                        got.add(hit[0])
                        hits.append((hit[0], hit[1].reshape(1, 4, 2), True))
            found.append(hits)
        # a view only counts when the same ID is in BOTH eyes this frame: a real marker in
        # front of you always is, a false hit on clutter or the frame edge almost never
        both = {m for m, _, _ in found[0]} & {m for m, _, _ in found[1]}
        dup_both = {m for m in both
                    if all(sum(1 for x, _, _ in f if x == m) > 1 for f in found)}
        drawn = []
        for eye_i, hits in enumerate(found):
            for mid, c, rec in hits:
                if mid not in both:
                    drawn.append((eye_i, mid, {"corners": c.reshape(4, 2), "valid": False,
                                               "why": "one eye"}))
                    continue
                r = analyse(eyes[eye_i], c, dname, mid, h)
                with self.lock:
                    if self.dict != dname:
                        return
                    ms = self.markers.get(mid)
                    if ms is None:
                        ms = MarkerStats(mid, Ideal.get(dname, mid).shape[0])
                        ms.ideal = Ideal.get(dname, mid)
                        self.markers[mid] = ms
                    if mid in dup_both and eye_i == 0:
                        ms.dups += 1
                    if rec:
                        ms.recovered += 1
                    ms.add("LR"[eye_i], r)
                drawn.append((eye_i, mid, r))
        self._publish(frame, drawn, eyes)

    def _publish(self, frame, drawn, eyes):
        s = 0.4
        disp = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        half = disp.shape[1] // 2
        cv2.line(disp, (half, 0), (half, disp.shape[0]), (80, 80, 80), 1)
        for eye_i, mid, r in drawn:
            pts = (r["corners"] * s + [eye_i * half, 0]).astype(np.int32)
            if not r["valid"]:
                col, label = (150, 150, 150), f"{mid} ({r['why']})"
            elif r["flips"]:
                col, label = (40, 40, 255), f"{mid}  {r['flips']} bad"
            elif r["weak"]:
                col, label = (0, 190, 255), f"{mid}  {r['weak']} weak"
            else:
                col, label = (60, 220, 60), f"{mid}"
            cv2.polylines(disp, [pts], True, col, 2)
            cv2.circle(disp, tuple(pts[0]), 4, col, -1)
            org = (int(pts[:, 0].min()), int(pts[:, 1].min()) - 6)
            cv2.putText(disp, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4)
            cv2.putText(disp, label, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1)
        for e in range(2):
            cv2.rectangle(disp, (e * half, 0), (e * half + int(STRIP[0] * s), int(STRIP[1] * s)),
                          (60, 60, 60), 1)
        if self.detector is None:
            cv2.putText(disp, "detecting dictionary... show a marker", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255), 2)
        ok, buf = cv2.imencode(".jpg", disp, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.jpeg, self.jpeg_seq = buf.tobytes(), self.jpeg_seq + 1

    def state(self):
        with self.lock:
            ms = [m.summary() for m in sorted(self.markers.values(), key=lambda m: m.id)]
            seen = set(self.markers)
        exp = self.expected
        return {"camera": self.cam.status, "cam_fps": round(self.cam.rate, 1),
                "proc_fps": round(self.proc_rate, 1), "dict": self.dict, "error": self.error,
                "dict_req": self.dict_req, "dict_choices": DICT_CHOICES,
                "sniff": {k: v[0] for k, v in self.sniff.items()},
                "expected": exp, "missing": [i for i in exp if i not in seen],
                "unexpected": sorted(seen - set(exp)) if exp else [],
                "markers": ms, "held_by_camrig": "camrig" in self.cam.status
                or "python3" in self.cam.status}

    def composite_png(self, mid):
        import copy
        with self.lock:
            m = self.markers.get(mid)
            m = copy.deepcopy(m) if m else None
        if m is None:
            return None
        return cv2.imencode(".png", m.composite())[1].tobytes()

    def frame_after(self, seq, timeout=1.0):
        """Long-poll: the next annotated frame newer than `seq`."""
        t = time.time() + timeout
        while self.jpeg_seq <= seq and time.time() < t:
            time.sleep(0.01)
        return self.jpeg_seq, self.jpeg

    def save(self):
        d = os.path.expanduser(f"~/Desktop/arucocube_{datetime.now():%Y%m%d_%H%M%S}")
        os.makedirs(d, exist_ok=True)
        st = self.state()
        with open(f"{d}/report.json", "w") as f:
            json.dump(st, f, indent=2, default=str)
        lines = [f"# ArUco cube check {datetime.now():%Y-%m-%d %H:%M}", "",
                 f"dictionary {st['dict']}  camera {st['camera']}", ""]
        if st["missing"]:
            lines.append(f"**never seen:** {st['missing']}")
        if st["unexpected"]:
            lines.append(f"**unexpected IDs:** {st['unexpected']}")
        lines += ["", "| ID | verdict | views L/R | worst cell | bow % | reasons |",
                  "|---|---|---|---|---|---|"]
        for m in st["markers"]:
            lines.append(f"| {m['id']} | {m['verdict']} | {m['views']['L']}/{m['views']['R']}"
                         f" | {m.get('worst_cell', '-')} | {m['bow_pct'] or '-'} | "
                         f"{'; '.join(m['reasons'])} |")
            png = self.composite_png(m["id"])
            if png:
                open(f"{d}/marker_{m['id']}.png", "wb").write(png)
        open(f"{d}/report.md", "w").write("\n".join(lines) + "\n")
        return d


# ---------------------------------------------------------------- http

def make_handler(eng, cam):
    ui = os.path.join(HERE, "ui.html")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self._send(200, open(ui, "rb").read(), "text/html; charset=utf-8")
            if p == "/api/state":
                return self._json(eng.state())
            m = re.fullmatch(r"/marker/(\d+)\.png", p)
            if m:
                png = eng.composite_png(int(m[1]))
                return self._send(200, png, "image/png") if png else self._send(404, b"", "text/plain")
            if p == "/frame.jpg":
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                seq, j = eng.frame_after(int(q.get("after", ["-1"])[0]))
                if j is None:
                    return self._send(503, b"", "text/plain")
                self.send_response(200)
                self.send_header("content-type", "image/jpeg")
                self.send_header("cache-control", "no-store")
                self.send_header("x-seq", str(seq))
                self.send_header("content-length", str(len(j)))
                self.end_headers()
                self.wfile.write(j)
                return
            if p == "/stream":
                self.send_response(200)
                self.send_header("content-type", "multipart/x-mixed-replace; boundary=f")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                last = -1
                try:
                    while True:
                        if eng.jpeg_seq == last or eng.jpeg is None:
                            time.sleep(0.01)
                            continue
                        last, j = eng.jpeg_seq, eng.jpeg
                        self.wfile.write(b"--f\r\ncontent-type: image/jpeg\r\ncontent-length: "
                                         + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            p = self.path
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/reset":
                eng.reset()
                return self._json({"ok": True})
            if p == "/api/config":
                if "dict" in body and body["dict"] in DICT_CHOICES:
                    eng.set_dict(body["dict"])
                if "expected" in body:
                    eng.expected = parse_ids(body["expected"])
                return self._json({"ok": True})
            if p == "/api/save":
                return self._json({"ok": True, "dir": eng.save()})
            if p == "/api/take":       # ask camrig to let go of the node
                dev = getattr(cam, "busy_dev", None) or cam.dev
                try:
                    req = urllib.request.Request(
                        "http://localhost:8088/api/close", method="POST",
                        data=json.dumps({"dev": dev}).encode(),
                        headers={"content-type": "application/json"})
                    urllib.request.urlopen(req, timeout=5).read()
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
            self._send(404, b"not found", "text/plain")

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", help="capture node (default: first docked DECXIN)")
    ap.add_argument("--dict", default="auto", choices=DICT_CHOICES)
    ap.add_argument("--expected", default="", help="marker IDs on the cube, e.g. 0-5")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--port", type=int, default=8786)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}"
    try:            # bind first: a second double-click must not grab the camera too
        srv = ThreadingHTTPServer((a.host, a.port), BaseHTTPRequestHandler)
    except OSError:
        print(f"arucocube already running at {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    cam = Camera(a.dev, a.fps)
    eng = Engine(cam, a.dict, parse_ids(a.expected))
    srv.RequestHandlerClass = make_handler(eng, cam)
    print(f"arucocube on {url}")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
