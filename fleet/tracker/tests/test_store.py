"""Offline tests for store.py: the SPEC §2 columns every loader returns, bool and JSON
decoding, the window loaders, notes (status semantics, soft delete), summaries, kv,
clear_cache, prune and the schema version.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import store as store_mod  # noqa: E402
from store import Store  # noqa: E402

# SPEC §2, written out by hand on purpose: if store.py drifts from the spec, this fails.
SPEC = {
    "takes": "id cpu_id trip_item_id trip_id created_at updated_at first_copied_at uploaded_at started_at "
             "clock_fixed boot_id software_version board camera_unit usb_path recorded_s kept_s settled "
             "readiness_status codes fault_kind nominal_fps fps p99_dev_ms out_of_band_pct score drop_pct sheet_path "
             "sheet_grid",
    "items": "id trip_id cpu_id camera_id checkout_at returned_at missing task destination",
    "starcaps": "cpu_id name camera_id board created_at",
    "cameras": "id status retired created_at",
    "cal_versions": "id camera_id camera_key rev method calibrated_at uploaded_at superseded_at verdict "
                    "evidence_status rmse_left rmse_right rmse_stereo baseline_mm",
    "cal_current": "id camera_id kind rev calibrated_at method evidence_status verdict fit_digest",
    "camera_events": "id at action camera headset replaced source",
    "tickets": "id subject headset camera category critical description reporter_name status created_at "
               "resolved_at resolution_note replacement_camera photos",
    "devices": "device_id cpu_id name last_seen software_version board status recording disk_free disk_total "
               "connection_type camera clock_synced",
    "device_events": "device_id seq ts type msg",
}
SPEC = {k: v.split() for k, v in SPEC.items()}
NOTE_KEYS = ["id", "subject_type", "subject_id", "created_at", "text", "status"]

DAY = 86400.0
T0 = 1791700000.0            # a fixed "now" for the fixtures (2026-10-11, Cairo)


def take(id_, **kw):
    row = {c: None for c in SPEC["takes"]}
    row.update({"id": id_, "cpu_id": "cpuA", "created_at": T0, "updated_at": T0, "recorded_s": 600.0,
                "clock_fixed": False, "settled": True, "codes": ["no_chunks"], "nominal_fps": 60, "fps": 60.1})
    row.update(kw)
    return row


def item(id_, **kw):
    row = {"id": id_, "trip_id": "co_" + id_, "cpu_id": "cpuA", "camera_id": "43", "checkout_at": T0 - 3600,
           "returned_at": T0 + 3600, "missing": False, "task": "Repair Services", "destination": "Cairo"}
    row.update(kw)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "tracker.db"
        self.s = Store(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def fill(self):
        s = self.s
        s.replace_all("starcaps", [{"cpu_id": "cpuA", "name": "starcap12", "camera_id": "43", "board": None,
                                    "created_at": T0 - 90 * DAY}])
        s.replace_all("cameras", [{"id": "43", "status": None, "retired": False, "created_at": T0 - 99 * DAY},
                                  {"id": "77", "status": None, "retired": True, "created_at": T0 - 99 * DAY}])
        s.replace_all("cal_versions", [{"id": "calv_43.1_ab", "camera_id": "43", "camera_key": "43.1", "rev": 1,
                                        "method": "basalt_kb4", "calibrated_at": T0 - 5 * DAY,
                                        "uploaded_at": T0 - 5 * DAY, "superseded_at": None, "verdict": "PASS",
                                        "evidence_status": "complete", "rmse_left": 0.26, "rmse_right": 0.23,
                                        "rmse_stereo": 0.31, "baseline_mm": 60.07}])
        s.replace_all("cal_current", [{"id": "cal_43r1", "camera_id": "43", "kind": "stereo", "rev": 1,
                                       "calibrated_at": T0 - 5 * DAY, "method": "basalt_kb4",
                                       "evidence_status": "complete", "verdict": "PASS", "fit_digest": "ab"}])
        s.replace_all("camera_events", [{"id": "ev1", "at": T0, "action": "camera.attached", "camera": "43",
                                         "headset": "starcap12", "replaced": "16", "source": "device"}])
        s.replace_all("tickets", [{"id": "mt_1", "subject": "starcap", "headset": "starcap12", "camera": "43",
                                   "category": "hardware", "critical": True, "description": "x",
                                   "reporter_name": "w", "status": "open", "created_at": T0, "resolved_at": None,
                                   "resolution_note": None, "replacement_camera": None, "photos": 2}])
        s.replace_all("devices", [{"device_id": "pi-1", "cpu_id": "cpuA", "name": "starcap12", "last_seen": T0,
                                   "software_version": "1.67.0", "board": "Orange Pi 5", "status": "idle",
                                   "recording": False, "disk_free": 10, "disk_total": 20,
                                   "connection_type": "wifi", "camera": "43", "clock_synced": None}])
        s.upsert("device_events", [{"device_id": "pi-1", "seq": 5, "ts": T0, "type": "power_on", "msg": "boot"},
                                   {"device_id": "pi-1", "seq": 6, "ts": T0 + 10, "type": "rec_start", "msg": ""},
                                   {"device_id": "pi-2", "seq": 1, "ts": T0 + 20, "type": "camera_lost", "msg": ""}])
        s.upsert("items", [item("i1"), item("i2", checkout_at=T0 - 2 * DAY, camera_id="16"),
                           item("open1", returned_at=None, checkout_at=T0 - 30 * DAY)])
        s.upsert("takes", [take("t1", trip_item_id="i1", camera_unit="43.1"),
                           take("t2", trip_item_id="i2", created_at=T0 + 60, camera_unit=None, settled=None),
                           take("t3", trip_item_id=None, created_at=T0 + 120, camera_unit="43", codes=[])])


class LoaderShapes(Base):
    def test_every_loader_returns_exactly_the_spec_columns(self):
        self.fill()
        s, w = self.s, (T0 - 100 * DAY, T0 + 100 * DAY)
        cases = {
            "starcaps": s.starcaps(), "cameras": s.cameras(), "cal_versions": s.cal_versions(),
            "cal_current": s.cal_current(), "tickets": s.tickets(), "devices": s.devices(),
            "items": s.items(*w) + s.open_items(), "camera_events": s.camera_events(*w),
            "takes": s.takes_created(*w) + s.takes_off_trip(*w) + s.takes_for_camera(["43.1", "43"], *w),
            "device_events": s.device_events(*w),
        }
        for table, rows in cases.items():
            self.assertTrue(rows, table)
            for r in rows:
                self.assertEqual(list(r), SPEC[table], table)
        for r in s.field_takes(*w):
            self.assertEqual(list(r), SPEC["takes"] + ["checkout_at", "returned_at", "item_camera_id"])

    def test_store_columns_match_spec(self):
        for table, cols in SPEC.items():
            self.assertEqual([c for c, _ in store_mod.TABLES[table]], cols, table)

    def test_bools_are_bools_and_json_is_decoded(self):
        self.fill()
        t1 = {t["id"]: t for t in self.s.takes_created(0, 2e10)}
        self.assertIs(t1["t1"]["clock_fixed"], False)
        self.assertIs(t1["t1"]["settled"], True)
        self.assertIsNone(t1["t2"]["settled"])          # production said nothing: stays None
        self.assertEqual(t1["t1"]["codes"], ["no_chunks"])
        self.assertEqual(t1["t3"]["codes"], [])
        self.assertIsInstance(t1["t1"]["nominal_fps"], int)
        self.assertIs(self.s.cameras()[1]["retired"], True)
        self.assertIs(self.s.tickets()[0]["critical"], True)
        self.assertIs(self.s.items(0, 2e10)[0]["missing"], False)
        d = self.s.devices()[0]
        self.assertIs(d["recording"], False)
        self.assertIsNone(d["clock_synced"])

    def test_codes_null_comes_back_as_empty_list(self):
        self.s.upsert("takes", [take("x", codes=None)])
        self.assertEqual(self.s.takes_created(0, 2e10)[0]["codes"], [])


class Windows(Base):
    def test_items_half_open_and_open_items(self):
        self.fill()
        self.assertEqual([i["id"] for i in self.s.items(T0 - 3600, T0)], ["i1"])
        self.assertEqual(self.s.items(T0 - 3599, T0), [])                 # [t0, t1): start excluded
        self.assertEqual([i["id"] for i in self.s.open_items()], ["open1"])

    def test_field_takes_count_on_the_checkout_day(self):
        self.fill()
        # t2 was created at T0+60 but its item went out two days earlier: it belongs there.
        day = self.s.field_takes(T0 - 2 * DAY - 1, T0 - 2 * DAY + 1)
        self.assertEqual([t["id"] for t in day], ["t2"])
        self.assertEqual(day[0]["item_camera_id"], "16")
        self.assertEqual(day[0]["checkout_at"], T0 - 2 * DAY)
        self.assertEqual([t["id"] for t in self.s.field_takes(T0 - 3600, T0)], ["t1"])

    def test_off_trip_and_camera(self):
        self.fill()
        self.assertEqual([t["id"] for t in self.s.takes_off_trip(0, 2e10)], ["t3"])
        self.assertEqual([t["id"] for t in self.s.takes_for_camera(["43.1"], 0, 2e10)], ["t1"])
        self.assertEqual([t["id"] for t in self.s.takes_for_camera({"43", "43.1"}, 0, 2e10)], ["t1", "t3"])
        self.assertEqual(self.s.takes_for_camera([], 0, 2e10), [])
        self.assertEqual(self.s.takes_created(T0, T0 + 60), [self.s.takes_created(T0, T0 + 1)[0]])

    def test_device_events_filters(self):
        self.fill()
        s = self.s
        self.assertEqual(len(s.device_events(0, 2e10)), 3)
        self.assertEqual([e["seq"] for e in s.device_events(0, 2e10, device_ids=["pi-1"])], [5, 6])
        self.assertEqual([e["type"] for e in s.device_events(0, 2e10, types=["camera_lost", "rec_start"])],
                         ["rec_start", "camera_lost"])
        self.assertEqual(s.device_events(0, 2e10, device_ids=[]), [])
        self.assertEqual(s.device_events(T0 + 5, T0 + 15)[0]["seq"], 6)
        self.assertEqual(s.max_seq(), {"pi-1": 6, "pi-2": 1})


class Writes(Base):
    def test_upsert_updates_in_place(self):
        self.s.upsert("takes", [take("a", score=None)])
        self.s.upsert("takes", [take("a", score=99.5)])
        rows = self.s.takes_created(0, 2e10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 99.5)

    def test_upsert_in_batches(self):
        n = store_mod.BATCH * 2 + 7
        self.assertEqual(self.s.upsert("takes", [take(f"t{i}") for i in range(n)]), n)
        self.assertEqual(self.s.count("takes"), n)

    def test_replace_all_drops_rows_gone_upstream(self):
        self.s.replace_all("tickets", [{"id": "a"}, {"id": "b"}])
        self.s.replace_all("tickets", [{"id": "b"}])
        self.assertEqual([t["id"] for t in self.s.tickets()], ["b"])

    def test_replace_where_only_touches_the_window(self):
        self.s.upsert("camera_events", [{"id": "old", "at": T0 - 50 * DAY}, {"id": "new", "at": T0}])
        self.s.replace_where("camera_events", [{"id": "newer", "at": T0 + 1}], "at >= ?", (T0 - DAY,))
        self.assertEqual(sorted(e["id"] for e in self.s.camera_events(0, 2e10)), ["newer", "old"])

    def test_unknown_column_is_refused(self):
        with self.assertRaises(KeyError):
            self.s.upsert("takes", [{"id": "x", "not_a_column": 1}])

    def test_delete_takes_missing(self):
        self.s.upsert("takes", [take("a", created_at=T0), take("b", created_at=T0 + 1),
                                take("c", created_at=T0 - 10 * DAY)])
        self.assertEqual(self.s.delete_takes_missing(["a"], T0 - DAY, T0 + DAY), 1)
        self.assertEqual(sorted(t["id"] for t in self.s.takes_created(0, 2e10)), ["a", "c"])

    def test_prune_keeps_open_items(self):
        self.fill()
        old = T0 - 60 * DAY
        self.s.upsert("items", [item("old", checkout_at=old), item("old_open", checkout_at=old, returned_at=None)])
        self.s.upsert("takes", [take("old", created_at=old)])
        self.s.upsert("device_events", [{"device_id": "pi-1", "seq": 1, "ts": old, "type": "power_on"}])
        out = self.s.prune(T0 - 50 * DAY)
        self.assertEqual(out, {"takes": 1, "items": 1, "camera_events": 0, "device_events": 1})
        self.assertIn("old_open", [i["id"] for i in self.s.open_items()])

    def test_fill_starcap_boards(self):
        self.fill()
        self.s.upsert("starcaps", [{"cpu_id": "cpuB", "name": "starcap13"}, {"cpu_id": "cpuC", "name": "x",
                                                                            "board": "DECXIN"}])
        self.s.upsert("takes", [take("tb", cpu_id="cpuB", board="FriendlyElec NanoPi R76S")])
        self.s.fill_starcap_boards()
        boards = {c["cpu_id"]: c["board"] for c in self.s.starcaps()}
        self.assertEqual(boards, {"cpuA": "Orange Pi 5", "cpuB": "FriendlyElec NanoPi R76S", "cpuC": "DECXIN"})


class Kv(Base):
    def test_round_trip(self):
        self.assertIsNone(self.s.kv_get("nope"))
        self.assertEqual(self.s.kv_get("nope", {"d": 1}), {"d": 1})
        value = {"a": [1, 2.5, None, True], "b": {"c": "starcap12"}}
        self.s.kv_set("k", value)
        self.assertEqual(self.s.kv_get("k"), value)
        self.s.kv_set("k", ["replaced"])
        self.assertEqual(self.s.kv_get("k"), ["replaced"])


class Notes(Base):
    def test_add_and_list_newest_first(self):
        a = self.s.add_note("cap", "starcap12", "loose plug")
        time.sleep(0.01)
        b = self.s.add_note("camera", "43.1", "soft left eye", status="watch")
        self.assertEqual(list(a), NOTE_KEYS)
        self.assertEqual([n["id"] for n in self.s.notes()], [b["id"], a["id"]])
        self.assertEqual([n["id"] for n in self.s.notes(subject_type="cap")], [a["id"]])
        self.assertEqual([n["id"] for n in self.s.notes("camera", "43.1")], [b["id"]])
        self.assertEqual(len(self.s.notes(limit=1)), 1)
        for n in self.s.notes():
            self.assertEqual(list(n), NOTE_KEYS)

    def test_validation(self):
        for args in (("starcap", "x", "t"), ("cap", "", "t"), ("cap", "x", "", None),
                     ("cap", "x", "t", "broken"), ("cap", "x", "y" * 5000)):
            with self.assertRaises(ValueError, msg=args):
                self.s.add_note(*args)
        self.assertEqual(self.s.add_note("cap", "x", "", status="Repair")["status"], "repair")
        self.assertIsNone(self.s.add_note("cap", "x", "t", status="")["status"])

    def test_soft_delete(self):
        n = self.s.add_note("cap", "starcap12", "x")
        self.assertTrue(self.s.delete_note(n["id"]))
        self.assertFalse(self.s.delete_note(n["id"]))       # already hidden
        self.assertFalse(self.s.delete_note(99999))
        self.assertEqual(self.s.notes(), [])
        with sqlite3.connect(self.path) as raw:             # still in the file
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_status_is_the_newest_note_with_a_status(self):
        s = self.s
        self.assertIsNone(s.current_status("cap", "starcap12"))
        s.add_note("cap", "starcap12", "sent for repair", status="repair")
        s.add_note("cap", "starcap12", "just a remark")                 # no status: changes nothing
        self.assertEqual(s.current_status("cap", "starcap12")["status"], "repair")
        watch = s.add_note("cap", "starcap12", "back, keep an eye", status="watch")
        self.assertEqual(s.current_status("cap", "starcap12")["status"], "watch")
        s.delete_note(watch["id"])                                      # deleting falls back
        self.assertEqual(s.current_status("cap", "starcap12")["status"], "repair")
        s.add_note("cap", "starcap12", "fixed", status="ok")            # ok clears it
        self.assertIsNone(s.current_status("cap", "starcap12"))

    def test_current_statuses_lists_every_unit(self):
        s = self.s
        s.add_note("cap", "starcap1", "", status="dead")
        s.add_note("cap", "starcap2", "", status="lost")
        s.add_note("camera", "43", "", status="watch")
        s.add_note("cap", "starcap3", "", status="ok")
        got = {(r["subject_type"], r["subject_id"]): r["status"] for r in s.current_statuses()}
        self.assertEqual(got, {("cap", "starcap1"): "dead", ("cap", "starcap2"): "lost", ("camera", "43"): "watch"})
        self.assertEqual(len(s.current_statuses("camera")), 1)
        self.assertEqual(set(s.current_statuses()[0]), {"subject_type", "subject_id", "status", "since", "text"})


class Summaries(Base):
    def test_put_get(self):
        self.assertIsNone(self.s.summary_get("2026-10-11"))
        a = self.s.summary_put("2026-10-11", "**starcap daily**")
        self.assertEqual(self.s.summary_get("2026-10-11"), a)
        self.assertEqual(set(a), {"date", "text", "written_at"})
        time.sleep(0.01)
        b = self.s.summary_put("2026-10-11", "rewritten")
        self.assertEqual(self.s.summary_get("2026-10-11")["text"], "rewritten")
        self.assertGreater(b["written_at"], a["written_at"])


class ClearCache(Base):
    def test_keeps_notes_and_summaries(self):
        self.fill()
        self.s.kv_set("sync", {"last_ok_at": 1})
        note = self.s.add_note("cap", "starcap12", "keep me", status="repair")
        self.s.summary_put("2026-10-11", "keep me too")
        self.s.clear_cache()
        for table in store_mod.CACHE_TABLES:
            self.assertEqual(self.s.count(table), 0, table)
        self.assertEqual(self.s.notes(), [note])
        self.assertEqual(self.s.summary_get("2026-10-11")["text"], "keep me too")
        self.assertEqual(self.s.current_status("cap", "starcap12")["status"], "repair")


class Schema(Base):
    def test_version_and_reopen(self):
        self.s.add_note("cap", "starcap1", "survives a reopen")
        self.s.close()
        self.s = Store(self.path)
        with sqlite3.connect(self.path) as raw:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], store_mod.SCHEMA_VERSION)
            self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        for name in ("takes_trip_item", "takes_created", "takes_camera_unit", "items_checkout", "items_returned",
                     "device_events_ts"):
            self.assertIn(name, indexes)
        self.assertEqual(len(self.s.notes()), 1)

    def test_a_v1_cache_is_upgraded_in_place(self):
        # the cache as first shipped on 2026-10-12, before QC runs were synced
        self.s.close()
        self.path.unlink()
        with sqlite3.connect(self.path) as raw:
            for stmt in store_mod.MIGRATIONS[0]:
                raw.execute(stmt)
            raw.execute("INSERT INTO notes (subject_type, subject_id, created_at, text) VALUES ('cap', 'starcap1', 1, 'mine')")
            raw.execute("PRAGMA user_version=1")
        self.s = Store(self.path)
        with sqlite3.connect(self.path) as raw:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], store_mod.SCHEMA_VERSION)
        self.s.replace_all("qc_runs", [{"id": "r1", "cpu_id": "cpuA", "name": "starcap12", "model": "NanoPi",
                                        "camera_id": "43", "version": "1.67.0", "verdict": "pass",
                                        "started_at": 1.0, "finished_at": 2.0, "headline": "ok",
                                        "failed_steps": ["Soak take"], "station": "waleed"}])
        self.assertEqual(self.s.qc_runs()[0]["failed_steps"], ["Soak take"])
        self.assertEqual([n["text"] for n in self.s.notes()], ["mine"])                  # notes untouched

    def test_a_v2_cache_gains_sheet_paths_and_re_pulls_its_takes(self):
        # the cache before contact sheets (2026-10-16): takes without sheet_path, a takes cursor
        self.s.close()
        self.path.unlink()
        with sqlite3.connect(self.path) as raw:
            for stmt in store_mod.MIGRATIONS[0] + store_mod.MIGRATIONS[1]:
                raw.execute(stmt)
            raw.execute("INSERT INTO takes (id, created_at) VALUES ('old', 1)")
            raw.execute("INSERT INTO kv (key, value, updated_at) VALUES ('takes_cursor', '{\"backfilled_at\": 1}', 1)")
            raw.execute("INSERT INTO kv (key, value, updated_at) VALUES ('sync', '{}', 1)")
            raw.execute("INSERT INTO notes (subject_type, subject_id, created_at, text) VALUES ('cap', 'starcap1', 1, 'mine')")
            raw.execute("PRAGMA user_version=2")
            self.assertNotIn("sheet_path", [r[1] for r in raw.execute("PRAGMA table_info(takes)")])
        self.s = Store(self.path)
        self.assertIsNone(self.s.kv_get("takes_cursor"))                                  # next sync pulls everything
        self.assertEqual(self.s.kv_get("sync"), {})
        old = self.s.takes_created(0, 10)[0]
        self.assertEqual((old["sheet_path"], old["sheet_grid"]), (None, None))
        grid = {"cols": 8, "rows": 1, "tile_w": 480, "tile_h": 300, "pad": 2, "tiles": 4}
        self.s.upsert("takes", [dict(old, sheet_path="analysis/c/r/contactsheet.jpg", sheet_grid=grid)])
        new = self.s.takes_created(0, 10)[0]
        self.assertEqual((new["sheet_path"], new["sheet_grid"]), ("analysis/c/r/contactsheet.jpg", grid))
        self.assertEqual([n["text"] for n in self.s.notes()], ["mine"])

    def test_newer_file_is_refused(self):
        self.s.close()
        with sqlite3.connect(self.path) as raw:
            raw.execute(f"PRAGMA user_version={store_mod.SCHEMA_VERSION + 1}")
        with self.assertRaises(RuntimeError):
            Store(self.path)
        with sqlite3.connect(self.path) as raw:
            raw.execute(f"PRAGMA user_version={store_mod.SCHEMA_VERSION}")
        self.s = Store(self.path)

    def test_two_connections_and_threads_share_the_file(self):
        other = Store(self.path)
        try:
            errors = []

            def writer(st, prefix):
                try:
                    for i in range(30):
                        st.upsert("takes", [take(f"{prefix}{i}")])
                        st.kv_set(prefix, i)
                except Exception as e:      # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(st, p)) for st, p in
                       ((self.s, "a"), (other, "b"), (self.s, "c"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(self.s.count("takes"), 90)
            for st in (self.s, other):
                self.assertEqual(st._db.execute("PRAGMA busy_timeout").fetchone()[0], 15000)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
