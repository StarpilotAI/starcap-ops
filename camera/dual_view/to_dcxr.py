#!/usr/bin/env python3
"""Pack an export.py camera folder's frames into a .dcxr (Youssef's container), and verify it.

    python3 to_dcxr.py CAMDIR        # CAMDIR/frames/*.jpg + CAMDIR/frames.csv -> CAMDIR/frames.dcxr

.dcxr = b"DCXR0001", then per frame: u64 LE t_ns (host CLOCK_MONOTONIC) + u32 LE jpeg length + the camera's JPEG.
"""
import csv, struct, sys
from pathlib import Path

d = Path(sys.argv[1])
rows = list(csv.DictReader(open(d / "frames.csv")))
jpgs = sorted((d / "frames").glob("*.jpg"))
assert len(rows) == len(jpgs), (len(rows), len(jpgs))
with open(d / "frames.dcxr", "wb") as f:
    f.write(b"DCXR0001")
    for r, p in zip(rows, jpgs):
        j = p.read_bytes()
        f.write(struct.pack("<QI", int(r["t_host_monotonic_ns"]), len(j))); f.write(j)
# read back: every record must match its source jpg and stamp, and the file must end exactly
data = (d / "frames.dcxr").read_bytes(); off = 8; n = 0
assert data[:8] == b"DCXR0001"
for r, p in zip(rows, jpgs):
    t, ln = struct.unpack_from("<QI", data, off)
    assert t == int(r["t_host_monotonic_ns"]) and data[off + 12:off + 12 + ln] == p.read_bytes(), p
    off += 12 + ln; n += 1
assert off == len(data)
print(f"{d}: {n} frames, {len(data) / 1e9:.2f} GB, verified", flush=True)
