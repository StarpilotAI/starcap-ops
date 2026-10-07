"""
calibration.py: does THIS camera have a usable stereo calibration?

The calibration is not on the board. Station A3's bench uploads it to the dashboard,
and the station API hands back one camera's document:

    GET /api/ingest/camident/calibration?camera=<camera_id>     (X-Starcap-Password)

<camera_id> is the board's `camera.camera_id` VERBATIM ("63.1", not 63). A re-lensed
camera keeps its old lens's fit on the bare id, so looking up the bare number reports a
stale July calibration, with the wrong FOV, as if it were current.

What QC requires (white balance is NOT required, this line does not WB-calibrate):
  * a document exists for this lens id
  * it is the current method (KB4 fisheye, 4 coefficients), not a legacy fit
  * it is FIELD READY by the dashboard's own rule (camident-core.ts
    evidenceFieldReady): the evidence bundle is complete, it declares every required
    media category, and the bench's acceptance verdict is an unqualified PASS
  * FOV (computed here from K and the distortion, per eye) >= 110 x 80 deg
  * a sane stereo baseline (LIMITS), with the right eye on the expected side

The FOV is recomputed rather than read from `fov_deg`: the bench drops that field when
any span is unreliable, and recomputing is also how a fit that folds at the image
edge (non-monotonic distortion) gets caught instead of reported as a huge FOV.
"""
import math
import time
import urllib.parse

import numpy as np

import board

# camident-core.ts REQUIRED_EVIDENCE_MEDIA: what an evidence manifest must declare
# before it can be trusted as complete (camera 189 went "complete" without them)
REQUIRED_EVIDENCE_MEDIA = ("recordings/A/", "recordings/B/", "fit_images/")


def unqualified_pass(verdict):
    """camident-core.ts isUnqualifiedPass: the WHOLE string, trimmed, upper-cased, is
    PASS. "PASS (DEVELOPMENT ONLY - NOT ACCEPTANCE EVIDENCE)" is not a pass."""
    return isinstance(verdict, str) and verdict.strip().upper() == "PASS"


def missing_required_media(files):
    paths = [str((f or {}).get("path") or "") for f in files or []]
    return [p for p in REQUIRED_EVIDENCE_MEDIA if not any(x.startswith(p) for x in paths)]


def _theta(theta_d, k):
    """Invert theta_d = theta (1 + k1 t^2 + k2 t^4 + k3 t^6 + k4 t^8) by Newton."""
    k1, k2, k3, k4 = k
    t = theta_d
    for _ in range(50):
        t2 = t * t
        f = t * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4)))) - theta_d
        df = 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + t2 * 9 * k4)))
        if df == 0:
            break
        step = f / df
        t -= step
        if abs(step) < 1e-13:
            break
    return t


def _bearing(u, v, K, k):
    fx, fy, cx, cy = K[0][0], K[1][1], K[0][2], K[1][2]
    x, y = (u - cx) / fx, (v - cy) / fy
    td = math.hypot(x, y)
    if td < 1e-12:
        return np.array([0.0, 0.0, 1.0])
    th = _theta(td, k)
    s = math.sin(th) / td
    return np.array([x * s, y * s, math.cos(th)])


def _angle(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b)))))


def kb4_fov(K, k, W, H):
    """(hfov, vfov, monotonic) for one eye of a KB4 fit, in degrees: the angle between
    the bearings of the two edge pixels on the principal-point row / column. The same
    method as the calibration harness, which reproduces its stored fov_deg to 0.01 deg."""
    cx, cy = K[0][2], K[1][2]
    cxp, cyp = min(max(cx, 0), W - 1), min(max(cy, 0), H - 1)
    h = _angle(_bearing(0, cyp, K, k), _bearing(W - 1, cyp, K, k))
    v = _angle(_bearing(cxp, 0, K, k), _bearing(cxp, H - 1, K, k))
    mono = True
    for px, py in ((0, cyp), (W - 1, cyp), (cxp, 0), (cxp, H - 1)):
        last = -1.0
        for s in np.linspace(0.0, 1.0, 65):
            b = _bearing(cx + s * (px - cx), cy + s * (py - cy), K, k)
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(b[2])))))
            if ang < last - 1e-9:
                mono = False
            last = ang
    return h, v, mono


def fetch(camera_id):
    """The dashboard's calibration document for this lens id -> (doc | None, error | None).
    A missing document is (None, None): that is an answer, not an error."""
    path = "/api/ingest/camident/calibration?camera=" + urllib.parse.quote(str(camera_id))
    try:
        res = board.opmname._dashboard_call("GET", path, timeout=20)
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("dashboard 404"):
            return None, None
        return None, msg
    if not res.get("ok"):
        return None, str(res.get("error") or "the dashboard refused the lookup")
    return res.get("calibration") or None, None


def check(camera_id, limits):
    """-> {"state": pass|warn|fail, "summary", "details": [...], "data": {...}}"""
    if not camera_id:
        return {"state": "fail", "summary": "no camera ID to look up (see the camera check)"}
    doc, err = fetch(camera_id)
    if err:
        return {"state": "fail", "summary": f"couldn't check the calibration: {err}",
                "details": ["QC can't vouch for a camera it can't look up. Fix the network and run again."]}
    if doc is None:
        return {"state": "fail",
                "summary": f"camera {camera_id} has no calibration on the dashboard: calibrate it (station A3)"}
    details, data = [], {"camera": doc.get("camera"), "model": doc.get("model"), "method": doc.get("method"),
                         "baseline_mm": doc.get("baseline_mm"), "rmse": doc.get("rmse"),
                         "calibrated_at": doc.get("calibrated_at")}
    model = doc.get("model") or "pinhole_rational"
    if model != "fisheye_kb4" or doc.get("method") == "bench_charuco":
        return {"state": "fail", "data": data,
                "summary": f"camera {camera_id} only has a legacy calibration ({model}); "
                           "recalibrate it on the current bench (station A3)"}
    # field-ready, in the order that names the right repair: an unfinished upload is an
    # upload problem (the acceptance verdict only lands when the bundle completes, so a
    # partial one has none yet: that must NOT read as "recalibrate"), a manifest missing
    # its media is a submission problem, and only a real non-PASS verdict means the fit
    ev = doc.get("evidence") or {}
    verdict = (ev.get("acceptance") or {}).get("verdict")
    missing = missing_required_media(ev.get("files"))
    data["evidence"] = {"status": ev.get("status"), "verdict": verdict, "missing_media": missing}
    if ev.get("status") != "complete":
        return {"state": "fail", "data": data,
                "summary": (f"camera {camera_id}'s calibration evidence never finished uploading "
                            f"({ev.get('status') or 'no evidence'}). The fit may be fine: re-run "
                            "upload_audit.py --fix on the bench; don't recalibrate")}
    if missing:
        return {"state": "fail", "data": data,
                "summary": (f"camera {camera_id}'s evidence bundle never declared {', '.join(missing)}, "
                            "so the dashboard won't call it field ready: re-submit it from the bench (station A3)")}
    if not unqualified_pass(verdict):
        return {"state": "fail", "data": data,
                "summary": (f"the calibration bench graded camera {camera_id} "
                            + (f"'{verdict}'" if verdict else "with no verdict")
                            + ", not PASS: recalibrate it (station A3)")}
    W, H = doc.get("lens_size") or (1920, 1200)
    fovs = []
    for eye, Kk, dk in (("left", "K0", "dist0"), ("right", "K1", "dist1")):
        K, d = doc.get(Kk), doc.get(dk)
        if not (isinstance(K, list) and len(K) == 3 and isinstance(d, list) and len(d) == 4):
            return {"state": "fail", "data": data,
                    "summary": f"the calibration document is missing {Kk}/{dk}"}
        h, v, mono = kb4_fov(K, d, W, H)
        fovs.append((eye, h, v, mono))
        details.append(f"{eye} lens: fx {K[0][0]:.1f}, FOV {h:.1f}° × {v:.1f}°")
    data["fov"] = {e: {"h": round(h, 2), "v": round(v, 2), "monotonic": m} for e, h, v, m in fovs}
    if not all(m for *_x, m in fovs):
        return {"state": "fail", "data": data, "details": details,
                "summary": "the lens fit folds back at the image edge (non-monotonic): recalibrate (station A3)"}
    hmin = min(h for _e, h, _v, _m in fovs)
    vmin = min(v for _e, _h, v, _m in fovs)
    if hmin < limits["fov_h_min"] or vmin < limits["fov_v_min"]:
        return {"state": "fail", "data": data, "details": details,
                "summary": f"FOV {hmin:.0f}° × {vmin:.0f}° is under the spec "
                           f"({limits['fov_h_min']:.0f}° × {limits['fov_v_min']:.0f}°)"}
    T = doc.get("T") or []
    base = float(doc.get("baseline_mm") or (math.hypot(*T) if len(T) == 3 else 0.0))
    b_lo, b_hi = limits["baseline_mm"]
    if not (b_lo <= base <= b_hi):
        return {"state": "fail", "data": data, "details": details,
                "summary": f"stereo baseline {base:.1f} mm is outside {b_lo:.0f}-{b_hi:.0f} mm"}
    if len(T) == 3 and T[0] > 0:
        return {"state": "fail", "data": data, "details": details,
                "summary": f"the calibration has the eyes the wrong way round (T.x = {T[0]:+.1f} mm)"}
    rm = doc.get("rmse") or {}
    details.append(f"baseline {base:.1f} mm · reprojection error {float(rm.get('stereo') or 0):.2f} px")
    details.append("camera-to-IMU: the IMU is taken as the left camera's frame (not measured per unit)")
    summary = f"camera {camera_id} · FOV {hmin:.0f}° × {vmin:.0f}° · baseline {base:.1f} mm · bench PASS"
    cal_at = doc.get("calibrated_at")
    if isinstance(cal_at, (int, float)) and cal_at > 0:
        age_d = (time.time() - cal_at / 1000.0) / 86400.0
        data["age_days"] = round(age_d, 1)
        details.append(f"calibrated {age_d:.0f} days ago")
        if age_d > limits["recal_days"]:
            return {"state": "warn", "data": data, "details": details,
                    "summary": summary + f" · {age_d:.0f} days old (recalibration due after {limits['recal_days']})"}
    return {"state": "pass", "data": data, "details": details, "summary": summary}
