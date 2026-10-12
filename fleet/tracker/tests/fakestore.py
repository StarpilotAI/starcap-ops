"""
fakestore.py: an in-memory stand-in for store.Store, fed from row lists.

It answers the loaders api.py calls (SPEC §2) with the same filters as the real one,
plus notes, summaries and kv, so every view is tested with no SQLite and no network.
Loaders hand out copies, so code that mutated a row would not leak into the next
call (and a test would catch it). The row builders fill every SPEC column with a
plain default: a test states only what it is about.
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clock  # noqa: E402


def at(day, hours=12.0):
    """`hours` of elapsed time after the start of a Cairo day (DST-safe: on the 25-hour
    2026-10-29, at(day, 24.5) is the second 23:30)."""
    return clock.day_bounds(day)[0] + hours * 3600


def _row(columns, defaults, kw):
    unknown = set(kw) - set(columns)
    if unknown:
        raise KeyError(f"not a column: {sorted(unknown)}")
    return {c: kw.get(c, defaults.get(c)) for c in columns}


TAKE_COLS = ("id", "cpu_id", "trip_item_id", "trip_id", "created_at", "updated_at", "first_copied_at",
             "uploaded_at", "started_at", "clock_fixed", "boot_id", "software_version", "board",
             "camera_unit", "usb_path", "recorded_s", "kept_s", "settled", "readiness_status", "codes",
             "fault_kind", "nominal_fps", "fps", "p99_dev_ms", "out_of_band_pct", "score", "drop_pct")
ITEM_COLS = ("id", "trip_id", "cpu_id", "camera_id", "checkout_at", "returned_at", "missing", "task", "destination")
STARCAP_COLS = ("cpu_id", "name", "camera_id", "board", "created_at")
CAMERA_COLS = ("id", "status", "retired", "created_at")
CAL_VERSION_COLS = ("id", "camera_id", "camera_key", "rev", "method", "calibrated_at", "uploaded_at",
                    "superseded_at", "verdict", "evidence_status", "rmse_left", "rmse_right", "rmse_stereo",
                    "baseline_mm")
CAL_CURRENT_COLS = ("id", "camera_id", "kind", "rev", "calibrated_at", "method", "evidence_status", "verdict",
                    "fit_digest")
CAMERA_EVENT_COLS = ("id", "at", "action", "camera", "headset", "replaced", "source")
TICKET_COLS = ("id", "subject", "headset", "camera", "category", "critical", "description", "reporter_name",
               "status", "created_at", "resolved_at", "resolution_note", "replacement_camera", "photos")
DEVICE_COLS = ("device_id", "cpu_id", "name", "last_seen", "software_version", "board", "status", "recording",
               "disk_free", "disk_total", "connection_type", "camera", "clock_synced")
DEVICE_EVENT_COLS = ("device_id", "seq", "ts", "type", "msg")

_ids = itertools.count(1)


def take(**kw):
    """A good 10-minute 60 fps take unless told otherwise."""
    return _row(TAKE_COLS, {"id": f"take{next(_ids)}", "recorded_s": 600.0, "kept_s": 600.0, "settled": True,
                            "readiness_status": "ready", "codes": [], "nominal_fps": 60, "fps": 60.0,
                            "score": 100.0, "p99_dev_ms": 1.0, "software_version": "1.67.0",
                            "board": "Orange Pi 5", "clock_fixed": False}, kw)


def item(**kw):
    return _row(ITEM_COLS, {"id": f"item{next(_ids)}", "trip_id": "trip", "missing": False, "task": "Kitchen"}, kw)


def starcap(**kw):
    return _row(STARCAP_COLS, {"board": "Orange Pi 5"}, kw)


def camera(**kw):
    return _row(CAMERA_COLS, {"status": "active", "retired": False}, kw)


def cal_version(**kw):
    key = str(kw.get("camera_key", kw.get("camera_id")))
    base, _, rev = key.partition(".")
    return _row(CAL_VERSION_COLS, {"id": f"cv{next(_ids)}", "camera_id": base, "camera_key": key,
                                   "rev": int(rev or 0), "method": "basalt_kb4", "verdict": "PASS",
                                   "evidence_status": "complete", "rmse_left": 0.2, "rmse_right": 0.2,
                                   "rmse_stereo": 0.3, "baseline_mm": 60.0}, kw)


def cal_current(**kw):
    return _row(CAL_CURRENT_COLS, {"id": f"cc{next(_ids)}", "kind": "stereo", "rev": 0}, kw)


def qc_run(**kw):
    """A qc_runs row as sync stores it."""
    base = {"id": f"qc{next(_ids)}", "cpu_id": "cpu1", "name": "starcap1", "model": "FriendlyElec NanoPi R76S",
            "camera_id": "43", "version": "1.67.0", "verdict": "pass", "started_at": None, "finished_at": None,
            "headline": "Every check passed.", "failed_steps": [], "station": "waleed"}
    return {**base, **kw}


def current_for(versions):
    """The dashboard's current stereo row for each lens: its newest PASS-or-ungraded fit."""
    newest = {}
    for v in versions:
        ok = v.get("verdict") is None or str(v["verdict"]).strip().upper() == "PASS"
        if ok and v["calibrated_at"] >= newest.get(v["camera_key"], {}).get("calibrated_at", float("-inf")):
            newest[v["camera_key"]] = v
    return [cal_current(camera_id=v["camera_id"], rev=v["rev"], calibrated_at=v["calibrated_at"],
                        method=v.get("method"), verdict=v.get("verdict")) for v in newest.values()]


def camera_event(**kw):
    return _row(CAMERA_EVENT_COLS, {"id": f"ce{next(_ids)}", "action": "camera.attached", "source": "kamerat"}, kw)


def ticket(**kw):
    return _row(TICKET_COLS, {"id": f"mt{next(_ids)}", "subject": "starcap", "category": "hardware",
                              "critical": False, "description": "broken", "reporter_name": "Ahmed",
                              "status": "open", "photos": 0}, kw)


def device(**kw):
    return _row(DEVICE_COLS, {"name": None, "software_version": "1.67.0", "board": "Orange Pi 5"}, kw)


def device_event(**kw):
    return _row(DEVICE_EVENT_COLS, {"seq": next(_ids), "msg": ""}, kw)


def _within(value, t0, t1):
    return value is not None and t0 <= value < t1


class FakeStore:
    def __init__(self, takes=(), items=(), starcaps=(), cameras=(), cal_versions=(), cal_current=(),
                 camera_events=(), tickets=(), devices=(), device_events=(), kv=None, qc_runs=()):
        self._takes, self._items, self._starcaps = list(takes), list(items), list(starcaps)
        self._cameras, self._cal_versions, self._cal_current = list(cameras), list(cal_versions), list(cal_current)
        self._camera_events, self._tickets = list(camera_events), list(tickets)
        self._devices, self._device_events = list(devices), list(device_events)
        self._kv = dict(kv or {})
        self._qc_runs = list(qc_runs)
        self._notes, self._summaries, self._note_ids = [], {}, itertools.count(1)

    # -- loaders ---------------------------------------------------------------
    @staticmethod
    def _copy(rows, key=None):
        return [dict(r) for r in (sorted(rows, key=key) if key else rows)]

    def starcaps(self):
        return self._copy(self._starcaps)

    def cameras(self):
        return self._copy(self._cameras)

    def qc_runs(self):
        return self._copy(self._qc_runs, key=lambda r: r.get("finished_at") or 0)

    def cal_versions(self):
        return self._copy(self._cal_versions)

    def cal_current(self):
        return self._copy(self._cal_current)

    def tickets(self):
        return self._copy(self._tickets, key=lambda t: t["created_at"])

    def devices(self):
        return self._copy(self._devices)

    def items(self, t0, t1):
        return self._copy([i for i in self._items if _within(i["checkout_at"], t0, t1)],
                          key=lambda i: i["checkout_at"])

    def open_items(self):
        return self._copy([i for i in self._items if i["returned_at"] is None], key=lambda i: i["checkout_at"])

    def field_takes(self, t0, t1):
        items = {i["id"]: i for i in self._items if _within(i["checkout_at"], t0, t1)}
        return [dict(t, checkout_at=items[t["trip_item_id"]]["checkout_at"],
                     returned_at=items[t["trip_item_id"]]["returned_at"],
                     item_camera_id=items[t["trip_item_id"]]["camera_id"])
                for t in self._takes if t["trip_item_id"] in items]

    def takes_created(self, t0, t1):
        return self._copy([t for t in self._takes if _within(t["created_at"], t0, t1)])

    def takes_off_trip(self, t0, t1):
        return self._copy([t for t in self._takes if t["trip_item_id"] is None and _within(t["created_at"], t0, t1)])

    def takes_for_camera(self, keys, t0, t1):
        keys = {str(k) for k in keys}
        return self._copy([t for t in self._takes if t["camera_unit"] in keys and _within(t["created_at"], t0, t1)])

    def camera_events(self, t0, t1):
        return self._copy([e for e in self._camera_events if _within(e["at"], t0, t1)], key=lambda e: e["at"])

    def device_events(self, t0, t1, device_ids=None, types=None):
        return self._copy([e for e in self._device_events if _within(e["ts"], t0, t1)
                           and (device_ids is None or e["device_id"] in device_ids)
                           and (types is None or e["type"] in types)], key=lambda e: e["ts"])

    def kv_get(self, key, default=None):
        return self._kv.get(key, default)

    def kv_set(self, key, value):
        self._kv[key] = value

    # -- my notes --------------------------------------------------------------
    def notes(self, subject_type=None, subject_id=None, limit=200):
        rows = [n for n in self._notes if n["deleted_at"] is None
                and subject_type in (None, n["subject_type"]) and subject_id in (None, n["subject_id"])]
        rows.sort(key=lambda n: (-n["created_at"], -n["id"]))
        return [{k: n[k] for k in ("id", "subject_type", "subject_id", "created_at", "text", "status")}
                for n in rows[:int(limit)]]

    def add_note(self, subject_type, subject_id, text, status=None):
        note = {"id": next(self._note_ids), "subject_type": subject_type, "subject_id": str(subject_id),
                "created_at": clock.now(), "text": text, "status": status, "deleted_at": None}
        self._notes.append(note)
        return {k: v for k, v in note.items() if k != "deleted_at"}

    def delete_note(self, note_id):
        for n in self._notes:
            if n["id"] == note_id and n["deleted_at"] is None:
                n["deleted_at"] = clock.now()
                return True
        return False

    # -- summaries -------------------------------------------------------------
    def summary_get(self, date):
        return dict(self._summaries[date]) if date in self._summaries else None

    def summary_put(self, date, text):
        self._summaries[date] = {"date": date, "text": text, "written_at": clock.now()}
        return dict(self._summaries[date])
