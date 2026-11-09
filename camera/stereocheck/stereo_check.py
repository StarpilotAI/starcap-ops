#!/usr/bin/python3 -s
"""
stereo-check: is a docked DECXIN still on its calibration?

Captures frames, corrects (rectifies) both eyes with the camera's calibrations, and
measures how far the right eye sits above/below the left one. After a correct
calibration every object is on the same row in both eyes, so the offset is ~0.
Spec: under 1 px.

  stereo-check [label]                 capture 5 frames of the docked camera, measure
                                       with its current and previous calibration
  stereo-check [label] --unit 221      pick the camera when several are docked
  stereo-check --from <run folder>     re-measure frames already on disk (no capture)
  stereo-check compare out in          same objects in two runs of one camera
                                       (e.g. out of / in the enclosure), current calibration
Options: --unit N, --frames N, --cal <calibration id> (use instead of the current one)

Output, all under ~/Desktop/stereo_check/cam<unit>/:
  <label>/f1..f5.jpg        untouched camera frames
  <label>/MARKED.jpg        same objects with no correction / previous / current calibration
  <label>/result.json       the numbers, plus the calibrations used (re-measure offline)
  COMPARE_<a>_vs_<b>.jpg    from `compare`
  history.csv               one line per run

Picture key: red line = the row the object is on in the LEFT eye, yellow ring = where
the same object is in the RIGHT eye, green arrow = how far off (zooms are x4).

Calibrations come from two places and the NEWER one is "current": the dashboard's
calibration_versions table (the tracker's read-only Postgres client,
fleet/tracker/sources.py) and the robot bench's own run folder
(~/wide-fov-stereo-calibration/runs/camera_<unit>/, STEREO_CHECK_BENCH_RUNS to move
it), so a camera calibrated minutes ago is checked against that fit even before its
upload reaches the dashboard. Raw (uncorrected)
offsets are shown only as the "before" view: they change with the scene and depth,
never judge a camera by them.
"""
import argparse, csv, datetime, glob, json, os, re, subprocess, sys, time, urllib.request

import cv2
import numpy as np

HERE = os.path.dirname(os.path.realpath(__file__))
OUT_ROOT = os.path.expanduser("~/Desktop/stereo_check")
SPEC_PX = 1.0
CAMRIG = "http://127.0.0.1:8088"
# DECXIN raw 4000x1200 = [IMU strip 0-160 | RIGHT eye 160-2080 | LEFT eye 2080-4000]
# (starcap-qc checks.raw_eyes). Splitting at 2000 swaps the eyes and flips the sign.
RIGHT_COLS, LEFT_COLS = (160, 2080), (2080, 4000)
MIN_PTS = 30          # matches a frame needs to count
THIRDS = ((0, 640), (640, 1280), (1280, 1920))


# ---------------------------------------------------------------- calibrations
# A calibration lives in two places: the dashboard (calibration_versions, read-only
# through the tracker's client) and, from the moment the robot bench grades it, the
# bench's own run folder - before its upload lands, or when the upload never does.
# Both are read and the NEWER one is "current", so a camera calibrated minutes ago
# is checked against that calibration and not the last one the dashboard knows.
BENCH_RUNS = os.path.expanduser(os.environ.get("STEREO_CHECK_BENCH_RUNS", "~/wide-fov-stereo-calibration/runs"))


def when(c):
    """calibrated_at as an aware datetime, whichever of the two spellings it is in."""
    try:
        d = datetime.datetime.fromisoformat(str(c["calibrated_at"]))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, KeyError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def dashboard_calibrations(unit):
    """Every fisheye_kb4 calibration of a camera on the dashboard, read-only."""
    sys.path.insert(0, os.path.join(HERE, "..", "..", "fleet", "tracker"))
    import sources
    pg = sources.Postgres.from_env()
    base = unit.split(".")[0]
    try:
        rows = pg.query(
            "select id, camera_id, camera_key, rev, method, calibrated_at, superseded_at, verdict, doc"
            " from calibration_versions where camera_id = %(b)s or camera_key = %(u)s"
            " order by calibrated_at", {"b": base, "u": unit})
    finally:
        pg.close()
    # a re-lensed camera (31.1) keeps the bare id; prefer the fitted lens's own key
    if any(r["camera_key"] == unit for r in rows):
        rows = [r for r in rows if r["camera_key"] == unit]
    cals = []
    for r in rows:
        d = r["doc"] or {}
        if d.get("model") != "fisheye_kb4" or not d.get("R"):
            continue
        cals.append({"id": r["id"], "source": "dashboard", "key": r["camera_key"], "rev": r["rev"],
                     "method": r["method"], "calibrated_at": str(r["calibrated_at"]),
                     "superseded_at": str(r["superseded_at"]) if r["superseded_at"] else None,
                     "verdict": r["verdict"],
                     **{k: d.get(k) for k in ("R", "T", "K0", "K1", "dist0", "dist1", "lens_size")}})
    return cals


def bench_calibration(unit):
    """The robot bench's latest calibration of this camera, in the dashboard's shape
    (the same mapping as the bench's tools/upload_calibration.py build_payload:
    K0 = left lens, R/T = cam_left -> cam_right, T in mm). None if there is none."""
    run = os.path.join(BENCH_RUNS, f"camera_{unit}")
    for sub in ("graded", "submission", "fisheye"):
        f = os.path.join(run, sub, "calibration.json")
        if os.path.exists(f):
            break
    else:
        return None
    c = json.load(open(f))
    if c.get("reference_camera") != "cam_left" or sorted(c.get("cameras", {})) != ["cam_left", "cam_right"]:
        return None
    L, R = c["cameras"]["cam_left"], c["cameras"]["cam_right"]
    if L.get("model") != "fisheye_kb4" or R.get("model") != "fisheye_kb4":
        return None
    pair = next((p for p in c.get("pairs", []) if p.get("source") == "cam_left" and p.get("target") == "cam_right"), None)
    if not pair:
        return None
    verdict = None
    summ = os.path.join(os.path.dirname(f), "summary.txt")
    if os.path.exists(summ):
        m = re.search(r"^decision\s*:\s*(\S+)", open(summ).read(), re.M)
        verdict = m.group(1) if m else None
    at = (c.get("provenance") or {}).get("calibrated_at_utc") or \
        datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc).isoformat()
    return {"id": f"bench:camera_{unit}@{at[:19]}", "source": "bench", "key": unit, "rev": None,
            "method": (c.get("provenance") or {}).get("calibrator"), "calibrated_at": at,
            "superseded_at": None, "verdict": verdict, "path": f,
            "K0": L["K"], "dist0": L["coefficients"], "K1": R["K"], "dist1": R["coefficients"],
            "R": pair["R"], "T": [float(x) * 1000.0 for x in pair["t_m"]], "lens_size": list(L["image_size"])}


def same_fit(a, b):
    return abs((when(a) - when(b)).total_seconds()) < 5 and np.allclose(a["K0"], b["K0"], atol=1e-6)


def fetch_calibrations(unit, log=print):
    """Dashboard + bench calibrations of a camera, oldest first. A bench fit that is
    already on the dashboard appears once, as the dashboard's, marked 'dashboard+bench'."""
    try:
        cals = dashboard_calibrations(unit)
    except Exception as e:                            # noqa: BLE001  the bench copy may still do
        log(f"dashboard unreachable ({e})")
        cals = []
    try:
        b = bench_calibration(unit)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"could not read the bench's calibration of camera {unit} ({e})")
        b = None
    if b:
        twin = next((c for c in cals if same_fit(c, b)), None)
        if twin:
            twin["source"] = "dashboard+bench"
        else:
            cals.append(b)
    return sorted(cals, key=when)


def pick(cals, cal_id=None):
    """(current, previous). current = the one asked for, else the NEWEST of the
    dashboard's live calibration and the bench's; previous = the latest one before it."""
    if not cals:
        raise CheckError("no fisheye_kb4 calibration found for this camera, on the dashboard or the bench")
    if cal_id:
        cur = next((c for c in cals if c["id"] == cal_id), None)
        if not cur:
            raise CheckError(f"calibration {cal_id} not found; have: {[c['id'] for c in cals]}")
    else:
        live = [c for c in cals if not c["superseded_at"]] or cals
        cur = max(live, key=when)
    older = [c for c in cals if when(c) < when(cur)]
    return cur, (older[-1] if older else None)


def where(c):
    return {"dashboard": "dashboard", "bench": "bench only, not on the dashboard",
            "dashboard+bench": "dashboard + bench"}.get(c.get("source"), c.get("source") or "?")


def rect_maps(c):
    W, H = c.get("lens_size") or (1920, 1200)
    K0, K1 = np.array(c["K0"], float), np.array(c["K1"], float)
    D0, D1 = np.array(c["dist0"], float).reshape(4, 1), np.array(c["dist1"], float).reshape(4, 1)
    R0, R1, P0, P1, _ = cv2.fisheye.stereoRectify(
        K0, D0, K1, D1, (W, H), np.array(c["R"], float), np.array(c["T"], float).reshape(3, 1),
        flags=cv2.CALIB_ZERO_DISPARITY, newImageSize=(W, H), balance=0.0, fov_scale=1.0)
    return (cv2.fisheye.initUndistortRectifyMap(K0, D0, R0, P0, (W, H), cv2.CV_16SC2),
            cv2.fisheye.initUndistortRectifyMap(K1, D1, R1, P1, (W, H), cv2.CV_16SC2))


# ---------------------------------------------------------------- measuring
_sift = cv2.SIFT_create(8000)
_bf = cv2.BFMatcher()


def eyes(path):
    g = cv2.imread(path)
    if g is None or g.shape[1] != 4000:
        raise CheckError(f"{path}: not a 4000x1200 DECXIN frame ({None if g is None else g.shape})")
    return g[:, LEFT_COLS[0]:LEFT_COLS[1]], g[:, RIGHT_COLS[0]:RIGHT_COLS[1]]


def match(path, maps=None):
    """Matched points between the eyes of one frame (corrected with `maps`, or raw)."""
    L, R = eyes(path)
    if maps:
        (a1, a2), (b1, b2) = maps
        L, R = cv2.remap(L, a1, a2, cv2.INTER_LINEAR), cv2.remap(R, b1, b2, cv2.INTER_LINEAR)
    k1, d1 = _sift.detectAndCompute(cv2.cvtColor(L, cv2.COLOR_BGR2GRAY), None)
    k2, d2 = _sift.detectAndCompute(cv2.cvtColor(R, cv2.COLOR_BGR2GRAY), None)
    if d1 is None or d2 is None or len(k1) < 2 or len(k2) < 2:
        m = []
    else:
        m = [p for p, q in _bf.knnMatch(d1, d2, k=2) if p.distance < 0.6 * q.distance]
    P1 = np.float32([k1[p.queryIdx].pt for p in m]).reshape(-1, 2)
    P2 = np.float32([k2[p.trainIdx].pt for p in m]).reshape(-1, 2)
    D = np.float32([d1[p.queryIdx] for p in m]).reshape(-1, 128)
    dy, dx = P2[:, 1] - P1[:, 1], P2[:, 0] - P1[:, 0]
    ok = (np.abs(dy) < 40) & (dx < 5) & (dx > -500)   # same row band, plausible disparity
    return dict(l=L, r=R, P1=P1[ok], P2=P2[ok], D=D[ok], dy=dy[ok])


def measure(frames, maps=None):
    res = [match(f, maps) for f in frames]
    good = [x for x in res if len(x["dy"]) >= MIN_PTS]
    if not good:
        return None
    meds = [float(np.median(x["dy"])) for x in good]
    allp = np.vstack([x["P1"] for x in good])
    ally = np.concatenate([x["dy"] for x in good])
    thirds = []
    for a, b in THIRDS:
        sel = (allp[:, 0] >= a) & (allp[:, 0] < b)
        thirds.append(round(float(np.median(ally[sel])), 2) if sel.sum() >= 10 else None)
    return dict(offset=round(float(np.median(meds)), 2), per_frame=[round(v, 2) for v in meds],
                frames=len(good), points=int(np.median([len(x["dy"]) for x in good])),
                thirds=thirds, best=max(good, key=lambda x: len(x["dy"])))


# ---------------------------------------------------------------- capture
def docked_cameras():
    """{unit: /dev/videoN} for every DECXIN capture node (sysfs index 0)."""
    out = {}
    for v in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            name = open(v + "/name").read().strip()
            idx = open(v + "/index").read().strip()
        except OSError:
            continue
        m = re.match(r"DECXIN\s+(?:Stereo\s+)?Camera\s*([\w.]+)", name)
        if m and idx == "0":
            out[m.group(1)] = "/dev/" + os.path.basename(v)
    return out


def busy(dev):
    return subprocess.run(["fuser", dev], capture_output=True).returncode == 0


def capture(dev, folder, n, log=print):
    os.makedirs(folder, exist_ok=True)
    if not busy(dev):
        # one open, skip the first second for auto-exposure, then a frame every ~0.5 s
        subprocess.run(["ffmpeg", "-loglevel", "fatal", "-f", "v4l2", "-input_format", "mjpeg",
                        "-video_size", "4000x1200", "-framerate", "60", "-i", dev,
                        "-vf", r"select=gte(n\,60)*not(mod(n\,30))", "-fps_mode", "passthrough",
                        "-frames:v", str(n), "-q:v", "2", "-y", f"{folder}/f%d.jpg"],
                       timeout=60 + 2 * n)
    else:
        log(f"{dev} is held by another program (camrig?), taking its snapshots")
        for i in range(n):
            try:
                r = json.load(urllib.request.urlopen(f"{CAMRIG}/snapshot?dev={dev}&eye=full", timeout=10))
                if r.get("path"):
                    os.replace(r["path"], f"{folder}/f{i + 1}.jpg")
            except Exception:                         # noqa: BLE001  camrig may be wedged
                pass
            time.sleep(0.8)
    return frames_in(folder)


def frames_in(folder):
    return sorted(glob.glob(os.path.join(folder, "f*.jpg")),
                  key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))


# ---------------------------------------------------------------- pictures
ZOOM, CW, CH = 4, 70, 45
ROW_H = 40 + 2 * CH * ZOOM


def zoom(s, j, label):
    """Left-eye and right-eye crop around matched point j, red line on the left-eye row."""
    (xl, yl), (xr, yr) = s["P1"][j], s["P2"][j]

    def crop(img, x, y):
        x, y = int(round(x)), int(round(y))
        c = cv2.copyMakeBorder(img, CH, CH, CW, CW, cv2.BORDER_CONSTANT)[y:y + 2 * CH, x:x + 2 * CW]
        return cv2.resize(c, None, fx=ZOOM, fy=ZOOM, interpolation=cv2.INTER_NEAREST)

    A, B = crop(s["l"], xl, yl), crop(s["r"], xr, yl)
    cy, cx = CH * ZOOM, CW * ZOOM
    fy = int(cy + (yr - yl) * ZOOM)
    for im in (A, B):
        cv2.line(im, (0, cy), (im.shape[1], cy), (0, 0, 255), 2)
    cv2.circle(A, (cx, cy), 14, (0, 255, 255), 3)
    cv2.circle(B, (cx, fy), 14, (0, 255, 255), 3)
    if abs(fy - cy) > 4:
        cv2.arrowedLine(B, (cx + 40, cy), (cx + 40, fy), (0, 255, 0), 3, tipLength=0.25)
    t = np.full((40, A.shape[1] * 2 + 10, 3), 30, np.uint8)
    cv2.putText(t, f"{label}   {yr - yl:+.1f} px", (8, 28), 0, 0.8, (0, 255, 0), 2)
    return np.vstack([t, np.hstack([A, np.full((A.shape[0], 10, 3), 255, np.uint8), B])])


def same_objects(cols):
    """Up to 3 objects (one per third of the frame) found in every column.
    cols = [(label, match-dict with 'med')]; the last column is the reference."""
    ref = cols[-1][1]
    links = []
    for _, s in cols[:-1]:
        mp = {}
        if len(ref["D"]) >= 2 and len(s["D"]) >= 2:
            for a, b in _bf.knnMatch(ref["D"], s["D"], k=2):
                if a.distance < 0.6 * b.distance:
                    mp[a.queryIdx] = a.trainIdx
        links.append(mp)
    common = [q for q in range(len(ref["dy"]))
              if all(q in mp for mp in links) and 150 < ref["P1"][q][1] < 1050]
    picks = []
    for a, b in ((0, 700), (700, 1250), (1250, 1920)):
        cand = [q for q in common if a <= ref["P1"][q][0] < b]
        if not cand:
            continue
        # the most typical object: closest to every column's own median
        q = min(cand, key=lambda q: abs(ref["dy"][q] - ref["med"]) + sum(
            abs(s["dy"][mp[q]] - s["med"]) for (_, s), mp in zip(cols[:-1], links)))
        picks.append([mp[q] for mp in links] + [q])
    return picks


def picture(cols, title, out):
    picks = same_objects(cols)
    if not picks:
        print("  (no object found in every view, no picture: point it at a textured scene)")
        return None
    gap = np.full((ROW_H, 30, 3), 255, np.uint8)
    rows = []
    for idx in picks:
        parts = []
        for (label, s), j in zip(cols, idx):
            parts += [zoom(s, j, label), gap]
        rows.append(np.hstack(parts[:-1]))
    W = rows[0].shape[1]
    head = np.full((110, W, 3), 30, np.uint8)
    cv2.putText(head, title, (12, 42), 0, 0.8, (255, 255, 255), 2)
    cv2.putText(head, "   ".join(f"{label}: {s['med']:+.1f} px" for label, s in cols), (12, 90), 0, 0.85, (0, 255, 0), 2)
    sep = np.full((12, W, 3), 255, np.uint8)
    cv2.imwrite(out, np.vstack([head] + [np.vstack([r, sep]) for r in rows]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    return out


# ---------------------------------------------------------------- checks
class CheckError(Exception):
    """A check that cannot go on; the message is meant for the operator."""


def local(ts):
    """'2026-10-26 14:19:25.1+00:00' (UTC from the database) -> '2026-10-26 17:19' local."""
    try:
        return datetime.datetime.fromisoformat(str(ts)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts)[:16]


def verdict(off):
    return "PASS" if abs(off) < SPEC_PX else "FAIL"


def describe(label, m):
    t = "/".join("  -" if v is None else f"{v:+.1f}" for v in m["thirds"])
    return (f"{label:<28} {m['offset']:+6.2f} px   frames {m['frames']}  ~{m['points']} pts"
            f"  range {min(m['per_frame']):+.1f}..{max(m['per_frame']):+.1f}  left/mid/right {t}")


def pick_camera(unit=None):
    """(unit, device) of the docked camera to check."""
    cams = docked_cameras()
    if not cams:
        raise CheckError("no DECXIN camera docked")
    if unit:
        if unit not in cams:
            raise CheckError(f"camera {unit} is not docked; docked: {', '.join(cams)}")
        return unit, cams[unit]
    if len(cams) > 1:
        raise CheckError(f"several cameras docked ({', '.join(cams)}): pick one")
    return next(iter(cams.items()))


def calibrations_for(unit, folder=None, log=print):
    cals = fetch_calibrations(unit, log)
    if cals:
        return cals
    old = os.path.join(folder, "result.json") if folder else None
    if old and os.path.exists(old):
        log("no calibration reachable; using the ones saved with this run")
        return json.load(open(old))["calibrations"]
    raise CheckError(f"no calibration of camera {unit} on the dashboard or the bench")


def newest_note(cals):
    """One line: the dashboard's newest calibration vs the bench's, and which is used."""
    d = [c for c in cals if c["source"] in ("dashboard", "dashboard+bench") and not c["superseded_at"]]
    b = [c for c in cals if c["source"] in ("bench", "dashboard+bench")]
    dl = f"dashboard {local(max(d, key=when)['calibrated_at'])}" if d else "dashboard: none"
    bl = f"bench {local(max(b, key=when)['calibrated_at'])}" if b else "bench: none"
    if b and b[-1]["source"] == "bench" and (not d or when(b[-1]) > when(max(d, key=when))):
        return f"{dl} vs {bl}: the bench one is newer and not on the dashboard yet, using it"
    if b and b[-1]["source"] == "dashboard+bench":
        return f"{dl} = {bl}: the bench calibration is already on the dashboard"
    return f"{dl} vs {bl}: using the dashboard's"


def run_folder(unit, label):
    return os.path.join(OUT_ROOT, f"cam{unit}", label)


def check(unit=None, label=None, frames=5, source=None, cal_id=None, log=print, before_capture=None):
    """Capture (or re-read) frames, measure them with the previous and current
    calibration, write MARKED.jpg / result.json / history.csv. Returns the result."""
    if source:
        folder = os.path.abspath(os.path.expanduser(source))
        old = os.path.join(folder, "result.json")
        unit = unit or (json.load(open(old))["camera"] if os.path.exists(old) else None)
        if not unit:
            m = re.search(r"cam([\w.]+)", folder)
            unit = m.group(1) if m else None
        if not unit:
            raise CheckError("which camera? pass --unit")
        shots = frames_in(folder)
        if not shots:
            raise CheckError(f"no f*.jpg frames in {folder}")
        label = os.path.basename(folder)
    else:
        unit, dev = pick_camera(unit)
        label = re.sub(r"[^\w.-]+", "_", label or "").strip("_") or time.strftime("%m%d_%H%M%S")
        folder = run_folder(unit, label)
        if frames_in(folder):
            os.rename(folder, folder + time.strftime("_old%H%M%S"))
        if before_capture:
            before_capture(unit)
        log(f"camera {unit} on {dev}: capturing {frames} frames")
        shots = capture(dev, folder, frames, log)
        if not shots:
            raise CheckError(f"camera {unit}: no frames (is a live view holding it? close it and retry)")

    cals = calibrations_for(unit, folder, log)
    log(newest_note(cals))
    cur, prev = pick(cals, cal_id)
    log(f"current = {local(cur['calibrated_at'])} ({where(cur)})"
        + (f", previous = {local(prev['calibrated_at'])} ({where(prev)})" if prev else ""))
    cols, res = [], {}
    raw = measure(shots)
    if raw:
        raw["best"]["med"] = raw["offset"]
        cols.append(("NO correction", raw["best"]))
        res["raw"] = raw
    for tag, c in (("previous", prev), ("current", cur)):
        if not c:
            continue
        m = measure(shots, rect_maps(c))
        if not m:
            raise CheckError(f"camera {unit}: too few matches with calibration {local(c['calibrated_at'])}"
                             " (point it at a textured, well-lit scene)")
        m["best"]["med"] = m["offset"]
        cols.append((f"{tag.upper()} {local(c['calibrated_at'])}{' (bench)' if c['source'] == 'bench' else ''}", m["best"]))
        res[tag] = m

    log("drawing the marked picture")
    pic = picture(cols, f"CAMERA {unit} ({label}) - same object in every view. Red line = its row in the LEFT eye;"
                        " yellow ring = where it is in the RIGHT eye (x4).", os.path.join(folder, "MARKED.jpg"))
    cur_m = res["current"]
    v = verdict(cur_m["offset"])
    notes = []
    worst = max((abs(t) for t in cur_m["thirds"] if t is not None), default=0)
    if v == "PASS" and worst >= SPEC_PX:
        notes.append(f"one third of the frame reads {worst:.1f} px; the calibration fits less well there")
    if cur_m["points"] < 100:
        notes.append(f"only ~{cur_m['points']} matched points per frame; a brighter, busier scene gives a firmer number")

    strip = lambda m: {k: v for k, v in m.items() if k != "best"}
    cal = lambda c: {"calibration": c["id"], "calibrated_at": c["calibrated_at"], "calibrated_local": local(c["calibrated_at"]),
                     "source": c.get("source"), "where": where(c), "cal_verdict": c.get("verdict")}
    result = {"camera": unit, "label": label, "measured": time.strftime("%Y-%m-%d %H:%M:%S"),
              "verdict": v, "spec_px": SPEC_PX, "notes": notes,
              "current": {**cal(cur), **strip(cur_m)},
              "previous": {**cal(prev), **strip(res["previous"])} if prev else None,
              "raw": strip(res["raw"]) if "raw" in res else None,
              "picture": pic, "calibrations": [c for c in (prev, cur) if c]}
    json.dump(result, open(os.path.join(folder, "result.json"), "w"), indent=1)
    cache_put(folder, cur, cur_m)
    if prev:
        cache_put(folder, prev, res["previous"])
    hist = os.path.join(os.path.dirname(folder), "history.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["measured", "label", "calibration", "calibrated_at", "offset_px", "verdict",
                        "previous_cal_offset_px", "raw_px", "frames", "points"])
        w.writerow([result["measured"], label, cur["id"], local(cur["calibrated_at"]),
                    cur_m["offset"], v, res["previous"]["offset"] if prev else "",
                    res["raw"]["offset"] if "raw" in res else "", cur_m["frames"], cur_m["points"]])
    return result


def compare(unit, a, b, cal_id=None, log=print):
    """Same objects in two saved runs of one camera, both with its current calibration."""
    base = os.path.join(OUT_ROOT, f"cam{unit}")
    cals = calibrations_for(unit, log=log)
    log(newest_note(cals))
    cur, _ = pick(cals, cal_id)
    maps = rect_maps(cur)
    cols, ms = [], []
    for lab in (a, b):
        fr = frames_in(os.path.join(base, lab))
        if not fr:
            raise CheckError(f"no frames in run '{lab}' of camera {unit}")
        log(f"measuring '{lab}'")
        m = measure(fr, maps)
        if not m:
            raise CheckError(f"'{lab}': too few matches")
        m["best"]["med"] = m["offset"]
        cols.append((lab.upper(), m["best"]))
        ms.append(m)
    log("drawing the comparison")
    out = picture(cols, f"CAMERA {unit} - same object in both runs, current calibration ({local(cur['calibrated_at'])})."
                        " Red line = the row it should be on.", os.path.join(base, f"COMPARE_{a}_vs_{b}.jpg"))
    strip = lambda m: {k: v for k, v in m.items() if k != "best"}
    return {"camera": unit, "a": a, "b": b, "calibration": cur["id"], "calibrated_local": local(cur["calibrated_at"]),
            "where": where(cur),
            "runs": {a: strip(ms[0]), b: strip(ms[1])},
            "difference": round(ms[1]["offset"] - ms[0]["offset"], 2), "picture": out}


def runs(unit):
    """Saved runs of a camera, newest first."""
    out = []
    for f in glob.glob(os.path.join(OUT_ROOT, f"cam{unit}", "*", "result.json")):
        try:
            r = json.load(open(f))
        except (OSError, ValueError):
            continue
        out.append({"label": r.get("label") or os.path.basename(os.path.dirname(f)),
                    "measured": r.get("measured"), "verdict": r.get("verdict"),
                    "offset": (r.get("current") or {}).get("offset"),
                    "previous_offset": (r.get("previous") or {}).get("offset"),
                    "calibrated_local": (r.get("current") or {}).get("calibrated_local")
                        or local((r.get("current") or {}).get("calibrated_at", "")),
                    "picture": os.path.join(os.path.dirname(f), "MARKED.jpg")})
    return sorted(out, key=lambda r: r["measured"] or "", reverse=True)


# ---------------------------------------------------------------- cases (graph)
# Every run of a camera measured with the SAME calibration (its current one, and
# the previous one), so the cases you named - in / out / tightened / bench - sit on
# one scale. Measurements are cached per run and calibration in measures.json.
AUTO_LABEL = re.compile(r"^\d{4}_\d{6}$")


def _strip(m):
    return {k: v for k, v in m.items() if k != "best"}


def cache_put(folder, cal, m):
    f = os.path.join(folder, "measures.json")
    try:
        c = json.load(open(f))
    except (OSError, ValueError):
        c = {}
    c[cal["id"]] = _strip(m) if m else None
    json.dump(c, open(f, "w"), indent=1)


def cache_get(folder, cal):
    try:
        c = json.load(open(os.path.join(folder, "measures.json")))
    except (OSError, ValueError):
        return False, None
    return (cal["id"] in c), c.get(cal["id"])


def case_folders(unit, include_unnamed=False):
    """Run folders of a camera that hold frames, oldest capture first."""
    out = []
    for d in glob.glob(os.path.join(OUT_ROOT, f"cam{unit}", "*")):
        name = os.path.basename(d)
        fr = frames_in(d)
        if not os.path.isdir(d) or not fr or re.search(r"_old\d{6}$", name):
            continue
        if AUTO_LABEL.match(name) and not include_unnamed:
            continue
        out.append((os.path.getmtime(fr[0]), name, d))
    return [(n, d, t) for t, n, d in sorted(out)]


def cases(unit, include_unnamed=False, compute=False, log=print):
    """{calibration, previous, runs:[{label, captured, current, previous}], missing}."""
    cals = calibrations_for(unit, log=log)
    cur, prev = pick(cals)
    meta = lambda c: c and {"id": c["id"], "calibrated_local": local(c["calibrated_at"]), "where": where(c)}
    rows, missing = [], 0
    maps = {}
    for name, d, t in case_folders(unit, include_unnamed):
        row = {"label": name, "captured": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)),
               "named": not AUTO_LABEL.match(name)}
        for tag, c in (("current", cur), ("previous", prev)):
            if not c:
                row[tag] = None
                continue
            have, m = cache_get(d, c)
            if not have and compute:
                log(f"measuring '{name}' with the {tag} calibration ({local(c['calibrated_at'])})")
                if c["id"] not in maps:
                    maps[c["id"]] = rect_maps(c)
                mm = measure(frames_in(d), maps[c["id"]])
                cache_put(d, c, mm)
                have, m = True, (_strip(mm) if mm else None)
            if not have:
                missing += 1
            row[tag] = m
        rows.append(row)
    return {"camera": unit, "calibration": meta(cur), "previous": meta(prev), "runs": rows,
            "missing": missing, "spec_px": SPEC_PX}


# ---------------------------------------------------------------- command line
def print_check(r):
    print()
    if r["raw"]:
        print(describe("no correction (raw)", r["raw"]) + "   <- before view only, changes with the scene")
    if r["previous"]:
        print(describe(f"previous cal {r['previous']['calibrated_local']}", r["previous"]))
    c = r["current"]
    print(describe(f"current cal {c['calibrated_local']}", c))
    print(f"\ncamera {r['camera']}: {r['verdict']}  {c['offset']:+.2f} px with its current calibration"
          f" ({c['calibration']}, {c['calibrated_local']}); spec under {SPEC_PX:g} px")
    for n in r["notes"]:
        print("  note: " + n)
    if r["picture"]:
        print(f"picture: {r['picture']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", nargs="?", help="run name (folder), default the time; or `compare`")
    ap.add_argument("other", nargs="*", help="for compare: the two run names")
    ap.add_argument("--unit", help="camera number, when several are docked")
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--from", dest="source", help="re-measure frames already in this folder")
    ap.add_argument("--cal", help="calibration id to treat as current")
    args = ap.parse_args()
    log = lambda s: print("  " + s)
    try:
        if args.label == "compare":
            if len(args.other) != 2:
                raise SystemExit("usage: stereo-check compare <run A> <run B> [--unit N]")
            a, b = args.other
            unit = args.unit
            if not unit:
                cam_dirs = [os.path.dirname(os.path.dirname(p))
                            for p in glob.glob(os.path.join(OUT_ROOT, "cam*", a, "f1.jpg"))]
                found = sorted(os.path.basename(d) for d in cam_dirs if os.path.exists(os.path.join(d, b, "f1.jpg")))
                if len(found) != 1:
                    raise SystemExit(f"pass --unit (cameras with both '{a}' and '{b}': {found or 'none'})")
                unit = found[0][3:]
            r = compare(unit, a, b, args.cal, log)
            for lab in (a, b):
                print(describe(lab, r["runs"][lab]))
            print(f"\ncamera {unit}: {b} minus {a} = {r['difference']:+.2f} px"
                  f"  (calibration {r['calibration']}, {r['calibrated_local']})")
            if r["picture"]:
                print(f"picture: {r['picture']}")
            return
        if args.other:
            raise SystemExit("one label only (did you mean `compare`?)")
        print_check(check(args.unit, args.label, args.frames, args.source, args.cal, log))
    except CheckError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
