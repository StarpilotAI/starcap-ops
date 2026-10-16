"""
api.py: the page's JSON (SPEC §4), one function per endpoint.

Each function takes the store first, reads the local cache through its loaders (never
SQL), hands the rows to the pure modules, and converts at the edge: every epoch-second
time field becomes a Cairo ISO string via clock.iso(). Input is checked here, so the
pure modules can trust a real date and a known unit:

    BadRequest -> 400   a malformed or future date, an unknown note subject or status,
                        a note that is empty or too long, a days count out of range
    NotFound   -> 404   a cap, camera, note or cap-day the cache doesn't know
    Conflict   -> 409   that day's summary is already in the drafts file

A request reads each table at most once (_Req), however many views it builds: the
morning summary builds six of them.
"""
import re
from collections import Counter, defaultdict
from datetime import date as Date
from functools import cached_property

import calib
import clock
import config
import fleet
import maint
import pipeline as pipes
import qc
import rules
import summary as digest

SUBJECT_TYPES = ("cap", "camera")
NOTE_STATUSES = (None, "ok", "watch", "repair", "dead", "lost")
NOTE_MAX_CHARS = 4000            # store.py refuses longer notes too
ALL_NOTES = 1_000_000            # my notes are a few a day: this limit means "all of them"
NOTES_RECENT = 20
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_KEYS = {"at", "last_heard", "last_seen", "since", "events_from"}
TREND_KPIS = ("caps_out", "caps_recorded", "hours_total", "hours_good", "timing_pass_pct", "fps30_hours",
              "expired_cal_hours")


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
class _Req:
    """One request's view of the store: `now` read once, each table loaded at most once."""

    def __init__(self, store):
        self.store = store
        self.now = clock.now()
        self.today = clock.today(self.now)
        self.yesterday = clock.shift_day(self.today, -1)

    @cached_property
    def starcaps(self):
        return self.store.starcaps()

    @cached_property
    def devices(self):
        return self.store.devices()

    @cached_property
    def tickets(self):
        return self.store.tickets()

    @cached_property
    def open_items(self):
        return self.store.open_items()

    @cached_property
    def qc_runs(self):
        return self.store.qc_runs()

    @cached_property
    def qc_by_cpu(self):
        return qc.by_cpu(self.qc_runs)

    def qc_status(self, cpu_id, board):
        """StarCap QC status of a cap, None unless it is a NanoPi (see qc.py)."""
        return qc.status(cpu_id, board, self.qc_by_cpu, qc.qc_cpus(self.qc_runs))

    @cached_property
    def cal_current(self):
        return self.store.cal_current()

    @cached_property
    def cal_versions(self):
        return self.store.cal_versions()

    @cached_property
    def blocked(self):
        return self.store.kv_get("maint_blocked", [])

    @cached_property
    def broken(self):
        return self.store.kv_get("maint_broken_cam", [])

    @cached_property
    def kamerat_out(self):
        return self.store.kv_get("kamerat_out", {})

    @cached_property
    def lenses(self):
        return calib.Lenses(self.store.kv_get("cameras_revs", []), self.cal_current)

    @cached_property
    def cal(self):
        return calib.CalIndex(self.cal_versions, self.cal_current)

    @cached_property
    def fits(self):
        return calib.fit_history(self.cal_versions)

    @cached_property
    def imus(self):
        return calib.imu_rows(self.cal_current)

    @cached_property
    def cams(self):
        return calib.cameras(self.store.cameras(), self.store.kv_get("cameras_retired", []), self.broken,
                             self.lenses, self.cal, self.now)

    @cached_property
    def heard_at(self):
        """When sync last read the heartbeats (kv `heartbeats_at`), else now. Heartbeat
        ages are measured from here, so a healthy station doesn't read silent just
        because the next sync is still minutes away."""
        read_at = self.store.kv_get("heartbeats_at")
        return min(self.now, read_at) if read_at else self.now

    @cached_property
    def statuses(self):
        return maint.current_statuses(self.store.notes(limit=ALL_NOTES))

    @cached_property
    def ctx(self):
        return rules.Context(self.starcaps, self.devices, self.lenses, self.cal,
                             maint.OpenTickets(self.tickets), self.statuses, self.store.kv_get("events_since"))

    @cached_property
    def usage(self):
        t0 = clock.day_bounds(clock.shift_day(self.today, 1 - config.TREND_DAYS))[0]
        return calib.usage(self.store.field_takes(t0, self.now + 1), self.open_items, self.kamerat_out,
                           self.starcaps, self.lenses, self.now)


def _edge(obj):
    """Epoch seconds -> Cairo ISO for every time field: keys named `at`, `*_at`,
    `last_heard`, `last_seen` or `since`."""
    if isinstance(obj, dict):
        return {k: clock.iso(v) if _is_time(k, v) else _edge(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_edge(v) for v in obj]
    return obj


def _is_time(key, value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (key in _TIME_KEYS or key.endswith("_at")))


def _date(req, value, default):
    """A YYYY-MM-DD Cairo day that is not in the future, or `default` when none given."""
    if value in (None, ""):
        if default is None:
            raise BadRequest("date is required (YYYY-MM-DD)")
        return default
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise BadRequest(f"not a date: {value!r} (want YYYY-MM-DD)")
    try:
        Date.fromisoformat(value)
    except ValueError:
        raise BadRequest(f"not a date: {value!r}") from None
    if value > req.today:
        raise BadRequest(f"{value} is in the future (today is {req.today})")
    return value


def _starcap(req, cap):
    """The registry row of a cap name (or of a cpu_id the day view showed for an unnamed cap)."""
    for r in req.starcaps:
        if cap in (r["name"], r["cpu_id"]):
            return r
    raise NotFound(f"no cap called {cap!r}")


def _camera(req, camera_id):
    base = calib.base_of(camera_id)
    for c in req.cams:
        if c["base"] == base:
            return c
    raise NotFound(f"no camera {camera_id!r}")


def _span(req, days):
    """The last `days` Cairo days ending today, and their [t0, t1) bounds."""
    span = clock.days_between(clock.shift_day(req.today, 1 - days), req.today)
    return span, clock.day_bounds(span[0])[0], clock.day_bounds(span[-1])[1]


def _by_day(rows):
    """Rows with a checkout_at -> {Cairo day: rows}."""
    out = defaultdict(list)
    for r in rows:
        out[clock.cairo_date(r["checkout_at"])].append(r)
    return out


def _events(req, items):
    """{cpu_id: device events} for these check-outs, from the first check-out to the
    last return (or now, while any is out)."""
    cpus = {i["cpu_id"] for i in items}
    devices = [d["device_id"] for d in req.devices if d["cpu_id"] in cpus]
    if not devices:
        return {}
    t0 = min(i["checkout_at"] for i in items)
    t1 = req.now if any(i["returned_at"] is None for i in items) else max(i["returned_at"] for i in items)
    return rules.events_by_cpu(req.store.device_events(t0, t1 + 1, devices, rules.EVENT_TYPES), req.ctx)


def _latest(req, day):
    """The latest firmware as of the end of `day`, from the 7 days of takes before it."""
    end = min(clock.day_bounds(day)[1], req.now)
    return rules.latest_version(req.store.takes_created(end - 7 * clock.DAY_S, end))


def _field_day(req, day, latest=None):
    t0, t1 = clock.day_bounds(day)
    items = req.store.items(t0, t1)
    rows = rules.cap_days(items, req.store.field_takes(t0, t1), req.ctx, req.now, _events(req, items), latest)
    return [_dressed(req, r) for r in rows]


def _dressed(req, row):
    """A cap-day plus its QC status and, when it got nothing, the tickets around its trip."""
    tickets = (maint.trip_tickets(req.tickets, row["cap"], row["cameras"], row["checkout_at"], row["returned_at"],
                                  req.now) if row["status"] in rules.NOTHING else [])
    return dict(row, qc=req.qc_status(row["cpu_id"], row["board"]), trip_tickets=tickets)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status(store, sync_state):
    """GET /api/status. `sync_state` is the server's {"running", "next_at"}; the rest of
    "sync" is what the last sync run recorded."""
    req = _Req(store)
    last = store.kv_get("sync", {}) or {}
    return _edge({
        "now": clock.iso(req.now), "today": req.today, "yesterday": req.yesterday,
        "sync": {"running": bool(sync_state.get("running")), "next_at": sync_state.get("next_at"),
                 "last_run_at": last.get("last_run_at"), "last_ok_at": last.get("last_ok_at"),
                 "last_error": last.get("last_error"), "sources": last.get("sources") or {}},
        "config": {"cal_per_day": config.CAL_PER_DAY, "recal_days": config.RECAL_DAYS,
                   "min_clip_s": config.MIN_CLIP_S, "timing_pass_score": config.TIMING_PASS_SCORE,
                   "target_fps": config.TARGET_FPS},
        "dashboard_url": config.DASHBOARD_URL,
        "has_data": bool(req.starcaps),
    })


# --------------------------------------------------------------------------- #
# day
# --------------------------------------------------------------------------- #
def day(store, date=None):
    """GET /api/day (default yesterday)."""
    req = _Req(store)
    return _edge(_day(req, _date(req, date, req.yesterday)))


def _day(req, day):
    latest = _latest(req, day)
    rows = _field_day(req, day, latest)
    prev_rows = _field_day(req, clock.shift_day(day, -1))
    prev = rules.day_kpis(prev_rows) if prev_rows else {}
    t0, t1 = clock.day_bounds(day)
    since = req.ctx.events_since
    return {"date": day, "weekday": clock.weekday(day), "is_today": day == req.today, "latest_version": latest,
            "events_from": since, "events_complete": req.ctx.logged(t0),
            "arrival": rules.arrival(rows),
            "kpis": {k: {"value": v, "prev": prev.get(k)} for k, v in rules.day_kpis(rows).items()},
            "hours": rules.day_hours(rows),
            "caps": [rules.rounded(r) for r in rules.sort_rows(rows)],
            "off_trip": rules.off_trip(req.store.takes_off_trip(t0, t1))}


def day_cap(store, date, cap):
    """GET /api/day/cap: one cap's row, its takes (by start) and its event log."""
    req = _Req(store)
    day, cpu = _date(req, date, None), _starcap(req, cap)["cpu_id"]
    t0, t1 = clock.day_bounds(day)
    items = [i for i in req.store.items(t0, t1) if i["cpu_id"] == cpu]
    if not items:
        raise NotFound(f"{cap} was not checked out on {day}")
    ids = {i["id"] for i in items}
    takes = sorted((t for t in req.store.field_takes(t0, t1) if t["trip_item_id"] in ids),
                   key=lambda t: (t["started_at"] is None, t["started_at"] or t["created_at"]))
    events = _events(req, items)
    row = _dressed(req, rules.cap_days(items, takes, req.ctx, req.now, events, _latest(req, day))[0])
    return _edge({"date": day, "cap": row["cap"], "row": rules.rounded(row),
                  "takes": [rules.take_row(t, req.lenses) for t in takes],
                  "events": rules.event_log(events.get(cpu, ()), row, req.now)})


# --------------------------------------------------------------------------- #
# calibration, maintenance, field, pipeline
# --------------------------------------------------------------------------- #
def calibration(store, date=None):
    """GET /api/calibration (default today)."""
    req = _Req(store)
    return _edge(_calibration(req, _date(req, date, req.today)))


def _calibration(req, day):
    return calib.view(day, req.today, req.cams, req.usage, req.fits, req.imus)


def maintenance(store, date=None):
    """GET /api/maintenance (default yesterday)."""
    req = _Req(store)
    return _edge(_maintenance(req, _date(req, date, req.yesterday)))


def _maintenance(req, day):
    window = clock.days_between(clock.shift_day(day, 1 - config.NEEDS_ATTENTION_DAYS), day)
    return maint.view(day, req.now, req.tickets, [(d, _field_day(req, d)) for d in window], req.statuses,
                      req.store.notes(limit=NOTES_RECENT), req.blocked, req.broken, _in_shop(req))


def _in_shop(req):
    """maint.in_shop, with each cap's last field day before its ticket from the day view."""
    cap_cpu = {r["name"]: r["cpu_id"] for r in req.starcaps if r["name"]}
    items = req.store.items(0, req.now + 1)
    since = {}
    for t in req.tickets:
        if maint.is_open(t) and maint.subject(t) == "starcap" and maint.unit(t):
            since[maint.unit(t)] = min(t["created_at"], since.get(maint.unit(t), t["created_at"]))
    for cap in req.blocked:
        since.setdefault(cap, req.now)
    days, trips = {}, {}
    for cap, t in since.items():
        cpu = cap_cpu.get(cap)
        before = [i for i in items if i["cpu_id"] == cpu and i["checkout_at"] < t]
        if not before:
            continue
        last = max(before, key=lambda i: i["checkout_at"])
        day = clock.cairo_date(last["checkout_at"])
        if day not in days:
            days[day] = {r["cpu_id"]: r for r in _field_day(req, day)}
        r = days[day].get(cpu)
        if r:
            trips[cap] = {"day": day, "checkout_at": last["checkout_at"], "hours": round(r["hours"]["total"], 1),
                          "good_pct": r.get("good_pct"), "flags": r.get("flags") or []}
    return maint.in_shop(req.tickets, req.now, req.heard_at, cap_cpu, items, fleet.last_heard(req.devices),
                         req.qc_by_cpu, req.statuses, trips, req.blocked, req.broken)


def field(store):
    """GET /api/field."""
    return _edge(_field(_Req(store)))


def _field(req):
    view = fleet.view(req.now, req.heard_at, req.starcaps, req.devices, req.open_items, req.kamerat_out,
                      req.blocked, req.lenses)
    found = (req.store.kv_get("qc_source") or {}).get("found", False)
    view["qc"] = qc.overview(req.starcaps, req.qc_runs, {i["cpu_id"] for i in req.open_items}, found)
    return view


def pipeline(store):
    """GET /api/pipeline."""
    return _edge(_pipeline(_Req(store)))


def _pipeline(req):
    now, kv = req.now, req.store.kv_get
    events = req.store.device_events(now - 3 * clock.DAY_S, now + 1, types=("rec_start",))
    rec_starts = {cpu: [e["ts"] for e in evs] for cpu, evs in rules.events_by_cpu(events, req.ctx).items()}
    return pipes.view(now, req.heard_at, kv("stations", []), kv("uploaders", []), kv("uploader_progress", {}),
                      kv("label_tick"), req.store.takes_created(now - config.HISTORY_DAYS * clock.DAY_S, now + 1),
                      req.store.items(now - 14 * clock.DAY_S, now + 1), rec_starts)


# --------------------------------------------------------------------------- #
# trends
# --------------------------------------------------------------------------- #
def trends(store, days=config.TREND_DAYS):
    """GET /api/trends: one row per day, oldest first, ending today (partial)."""
    req = _Req(store)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= config.HISTORY_DAYS:
        raise BadRequest(f"days must be a whole number from 1 to {config.HISTORY_DAYS}")
    span, t0, t1 = _span(req, days)
    items, takes = _by_day(req.store.items(t0, t1)), _by_day(req.store.field_takes(t0, t1))
    fits = Counter(clock.cairo_date(f["calibrated_at"]) for f in req.fits)
    tickets = {r["date"]: r["open"] for r in maint.trend(req.tickets, span)}
    out = []
    for d in span:
        k = rules.day_kpis(rules.cap_days(items[d], takes[d], req.ctx, req.now))
        out.append({"date": d, **{name: k[name] for name in TREND_KPIS},
                    "cal_fits": fits[d], "open_tickets": tickets[d],
                    "overdue_cameras": calib.overdue_at(req.cams, req.cal, min(clock.day_bounds(d)[1], req.now)),
                    "partial": d == req.today})
    return {"days": out}


# --------------------------------------------------------------------------- #
# one unit
# --------------------------------------------------------------------------- #
def cap(store, name):
    """GET /api/cap/<name>: the last TREND_DAYS field days of one cap, newest first."""
    req = _Req(store)
    reg = _starcap(req, name)
    cpu, name = reg["cpu_id"], reg["name"] or reg["cpu_id"]
    span, t0, t1 = _span(req, config.TREND_DAYS)
    items = [i for i in req.store.items(t0, t1) if i["cpu_id"] == cpu]
    ids = {i["id"] for i in items}
    takes = _by_day(t for t in req.store.field_takes(t0, t1) if t["trip_item_id"] in ids)
    events = _events(req, items)
    rows = [(d, rules.cap_days(its, takes[d], req.ctx, req.now, events)[0])
            for d, its in sorted(_by_day(items).items(), reverse=True)]
    devices = sorted((d for d in req.devices if d["cpu_id"] == cpu and d.get("last_seen") is not None),
                     key=lambda d: -d["last_seen"])
    heard = devices[0]["last_seen"] if devices else None
    open_item = min((i for i in req.open_items if i["cpu_id"] == cpu), key=lambda i: i["checkout_at"], default=None)
    note = req.statuses.get(("cap", name))
    return _edge({
        "cap": name, "cpu_id": cpu, "board": reg.get("board"),
        "version": devices[0].get("software_version") if devices else None,
        "online": heard is not None and req.heard_at - heard < config.ONLINE_S, "last_heard": heard,
        "out_now": None if open_item is None else {
            "checkout_at": open_item["checkout_at"],
            "days_out": round((req.now - open_item["checkout_at"]) / clock.DAY_S, 1),
            "camera": req.lenses.key(open_item["camera_id"]) if open_item.get("camera_id") else None},
        "camera_now": req.lenses.key(reg["camera_id"]) if reg.get("camera_id") else None,
        "my_status": note["status"] if note else None,
        "qc": req.qc_status(cpu, reg.get("board")),
        "qc_runs": qc.history(cpu, req.qc_by_cpu),
        "totals_30d": {"days_out": len(rows), "hours": round(sum(r["hours"]["total"] for _d, r in rows), 2),
                       "good": round(sum(r["hours"]["good"] for _d, r in rows), 2),
                       "no_footage_days": sum(r["status"] in rules.NOTHING for _d, r in rows)},
        "days": [{"date": d, "status": r["status"], "camera": r["camera"],
                  "hours": rules.rounded(r)["hours"], "takes": r["takes"], "flags": r["flags"]} for d, r in rows],
        "tickets": maint.unit_tickets(req.tickets, req.now, cap=name),
        "camera_history": [{"at": e["at"], "action": e["action"], "camera": e["camera"], "replaced": e["replaced"]}
                           for e in reversed(store.camera_events(0, req.now + 1)) if e["headset"] == name],
        "notes": [maint.note_row(n) for n in store.notes("cap", name)],
    })


def camera(store, camera_id):
    """GET /api/camera/<id>: "43" means its fitted lens, "43.1" that lens exactly."""
    req = _Req(store)
    cam = _camera(req, str(camera_id))
    base, rev = calib.split_key(str(camera_id))
    key = calib.camera_key(base, rev) if rev else cam["camera"]
    state = calib.lens_state(key, req.cal, req.now)
    imu = calib.newest_imu(req.imus).get(base)
    span, t0, t1 = _span(req, config.TREND_DAYS)
    keys = [calib.camera_key(base, r) for r in range(max(rev, req.lenses.rev(base)) + 1)]
    checkout = {i["id"]: i["checkout_at"] for i in store.items(t0 - 14 * clock.DAY_S, t1)}
    names = {r["cpu_id"]: r["name"] or r["cpu_id"] for r in req.starcaps}
    used = defaultdict(lambda: {"hours": 0.0, "caps": set()})
    for t in store.takes_for_camera(keys, t0, t1):
        d = clock.cairo_date(checkout.get(t["trip_item_id"], t["created_at"]))
        used[d]["hours"] += (t.get("recorded_s") or 0.0) / 3600
        used[d]["caps"].add(names.get(t["cpu_id"], t["cpu_id"]))
    return _edge({
        "camera": key, "base": base, "rev": calib.split_key(key)[1], "state": state["state"],
        "days_left": state["days_left"], "due_on": state["due_on"], "last_cal_at": state["last_cal_at"],
        "retired": cam["retired"], "broken": cam["broken"],
        "fits": [f for f in req.fits if f["base"] == base],
        "imu": None if imu is None else {"calibrated_at": imu["calibrated_at"],
                                         "outdated": calib.imu_outdated(imu, state)},
        "usage": [{"date": d, "hours": round(u["hours"], 2), "caps": sorted(u["caps"], key=calib.natural_key)}
                  for d, u in sorted(used.items(), reverse=True) if d >= span[0]],
        "cap_history": [{"at": e["at"], "action": e["action"], "cap": e["headset"], "replaced": e["replaced"]}
                        for e in reversed(store.camera_events(0, req.now + 1))
                        if e["camera"] is not None and calib.base_of(e["camera"]) == base],
        "tickets": maint.unit_tickets(req.tickets, req.now, camera=base),
        "notes": [maint.note_row(n) for n in store.notes("camera", limit=ALL_NOTES)
                  if calib.base_of(n["subject_id"]) == base],
    })


# --------------------------------------------------------------------------- #
# my notes
# --------------------------------------------------------------------------- #
def notes(store, subject_type=None, subject_id=None):
    """GET /api/notes, newest first."""
    if subject_type in (None, "") and subject_id not in (None, ""):
        raise BadRequest("subject_id needs a subject_type")
    if subject_type not in (None, "") and subject_type not in SUBJECT_TYPES:
        raise BadRequest(f"subject_type must be one of {', '.join(SUBJECT_TYPES)}")
    rows = store.notes(subject_type or None, subject_id or None)
    return _edge({"notes": [maint.note_row(n) for n in rows]})


def add_note(store, body):
    """POST /api/notes {subject_type, subject_id, text, status} -> the note."""
    req = _Req(store)
    kind, unit = body.get("subject_type"), str(body.get("subject_id") or "").strip()
    text, status = body.get("text") or "", body.get("status") or None
    if isinstance(status, str):
        status = status.strip().lower() or None
    if kind not in SUBJECT_TYPES:
        raise BadRequest(f"subject_type must be one of {', '.join(SUBJECT_TYPES)}")
    if not isinstance(text, str):
        raise BadRequest("text must be a string")
    text = text.strip()
    if status not in NOTE_STATUSES:
        raise BadRequest(f"status must be one of {', '.join(s for s in NOTE_STATUSES if s)}, or empty")
    if len(text) > NOTE_MAX_CHARS:
        raise BadRequest(f"a note is at most {NOTE_MAX_CHARS} characters")
    if not text and status is None:
        raise BadRequest("a note needs text or a status")
    if kind == "cap":
        reg = _starcap(req, unit)
        unit = reg["name"] or reg["cpu_id"]
    else:
        _camera(req, unit)
        unit = calib.norm_key(unit)
    return _edge(maint.note_row(store.add_note(kind, unit, text, status)))


def delete_note(store, note_id):
    """POST /api/notes/<id>/delete."""
    if isinstance(note_id, bool) or not isinstance(note_id, int):
        raise BadRequest(f"not a note id: {note_id!r}")
    if not store.delete_note(note_id):
        raise NotFound(f"no note {note_id}")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# the morning summary
# --------------------------------------------------------------------------- #
def _views(req, day):
    return {"date": day, "today": req.today, "day": _day(req, day), "before": _day(req, clock.shift_day(day, -1)),
            "calibration": _calibration(req, day), "maintenance": _maintenance(req, day),
            "field": _field(req), "pipeline": _pipeline(req)}


def summary(store, date=None):
    """GET /api/summary (default yesterday): the written text if that day went into the
    drafts file, else what would be written now (written_at null)."""
    req = _Req(store)
    day = _date(req, date, req.yesterday)
    saved = store.summary_get(day)
    if saved:
        return _edge({"date": day, "text": saved["text"], "written_at": saved["written_at"]})
    return {"date": day, "text": digest.text(_views(req, day)), "written_at": None}


def write_summary(store, body):
    """POST /api/summary/write {date, force} -> {ok, path, written_at}."""
    req = _Req(store)
    day = _date(req, body.get("date"), req.yesterday)
    try:
        return _edge(digest.write(store, day, digest.text(_views(req, day)), bool(body.get("force"))))
    except digest.AlreadyWritten as e:
        raise Conflict(str(e)) from None


def units(store):
    """GET /api/units: every cap name and camera (fitted lens id) for the pickers, and
    each cap's board kind (NanoPi / Orange Pi) for the tag next to every cap name."""
    req = _Req(store)
    seen = qc.qc_cpus(req.qc_runs)
    boards = {r["name"]: qc.board_kind(r.get("board"), r["cpu_id"], seen) for r in req.starcaps if r["name"]}
    return {"caps": sorted(boards, key=calib.natural_key),
            "cameras": sorted({c["camera"] for c in req.cams}, key=calib.natural_key),
            "boards": {k: v for k, v in boards.items() if v}}
