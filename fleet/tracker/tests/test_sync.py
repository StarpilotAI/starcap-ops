"""Offline tests for sync.py: the normalisation helpers, and whole passes of the Syncer
against fake Postgres and Upstash objects (which enforce the same SQL guard and Redis
allowlist as the real clients, so a test that sends a write fails).

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import fcntl
import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
import sources  # noqa: E402
import sync  # noqa: E402
from store import Store  # noqa: E402

DAY = 86400.0
PHONE = "+201001234567"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakePG:
    """Answers each statement by which of sync's SQL constants it is. Every statement
    goes through the real read-only guard first."""

    def __init__(self, data=None, fail=None, timeout_if=None, block=None):
        self.data = {"starcaps": [], "cameras": [], "cal_versions": [], "cal_current": [], "items": [],
                     "takes": [], "camera_events": []}
        self.data.update(data or {})
        self.fail = fail or {}
        self.timeout_if = timeout_if
        self.block = block                  # (entered Event, release Event): pause the first query
        self.calls = []
        self.ensured = 0

    def ensure(self):
        self.ensured += 1

    def kind(self, sql):
        named = {sync.SQL_STARCAPS: "starcaps", sync.SQL_CAMERAS: "cameras",
                 sync.SQL_CAL_VERSIONS: "cal_versions", sync.SQL_CAL_CURRENT: "cal_current",
                 sync.SQL_ITEMS: "items", sync.SQL_CAMERA_EVENTS: "camera_events",
                 sync.takes_sql(sync.TAKES_BY_CREATED): "takes_created",
                 sync.takes_sql(sync.TAKES_BY_UPDATED): "takes_updated"}
        return named[sql]

    def query(self, sql, params=None, timeout_s=None):
        sources.check_read_only(sql)
        kind = self.kind(sql)
        p = dict(params or {})
        self.calls.append((kind, p))
        if self.block:
            entered, release = self.block
            self.block = None
            entered.set()
            release.wait(5)
        if kind in self.fail:
            raise self.fail[kind]
        if kind == "takes_created":
            if self.timeout_if and self.timeout_if(p):
                raise sources.QueryTimeout("cancelled")
            return [r for r in self.data["takes"] if p["t0"] <= r["created_at"] < p["t1"]]
        if kind == "takes_updated":
            return [r for r in self.data["takes"]
                    if r["updated_at"] > p["updated"] and p["floor"] <= r["created_at"] < p["t0"]]
        return [dict(r) for r in self.data[kind]]

    def windows(self):
        return [(p["t0"], p["t1"]) for k, p in self.calls if k == "takes_created"]


class FakeRedis:
    """An in-memory Upstash. Every command is checked against the real allowlist."""

    def __init__(self, strings=None, hashes=None, sets=None, zsets=None):
        self.strings = dict(strings or {})
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.sets = {k: set(v) for k, v in (sets or {}).items()}
        self.zsets = {k: list(v) for k, v in (zsets or {}).items()}   # key -> [(score, member)]
        self.sent = []
        self.requests = 0

    def stats(self):
        return {"requests": self.requests, "commands": len(self.sent), "bytes_sent": 0, "bytes_received": 0}

    def pipeline(self, cmds):
        cmds = [[str(a) for a in c] for c in cmds]
        for c in cmds:
            sources.check_read_command(c)
        self.requests += (len(cmds) + 49) // 50
        self.sent += cmds
        return [self._one(c) for c in cmds]

    def _zsorted(self, key):
        return sorted(self.zsets.get(key, []), key=lambda sm: (sm[0], sm[1]))

    def _one(self, c):
        name, key = c[0].upper(), c[1] if len(c) > 1 else None
        if name == "GET":
            return self.strings.get(key)
        if name == "MGET":
            return [self.strings.get(k) for k in c[1:]]
        if name == "HGETALL":
            return [x for f, v in self.hashes.get(key, {}).items() for x in (f, v)]
        if name == "HKEYS":
            return list(self.hashes.get(key, {}))
        if name == "SMEMBERS":
            return sorted(self.sets.get(key, set()))
        if name == "ZREVRANGE":
            members = [m for _, m in reversed(self._zsorted(key))]
            start, stop = int(c[2]), int(c[3])
            return members[start:None if stop == -1 else stop + 1]
        if name == "ZRANGEBYSCORE":
            lo, hi = c[2], c[3]
            excl = lo.startswith("(")
            lo_v = float(lo.lstrip("("))
            hi_v = float("inf") if hi == "+inf" else float(hi)
            out = [m for s, m in self._zsorted(key) if (s > lo_v if excl else s >= lo_v) and s <= hi_v]
            if "LIMIT" in c:
                i = c.index("LIMIT")
                off, n = int(c[i + 1]), int(c[i + 2])
                out = out[off:off + n]
            return out
        raise AssertionError(f"fake redis has no {name}")

    def keys_touched(self, name):
        return [c[1] for c in self.sent if c[0] == name]


# --------------------------------------------------------------------------- #
# fixtures shaped like the real rows (2026-10-12 discovery)
# --------------------------------------------------------------------------- #
def raw_take(id_, created_at, **kw):
    row = {"id": id_, "cpu_id": "cpuA", "trip_item_id": "i1", "trip_id": "co_1", "created_at": created_at,
           "updated_at": created_at, "recorded_s": 600.5, "kept_s": 580.0, "settled": False,
           "readiness_status": "ready", "first_copied_at": "2026-10-11T08:00:00Z",
           "uploaded_at": 1791805884760, "started_at": "2026-10-11T10:22:04+0300", "clock_fixed": True,
           "boot_id": "f361aa", "software_version": "1.67.0", "board": "Orange Pi 5", "camera_unit": "43.1",
           "usb_path": "1-1", "fault_kind": None, "codes": ["no_chunks"], "fps_left": 60.05, "fps_stereo": None,
           "nominal_fps": 60, "score": 99.6, "p99_dev_ms": 3.3, "out_of_band_pct": 0.2, "drop_pct": 0.0}
    row.update(kw)
    return row


def pg_data(now):
    return {
        "starcaps": [{"cpu_id": "cpuA", "name": "starcap12", "camera_id": "43", "board": None,
                      "created_at": now - 90 * DAY},
                     {"cpu_id": "cpuB", "name": "starcap13", "camera_id": None, "board": None,
                      "created_at": now - 90 * DAY}],
        "cameras": [{"id": "43", "status": None, "retired": False, "created_at": now - 99 * DAY},
                    {"id": "77", "status": None, "retired": True, "created_at": now - 99 * DAY}],
        "cal_versions": [{"id": "calv_43.1_ab", "camera_id": "43", "camera_key": "43.1", "rev": 1,
                          "method": "basalt_kb4", "calibrated_at": now - 5 * DAY, "uploaded_at": now - 5 * DAY,
                          "superseded_at": None, "verdict": "PASS", "evidence_status": "complete",
                          "rmse_left": 0.26, "rmse_right": 0.23, "rmse_stereo": 0.31, "baseline_mm": 60.07}],
        "cal_current": [{"id": "cal_43r1", "camera_id": "43", "kind": "stereo", "rev": None,
                         "calibrated_at": now - 5 * DAY, "method": "basalt_kb4", "evidence_status": "complete",
                         "verdict": "PASS", "fit_digest": "ab"},
                        {"id": "imu_106", "camera_id": "106", "kind": "imu", "rev": None,
                         "calibrated_at": now - 4 * DAY, "method": "basalt_imu", "evidence_status": "complete",
                         "verdict": "FAIL", "fit_digest": "cd"}],
        "items": [{"id": "i1", "trip_id": "co_1", "cpu_id": "cpuA", "camera_id": "43",
                   "checkout_at": now - DAY, "returned_at": now - 0.5 * DAY, "missing": False,
                   "task": "Repair Services", "destination": "Shubra"}],
        "takes": [raw_take(f"t{i}", now - i * 0.5 * DAY) for i in range(1, 80)],
        "camera_events": [{"id": "ev1", "at": now - DAY, "action": "camera.attached", "camera": "43",
                           "headset": "starcap12", "replaced": "16", "source": "device"}],
    }


def log_member(seq, ts_s, type_, msg="m"):
    return json.dumps({"seq": seq, "ts": int(ts_s * 1000), "utc": "x", "type": type_, "msg": msg,
                       "boot_id": "b", "data": {"k": 1}})


TYPES_CYCLE = ("power_on", "led", "rec_start", "wifi", "camera_lost", "rec_stop", "button")


def redis_data(now, events=1200):
    fleet = {
        "pi-1|self:pi-1": {"device_id": "pi-1", "cpu_id": "cpuA", "name": "starcap12",
                           "last_seen": int((now - 2000) * 1000), "software_version": "1.67.0",
                           "board": "Orange Pi 5", "status": "recording", "recording": True, "disk_free": 5,
                           "disk_total": 10, "connection_type": "wifi", "camera": 43, "clock_synced": True},
        "pi-2|self:pi-2": {"device_id": "pi-2", "cpu_id": "cpuA", "last_seen": int((now - 20 * DAY) * 1000)},
        "pi-3|self:pi-3": {"device_id": "pi-3", "cpu_id": "cpuVIN", "last_seen": int(now * 1000)},
        "pi-4|self:pi-4": {"device_id": "pi-4", "cpu_id": "cpuB", "last_seen": int((now - 2000) * 1000)},
    }
    tickets = {
        "mt_1": {"id": "mt_1", "subject": "camera", "headset": "starcap12", "camera": 43, "category": "hardware",
                 "critical": False, "description": "soft eye", "reporter_name": "w", "photos": ["a", "b"],
                 "status": "open", "created_at": int((now - 3 * DAY) * 1000)},
        "mt_2": {"id": "mt_2", "headset": "starcap13", "category": "software", "critical": True,
                 "description": "x", "photos": [], "status": "resolved", "created_at": int((now - 9 * DAY) * 1000),
                 "resolved_at": int((now - 8 * DAY) * 1000), "resolution_note": "swapped",
                 "replacement_camera": 16},
    }
    logs = [(s, log_member(s, now - 5 * DAY + s * (5 * DAY / events), TYPES_CYCLE[s % len(TYPES_CYCLE)]))
            for s in range(1, events + 1)]
    return FakeRedis(
        strings={"starcap:ship:label:tick": json.dumps({"status": "ok", "ok": True, "at": int(now * 1000),
                                                       "last_ok_at": int(now * 1000),
                                                       "label": {"waiting": 44, "pending": 0}, "reasons": []}),
                 **{f"starcap:maint:ticket:{k}": json.dumps(v) for k, v in tickets.items()}},
        hashes={
            "starcap:fleet": {k: json.dumps(v) for k, v in fleet.items()},
            "starcap:kamerat:out": {"starcap12": json.dumps({"headset": "starcap12", "operator_id": "op_201001234567",
                                                             "operator_name": "Ali", "operator_phone": PHONE,
                                                             "event_id": "co_1", "checked_out_at": int(now * 1000),
                                                             "destination": "Shubra", "camera": "43"}),
                                    "starcap2": json.dumps({"operator_name": "Mona", "operator_phone": PHONE,
                                                            "checked_out_at": int(now * 1000), "camera": 9})},
            "starcap:cameras:retired": {"169": json.dumps({"camera": "169"}), "77": json.dumps({"camera": "77"})},
            "starcap:opm": {"opm-1": json.dumps({"station_name": "copy1", "last_seen": int(now * 1000),
                                                 "activity": {"state": "idle", "message": "up"},
                                                 "drives": [{"role": "staging", "bytes_free": 5, "bytes_total": 9,
                                                             "path": "/media/x"}],
                                                 "pending_review": 0, "error": None})},
            "starcap:ssd": {"ssd-1": json.dumps({"name": "CELL-2", "last_seen": int(now * 1000),
                                                 "detection": {"state": "uploading", "level": "ok", "message": "m",
                                                               "drives": []},
                                                 "current": {"run": "rec_1", "sent": 100, "total": 1000,
                                                             "rate_bps": 5, "file": "f"},
                                                 "runs": [{"status": "done"}] * 3 + [{"status": "pending"}],
                                                 "logs": [{"t": int(now * 1000) - i, "level": "info", "msg": f"l{i}"}
                                                          for i in range(30)],
                                                 "error": None})},
        },
        sets={"starcap:maint:blocked": {"starcap13", "starcap2"}, "starcap:maint:broken_cam": {"9", "100"},
              "starcap:cameras:revs": {"cam-61.1", "cam-1.1", "cam-100.2"}},
        zsets={"starcap:maint:index": [(t["created_at"], k) for k, t in tickets.items()],
               "starcap:logs:pi-1": logs,
               "starcap:logs:pi-2": [(1, log_member(1, now - 20 * DAY, "power_on"))],
               "starcap:logs:pi-3": [(1, log_member(1, now, "power_on"))]},
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tracker.db"
        self.store = Store(self.db)
        self.now = time.time()
        self.qc_dir = Path(self.tmp.name) / "qc-reports"         # never the real ~/starcap-qc/reports
        patch = mock.patch.object(config, "QC_REPORTS_DIR", self.qc_dir)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def syncer(self, pg=None, redis=None):
        return sync.Syncer(self.store, pg=pg or FakePG(pg_data(self.now)), redis=redis or redis_data(self.now))


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
class Normalise(unittest.TestCase):
    def test_take(self):
        t = sync.normalise_take(raw_take("a", 1791700000.0, nominal_fps=60.0, fps_left=None, fps_stereo=59.9,
                                         camera_unit=43, settled=None, codes=None))
        self.assertEqual(list(t), [c for c, _ in __import__("store").TABLES["takes"]])
        self.assertEqual(t["uploaded_at"], 1791805884.76)                     # ms -> s
        self.assertEqual(t["started_at"], 1791703324.0)                       # "+0300" = 07:22:04Z
        self.assertEqual(t["first_copied_at"], 1791705600.0)                  # "...Z"
        self.assertEqual(t["nominal_fps"], 60)
        self.assertIsInstance(t["nominal_fps"], int)
        self.assertEqual(t["fps"], 59.9)                                      # stereo fallback
        self.assertEqual(t["camera_unit"], "43")
        self.assertIsNone(t["settled"])
        self.assertEqual(t["codes"], [])
        self.assertIs(t["clock_fixed"], True)

    def test_take_odd_values(self):
        t = sync.normalise_take(raw_take("a", 1.0, fps_left="n/a", fps_stereo=None, score="99.5", nominal_fps=None,
                                         uploaded_at=None, started_at="garbage", codes=["no_chunks", 3]))
        self.assertIsNone(t["fps"])
        self.assertEqual(t["score"], 99.5)
        self.assertIsNone(t["nominal_fps"])
        self.assertIsNone(t["uploaded_at"])
        self.assertIsNone(t["started_at"])
        self.assertEqual(t["codes"], ["no_chunks", "3"])

    def test_cal_rev(self):
        self.assertEqual(sync.cal_rev("2", "cal_39r1"), 2)
        self.assertEqual(sync.cal_rev(None, "cal_39r1"), 1)
        self.assertEqual(sync.cal_rev(None, "cal_163"), 0)
        self.assertEqual(sync.cal_rev(None, "imu_106"), 0)

    def test_ticket(self):
        t = sync.normalise_ticket({"headset": "starcap5", "camera": 43, "photos": ["a"], "critical": 1,
                                   "created_at": 1791700000123, "replacement_camera": 16.0}, "mt_9")
        self.assertEqual((t["id"], t["subject"], t["camera"], t["photos"], t["critical"]),
                         ("mt_9", "starcap", "43", 1, True))
        self.assertEqual(t["replacement_camera"], "16")
        self.assertEqual(t["created_at"], 1791700000.123)
        self.assertIsNone(sync.normalise_ticket(None))
        self.assertIsNone(sync.normalise_ticket({}))

    def test_devices_from_fleet_freshest_wins(self):
        flat = ["pi-1|self:pi-1", json.dumps({"device_id": "pi-1", "last_seen": 2000, "camera": 43,
                                              "recording": 0, "board": "A"}),
                "pi-1|ssd-x", json.dumps({"device_id": "pi-1", "last_seen": 1000, "board": "B"}),
                "pi-9|self:pi-9", "not json"]
        rows = sync.devices_from_fleet(flat)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["board"], rows[0]["camera"], rows[0]["recording"]), ("A", "43", False))
        self.assertEqual(rows[0]["last_seen"], 2000.0)          # small numbers are seconds already

    def test_kamerat_out_drops_phone_and_id(self):
        out = sync.kamerat_out(["starcap10", json.dumps({"operator_name": "A", "operator_phone": PHONE,
                                                         "operator_id": "op_x", "checked_out_at": 1791700000000,
                                                         "camera": 51}),
                                "starcap2", json.dumps({"operator_name": "B", "checked_out_at": 1791700000000})])
        self.assertEqual(list(out), ["starcap2", "starcap10"])
        self.assertEqual(out["starcap10"], {"operator_name": "A", "checked_out_at": 1791700000.0, "camera": "51",
                                            "destination": None})
        self.assertNotIn(PHONE, json.dumps(out))

    def test_uploader_summary(self):
        u = sync.uploader_from_ssd("ssd-1", {"name": "n", "last_seen": 1791700000000,
                                             "runs": [{"status": "done"}, {"status": "done"}, {"status": "weird"}],
                                             "logs": [{"t": 1791700000000 + i, "level": "info", "msg": str(i)}
                                                      for i in range(25)],
                                             "current": {"run": "r", "sent": "10", "total": 20, "rate_bps": 3}})
        self.assertEqual(u["runs"], {"pending": 0, "uploading": 0, "done": 2, "error": 0, "weird": 1})
        self.assertEqual(len(u["logs"]), 20)
        self.assertEqual(u["logs"][-1], {"at": 1791700000.024, "level": "info", "msg": "24"})
        self.assertEqual(u["current"]["sent"], 10)
        self.assertEqual(u["last_seen"], 1791700000.0)

    def test_uploader_progress(self):
        ups = [{"id": "u", "current": {"run": "r1", "sent": 10}}]
        p1 = sync.uploader_progress(None, ups, 100.0)
        self.assertEqual(p1, {"u": {"run": "r1", "sent": 10, "changed_at": 100.0}})
        self.assertEqual(sync.uploader_progress(p1, ups, 200.0)["u"]["changed_at"], 100.0)      # stuck
        moved = [{"id": "u", "current": {"run": "r1", "sent": 11}}]
        self.assertEqual(sync.uploader_progress(p1, moved, 300.0)["u"]["changed_at"], 300.0)
        other = [{"id": "u", "current": {"run": "r2", "sent": 10}}]
        self.assertEqual(sync.uploader_progress(p1, other, 400.0)["u"]["changed_at"], 400.0)
        idle = [{"id": "u", "current": None}]
        p2 = sync.uploader_progress(p1, idle, 500.0)
        self.assertEqual(sync.uploader_progress(p2, idle, 600.0)["u"]["changed_at"], 500.0)

    def test_label_tick(self):
        t = sync.label_tick({"at": 1791700000000, "ok": True, "last_ok_at": 1791690000000,
                             "label": {"waiting": 44}, "status": "ok"})
        self.assertEqual((t["at"], t["last_ok_at"], t["waiting"], t["label"]["waiting"]),
                         (1791700000.0, 1791690000.0, 44, 44))
        self.assertIsNone(sync.label_tick(None))

    def test_event_row(self):
        self.assertIsNone(sync.event_row("pi", {"seq": 1, "ts": 1791700000000, "type": "led"}))
        self.assertIsNone(sync.event_row("pi", {"seq": None, "ts": 1791700000000, "type": "power_on"}))
        r = sync.event_row("pi", {"seq": 7, "ts": 1791700000000, "type": "camera_usb2", "msg": "x" * 900})
        self.assertEqual((r["seq"], r["ts"], r["type"], len(r["msg"])), (7, 1791700000.0, "camera_usb2", 500))

    def test_windows_newest_first_and_contiguous(self):
        w = sync.windows(0.0, 10.0, 4.0)
        self.assertEqual(w, [(6.0, 10.0), (2.0, 6.0), (0.0, 2.0)])
        self.assertEqual(sync.windows(5.0, 5.0, 1.0), [])

    def test_natural_key(self):
        self.assertEqual(sorted(["starcap10", "starcap2", "starcap1"], key=sync.natural_key),
                         ["starcap1", "starcap2", "starcap10"])
        self.assertEqual(sorted(["100", "43.1", "43", "9"], key=sync.natural_key), ["9", "43", "43.1", "100"])


class SqlShape(unittest.TestCase):
    ALL = (sync.SQL_STARCAPS, sync.SQL_CAMERAS, sync.SQL_CAL_VERSIONS, sync.SQL_CAL_CURRENT, sync.SQL_ITEMS,
           sync.SQL_CAMERA_EVENTS, sync.takes_sql(sync.TAKES_BY_CREATED), sync.takes_sql(sync.TAKES_BY_UPDATED))

    def test_every_statement_passes_the_guard_and_names_the_project(self):
        for sql in self.ALL:
            sources.check_read_only(sql)
            self.assertIn("%(project)s", sql)
            self.assertNotIn("select *", sql.lower())

    def test_dragons_are_dropped_in_sql(self):
        for sql in (sync.SQL_STARCAPS, sync.SQL_ITEMS, sync.SQL_TAKES):
            self.assertIn("'dragonCamera'", sql)
        for sql in (sync.SQL_CAMERAS, sync.SQL_CAL_VERSIONS, sync.SQL_CAL_CURRENT, sync.SQL_CAMERA_EVENTS,
                    sync.SQL_ITEMS):
            self.assertIn("'dragon:'", sql)

    def test_takes_reads_the_document_once_and_never_returns_it(self):
        sql = sync.takes_sql(sync.TAKES_BY_CREATED)
        self.assertEqual(sql.count("data || '{}'::jsonb as d"), 1)
        self.assertEqual(sql.count("data"), 1)
        self.assertIn("offset 0", sql)


# --------------------------------------------------------------------------- #
# whole passes
# --------------------------------------------------------------------------- #
class Passes(Base):
    def test_first_pass_fills_everything(self):
        s = self.syncer()
        status = s.run_once()
        self.assertEqual(list(status["sources"]), list(sync.STEPS))
        for name, src in status["sources"].items():
            self.assertEqual(set(src), {"ok", "at", "rows", "took_s", "error"})
            self.assertTrue(src["ok"], (name, src["error"]))
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["last_ok_at"])
        self.assertEqual(self.store.kv_get("sync"), status)
        st = self.store
        self.assertEqual(len(st.starcaps()), 2)
        self.assertEqual({c["cpu_id"]: c["board"] for c in st.starcaps()}["cpuA"], "Orange Pi 5")   # filled
        self.assertEqual([c["rev"] for c in st.cal_current()], [0, 1])
        self.assertEqual(st.kv_get("cameras_revs"), ["1.1", "61.1", "100.2"])
        self.assertEqual(st.kv_get("cameras_retired"), ["77", "169"])
        self.assertEqual(st.kv_get("maint_blocked"), ["starcap2", "starcap13"])
        self.assertEqual(st.kv_get("maint_broken_cam"), ["9", "100"])
        self.assertEqual({t["id"]: t["camera"] for t in st.tickets()}, {"mt_1": "43", "mt_2": None})
        self.assertEqual(st.kv_get("label_tick")["waiting"], 44)
        self.assertAlmostEqual(st.kv_get("heartbeats_at"), time.time(), delta=60)   # api ages heartbeats from it
        self.assertEqual(st.kv_get("stations")[0]["drives"], [{"role": "staging", "bytes_free": 5, "bytes_total": 9}])
        self.assertEqual(st.kv_get("uploaders")[0]["runs"]["done"], 3)
        self.assertEqual(len(st.takes_created(0, 2e10)), 79)
        self.assertEqual(sorted(d["device_id"] for d in st.devices()), ["pi-1", "pi-2", "pi-3", "pi-4"])

    def test_no_phone_number_anywhere_in_the_cache(self):
        self.syncer().run_once()
        self.store.close()
        blob = self.db.read_bytes() + (self.db.with_name(self.db.name + "-wal").read_bytes()
                                       if self.db.with_name(self.db.name + "-wal").exists() else b"")
        self.assertNotIn(PHONE.encode(), blob)
        self.assertNotIn(b"201001234567", blob)
        self.assertNotIn(b"operator_id", blob)
        self.store = Store(self.db)

    def test_a_failing_step_is_isolated_and_scrubbed(self):
        sources.Postgres("postgresql://u:pw-very-secret@db.example.com/x")      # registers the secret
        pg = FakePG(pg_data(self.now), fail={"cameras": sources.SourceError(
            "boom at postgresql://u:pw-very-secret@db.example.com/x password pw-very-secret")})
        status = self.syncer(pg=pg).run_once()
        cams = status["sources"]["cameras"]
        self.assertFalse(cams["ok"])
        self.assertNotIn("pw-very-secret", cams["error"])
        self.assertNotIn("pw-very-secret", json.dumps(self.store.kv_get("sync")))
        self.assertTrue(status["last_error"].startswith("cameras: "))
        self.assertIsNone(status["last_ok_at"])
        others = [n for n in sync.STEPS if n != "cameras"]
        self.assertTrue(all(status["sources"][n]["ok"] for n in others))

    def test_postgres_down_fails_every_postgres_step_fast(self):
        class Down(FakePG):
            def ensure(self):
                self.ensured += 1
                raise sources.SourceError(sources.PSYCOPG2_HINT)

        pg = Down()
        status = self.syncer(pg=pg).run_once()
        pg_steps = ("starcaps", "cameras", "cal_versions", "cal_current", "items", "takes", "camera_events")
        for n in pg_steps:
            self.assertIn("sudo apt install python3-psycopg2", status["sources"][n]["error"])
        self.assertEqual(pg.ensured, 1)                                  # tried once, not seven times
        self.assertTrue(status["sources"]["redis_small"]["ok"])
        self.assertTrue(status["sources"]["device_events"]["ok"])

    def test_second_call_while_running_returns_at_once(self):
        entered, release = threading.Event(), threading.Event()
        s = self.syncer(pg=FakePG(pg_data(self.now), block=(entered, release)))
        t = threading.Thread(target=s.run_once)
        t.start()
        self.assertTrue(entered.wait(5))
        self.assertTrue(s.running)
        t0 = time.monotonic()
        again = s.run_once()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(again["skipped"])
        release.set()
        t.join(10)
        self.assertFalse(s.running)
        self.assertNotIn("skipped", self.store.kv_get("sync"))

    def test_rebuild_keeps_notes_and_never_wipes_under_a_running_pass(self):
        s = self.syncer()
        s.run_once()
        note = self.store.add_note("cap", "starcap12", "keep", status="watch")
        self.store.summary_put("2026-10-11", "keep too")
        self.store.upsert("takes", [sync.normalise_take(raw_take("stale", self.now - DAY))])
        fd = os.open(str(self.db) + ".sync.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)              # another process is mid-pass
            self.assertTrue(s.run_once(rebuild=True)["skipped"])
            self.assertIn("stale", {t["id"] for t in self.store.takes_created(0, 2e10)})   # nothing wiped
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        status = s.run_once(rebuild=True)
        self.assertIsNone(status["last_error"])
        ids = {t["id"] for t in self.store.takes_created(0, 2e10)}
        self.assertNotIn("stale", ids)
        self.assertEqual(len(ids), 79)
        self.assertEqual(self.store.notes(), [note])
        self.assertEqual(self.store.summary_get("2026-10-11")["text"], "keep too")

    def test_another_process_holding_the_lock_skips(self):
        fd = os.open(str(self.db) + ".sync.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.syncer().run_once()
            self.assertTrue(out["skipped"])
            self.assertIn("another process", out["reason"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertNotIn("skipped", self.syncer().run_once())


class Takes(Base):
    def test_backfill_then_incremental_then_full(self):
        pg = FakePG(pg_data(self.now))
        s = self.syncer(pg=pg)
        s.run_once()
        w = pg.windows()
        self.assertEqual(len(w), 23)                                     # 45 days + 1 ahead, 2-day windows
        self.assertEqual(w[0][1] - w[0][0], 2 * DAY)
        self.assertGreater(w[0][0], w[-1][0])                            # newest first
        for (a0, a1), (b0, b1) in zip(w, w[1:]):
            self.assertEqual(b1, a0)                                     # contiguous, no gaps
        self.assertAlmostEqual(w[-1][0], self.now - config.HISTORY_DAYS * DAY, delta=60)
        self.assertNotIn("takes_updated", [k for k, _ in pg.calls])
        first_cursor = self.store.kv_get("takes_cursor")
        self.assertIn("backfilled_at", first_cursor)

        pg.calls.clear()
        s.run_once()
        w = pg.windows()
        recent_days = sync.TAKES_RECENT_DAYS + 1                         # +1 day ahead (server clock)
        self.assertEqual(len(w), math.ceil(recent_days * DAY / sync.TAKES_WINDOW_S))
        self.assertAlmostEqual(w[-1][0], time.time() - sync.TAKES_RECENT_DAYS * DAY, delta=60)
        upd = [p for k, p in pg.calls if k == "takes_updated"]
        self.assertEqual(len(upd), 1)
        self.assertAlmostEqual(upd[0]["updated"], first_cursor["synced_at"] - 600, delta=1)
        self.assertAlmostEqual(upd[0]["t0"], w[-1][0], delta=1)

        pg.calls.clear()
        s.run_once(full=True)
        self.assertEqual(len(pg.windows()), 23)

    def test_updated_rows_outside_the_recent_window_are_picked_up(self):
        data = pg_data(self.now)
        pg = FakePG(data)
        s = self.syncer(pg=pg)
        s.run_once()
        old = next(r for r in data["takes"] if r["created_at"] < self.now - 10 * DAY)
        old.update(score=12.0, updated_at=time.time())
        s.run_once()
        got = {t["id"]: t for t in self.store.takes_created(0, 2e10)}
        self.assertEqual(got[old["id"]]["score"], 12.0)

    def test_window_split_on_timeout(self):
        pg = FakePG(pg_data(self.now), timeout_if=lambda p: p["t1"] - p["t0"] > DAY)
        status = self.syncer(pg=pg).run_once()
        self.assertTrue(status["sources"]["takes"]["ok"], status["sources"]["takes"]["error"])
        self.assertEqual(self.store.count("takes"), 79)
        done = [(a, b) for a, b in pg.windows() if b - a <= DAY]
        self.assertEqual(sum(b - a for a, b in done), 46 * DAY)            # still covers it all

    def test_window_that_never_fits_fails_the_step(self):
        pg = FakePG(pg_data(self.now), timeout_if=lambda p: True)
        status = self.syncer(pg=pg).run_once()
        self.assertFalse(status["sources"]["takes"]["ok"])
        self.assertIn("QueryTimeout", status["sources"]["takes"]["error"])
        self.assertTrue(status["sources"]["items"]["ok"])

    def test_take_deleted_upstream_disappears(self):
        data = pg_data(self.now)
        s = self.syncer(pg=FakePG(data))
        s.run_once()
        gone = data["takes"].pop(0)                                      # newest: inside the recent window
        s.run_once()
        self.assertNotIn(gone["id"], {t["id"] for t in self.store.takes_created(0, 2e10)})


class QcReports(Base):
    REPORT = {"report_id": "20261012-180027_80e439_c8488afffed8.json", "verdict": "fail", "station": "waleed",
              "started": 1791817227.65, "finished": 1791817896.0, "headline": "2 checks failed",
              "board": {"cpu_id": "c8488afffed82add", "name": "starcap185", "model": "FriendlyElec NanoPi R76S",
                        "camera_id": 238, "version": "1.67.0"},
              "steps": [{"key": "connect", "title": "Board answers", "state": "pass"},
                        {"key": "soak", "title": "Soak take", "state": "fail"},
                        {"key": "timing", "title": "Frame timing", "state": "fail"},
                        {"key": "cleanup", "state": "skip"}]}

    def test_a_report_becomes_a_row(self):
        row = sync.qc_run("x.json", self.REPORT)
        self.assertEqual({k: row[k] for k in ("id", "cpu_id", "name", "camera_id", "verdict", "failed_steps")},
                         {"id": "20261012-180027_80e439_c8488afffed8.json", "cpu_id": "c8488afffed82add",
                          "name": "starcap185", "camera_id": "238", "verdict": "fail",
                          "failed_steps": ["Soak take", "Frame timing"]})
        self.assertEqual((row["started_at"], row["finished_at"]), (1791817227.65, 1791817896.0))
        self.assertIsNone(sync.qc_run("y.json", {"verdict": "cancelled", "board": {}}))   # never reached a board

    def test_the_step_reads_the_folder_and_skips_junk(self):
        self.qc_dir.mkdir()
        (self.qc_dir / "a.json").write_text(json.dumps(self.REPORT))
        (self.qc_dir / "b.json").write_text("{half a report")                    # still being written
        (self.qc_dir / "c.json").write_text(json.dumps(["not", "a", "report"]))
        (self.qc_dir / "a").mkdir()                                              # its log folder
        self.assertEqual(self.syncer()._step_qc_runs(), 1)
        self.assertEqual([r["failed_steps"] for r in self.store.qc_runs()], [["Soak take", "Frame timing"]])
        self.assertEqual(self.store.kv_get("qc_source"), {"dir": str(self.qc_dir), "found": True, "reports": 1})

    def test_no_qc_on_this_laptop_is_not_an_error(self):
        self.assertEqual(self.syncer()._step_qc_runs(), 0)
        self.assertFalse(self.store.kv_get("qc_source")["found"])


class DeviceEvents(Base):
    @mock.patch.object(config, "EVENT_BACKFILL_DAYS", 3)
    def test_backfill_then_incremental(self):
        r = redis_data(self.now)
        s = self.syncer(redis=r)
        s.run_once()
        self.assertAlmostEqual(self.store.kv_get("events_since"), self.now - 3 * DAY, delta=60)
        stored = self.store.device_events(0, 2e10)
        self.assertTrue(stored)
        cutoff = self.now - config.EVENT_BACKFILL_DAYS * DAY
        self.assertTrue(all(e["ts"] >= cutoff - 1 for e in stored))
        self.assertTrue({e["type"] for e in stored} <= set(sync.EVENT_TYPES))
        self.assertNotIn("led", {e["type"] for e in stored})
        self.assertEqual({e["device_id"] for e in stored}, {"pi-1"})
        touched = r.keys_touched("ZREVRANGE")
        self.assertIn("starcap:logs:pi-1", touched)
        self.assertIn("starcap:logs:pi-4", touched)                       # active, no log yet
        self.assertNotIn("starcap:logs:pi-2", touched)                    # not heard from in 14 days
        self.assertNotIn("starcap:logs:pi-3", touched)                    # not a StarCap (vin / bench)
        # 1200 events over 5 days: 3 days is 720 events, so two 500-chunks were needed
        self.assertEqual(touched.count("starcap:logs:pi-1"), 2)
        self.assertEqual(self.store.kv_get("device_event_cursor")["seq"]["pi-1"], 1200)

        r.zsets["starcap:logs:pi-1"] += [(s_, log_member(s_, self.now, "camera_usb2")) for s_ in range(1201, 1231)]
        r.sent.clear()
        with mock.patch.object(sync, "EVENT_PAGE", 10):
            s.run_once()
        z = [c for c in r.sent if c[0] in ("ZRANGEBYSCORE", "ZREVRANGE") and c[1].startswith("starcap:logs:")]
        self.assertEqual([c[0] for c in z if c[1] == "starcap:logs:pi-1"], ["ZRANGEBYSCORE"] * 4)   # 10+10+10+0
        self.assertEqual(z[0][2], "(1200")
        self.assertEqual(len(self.store.device_events(0, 2e10, types=["camera_usb2"])), 30)
        self.assertEqual(self.store.kv_get("device_event_cursor")["seq"]["pi-1"], 1230)

    def test_a_wider_backfill_window_pulls_again_and_moves_events_since_back(self):
        r = redis_data(self.now)
        s = self.syncer(redis=r)
        with mock.patch.object(config, "EVENT_BACKFILL_DAYS", 3):
            s.run_once()
        three = self.store.device_events(0, 2e10, device_ids=["pi-1"])
        with mock.patch.object(config, "EVENT_BACKFILL_DAYS", 5):
            s.run_once()
        five = self.store.device_events(0, 2e10, device_ids=["pi-1"])
        self.assertAlmostEqual(self.store.kv_get("events_since"), self.now - 5 * DAY, delta=60)
        self.assertGreater(min(e["ts"] for e in three), self.now - 3 * DAY - 60)
        self.assertLess(min(e["ts"] for e in five), self.now - 4 * DAY)            # the older days came in
        self.assertGreater(len(five), len(three))

    def test_quiet_devices_are_skipped(self):
        r = redis_data(self.now)
        s = self.syncer(redis=r)
        s.run_once()                                                   # pi-1 and pi-4 both last heard now-2000
        fleet = r.hashes["starcap:fleet"]
        doc = json.loads(fleet["pi-4|self:pi-4"])
        doc["last_seen"] = int(self.now * 1000)                         # pi-4 keeps beating; pi-1 went quiet
        fleet["pi-4|self:pi-4"] = json.dumps(doc)
        r.sent.clear()
        s.run_once()
        self.assertIn("starcap:logs:pi-1", r.keys_touched("ZRANGEBYSCORE"))   # within slack of the last pass
        r.sent.clear()
        s.run_once()
        self.assertNotIn("starcap:logs:pi-1", r.keys_touched("ZRANGEBYSCORE"))  # quiet since: skipped
        self.assertIn("starcap:logs:pi-4", r.keys_touched("ZRANGEBYSCORE"))
        doc = json.loads(fleet["pi-1|self:pi-1"])
        doc["last_seen"] = int(time.time() * 1000) + 5000                # pi-1 is back
        fleet["pi-1|self:pi-1"] = json.dumps(doc)
        r.sent.clear()
        s.run_once()
        self.assertIn("starcap:logs:pi-1", r.keys_touched("ZRANGEBYSCORE"))

    def test_a_new_type_list_rebackfills(self):
        r = redis_data(self.now)
        s = self.syncer(redis=r)
        s.run_once()
        r.sent.clear()
        with mock.patch.object(sync, "EVENT_TYPES", sync.EVENT_TYPES + ("button",)):
            s.run_once()
            self.assertIn("starcap:logs:pi-1", r.keys_touched("ZREVRANGE"))
            self.assertTrue(self.store.device_events(0, 2e10, types=["button"]))

    def test_every_redis_command_is_a_read(self):
        r = redis_data(self.now)
        self.syncer(redis=r).run_once()
        self.assertTrue(r.sent)
        for c in r.sent:
            self.assertIn(c[0].upper(), sources.READ_COMMANDS)


class Cli(Base):
    def test_main_prints_a_table_without_secrets(self):
        self.store.close()
        pg, r = FakePG(pg_data(self.now)), redis_data(self.now)
        out = io.StringIO()
        with mock.patch.object(sources.Postgres, "from_env", return_value=pg), \
                mock.patch.object(sources.Redis, "from_env", return_value=r), redirect_stdout(out):
            code = sync.main(["--db", str(self.db)])
        self.store = Store(self.db)
        text = out.getvalue()
        self.assertEqual(code, 0)
        for name in sync.STEPS:
            self.assertIn(name, text)
        self.assertIn("upstash:", text)
        self.assertNotIn(PHONE, text)


if __name__ == "__main__":
    unittest.main()
