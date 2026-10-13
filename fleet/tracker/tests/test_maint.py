"""Offline tests for maint.py: ticket numbers, the open trend, needs-attention, my statuses.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest

from fakestore import at, item, qc_run, ticket

import clock
import maint

DAY = "2026-10-11"
NOW = at("2026-10-12", 10)


def flag(code, severity):
    return {"code": code, "severity": severity, "label": code, "detail": code}


def row(cap, camera="43", *flags):
    return {"cap": cap, "camera": camera, "cameras": [camera], "flags": list(flags)}


def note(n, subject_id, status, text="", subject_type="cap"):
    return {"id": n, "subject_type": subject_type, "subject_id": subject_id, "created_at": float(n),
            "text": text, "status": status}


class Tickets(unittest.TestCase):
    def setUp(self):
        self.tickets = [
            ticket(id="old", headset="starcap1", critical=True, created_at=at("2026-09-01")),
            ticket(id="new", headset="starcap2", critical=False, created_at=at(DAY, 9)),
            ticket(id="crit", headset="starcap3", critical=True, created_at=at(DAY, 10)),
            ticket(id="done", headset="starcap4", status="resolved", created_at=at("2026-10-09", 12),
                   resolved_at=at(DAY, 12)),
            ticket(id="slow", headset="starcap4", status="resolved", created_at=at("2026-10-01", 12),
                   resolved_at=at("2026-10-05", 12)),
            ticket(id="ancient", headset="starcap5", status="resolved", created_at=at("2026-07-01"),
                   resolved_at=at("2026-08-01")),
            ticket(id="cam", subject="camera", camera="43", headset="starcap1", created_at=at(DAY, 11)),
        ]

    def test_stats(self):
        s = maint.stats(self.tickets, DAY, NOW, ["starcap1", "starcap3", "starcap1"], ["43"])
        self.assertEqual(s, {"open": 4, "critical": 2, "opened": 3, "resolved": 1,
                             "median_resolve_days": 3.0,            # 2 days and 4 days; July's is out of the window
                             "oldest_open_days": round((NOW - at("2026-09-01")) / clock.DAY_S, 1),
                             "blocked_caps": 2, "broken_cams": 1})

    def test_open_list_critical_then_oldest(self):
        self.assertEqual([t["id"] for t in maint.open_list(self.tickets, NOW)], ["old", "crit", "new", "cam"])

    def test_units(self):
        rows = {t["id"]: t for t in maint.open_list(self.tickets, NOW)}
        self.assertEqual((rows["cam"]["subject"], rows["cam"]["unit"]), ("camera", "43"))
        self.assertEqual(rows["old"]["unit"], "starcap1")
        legacy = maint.ticket_row(dict(ticket(headset="starcap8", created_at=NOW), subject=None), NOW)
        self.assertEqual((legacy["subject"], legacy["unit"]), ("starcap", "starcap8"))

    def test_trend_rebuilds_the_open_count(self):
        days = clock.days_between("2026-10-08", DAY)
        t = {r["date"]: r for r in maint.trend(self.tickets, days)}
        self.assertEqual(t["2026-10-08"]["open"], 1)                 # only "old"
        self.assertEqual(t["2026-10-09"]["open"], 2)                 # + "done", created that day
        self.assertEqual(t[DAY], {"date": DAY, "open": 4, "opened": 3, "resolved": 1})

    def test_reopened_ticket_is_open(self):
        reopened = ticket(headset="starcap6", created_at=at("2026-10-01"), status="open", resolved_at=None)
        self.assertTrue(maint.is_open(reopened))
        self.assertEqual(maint.trend([reopened], [DAY])[0]["open"], 1)

    def test_repeat_offenders(self):
        since = clock.day_bounds("2026-09-12")[0]
        rep = maint.repeat_offenders(self.tickets, since, clock.day_bounds(DAY)[1])
        self.assertEqual(rep, [{"unit": "starcap4", "subject": "starcap", "tickets_30d": 2, "open": 0,
                                "last_at": at("2026-10-09", 12)}])

    def test_by_category(self):
        tickets = self.tickets + [ticket(category="software", created_at=at(DAY))]
        cats = maint.by_category(tickets, clock.day_bounds("2026-09-12")[0], clock.day_bounds(DAY)[1])
        self.assertEqual(cats, [{"category": "hardware", "open": 4, "last_30d": 5},
                                {"category": "software", "open": 1, "last_30d": 1}])

    def test_unit_tickets(self):
        on_cap = [t["id"] for t in maint.unit_tickets(self.tickets, NOW, cap="starcap1")]
        on_cam = [t["id"] for t in maint.unit_tickets(self.tickets, NOW, camera="43")]
        self.assertEqual((on_cap, on_cam), (["old"], ["cam"]))


class NeedsAttention(unittest.TestCase):
    def attention(self, days, tickets=(), notes=()):
        return maint.needs_attention(days, maint.OpenTickets(tickets), maint.current_statuses(notes))

    def test_merges_flags_and_days(self):
        days = [("2026-10-09", [row("starcap5", "16", flag("timing", "warn"))]),
                ("2026-10-10", [row("starcap5", "16", flag("old_firmware", "info"))]),
                (DAY, [row("starcap5", "16.1", flag("fps30", "bad"), flag("timing", "warn"))])]
        out = self.attention(days)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual((e["cap"], e["camera"], e["severity"]), ("starcap5", "16.1", "bad"))
        self.assertEqual([f["code"] for f in e["flags"]], ["fps30", "timing"])
        self.assertEqual(e["days"], ["2026-10-09", DAY])

    def test_info_only_is_not_attention(self):
        self.assertEqual(self.attention([(DAY, [row("starcap5", "16", flag("old_firmware", "info"))])]), [])

    def test_warn_only_must_repeat_but_one_bad_day_is_enough(self):
        once = [(DAY, [row("starcap5", "16", flag("camera_drops", "warn"))])]
        self.assertEqual(self.attention(once), [])
        twice = [("2026-10-10", [row("starcap5", "16", flag("reboots", "warn"))])] + once
        self.assertEqual([e["days"] for e in self.attention(twice)], [["2026-10-10", DAY]])
        self.assertEqual(len(self.attention([(DAY, [row("starcap5", "16", flag("no_footage", "bad"))])])), 1)

    def test_expired_calibration_is_not_a_repair(self):
        # the calibration page's queue owns an expired lens; it is not a ticket to file
        self.assertEqual(self.attention([(DAY, [row("starcap5", "16", flag("expired_cal", "warn"))])]), [])
        mixed = self.attention([(DAY, [row("starcap5", "16", flag("expired_cal", "warn"),
                                           flag("fps30", "bad"))])])
        self.assertEqual([f["code"] for f in mixed[0]["flags"]], ["fps30"])

    def test_open_ticket_on_the_cap_excludes(self):
        days = [(DAY, [row("starcap5", "16", flag("fps30", "bad"))])]
        open_on_cap = [ticket(headset="starcap5", created_at=at(DAY))]
        self.assertEqual(self.attention(days, open_on_cap), [])
        closed = [ticket(headset="starcap5", created_at=at(DAY), status="resolved", resolved_at=at(DAY, 13))]
        self.assertEqual(len(self.attention(days, closed)), 1)

    def test_open_ticket_on_its_camera_excludes(self):
        days = [(DAY, [row("starcap5", "16.1", flag("fps30", "bad"))])]
        self.assertEqual(self.attention(days, [ticket(subject="camera", camera="16", headset="starcap9",
                                                      created_at=at(DAY))]), [])

    def test_a_camera_ticket_only_names_the_cap_as_provenance(self):
        # camera 7 broke on starcap5 and moved on; starcap5 now carries camera 16
        days = [(DAY, [row("starcap5", "16", flag("fps30", "bad"))])]
        provenance = [ticket(subject="camera", camera="7", headset="starcap5", created_at=at(DAY))]
        self.assertEqual(len(self.attention(days, provenance)), 1)

    def test_my_status_parks_it(self):
        days = [(DAY, [row("starcap5", "16", flag("fps30", "bad"))])]
        for status in maint.PARKED:
            self.assertEqual(self.attention(days, notes=[note(1, "starcap5", status)]), [], status)
        self.assertEqual(len(self.attention(days, notes=[note(1, "starcap5", "watch")])), 1)
        cleared = [note(2, "starcap5", "ok"), note(1, "starcap5", "repair")]
        self.assertEqual(len(self.attention(days, notes=cleared)), 1)

    def test_order(self):
        # bad first, then the most days flagged, then by name
        timing = flag("timing", "warn")
        days = [("2026-10-09", [row("starcap2", "2", timing)]),
                ("2026-10-10", [row("starcap2", "2", timing), row("starcap10", "10", timing)]),
                (DAY, [row("starcap2", "2", timing), row("starcap10", "10", timing),
                       row("starcap3", "3", flag("no_footage", "bad"))])]
        self.assertEqual([e["cap"] for e in self.attention(days)], ["starcap3", "starcap2", "starcap10"])


class Notes(unittest.TestCase):
    def test_current_status(self):
        notes = [note(5, "starcap1", None, "just a remark"), note(4, "starcap1", "repair", "sent to Ali"),
                 note(3, "starcap1", "watch"), note(2, "starcap2", "ok"), note(1, "starcap2", "dead"),
                 note(6, "43.1", "lost", subject_type="camera")]
        statuses = maint.current_statuses(sorted(notes, key=lambda n: -n["created_at"]))
        self.assertEqual({k: n["status"] for k, n in statuses.items()},
                         {("cap", "starcap1"): "repair", ("camera", "43.1"): "lost"})
        self.assertEqual(maint.status_list(statuses)[0],
                         {"subject_type": "camera", "subject_id": "43.1", "status": "lost", "since": 6.0, "text": ""})


class InShop(unittest.TestCase):
    """What each unit in maintenance has done since its ticket."""
    CAPS = {f"starcap{n}": f"cpu{n}" for n in range(1, 10)}
    OPENED = at("2026-10-01")

    def shop(self, tickets, items=(), heard=None, qc=(), statuses=None, trips=None, blocked=(), broken=(),
             heard_at=NOW):
        rows = maint.in_shop(tickets, NOW, heard_at, self.CAPS, list(items), heard or {}, qc_by_cpu(qc),
                             statuses or {}, trips or {}, list(blocked), list(broken))
        return {r["unit"]: r for r in rows}

    def t(self, cap, **kw):
        return ticket(headset=cap, critical=True, created_at=self.OPENED, **kw)

    def test_each_verdict(self):
        tickets = [self.t(f"starcap{n}") for n in range(1, 9)]
        items = [item(cpu_id="cpu1", checkout_at=at("2026-10-11"), returned_at=None),          # out now
                 item(cpu_id="cpu2", checkout_at=at("2026-10-05"), returned_at=at("2026-10-05", 20))]
        heard = {"cpu5": NOW - 30, "cpu6": at("2026-10-10"), "cpu7": at("2026-10-02"), "cpu8": at("2026-09-20")}
        qc = [qc_run(cpu_id="cpu3", verdict="fail", finished_at=at("2026-10-08"), failed_steps=["Camera"]),
              qc_run(cpu_id="cpu4", verdict="pass", finished_at=at("2026-10-09")),
              qc_run(cpu_id="cpu8", verdict="pass", finished_at=at("2026-09-25"))]               # before the ticket
        got = {u: (r["verdict"]["severity"], r["verdict"]["text"]) for u, r in self.shop(tickets, items, heard, qc).items()}
        self.assertEqual(got, {
            "starcap1": ("bad", "Out in the field now"),
            "starcap2": ("warn", "Went out 1× since the ticket"),
            "starcap3": ("bad", "Failed QC since the ticket"),
            "starcap4": ("ok", "Passed QC since the ticket"),
            "starcap5": ("info", "Online now"),
            "starcap6": ("info", "Powered on since the ticket"),
            "starcap7": ("warn", "Quiet for 9 d"),
            "starcap8": ("warn", "Untouched since the ticket")})

    def test_worst_first_then_oldest(self):
        tickets = [self.t("starcap1"), ticket(headset="starcap2", created_at=at("2026-09-01")),
                   ticket(headset="starcap3", created_at=at("2026-10-12", 6))]                    # just reported
        rows = maint.in_shop(tickets, NOW, NOW, self.CAPS, [], {}, {}, {}, {}, [], [])
        self.assertEqual([r["unit"] for r in rows], ["starcap1", "starcap2", "starcap3"])         # critical first
        self.assertEqual(rows[2]["verdict"]["text"], "Just reported")

    def test_tickets_are_grouped_per_unit_and_blocked_and_broken_units_show_without_one(self):
        tickets = [self.t("starcap1", description="type c"), self.t("starcap1", description="loose cable"),
                   ticket(subject="camera", camera="43", headset="starcap2", created_at=self.OPENED)]
        rows = self.shop(tickets, blocked=["starcap1", "starcap9"], broken=["43", "50"],
                         items=[item(cpu_id="cpu3", camera_id="50", checkout_at=at("2026-10-11"), returned_at=None)])
        self.assertEqual([t["description"] for t in rows["starcap1"]["tickets"]], ["type c", "loose cable"])
        self.assertEqual((rows["starcap9"]["tickets"], rows["starcap9"]["verdict"]["text"]), ([], "Blocked, no open ticket"))
        self.assertEqual((rows["43"]["on_cap"], rows["43"]["out_now"], rows["43"]["broken"]), ("starcap2", False, True))
        self.assertEqual((rows["50"]["on_cap"], rows["50"]["verdict"]["severity"]), ("starcap3", "bad"))   # broken, in use

    def test_my_status_and_last_trip_ride_along(self):
        trip = {"day": "2026-09-30", "checkout_at": at("2026-09-30"), "hours": 4.2, "good_pct": 50.0, "flags": []}
        rows = self.shop([self.t("starcap1")], statuses={("cap", "starcap1"): note(5, "starcap1", "repair", "at the shop")},
                         trips={"starcap1": trip})
        self.assertEqual(rows["starcap1"]["my_status"], {"status": "repair", "text": "at the shop", "since": 5.0})
        self.assertEqual(rows["starcap1"]["last_trip"], trip)


def qc_by_cpu(runs):
    import qc
    return qc.by_cpu(runs)


if __name__ == "__main__":
    unittest.main()
