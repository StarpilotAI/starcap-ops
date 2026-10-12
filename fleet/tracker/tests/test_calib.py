"""Offline tests for calib.py: fitted lenses, fit index, states and the bench plan.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest
from collections import Counter

from fakestore import at, cal_current, cal_version, camera, current_for, item, starcap, take

import calib
import clock
import config

TODAY = "2026-10-12"
NOW = at(TODAY, 9)


def day(n):
    return clock.shift_day(TODAY, n)


def due_fit(key, due_day, **kw):
    """A fit that falls due at noon on `due_day`."""
    return cal_version(camera_key=key, calibrated_at=at(due_day, 12) - config.RECAL_DAYS * clock.DAY_S, **kw)


def cams_due(due, broken=(), never=(), out_now=(), hours=None, revs=(), retired=()):
    """Cameras {id: due day}, plus fit-less `never` ones, with usage filled in."""
    rows = [camera(id=c) for c in [*due, *never]]
    versions = [due_fit(c, d) for c, d in due.items()]
    cal = calib.CalIndex(versions, current_for(versions))
    cams = calib.cameras(rows, retired, broken, calib.Lenses(revs), cal, NOW)
    use = {c: dict(calib.NO_USAGE, out_now=c in out_now, hours_7d=(hours or {}).get(c, 0.0)) for c in due}
    return calib.with_usage(cams, use)


def planned_on(plan):
    return Counter(c["plan_day"] for c in plan)


class Keys(unittest.TestCase):
    def test_natural_order(self):
        ids = ["199", "43.1", "9", "43", "starcap12", "starcap5"]
        self.assertEqual(sorted(ids, key=calib.natural_key), ["9", "43", "43.1", "199", "starcap5", "starcap12"])

    def test_split_key(self):
        self.assertEqual(calib.split_key("88.1"), ("88", 1))
        self.assertEqual(calib.split_key("88"), ("88", 0))
        self.assertEqual(calib.norm_key("88.0"), "88")

    def test_fitted_lens_is_the_highest_rev_anywhere(self):
        lenses = calib.Lenses(["61.1", "88.1"], [cal_current(camera_id="61", kind="wb", rev=2)])
        self.assertEqual(lenses.key("61"), "61.2")
        self.assertEqual(lenses.key("88"), "88.1")
        self.assertEqual(lenses.key("5"), "5")
        self.assertEqual(lenses.key("88.3"), "88.3")      # an explicit lens id is kept

    def test_take_key(self):
        lenses = calib.Lenses(["43.1"])
        self.assertEqual(calib.take_key({"camera_unit": "50", "item_camera_id": "43"}, lenses), "50")
        self.assertEqual(calib.take_key({"camera_unit": None, "item_camera_id": "43"}, lenses), "43.1")
        self.assertIsNone(calib.take_key({"camera_unit": None, "item_camera_id": None}, lenses))


class Fits(unittest.TestCase):
    def test_index_skips_failed_fits(self):
        good = cal_version(camera_key="43", calibrated_at=at("2026-10-01"))
        failed = cal_version(camera_key="43", calibrated_at=at("2026-10-05"), verdict="FAIL")
        spaced = cal_version(camera_key="43", calibrated_at=at("2026-10-03"), verdict=" pass ")
        cal = calib.CalIndex([failed, good, spaced], current_for([failed, good, spaced]))
        self.assertIs(cal.last("43"), spaced)
        self.assertIs(cal.at("43", at("2026-10-02")), good)
        self.assertIsNone(cal.at("43", at("2026-09-30")))
        self.assertIsNone(cal.last("44"))

    def test_a_fit_only_the_current_table_remembers_counts(self):
        # cameras 26, 75, 93: fitted in July/August, before calibration_versions began
        cal = calib.CalIndex([], [cal_current(camera_id="26", calibrated_at=at("2026-08-02"))])
        self.assertEqual(cal.last("26")["calibrated_at"], at("2026-08-02"))
        self.assertEqual(cal.at("26", at("2026-08-10"))["calibrated_at"], at("2026-08-02"))

    def test_history_without_a_current_row_is_no_calibration_now(self):
        # cameras 91, 97: one ungraded 08-11 version, no current row (dashboard: stereo_missing)
        v = cal_version(camera_key="91", calibrated_at=at("2026-08-11"), verdict=None)
        cal = calib.CalIndex([v])
        self.assertIsNone(cal.last("91"))
        self.assertIs(cal.at("91", at("2026-08-20")), v)                  # but it was in force then
        cams = calib.cameras([camera(id="91")], [], [], calib.Lenses([]), cal, NOW)
        self.assertEqual(cams[0]["state"], "never")

    def test_states_at_the_lines(self):
        def state(seconds_left):
            fit = {"calibrated_at": NOW + seconds_left - config.RECAL_DAYS * clock.DAY_S}
            return calib.state_of(calib.days_left(fit, NOW))
        self.assertEqual(state(0), "overdue")
        self.assertEqual(state(-3 * clock.DAY_S), "overdue")
        self.assertEqual(state(60), "due_soon")
        self.assertEqual(state(config.EXPIRY_WARN_DAYS * clock.DAY_S), "due_soon")
        self.assertEqual(state(config.EXPIRY_WARN_DAYS * clock.DAY_S + 60), "ok")
        self.assertEqual(calib.state_of(None), "never")

    def test_relensed_camera_without_a_fit_on_the_new_lens_is_never(self):
        cal = calib.CalIndex([cal_version(camera_key="88", calibrated_at=at("2026-10-10"))])
        cams = calib.cameras([camera(id="88")], [], [], calib.Lenses(["88.1"]), cal, NOW)
        self.assertEqual((cams[0]["camera"], cams[0]["state"]), ("88.1", "never"))

    def test_retired_broken_dragon(self):
        rows = [camera(id="1"), camera(id="2", retired=True), camera(id="3"), camera(id="4"),
                camera(id="dragon:abc")]
        cams = {c["base"]: c for c in calib.cameras(rows, ["3"], ["4"], calib.Lenses([]), calib.CalIndex([]), NOW)}
        self.assertEqual(sorted(cams), ["1", "2", "3", "4"])
        self.assertEqual([b for b, c in cams.items() if c["retired"]], ["2", "3"])
        self.assertTrue(cams["4"]["broken"])

    def test_fit_history_first_ever_and_bench(self):
        fits = calib.fit_history([
            cal_version(camera_key="88", calibrated_at=at("2026-09-01"), method="opencv_fisheye_kb4"),
            cal_version(camera_key="88", calibrated_at=at("2026-10-01"), method=None, verdict="FAIL"),
            cal_version(camera_key="88.1", calibrated_at=at("2026-10-08"), method="basalt_kb4"),
            cal_version(camera_key="7", calibrated_at=at("2026-10-08"), method="new_bench")])
        by = {(f["camera"], clock.cairo_date(f["calibrated_at"])): f for f in fits}
        self.assertTrue(by[("88", "2026-09-01")]["first_ever"])
        second = by[("88", "2026-10-01")]
        self.assertEqual((second["first_ever"], second["previous_at"], second["bench"]),
                         (False, at("2026-09-01"), "Charuco (old)"))
        self.assertTrue(by[("88.1", "2026-10-08")]["first_ever"])      # new glass, first fit of its own
        self.assertEqual(by[("88.1", "2026-10-08")]["bench"], "Basalt")
        self.assertEqual(by[("88", "2026-09-01")]["bench"], "Robot bench")
        self.assertEqual(by[("7", "2026-10-08")]["bench"], "new_bench")
        self.assertEqual([f["calibrated_at"] for f in fits], sorted((f["calibrated_at"] for f in fits), reverse=True))

    def test_day_fits_and_history(self):
        fits = calib.fit_history([cal_version(camera_key="1", calibrated_at=at("2026-09-01")),
                                  cal_version(camera_key="1", calibrated_at=at(TODAY, 10)),
                                  cal_version(camera_key="2", calibrated_at=at(TODAY, 11))])
        imus = calib.imu_rows([cal_current(camera_id="2", kind="imu", calibrated_at=at(TODAY, 12)),
                               cal_current(camera_id="1", kind="stereo", calibrated_at=at(TODAY, 12))])
        d = calib.day_fits(TODAY, fits, imus)
        self.assertEqual((d["done"], d["recal"], d["new"], d["imu"]), (2, 1, 1, 1))
        self.assertEqual(d["imu_fits"], [{"camera": "2", "calibrated_at": at(TODAY, 12)}])
        h = calib.history([day(-1), TODAY], fits, imus)
        self.assertEqual(h, [{"date": day(-1), "recal": 0, "new": 0, "imu": 0},
                             {"date": TODAY, "recal": 1, "new": 1, "imu": 1}])


class Plan(unittest.TestCase):
    def test_earliest_deadline_first(self):
        plan = calib.plan(cams_due({"a": day(3), "b": day(1), "c": day(2)}), TODAY, 0)
        self.assertEqual([c["camera"] for c in plan], ["b", "c", "a"])
        self.assertEqual({c["plan_day"] for c in plan}, {TODAY})

    def test_today_skips_cameras_out_in_the_field_and_ties_go_to_the_busiest(self):
        cams = cams_due({"quiet": day(2), "busy": day(2), "out": day(2)}, out_now=["out"],
                        hours={"quiet": 5.0, "busy": 30.0})
        plan = calib.plan(cams, TODAY, config.CAL_PER_DAY - 1)        # room for one today
        self.assertEqual([(c["camera"], c["plan_day"]) for c in plan],
                         [("busy", TODAY), ("quiet", day(1)), ("out", day(1))])

    def test_overdue_cameras_in_rotation_go_before_idle_ones(self):
        # 2026-10-12: two idle cameras 31 days overdue sat ahead of 11.1 with 27 h that week
        cams = cams_due({"idle": day(-31), "busy": day(-11), "quiet": day(-12)},
                        hours={"busy": 27.2, "quiet": 7.7})
        plan = calib.plan(cams, TODAY, config.CAL_PER_DAY - 2)        # room for two today
        self.assertEqual([(c["camera"], c["plan_day"]) for c in plan],
                         [("busy", TODAY), ("quiet", TODAY), ("idle", day(1))])

    def test_a_failed_camera_imu_fit_is_not_coverage(self):
        rows = [cal_current(camera_id="106", kind="imu", calibrated_at=NOW, verdict="FAIL"),
                cal_current(camera_id="107", kind="imu", calibrated_at=NOW, verdict="PASS"),
                cal_current(camera_id="108", kind="imu", calibrated_at=NOW, verdict=None)]
        self.assertEqual([r["camera_id"] for r in calib.imu_rows(rows)], ["107", "108"])

    def test_catch_when_back(self):
        cams = cams_due({"gone": day(-3), "soon": day(4), "fine": day(20), "home": day(-1)},
                        out_now=["gone", "soon", "fine"])
        back = calib.catch_when_back(calib.plan(cams, TODAY, 0))
        self.assertEqual([c["camera"] for c in back], ["gone", "soon"])

    def test_todays_capacity_counts_fits_already_done(self):
        plan = calib.plan(cams_due({f"c{i}": day(-1) for i in range(15)}), TODAY, 4)
        self.assertEqual(planned_on(plan), {TODAY: config.CAL_PER_DAY - 4, day(1): 9})
        self.assertTrue(all(c["late"] for c in plan))
        # a backlog being worked down clears; it is not "falling behind"
        self.assertEqual(calib.backlog(calib.forecast(plan, TODAY)), (day(1), None))

    def test_pull_forward_window(self):
        reach = config.PULL_FORWARD_DAYS
        plan = {c["camera"]: c for c in calib.plan(cams_due({"in": day(reach), "out": day(reach + 1)}), TODAY, 0)}
        self.assertEqual(plan["in"]["plan_day"], TODAY)
        self.assertEqual(plan["out"]["plan_day"], day(1))
        self.assertFalse(plan["in"]["late"] or plan["out"]["late"])

    def test_late_when_the_bench_cannot_keep_up(self):
        plan = calib.plan(cams_due({f"c{i}": day(1) for i in range(35)}), TODAY, 0)
        self.assertEqual(planned_on(plan), {TODAY: 10, day(1): 10, day(2): 10, day(3): 5})
        self.assertEqual(sum(c["late"] for c in plan), 15)
        self.assertEqual(calib.backlog(calib.forecast(plan, TODAY)), (TODAY, day(1)))

    def test_due_today_planned_today_is_not_late(self):
        plan = calib.plan(cams_due({"x": TODAY}), TODAY, 0)
        self.assertEqual((plan[0]["plan_day"], plan[0]["late"]), (TODAY, False))

    def test_broken_and_never_are_not_planned(self):
        plan = calib.plan(cams_due({"ok": day(1), "bad": day(-5)}, broken=["bad"], never=["new"]), TODAY, 0)
        self.assertEqual([c["camera"] for c in plan], ["ok"])

    def test_a_spike_spreads_over_the_days_before_it(self):
        spike_day = day(20)
        plan = calib.plan(cams_due({f"s{i}": spike_day for i in range(51)}), TODAY, 0)
        first = 20 - config.PULL_FORWARD_DAYS
        self.assertEqual(planned_on(plan), {day(first): 10, day(first + 1): 10, day(first + 2): 10,
                                            day(first + 3): 10, day(first + 4): 10, day(first + 5): 1})
        self.assertFalse(any(c["late"] for c in plan))
        days = calib.forecast(plan, TODAY)
        self.assertEqual(calib.spikes(days), [{"date": spike_day, "due": 51}])
        self.assertEqual(calib.backlog(days), (TODAY, None))

    def test_a_busy_day_is_not_a_spike(self):
        line = config.SPIKE_DAYS * config.CAL_PER_DAY
        days = calib.forecast(calib.plan(cams_due({**{f"a{i}": day(9) for i in range(line)},
                                                   **{f"b{i}": day(20) for i in range(line + 1)}}), TODAY, 0), TODAY)
        self.assertEqual(calib.spikes(days), [{"date": day(20), "due": line + 1}])

    def test_forecast(self):
        plan = calib.plan(cams_due({"old": day(-1), "now": TODAY, "soon": day(2)}), TODAY, config.CAL_PER_DAY - 1)
        days = {d["date"]: d for d in calib.forecast(plan, TODAY)}
        self.assertEqual(len(days), config.PLAN_DAYS)
        self.assertEqual(days[TODAY], {"date": TODAY, "due": 1, "planned": 1, "overdue_end": 1})
        self.assertEqual(days[day(1)], {"date": day(1), "due": 0, "planned": 2, "overdue_end": 0})


class Usage(unittest.TestCase):
    def setUp(self):
        self.caps = [starcap(cpu_id="c1", name="starcap1", camera_id="43"),
                     starcap(cpu_id="c2", name="starcap2", camera_id="50")]

    def use(self, takes=(), open_items=(), kamerat=None):
        return calib.usage(takes, open_items, kamerat or {}, self.caps, calib.Lenses(["43.1"]), NOW)

    def test_registry_kamerat_then_open_item(self):
        self.assertEqual(self.use()["43"]["on_cap"], "starcap1")
        u = self.use(kamerat={"starcap9": {"camera": "43.1", "operator_name": "Omar"}})
        self.assertEqual((u["43"]["on_cap"], u["43"]["out_now"]), ("starcap9", True))
        u = self.use(open_items=[item(cpu_id="c2", camera_id="43", checkout_at=at(TODAY, 8))],
                     kamerat={"starcap9": {"camera": "43"}})
        self.assertEqual((u["43"]["on_cap"], u["43"]["out_now"]), ("starcap2", True))
        self.assertFalse(self.use()["50"]["out_now"])

    def test_hours_7d_and_last_used(self):
        def ft(checkout, **kw):
            return dict(take(recorded_s=3600, **kw), checkout_at=checkout, returned_at=None, item_camera_id="43")
        u = self.use([ft(at(day(-2))), ft(at(day(-3)), camera_unit="43.1"), ft(at(day(-9)))])
        self.assertEqual(u["43"]["hours_7d"], 2.0)
        self.assertEqual(u["43"]["last_used"], day(-2))


class View(unittest.TestCase):
    def test_counts_queue_never_broken_coverage(self):
        due = {"ok": day(20), "soon": day(3), "late": day(-4), "bust": day(10)}
        rows = [camera(id=c) for c in [*due, "new", "gone"]]
        versions = [due_fit(c, d) for c, d in due.items()] + [cal_version(camera_key="gone", calibrated_at=NOW)]
        imus = [cal_current(camera_id="ok", kind="imu", calibrated_at=NOW),
                cal_current(camera_id="soon", kind="imu", calibrated_at=at("2026-08-01"))]
        cal = calib.CalIndex(versions, current_for(versions))
        cams = calib.cameras(rows, ["gone"], ["bust"], calib.Lenses([]), cal, NOW)
        v = calib.view(TODAY, TODAY, cams, {}, calib.fit_history(versions), calib.imu_rows(imus))
        c = v["counts"]
        self.assertEqual(c, {"active": 5, "ok": 1, "due_soon": 1, "overdue": 1, "never": 1, "late_in_plan": 1,
                             "retired": 1, "broken": 1})
        self.assertEqual(c["active"], c["ok"] + c["due_soon"] + c["overdue"] + c["never"] + c["broken"])
        self.assertEqual([q["camera"] for q in v["queue"]], ["late", "soon"])
        self.assertEqual([q["camera"] for q in v["plan_today"]], ["late", "soon"])
        self.assertEqual([n["camera"] for n in v["never"]], ["new"])
        self.assertEqual(v["broken"], ["bust"])
        self.assertEqual(v["coverage"], {"valid": 2, "active": 5, "valid_pct": 40.0, "imu": 2, "imu_pct": 40.0,
                                         "imu_outdated": 1})
        self.assertEqual(v["today"]["done"], 1)                      # camera "gone" was fitted today
        self.assertEqual(len(v["history"]), config.TREND_DAYS)


if __name__ == "__main__":
    unittest.main()
