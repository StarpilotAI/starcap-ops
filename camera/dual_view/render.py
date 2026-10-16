#!/usr/bin/env python3
"""Render a dual_view recording to one H.264 mp4: the 168 across the top, the two monos below (mono_8-1 left,
mono_2-2 right), with ArUco markers (DICT_4X4_50) detected at full res on the monos and drawn on.

    python3 render.py ~/Videos/20261008_215423      # -> <stamp>_combined_aruco.mp4 + <stamp>_aruco.csv

Streams are lined up by their first CLOCK_MONOTONIC stamp (leading frames dropped from the earlier starters).
"""
import csv, subprocess, sys
import cv2
import numpy as np

W, TOP_H, MONO_W, MONO_H = 1920, 576, 960, 554
TILES = [("168", None), ("mono_8-1", "left"), ("mono_2-2", "right")]

stem = sys.argv[1]
first = {k: int(open(f"{stem}_{k}.csv").read().split()[1]) for k, _ in TILES}
start = max(first.values())
caps = {}
for k, _ in TILES:
    caps[k] = cv2.VideoCapture(f"{stem}_{k}.avi")
    for _ in range(round((start - first[k]) / 16.67e6)):
        caps[k].grab()
det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), cv2.aruco.DetectorParameters())
out = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{TOP_H + MONO_H}",
                        "-r", "60", "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", f"{stem}_combined_aruco.mp4"], stdin=subprocess.PIPE)
log = csv.writer(open(f"{stem}_aruco.csv", "w", newline=""))
log.writerow(["frame", "camera", "id", "cx", "cy"])
n = seen = 0
while True:
    frames = {k: caps[k].read()[1] for k, _ in TILES}
    if any(f is None for f in frames.values()):
        break
    row = []
    for k, side in TILES[1:]:
        f = frames[k]
        corners, ids, _ = det.detectMarkers(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
        if ids is not None:
            seen += 1
            for c, i in zip(corners, ids.flatten()):
                cx, cy = c[0].mean(axis=0)
                log.writerow([n, k, int(i), f"{cx:.1f}", f"{cy:.1f}"])
                cv2.polylines(f, [c.astype(np.int32)], True, (0, 255, 0), 6)
                cv2.putText(f, str(i), (int(cx) - 20, int(cy) + 15), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 6)
        t = cv2.resize(f, (MONO_W, MONO_H), interpolation=cv2.INTER_AREA)
        cv2.putText(t, f"{k}  aruco: {0 if ids is None else len(ids)}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        row.append(t)
    top = cv2.resize(frames["168"], (W, TOP_H), interpolation=cv2.INTER_AREA)
    out.stdin.write(np.vstack([top, np.hstack(row)]).tobytes())
    n += 1
out.stdin.close(); out.wait()
print(f"{stem}_combined_aruco.mp4: {n} frames, markers in {seen}/{2 * n} mono frames", flush=True)
