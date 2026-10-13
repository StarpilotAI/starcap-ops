"""Offline tests for qc.py: StarCap QC status per cap, NanoPi only.

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import unittest

from fakestore import at, qc_run, starcap

import qc

NANOPI, ORANGEPI = "FriendlyElec NanoPi R76S", "Orange Pi 5"


def status(cpu, board, runs):
    return qc.status(cpu, board, qc.by_cpu(runs), qc.qc_cpus(runs))


class Status(unittest.TestCase):
    def test_orange_pi_has_no_qc_status_at_all(self):
        self.assertIsNone(status("o1", ORANGEPI, []))
        self.assertIsNone(status("o1", None, []))                         # unknown board, QC never saw it

    def test_a_nanopi_qc_never_saw_is_never(self):
        self.assertEqual(status("n1", NANOPI, [])["status"], "never")

    def test_a_board_qc_saw_is_a_nanopi_even_with_no_board_on_record(self):
        runs = [qc_run(cpu_id="n2", verdict="pass", finished_at=at("2026-10-09", 12))]
        self.assertEqual(status("n2", None, runs)["status"], "pass")

    def test_cancelled_runs_count_for_nothing(self):
        runs = [qc_run(cpu_id="n3", verdict="cancelled", finished_at=at("2026-10-09", 12)),
                qc_run(cpu_id="n3", verdict=None, finished_at=at("2026-10-09", 13))]
        self.assertEqual(status("n3", None, runs)["status"], "never")      # seen, so NanoPi; never finished

    def test_the_latest_finished_run_decides(self):
        runs = [qc_run(cpu_id="n4", verdict="pass", finished_at=at("2026-10-08", 12)),
                qc_run(cpu_id="n4", verdict="fail", finished_at=at("2026-10-10", 12), failed_steps=["Soak", "Timing"]),
                qc_run(cpu_id="n4", verdict="cancelled", finished_at=at("2026-10-11", 12))]
        st = status("n4", NANOPI, runs)
        self.assertEqual((st["status"], st["at"], st["passed_at"], st["failed_steps"], st["runs"]),
                         ("fail", at("2026-10-10", 12), at("2026-10-08", 12), ["Soak", "Timing"], 2))

    def test_a_pass_clears_the_failed_steps(self):
        runs = [qc_run(cpu_id="n5", verdict="fail", finished_at=at("2026-10-08", 12), failed_steps=["Camera"]),
                qc_run(cpu_id="n5", verdict="pass", finished_at=at("2026-10-09", 12))]
        self.assertEqual(status("n5", NANOPI, runs)["failed_steps"], [])

    def test_board_kind(self):
        self.assertEqual([qc.board_kind(NANOPI), qc.board_kind(ORANGEPI), qc.board_kind("Orange Pi 5B"),
                          qc.board_kind(None, "x", {"x"}), qc.board_kind(None, "y", {"x"})],
                         ["NanoPi", "Orange Pi", "Orange Pi", "NanoPi", None])

    def test_history_is_newest_first_and_finished_only(self):
        runs = [qc_run(cpu_id="n6", verdict="fail", finished_at=at("2026-10-08", 12), failed_steps=["Camera"]),
                qc_run(cpu_id="n6", verdict="cancelled", finished_at=at("2026-10-08", 13)),
                qc_run(cpu_id="n6", verdict="pass", finished_at=at("2026-10-09", 12), version="1.67.0", camera_id="238")]
        h = qc.history("n6", qc.by_cpu(runs))
        self.assertEqual([(r["verdict"], r["failed_steps"]) for r in h], [("pass", []), ("fail", ["Camera"])])
        self.assertEqual((h[0]["version"], h[0]["camera"]), ("1.67.0", "238"))


class Overview(unittest.TestCase):
    def test_counts_and_who_still_needs_qc(self):
        caps = [starcap(cpu_id="a", name="starcap1", board=NANOPI),           # passed
                starcap(cpu_id="b", name="starcap2", board=NANOPI),           # never, in the office
                starcap(cpu_id="c", name="starcap3", board=NANOPI),           # failed, out
                starcap(cpu_id="d", name="starcap4", board=NANOPI),           # never, out
                starcap(cpu_id="e", name="starcap5", board=ORANGEPI),         # not QC'd by design
                starcap(cpu_id="f", name="WALEEDTEST1", board=NANOPI)]        # bench board, not fleet
        runs = [qc_run(cpu_id="a", verdict="pass", finished_at=at("2026-10-09", 12)),
                qc_run(cpu_id="c", verdict="fail", finished_at=at("2026-10-09", 12), failed_steps=["Wi-Fi"])]
        v = qc.overview(caps, runs, out_cpus={"c", "d"}, available=True)
        self.assertEqual((v["nanopi"], v["passed"], v["failed"], v["never"]), (4, 1, 1, 2))
        # out in the field first (failed before never), then the office
        self.assertEqual([(e["cap"], e["status"], e["out_now"]) for e in v["not_passed"]],
                         [("starcap3", "fail", True), ("starcap4", "never", True), ("starcap2", "never", False)])


if __name__ == "__main__":
    unittest.main()
