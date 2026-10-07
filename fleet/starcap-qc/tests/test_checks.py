"""Offline tests for the pure measurements (checks.py) and the calibration rules.

    cd fleet/starcap-qc && python3 -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import calibration  # noqa: E402
import checks  # noqa: E402
import qc  # noqa: E402

T = 1 / 60.0


class Timing(unittest.TestCase):
    def test_perfect_60(self):
        r = checks.frame_timing(np.arange(3600) * T, 60)
        self.assertTrue(r["pass"])
        self.assertEqual(r["outside_tol"], 0)
        self.assertAlmostEqual(r["avg_fps"], 60.0, places=6)

    def test_camera_period_is_not_nominal(self):
        # a real DECXIN runs 16.626-16.640 ms, not 16.667: that is 0.03 ms off, fine
        r = checks.frame_timing(np.arange(3600) * 0.016626, 60)
        self.assertTrue(r["pass"])
        self.assertLess(r["p99_dev_ms"], 0.1)

    def test_one_percent_is_the_line(self):
        dt = np.full(10000, T)
        dt[:100] = 2 * T                           # exactly 1.00% dropped -> allowed
        self.assertTrue(checks.interval_timing([dt], 60)["pass"])
        dt[:101] = 2 * T                           # 1.01% -> fails
        r = checks.interval_timing([dt], 60)
        self.assertFalse(r["pass"])
        self.assertIn("1.01%", r["reason"])

    def test_firmware_glitch_costs_one_interval(self):
        # [9.984, 9.984, 29.8] ms: two short intervals inside the band, one late outside
        dt = np.full(1000, T)
        dt[10:13] = [0.009984, 0.009984, 0.0298]
        r = checks.interval_timing([dt], 60)
        self.assertEqual(r["outside_tol"], 1)

    def test_repeated_stamp_fails(self):
        ts = np.arange(1000) * T
        ts[500] = ts[499]
        r = checks.frame_timing(ts, 60)
        self.assertFalse(r["pass"])
        self.assertIn("repeat or go backwards", r["reason"])

    def test_split_files_do_not_invent_a_gap(self):
        a = np.diff(np.arange(1000) * T)
        b = np.diff(500 + np.arange(1000) * T)      # a second file, its clock far away
        r = checks.interval_timing([a, b], 60)
        self.assertTrue(r["pass"])
        self.assertEqual(r["late_gaps"], 0)

    def test_too_few(self):
        self.assertFalse(checks.frame_timing([0.0], 60)["pass"])

    def test_unwrap_u32(self):
        us = (np.arange(1000) * 16640 + (2 ** 32 - 300000)) % (2 ** 32)
        un = checks.unwrap_u32(us)
        self.assertTrue(np.all(np.diff(un) == 16640))
        r = checks.frame_timing(un / 1e6, 60)
        self.assertTrue(r["pass"])


class Imu(unittest.TestCase):
    def _imu(self, n=6000, rate=600.0, g=(0.0, -9.81, 0.0)):
        t = np.arange(n) / rate
        a = np.tile(g, (n, 1)) + np.random.default_rng(1).normal(0, 0.02, (n, 3))
        w = np.random.default_rng(2).normal(0, 0.2, (n, 3))
        return t, a, w

    def test_healthy(self):
        t, a, w = self._imu()
        r = checks.imu_stats(t, a, w)
        self.assertAlmostEqual(r["rate_hz"], 600.0, places=3)
        self.assertAlmostEqual(r["gravity_ms2"], 9.81, delta=0.05)
        self.assertFalse(r["accel_flat"])

    def test_stuck_sensor(self):
        t, a, w = self._imu()
        a[:] = [0.1, 0.2, 0.3]
        self.assertTrue(checks.imu_stats(t, a, w)["accel_flat"])

    def test_stuck_gyro(self):
        t, a, w = self._imu()
        w[:] = 0.0
        r = checks.imu_stats(t, a, w)
        self.assertTrue(r["gyro_flat"])
        self.assertFalse(checks.imu_stats(*self._imu())["gyro_flat"])

    def test_gravity_from_still_stretches(self):
        t, a, w = self._imu()
        a[:3000] *= 2.5                            # handled: big accelerations...
        w[:3000] = 50.0                            # ...while rotating
        r = checks.imu_stats(t, a, w)
        self.assertAlmostEqual(r["gravity_ms2"], 9.81, delta=0.05)


class Eyes(unittest.TestCase):
    def _scene(self, seed):
        rng = np.random.default_rng(seed)
        img = rng.integers(20, 230, (1200, 1920, 3), dtype=np.uint8)
        return img

    def _m(self, img):
        return checks.eye_metrics(img)

    def test_raw_eyes_columns(self):
        f = np.zeros((1200, 4000), np.uint8)
        f[:, 160:2080] = 1                         # right lens
        f[:, 2080:] = 2                            # left lens
        left, right = checks.raw_eyes(f)
        self.assertEqual(left.shape, (1200, 1920))
        self.assertTrue(np.all(left == 2) and np.all(right == 1))

    def test_healthy_pair(self):
        a, b = self._scene(1), self._scene(2)
        p, n = checks.judge_eyes([self._m(a), self._m(b)], [3.0, 3.0])
        self.assertEqual((p, n), ([], []))

    def test_one_black_eye_fails(self):
        a = self._scene(1)
        black = np.full_like(a, 3)
        p, _n = checks.judge_eyes([self._m(a), self._m(black)], [3.0, 0.5])
        self.assertTrue(p and "right eye" in p[0] and "black" in p[0])

    def test_one_frozen_eye_fails(self):
        a, b = self._scene(1), self._scene(2)
        p, _n = checks.judge_eyes([self._m(a), self._m(b)], [3.0, 0.0])
        self.assertTrue(p and "right eye" in p[0] and "frozen" in p[0])

    def test_both_dark_or_flat_fails_with_a_hint(self):
        # QC can't vouch for eyes it can't see: lens caps, a dark room or a blank wall
        # fail too, with a message saying what to do
        wall = np.full((1200, 1920, 3), 232, np.uint8)
        p, _n = checks.judge_eyes([self._m(wall), self._m(wall)], [0.4, 0.4])
        self.assertTrue(p and "lens caps" in p[0], p)
        dark = np.full((1200, 1920, 3), 2, np.uint8)
        p, _n = checks.judge_eyes([self._m(dark), self._m(wall)], [0.0, 0.4])
        self.assertTrue(p and "neither eye" in p[0], p)

    def test_both_frozen_fails(self):
        a, b = self._scene(1), self._scene(2)
        p, _n = checks.judge_eyes([self._m(a), self._m(b)], [0.0, 0.0])
        self.assertTrue(p and "both eyes are frozen" in p[0])

    def test_motion(self):
        a = self._scene(1)
        self.assertEqual(checks.frame_motion(a, a.copy()), 0.0)
        self.assertGreater(checks.frame_motion(a, self._scene(2)), 1.0)


# camera 167's real document (dashboard, 2026-10): a 175-degree lens on KB4
DOC_167 = {
    "camera": "167", "model": "fisheye_kb4", "method": "opencv_fisheye_kb4",
    "K0": [[679.1459516967163, 0, 963.5189080694666], [0, 679.4946681616613, 618.179794208755], [0, 0, 1]],
    "dist0": [0.04852425311014523, -0.015477288901833225, -0.0068689863441648595, 0.0030295860762137683],
    "K1": [[681.1557759298513, 0, 977.0007463316936], [0, 681.4607963317959, 624.1910730601137], [0, 0, 1]],
    "dist1": [0.05057654605371113, -0.018921300564595893, -0.0040538366784861795, 0.0023983864153354056],
    "T": [-60.05658965139833, 0.15252833997217582, 0.06427471622366941], "baseline_mm": 60.06,
    "rmse": {"stereo": 0.309}, "lens_size": [1920, 1200],
    "fov_deg": {"camera0": {"h": 157.38, "v": 98.58}, "camera1": {"h": 156.52, "v": 98.25}},
    "evidence": {"status": "complete", "acceptance": {"verdict": "PASS"},
                 "files": [{"path": "calibration.json"}, {"path": "recordings/A/left.mp4"},
                           {"path": "recordings/B/left.mp4"}, {"path": "fit_images/0001.png"}]},
}


class Calibration(unittest.TestCase):
    def setUp(self):
        self._fetch = calibration.fetch

    def tearDown(self):
        calibration.fetch = self._fetch

    def _with(self, doc, err=None, **over):
        d = dict(doc, **over) if doc else doc
        calibration.fetch = lambda cid: (d, err)
        return calibration.check("167", qc.LIMITS)

    def test_fov_matches_the_bench(self):
        W, H = DOC_167["lens_size"]
        for eye, K, D in (("camera0", "K0", "dist0"), ("camera1", "K1", "dist1")):
            h, v, mono = calibration.kb4_fov(DOC_167[K], DOC_167[D], W, H)
            self.assertAlmostEqual(h, DOC_167["fov_deg"][eye]["h"], delta=0.01)
            self.assertAlmostEqual(v, DOC_167["fov_deg"][eye]["v"], delta=0.01)
            self.assertTrue(mono)

    def test_pass(self):
        r = self._with(DOC_167, calibrated_at=time.time() * 1000 - 3 * 86400e3)
        self.assertEqual(r["state"], "pass", r["summary"])

    def test_old_calibration_warns(self):
        r = self._with(DOC_167, calibrated_at=time.time() * 1000 - 45 * 86400e3)
        self.assertEqual(r["state"], "warn")

    def test_missing(self):
        self.assertEqual(self._with(None)["state"], "fail")

    def test_lookup_error_fails_not_passes(self):
        r = self._with(None, err="dashboard 402: billing")
        self.assertEqual(r["state"], "fail")
        self.assertIn("couldn't check", r["summary"])

    def test_legacy(self):
        r = self._with(DOC_167, model=None, method=None)
        self.assertEqual(r["state"], "fail")
        self.assertIn("legacy", r["summary"])

    def test_unfinished_upload_is_not_a_recalibration(self):
        # the shape the dashboard really stores: a declared bundle carries no verdict
        # until it completes, and that must read as an upload problem
        r = self._with(DOC_167, evidence={"status": "partial", "files": DOC_167["evidence"]["files"]})
        self.assertEqual(r["state"], "fail")
        self.assertIn("upload_audit.py --fix", r["summary"])
        self.assertNotIn("recalibrate it", r["summary"])

    def test_missing_required_media(self):
        # camera 189: "complete" with only the small files declared
        r = self._with(DOC_167, evidence={"status": "complete", "acceptance": {"verdict": "PASS"},
                                          "files": [{"path": "package.json"}]})
        self.assertEqual(r["state"], "fail")
        self.assertIn("recordings/A/", r["summary"])

    def test_verdict_spelling(self):
        ev = dict(DOC_167["evidence"], acceptance={"verdict": "  pass "})
        self.assertEqual(self._with(DOC_167, evidence=ev, calibrated_at=time.time() * 1000)["state"], "pass")
        ev = dict(DOC_167["evidence"], acceptance={"verdict": "PASS (DEVELOPMENT ONLY - NOT ACCEPTANCE EVIDENCE)"})
        r = self._with(DOC_167, evidence=ev)
        self.assertEqual(r["state"], "fail")
        self.assertIn("recalibrate", r["summary"])

    def test_narrow_lens_fails_fov(self):
        K = [[1600.0, 0, 960.0], [0, 1600.0, 600.0], [0, 0, 1]]
        r = self._with(DOC_167, K0=K, K1=K, dist0=[0, 0, 0, 0], dist1=[0, 0, 0, 0])
        self.assertEqual(r["state"], "fail")
        self.assertIn("under the spec", r["summary"])

    def test_baseline(self):
        r = self._with(DOC_167, baseline_mm=48.0)
        self.assertEqual(r["state"], "fail")
        self.assertIn("baseline", r["summary"])

    def test_eyes_reversed_in_calibration(self):
        r = self._with(DOC_167, T=[60.0, 0.1, 0.1])
        self.assertEqual(r["state"], "fail")
        self.assertIn("wrong way round", r["summary"])

    def test_no_camera_id(self):
        self.assertEqual(calibration.check(None, qc.LIMITS)["state"], "fail")


if __name__ == "__main__":
    unittest.main()
