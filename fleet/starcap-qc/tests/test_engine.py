"""The whole QC engine against a simulated board (tests/fakeboard.py): the happy path,
each way a board can fail, a cancel, and above all the test-take rules:

  * every take QC caused is stopped and deleted, including ones it never claimed and
    ones a clock step renamed under it
  * a take QC did not cause is never stopped or deleted, renamed or not
  * a run whose clean-up failed does not pass, and its window stays on disk
  * an interrupted run's leftovers are removed later without touching newer footage

    cd fleet/starcap-qc && python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import qc  # noqa: E402
from fakeboard import FakeBoard, device  # noqa: E402


def run_qc(fb, minutes=0, stop_take_ok=False, cancel_after=None, timeout=120, pending=None, logs_root=None):
    done = {}
    run = qc.QCRun(device(fb), minutes, on_finish=lambda r: done.setdefault("r", r.report()),
                   stop_take_ok=stop_take_ok, logs_root=logs_root,
                   on_pending=(lambda r, rec: pending.append(rec)) if pending is not None else None)
    run.start()
    t_end = time.time() + timeout
    while run.active and time.time() < t_end:
        if cancel_after is not None and time.time() - run.started > cancel_after:
            run.cancel()
            cancel_after = None
        time.sleep(0.05)
    if run.active:
        raise AssertionError("QC run didn't finish:\n" + json.dumps(run.to_dict()["steps"], indent=1))
    # a run reads as finished only once its report is written (see QCRun._main)
    assert "r" in done, "the run finished before its report was saved"
    return run, done["r"]


def states(run):
    return {s["key"]: s["state"] for s in run.to_dict()["steps"]}


def summary(run, key):
    return next(s["summary"] for s in run.to_dict()["steps"] if s["key"] == key)


def dump(run):
    return json.dumps(run.to_dict()["steps"], indent=1)


class Engine(unittest.TestCase):
    def test_happy_path(self):
        fb = FakeBoard().install(self)
        pending = []
        run, rep = run_qc(fb, pending=pending)
        st = states(run)
        self.assertEqual(run.verdict, "pass", dump(run))
        for k in ("connect", "software", "storage", "camera", "calibration", "wifi", "image",
                  "button", "soak", "timing", "footage", "cleanup"):
            self.assertEqual(st[k], "pass", f"{k}: {summary(run, k)}")
        self.assertEqual(fb.takes, {}, "the QC take must be deleted")
        self.assertEqual(len(fb.deleted), 1)
        self.assertEqual(rep["verdict"], "pass")
        self.assertIn("timing", rep["context"])
        json.dumps(rep)                              # the report must always serialise
        self.assertTrue(run.frame_jpeg)
        # the window was saved while the take existed, and cleared once it was gone
        self.assertTrue(pending and pending[0] and pending[0]["window"]["boot_id"] == fb.boot_id)
        self.assertIsNone(pending[-1])

    def test_footage_deleted_logs_kept(self):
        # the operator's ask: after QC, the test footage is gone and the logs stay
        fb = FakeBoard().install(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run, rep = run_qc(fb, logs_root=tmp.name)
        self.assertEqual(run.verdict, "pass", dump(run))
        self.assertEqual(fb.takes, {}, "the footage must be deleted from the board")
        logs = Path(tmp.name) / run.stem
        take = fb.deleted[0]
        for f in ("frames.csv.gz", "imu.csv.gz", "metrics.csv", "device_info.json", "final_status.json"):
            self.assertTrue((logs / take / f).is_file(), f)
        self.assertTrue((logs / "stereocam.log").is_file())
        import gzip
        self.assertTrue(gzip.decompress((logs / take / "frames.csv.gz").read_bytes()).startswith(b"pts_ns,"))
        self.assertIn("logs are kept", summary(run, "cleanup"))
        self.assertTrue(run.to_dict()["has_logs"])
        self.assertIn(f"{take}/frames.csv.gz", rep["context"]["logs"]["files"])
        self.assertFalse(any(p.suffix == ".mp4" for p in logs.rglob("*")), "never any video")

    def test_logs_that_cant_be_kept_never_keep_the_footage(self):
        fb = FakeBoard().install(self)
        self.addCleanup(setattr, qc, "save_take_logs", qc.save_take_logs)

        def broken(ip, take, dest):
            raise RuntimeError("disk full")
        qc.save_take_logs = broken
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run, _ = run_qc(fb, logs_root=tmp.name)
        self.assertEqual(fb.takes, {})
        self.assertEqual(states(run)["cleanup"], "pass")
        self.assertIn("couldn't keep its logs", summary(run, "cleanup"))

    def test_camera_fps_is_not_skewed_by_slow_ssh(self):
        # the first live run read 65.7 fps on a 60.1 fps camera: the first frame count
        # was taken before a slow SSH call and the clock started after it
        fb = FakeBoard(ssh_delay=0.4).install(self)
        self.addCleanup(setattr, qc, "LIMITS", qc.LIMITS)
        qc.LIMITS = dict(qc.LIMITS, fps_window_s=2.0)
        run, rep = run_qc(fb)
        self.assertAlmostEqual(rep["context"]["camera"]["fps_measured"], 60.1, delta=0.4)
        self.assertEqual(run.verdict, "pass", dump(run))

    def test_no_button_press(self):
        fb = FakeBoard(press_after_s=999).install(self)
        run, _ = run_qc(fb)
        st = states(run)
        self.assertEqual(run.verdict, "fail")
        self.assertEqual(st["button"], "fail")
        self.assertEqual([st[k] for k in ("soak", "timing", "footage")], ["skip"] * 3)
        self.assertEqual(st["cleanup"], "skip")
        self.assertEqual(fb.takes, {})

    def test_press_in_the_grace_period_counts(self):
        # the countdown is 4 s here; a press at 5 s (inside the 3 s grace) still counts
        fb = FakeBoard(press_after_s=5.0).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["button"], "pass", dump(run))
        self.assertEqual(run.verdict, "pass")
        self.assertEqual(fb.takes, {})

    def test_fail_fast_on_usb2(self):
        fb = FakeBoard(usb_speed=480).install(self)
        run, _ = run_qc(fb)
        st = states(run)
        self.assertEqual(st["camera"], "fail")
        self.assertIn("USB", summary(run, "camera"))
        self.assertEqual(st["button"], "skip", "no button prompt once a quick check failed")
        self.assertEqual(st["soak"], "skip")
        self.assertEqual(fb.takes, {})

    def test_reboot_mid_soak(self):
        fb = FakeBoard(reboot_after_s=1.5).install(self)
        run, _ = run_qc(fb)
        st = states(run)
        self.assertEqual(st["soak"], "fail")
        self.assertIn("rebooted", summary(run, "soak"))
        self.assertEqual(st["timing"], "skip")
        self.assertEqual(st["cleanup"], "pass", summary(run, "cleanup"))
        self.assertEqual(fb.takes, {})

    def test_take_that_restarted_itself_is_also_removed(self):
        # a camera dropout: the recorder ends take 1 and resumes into take 2 on its own.
        # QC claimed only take 1; both are its test takes and both must go.
        fb = FakeBoard(resume_after_s=1.0).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["soak"], "fail")
        self.assertIn("restarted itself", summary(run, "soak"))
        self.assertEqual(states(run)["cleanup"], "pass", summary(run, "cleanup"))
        self.assertFalse(fb.recording, "the resumed take must be stopped")
        self.assertEqual(fb.takes, {})
        self.assertEqual(len(fb.deleted), 2)

    def test_clock_step_mid_soak_and_after_stop(self):
        # NTP steps the clock: the recorder renames the take while it records, and
        # again (all takes of this boot) once it has stopped
        fb = FakeBoard(rename_after_s=1.0, rename_after_stop=True).install(self)
        theirs = fb._new_take(preexisting=True)       # an operator take from this boot...
        fb._stop()
        run, _ = run_qc(fb, stop_take_ok=True)
        self.assertEqual(run.verdict, "pass", dump(run))
        self.assertEqual(states(run)["cleanup"], "pass", summary(run, "cleanup"))
        # ...renamed too, and still on the card: only QC's own take went
        left = list(fb.takes.values())
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["rid"], f"rec_fake_{theirs}")
        self.assertEqual(len(fb.deleted), 1)

    def test_cleanup_waits_for_the_recorders_own_check(self):
        fb = FakeBoard(finalize_s=1.5).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(run.verdict, "pass", dump(run))
        self.assertEqual(fb.takes, {})

    def test_a_stale_finalizing_marker_does_not_block_cleanup_forever(self):
        fb = FakeBoard(stale_finalizing=True).install(self)
        window = {"boot_id": fb.boot_id, "mono_start": fb.mono() - 1, "mono_last": fb.mono() + 60,
                  "pre_rids": [], "pre_runs": []}
        fb._new_take()
        fb._stop()
        t0 = time.time()
        state, msg, settled = qc.remove_qc_takes(fb.ip, window, 0, budget_s=33)
        self.assertEqual((state, settled), ("pass", True), msg)
        self.assertEqual(fb.takes, {})
        self.assertLess(time.time() - t0, 30)

    def test_failed_cleanup_fails_the_run_and_stays_pending(self):
        fb = FakeBoard(delete_refuses=True).install(self)
        self.addCleanup(setattr, qc, "remove_qc_takes", qc.remove_qc_takes)
        real = qc.remove_qc_takes
        qc.remove_qc_takes = lambda ip, window, minutes, note=None, budget_s=None: real(
            ip, window, minutes, note=note or (lambda s: None), budget_s=2)
        pending = []
        run, rep = run_qc(fb, pending=pending)
        self.assertEqual(states(run)["cleanup"], "fail")
        self.assertEqual(run.verdict, "fail", "a test take left on the card must fail the run")
        self.assertIn("Remove test take", run.headline)
        self.assertIsNotNone(pending[-1], "the window must stay on disk for a retry")
        self.assertEqual(rep["verdict"], "fail")

    def test_cancel_mid_soak(self):
        fb = FakeBoard().install(self)
        run, rep = run_qc(fb, cancel_after=2.5)
        self.assertEqual(run.verdict, "cancelled")
        self.assertEqual(states(run)["cleanup"], "pass", summary(run, "cleanup"))
        self.assertEqual(fb.takes, {}, "a cancelled run must still remove its take")
        self.assertEqual(rep["verdict"], "cancelled")

    def test_cancel_right_after_the_press(self):
        # Stop QC lands between the press starting the take and QC claiming it
        fb = FakeBoard(press_after_s=0.1).install(self)
        orig = qc.QCRun._claim

        def slow_claim(run_self, run_name, by):
            run_self.cancel()
            return orig(run_self, run_name, by)
        self.addCleanup(setattr, qc.QCRun, "_claim", orig)
        qc.QCRun._claim = slow_claim
        run, _ = run_qc(fb)
        self.assertEqual(run.verdict, "cancelled")
        self.assertFalse(fb.recording)
        self.assertEqual(fb.takes, {})

    def test_timing_fail(self):
        fb = FakeBoard(drop_pct=3.0).install(self)
        run, _ = run_qc(fb)
        st = states(run)
        self.assertEqual(st["timing"], "fail")
        self.assertIn("3.0", summary(run, "timing"))
        self.assertEqual(st["footage"], "pass", "footage is still checked after a timing failure")
        self.assertEqual(run.verdict, "fail")
        self.assertEqual(fb.takes, {})

    def test_recorder_csv_hole_with_complete_footage_passes(self):
        # starcap91, live run 3: the recorder's own frames.csv/imu.csv lost ~0.57 s of rows
        # (its logging branch is leaky) while the footage and its IMU strip were complete.
        # Delivery decodes the strip, so the unit is fine: QC must not fail it.
        fb = FakeBoard(csv_hole_ms=570).install(self)
        run, rep = run_qc(fb)
        self.assertEqual(run.verdict, "pass", dump(run))
        self.assertEqual(rep["context"]["imu"]["source"], "delivered")
        self.assertTrue(any("frames.csv has a 5" in d for d in
                            next(s for s in run.to_dict()["steps"] if s["key"] == "timing")["details"]))

    def test_delivery_refusing_the_strip_fails(self):
        fb = FakeBoard(refuse_strip="the episode's IMU has a 0.84 s hole").install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["timing"], "fail")
        self.assertIn("delivery would refuse", summary(run, "timing"))
        self.assertEqual(fb.takes, {})

    def test_without_the_delivery_decoder_it_falls_back_and_says_so(self):
        fb = FakeBoard(delivery_available=False).install(self)
        run, rep = run_qc(fb)
        self.assertEqual(states(run)["timing"], "warn", summary(run, "timing"))
        self.assertIn("recorder's frames.csv", summary(run, "timing"))
        self.assertEqual(rep["context"]["imu"]["source"], "recorder imu.csv")
        self.assertEqual(run.verdict, "pass")

    def test_many_duplicate_stamps_fail_with_the_exposure_hint(self):
        # (the fallback path: delivered timelines are refused by the decoder instead)
        fb = FakeBoard(dup_stamps=40, delivery_available=False).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["timing"], "fail")
        self.assertIn("exposure", summary(run, "timing"))

    def test_a_single_duplicate_stamp_is_tolerated(self):
        fb = FakeBoard(dup_stamps=1, delivery_available=False).install(self)
        self.addCleanup(setattr, qc, "SOAK_EXTRA_S", qc.SOAK_EXTRA_S)
        qc.SOAK_EXTRA_S = 18                          # ~1080 frames: one repeat is under 0.1%
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["timing"], "warn", summary(run, "timing"))

    def test_stuck_accelerometer(self):
        fb = FakeBoard(stuck_accel=True).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["footage"], "fail")
        self.assertIn("accelerometer", summary(run, "footage"))

    def test_stuck_gyro(self):
        fb = FakeBoard(stuck_gyro=True).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["footage"], "fail")
        self.assertIn("gyroscope", summary(run, "footage"))

    def test_no_calibration(self):
        fb = FakeBoard(calibration_state="none").install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["calibration"], "fail")
        self.assertEqual(states(run)["soak"], "skip")

    def test_stale_sd_loader(self):
        fb = FakeBoard(sd_loader_bytes=4096).install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["storage"], "fail")
        self.assertIn("bootloader", summary(run, "storage"))

    def test_wifi_hears_nothing(self):
        fb = FakeBoard(wifi_seen=0).install(self)
        fb_ssh = fb.ssh

        def no_carrier(ip, script, timeout=30):
            r = fb_ssh(ip, script, timeout)
            if "phy80211" in script:
                r.stdout = r.stdout.replace("CARRIER=1", "CARRIER=0").replace("SSID=Starpilot-5G-1", "SSID=--")
            return r
        import board
        self.addCleanup(setattr, board, "ssh", board.ssh)
        board.ssh = no_carrier
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["wifi"], "fail")
        self.assertIn("antenna", summary(run, "wifi"))

    def test_implicit_software_is_a_warning_not_a_fail(self):
        fb = FakeBoard(software_label="IMPLICIT").install(self)
        run, _ = run_qc(fb)
        self.assertEqual(states(run)["software"], "warn")
        self.assertEqual(run.verdict, "pass")
        self.assertIn("Worth a look", run.headline)

    def test_existing_take_is_never_touched_without_consent(self):
        fb = FakeBoard().install(self)
        theirs = fb._new_take(preexisting=True)
        run, _ = run_qc(fb, stop_take_ok=False)
        self.assertEqual(run.verdict, "fail")
        self.assertIn("recording a take", summary(run, "image"))
        self.assertTrue(fb.recording and fb.dir == theirs, "their take must still be running")
        self.assertIn(theirs, fb.takes)

    def test_existing_take_is_stopped_and_kept_with_consent(self):
        fb = FakeBoard().install(self)
        theirs = fb._new_take(preexisting=True)
        run, rep = run_qc(fb, stop_take_ok=True)
        self.assertEqual(run.verdict, "pass", dump(run))
        self.assertIn(theirs, fb.takes, "the operator's own take stays on the card")
        self.assertEqual(list(fb.takes), [theirs], "only the QC take is deleted")
        self.assertEqual(rep["context"].get("stopped_existing_take"), theirs)


class Recovery(unittest.TestCase):
    """remove_qc_takes() for a run that was cut short: only takes from its window go."""

    def test_leftover_removed_newer_footage_kept(self):
        fb = FakeBoard().install(self)
        before = fb._new_take()
        fb._stop()
        window = {"boot_id": fb.boot_id, "mono_start": fb.mono(), "mono_last": fb.mono() + 30,
                  "pre_rids": [fb.takes[before]["rid"]], "pre_runs": [before]}
        leftover = fb._new_take()                    # the interrupted run's test take, still recording
        later = fb.mono() + 600
        fb._stop()
        newer = fb._new_take()                       # footage recorded after QC died...
        fb.takes[newer]["started_mono"] = later      # ...ten minutes later
        fb._stop()
        other_boot = fb._new_take()
        fb.takes[other_boot]["boot_id"] = "another-boot"
        fb._stop()
        state, msg, settled = qc.remove_qc_takes(fb.ip, window, 5, budget_s=20)
        self.assertEqual((state, settled), ("pass", True), msg)
        self.assertEqual(fb.deleted, [leftover])
        self.assertEqual(set(fb.takes), {before, newer, other_boot})

    def test_still_recording_leftover_is_stopped_first(self):
        fb = FakeBoard().install(self)
        window = {"boot_id": fb.boot_id, "mono_start": fb.mono() - 1, "mono_last": fb.mono() + 30,
                  "pre_rids": [], "pre_runs": []}
        fb._new_take()
        state, msg, _settled = qc.remove_qc_takes(fb.ip, window, 5, budget_s=20)
        self.assertEqual(state, "pass", msg)
        self.assertFalse(fb.recording)
        self.assertEqual(fb.takes, {})

    def test_never_stops_someone_elses_take(self):
        fb = FakeBoard().install(self)
        window = {"boot_id": fb.boot_id, "mono_start": fb.mono() - 100, "mono_last": fb.mono() - 50,
                  "pre_rids": [], "pre_runs": []}
        theirs = fb._new_take()                      # started after the window closed
        state, _msg, settled = qc.remove_qc_takes(fb.ip, window, 5, budget_s=10)
        self.assertEqual((state, settled), ("skip", True))
        self.assertTrue(fb.recording and fb.dir == theirs)

    def test_unidentified_new_take_fails_and_is_kept(self):
        fb = FakeBoard().install(self)
        window = {"boot_id": fb.boot_id, "mono_start": fb.mono(), "mono_last": fb.mono() + 30,
                  "pre_rids": [], "pre_runs": []}
        run = fb._new_take()
        fb._stop()
        t = fb.takes[run]
        t["boot_id"] = t["started_mono"] = t["rid"] = None   # no device_info.json
        state, msg, settled = qc.remove_qc_takes(fb.ip, window, 5, budget_s=10)
        self.assertEqual((state, settled), ("fail", True))
        self.assertIn(run, msg)
        self.assertIn(run, fb.takes, "never deleted blind")


class Describe(unittest.TestCase):
    """The dock scanner's card for one address."""

    def setUp(self):
        import board
        self.board = board
        board._IDENT.clear()
        self.addCleanup(board._IDENT.clear)
        self.addCleanup(setattr, board, "status", board.status)
        self.addCleanup(setattr, board.opmname, "identify_for_rename", board.opmname.identify_for_rename)

    def _no_api(self, ip, timeout=6.0):
        raise self.board.BoardError("no answer")

    def test_a_switch_on_the_dock_is_not_a_starcap(self):
        self.board.status = self._no_api
        self.board.opmname.identify_for_rename = lambda ip, timeout=25: {"ok": False, "error": "refused"}
        self.assertIsNone(self.board.describe("10.77.0.254"))

    def test_recorder_down_but_ssh_up_is_shown(self):
        self.board.status = self._no_api
        self.board.opmname.identify_for_rename = lambda ip, timeout=25: {
            "ok": True, "cpu_id": "c1", "model": "NanoPi R76S", "boot": "emmc", "wifi": "connected", "name": "n"}
        d = self.board.describe("10.77.0.9")
        self.assertTrue(d["recorder_down"])
        self.assertEqual(d["cpu_id"], "c1")

    def test_one_missed_poll_keeps_the_card(self):
        self.board.status = self._no_api
        prev = {"ip": "10.77.0.9", "cpu_id": "c1", "seen": time.time() - 3}
        d = self.board.describe("10.77.0.9", prev)
        self.assertTrue(d["stale"])
        self.assertEqual(d["cpu_id"], "c1")

    def test_healthy(self):
        fb = FakeBoard()
        self.board.status = fb.status
        self.board.opmname.identify_for_rename = lambda ip, timeout=25: {
            "ok": True, "cpu_id": "c1", "model": "NanoPi R76S", "boot": "emmc", "wifi": "connected", "name": "x"}
        d = self.board.describe(fb.ip)
        self.assertEqual((d["cpu_id"], d["camera_id"], d["name"]), ("c1", "63.1", "starcap999"))
        self.assertNotIn("error", d)


class WebUI(unittest.TestCase):
    """The server-side rules: naming needs a green run, an interrupted run's clean-up
    comes first, bad input is refused cleanly."""

    def setUp(self):
        import webui
        self.webui = webui
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, val in (("REPORTS", Path(self.tmp.name)),
                          ("PENDING_FILE", Path(self.tmp.name) / ".pending_cleanup.json")):
            self.addCleanup(setattr, webui, name, getattr(webui, name))
            setattr(webui, name, val)
        self.addCleanup(webui.RUNS.clear)
        self.addCleanup(webui.PENDING.clear)
        self.addCleanup(webui.PENDING_STATE.clear)
        self.addCleanup(webui.DEVICES.clear)
        self.calls = []
        om = webui.board.opmname
        for name, fn in (("check_rename", lambda n, c: {"taken_by": [], "current_name": None}),
                         ("set_authoritative_name", lambda c, n: self.calls.append((c, n)) or {"assigned_name": n}),
                         ("name_survey", lambda: {"next": "starcap162"})):
            self.addCleanup(setattr, om, name, getattr(om, name))
            setattr(om, name, fn)

    def _run(self, verdict):
        r = qc.QCRun({"ip": "10.77.0.50", "cpu_id": "cpu1"}, 5)
        r.verdict, r.active = verdict, False
        self.webui.RUNS["cpu1"] = r
        return r

    def test_rename_refused_without_a_pass(self):
        self._run("fail")
        res = self.webui.api_rename({"cpu_id": "cpu1", "name": "starcap162"})
        self.assertFalse(res["ok"])
        self.assertIn("not passed", res["error"])
        self.assertEqual(self.calls, [])

    def test_rename_after_pass(self):
        r = self._run("pass")
        self.webui._save_report(r)
        res = self.webui.api_rename({"cpu_id": "cpu1", "name": "starcap162"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.calls, [("cpu1", "starcap162")])
        saved = json.loads((self.webui.REPORTS / r.report_file).read_text())
        self.assertEqual(saved["named_as"], "starcap162")

    def test_name_taken_off_another_board_is_remembered(self):
        r = self._run("pass")
        self.webui.board.opmname.set_authoritative_name = lambda c, n: {"assigned_name": n, "taken_from": ["x"]}
        self.assertTrue(self.webui.api_rename({"cpu_id": "cpu1", "name": "starcap162", "force": True})["ok"])
        self.assertIn("taken off 1 other board", r.named_note)

    def test_rename_rejects_bad_names(self):
        self._run("pass")
        for bad in ("", "star cap", "x" * 41, "a/b"):
            self.assertFalse(self.webui.api_rename({"cpu_id": "cpu1", "name": bad})["ok"], bad)
        self.assertEqual(self.calls, [])

    def test_duplicate_name_needs_confirmation(self):
        self._run("pass")
        om = self.webui.board.opmname
        om.check_rename = lambda n, c: {"taken_by": ["other"], "current_name": None}
        res = self.webui.api_rename({"cpu_id": "cpu1", "name": "starcap162"})
        self.assertTrue(res.get("needs_confirm"))
        self.assertEqual(self.calls, [])
        res = self.webui.api_rename({"cpu_id": "cpu1", "name": "starcap162", "force": True})
        self.assertTrue(res["ok"])

    def test_start_validation(self):
        self.assertFalse(self.webui.api_start({"ip": "10.77.0.50", "minutes": 7})["ok"])
        self.assertFalse(self.webui.api_start({"ip": "10.9.9.9", "minutes": 5})["ok"])
        self.assertFalse(self.webui.api_start({"minutes": "x"})["ok"])

    def test_start_waits_for_an_interrupted_runs_cleanup(self):
        self.webui.DEVICES["10.77.0.50"] = {"ip": "10.77.0.50", "cpu_id": "cpu1", "seen": time.time()}
        self.webui.PENDING["cpu1"] = {"window": {"boot_id": "b", "mono_start": 1, "mono_last": 2}, "minutes": 5}
        self.webui.PENDING_STATE["cpu1"] = {"state": "running", "at": time.time()}   # no thread in the test
        res = self.webui.api_start({"ip": "10.77.0.50", "minutes": 5})
        self.assertFalse(res["ok"])
        self.assertIn("interrupted", res["error"])
        self.assertNotIn("cpu1", self.webui.RUNS)

    def test_pending_survives_a_restart(self):
        r = qc.QCRun({"ip": "10.77.0.50", "cpu_id": "cpu1"}, 5)
        rec = {"cpu_id": "cpu1", "window": {"boot_id": "b", "mono_start": 1.0, "mono_last": 2.0}, "minutes": 5}
        self.webui.on_pending(r, rec)
        self.webui.PENDING.clear()
        self.webui._pending_load()
        self.assertEqual(self.webui.PENDING["cpu1"]["window"]["boot_id"], "b")
        self.webui.on_pending(r, None)
        self.assertEqual(json.loads(self.webui.PENDING_FILE.read_text()), {})

    def test_history_reads_reports_but_not_the_pending_file(self):
        r = self._run("pass")
        r.finished = time.time()
        self.webui._save_report(r)
        self.webui.on_pending(r, {"cpu_id": "cpu1", "window": {"boot_id": "b"}, "minutes": 5})
        h = self.webui.history()
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["verdict"], "pass")

    def test_open_logs_only_opens_a_runs_own_folder(self):
        for bad in ("../x.json", "/etc/passwd", ".pending_cleanup.json", "a/b.json", ""):
            self.assertFalse(self.webui.api_open_logs({"file": bad})["ok"], bad)
        self.assertFalse(self.webui.api_open_logs({"file": "20261007-120000_abc_cpu1.json"})["ok"],
                         "no folder: nothing to open")

    def test_report_name_is_safe_for_odd_cpu_ids(self):
        r = qc.QCRun({"ip": "10.77.0.50", "cpu_id": "mac:1a:b3:67"}, 5)
        rid = r.report()["report_id"]
        self.assertRegex(rid, self.webui._REPORT_ID)


if __name__ == "__main__":
    unittest.main()
