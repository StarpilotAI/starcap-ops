"""Offline tests for fleet.py: who is out, for how long, and who has gone quiet.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest

from fakestore import at, device, item, starcap

import calib
import clock
import config
import fleet

NOW = at("2026-10-12", 12)
H = 3600.0


class FieldWatch(unittest.TestCase):
    def setUp(self):
        self.caps = [starcap(cpu_id=f"c{i}", name=f"starcap{i}") for i in range(1, 6)]
        self.lenses = calib.Lenses(["43.1"])

    def view(self, items, devices=(), kamerat=None, blocked=()):
        return fleet.view(NOW, NOW, self.caps, list(devices), items, kamerat or {}, list(blocked), self.lenses)

    def test_silent_counts_from_checkout_when_last_heard_before_it(self):
        out = at("2026-10-11", 12)
        v = self.view([item(cpu_id="c1", camera_id="43", checkout_at=out, returned_at=None)],
                      [device(device_id="d1", cpu_id="c1", last_seen=out - 5 * clock.DAY_S)])
        e = v["out"][0]
        self.assertEqual(e["silent_h"], round((NOW - out) / H, 2))
        self.assertEqual(e["last_heard"], out - 5 * clock.DAY_S)
        self.assertEqual(e["camera"], "43.1")

    def test_silent_counts_from_last_heard_when_it_spoke_in_the_field(self):
        out = at("2026-10-10", 12)
        heard = at("2026-10-11", 18)
        v = self.view([item(cpu_id="c1", checkout_at=out, returned_at=None)],
                      [device(device_id="d1", cpu_id="c1", last_seen=heard - H),
                       device(device_id="d2", cpu_id="c1", last_seen=heard)])
        self.assertEqual(v["out"][0]["silent_h"], round((NOW - heard) / H, 2))

    def test_never_heard_is_silent_since_checkout(self):
        out = at("2026-10-12", 6)
        v = self.view([item(cpu_id="c1", checkout_at=out, returned_at=None)])
        self.assertEqual((v["out"][0]["last_heard"], v["out"][0]["silent_h"]), (None, 6.0))

    def test_silent_line(self):
        line = config.SILENT_H * H
        v = self.view([item(cpu_id="c1", checkout_at=NOW - line - 60, returned_at=None),
                       item(cpu_id="c2", checkout_at=NOW - line, returned_at=None)])
        self.assertEqual([e["cap"] for e in v["silent"]], ["starcap1"])

    def test_long_out_line_and_order(self):
        line = config.LONG_TRIP_DAYS * clock.DAY_S
        v = self.view([item(cpu_id="c2", checkout_at=NOW - line - 60, returned_at=None),
                       item(cpu_id="c3", checkout_at=NOW - line, returned_at=None),
                       item(cpu_id="c1", checkout_at=NOW - line - 7200, returned_at=None)])
        self.assertEqual([e["cap"] for e in v["long_out"]], ["starcap1", "starcap2"])
        self.assertEqual([e["cap"] for e in v["out"]], ["starcap1", "starcap2", "starcap3"])

    def test_operator_comes_from_kamerat(self):
        v = self.view([item(cpu_id="c1", checkout_at=NOW - H, returned_at=None, task="Kitchen")],
                      kamerat={"starcap1": {"operator_name": "Mona", "checked_out_at": NOW - H}})
        self.assertEqual((v["out"][0]["operator"], v["out"][0]["task"]), ("Mona", "Kitchen"))

    def test_two_open_checkouts_show_the_older(self):
        v = self.view([item(cpu_id="c1", checkout_at=NOW - H, returned_at=None),
                       item(cpu_id="c1", checkout_at=NOW - 2 * H, returned_at=None)])
        self.assertEqual(len(v["out"]), 1)
        self.assertEqual(v["out"][0]["checkout_at"], NOW - 2 * H)

    def test_snapshot(self):
        items = [item(cpu_id="c1", checkout_at=NOW - H, returned_at=None),
                 item(cpu_id="c9", checkout_at=NOW - H, returned_at=None)]      # unnamed: not in the fleet
        devices = [device(device_id="d3", cpu_id="c3", last_seen=NOW - config.ONLINE_S + 1),
                   device(device_id="d4", cpu_id="c4", last_seen=NOW - config.ONLINE_S)]
        self.caps.append(starcap(cpu_id="c9", name=None))
        self.caps.append(starcap(cpu_id="c10", name="WALEEDTEST1"))              # a bench board: not fleet
        v = self.view(items, devices, blocked=["starcap4", "starcap5", "starcap4"])
        self.assertEqual(v["fleet"], {"total": 5, "out": 1, "office": 4, "online": 1, "blocked": 2})


if __name__ == "__main__":
    unittest.main()
