"""Offline tests for pipeline.py: heartbeat health, stuck uploads, backlog and lag.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest

from fakestore import at, item, take

import config
import pipeline

NOW = at("2026-10-12", 12)
H = 3600.0


def station(age, error=None, **kw):
    return dict({"id": "opm-1", "station_name": "copy station", "last_seen": NOW - age,
                 "activity": {"state": "copying", "message": "starcap5 3/9"},
                 "drives": [{"role": "staging", "bytes_free": 512e9, "bytes_total": 1e12},
                            {"role": "target", "bytes_free": 3e12, "bytes_total": 4e12}],
                 "pending_review": 2, "error": error}, **kw)


def uploader(age=10, error=None, current=None, **kw):
    return dict({"id": "ssd-1", "name": "uploader A", "last_seen": NOW - age,
                 "detection": {"state": "ready", "level": "ok", "message": "1 drive"},
                 "runs": {"done": 70, "uploading": 1}, "current": current, "error": error}, **kw)


class Health(unittest.TestCase):
    def test_station_reporting(self):
        ok = pipeline.stations([station(config.STATION_STALE_S - 1)], NOW, [])[0]
        err = pipeline.stations([station(10, "drive full")], NOW, [])[0]
        self.assertEqual((ok["status"], err["status"]), ("ok", "warn"))

    def test_a_quiet_station_is_idle_unless_footage_waits_for_it(self):
        stale, down = config.STATION_STALE_S, config.STATION_DOWN_S
        waiting = [item(id="it1", returned_at=NOW - 60)]                             # back after it went quiet
        status = [pipeline.stations([station(age)], NOW, w)[0]["status"]
                  for age, w in ((stale, []), (9 * H, []), (stale, waiting), (down - 1, waiting), (down, waiting))]
        self.assertEqual(status, ["idle", "idle", "warn", "warn", "bad"])

    def test_a_cap_skipped_while_the_station_ran_is_not_the_stations_fault(self):
        # starcap33 on 2026-10-12: back 01:33, station copied others until 04:14, its footage never came
        back = item(id="it1", returned_at=NOW - 10 * H)
        ran_on = pipeline.stations([station(7 * H)], NOW, [back])[0]               # seen 3 h after the return
        died = pipeline.stations([station(10.5 * H)], NOW, [back])[0]             # quiet since before it
        self.assertEqual((ran_on["status"], ran_on["waiting_caps"]), ("idle", 0))
        self.assertEqual((died["status"], died["waiting_caps"]), ("bad", 1))

    def test_station_row(self):
        s = pipeline.stations([station(30)], NOW, [item(id="a", returned_at=NOW - 60),
                                                   item(id="b", returned_at=NOW - 60)])[0]
        self.assertEqual({k: s[k] for k in ("name", "age_s", "state", "staging_free_gb", "pending_review",
                                            "waiting_caps")},
                         {"name": "copy station", "age_s": 30, "state": "copying", "staging_free_gb": 512.0,
                          "pending_review": 2, "waiting_caps": 2})


class AwaitingCopy(unittest.TestCase):
    def back(self, hours_ago, **kw):
        return item(cpu_id="cpu1", checkout_at=NOW - 12 * H, returned_at=NOW - hours_ago * H, **kw)

    def test_back_with_recordings_and_nothing_copied(self):
        rec = {"cpu1": [NOW - 6 * H]}
        waiting = self.back(2, id="w")
        self.assertEqual(pipeline.awaiting_copy([waiting], set(), rec, NOW), [waiting])
        self.assertEqual(pipeline.awaiting_copy([waiting], {"w"}, rec, NOW), [])           # copied

    def test_not_yet_due_or_too_old_or_still_out(self):
        rec = {"cpu1": [NOW - 6 * H]}
        for it in (self.back(0.5, id="just back"), self.back(30, id="old news"),
                   item(cpu_id="cpu1", checkout_at=NOW - 12 * H, returned_at=None, id="out")):
            self.assertEqual(pipeline.awaiting_copy([it], set(), rec, NOW), [], it["id"])

    def test_a_cap_that_never_recorded_is_not_waiting(self):
        it = self.back(2, id="w")
        self.assertEqual(pipeline.awaiting_copy([it], set(), {}, NOW), [])
        self.assertEqual(pipeline.awaiting_copy([it], set(), {"cpu1": [NOW - 20 * H]}, NOW), [])  # before checkout

    def test_uploader_stuck(self):
        cur = {"run": "r1", "sent": 4e9, "total": 9e9, "rate_bps": 25e6}
        line = config.UPLOAD_STUCK_MIN * 60
        stuck = pipeline.uploaders([uploader(current=cur)], {"ssd-1": {"run": "r1", "sent": 4e9,
                                                                        "changed_at": NOW - line}}, NOW)[0]
        moving = pipeline.uploaders([uploader(current=cur)], {"ssd-1": {"run": "r1", "sent": 4e9,
                                                                         "changed_at": NOW - line + 60}}, NOW)[0]
        self.assertEqual((stuck["stuck"], stuck["stuck_min"], stuck["status"]),
                         (True, config.UPLOAD_STUCK_MIN, "bad"))
        self.assertEqual((moving["stuck"], moving["status"]), (False, "ok"))
        self.assertEqual(moving["current"], {"run": "r1", "sent": 4e9, "total": 9e9, "pct": 44.4, "rate_mbps": 25.0})
        self.assertEqual(moving["runs"], {"pending": 0, "uploading": 1, "done": 70, "error": 0})

    def test_idle_uploader_is_not_stuck(self):
        idle = pipeline.uploaders([uploader()], {"ssd-1": {"run": "r1", "sent": 1, "changed_at": NOW - 9 * H}}, NOW)
        done = pipeline.uploaders([uploader(current={"run": "r1", "sent": 5, "total": 5})],
                                  {"ssd-1": {"run": "r1", "sent": 5, "changed_at": NOW - 9 * H}}, NOW)
        self.assertEqual([(u["stuck"], u["status"]) for u in idle + done], [(False, "ok"), (False, "ok")])
        self.assertIsNone(idle[0]["current"])

    def test_uploader_error_or_down_is_bad(self):
        self.assertEqual(pipeline.uploaders([uploader(error="gcs 403")], {}, NOW)[0]["status"], "bad")
        self.assertEqual(pipeline.uploaders([uploader(age=config.STATION_STALE_S)], {}, NOW)[0]["status"], "warn")
        self.assertEqual(pipeline.uploaders([uploader(age=config.STATION_DOWN_S)], {}, NOW)[0]["status"], "bad")
        self.assertEqual(pipeline.uploaders([uploader(last_seen=None)], {}, NOW)[0]["status"], "bad")


class Flow(unittest.TestCase):
    def test_backlog(self):
        takes = [take(created_at=NOW - 30 * H, uploaded_at=None, recorded_s=900),
                 take(created_at=NOW - 2 * H, uploaded_at=None, recorded_s=900),
                 take(created_at=NOW - 5 * H, uploaded_at=NOW - H)]
        self.assertEqual(pipeline.backlog(takes, NOW), {"takes": 2, "hours": 0.5, "oldest_h": 30.0,
                                                        "over_24h": {"takes": 1, "hours": 0.25}})
        self.assertEqual(pipeline.backlog([], NOW), {"takes": 0, "hours": 0.0, "oldest_h": None,
                                                      "over_24h": {"takes": 0, "hours": 0.0}})

    def test_percentile_interpolates(self):
        self.assertEqual(pipeline.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(pipeline.percentile(list(range(1, 11)), 90), 9.1)
        self.assertEqual(pipeline.spread([]), {"p50": None, "p90": None, "n": 0})

    def test_lag(self):
        items = [item(id="a", checkout_at=NOW - 3 * 24 * H, returned_at=NOW - 2 * 24 * H),
                 item(id="b", checkout_at=NOW - 3 * 24 * H, returned_at=NOW - 30 * H),
                 item(id="old", checkout_at=NOW - 13 * 24 * H, returned_at=NOW - 10 * 24 * H),
                 item(id="early", checkout_at=NOW - 24 * H, returned_at=NOW - 10 * H)]
        takes = [take(trip_item_id="a", created_at=NOW - 46 * H, uploaded_at=NOW - 40 * H),   # copied 2 h after
                 take(trip_item_id="a", created_at=NOW - 44 * H, uploaded_at=NOW - 30 * H),
                 take(trip_item_id="b", created_at=NOW - 26 * H, uploaded_at=None),            # copied 4 h after
                 take(trip_item_id="old", created_at=NOW - 9 * 24 * H, uploaded_at=NOW - 8 * 24 * H),
                 take(trip_item_id="early", created_at=NOW - 11 * H, uploaded_at=NOW - 9 * H)]  # before the return
        lag = pipeline.lag(takes, items, NOW)
        # uploaded this week: both "a" takes (out at -72 h, up at -40 and -30 h) and "early"
        # (out -24 h, up -9 h); "b" never uploaded, "old" is outside the week
        self.assertEqual(lag["checkout_to_cloud_h"], {"p50": 32.0, "p90": 40.0, "n": 3})  # 15, 32 and 42 h
        self.assertEqual(lag["copy_to_cloud_h"], {"p50": 6.0, "p90": 12.4, "n": 3})       # 2, 6 and 14 h

    def test_label(self):
        tick = {"at": NOW - 14 * 60, "ok": True, "last_ok_at": NOW - 14 * 60, "label": {"waiting": 44}}
        self.assertEqual(pipeline.label(tick, NOW), {"at": NOW - 840, "age_min": 14, "ok": True, "waiting": 44})
        self.assertEqual(pipeline.label(None, NOW), {"at": None, "age_min": None, "ok": None, "waiting": None})

    def test_problems(self):
        stranded = item(id="it1", cpu_id="cpu1", checkout_at=NOW - 12 * H, returned_at=NOW - 2.5 * H)
        old = take(id="t-old", trip_item_id=None, created_at=NOW - 30 * H, uploaded_at=None, recorded_s=7200)
        v = pipeline.view(NOW, NOW, [station(2 * H)],
                          [uploader(current={"run": "r", "sent": 1, "total": 9}),
                           uploader(id="ssd-2", name="u2", error="gcs 403")],
                          {"ssd-1": {"run": "r", "sent": 1, "changed_at": NOW - 40 * 60}},
                          {"at": NOW, "ok": False, "label": {"waiting": 1}}, [old], [stranded],
                          {"cpu1": [NOW - 8 * H]})
        self.assertEqual(pipeline.problems(v), [
            "copy station silent 2 h with footage from 1 cap waiting", "uploader A stuck 40 min",
            "u2 error: gcs 403", "1 takes (2 h) copied but not in the cloud after a day, oldest 30 h",
            "label tick failing"])
        quiet = pipeline.view(NOW, NOW, [station(9 * H)], [uploader()], {}, {"at": NOW, "ok": True}, [], [], {})
        self.assertEqual(pipeline.problems(quiet), [])                    # idle overnight station is fine


if __name__ == "__main__":
    unittest.main()
