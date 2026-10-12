"""
calib.py: which cameras need the calibration bench, and in what order.

A stereo calibration is good for RECAL_DAYS (the dashboard's RECALIBRATION_DAYS),
so every camera comes due a month after its last fit, and one person gets through
about CAL_PER_DAY a day. Three things make that harder than a sorted list:

  * A camera is graded by its FITTED lens. A re-lensed camera 88 is "88.1": the old
    fit on "88" describes glass that is no longer on it, and often "88" has no
    document at all. The fitted lens is the highest revision anyone has recorded
    (the revs set, or any current calibration row of that camera), and only fits
    on THAT key count. Retired and broken stay on the base id: they describe the
    body, not the glass.
  * A fit counts when the bench graded it PASS, or graded it nothing at all (the
    older benches never wrote a verdict).
  * Fits come in bursts: 51 new cameras fitted on one day all come due on one day a
    month later. Earliest-deadline-first with PULL_FORWARD_DAYS of slack spreads
    such a spike over the days before it, instead of finding out that week.

Pure functions: rows in, dicts out, `now` passed in, times left as epoch seconds.
"""
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict

import clock
import config

BENCHES = {"basalt_kb4": "Basalt", "opencv_fisheye_kb4": "Robot bench", None: "Charuco (old)"}
NO_USAGE = {"out_now": False, "on_cap": None, "hours_7d": 0.0, "last_used": None}


# --------------------------------------------------------------------------- #
# camera ids
# --------------------------------------------------------------------------- #
def natural_key(s):
    """Sort key that puts "9" before "43" before "43.1" before "199", and starcap5
    before starcap12. re.split with a group alternates text and digits, so the
    tuples always compare str with str and int with int."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(s)))


def split_key(key):
    """'88.1' -> ('88', 1), '88' -> ('88', 0): the revision is the number after the last dot."""
    s = str(key).strip()
    base, dot, rev = s.rpartition(".")
    if dot and base and rev.isdigit():
        return base, int(rev)
    return s, 0


def camera_key(base, rev):
    return f"{base}.{rev}" if rev else str(base)


def norm_key(key):
    return camera_key(*split_key(key))


def base_of(key):
    return split_key(key)[0]


def valid_verdict(verdict):
    """camident-core.ts isUnqualifiedPass, plus NULL: the whole string, trimmed and
    upper-cased, is PASS, or the bench wrote no verdict."""
    return verdict is None or str(verdict).strip().upper() == "PASS"


def bench(method):
    return BENCHES.get(method, method)


class Lenses:
    """The fitted lens of every camera: its highest revision in the revs set
    ("61.1") or on any current calibration row of that base."""

    def __init__(self, revs, cal_current=()):
        self._rev = {}
        for key in revs:
            self._bump(*split_key(key))
        for row in cal_current:
            self._bump(str(row["camera_id"]), int(row.get("rev") or 0))

    def _bump(self, base, rev):
        self._rev[base] = max(rev, self._rev.get(base, 0))

    def rev(self, base):
        return self._rev.get(str(base), 0)

    def key(self, camera):
        """The key a camera's fits live under: an explicit "43.1" stays as it is, a
        bare "43" gets its fitted revision."""
        base, rev = split_key(camera)
        return camera_key(base, rev or self.rev(base))


def take_key(take, lenses):
    """The lens a take was shot on: the recorder's own camera.unit ("43.1"), else the
    check-out's camera with its fitted revision, else None."""
    if take.get("camera_unit"):
        return norm_key(take["camera_unit"])
    item = take.get("item_camera_id")
    return lenses.key(item) if item else None


class CalIndex:
    """Every fit that counts, per camera key, oldest first, so "the fit in force at
    time t" is one bisect (the day view asks it for every take).

    Two sources, because neither is complete. `versions` (calibration_versions) is the
    history, but it starts later than the fleet: cameras 26, 75 and 93 were fitted in
    July/August and only the dashboard's current row (camera_calibrations, `current`)
    remembers it. And only a current row means a calibration is in force now: 91 and
    97 have a single ungraded 08-11 version and no current row, which the dashboard
    calls stereo_missing. So `at` (what was in force when a take was shot) reads both,
    and `last` (the camera's state today) needs a current row for that lens."""

    def __init__(self, versions, current=()):
        fits = defaultdict(dict)
        for v in versions:
            if v.get("calibrated_at") is not None and valid_verdict(v.get("verdict")):
                fits[norm_key(v["camera_key"])][round(v["calibrated_at"])] = v
        self._current = set()
        for r in current:
            if r.get("kind") != "stereo" or r.get("calibrated_at") is None or not valid_verdict(r.get("verdict")):
                continue
            key = camera_key(str(r["camera_id"]), int(r.get("rev") or 0))
            self._current.add(key)
            fits[key].setdefault(round(r["calibrated_at"]), {   # the version row is richer; keep it
                "camera_key": key, "camera_id": str(r["camera_id"]), "rev": int(r.get("rev") or 0),
                "calibrated_at": r["calibrated_at"], "method": r.get("method"), "verdict": r.get("verdict"),
                "evidence_status": r.get("evidence_status")})
        self._fits = {k: sorted(vs.values(), key=lambda v: v["calibrated_at"]) for k, vs in fits.items()}
        self._times = {k: [v["calibrated_at"] for v in vs] for k, vs in self._fits.items()}

    def at(self, key, t):
        """The newest fit of `key` calibrated at or before t, or None."""
        i = bisect_right(self._times.get(key, ()), t)
        return self._fits[key][i - 1] if i else None

    def last(self, key):
        """The fit in force now: the newest one, if the dashboard has a current row for
        this lens at all."""
        fits = self._fits.get(key)
        return fits[-1] if fits and key in self._current else None


# --------------------------------------------------------------------------- #
# state of one lens
# --------------------------------------------------------------------------- #
def due_at(fit):
    return fit["calibrated_at"] + config.RECAL_DAYS * clock.DAY_S


def days_left(fit, now):
    """Whole days until the fit expires, rounded up: 0 or less is overdue."""
    return math.ceil((due_at(fit) - now) / clock.DAY_S)


def state_of(days):
    if days is None:
        return "never"
    if days <= 0:
        return "overdue"
    return "due_soon" if days <= config.EXPIRY_WARN_DAYS else "ok"


def lens_state(key, cal, now):
    fit = cal.last(key)
    if fit is None:
        return {"last_cal_at": None, "due_at": None, "due_on": None, "days_left": None,
                "state": "never", "bench": None}
    days = days_left(fit, now)
    return {"last_cal_at": fit["calibrated_at"], "due_at": due_at(fit), "due_on": clock.cairo_date(due_at(fit)),
            "days_left": days, "state": state_of(days), "bench": bench(fit.get("method"))}


def cameras(rows, retired, broken, lenses, cal, now):
    """Every non-dragon camera with its fitted lens and calibration state. Retired and
    broken are flags on the BASE id; the state belongs to the fitted key."""
    retired, broken = set(map(str, retired)), set(map(str, broken))
    out = []
    for r in rows:
        base = str(r["id"])
        if base.startswith("dragon:"):
            continue
        key = lenses.key(base)
        out.append(dict(lens_state(key, cal, now), camera=key, base=base, rev=split_key(key)[1],
                        created_at=r.get("created_at"), retired=bool(r.get("retired")) or base in retired,
                        broken=base in broken))
    return out


def active(cams):
    return [c for c in cams if not c["retired"]]


def plannable(cams):
    """What the bench can work on: active, not broken, with a fit in force (a camera with
    none is listed as "never" instead)."""
    return [c for c in active(cams) if not c["broken"] and c["state"] != "never"]


def overdue_at(cams, cal, t):
    """How many active, unbroken cameras were past due at time t (for trends): the
    fitted key's fit in force at t was RECAL_DAYS old or more."""
    fits = (cal.at(c["camera"], t) for c in active(cams) if not c["broken"])
    return sum(1 for f in fits if f is not None and due_at(f) <= t)


# --------------------------------------------------------------------------- #
# usage
# --------------------------------------------------------------------------- #
def usage(takes, open_items, kamerat_out, starcaps, lenses, now):
    """What each camera (base id) is doing -> {base: {out_now, on_cap, hours_7d,
    last_used}}. `takes` are field takes (they carry checkout_at). A camera is out
    when an open check-out or the kamerat "who has it" list names it; the cap it is
    on is, most trusted first, that open check-out, the kamerat entry, then the
    starcap registry."""
    names = {r["cpu_id"]: r["name"] for r in starcaps}
    out = defaultdict(lambda: dict(NO_USAGE))
    for t in takes:
        key = take_key(t, lenses)
        if key is None:
            continue
        u = out[base_of(key)]
        day = clock.cairo_date(t["checkout_at"])
        u["last_used"] = max(u["last_used"] or day, day)
        if t["checkout_at"] >= now - 7 * clock.DAY_S:
            u["hours_7d"] += (t.get("recorded_s") or 0.0) / 3600
    for r in starcaps:
        if r.get("camera_id"):
            out[base_of(r["camera_id"])]["on_cap"] = r["name"]
    for cap, v in kamerat_out.items():
        if v.get("camera") is not None:
            out[base_of(v["camera"])].update(out_now=True, on_cap=cap)
    for i in open_items:
        if i.get("camera_id"):
            out[base_of(i["camera_id"])].update(out_now=True, on_cap=names.get(i["cpu_id"]) or i["cpu_id"])
    return dict(out)


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #
def in_rotation(c):
    """Recorded with this week, or out right now: its next trip makes footage, so an
    expired fit on it costs hours that an idle camera on a shelf doesn't."""
    return c["out_now"] or c["hours_7d"] > 0


def _priority(c, day):
    """Bench order on `day`: what is already past due first, the cameras in rotation
    before the idle ones and the busiest of those first; then what is pulled forward,
    earliest due first. (2026-10-12: strict earliest-deadline-first put two idle
    cameras, 31 days overdue and 0 h this week, ahead of 11.1 with 27 h.)"""
    if c["due_on"] <= day:
        return (0, not in_rotation(c), -c["hours_7d"], c["due_on"], natural_key(c["camera"]))
    return (1, c["due_on"], not in_rotation(c), -c["hours_7d"], natural_key(c["camera"]))


def plan(cams, today, done_today):
    """Day by day over PLAN_DAYS from today -> the plannable cameras in plan order, each
    with plan_day (None past the horizon) and late.

    Each day takes up to CAL_PER_DAY (today: minus the fits already done today) from
    the cameras due by that day + PULL_FORWARD_DAYS, in `_priority` order. Today it
    skips cameras out in the field: they can't be on the bench until their cap is back
    (`catch_when_back`); later days assume they are. A camera planned after its due
    day is late, and so is one the horizon never reaches although it falls due inside
    it."""
    todo = plannable(cams)
    days = [clock.shift_day(today, i) for i in range(config.PLAN_DAYS)]
    plan_day = {}
    for i, day in enumerate(days):
        room = max(0, config.CAL_PER_DAY - (done_today if i == 0 else 0))
        reach = clock.shift_day(day, config.PULL_FORWARD_DAYS)
        ready = [c for c in todo if c["camera"] not in plan_day and c["due_on"] <= reach
                 and not (i == 0 and c["out_now"])]
        picked = [c["camera"] for c in sorted(ready, key=lambda c: _priority(c, day))][:room]
        plan_day.update(dict.fromkeys(picked, day))
    beyond = clock.shift_day(days[-1], 1)
    out = [dict(c, plan_day=plan_day.get(c["camera"]),
                late=plan_day.get(c["camera"], beyond) > c["due_on"]) for c in todo]
    return sorted(out, key=lambda c: (c["plan_day"] or beyond, _priority(c, c["plan_day"] or beyond)))


def catch_when_back(planned):
    """Overdue or soon-due cameras out in the field right now, most urgent first: put
    them on the bench the day their cap comes back."""
    return sorted((c for c in planned if c["out_now"] and c["state"] in ("overdue", "due_soon")),
                  key=lambda c: (c["days_left"], natural_key(c["camera"])))


def forecast(planned, today):
    """Per plan day: cameras falling due that day, cameras planned, and how many will be
    past due at the end of it."""
    due = Counter(c["due_on"] for c in planned)
    booked = Counter(c["plan_day"] for c in planned)
    out = []
    for day in (clock.shift_day(today, i) for i in range(config.PLAN_DAYS)):
        overdue = sum(1 for c in planned if c["due_on"] <= day and not (c["plan_day"] and c["plan_day"] <= day))
        out.append({"date": day, "due": due[day], "planned": booked[day], "overdue_end": overdue})
    return out


def spikes(days):
    """Forecast days on which more cameras fall due than config.SPIKE_DAYS days of bench work.
    A day a little over CAL_PER_DAY is ordinary (the real forecast has several days of
    11-17) and the pull-forward window soaks it up unaided; 29 or 51 at once is worth
    knowing about weeks ahead."""
    return [{"date": d["date"], "due": d["due"]} for d in days if d["due"] > config.SPIKE_DAYS * config.CAL_PER_DAY]


def backlog(days):
    """From the forecast: `clear_by`, the first day that ends with nothing overdue (None
    if the plan never gets there), and `behind_from`, the first day after that on which
    something is overdue again (None while the plan keeps up). A backlog that is being
    worked down is not "falling behind"; a spike the bench can't absorb is."""
    clear = next((d["date"] for d in days if d["overdue_end"] == 0), None)
    again = next((d["date"] for d in days if clear and d["date"] > clear and d["overdue_end"] > 0), None)
    return clear, again


# --------------------------------------------------------------------------- #
# fits
# --------------------------------------------------------------------------- #
def fit_history(versions):
    """Every stereo fit, newest first, with first_ever (no earlier version of the same
    key, whatever its verdict) and previous_at."""
    rows = sorted((v for v in versions if v.get("calibrated_at") is not None),
                  key=lambda v: v["calibrated_at"])
    last, out = {}, []
    for v in rows:
        key = norm_key(v["camera_key"])
        out.append({"camera": key, "base": str(v["camera_id"]), "rev": int(v.get("rev") or 0),
                    "method": v.get("method"), "bench": bench(v.get("method")),
                    "calibrated_at": v["calibrated_at"], "first_ever": key not in last,
                    "previous_at": last.get(key), "rmse_left": _round(v.get("rmse_left"), 3),
                    "rmse_right": _round(v.get("rmse_right"), 3), "rmse_stereo": _round(v.get("rmse_stereo"), 3),
                    "baseline_mm": _round(v.get("baseline_mm"), 2), "verdict": v.get("verdict"),
                    "evidence": v.get("evidence_status")})
        last[key] = v["calibrated_at"]
    return out[::-1]


def _round(x, digits):
    return None if x is None else round(x, digits)


def imu_rows(cal_current):
    """Camera-IMU fits that count: PASS or ungraded, like stereo fits. (The only one in
    production on 2026-10-12, camera 106, failed, so coverage is 0, not 1.)"""
    return [r for r in cal_current if r.get("kind") == "imu" and r.get("calibrated_at") is not None
            and valid_verdict(r.get("verdict"))]


def fits_on(day, fits):
    return [f for f in fits if clock.cairo_date(f["calibrated_at"]) == day]


def day_fits(day, fits, imus):
    """What the bench did on one Cairo day."""
    todays = fits_on(day, fits)
    imu = [{"camera": camera_key(r["camera_id"], int(r.get("rev") or 0)), "calibrated_at": r["calibrated_at"]}
           for r in sorted(imus, key=lambda r: -r["calibrated_at"]) if clock.cairo_date(r["calibrated_at"]) == day]
    new = sum(f["first_ever"] for f in todays)
    return {"done": len(todays), "recal": len(todays) - new, "new": new, "imu": len(imu),
            "fits": todays, "imu_fits": imu}


def history(days, fits, imus):
    tally = defaultdict(Counter)
    for f in fits:
        tally[clock.cairo_date(f["calibrated_at"])]["new" if f["first_ever"] else "recal"] += 1
    for r in imus:
        tally[clock.cairo_date(r["calibrated_at"])]["imu"] += 1
    return [{"date": d, "recal": tally[d]["recal"], "new": tally[d]["new"], "imu": tally[d]["imu"]} for d in days]


def newest_imu(imus):
    """base -> its newest camera-IMU row."""
    out = {}
    for r in sorted(imus, key=lambda r: r["calibrated_at"]):
        out[str(r["camera_id"])] = r
    return out


def imu_outdated(imu, cam):
    """A camera-IMU fit older than the stereo fit it was solved against is stale."""
    return cam["last_cal_at"] is not None and imu["calibrated_at"] < cam["last_cal_at"]


def coverage(cams, imus):
    act = active(cams)
    valid = sum(1 for c in act if not c["broken"] and c["state"] in ("ok", "due_soon"))
    latest = newest_imu(imus)
    with_imu = [c for c in act if c["base"] in latest]
    outdated = sum(imu_outdated(latest[c["base"]], c) for c in with_imu)

    def pct(n):
        return round(100.0 * n / len(act), 1) if act else None
    return {"valid": valid, "active": len(act), "valid_pct": pct(valid), "imu": len(with_imu),
            "imu_pct": pct(len(with_imu)), "imu_outdated": outdated}


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
def queue_entry(c):
    return {"camera": c["camera"], "base": c["base"], "state": c["state"], "days_left": c["days_left"],
            "due_on": c["due_on"], "last_cal_at": c["last_cal_at"], "bench": c["bench"],
            "out_now": c["out_now"], "on_cap": c["on_cap"], "hours_7d": round(c["hours_7d"], 2),
            "last_used": c["last_used"], "plan_day": c["plan_day"], "late": c["late"]}


def never_entry(c):
    return {"camera": c["camera"], "created_at": c["created_at"], "hours_7d": round(c["hours_7d"], 2),
            "last_used": c["last_used"], "out_now": c["out_now"]}


def with_usage(cams, use):
    return [dict(c, **use.get(c["base"], NO_USAGE)) for c in cams]


def view(day, today, cams, use, fits, imus):
    """GET /api/calibration. `day` picks the bench day shown under "today" and ends the
    history; the counts, queue and plan are always as of now (today's plan)."""
    cams = with_usage(cams, use)
    act = active(cams)
    planned = plan(cams, today, len(fits_on(today, fits)))
    days = forecast(planned, today)
    clear_by, behind = backlog(days)
    states = Counter(c["state"] for c in act if not c["broken"])
    never = sorted((c for c in act if not c["broken"] and c["state"] == "never"),
                   key=lambda c: (-c["hours_7d"], natural_key(c["camera"])))
    return {
        "date": day, "target": config.CAL_PER_DAY,
        "today": day_fits(day, fits, imus),
        "counts": {"active": len(act), "ok": states["ok"], "due_soon": states["due_soon"],
                   "overdue": states["overdue"], "never": states["never"],
                   "late_in_plan": sum(c["late"] for c in planned),
                   "retired": len(cams) - len(act), "broken": sum(c["broken"] for c in act)},
        "plan_today": [queue_entry(c) for c in planned if c["plan_day"] == today],
        "catch_when_back": [queue_entry(c) for c in catch_when_back(planned)],
        "queue": [queue_entry(c) for c in planned if c["state"] in ("overdue", "due_soon")],
        "never": [never_entry(c) for c in never],
        "broken": sorted((c["base"] for c in act if c["broken"]), key=natural_key),
        "forecast": days,
        "spikes": spikes(days),
        "clear_by": clear_by,
        "behind_from": behind,
        "history": history(clock.days_between(clock.shift_day(day, 1 - config.TREND_DAYS), day), fits, imus),
        "coverage": coverage(cams, imus),
    }
