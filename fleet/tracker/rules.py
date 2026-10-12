"""
rules.py: what a take is worth and what a cap did on a field day, as pure functions.

OpenAI takes a clip when it is at least MIN_CLIP_S long, shot at 60 fps, and passes
the frame-timing rule: score >= TIMING_PASS_SCORE, i.e. at most 1 % of frame
intervals more than 10 ms off nominal (config.py cites the document). `bucket()`
puts every take in exactly one place, so a day's hours always add up.

A take counts on the Cairo day its trip item was CHECKED OUT, never on the recorder
clock: that clock is stale until NTP lands (one boot was 14 h off), so a take stamped
the evening before still belongs to the trip it came back from. The store does that
join (field_takes); takes with no trip item are bench, QC and office takes and are
reported apart as off-trip.

No store, no clock: `now` is passed in, times stay epoch seconds for api.py to
format, and a row's hours stay unrounded until rounded(), so a day's totals are
exact sums rather than sums of rounded numbers.
"""
import re
from collections import Counter, defaultdict

import calib
import config

BUCKETS = ("good", "fps30", "timing_fail", "untimed", "broken", "short", "pending")
HOURS = ("total",) + BUCKETS
NOTHING = ("no_footage", "footage_missing")      # back past the grace period, nothing arrived
SEVERITIES = ("bad", "warn", "info")

# what each device event type feeds; rec_start/rec_stop are loaded for the cap log
EVENT_COUNTS = {
    "power_on": ("power_on",), "unclean_boot": ("unclean_boot",), "camera_lost": ("camera_lost",),
    "rec_dead": ("rec_dead",), "rec_corrupt": ("rec_corrupt",), "camera_usb2": ("camera_usb2",),
    "storage": ("disk_full", "rec_refused", "storage_stalled", "storage_lost"),
}
_COUNTED = {t: name for name, types in EVENT_COUNTS.items() for t in types}
EVENT_TYPES = tuple(_COUNTED) + ("rec_start", "rec_stop")

# code -> (severity, label), in the order a row lists them (worst first)
FLAGS = {
    "no_footage": ("bad", "No footage"),
    "footage_missing": ("bad", "Footage missing"),
    "no_clips": ("bad", "No usable takes"),
    "fps30": ("bad", "30 fps"),
    "storage": ("bad", "Storage"),
    "timing": ("warn", "Timing"),
    "broken": ("warn", "Broken takes"),
    "short": ("warn", "Short takes"),
    "reboots": ("warn", "Reboots"),
    "camera_drops": ("warn", "Camera drops"),
    "expired_cal": ("warn", "Cal expired"),
    "old_firmware": ("info", "Old firmware"),
}


# --------------------------------------------------------------------------- #
# one take
# --------------------------------------------------------------------------- #
def bucket(take):
    """The one reason a take does or doesn't count, checked in this order. A take with
    no recorded_s yet has no length to judge, so it falls through to pending."""
    s = take.get("recorded_s")
    if s is not None and s < config.MIN_CLIP_S:
        return "short"
    if take.get("fault_kind") == "unrecoverable" or "no_chunks" in (take.get("codes") or ()):
        return "broken"
    if take.get("score") is None:
        return "pending" if take.get("readiness_status") is None else "untimed"
    if take.get("nominal_fps") == 30:
        return "fps30"
    if take["score"] < config.TIMING_PASS_SCORE:
        return "timing_fail"
    return "good"


def take_start(take, now):
    """When a field take started, for judging its calibration: the recorder's start when
    it falls inside the trip, else the check-out (a stale clock can put a take a day
    before the cap even left the office)."""
    start, out_at = take.get("started_at"), take["checkout_at"]
    end = now if take.get("returned_at") is None else take["returned_at"]
    return start if start is not None and out_at <= start <= end else out_at


def expired(take, key, cal, now):
    """Was the take shot on a lens with no fit in force at its start, or one already
    RECAL_DAYS old?"""
    t = take_start(take, now)
    fit = cal.at(key, t) if key else None
    return fit is None or calib.due_at(fit) <= t


def version_key(v):
    """'1.67.0' -> (1, 67, 0), compared as numbers so 1.10 beats 1.9; None if it isn't
    a version. A suffix ('1.67.0-dirty') is ignored."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)\S*", str(v or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def latest_version(takes):
    """The newest firmware run by at least LATEST_VERSION_MIN_CAPS distinct caps in
    these takes (the caller passes 7 days' worth): a canary on two boards isn't the
    version everyone should be on yet."""
    caps = defaultdict(set)
    for t in takes:
        if version_key(t.get("software_version")):
            caps[t["software_version"]].add(t["cpu_id"])
    ok = [v for v, c in caps.items() if len(c) >= config.LATEST_VERSION_MIN_CAPS]
    return max(ok, key=version_key) if ok else None


def take_row(take, lenses):
    """One take as /api/day/cap lists it."""
    return {"id": take["id"], "started_at": take.get("started_at"), "clock_fixed": take.get("clock_fixed"),
            "recorded_s": take.get("recorded_s"), "bucket": bucket(take), "nominal_fps": take.get("nominal_fps"),
            "fps": take.get("fps"), "score": take.get("score"), "p99_dev_ms": take.get("p99_dev_ms"),
            "out_of_band_pct": take.get("out_of_band_pct"), "drop_pct": take.get("drop_pct"),
            "camera": calib.take_key(take, lenses), "boot": (take.get("boot_id") or "")[:4] or None,
            "version": take.get("software_version"), "codes": take.get("codes") or [],
            "fault": take.get("fault_kind"), "uploaded_at": take.get("uploaded_at")}


# --------------------------------------------------------------------------- #
# one cap on one field day
# --------------------------------------------------------------------------- #
class Context:
    """The lookups every cap-day needs, built once per request: starcaps by cpu_id,
    which cpu_id each device belongs to, the fitted lenses and fit index, open tickets
    (an object whose .on(cap, cameras) gives ticket ids, see maint.OpenTickets), my
    statuses ({("cap", name): the note that set it}, see maint.current_statuses), and
    `events_since`, where the local device logs start (None: none synced yet)."""

    def __init__(self, starcaps, devices, lenses, cal, tickets, statuses, events_since=None):
        self.caps = {r["cpu_id"]: r for r in starcaps}
        self.cpu_of = {d["device_id"]: d["cpu_id"] for d in devices}
        self.lenses, self.cal, self.tickets, self.statuses = lenses, cal, tickets, statuses
        self.events_since = events_since

    def logged(self, t):
        """Whether the device logs cover time t, so an empty count means none happened."""
        return self.events_since is not None and t >= self.events_since


def events_by_cpu(events, ctx):
    """Device events -> {cpu_id: events oldest first}."""
    out = defaultdict(list)
    for e in sorted(events, key=lambda e: e["ts"]):
        if e["device_id"] in ctx.cpu_of:
            out[ctx.cpu_of[e["device_id"]]].append(e)
    return dict(out)


def count_events(events):
    counts = dict.fromkeys(EVENT_COUNTS, 0)
    for e in events:
        if e["type"] in _COUNTED:
            counts[_COUNTED[e["type"]]] += 1
    return counts


def on_usb2(take):
    """The camera enumerated on a USB 2 bus: "1-x" on the NanoPi R76S, "7-x" on the
    Orange Pi 5 (every take on those buses in a week ran at 30 fps). Firmware that
    doesn't report the path (1.37.2) proves nothing either way."""
    return str(take.get("usb_path") or "").startswith(("1-", "7-"))


def cap_status(still_out, has_takes, back_at, rec_starts, now):
    """rec_starts None = no device log for the trip: an empty return can't be told
    apart from footage that never got copied, so it reads no_footage."""
    if still_out:
        return "out"
    if has_takes:
        return "ok"
    if now - back_at < config.COPY_GRACE_H * 3600:
        return "waiting"
    return "footage_missing" if rec_starts else "no_footage"


def window(row, now):
    """[first check-out, last return] of a cap-day, or up to now while it is out."""
    return row["checkout_at"], now if row["returned_at"] is None else row["returned_at"]


def cap_days(items, takes, ctx, now, events=None, latest=None):
    """One row per cap checked out in `items` (several check-outs of one cap merge),
    with its field takes. `events` maps cpu_id -> device events; None means they were
    not loaded, so every count is 0 and a cap that came back empty reads no_footage.
    `latest` is the firmware the old_firmware flag compares against (None: no flag)."""
    cpu_of_item = {i["id"]: i["cpu_id"] for i in items}
    by_cap, cap_takes = defaultdict(list), defaultdict(list)
    for i in items:
        by_cap[i["cpu_id"]].append(i)
    for t in takes:
        cap_takes[cpu_of_item.get(t["trip_item_id"], t["cpu_id"])].append(t)
    return [_cap_day(cpu, its, cap_takes[cpu], ctx, now, None if events is None else events.get(cpu, ()), latest)
            for cpu, its in by_cap.items()]


def _cap_day(cpu_id, items, takes, ctx, now, events, latest):
    items = sorted(items, key=lambda i: i["checkout_at"])
    still_out = any(i["returned_at"] is None for i in items)
    out_at, back_at = items[0]["checkout_at"], None if still_out else max(i["returned_at"] for i in items)
    end = now if still_out else back_at
    logged = events is not None and ctx.logged(out_at)
    seen = [e for e in events or () if out_at <= e["ts"] <= end]
    rec_starts = sum(e["type"] == "rec_start" for e in seen) if logged else None
    hours, lens_h, expired_h, short = dict.fromkeys(HOURS, 0.0), Counter(), Counter(), 0
    for t in takes:
        h, b, key = (t.get("recorded_s") or 0.0) / 3600, bucket(t), calib.take_key(t, ctx.lenses)
        hours[b] += h
        hours["total"] += h
        short += b == "short"
        if key:
            lens_h[key] += h
        if expired(t, key, ctx.cal, now):
            expired_h[key] += h
    item_lenses = [ctx.lenses.key(i["camera_id"]) for i in items if i.get("camera_id")]
    camera = lens_h.most_common(1)[0][0] if lens_h else (item_lenses[0] if item_lenses else None)
    cameras = sorted(set(lens_h) | set(item_lenses), key=calib.natural_key)
    fit = ctx.cal.last(camera) if camera else None
    versions = [t["software_version"] for t in takes if version_key(t.get("software_version"))]
    cap = ctx.caps.get(cpu_id, {})
    name = cap.get("name") or cpu_id
    row = {
        "cap": name, "cpu_id": cpu_id, "board": cap.get("board") or _mode(t.get("board") for t in takes),
        "camera": camera, "cameras": cameras, "checkout_at": out_at, "returned_at": back_at,
        "task": next((i["task"] for i in items if i.get("task")), None),
        "status": cap_status(still_out, bool(takes), back_at, rec_starts, now),
        "takes": len(takes), "clips": len(takes) - short, "short_takes": short,
        "hours": hours,
        "good_pct": pct(hours["good"], hours["total"]),
        "timing_pass_pct": pct(hours["good"], hours["good"] + hours["timing_fail"]),
        "fps": _weighted_fps(takes), "nominal_fps": _mode(t.get("nominal_fps") for t in takes),
        "p99_dev_ms_max": _max(t.get("p99_dev_ms") for t in takes),
        "boots": len({t["boot_id"] for t in takes if t.get("boot_id")}),
        "version": max(versions, key=version_key) if versions else None,
        "expired_cal_hours": sum(expired_h.values()),
        "cal_days_left": calib.days_left(fit, now) if fit else None,
        "events": count_events(seen),
    }
    usb2 = any(bucket(t) == "fps30" and on_usb2(t) for t in takes)
    row["flags"] = flags(row, latest, now, rec_starts, expired_h, usb2, logged)
    row["severity"] = row["flags"][0]["severity"] if row["flags"] else None
    row["open_tickets"] = len(ctx.tickets.on(name, cameras))
    note = ctx.statuses.get(("cap", name))
    row["my_status"] = note["status"] if note else None
    return row


def flags(row, latest, now, rec_starts, expired_h, usb2=False, logged=True):
    """The flags of a cap-day (SPEC §3 table), worst first, each with one plain
    sentence carrying the numbers. Thresholds compare unrounded hours."""
    h, ev = row["hours"], row["events"]
    out = []

    def add(code, detail):
        severity, label = FLAGS[code]
        out.append({"code": code, "severity": severity, "label": label, "detail": detail})

    back_h = (now - row["returned_at"]) / 3600 if row["returned_at"] is not None else 0.0
    if row["status"] == "no_footage":
        add("no_footage", f"back {back_h:.0f} h with no takes, and the recorder never logged a rec_start"
            if logged else f"back {back_h:.0f} h with no takes (no device log that far back)")
    if row["status"] == "footage_missing":
        add("footage_missing", f"{rec_starts} rec_start logged on the trip, but no takes {back_h:.0f} h after return")
    if row["status"] == "ok" and row["takes"] and not row["clips"]:
        add("no_clips", f"{row['takes']} takes, all under {config.MIN_CLIP_S:g} s "
                        f"({h['total'] * 3600:.0f} s in all)")
    if h["fps30"] >= config.FLAG_MIN_H:
        add("fps30", f"{h['fps30']:.1f} h at 30 fps" + (": camera on USB 2" if usb2 else ""))
    if ev["storage"] > 0:
        add("storage", f"{ev['storage']} storage events (disk full, write refused, stalled or lost)")
    if h["timing_fail"] >= config.FLAG_MIN_H:
        add("timing", f"{h['timing_fail']:.1f} h failed frame timing (score under {config.TIMING_PASS_SCORE:g})")
    if h["broken"] + h["untimed"] >= config.FLAG_MIN_H:
        add("broken", f"{h['broken'] + h['untimed']:.1f} h with no usable video or no frame timing")
    if row["short_takes"] >= config.SHORT_TAKES_MIN and 2 * row["short_takes"] >= row["takes"]:
        add("short", f"{row['short_takes']} of {row['takes']} takes under {config.MIN_CLIP_S:g} s")
    if ev["power_on"] >= config.POWER_ON_LOOP_MIN:
        add("reboots", f"powered on {ev['power_on']} times: reboot loop")
    elif ev["unclean_boot"] >= config.REBOOTS_MIN:
        add("reboots", f"{ev['unclean_boot']} unclean reboots")
    if ev["camera_lost"] >= config.CAMERA_DROPS_MIN:
        add("camera_drops", f"camera dropped {ev['camera_lost']} times")
    if row["expired_cal_hours"] >= config.EXPIRED_CAL_MIN_H:
        lenses = ", ".join(k for k, _ in expired_h.most_common() if k) or "an unknown camera"
        add("expired_cal", f"{row['expired_cal_hours']:.1f} h on {lenses} past the "
                           f"{config.RECAL_DAYS}-day calibration")
    if latest and row["version"] and version_key(row["version"]) < version_key(latest):
        add("old_firmware", f"runs {row['version']}, latest is {latest}")
    return out


def rounded(row):
    """A cap-day as the API shows it: hours to 2 decimals."""
    return dict(row, hours={k: round(v, 2) for k, v in row["hours"].items()},
                expired_cal_hours=round(row["expired_cal_hours"], 2))


def event_log(events, row, now):
    """The cap's counted events plus rec_start/rec_stop inside its window, oldest first."""
    start, end = window(row, now)
    return [{"at": e["ts"], "type": e["type"], "msg": e.get("msg")}
            for e in sorted(events, key=lambda e: e["ts"]) if start <= e["ts"] <= end and e["type"] in EVENT_TYPES]


# --------------------------------------------------------------------------- #
# one field day
# --------------------------------------------------------------------------- #
def severity_rank(severity):
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def sort_rows(rows):
    """Worst severity first (bad, warn, info, none), then most hours."""
    return sorted(rows, key=lambda r: (severity_rank(r["severity"]), -r["hours"]["total"],
                                       calib.natural_key(r["cap"])))


def day_hours(rows):
    return {k: round(sum(r["hours"][k] for r in rows), 2) for k in HOURS}


def day_kpis(rows):
    h = {k: sum(r["hours"][k] for r in rows) for k in HOURS}
    return {"caps_out": len(rows),
            "caps_recorded": sum(r["clips"] > 0 for r in rows),
            "caps_no_footage": sum(r["status"] in NOTHING for r in rows),
            "hours_total": round(h["total"], 2), "hours_good": round(h["good"], 2),
            "good_pct": pct(h["good"], h["total"]),
            "timing_pass_pct": pct(h["good"], h["good"] + h["timing_fail"]),
            "fps30_hours": round(h["fps30"], 2),
            "fps30_caps": sum(r["hours"]["fps30"] > 0 for r in rows),
            "expired_cal_hours": round(sum(r["expired_cal_hours"] for r in rows), 2)}


def arrival(rows):
    """How much of the day has come back. pct = caps whose footage arrived / caps out."""
    n = Counter(r["status"] for r in rows)
    return {"caps_out": len(rows), "arrived": n["ok"], "still_out": n["out"], "waiting": n["waiting"],
            "no_footage": n["no_footage"], "footage_missing": n["footage_missing"],
            "pct": round(100.0 * n["ok"] / len(rows)) if rows else None}


def off_trip(takes):
    return {"takes": len(takes), "hours": round(sum((t.get("recorded_s") or 0.0) for t in takes) / 3600, 2),
            "caps": len({t["cpu_id"] for t in takes})}


# --------------------------------------------------------------------------- #
# small numbers
# --------------------------------------------------------------------------- #
def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole > 0 else None


def _weighted_fps(takes):
    """recorded_s-weighted mean fps of the takes that have one."""
    pairs = [(t["fps"], t.get("recorded_s") or 0.0) for t in takes if t.get("fps") is not None]
    weight = sum(s for _f, s in pairs)
    return round(sum(f * s for f, s in pairs) / weight, 2) if weight > 0 else None


def _mode(values):
    """The most common non-None value (ties: the larger, so 60 beats 30)."""
    counts = Counter(v for v in values if v is not None)
    return max(counts, key=lambda v: (counts[v], v)) if counts else None


def _max(values):
    vals = [v for v in values if v is not None]
    return round(max(vals), 2) if vals else None
