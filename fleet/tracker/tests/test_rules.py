"""Offline tests for rules.py: buckets, cap-day status, flags, expired calibration.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest
from unittest import mock

from fakestore import FakeStore, at, cal_version, current_for, device, device_event, item, starcap, take, ticket

import api
import calib
import clock
import config
import maint
import rules

DAY = "2026-10-11"
NOW = at("2026-10-12", 10)
H = 3600.0


def context(versions=(), revs=(), current=(), tickets=(), notes=(), devices=(), starcaps=None,
            events_since=at("2026-10-01")):
    caps = [starcap(cpu_id="cpu5", name="starcap5", camera_id="43")] if starcaps is None else starcaps
    return rules.Context(caps, devices, calib.Lenses(revs, current),
                         calib.CalIndex(versions, list(current) + current_for(versions)),
                         maint.OpenTickets(tickets), maint.current_statuses(notes), events_since)


def field_take(it, **kw):
    """A take as field_takes() hands it out: with its item's times and camera."""
    return dict(take(cpu_id=it["cpu_id"], trip_item_id=it["id"], created_at=at("2026-10-11", 20), **kw),
                checkout_at=it["checkout_at"], returned_at=it["returned_at"], item_camera_id=it["camera_id"])


def trip(out_h=8, back_h=18, day=DAY, **kw):
    return item(cpu_id="cpu5", camera_id="43", checkout_at=at(day, out_h),
                returned_at=None if back_h is None else at(day, back_h), **kw)


def row_for(takes=(), items=None, ctx=None, events=None, now=NOW, latest=None):
    items = [trip()] if items is None else items
    ctx = ctx or context(versions=[cal_version(camera_key="43", calibrated_at=at("2026-10-01", 10))])
    ev = None if events is None else rules.events_by_cpu(events, ctx)
    return rules.cap_days(items, list(takes), ctx, now, ev, latest)[0]


def codes(row):
    return [f["code"] for f in row["flags"]]


class Buckets(unittest.TestCase):
    def b(self, **kw):
        return rules.bucket(take(**kw))

    def test_good(self):
        self.assertEqual(self.b(), "good")

    def test_min_clip_is_the_line(self):
        self.assertEqual(self.b(recorded_s=config.MIN_CLIP_S), "good")
        self.assertEqual(self.b(recorded_s=config.MIN_CLIP_S - 0.01), "short")

    def test_short_beats_everything(self):
        self.assertEqual(self.b(recorded_s=5, fault_kind="unrecoverable", codes=["no_chunks"], score=None,
                                readiness_status=None), "short")

    def test_broken(self):
        self.assertEqual(self.b(fault_kind="unrecoverable"), "broken")
        self.assertEqual(self.b(codes=["late_imu", "no_chunks"]), "broken")

    def test_broken_beats_pending(self):
        self.assertEqual(self.b(codes=["no_chunks"], score=None, readiness_status=None), "broken")

    def test_pending_until_analysed(self):
        self.assertEqual(self.b(score=None, readiness_status=None), "pending")

    def test_unknown_length_is_not_short(self):
        self.assertEqual(self.b(recorded_s=None, score=None, readiness_status=None), "pending")

    def test_untimed_beats_fps30(self):
        self.assertEqual(self.b(score=None, readiness_status="warn"), "untimed")
        self.assertEqual(self.b(score=None, readiness_status="ready", nominal_fps=30), "untimed")

    def test_fps30_beats_timing(self):
        self.assertEqual(self.b(nominal_fps=30, score=40.0), "fps30")

    def test_timing_score_line(self):
        self.assertEqual(self.b(score=config.TIMING_PASS_SCORE), "good")
        self.assertEqual(self.b(score=config.TIMING_PASS_SCORE - 0.01), "timing_fail")


class Versions(unittest.TestCase):
    def test_version_key(self):
        self.assertGreater(rules.version_key("1.10.0"), rules.version_key("1.9.9"))
        self.assertEqual(rules.version_key("1.67.0-dirty"), (1, 67, 0))
        self.assertIsNone(rules.version_key("dev"))
        self.assertIsNone(rules.version_key(None))

    def _takes(self, version, caps, per_cap=1):
        return [take(cpu_id=f"cpu{i}", software_version=version) for i in range(caps) for _ in range(per_cap)]

    def test_latest_needs_enough_distinct_caps(self):
        n = config.LATEST_VERSION_MIN_CAPS
        old = self._takes("1.67.0", n)
        canary = [take(cpu_id=f"c{i}", software_version="1.68.0") for i in range(n - 1)]
        self.assertEqual(rules.latest_version(old + canary), "1.67.0")
        self.assertEqual(rules.latest_version(old + canary + [take(cpu_id="c99", software_version="1.68.0")]),
                         "1.68.0")

    def test_one_busy_cap_is_not_the_fleet(self):
        busy = self._takes("1.70.0", 1, per_cap=50)
        self.assertIsNone(rules.latest_version(busy))

    def test_versions_compare_as_numbers(self):
        n = config.LATEST_VERSION_MIN_CAPS
        mixed = self._takes("1.9.0", n) + [take(cpu_id=f"x{i}", software_version="1.10.0") for i in range(n)]
        self.assertEqual(rules.latest_version(mixed), "1.10.0")

    def test_old_firmware_flag(self):
        it = trip()
        old = row_for([field_take(it, software_version="1.64.0")], [it], latest="1.67.0")
        self.assertIn("old_firmware", codes(old))
        self.assertEqual(old["severity"], "info")
        same = row_for([field_take(it, software_version="1.67.0")], [it], latest="1.67.0")
        canary = row_for([field_take(it, software_version="1.68.0")], [it], latest="1.67.0")
        self.assertNotIn("old_firmware", codes(same) + codes(canary))

    def test_row_version_is_the_newest_run(self):
        it = trip()
        row = row_for([field_take(it, software_version="1.9.0"), field_take(it, software_version="1.10.0")], [it])
        self.assertEqual(row["version"], "1.10.0")


class Status(unittest.TestCase):
    def test_out(self):
        it = trip(back_h=None)
        self.assertEqual(row_for([], [it])["status"], "out")

    def test_out_beats_takes(self):
        it = trip(back_h=None)
        self.assertEqual(row_for([field_take(it)], [it])["status"], "out")

    def test_ok(self):
        it = trip()
        self.assertEqual(row_for([field_take(it, recorded_s=3)], [it])["status"], "ok")

    def test_waiting_then_no_footage(self):
        grace = config.COPY_GRACE_H * H
        it = trip()
        self.assertEqual(row_for([], [it], now=it["returned_at"] + grace - 60)["status"], "waiting")
        row = row_for([], [it], now=it["returned_at"] + grace + 60, events=[])
        self.assertEqual(row["status"], "no_footage")
        self.assertEqual(codes(row), ["no_footage"])

    def test_footage_missing_needs_a_rec_start_on_the_trip(self):
        it = trip()
        dev = [device(device_id="d5", cpu_id="cpu5")]
        ctx = context(devices=dev)
        during = [device_event(device_id="d5", ts=at(DAY, 10), type="rec_start")]
        before = [device_event(device_id="d5", ts=at(DAY, 7), type="rec_start")]
        self.assertEqual(row_for([], [it], ctx=ctx, events=during)["status"], "footage_missing")
        self.assertEqual(codes(row_for([], [it], ctx=ctx, events=during)), ["footage_missing"])
        self.assertEqual(row_for([], [it], ctx=ctx, events=before)["status"], "no_footage")

    def test_without_a_device_log_an_empty_return_is_not_called_missing(self):
        # logs start after this check-out: no rec_start proves nothing, and no counts are trusted
        it = trip()
        ctx = context(devices=[device(device_id="d5", cpu_id="cpu5")], events_since=at(DAY, 12))
        during = [device_event(device_id="d5", ts=at(DAY, 13), type="rec_start")]
        row = row_for([], [it], ctx=ctx, events=during)
        self.assertEqual(row["status"], "no_footage")
        self.assertIn("no device log that far back", row["flags"][0]["detail"])

    def use(self, events, it=None, events_since=at("2026-10-01"), heard=at("2026-10-12", 9)):
        it = it or trip()
        ctx = context(devices=[device(device_id="d5", cpu_id="cpu5", last_seen=heard)], events_since=events_since)
        evs = [device_event(device_id="d5", ts=at(DAY, h), type=kind) for h, kind in events]
        return row_for([], [it], ctx=ctx, events=evs)["use"]

    def test_never_switched_on_is_unused(self):
        use = self.use([])
        self.assertEqual(use["verdict"], "unused")
        self.assertEqual(use["detail"], "out 10.0 h, never switched on")
        self.assertEqual(self.use([], it=trip(out_h=18, back_h=18 + 4 / 60))["detail"], "out 4 min, never switched on")

    def test_a_boot_while_docked_at_check_in_is_not_a_try(self):
        use = self.use([(18 - 3 / 60, "power_on")])          # 3 min before the 18:00 return
        self.assertEqual(use["verdict"], "unused")
        self.assertIn("only booted when docked", use["detail"])

    def test_switched_on_without_a_take_is_tried(self):
        use = self.use([(10, "power_on"), (12, "power_on"), (12, "camera_lost")])
        self.assertEqual((use["verdict"], use["boots"]), ("tried", 2))

    def test_a_take_started_is_lost_footage(self):
        self.assertEqual(self.use([(10, "power_on"), (10.1, "rec_start")])["verdict"], "lost")

    def test_not_online_since_the_return_is_silent_not_unused(self):
        self.assertEqual(self.use([], heard=at(DAY, 7))["verdict"], "silent")
        self.assertEqual(self.use([(10, "power_on")], heard=at(DAY, 12))["verdict"], "silent")
        # a take it did log still says the footage is lost, whatever came after
        self.assertEqual(self.use([(10, "rec_start")], heard=at(DAY, 12))["verdict"], "lost")

    def test_no_log_is_unknown(self):
        self.assertEqual(self.use([], events_since=at(DAY, 12))["verdict"], "unknown")

    def test_only_got_nothing_rows_carry_a_use(self):
        it = trip()
        self.assertIsNone(row_for([field_take(it)], [it])["use"])

    def test_unused_kpi(self):
        rows = [{"use": {"verdict": v}, "status": "no_footage", "clips": 0, "hours": dict.fromkeys(rules.HOURS, 0.0),
                 "expired_cal_hours": 0.0} for v in ("unused", "unused", "tried")]
        self.assertEqual(rules.day_kpis(rows)["caps_unused"], 2)

    def test_two_checkouts_of_one_cap_merge(self):
        a, b = trip(out_h=8, back_h=12), trip(out_h=13, back_h=None)
        row = row_for([field_take(a)], [a, b])
        self.assertEqual(row["status"], "out")
        self.assertEqual(row["checkout_at"], a["checkout_at"])
        self.assertIsNone(row["returned_at"])
        b["returned_at"] = at(DAY, 19)
        row = row_for([field_take(a), field_take(b)], [a, b])
        self.assertEqual((row["status"], row["returned_at"], row["takes"]), ("ok", at(DAY, 19), 2))


class Numbers(unittest.TestCase):
    def test_hours_pcts_and_fps(self):
        it = trip()
        takes = [field_take(it, recorded_s=3600, fps=60.0), field_take(it, recorded_s=1800, score=50.0, fps=57.0),
                 field_take(it, recorded_s=10, fps=None)]
        row = row_for(takes, [it])
        self.assertAlmostEqual(row["hours"]["total"], 5410 / H)
        self.assertAlmostEqual(row["hours"]["timing_fail"], 0.5)
        self.assertEqual((row["takes"], row["clips"], row["short_takes"]), (3, 2, 1))
        self.assertEqual(row["timing_pass_pct"], round(100 * 3600 / 5400, 1))
        self.assertEqual(row["good_pct"], round(100 * 3600 / 5410, 1))
        self.assertEqual(row["fps"], round((60 * 3600 + 57 * 1800) / 5400, 2))
        self.assertEqual(row["nominal_fps"], 60)

    def test_no_takes_has_no_percentages(self):
        row = row_for([], [trip(back_h=None)])
        self.assertIsNone(row["good_pct"])
        self.assertIsNone(row["timing_pass_pct"])
        self.assertIsNone(row["fps"])

    def test_camera_is_the_one_with_most_hours(self):
        it = trip()
        row = row_for([field_take(it, camera_unit="50", recorded_s=60), field_take(it, camera_unit="43.1")], [it])
        self.assertEqual(row["camera"], "43.1")
        self.assertEqual(row["cameras"], ["43", "43.1", "50"])

    def test_rounded_keeps_totals_exact(self):
        it = trip()
        rows = [row_for([field_take(it, recorded_s=1001)], [it]) for _ in range(3)]
        self.assertEqual(rules.day_hours(rows)["total"], round(3003 / H, 2))
        self.assertEqual(rules.rounded(rows[0])["hours"]["total"], round(1001 / H, 2))


class Flags(unittest.TestCase):
    """Every threshold, just under and just at the line."""

    def setUp(self):
        self.it = trip()
        self.line = config.FLAG_MIN_H * H

    def flagged(self, takes=(), events=None):
        ctx = context(versions=[cal_version(camera_key="43", calibrated_at=at("2026-10-01", 10))],
                      devices=[device(device_id="d5", cpu_id="cpu5")])
        return codes(row_for(takes, [self.it], ctx=ctx, events=events))

    def test_fps30(self):
        self.assertIn("fps30", self.flagged([field_take(self.it, nominal_fps=30, recorded_s=self.line)]))
        self.assertNotIn("fps30", self.flagged([field_take(self.it, nominal_fps=30, recorded_s=self.line - 1)]))

    def test_timing(self):
        self.assertIn("timing", self.flagged([field_take(self.it, score=90.0, recorded_s=self.line)]))
        self.assertNotIn("timing", self.flagged([field_take(self.it, score=90.0, recorded_s=self.line - 1)]))

    def test_broken_counts_broken_plus_untimed(self):
        half = self.line / 2
        both = [field_take(self.it, codes=["no_chunks"], recorded_s=half),
                field_take(self.it, score=None, readiness_status="warn", recorded_s=half)]
        self.assertIn("broken", self.flagged(both))
        both[1]["recorded_s"] = half - 1
        self.assertNotIn("broken", self.flagged(both))

    def test_short_needs_enough_and_half(self):
        n = config.SHORT_TAKES_MIN

        def takes(short, long):
            return ([field_take(self.it, recorded_s=5) for _ in range(short)]
                    + [field_take(self.it) for _ in range(long)])
        self.assertIn("short", self.flagged(takes(n, n)))
        self.assertNotIn("short", self.flagged(takes(n - 1, 0)))
        self.assertNotIn("short", self.flagged(takes(n, n + 1)))

    def _events(self, kind, n):
        return [device_event(device_id="d5", ts=at(DAY, 9 + i * 0.1), type=kind) for i in range(n)]

    def test_storage_any_of_four_kinds(self):
        self.assertNotIn("storage", self.flagged([field_take(self.it)], events=[]))
        for kind in ("disk_full", "rec_refused", "storage_stalled", "storage_lost"):
            self.assertIn("storage", self.flagged([field_take(self.it)], events=self._events(kind, 1)), kind)

    def test_reboots(self):
        n = config.REBOOTS_MIN
        self.assertIn("reboots", self.flagged([field_take(self.it)], events=self._events("unclean_boot", n)))
        self.assertNotIn("reboots", self.flagged([field_take(self.it)], events=self._events("unclean_boot", n - 1)))

    def test_power_on_storm_is_a_reboot_loop(self):
        # a looping board logs power_on after power_on but rarely an unclean_boot each time
        n = config.POWER_ON_LOOP_MIN
        self.assertIn("reboots", self.flagged([field_take(self.it)], events=self._events("power_on", n)))
        self.assertNotIn("reboots", self.flagged([field_take(self.it)], events=self._events("power_on", n - 1)))

    def test_camera_drops(self):
        n = config.CAMERA_DROPS_MIN
        self.assertIn("camera_drops", self.flagged([field_take(self.it)], events=self._events("camera_lost", n)))
        self.assertNotIn("camera_drops",
                         self.flagged([field_take(self.it)], events=self._events("camera_lost", n - 1)))

    def test_events_outside_the_trip_do_not_count(self):
        late = [device_event(device_id="d5", ts=at(DAY, 20), type="disk_full")]
        self.assertNotIn("storage", self.flagged([field_take(self.it)], events=late))

    def test_expired_cal(self):
        line = config.EXPIRED_CAL_MIN_H * H
        stale = [field_take(self.it, camera_unit="77", recorded_s=line)]
        self.assertIn("expired_cal", self.flagged(stale))
        stale[0]["recorded_s"] = line - 1
        self.assertNotIn("expired_cal", self.flagged(stale))

    def test_worst_flag_sets_severity_and_comes_first(self):
        it = self.it
        row = row_for([field_take(it, score=90.0, recorded_s=self.line),
                       field_take(it, nominal_fps=30, recorded_s=2.1 * H, usb_path="1-1")], [it], latest="1.70.0")
        self.assertEqual(codes(row), ["fps30", "timing", "old_firmware"])
        self.assertEqual(row["severity"], "bad")
        self.assertEqual(row["flags"][0], {"code": "fps30", "severity": "bad", "label": "30 fps",
                                           "detail": "2.1 h at 30 fps: camera on USB 2"})

    def test_usb2_is_only_claimed_with_the_bus_to_show_for_it(self):
        # starcap133 ran 30 fps on 1-1; starcap13's firmware (1.37.2) reports no USB path
        for path, said in (("1-1", True), ("7-1", True), ("2-1", False), (None, False)):
            row = row_for([field_take(self.it, nominal_fps=30, recorded_s=H, usb_path=path)], [self.it])
            detail = next(f["detail"] for f in row["flags"] if f["code"] == "fps30")
            self.assertEqual(detail.endswith("camera on USB 2"), said, path)

    def test_takes_that_are_all_short_are_no_usable_footage(self):
        # starcap71 on 2026-10-10: out 12 h, back with 9 takes and 36 s in all
        row = row_for([field_take(self.it, recorded_s=4.0) for _ in range(9)], [self.it])
        self.assertEqual((row["status"], row["clips"]), ("ok", 0))
        self.assertEqual(row["flags"][0]["code"], "no_clips")
        self.assertEqual(rules.day_kpis([row])["caps_recorded"], 0)


class ExpiredCal(unittest.TestCase):
    def expired_h(self, versions, revs=(), **take_kw):
        it = trip()
        ctx = context(versions=versions, revs=revs)
        return row_for([field_take(it, recorded_s=3600, **take_kw)], [it], ctx=ctx)["expired_cal_hours"]

    def fit(self, key, day, h=10.0, **kw):
        return cal_version(camera_key=key, calibrated_at=at(day, h), **kw)

    def test_fresh_fit(self):
        self.assertEqual(self.expired_h([self.fit("43", "2026-10-01")], camera_unit="43"), 0)

    def test_thirty_days_is_expired(self):
        start = at(DAY, 10)
        on_the_line = cal_version(camera_key="43", calibrated_at=start - config.RECAL_DAYS * clock.DAY_S)
        just_inside = cal_version(camera_key="43", calibrated_at=start - config.RECAL_DAYS * clock.DAY_S + 60)
        self.assertEqual(self.expired_h([on_the_line], camera_unit="43", started_at=start), 1.0)
        self.assertEqual(self.expired_h([just_inside], camera_unit="43", started_at=start), 0)

    def test_fit_after_take_start_does_not_count(self):
        fits = [self.fit("43", "2026-09-01"), self.fit("43", DAY, 15)]
        self.assertEqual(self.expired_h(fits, camera_unit="43", started_at=at(DAY, 10)), 1.0)
        self.assertEqual(self.expired_h(fits, camera_unit="43", started_at=at(DAY, 16)), 0)

    def test_stale_recorder_clock_falls_back_to_checkout(self):
        # fitted an hour before check-out; the recorder thinks the take began the day before
        fits = [self.fit("43", DAY, 7)]
        self.assertEqual(self.expired_h(fits, camera_unit="43", started_at=at("2026-10-10", 20)), 0)
        self.assertEqual(self.expired_h(fits, camera_unit="43", started_at=None), 0)

    def test_failed_fit_does_not_count(self):
        self.assertEqual(self.expired_h([self.fit("43", "2026-10-01", verdict="FAIL")], camera_unit="43"), 1.0)
        self.assertEqual(self.expired_h([self.fit("43", "2026-10-01", verdict=None)], camera_unit="43"), 0)

    def test_relensed_camera_is_judged_on_its_new_lens(self):
        old_glass = [self.fit("88", "2026-10-05")]
        self.assertEqual(self.expired_h(old_glass, camera_unit="88.1"), 1.0)
        self.assertEqual(self.expired_h(old_glass + [self.fit("88.1", "2026-10-05")], camera_unit="88.1"), 0)

    def test_missing_unit_falls_back_to_item_camera_plus_revs(self):
        fits = [self.fit("43", "2026-10-05"), self.fit("43.1", "2026-09-01")]
        self.assertEqual(self.expired_h(fits, camera_unit=None), 0)               # no revs: "43"
        self.assertEqual(self.expired_h(fits, revs=["43.1"], camera_unit=None), 1.0)   # fitted lens 43.1

    def test_cal_days_left(self):
        it = trip()
        ctx = context(versions=[self.fit("43", "2026-10-01")])
        row = row_for([field_take(it, camera_unit="43")], [it], ctx=ctx)
        self.assertEqual(row["cal_days_left"], calib.days_left({"calibrated_at": at("2026-10-01", 10)}, NOW))


class OpenTicketsAndNotes(unittest.TestCase):
    def test_open_tickets_on_cap_or_camera(self):
        it = trip()
        tickets = [ticket(headset="starcap5", created_at=at("2026-10-01")),
                   ticket(subject="camera", camera="43", headset="starcap9", created_at=at("2026-10-01")),
                   ticket(headset="starcap5", status="resolved", created_at=at("2026-09-01"),
                          resolved_at=at("2026-09-02"))]
        row = row_for([field_take(it, camera_unit="43")], [it], ctx=context(tickets=tickets))
        self.assertEqual(row["open_tickets"], 2)

    def test_my_status(self):
        notes = [{"id": 2, "subject_type": "cap", "subject_id": "starcap5", "created_at": 2, "text": "",
                  "status": "watch"}]
        it = trip()
        self.assertEqual(row_for([], [it], ctx=context(notes=notes))["my_status"], "watch")


class FieldDay(unittest.TestCase):
    """Attribution through the real loaders' contract (fakestore) and api.day."""

    def setUp(self):
        it = item(id="i5", cpu_id="cpu5", camera_id="43", checkout_at=at(DAY, 9), returned_at=at(DAY, 18))
        self.store = FakeStore(
            items=[it], starcaps=[starcap(cpu_id="cpu5", name="starcap5", camera_id="43")],
            takes=[take(cpu_id="cpu5", trip_item_id="i5", started_at=at("2026-10-10", 20),
                        created_at=at(DAY, 20), recorded_s=3600),
                   take(cpu_id="cpu5", trip_item_id="i5", created_at=at("2026-10-12", 8), recorded_s=1800),
                   take(cpu_id="cpu7", trip_item_id=None, created_at=at(DAY, 11), recorded_s=720)])

    def day(self, date):
        with mock.patch.object(clock, "now", return_value=NOW):
            return api.day(self.store, date)

    def test_counts_on_checkout_day_whatever_the_clock_says(self):
        d = self.day(DAY)
        self.assertEqual(d["hours"]["total"], 1.5)
        self.assertEqual([c["cap"] for c in d["caps"]], ["starcap5"])
        self.assertEqual(self.day("2026-10-10")["hours"]["total"], 0)

    def test_off_trip_is_separate(self):
        d = self.day(DAY)
        self.assertEqual(d["off_trip"], {"takes": 1, "hours": 0.2, "caps": 1})
        self.assertNotIn("cpu7", [c["cpu_id"] for c in d["caps"]])


if __name__ == "__main__":
    unittest.main()
