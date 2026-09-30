#!/usr/bin/env python3
"""Derive per-eye WB gains from one frame, verify them on a DIFFERENT frame.

The DECXIN exposes a single global white_balance_temperature for both sensors,
so a per-eye cast cannot be corrected on the camera - it has to be done here.
"""
import sys
import numpy as np
from PIL import Image

HALF_TRIM = .12  # margin trim: MJPEG decode corruption lands on frame edges

def load(path):
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float64)

def eye_slices(w):
    half = w // 2
    return slice(0, half), slice(half, w)

def sample(px):
    """Grey-world ratios over mid-tones only."""
    lum = px.mean(axis=2)
    k = (lum > 25) & (lum < 235)
    if k.sum() < 500:
        k = np.ones_like(lum, dtype=bool)
    q = px[k]
    return q[:, 0].mean() / q[:, 1].mean(), q[:, 2].mean() / q[:, 1].mean()

def inner(a, es):
    """Central region of one eye, away from edges and the stereo seam."""
    h, w, _ = a.shape
    half = w // 2
    m = int(h * HALF_TRIM)
    x0 = es.start + int(half * HALF_TRIM)
    return a[m:h - m, x0:x0 + int(half * (1 - 2 * HALF_TRIM))]

def derive(path):
    a = load(path)
    gains = []
    for es in eye_slices(a.shape[1]):
        rg, bg = sample(inner(a, es))
        gains.append((1.0 / rg, 1.0 / bg))
    return gains

def apply_gains(a, gains):
    out = a.copy()
    for es, (gr, gb) in zip(eye_slices(a.shape[1]), gains):
        out[:, es, 0] *= gr
        out[:, es, 2] *= gb
    return np.clip(out, 0, 255)

calib, verify = sys.argv[1], sys.argv[2]
gains = derive(calib)
print(f"gains derived from {calib}:")
for name, (gr, gb) in zip(("LEFT ", "RIGHT"), gains):
    print(f"   {name}  R x{gr:.4f}   G x1.0000   B x{gb:.4f}")

print(f"\napplied to a DIFFERENT frame ({verify}) - not the one they were fitted on:")
a = load(verify)
fixed = apply_gains(a, gains)
for tag, img in (("before", a), ("after ", fixed)):
    r = []
    for es in eye_slices(img.shape[1]):
        r.append(sample(inner(img, es)))
    (lrg, lbg), (rrg, rbg) = r
    print(f"   {tag}:  L R/G={lrg:5.3f} B/G={lbg:5.3f}   |   "
          f"R R/G={rrg:5.3f} B/G={rbg:5.3f}   |   "
          f"L/R delta R/G={rrg-lrg:+.3f} B/G={rbg-lbg:+.3f}")

Image.fromarray(fixed.astype(np.uint8)).resize((1000, 300), Image.LANCZOS) \
     .save("view_after.jpg", quality=88)
Image.fromarray(a.astype(np.uint8)).resize((1000, 300), Image.LANCZOS) \
     .save("view_raw.jpg", quality=88)

# stack before/after for a single visual comparison
top = Image.open("view_raw.jpg"); bot = Image.open("view_after.jpg")
comp = Image.new("RGB", (1000, 604), "black")
comp.paste(top, (0, 0)); comp.paste(bot, (0, 304))
comp.save("view_compare.jpg", quality=90)
print("\nwrote view_compare.jpg (top = as captured, bottom = per-eye corrected)")
