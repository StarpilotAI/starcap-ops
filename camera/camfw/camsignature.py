#!/usr/bin/env python3
"""Is this camera an OLD-behaviour or NEW-behaviour unit? — 10 seconds, no take.

Reads the camera's own exposure timestamps out of the in-frame IMU strip and
looks at how it quantises two things. Measured on real fleet data:

    JUL 2026 units   interval 16640 us, ONE value    exposure 7495 us, ONE value
                     zero jitter events in 53 minutes
    AUG 2026 onward  interval 16626/16627, dithers   exposure 4993/4994, dithers
                     jitter present in every take

A dithering frame length means an active frame-timing loop is running every
frame. The jitter fault (two frames at the readout floor, then one long catch-up)
is that loop glitching, so which family a unit belongs to predicts whether it can
produce the fault at all.

Only the 160-px strip is pulled off the camera, not the whole 4000x1200 frame, so
this costs ~180 KB a frame instead of 7.2 MB.

    ./camsignature.py [/dev/videoN] [seconds]
"""
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from imu_strip import StripDecoder                            # noqa: E402

W, H, STRIP = 4000, 1200, 160


def main():
    dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/video0"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    dec = StripDecoder(W)
    rows = dec.rows_needed()
    n = int(secs * 60)
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", f"{W}x{H}", "-i", dev, "-frames:v", str(n),
         "-vf", f"crop={STRIP}:{rows}:0:0", "-pix_fmt", "nv12",
         "-f", "rawvideo", "-"], capture_output=True)
    fsz = STRIP * rows * 3 // 2
    frames = len(p.stdout) // fsz
    if frames < 30:
        print(f"only {frames} frames off {dev} — is it in use?\n"
              f"{(p.stderr or b'').decode()[:300]}")
        return 1

    starts, expos = [], []
    for i in range(frames):
        buf = np.frombuffer(p.stdout[i*fsz:(i+1)*fsz], np.uint8)
        exp, _ = dec.decode(buf[:rows*STRIP], STRIP)
        if exp:
            starts.append(exp[0])
            expos.append((exp[1] - exp[0]) & 0xFFFFFFFF)
    if len(starts) < 30:
        print(f"strip decoded on only {len(starts)}/{frames} frames")
        return 1

    es = np.array(starts, dtype=np.int64)
    # Drop the capture warm-up. The first frames off a freshly opened device
    # repeat one timestamp until the strip starts updating, and a run of zero
    # intervals otherwise reads as a long burst of "early" frames — which is
    # exactly the fault this is looking for, invented out of nothing.
    # Drop EVERY duplicate stamp, not just the leading warm-up block. A freshly
    # opened device repeats one timestamp for ~80 frames, and a stray repeat
    # turns up later too; each one is a zero interval, which reads as a frame
    # that arrived infinitely early - i.e. it invents the exact fault this is
    # looking for. Keep the count: a high rate means the camera stopped stamping
    # every frame and no timing number here can be trusted.
    dup = int((np.diff(es) == 0).sum())
    es = es[np.concatenate(([True], np.diff(es) > 0))]
    iv = np.diff(es)
    iv = iv[(iv > 1000) & (iv < 100000)]
    mode = int(np.bincount(iv).argmax())
    from collections import Counter
    near = Counter(iv[np.abs(iv - mode) <= 3].tolist())
    ec = Counter(expos)

    ok = np.abs(np.diff(es) - mode) <= 1
    d = np.diff(es)
    ev, i = [], 0
    while i < len(d):
        if not ok[i]:
            j = i
            while j < len(d) and not ok[j]:
                j += 1
            ev.append(d[i:j]); i = j
        else:
            i += 1
    njit = sum(1 for s in ev if any(x < mode - 3000 for x in s))

    print(f"{dev}   {len(es)} frames   {(es[-1]-es[0])/1e6:.1f} s"
          f"   ({dup} duplicate stamps dropped)")
    print(f"  frame interval : {dict(sorted(near.items(), key=lambda x: -x[1])[:3])}"
          f"   -> {1e6/mode:.3f} fps")
    print(f"  exposure       : {dict(sorted(ec.items(), key=lambda x: -x[1])[:3])}")
    print(f"  jitter events  : {njit}")
    iv_vals, ex_vals = len(near), len(ec)
    print()
    if iv_vals == 1 and ex_vals == 1:
        print("  => OLD behaviour: both static. No frame-timing loop running.")
    elif iv_vals > 1:
        print("  => NEW behaviour: the frame length DITHERS, so a timing loop is")
        print("     running every frame — this unit can produce the jitter fault.")
    else:
        print("  => mixed/unknown: interval static but exposure dithers.")
    return 0


sys.exit(main())
