"""api.py on a fakestore: every endpoint returns exactly the SPEC §4 keys, input is
checked, and times come out as Cairo ISO strings, across the October DST change too.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakestore import (FakeStore, at, cal_current, cal_version, camera, camera_event, device, device_event, item, qc_run,
                       starcap, take, ticket)

import api
import clock
import config

TODAY, YDAY, PREV = "2026-10-12", "2026-10-11", "2026-10-10"
NOW = at(TODAY, 10)

STATUS_KEYS = {"now", "today", "yesterday", "sync", "config", "dashboard_url", "has_data"}
SYNC_KEYS = {"running", "next_at", "last_run_at", "last_ok_at", "last_error", "sources"}
CONFIG_KEYS = {"cal_per_day", "recal_days", "min_clip_s", "timing_pass_score", "target_fps"}
DAY_KEYS = {"date", "weekday", "is_today", "latest_version", "events_from", "events_complete", "arrival", "kpis",
            "hours", "caps", "off_trip"}
ARRIVAL_KEYS = {"caps_out", "arrived", "still_out", "waiting", "no_footage", "footage_missing", "pct"}
KPI_KEYS = {"caps_out", "caps_recorded", "caps_no_footage", "hours_total", "hours_good", "good_pct",
            "timing_pass_pct", "fps30_hours", "fps30_caps", "expired_cal_hours"}
HOURS_KEYS = {"total", "good", "fps30", "timing_fail", "untimed", "broken", "short", "pending"}
ROW_KEYS = {"cap", "cpu_id", "board", "camera", "cameras", "checkout_at", "returned_at", "task", "status", "takes",
            "clips", "short_takes", "hours", "good_pct", "timing_pass_pct", "fps", "nominal_fps", "p99_dev_ms_max",
            "boots", "version", "expired_cal_hours", "cal_days_left", "events", "flags", "severity",
            "open_tickets", "my_status", "qc"}
EVENT_KEYS = {"power_on", "unclean_boot", "camera_lost", "rec_dead", "rec_corrupt", "camera_usb2", "storage"}
FLAG_KEYS = {"code", "severity", "label", "detail"}
TAKE_KEYS = {"id", "started_at", "clock_fixed", "recorded_s", "bucket", "nominal_fps", "fps", "score", "p99_dev_ms",
             "out_of_band_pct", "drop_pct", "camera", "boot", "version", "codes", "fault", "uploaded_at"}
CAL_KEYS = {"date", "target", "today", "counts", "plan_today", "catch_when_back", "queue", "never", "broken",
            "forecast", "spikes", "clear_by", "behind_from", "history", "coverage"}
CAL_TODAY_KEYS = {"done", "recal", "new", "imu", "fits", "imu_fits"}
FIT_KEYS = {"camera", "base", "rev", "method", "bench", "calibrated_at", "first_ever", "previous_at", "rmse_left",
            "rmse_right", "rmse_stereo", "baseline_mm", "verdict", "evidence"}
COUNT_KEYS = {"active", "ok", "due_soon", "overdue", "never", "late_in_plan", "retired", "broken"}
QUEUE_KEYS = {"camera", "base", "state", "days_left", "due_on", "last_cal_at", "bench", "out_now", "on_cap",
              "hours_7d", "last_used", "plan_day", "late"}
NEVER_KEYS = {"camera", "created_at", "hours_7d", "last_used", "out_now"}
COVERAGE_KEYS = {"valid", "active", "valid_pct", "imu", "imu_pct", "imu_outdated"}
MAINT_KEYS = {"date", "stats", "by_category", "open", "recent_resolved", "needs_attention", "repeat", "blocked",
              "broken_cams", "my_status", "notes_recent", "links", "trend"}
STATS_KEYS = {"open", "critical", "opened", "resolved", "median_resolve_days", "oldest_open_days", "blocked_caps",
              "broken_cams"}
TICKET_KEYS = {"id", "subject", "unit", "headset", "camera", "category", "critical", "description", "reporter",
               "created_at", "age_days", "photos"}
NOTE_KEYS = {"id", "subject_type", "subject_id", "created_at", "text", "status"}
OUT_KEYS = {"cap", "camera", "checkout_at", "days_out", "last_heard", "silent_h", "operator", "task"}
STATION_KEYS = {"id", "name", "last_seen", "age_s", "state", "message", "staging_free_gb", "pending_review", "error",
                "waiting_caps", "status"}
UPLOADER_KEYS = {"id", "name", "last_seen", "age_s", "state", "level", "message", "runs", "current", "error",
                 "stuck", "stuck_min", "status"}
TREND_DAY_KEYS = {"date", "caps_out", "caps_recorded", "hours_total", "hours_good", "timing_pass_pct", "fps30_hours",
                  "expired_cal_hours", "cal_fits", "open_tickets", "overdue_cameras", "partial"}
CAP_KEYS = {"cap", "cpu_id", "board", "version", "online", "last_heard", "out_now", "camera_now", "my_status",
            "qc", "qc_runs", "totals_30d", "days", "tickets", "camera_history", "notes"}
CAMERA_KEYS = {"camera", "base", "rev", "state", "days_left", "due_on", "last_cal_at", "retired", "broken", "fits",
               "imu", "usage", "cap_history", "tickets", "notes"}


def world():
    """Three caps out yesterday: starcap1 recorded (some of it at 30 fps), starcap2 is
    still out, starcap3 came back with nothing although it logged a rec_start."""
    kv = {
        "events_since": at("2026-10-01"), "qc_source": {"dir": "/qc", "found": True, "reports": 3},
        "maint_blocked": ["starcap2"], "maint_broken_cam": ["100"], "cameras_retired": ["9"],
        "cameras_revs": ["88.1"],
        "kamerat_out": {"starcap2": {"operator_name": "Mona", "checked_out_at": at(YDAY, 8.5), "camera": "50",
                                     "destination": "Giza"}},
        "stations": [{"id": "opm-1", "station_name": "copy station", "version": "1", "last_seen": NOW - 30,
                      "activity": {"state": "idle", "message": ""},
                      "drives": [{"role": "staging", "bytes_free": 5e11, "bytes_total": 1e12}],
                      "pending_review": 0, "error": None}],
        "uploaders": [{"id": "ssd-1", "name": "uploader A", "version": "1", "last_seen": NOW - 20,
                       "detection": {"state": "ready", "level": "ok", "message": ""}, "error": None,
                       "current": {"run": "r1", "sent": 10, "total": 100, "rate_bps": 2e7},
                       "runs": {"pending": 1, "uploading": 1, "done": 9, "error": 0}, "logs": []}],
        "uploader_progress": {"ssd-1": {"run": "r1", "sent": 10, "changed_at": NOW - 60}},
        "label_tick": {"at": NOW - 600, "ok": True, "last_ok_at": NOW - 600, "waiting": 3, "label": {"waiting": 3}},
        "sync": {"last_run_at": NOW - 120, "last_ok_at": NOW - 120, "last_error": None,
                 "sources": {"takes": {"ok": True, "at": NOW - 120, "rows": 5, "took_s": 0.4, "error": None}}},
    }
    return FakeStore(
        starcaps=[starcap(cpu_id="cpu1", name="starcap1", camera_id="43"),
                  starcap(cpu_id="cpu2", name="starcap2", camera_id="50"),
                  starcap(cpu_id="cpu3", name="starcap3", camera_id="88")],
        cameras=[camera(id=c, created_at=at("2026-07-01")) for c in ("43", "50", "88", "9", "100")],
        cal_versions=[cal_version(camera_key="43", calibrated_at=at("2026-10-01", 10)),
                      cal_version(camera_key="43", calibrated_at=at("2026-09-01", 10), method="opencv_fisheye_kb4"),
                      cal_version(camera_key="50", calibrated_at=at("2026-09-01", 10)),
                      cal_version(camera_key="88", calibrated_at=at("2026-08-01", 10)),
                      cal_version(camera_key="100", calibrated_at=at("2026-10-05", 10)),
                      cal_version(camera_key="9", calibrated_at=at(TODAY, 9))],
        cal_current=[cal_current(camera_id=c, kind="stereo", calibrated_at=t) for c, t in
                     (("43", at("2026-10-01", 10)), ("50", at("2026-09-01", 10)), ("88", at("2026-08-01", 10)),
                      ("100", at("2026-10-05", 10)), ("9", at(TODAY, 9)))]
                    + [cal_current(camera_id="43", kind="imu", calibrated_at=at("2026-10-02", 10))],
        items=[item(id="i1", cpu_id="cpu1", camera_id="43", checkout_at=at(YDAY, 8), returned_at=at(YDAY, 18)),
               item(id="i2", cpu_id="cpu2", camera_id="50", checkout_at=at(YDAY, 8.5), returned_at=None,
                    task="Repair Services"),
               item(id="i3", cpu_id="cpu3", camera_id="88", checkout_at=at(YDAY, 9), returned_at=at(YDAY, 19)),
               item(id="i0", cpu_id="cpu1", camera_id="43", checkout_at=at(PREV, 8), returned_at=at(PREV, 18))],
        takes=[take(id="t1", cpu_id="cpu1", trip_item_id="i1", camera_unit="43", started_at=at(YDAY, 9),
                    created_at=at(YDAY, 20), recorded_s=3600, boot_id="f361aaaa", uploaded_at=at(YDAY, 23)),
               take(id="t2", cpu_id="cpu1", trip_item_id="i1", camera_unit="43", started_at=at(YDAY, 10),
                    created_at=at(YDAY, 20), recorded_s=1800, nominal_fps=30, boot_id="f361aaaa"),
               take(id="t3", cpu_id="cpu1", trip_item_id="i1", camera_unit="43", started_at=at(YDAY, 11),
                    created_at=at(YDAY, 20), recorded_s=20, boot_id="b0b0"),
               take(id="t0", cpu_id="cpu1", trip_item_id="i0", camera_unit="43", started_at=at(PREV, 9),
                    created_at=at(PREV, 20), recorded_s=7200, uploaded_at=at(PREV, 23)),
               take(id="tx", cpu_id="cpu2", trip_item_id=None, created_at=at(YDAY, 12), recorded_s=600)],
        devices=[device(device_id="d1", cpu_id="cpu1", last_seen=NOW - 60, software_version="1.67.0"),
                 device(device_id="d2", cpu_id="cpu2", last_seen=at(YDAY, 7)),
                 device(device_id="d3", cpu_id="cpu3", last_seen=at(YDAY, 20))],
        device_events=[device_event(device_id="d3", ts=at(YDAY, 10), type="rec_start", msg="rec"),
                       device_event(device_id="d1", ts=at(YDAY, 8.2), type="power_on"),
                       device_event(device_id="d1", ts=at(YDAY, 9), type="rec_start"),
                       device_event(device_id="d1", ts=at(YDAY, 9.5), type="camera_lost"),
                       device_event(device_id="d1", ts=at(YDAY, 20), type="camera_lost")],
        tickets=[ticket(id="mt1", headset="starcap2", critical=True, created_at=at("2026-10-05")),
                 ticket(id="mt2", headset="starcap1", status="resolved", created_at=at("2026-10-01", 12),
                        resolved_at=at(YDAY, 12), resolution_note="new cable"),
                 ticket(id="mt3", subject="camera", camera="100", headset="starcap3", created_at=at("2026-10-06"))],
        camera_events=[camera_event(at=at("2026-10-01"), camera="43", headset="starcap1", replaced="12")],
        # starcap2: failed QC, then passed; starcap3: only a cancelled run (QC saw it, so it is a NanoPi)
        qc_runs=[qc_run(cpu_id="cpu2", name="starcap2", verdict="fail", finished_at=at("2026-10-08", 15),
                        failed_steps=["Camera"], headline="1 check failed"),
                 qc_run(cpu_id="cpu2", name="starcap2", verdict="pass", finished_at=at("2026-10-09", 15)),
                 qc_run(cpu_id="cpu3", name="starcap3", verdict="cancelled", finished_at=at("2026-10-09", 16))],
        kv=kv)


class Api(unittest.TestCase):
    """Runs each test at 10:00 Cairo on 2026-10-12."""

    now = NOW

    def setUp(self):
        self.store = world()
        patch = mock.patch.object(clock, "now", return_value=self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def keys(self, d, expected):
        self.assertEqual(set(d), expected)


class Keys(Api):
    def test_status(self):
        s = api.status(self.store, {"running": False, "next_at": NOW + 600})
        self.keys(s, STATUS_KEYS)
        self.keys(s["sync"], SYNC_KEYS)
        self.keys(s["config"], CONFIG_KEYS)
        self.assertEqual((s["now"], s["today"], s["yesterday"]), (clock.iso(NOW), TODAY, YDAY))
        self.assertEqual(s["sync"]["next_at"], clock.iso(NOW + 600))
        self.assertEqual(s["sync"]["sources"]["takes"]["at"], clock.iso(NOW - 120))
        self.assertTrue(s["has_data"])
        self.assertFalse(api.status(FakeStore(), {"running": True, "next_at": None})["has_data"])

    def test_day(self):
        d = api.day(self.store)
        self.keys(d, DAY_KEYS)
        self.keys(d["arrival"], ARRIVAL_KEYS)
        self.keys(d["kpis"], KPI_KEYS)
        for kpi in d["kpis"].values():
            self.keys(kpi, {"value", "prev"})
        self.keys(d["hours"], HOURS_KEYS)
        self.keys(d["off_trip"], {"takes", "hours", "caps"})
        for row in d["caps"]:
            self.keys(row, ROW_KEYS)
            self.keys(row["hours"], HOURS_KEYS)
            self.keys(row["events"], EVENT_KEYS)
            for f in row["flags"]:
                self.keys(f, FLAG_KEYS)
        self.assertEqual((d["date"], d["weekday"], d["is_today"]), (YDAY, "Sun", False))

    def test_day_cap(self):
        d = api.day_cap(self.store, YDAY, "starcap1")
        self.keys(d, {"date", "cap", "row", "takes", "events"})
        self.keys(d["row"], ROW_KEYS)
        for t in d["takes"]:
            self.keys(t, TAKE_KEYS)
        for e in d["events"]:
            self.keys(e, {"at", "type", "msg"})

    def test_calibration(self):
        c = api.calibration(self.store)
        self.keys(c, CAL_KEYS)
        self.keys(c["today"], CAL_TODAY_KEYS)
        for f in c["today"]["fits"]:
            self.keys(f, FIT_KEYS)
        self.keys(c["counts"], COUNT_KEYS)
        for q in c["queue"] + c["plan_today"]:
            self.keys(q, QUEUE_KEYS)
        for n in c["never"]:
            self.keys(n, NEVER_KEYS)
        for f in c["forecast"]:
            self.keys(f, {"date", "due", "planned", "overdue_end"})
        for h in c["history"]:
            self.keys(h, {"date", "recal", "new", "imu"})
        self.keys(c["coverage"], COVERAGE_KEYS)

    def test_maintenance(self):
        api.add_note(self.store, {"subject_type": "cap", "subject_id": "starcap3", "text": "card?", "status": "watch"})
        m = api.maintenance(self.store)
        self.keys(m, MAINT_KEYS)
        self.keys(m["stats"], STATS_KEYS)
        for t in m["open"]:
            self.keys(t, TICKET_KEYS)
        for t in m["recent_resolved"]:
            self.keys(t, TICKET_KEYS | {"resolved_at", "resolution_note"})
        for c in m["by_category"]:
            self.keys(c, {"category", "open", "last_30d"})
        for e in m["needs_attention"]:
            self.keys(e, {"cap", "camera", "severity", "flags", "days"})
        for s in m["my_status"]:
            self.keys(s, {"subject_type", "subject_id", "status", "since", "text"})
        for n in m["notes_recent"]:
            self.keys(n, NOTE_KEYS)
        self.keys(m["links"], {"tickets", "file"})
        for t in m["trend"]:
            self.keys(t, {"date", "open", "opened", "resolved"})
        self.assertTrue(m["open"] and m["recent_resolved"] and m["needs_attention"] and m["my_status"])

    def test_field(self):
        f = api.field(self.store)
        self.keys(f, {"fleet", "long_out", "silent", "out", "qc"})
        self.keys(f["fleet"], {"total", "out", "office", "online", "blocked"})
        self.keys(f["qc"], {"available", "nanopi", "passed", "failed", "never", "not_passed"})
        for e in f["qc"]["not_passed"]:
            self.keys(e, {"cap", "status", "at", "failed_steps", "out_now", "runs"})
        for e in f["out"] + f["silent"] + f["long_out"]:
            self.keys(e, OUT_KEYS)

    def test_pipeline(self):
        p = api.pipeline(self.store)
        self.keys(p, {"stations", "uploaders", "backlog", "lag", "label"})
        self.keys(p["stations"][0], STATION_KEYS)
        u = p["uploaders"][0]
        self.keys(u, UPLOADER_KEYS)
        self.keys(u["runs"], {"pending", "uploading", "done", "error"})
        self.keys(u["current"], {"run", "sent", "total", "pct", "rate_mbps"})
        self.keys(p["backlog"], {"takes", "hours", "oldest_h", "over_24h"})
        self.keys(p["lag"], {"checkout_to_cloud_h", "copy_to_cloud_h"})
        for spread in p["lag"].values():
            self.keys(spread, {"p50", "p90", "n"})
        self.keys(p["label"], {"at", "age_min", "ok", "waiting"})

    def test_trends(self):
        t = api.trends(self.store, 3)
        self.keys(t, {"days"})
        for d in t["days"]:
            self.keys(d, TREND_DAY_KEYS)

    def test_cap(self):
        c = api.cap(self.store, "starcap1")
        self.keys(c, CAP_KEYS)
        self.keys(c["totals_30d"], {"days_out", "hours", "good", "no_footage_days"})
        for d in c["days"]:
            self.keys(d, {"date", "status", "camera", "hours", "takes", "flags"})
        for e in c["camera_history"]:
            self.keys(e, {"at", "action", "camera", "replaced"})
        self.keys(api.cap(self.store, "starcap2")["out_now"], {"checkout_at", "days_out", "camera"})

    def test_camera(self):
        c = api.camera(self.store, "43")
        self.keys(c, CAMERA_KEYS)
        for f in c["fits"]:
            self.keys(f, FIT_KEYS)
        self.keys(c["imu"], {"calibrated_at", "outdated"})
        for u in c["usage"]:
            self.keys(u, {"date", "hours", "caps"})

    def test_notes(self):
        note = api.add_note(self.store, {"subject_type": "camera", "subject_id": "43", "text": " smudge "})
        self.keys(note, NOTE_KEYS)
        self.keys(api.notes(self.store), {"notes"})
        self.keys(api.notes(self.store, "camera", "43")["notes"][0], NOTE_KEYS)
        self.assertEqual(api.delete_note(self.store, note["id"]), {"ok": True})

    def test_summary_and_units(self):
        self.keys(api.summary(self.store), {"date", "text", "written_at"})
        self.keys(api.units(self.store), {"caps", "cameras", "boards"})


class Values(Api):
    def test_day_rows_and_sorting(self):
        d = api.day(self.store, YDAY)
        self.assertEqual([(r["cap"], r["status"], r["severity"]) for r in d["caps"]],
                         [("starcap1", "ok", "bad"), ("starcap3", "footage_missing", "bad"), ("starcap2", "out", None)])
        one = d["caps"][0]
        self.assertEqual([f["code"] for f in one["flags"]], ["fps30"])
        self.assertEqual((one["takes"], one["clips"], one["short_takes"], one["boots"]), (3, 2, 1, 2))
        self.assertEqual(one["hours"]["total"], round(5420 / 3600, 2))
        self.assertEqual(one["events"]["camera_lost"], 1)                 # the 20:00 one is after the return
        self.assertEqual((one["checkout_at"], one["returned_at"]), (clock.iso(at(YDAY, 8)), clock.iso(at(YDAY, 18))))
        self.assertEqual(d["arrival"], {"caps_out": 3, "arrived": 1, "still_out": 1, "waiting": 0, "no_footage": 0,
                                        "footage_missing": 1, "pct": 33})
        self.assertEqual(d["kpis"]["caps_out"], {"value": 3, "prev": 1})
        self.assertEqual(d["kpis"]["hours_total"], {"value": 1.51, "prev": 2.0})
        self.assertEqual(d["kpis"]["fps30_caps"]["value"], 1)
        self.assertEqual(d["off_trip"], {"takes": 1, "hours": 0.17, "caps": 1})

    def test_day_cap_takes_and_log(self):
        d = api.day_cap(self.store, YDAY, "starcap1")
        self.assertEqual([t["id"] for t in d["takes"]], ["t1", "t2", "t3"])
        self.assertEqual([t["bucket"] for t in d["takes"]], ["good", "fps30", "short"])
        self.assertEqual((d["takes"][0]["boot"], d["takes"][0]["camera"]), ("f361", "43"))
        self.assertEqual([e["type"] for e in d["events"]], ["power_on", "rec_start", "camera_lost"])

    def test_calibration_values(self):
        c = api.calibration(self.store)
        self.assertEqual(c["counts"], {"active": 4, "ok": 1, "due_soon": 0, "overdue": 1, "never": 1,
                                       "late_in_plan": 1, "retired": 1, "broken": 1})
        self.assertEqual([(q["camera"], q["out_now"], q["on_cap"], q["late"]) for q in c["queue"]],
                         [("50", True, "starcap2", True)])
        self.assertEqual(([n["camera"] for n in c["never"]], c["broken"]), (["88.1"], ["100"]))
        self.assertEqual((c["today"]["done"], c["today"]["new"]), (1, 1))
        self.assertEqual(c["coverage"]["imu"], 1)

    def test_maintenance_values(self):
        m = api.maintenance(self.store)
        self.assertEqual([t["id"] for t in m["open"]], ["mt1", "mt3"])
        self.assertEqual((m["stats"]["resolved"], m["stats"]["median_resolve_days"]), (1, 10.0))
        self.assertEqual([e["cap"] for e in m["needs_attention"]], ["starcap1", "starcap3"])
        api.add_note(self.store, {"subject_type": "cap", "subject_id": "starcap3", "status": "repair"})
        self.assertEqual([e["cap"] for e in api.maintenance(self.store)["needs_attention"]], ["starcap1"])

    def test_field_values(self):
        f = api.field(self.store)
        self.assertEqual(f["fleet"], {"total": 3, "out": 1, "office": 2, "online": 1, "blocked": 1})
        out = f["out"][0]
        self.assertEqual((out["cap"], out["operator"], out["task"], out["silent_h"]),
                         ("starcap2", "Mona", "Repair Services", 25.5))
        self.assertEqual([e["cap"] for e in f["silent"]], ["starcap2"])

    def test_qc_status_is_for_nanopis_only(self):
        rows = {r["cap"]: r["qc"] for r in api.day(self.store, YDAY)["caps"]}
        self.assertIsNone(rows["starcap1"])                                    # an Orange Pi: not QC'd by design
        self.assertEqual((rows["starcap2"]["status"], rows["starcap2"]["runs"]), ("pass", 2))
        self.assertEqual(rows["starcap3"]["status"], "never")                  # cancelled runs don't count
        c = api.cap(self.store, "starcap2")
        self.assertEqual([(r["verdict"], r["failed_steps"]) for r in c["qc_runs"]], [("pass", []), ("fail", ["Camera"])])
        f = api.field(self.store)["qc"]
        self.assertEqual((f["available"], f["nanopi"], f["passed"], f["failed"], f["never"]), (True, 2, 1, 0, 1))
        self.assertEqual([e["cap"] for e in f["not_passed"]], ["starcap3"])

    def test_pipeline_values(self):
        p = api.pipeline(self.store)
        self.assertEqual((p["stations"][0]["status"], p["uploaders"][0]["status"]), ("ok", "ok"))
        self.assertEqual(p["backlog"]["takes"], 3)
        self.assertEqual(p["lag"]["checkout_to_cloud_h"], {"p50": 15.0, "p90": 15.0, "n": 2})   # out 08:00, up 23:00
        self.assertEqual(p["label"]["age_min"], 10)

    def _synced(self, read_at, recorded=True):
        """The last sync read Redis at `read_at`; every heartbeat it saw was 30-60 s old.
        recorded=False: a cache from before sync stamped `heartbeats_at`."""
        if recorded:
            self.store.kv_set("heartbeats_at", read_at)
        self.store.kv_set("uploaders", [dict(self.store.kv_get("uploaders")[0], last_seen=read_at - 30)])
        self.store.kv_set("uploader_progress", {"ssd-1": {"run": "r1", "sent": 10, "changed_at": read_at}})
        self.store._devices[0]["last_seen"] = read_at - 60

    def test_heartbeats_are_aged_when_sync_read_them(self):
        self._synced(NOW - 8 * 60)                                      # next sync is 2 min away
        u = api.pipeline(self.store)["uploaders"][0]
        self.assertEqual((u["age_s"], u["status"], u["stuck_min"]), (30, "ok", 0))
        self.assertEqual(api.field(self.store)["fleet"]["online"], 1)
        self.assertTrue(api.cap(self.store, "starcap1")["online"])

    def test_without_a_read_time_heartbeats_age_from_the_wall_clock(self):
        self._synced(NOW - 8 * 60, recorded=False)
        u = api.pipeline(self.store)["uploaders"][0]
        self.assertEqual((u["age_s"], u["status"]), (8 * 60 + 30, "warn"))
        self.assertEqual(api.field(self.store)["fleet"]["online"], 0)

    def test_trends_values(self):
        days = api.trends(self.store, 3)["days"]
        self.assertEqual([(d["date"], d["caps_out"], d["partial"]) for d in days],
                         [(PREV, 1, False), (YDAY, 3, False), (TODAY, 0, True)])
        self.assertEqual(days[-1]["cal_fits"], 1)
        self.assertEqual(days[-1]["open_tickets"], 2)
        self.assertEqual(days[-1]["overdue_cameras"], 1)

    def test_cap_values(self):
        c = api.cap(self.store, "starcap1")
        self.assertEqual([d["date"] for d in c["days"]], [YDAY, PREV])
        self.assertEqual(c["totals_30d"], {"days_out": 2, "hours": 3.51, "good": 3.0, "no_footage_days": 0})
        self.assertEqual((c["online"], c["version"], c["camera_now"], c["out_now"]), (True, "1.67.0", "43", None))
        self.assertEqual([t["id"] for t in c["tickets"]], ["mt2"])
        self.assertEqual(c["camera_history"], [{"at": clock.iso(at("2026-10-01")), "action": "camera.attached",
                                                "camera": "43", "replaced": "12"}])

    def test_camera_values(self):
        c = api.camera(self.store, "43")
        self.assertEqual((c["camera"], c["state"], len(c["fits"]), c["imu"]["outdated"]), ("43", "ok", 2, False))
        self.assertEqual([(u["date"], u["caps"]) for u in c["usage"]], [(YDAY, ["starcap1"]), (PREV, ["starcap1"])])
        self.assertEqual(c["cap_history"][0]["cap"], "starcap1")
        relensed = api.camera(self.store, "88")
        self.assertEqual((relensed["camera"], relensed["state"], relensed["rev"]), ("88.1", "never", 1))
        self.assertEqual(api.camera(self.store, "88")["fits"][0]["camera"], "88")

    def test_units(self):
        self.assertEqual(api.units(self.store), {"caps": ["starcap1", "starcap2", "starcap3"],
                                                 "cameras": ["9", "43", "50", "88.1", "100"],
                                                 "boards": {"starcap1": "Orange Pi", "starcap2": "NanoPi",
                                                            "starcap3": "NanoPi"}})

    def test_my_status_on_the_day_row(self):
        api.add_note(self.store, {"subject_type": "cap", "subject_id": "starcap1", "status": "Watch"})
        self.assertEqual(api.day(self.store)["caps"][0]["my_status"], "watch")


class Validation(Api):
    def test_dates(self):
        for bad in ("2026-13-01", "yesterday", "2026-10-1", 20261011):
            with self.assertRaises(api.BadRequest, msg=bad):
                api.day(self.store, bad)
        with self.assertRaises(api.BadRequest):
            api.maintenance(self.store, "2026-10-13")                  # tomorrow
        self.assertEqual(api.day(self.store, TODAY)["is_today"], True)
        with self.assertRaises(api.BadRequest):
            api.day_cap(self.store, None, "starcap1")

    def test_unknown_units(self):
        with self.assertRaises(api.NotFound):
            api.cap(self.store, "starcap99")
        with self.assertRaises(api.NotFound):
            api.camera(self.store, "999")
        with self.assertRaises(api.NotFound):
            api.day_cap(self.store, YDAY, "starcap99")
        with self.assertRaises(api.NotFound):
            api.day_cap(self.store, PREV, "starcap2")                  # not out that day

    def test_notes(self):
        ok = {"subject_type": "cap", "subject_id": "starcap1", "text": "fine"}
        for bad in ({**ok, "subject_type": "board"}, {**ok, "status": "broken"},
                    {**ok, "text": "x" * (api.NOTE_MAX_CHARS + 1)}, {**ok, "text": "  "}, {**ok, "text": 5}):
            with self.assertRaises(api.BadRequest, msg=bad):
                api.add_note(self.store, bad)
        with self.assertRaises(api.NotFound):
            api.add_note(self.store, {**ok, "subject_id": "starcap99"})
        with self.assertRaises(api.NotFound):
            api.add_note(self.store, {"subject_type": "camera", "subject_id": "999", "text": "?"})
        with self.assertRaises(api.BadRequest):
            api.notes(self.store, "board")
        with self.assertRaises(api.BadRequest):
            api.notes(self.store, None, "starcap1")
        with self.assertRaises(api.NotFound):
            api.delete_note(self.store, 404)
        with self.assertRaises(api.BadRequest):
            api.delete_note(self.store, "abc")

    def test_trend_days(self):
        for bad in (0, config.HISTORY_DAYS + 1, "30", True):
            with self.assertRaises(api.BadRequest, msg=bad):
                api.trends(self.store, bad)

    def test_summary_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slack messages.md"
            with mock.patch.object(config, "SLACK_FILE", path):
                res = api.write_summary(self.store, {})
                self.assertEqual(set(res), {"ok", "path", "written_at"})
                self.assertEqual(res["written_at"], clock.iso(NOW))
                with self.assertRaises(api.Conflict):
                    api.write_summary(self.store, {"date": YDAY})
                api.write_summary(self.store, {"date": YDAY, "force": True})
                self.assertEqual(path.read_text().count("**starcap daily, sun 11 oct**"), 2)
                saved = api.summary(self.store, YDAY)
                self.assertEqual(saved["written_at"], clock.iso(NOW))
                self.assertTrue(saved["text"].startswith("**starcap daily, sun 11 oct**"))


class Dst(unittest.TestCase):
    """Egypt leaves summer time at the end of Thursday 2026-10-29: 24:00 +03:00 becomes
    23:00 +02:00, so that Cairo day is 25 hours long."""

    def setUp(self):
        thu, fri = "2026-10-29", "2026-10-30"
        self.thu = thu
        self.morning = at(thu, 9)               # 09:00 +03:00
        self.late = at(thu, 24.5)               # the second 23:30, +02:00
        self.after = at(fri, 0.5)               # 00:30 +02:00 Friday
        self.store = FakeStore(
            starcaps=[starcap(cpu_id=f"c{i}", name=f"starcap{i}") for i in (1, 2, 3)],
            items=[item(id="a", cpu_id="c1", checkout_at=self.morning, returned_at=self.morning + 3600),
                   item(id="b", cpu_id="c2", checkout_at=self.late, returned_at=self.late + 600),
                   item(id="c", cpu_id="c3", checkout_at=self.after, returned_at=None)],
            takes=[take(cpu_id="c2", trip_item_id="b", created_at=self.after + 3600)])
        patch = mock.patch.object(clock, "now", return_value=at(fri, 12))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_long_thursday_keeps_its_late_checkout(self):
        d = api.day(self.store)                                         # default: yesterday = Thursday
        self.assertEqual(d["date"], self.thu)
        times = {r["cap"]: r["checkout_at"] for r in d["caps"]}
        self.assertEqual(times, {"starcap1": clock.iso(self.morning), "starcap2": clock.iso(self.late)})
        self.assertTrue(times["starcap1"].endswith("+03:00"))
        self.assertTrue(times["starcap2"].startswith("2026-10-29T23:30") and times["starcap2"].endswith("+02:00"))
        self.assertEqual(d["hours"]["total"], round(600 / 3600, 2))

    def test_status_and_trends_across_the_change(self):
        s = api.status(self.store, {"running": False, "next_at": None})
        self.assertEqual((s["today"], s["yesterday"]), ("2026-10-30", self.thu))
        self.assertTrue(s["now"].endswith("+02:00"))
        days = api.trends(self.store, 3)["days"]
        self.assertEqual([(d["date"], d["caps_out"]) for d in days],
                         [("2026-10-28", 0), (self.thu, 2), ("2026-10-30", 1)])

    def test_field_counts_elapsed_hours(self):
        out = api.field(self.store)["out"][0]
        self.assertEqual((out["cap"], out["silent_h"]), ("starcap3", 11.5))


if __name__ == "__main__":
    unittest.main()
