"""
checks.py: the measurements behind StarCap QC, as pure functions.

No board, no network, no state: each function takes numbers or pixels and returns
a plain dict, so every threshold here can be tested offline (tests/test_checks.py)
and qc.py only decides what is a pass.
"""
import numpy as np

WRAP_U32 = float(1 << 32)


def unwrap_u32(values):
    """The camera's microsecond clock is a u32 and wraps every ~71.6 minutes, so a
    5-minute take straddles a wrap ~7% of the time. Undo it before any difference.
    Same rule as the dashboard's frames_timing_worker.unwrap."""
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return v
    jumps = np.diff(v) < -WRAP_U32 / 2
    return v + np.concatenate(([0.0], np.cumsum(jumps) * WRAP_U32))


# --------------------------------------------------------------------------- #
# frame timing: the customer's p99 spec
# --------------------------------------------------------------------------- #
# Spec (Robotics Data Requirements 2026-07-24, section 2.4 "Timing quality"):
#     99th percentile frame-to-frame interval within +-10 ms from nominal FPS
# Judged the way the house judges it (delivery's packet_qa, firstSample's frame_select,
# the dashboard's frame-timing score): two-sided, i.e. no more than 1% of intervals may
# fall outside [T-10ms, T+10ms] with T = 16.667 ms. Equivalently p99(|dt - T|) <= 10 ms.
# A dropped frame is one ~2T interval (a deviation of T), so more than 1% drops fails.
# Intervals are taken in FILE ORDER and never sorted: a backwards or repeated stamp is
# a fault of its own, not something to tidy away.
SPEC_TOL_S = 0.010
SPEC_MAX_OUT_PCT = 1.0


def frame_timing(ts, nominal_fps, tol_s=SPEC_TOL_S, max_out_pct=SPEC_MAX_OUT_PCT):
    """ts: frame timestamps in seconds (any monotonic clock), in capture order.

    Returns the numbers the report shows. `pass` is the spec verdict; `reason`
    says why it failed in words."""
    t = np.asarray(ts, dtype=np.float64)
    return interval_timing([np.diff(t)] if t.size >= 2 else [], nominal_fps, tol_s, max_out_pct)


def interval_timing(dt_parts, nominal_fps, tol_s=SPEC_TOL_S, max_out_pct=SPEC_MAX_OUT_PCT):
    """Same verdict as frame_timing, from frame intervals in seconds. Takes a LIST of
    interval arrays so a take split across files is judged as one population without
    inventing the unmeasurable gap between the last frame of one file and the first
    of the next.

    Verdict: at most `max_out_pct` % of intervals outside +-tol of the nominal period,
    and no repeated or backwards stamps."""
    parts = [np.asarray(p, dtype=np.float64) for p in dt_parts if len(p)]
    dt = np.concatenate(parts) if parts else np.zeros(0)
    out = {"frames": int(dt.size + len(parts)), "nominal_fps": float(nominal_fps)}
    if dt.size < 2:
        out.update(ok=False, reason=f"only {dt.size} frame intervals", **{"pass": False})
        return out
    T = 1.0 / float(nominal_fps)
    back = int(np.sum(dt <= 0))            # duplicated or backwards timestamps
    span = float(np.sum(dt))
    dev = np.abs(dt - T)
    p99_dev = float(np.percentile(dev, 99))
    outside = int(np.sum(dev > tol_s))
    out_pct = 100.0 * outside / dt.size
    out.update({
        "ok": True,
        "duration_s": span,
        "avg_fps": dt.size / span if span > 0 else 0.0,
        "interval_ms": {"mean": float(np.mean(dt) * 1e3), "p1": float(np.percentile(dt, 1) * 1e3),
                        "p50": float(np.percentile(dt, 50) * 1e3), "p99": float(np.percentile(dt, 99) * 1e3),
                        "max": float(np.max(dt) * 1e3)},
        "p99_dev_ms": p99_dev * 1e3,
        "outside_tol": outside,
        "outside_pct": out_pct,
        "late_gaps": int(np.sum(dt > 1.5 * T)),      # a missing frame or worse
        "nonincreasing": back,
        "tol_ms": tol_s * 1e3,
        "max_out_pct": max_out_pct,
    })
    out["pass"] = out_pct <= max_out_pct and back == 0
    if back:
        out["reason"] = f"{back} timestamps repeat or go backwards"
    elif not out["pass"]:
        out["reason"] = (f"{out_pct:.2f}% of frame intervals are more than {tol_s * 1e3:.0f} ms off "
                         f"(limit {max_out_pct:g}%); p99 deviation {p99_dev * 1e3:.1f} ms")
    return out


# --------------------------------------------------------------------------- #
# IMU
# --------------------------------------------------------------------------- #
G = 9.80665


def imu_stats(t, accel, gyro_dps=None, accel_unit="m/s2", still_dps=3.0):
    """t: sample times (s). accel: Nx3. gyro_dps: Nx3 in deg/s, optional.

    Rate is samples over the span. Gravity is judged only on STILL stretches (gyro
    under `still_dps`), because a cap being handled reads anything from 0 to 3 g, and
    a set-down cap reads a clean 1 g whichever way it lies. A sensor that repeats one
    value on every axis is stuck (accel_flat / gyro_flat): a live MEMS sensor always
    shows noise in its last digits."""
    t = np.asarray(t, dtype=np.float64)
    a = np.asarray(accel, dtype=np.float64).reshape(-1, 3)
    out = {"samples": int(t.size)}
    if t.size < 10:
        out.update(ok=False, reason=f"only {t.size} IMU samples")
        return out
    span = float(t[-1] - t[0])
    dt = np.diff(t)
    out["rate_hz"] = (t.size - 1) / span if span > 0 else 0.0
    out["max_gap_ms"] = float(np.max(dt) * 1e3) if dt.size else 0.0
    out["nonincreasing"] = int(np.sum(dt <= 0))
    out["span_s"] = span
    scale = G if accel_unit == "g" else 1.0
    mag = np.linalg.norm(a, axis=1) * scale
    still = np.ones(t.size, dtype=bool)
    out["gyro_flat"] = False
    if gyro_dps is not None:
        g = np.asarray(gyro_dps, dtype=np.float64).reshape(-1, 3)
        out["gyro_flat"] = bool(np.all(np.ptp(g, axis=0) == 0))
        still = np.linalg.norm(g, axis=1) < still_dps
    out["still_frac"] = float(np.mean(still))
    use = mag[still] if np.sum(still) >= 50 else mag
    out["gravity_ms2"] = float(np.median(use))
    out["accel_flat"] = bool(np.all(np.ptp(a, axis=0) == 0))   # a stuck sensor repeats one value
    out["ok"] = True
    return out


# --------------------------------------------------------------------------- #
# image checks on a live stereo frame
# --------------------------------------------------------------------------- #
def raw_eyes(frame, right_cols=(160, 2080), left_cols=(2080, 4000)):
    """The DECXIN's raw 4000x1200 frame -> (physical left eye, physical right eye).

    The camera lays the frame out as [IMU strip 0-160 | RIGHT lens 160-2080 | LEFT lens
    2080-4000] (the eyes are swapped on the wire; the recorder un-swaps them when it
    writes left_/right_). These are the recorder's own crop columns (app.py:287-291)."""
    return frame[:, left_cols[0]:left_cols[1]], frame[:, right_cols[0]:right_cols[1]]


def _gray(img):
    import cv2
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def eye_metrics(eye_bgr):
    """Exposure, contrast, sharpness and colour for one eye."""
    import cv2
    g = _gray(eye_bgr)
    small = cv2.resize(g, (g.shape[1] // 4, g.shape[0] // 4), interpolation=cv2.INTER_AREA)
    p1, p50, p99 = (float(x) for x in np.percentile(small, (1, 50, 99)))
    hist = np.bincount(small.ravel(), minlength=256)
    # sharpness on the middle half, full resolution: the edges of a wide lens are soft
    # on every good unit, and the bench background behind the edges varies the most
    h, w = g.shape
    mid = g[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    lap = cv2.Laplacian(mid, cv2.CV_64F)
    m = {"mean": float(small.mean()), "std": float(small.std()),
         "p1": p1, "p50": p50, "p99": p99,
         "clip_hi_pct": 100.0 * float(np.mean(small >= 250)),
         "clip_lo_pct": 100.0 * float(np.mean(small <= 5)),
         "top_bin_pct": 100.0 * float(hist.max()) / max(1, small.size),
         "top_bin_value": int(hist.argmax()),
         "michelson": (p99 - p1) / (p99 + p1) if (p99 + p1) > 0 else 0.0,
         "sharpness": float(lap.var())}
    if eye_bgr.ndim == 3:
        b, gch, r = (float(eye_bgr[..., i].mean()) for i in range(3))
        m["rgb_mean"] = [r, gch, b]
    return m


def frame_motion(a, b):
    """Mean absolute pixel change between two frames of one eye. Sensor noise keeps
    this above ~0.3 even for a static scene; a frozen or dead stream repeats the
    same frame and reads ~0."""
    import cv2
    ga, gb = _gray(a), _gray(b)
    ga = cv2.resize(ga, (ga.shape[1] // 4, ga.shape[0] // 4), interpolation=cv2.INTER_AREA)
    gb = cv2.resize(gb, (gb.shape[1] // 4, gb.shape[0] // 4), interpolation=cv2.INTER_AREA)
    return float(np.mean(np.abs(ga.astype(np.int16) - gb.astype(np.int16))))


JUDGE_DEFAULTS = {"eye_black_p99": 20, "eye_flat_std": 4, "eye_frozen_motion": 0.05,
                  "eye_black_level_diff": 40, "eye_contrast_ratio": 0.6}


def judge_eyes(metrics, motion, limits=None):
    """The image verdict from two eyes' metrics and motion -> (problems, notes).

    A problem fails QC; a note is only a warning. Every eye has to show a live picture
    for QC to vouch for it, so a black, flat or frozen eye fails, and so do BOTH: QC
    can't tell lens caps or a dark room from two dead sensors, so the operator points
    the StarCap at the lit room and runs again, rather than a blind unit passing.
    (A flat 232 is the camera's limited-range white, not a dead sensor, but it is
    still no picture to judge.) The message says which it most likely is."""
    lim = dict(JUDGE_DEFAULTS, **(limits or {}))
    names = ("left eye", "right eye")
    bad = []
    for m, mo in zip(metrics, motion):
        if m["p99"] < lim["eye_black_p99"]:
            bad.append(("black", f"is black (brightest 1% at {m['p99']:.0f}/255)"))
        elif m["std"] < lim["eye_flat_std"]:
            bad.append(("flat", f"shows no picture (a flat {m['mean']:.0f})"))
        elif mo < lim["eye_frozen_motion"]:
            bad.append(("frozen", "is frozen (identical frames)"))
        else:
            bad.append(None)
    problems, notes = [], []
    if bad[0] and bad[1]:
        kinds = {bad[0][0], bad[1][0]}
        if kinds == {"frozen"}:
            problems.append("both eyes are frozen: the camera stream is stuck")
        else:
            problems.append("neither eye shows a picture (lens caps on? lights off? facing a blank wall?): "
                            "point the StarCap at the lit room and run QC again")
    else:
        for n, b in zip(names, bad):
            if b:
                problems.append(f"the {n} {b[1]}" + ("" if b[0] == "frozen" else ": check nothing covers the lens"))
    if not problems:
        a, b = metrics
        if abs(a["p1"] - b["p1"]) > lim["eye_black_level_diff"]:
            notes.append("the eyes differ a lot in black level (glare or a dirty lens?)")
        elif min(a["michelson"], b["michelson"]) < lim["eye_contrast_ratio"] * max(a["michelson"], b["michelson"]):
            notes.append("one eye has much less contrast than the other (glare or a dirty lens?)")
    return problems, notes
