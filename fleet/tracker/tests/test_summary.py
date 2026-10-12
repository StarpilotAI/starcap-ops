"""Offline tests for summary.py: the morning text and the append-only drafts file.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakestore import FakeStore, at

import clock
import config
import summary

DATE, TODAY = "2026-10-11", "2026-10-12"


def kpis(**v):
    base = {"caps_out": 0, "caps_recorded": 0, "caps_no_footage": 0, "hours_total": 0.0, "hours_good": 0.0,
            "good_pct": None, "timing_pass_pct": None, "fps30_hours": 0.0, "fps30_caps": 0,
            "expired_cal_hours": 0.0}
    return {k: {"value": x, "prev": None} for k, x in dict(base, **v).items()}


def arrival(caps_out=0, arrived=0, still_out=0, waiting=0, pct=None):
    return {"caps_out": caps_out, "arrived": arrived, "still_out": still_out, "waiting": waiting,
            "no_footage": 0, "footage_missing": 0, "pct": pct}


def cap(name, status="ok", fps30=0.0, expired=0.0, camera="1"):
    flags = [{"code": "fps30", "severity": "bad", "label": "30 fps", "detail": ""}] if fps30 >= 0.25 else []
    return {"cap": name, "status": status, "camera": camera, "flags": flags,
            "hours": {"fps30": fps30}, "expired_cal_hours": expired}


def views(day, before, cal=None, stats=None, field=None, pipe=None, date=DATE, today=TODAY):
    return {"date": date, "today": today, "day": day, "before": before,
            "calibration": cal or {"today": {"done": 0}, "target": 10, "counts": {"overdue": 0}, "spikes": []},
            "maintenance": {"stats": stats or {"opened": 0, "resolved": 0, "open": 0, "critical": 0}},
            "field": field or {"long_out": [], "silent": []},
            "pipeline": pipe or {"stations": [], "uploaders": [], "label": {"ok": True}}}


class Text(unittest.TestCase):
    def test_busy_day_reads_like_the_spec(self):
        fps30 = [cap(n, fps30=h) for n, h in (("starcap31", 4.0), ("starcap5", 9.0), ("starcap9", 6.0))]
        fps30 += [cap(f"starcap{100 + i}", fps30=2.0) for i in range(9)]
        rows = (fps30 + [cap("starcap50", expired=6.0, camera="48.1"), cap("starcap51", expired=3.0, camera="164"),
                         cap("starcap12", "no_footage"), cap("starcap40", "footage_missing"),
                         cap("starcap60", "out")])
        day = {"date": DATE, "caps": rows, "arrival": arrival(91, 84, 3, 2, 82),
               "kpis": kpis(caps_out=91, caps_recorded=84, caps_no_footage=2, hours_total=587.0, hours_good=512.3,
                            good_pct=87.3, timing_pass_pct=99.1, fps30_hours=46.0, fps30_caps=12,
                            expired_cal_hours=9.0)}
        before = {"date": "2026-10-10", "caps": [], "arrival": arrival(82, 82, pct=100),
                  "kpis": kpis(caps_out=82, hours_total=506.2, hours_good=471.0)}
        cal = {"today": {"done": 10}, "target": 10, "counts": {"overdue": 24}, "clear_by": "2026-10-14",
               "spikes": [{"date": "2026-10-29", "due": 29}, {"date": "2026-11-07", "due": 51},
                          {"date": "2026-11-09", "due": 51}]}
        field = {"long_out": [{"cap": "starcap19", "days_out": 4.3}], "silent": [{"cap": f"c{i}"} for i in range(10)]}
        text = summary.text(views(day, before, cal, {"opened": 4, "resolved": 2, "open": 36, "critical": 31}, field))
        self.assertEqual(text, "\n".join([
            "**starcap daily, sun 11 oct** _(82% of footage in so far)_",
            "- 91 caps out, 84 recorded, 2 got nothing (starcap12, starcap40)",
            "- 587 h recorded, 512 h good (87%), timing pass 99.1%",
            "- 30 fps on 12 caps (46 h): starcap5, starcap9, starcap31 +9",
            "- 9 h shot on cameras past calibration (48.1, 164)",
            "- sat 10 oct final: 82 caps, 506 h, 471 h good",
            "- calib: 10/10 yesterday, 24 overdue (caught up by wed 14 oct), heads up 51 come due sat 7 nov",
            "- tickets: 4 new, 2 closed, 36 open (31 critical)",
            "- field: out 3d+: starcap19 (4d) · silent 24h+: c0, c1, c2 +7",
            "- pipeline: all good",
        ]))

    def test_quiet_day_says_only_what_is_there(self):
        empty = {"date": DATE, "caps": [], "arrival": arrival(), "kpis": kpis()}
        before = {"date": "2026-10-10", "caps": [], "arrival": arrival(), "kpis": kpis()}
        self.assertEqual(summary.text(views(empty, before)),
                         "**starcap daily, sun 11 oct**\n- no caps out\n- calib: 0/10 yesterday\n"
                         "- pipeline: all good")

    def test_clean_day_has_no_problem_lines(self):
        day = {"date": TODAY, "caps": [cap("starcap1"), cap("starcap2")], "arrival": arrival(2, 2, pct=100),
               "kpis": kpis(caps_out=2, caps_recorded=2, hours_total=12.04, hours_good=12.04, good_pct=100.0,
                            timing_pass_pct=100.0)}
        before = {"date": DATE, "caps": [], "arrival": arrival(1, 0, still_out=1, pct=0), "kpis": kpis(caps_out=1)}
        text = summary.text(views(day, before, date=TODAY, today=TODAY))
        self.assertEqual(text.splitlines(), [
            "**starcap daily, mon 12 oct**",
            "- 2 caps out, 2 recorded",
            "- 12 h recorded, 12 h good (100%), timing pass 100.0%",
            "- sun 11 oct so far (0% in): 1 cap, 0 h, 0 h good",
            "- calib: 0/10 today",
            "- pipeline: all good"])

    def test_names_cut_at_three(self):
        self.assertEqual(summary.names(["a", "b", "c"]), "a, b, c")
        self.assertEqual(summary.names(["a", "b", "c", "d", "e"]), "a, b, c +2")
        self.assertEqual(summary.names([]), "")

    def test_several_long_trips_and_pipeline_trouble(self):
        empty = {"date": DATE, "caps": [], "arrival": arrival(), "kpis": kpis()}
        field = {"long_out": [{"cap": f"starcap{i}", "days_out": 9 - i} for i in range(5)], "silent": [{"cap": "x"}]}
        pipe = {"stations": [{"name": "opm", "status": "bad", "error": None, "age_s": 3 * 3600, "waiting_caps": 4}],
                "uploaders": [], "label": {"ok": False}}
        lines = summary.text(views(empty, empty, field=field, pipe=pipe)).splitlines()
        self.assertIn("- field: out 3d+: starcap0 (9d), starcap1 (8d), starcap2 (7d) +2 · silent 24h+: x", lines)
        self.assertEqual(lines[-1], "- pipeline: copy station silent 3 h with footage from 4 caps waiting, "
                                    "label tick failing")

    def test_a_long_trip_is_not_named_again_as_silent(self):
        field = {"long_out": [{"cap": "starcap106", "days_out": 20.2}],
                 "silent": [{"cap": "starcap106"}, {"cap": "starcap7"}]}
        self.assertEqual(summary.field_line(field), "- field: out 3d+: starcap106 (20d) · silent 24h+: starcap7")
        self.assertEqual(summary.field_line({"long_out": field["long_out"], "silent": [{"cap": "starcap106"}]}),
                         "- field: out 3d+: starcap106 (20d)")

    def test_hours_format(self):
        self.assertEqual([summary.hours(h) for h in (587.2, 9.96, 9.04, 2.34, 0.4)], ["587", "10", "9", "2.3", "0.4"])


class Write(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "drafts" / "slack messages.md"
        self.patch = mock.patch.object(config, "SLACK_FILE", self.path)
        self.patch.start()
        self.clock = mock.patch.object(clock, "now", return_value=at(TODAY, 9))
        self.clock.start()
        self.store = FakeStore()

    def tearDown(self):
        self.clock.stop()
        self.patch.stop()
        self.dir.cleanup()

    def test_creates_the_file(self):
        res = summary.write(self.store, DATE, "first")
        self.assertEqual(self.path.read_text(), "first")
        self.assertEqual(res, {"ok": True, "path": str(self.path), "written_at": at(TODAY, 9)})
        self.assertEqual(self.store.summary_get(DATE)["text"], "first")

    def test_appends_without_rewriting(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("## my own draft\nkeep me\n")
        summary.write(self.store, DATE, "**starcap daily**\n- a")
        summary.write(self.store, TODAY, "second")
        self.assertEqual(self.path.read_text(),
                         "## my own draft\nkeep me\n\n\n---\n\n**starcap daily**\n- a\n\n---\n\nsecond")

    def test_refuses_the_same_date_twice_unless_forced(self):
        summary.write(self.store, DATE, "one")
        with self.assertRaises(summary.AlreadyWritten):
            summary.write(self.store, DATE, "two")
        self.assertEqual(self.path.read_text(), "one")
        summary.write(self.store, DATE, "two", force=True)
        self.assertEqual(self.path.read_text(), "one\n\n---\n\ntwo")


if __name__ == "__main__":
    unittest.main()
