"""A simulated StarCap for engine tests: the recorder's HTTP API, the SSH answers
QC's scripts expect, and takes that start, stop, finalise, get renamed by a clock
step, restart by themselves, and get deleted (or refuse to be).

It patches the functions qc.py reaches the board through (board.status, board.ssh,
board.http_json, board.list_takes, board.delete_run, ...), so the real engine runs
unchanged.
"""
import base64
import json
import subprocess
import time
import types

import numpy as np

import board
import calibration
import delivery
import qc


class FakeBoard:
    def __init__(self, **kw):
        self.ip = "10.77.0.50"
        self.version = "1.67.0"
        self.camera_id = "63.1"
        self.usb_speed = 5000
        self.boot_id = "73ca3e96-e94f-4182-afaa-0943d948e77d"
        self.main_pid = "2259"
        self.mono0 = time.monotonic() - 1000.0     # the board has been up 1000 s
        self.recording = False
        self.dir = None
        self.take_t0 = None
        self.presses = 0
        self.takes = {}                 # run -> take dict (see _new_take)
        self.deleted = []
        self._seq = 0
        # behaviour switches
        self.press_after_s = 0.6        # the "operator" presses this long after the prompt
        self.press_starts_take = True
        self.reboot_after_s = None      # uptime goes backwards this far into a take
        self.resume_after_s = None      # the take "loses the camera" and restarts as a new take
        self.rename_after_s = None      # an NTP step renames every take of this boot
        self.rename_after_stop = False  # ...or right after the take is stopped
        self.finalize_s = 0.0           # how long the recorder's post-take check runs
        self.stale_finalizing = False   # a .finalizing marker that never clears
        self.delete_refuses = False     # the delete route answers 500 forever
        self.drop_pct = 0.0             # frames.csv: share of 2x intervals
        self.dup_stamps = 0
        self.stuck_accel = False
        self.stuck_gyro = False
        self.calibration_state = "pass"
        self.software_label = "READY"
        self.wifi_seen = 7
        self.sd_loader_bytes = 0
        self.temp = 61.0
        self.ssh_delay = 0.0
        self.delivery_available = True  # False: QC falls back to the recorder's CSVs
        self.refuse_strip = None        # a reason: the delivery decoder refuses the strip
        self.csv_hole_ms = 0            # a gap in the RECORDER's frames.csv/imu.csv only
        self._prompt_seen_at = None
        self.__dict__.update(kw)

    # ---------------------------------------------------------------- state
    def mono(self):
        return time.monotonic() - self.mono0

    def _name(self):
        self._seq += 1
        return f"20261007-12{self._seq:04d}"

    def _new_take(self, preexisting=False):
        run = self._name()
        self.takes[run] = {"run": run, "rid": f"rec_fake_{run}", "boot_id": self.boot_id,
                           "started_mono": self.mono(), "t0": time.time(), "t1": None,
                           "final_at": None, "renamed_from": None, "pre": preexisting}
        self.recording, self.dir, self.take_t0 = True, run, time.time()
        return run

    def _stop(self):
        if self.recording:
            t = self.takes[self.dir]
            t["t1"] = time.time()
            t["final_at"] = time.time() + self.finalize_s
            self.recording, self.dir = False, None
            if self.rename_after_stop:
                self.rename_all()

    def rename_all(self):
        """What _apply_clock_step does: every take of this boot gets its true start time."""
        for run in list(self.takes):
            t = self.takes.pop(run)
            if t["boot_id"] != self.boot_id:
                self.takes[run] = t
                continue
            new = self._name()
            t["renamed_from"], t["run"] = run, new
            self.takes[new] = t
            if self.dir == run:
                self.dir = new

    def _tick(self):
        """Time-driven events, evaluated whenever QC looks at the board."""
        if self._prompt_seen_at and time.time() - self._prompt_seen_at > self.press_after_s:
            self._prompt_seen_at = None
            self.presses += 1
            if self.press_starts_take and not self.recording:
                self._new_take()
        if self.recording:
            el = time.time() - self.take_t0
            if self.resume_after_s is not None and el > self.resume_after_s:
                self.resume_after_s = None
                self._stop()
                self._new_take()                  # the recorder rebuilds and resumes on its own
            if self.rename_after_s is not None and el > self.rename_after_s:
                self.rename_after_s = None
                self.rename_all()

    def uptime(self):
        if self.recording and self.reboot_after_s is not None and time.time() - self.take_t0 > self.reboot_after_s:
            return 20.0
        return self.mono()

    # ---------------------------------------------------------------- http
    def status(self, ip, timeout=6.0):
        self._tick()
        el = self.mono()
        rec = self.recording
        rec_frames = int((time.time() - self.take_t0) * 60.1) if rec else 0
        return {
            "camera": {"device": "/dev/video0", "card": f"DECXIN Camera {self.camera_id}: DECXIN Came",
                       "camera_number": 63, "camera_id": self.camera_id, "width": 4000, "height": 1200,
                       "fps_target": 60 if self.usb_speed == 5000 else 30, "capture_fps": 60.1,
                       "total_frames": int(el * 60.1),
                       "imu_live": {"total_samples": int(el * 600), "bad_frames": 0, "exposure_us": 4981},
                       "error": None,
                       "usb": {"path": "2-1", "speed_mbps": self.usb_speed,
                               "usb2_fallback": self.usb_speed < 5000, "heal": None},
                       "recording": rec, "dir": self.dir, "rec_frames": rec_frames, "dropped": 0,
                       "imu_hz": 600 if rec else 0, "duration": rec_frames / 60.0},
            "system": {"temps": {"soc-thermal": self.temp, "little-core-thermal": self.temp + 1},
                       "soc_temp": self.temp, "uptime": self.uptime()},
            "panel": {"button_present": True, "button_mode": "toggle", "presses": self.presses,
                      "error": None, "led_state": "recording" if rec else "idle"},
            "record_gate": "manual", "autostart": True, "upload": {"server": None},
            "last_run": None, "version": self.version, "device_id": "pi-fake000001",
            "name": "starcap999", "ip": self.ip,
        }

    def http_json(self, ip, path, method="GET", body=None, timeout=6.0):
        self._tick()
        if path == "/api/record/start" and method == "POST":
            if not self.recording:
                self._new_take()
            return {"ok": True, "recording": True}
        if path == "/api/record/stop" and method == "POST":
            self._stop()
            return {"ok": True, "recording": False}
        if path == "/api/recordings":
            return {"recordings": [{"run": r, "files": [{"name": f"{s}_000.mp4", "bytes": 1}
                                                         for s in ("left", "right", "imustrip")]
                                    + [{"name": "frames.csv", "bytes": 1}]} for r in self.takes]}
        if path == "/api/status":
            return self.status(ip)
        raise board.BoardError(f"{path} answered HTTP 404")

    def download(self, ip, run, name, dest, timeout=120.0):
        if run not in self.takes:
            raise board.BoardError(f"couldn't download {name}: HTTP 404")
        with open(dest, "wb") as f:
            f.write(b"fake strip")
        return 10

    def decode_strip(self, mods, strip, witness_csv=None):
        """The delivery decoder's answer for this take's strip: complete unless told."""
        if self.refuse_strip:
            raise delivery.Refused(self.refuse_strip)
        t = next(iter(sorted(self.takes.values(), key=lambda x: x["t0"])))
        for cand in self.takes.values():
            if cand["t1"] is not None and (t["t1"] is None or cand["t0"] >= t["t0"]):
                t = cand
        n = max(300, int(self._span(t) * 60.1))
        dt = np.full(n - 1, 16640.0)
        k = int(round(self.drop_pct / 100.0 * dt.size))
        if k:
            dt[np.linspace(10, dt.size - 10, k).astype(int)] = 33280.0
        exp = 4_000_000.0 + np.concatenate(([0.0], np.cumsum(dt)))
        rng = np.random.default_rng(7)
        m = n * 10
        ts = exp[0] + np.arange(m) * 1664.0
        a = np.tile([0.3, -9.79, 0.4], (m, 1)) + rng.normal(0, 0.02, (m, 3))
        g = rng.normal(0, 0.2, (m, 3))
        if self.stuck_accel:
            a[:] = [0.3, -9.79, 0.4]
        if self.stuck_gyro:
            g[:] = 0.0
        samples = [(int(x), list(aa), list(gg)) for x, aa, gg in zip(ts, a, g)]
        return {"frames": n, "packets": n, "exposures_us": [int(v) for v in exp], "samples": samples,
                "quality": {"hz": 600.9, "max_gap_us": 1664, "deficient": False},
                "filled": 0, "no_header": 0, "clamp": None}

    def delete_run(self, ip, run, timeout=60.0):
        self._tick()
        if self.delete_refuses:
            return 500
        if run not in self.takes:
            return 404
        if self.recording and self.dir == run:
            return 409
        self.takes.pop(run)
        self.deleted.append(run)
        return 200

    def list_takes(self, ip):
        self._tick()
        out = []
        for t in self.takes.values():
            finalizing = (self.stale_finalizing and t["t1"] is not None) or \
                         (t["final_at"] is not None and time.time() < t["final_at"])
            out.append({"run": t["run"], "path": f"/media/sdcard/multivideo/{t['run']}", "rid": t["rid"],
                        "renamed_from": t["renamed_from"], "boot_id": t["boot_id"],
                        "started_mono": t["started_mono"], "finalizing": bool(finalizing)})
        return out, self.mono()

    def preview_frames(self, ip, count=3, timeout=20.0):
        import cv2
        out = []
        rng = np.random.default_rng(int(time.time() * 1000) % 2 ** 31)
        for _ in range(count):
            img = rng.integers(30, 220, (1200, 4000, 3), dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            out.append(buf.tobytes())
        return out

    # ---------------------------------------------------------------- ssh
    def _take_in(self, script):
        for run, t in self.takes.items():
            if run in script:
                return t
        return None

    def ssh(self, ip, script, timeout=30):
        if self.ssh_delay:
            _real_sleep(self.ssh_delay)           # a real SSH round trip takes ~0.3-1 s
        self._tick()
        out = ""
        rc = 0
        if "tar czf -" in script:                  # save_take_logs: the take's logs, no video
            t = self._take_in(script)
            if not t:
                rc = 3
            else:
                out = self._logs_tar_b64(t)
        elif "journalctl -u stereocam" in script:
            out = "2026-10-07T20:00:00+0300 NanoPi-R76S stereocam[2259]: [rec] START\n"
        elif "random/boot_id" in script:
            out = f"{self.boot_id}\nMainPID={self.main_pid}\nNRestarts=0\nActiveState=active\n"
        elif "qc_write_test" in script:
            out = ("MNT=/dev/mmcblk0p1 rw,noatime\nDF=125388308480 113756123136\nWRITE=ok\nDISK=mmcblk0\n"
                   f"LOADERSIZE={1984 * 512}\nLOADER={self.sd_loader_bytes}\nEMMC=mmcblk2\n"
                   "LIFE=0x01 0x01\nEOL=0x01\n")
        elif "phy80211" in script:
            out = f"DEV=wlan0\nDRV=rtl88x2cs\nRF=0\nCARRIER=1\nSSID=Starpilot-5G-1\nMODE=infrastructure\nQ=78\nSEEN={self.wifi_seen}\n"
        elif "QCPY" in script:
            out = json.dumps({"cameras": [{"bus": "2-1", "product": "DECXIN Camera 63.1", "bcd": "0234",
                                           "ladder": [944, 2048, 3072, 5120, 6144, 9216], "speed": "5000"}]})
        elif "cpufreq" in script:
            out = "policy0 schedutil 2016000 2016000\npolicy4 performance 2304000 2304000\n"
        elif "final_status.json" in script:
            t = self._take_in(script)
            if not t:
                rc = 3
            elif t["t1"] is None or (t["final_at"] and time.time() < t["final_at"]):
                out = "BUSY\n"
            else:
                out = "JSON\n" + json.dumps(self._final_status(t))
        elif "frames.csv" in script:
            out = self._frames_csv(self._take_in(script))
        elif "imu.csv" in script:
            out = self._imu_csv(self._take_in(script))
        elif "device_info.json" in script:
            out = json.dumps({"recording_id": "rec_x", "eye_correction": {
                "applied": True, "left_file_eye": "left", "right_file_eye": "right"}})
        elif "ffprobe" in script:
            out = "left_000.mp4 1920x1200\nright_000.mp4 1920x1200\n"
        elif "ffmpeg" in script:
            import cv2
            img = np.random.default_rng(3).integers(0, 255, (400, 640, 3), dtype=np.uint8)
            b64 = base64.b64encode(cv2.imencode(".jpg", img)[1].tobytes()).decode()
            out = f"B64_left={b64}\nB64_right={b64}\n"
        else:
            raise AssertionError(f"fake board got an unexpected script:\n{script[:300]}")
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")

    def _span(self, t):
        return max(1.0, (t["t1"] or time.time()) - t["t0"])

    def _frames_csv(self, t):
        n = max(300, int(self._span(t) * 60.1))
        dt = np.full(n - 1, 16640)
        k = int(round(self.drop_pct / 100.0 * dt.size))
        if k:
            dt[np.linspace(10, dt.size - 10, k).astype(int)] = 33280
        if self.dup_stamps:
            dt[100:100 + self.dup_stamps] = 0
        if self.csv_hole_ms:
            dt[150] = int(self.csv_hole_ms * 1000)
        exp = (2 ** 32 - 200000 + np.concatenate(([0], np.cumsum(dt)))) % 2 ** 32   # crosses the wrap
        pts = np.arange(n) * 16_640_000 + 5_000_000_000
        lines = ["pts_ns,exposure_start_us,exposure_end_us,imu_samples_in_frame"]
        lines += [f"{p},{e},{(e + 4981) % 2 ** 32},10" for p, e in zip(pts, exp)]
        return "\n".join(lines) + "\n"

    def _imu_csv(self, t):
        n = max(3000, int(self._span(t) * 600))
        ts = np.arange(n) * 1664
        if self.csv_hole_ms:
            ts[1500:] += int(self.csv_hole_ms * 1000)
        ts = ts % 2 ** 32
        rng = np.random.default_rng(5)
        a = np.tile([0.3, -9.79, 0.4], (n, 1)) + rng.normal(0, 0.02, (n, 3))
        if self.stuck_accel:
            a[:] = [0.3, -9.79, 0.4]
        g = rng.normal(0, 0.2, (n, 3))
        if self.stuck_gyro:
            g[:] = 0.0
        lines = ["ts_us,ax_ms2,ay_ms2,az_ms2,gx_dps,gy_dps,gz_dps"]
        lines += [f"{t},{x:.4f},{y:.4f},{z:.4f},{u:.3f},{v:.3f},{w:.3f}"
                  for t, (x, y, z), (u, v, w) in zip(ts, a, g)]
        return "\n".join(lines) + "\n"

    def _logs_tar_b64(self, t):
        import io
        import tarfile
        buf = io.BytesIO()
        files = {"frames.csv": self._frames_csv(t), "imu.csv": self._imu_csv(t),
                 "metrics.csv": "t_s,cpu_pct\n0.0,15.1\n",
                 "device_info.json": json.dumps({"recording_id": t["rid"]}),
                 "final_status.json": json.dumps(self._final_status(t))}
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return base64.b64encode(buf.getvalue()).decode()

    def _final_status(self, t):
        n = max(300, int(self._span(t) * 60.1))
        files = {f"{s}_000.mp4": {"frames": n, "codec": "hevc", "avg_frame_rate": "2000000/33281"}
                 for s in ("left", "right", "imustrip")}
        return {"run": t["run"], "duration_s": self._span(t), "rec_frames": n, "dropped_pts_gaps": 0,
                "streams_ok": True, "imu_samples": n * 10, "imu_hz": 600, "bytes": 1, "files": files}

    # ---------------------------------------------------------------- wiring
    def install(self, test):
        """Patch everything QC reaches the board through; undone by the test's cleanup."""
        fb = self
        patches = [
            (board, "status", self.status), (board, "http_json", self.http_json),
            (board, "ssh", self.ssh), (board, "preview_frames", self.preview_frames),
            (board, "list_takes", self.list_takes), (board, "delete_run", self.delete_run),
            (board, "download", self.download),
            (delivery, "load", lambda: ((object(), "origin/main fake123") if fb.delivery_available
                                        else (None, "no starcap-dashboard checkout"))),
            (delivery, "decode_strip", self.decode_strip),
            (board, "sudo_password", lambda ip: "12345"),
            (board, "dockscan", lambda: types.SimpleNamespace(
                REMOTE="", PROFILES={"static-16640": {"ok": True, "verdict": "clean"},
                                     "dither-16626": {"ok": False, "verdict": "JITTERS"}},
                grade=lambda cam: ("static-16640", 2))),
            (board.opmname, "login_for", lambda ip: ("pi", "12345")),
            (board.dock_check, "probe", lambda b: {"VERSION": fb.version}),
            (board.dock_check, "assess", lambda info, latest: (
                fb.software_label, "", [] if fb.software_label == "READY" else [("UPDATE_ENABLE unset", "env")])),
            (qc, "_latest_version", lambda: ("1.67.0", "test", False)),
            (qc, "_port_open", lambda host, port: False),
            (calibration, "check", lambda cid, limits: (
                {"state": "pass", "summary": f"camera {cid} · FOV 155° × 98°", "details": [], "data": {}}
                if fb.calibration_state == "pass" else
                {"state": "fail", "summary": f"camera {cid} has no calibration on the dashboard"})),
            (qc, "SOAK_EXTRA_S", 3),
            (qc.time, "sleep", _fast_sleep),
        ]
        limits = dict(qc.LIMITS, fps_window_s=0.3, button_wait_s=4)
        patches.append((qc, "LIMITS", limits))
        for mod, name, val in patches:
            test.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, val)
        # the "operator": presses the button a moment after the prompt appears
        orig = qc.QCRun._t_button

        def watched(run_self):
            fb._prompt_seen_at = time.time()
            return orig(run_self)
        test.addCleanup(setattr, qc.QCRun, "_t_button", orig)
        qc.QCRun._t_button = watched
        return self


_real_sleep = time.sleep


def _fast_sleep(s):
    """qc.remove_qc_takes' polling sleeps, shortened (qc.time is the time module, so
    this is only installed for the length of a test)."""
    _real_sleep(min(s, 0.2))


def device(fb):
    return {"ip": fb.ip, "cpu_id": "ba5dc300b3eb77c3", "name": "starcap999", "model": "FriendlyElec NanoPi R76S",
            "version": fb.version, "boot": "emmc", "camera_id": fb.camera_id, "device_id": "pi-fake000001"}
