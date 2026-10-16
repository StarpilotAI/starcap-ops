#!/usr/bin/env python3
"""Export a dual_view recording as raw frames + decoded IMU, one folder per camera:

    python3 export.py ~/Videos/20261008_215423 OUTDIR
      OUTDIR/<stamp>/<camera>/frames/000000.jpg   the camera's own JPEG, byte-for-byte (ffmpeg -c copy)
      OUTDIR/<stamp>/<camera>/frames.csv          frame, t_host_monotonic_ns, exposure_start_us, exposure_end_us
      OUTDIR/<stamp>/<camera>/imu.csv             t_us, ax, ay, az (m/s^2), gx, gy, gz (deg/s)

IMU + exposure come from the strip DECXIN cameras embed in the top-left of every frame (camfw/lib/imu_strip.py);
t_us and exposure_*_us are the camera's own microsecond clock, ~600 Hz at 60 fps.
"""
import csv, subprocess, sys
from pathlib import Path
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "camfw" / "lib"))
from imu_strip import StripDecoder

stem, outdir = Path(sys.argv[1]), Path(sys.argv[2])
for cam in ("168", "mono_2-2", "mono_8-1"):
    d = outdir / stem.name / cam
    (d / "frames").mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", f"{stem}_{cam}.avi", "-c", "copy",
                    "-start_number", "0", str(d / "frames" / "%06d.jpg")], check=True)
    host = open(f"{stem}_{cam}.csv").read().split()[1:]
    jpgs = sorted((d / "frames").glob("*.jpg"))
    dec = StripDecoder(0)
    with open(d / "frames.csv", "w", newline="") as ff, open(d / "imu.csv", "w", newline="") as fi:
        fw, iw = csv.writer(ff), csv.writer(fi)
        fw.writerow(["frame", "t_host_monotonic_ns", "exposure_start_us", "exposure_end_us"])
        iw.writerow(["t_us", "ax", "ay", "az", "gx", "gy", "gz"])
        n_imu = 0
        for i, p in enumerate(jpgs):
            g = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            r = dec.decode(np.ascontiguousarray(g).tobytes(), g.shape[1])
            exp = r[0] if r and r[0] else ("", "")
            fw.writerow([i, host[i] if i < len(host) else "", *exp])
            for t, a, gy in (r[1] if r else []):
                iw.writerow([t, *(f"{v:.5f}" for v in a), *(f"{v:.4f}" for v in gy)]); n_imu += 1
    print(f"{stem.name} {cam}: {len(jpgs)} frames (host stamps {len(host)}), {n_imu} imu samples, "
          f"{dec.bad_frames} unreadable strips", flush=True)
