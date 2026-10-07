"""
qc.py: what StarCap QC tests, in what order, and what counts as a pass.

A QCRun drives one docked board through STEPS, top to bottom, on its own thread:

  quick checks (well under a minute; all of them run, so every problem shows at once)
   1 connect      the board answers; nothing on this network would end or steal the take
   2 software     latest recorder, auto-updating, on main (dock-check's own rules)
   3 storage      boots from eMMC; SD card mounted, writable, big enough, no stale
                  bootloader on it; eMMC wear
   4 camera       found, unit ID burned in, clean firmware build, USB 3, a true 60 fps
                  and a live IMU
   5 calibration  intrinsics + extrinsics on the dashboard for THIS lens, field-ready by
                  the dashboard's own rule; FOV in spec
   6 wifi         the module is there, unblocked, and hears networks
   7 image        both eyes alive (not black, covered or frozen)
  then, only if every quick check passed:
   8 button       the operator presses it; the press also starts the soak take
   9 soak         the minutes the operator picked (+15 s, so a file boundary is crossed):
                  fps holds, the take never stops or restarts, no reboot, no recorder
                  restart, no throttling, temperature sane
  10 timing       the customer's spec on the camera clock the delivery uses:
                  at most 1% of frame intervals outside 16.67 ms +-10 ms
  11 footage      the recorder's own post-take check passed; every file finalised, no
                  file missing, frame counts agree, HEVC 1920x1200 @ 60 per eye, the eyes
                  un-swapped, IMU >= 200 Hz, no long gaps, gravity sane, gyro alive
  12 cleanup      every take QC caused is stopped and deleted from the SD card, so none
                  is ever uploaded as real footage, after its LOG files are copied to
                  this laptop (next to the run's report): frames.csv, imu.csv,
                  metrics.csv, device_info.json, final_status.json, eye_correction.json,
                  plus the recorder's journal for the run. A run fails if this fails.

WHICH TAKES ARE QC'S. Right before the button prompt QC notes the board's boot id and
monotonic clock. A take is QC's when the recorder stamped it with that boot id and a
start time inside the QC window, which covers a take QC never claimed: one the
recorder restarted by itself after a camera or SD dropout, or one a late press
started. Names are never trusted: the recorder renames takes when NTP steps the clock.
And a take from outside the window (the operator's own footage) is never touched, not
even by the clean-up that finishes an interrupted run the next time the board docks.

The verdict is PASS only when no step FAILED. WARN never blocks; it flags something
worth a look that QC cannot decide on its own. Every pass/fail threshold is in LIMITS.
"""
import base64
import gzip
import io
import json
import math
import os
import re
import shlex
import socket
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

import board
import calibration
import checks
import delivery

SOAK_CHOICES_MIN = (5, 10)
SOAK_EXTRA_S = 15            # run past the 5-minute file boundary so the rollover is tested too

LIMITS = {
    "fps_nominal": 60,
    "fps_min": 59.5,          # true capture rate before the soak, and the take's average
    "soak_fps_dips_max": 3,   # live fps polls (every 2 s) allowed under fps_min during the soak
    "usb_mbps": 5000,         # USB 3; a 480M link records a flat 30 fps that still "passes" timing
    "eye_width": 1920, "eye_height": 1200,
    "frame_width": 4000, "frame_height": 1200,   # the raw side-by-side stream
    "file_fps_range": (59.0, 61.5),              # a recorded file's average frame rate
    "p99_tol_ms": 10.0,       # customer spec, section 2.4 ...
    "timing_max_out_pct": 1.0,  # ... read the house way: <=1% of intervals outside the band
    "dup_stamps_max_pct": 0.1,  # repeated camera stamps beyond this: exposure too long / strip broken
    "video_vs_rec_frames_pct": 0.5,   # frames in the files vs frames the recorder recorded
    "csv_rows_warn_pct": 0.5,   # frames.csv rows vs video frames (its branch is leaky)...
    "csv_rows_fail_pct": 2.0,   # ...past this the timing analysis can't be trusted
    "temp_warn_c": 80.0,      # the R76S field target
    "temp_fail_c": 85.0,      # the little-core trip: both clusters throttle, fps collapses
    "imu_min_hz": 200.0,      # customer spec, section 2.5
    "imu_low_hz": 550.0,      # delivery calls a take under this IMU-deficient
    "imu_gap_warn_ms": 200.0, # delivery: deficient
    "imu_gap_fail_ms": 250.0, # delivery: refused
    "imu_still_dps": 3.0,     # gyro under this = the cap is still (for the gravity check)
    "gravity_tol": 0.8,       # m/s^2 around 9.81, on still stretches
    "sd_min_total_gb": 28.0,  # a "32 GB" card formats to ~29
    "sd_min_free_gb": 5.0,
    "fov_h_min": 110.0, "fov_v_min": 80.0,       # customer spec, section 2.4
    "baseline_mm": (55.0, 65.0),
    "recal_days": 30,         # the dashboard's "recalibration due"
    "eye_black_p99": 20,      # an eye whose brightest 1% is under this is black
    "eye_flat_std": 4,        # ...or this flat is no picture at all
    "eye_frozen_motion": 0.05,  # sensor noise alone keeps a live eye well above this
    "eye_black_level_diff": 40,
    "eye_contrast_ratio": 0.6,
    "button_wait_s": 90,
    "fps_window_s": 6.0,
}

_RUN_NAME = re.compile(r"^\d{8}-\d{6}$")
OPM_LAN_PORT = 8770           # opm-lan stops takes on docked boards (stop_recording_on_dock)

STEPS = [
    ("connect", "Board answers", "SSH, recorder API, a clear network"),
    ("software", "Recorder software", "latest, auto-updating, on main"),
    ("storage", "eMMC + SD card", "eMMC boot, SD card ready"),
    ("camera", "Camera", "ID, firmware, USB 3, 60 fps, IMU"),
    ("calibration", "Calibration", "intrinsics + extrinsics, FOV"),
    ("wifi", "WiFi module", "present and hears networks"),
    ("image", "Live image", "both eyes alive"),
    ("button", "Button", "press seen; starts the take"),
    ("soak", "Soak recording", "fps, stops, reboots, heat"),
    ("timing", "Frame timing", "≤1% of frames off by >10 ms"),
    ("footage", "Footage", "files, format, eyes, IMU"),
    ("cleanup", "Remove test take", "never uploaded as real footage"),
]


class StepFail(Exception):
    """Raised inside a step: the step FAILED, with an operator-facing message."""

    def __init__(self, summary, details=None):
        super().__init__(summary)
        self.summary = summary
        self.details = details or []


class Cancelled(Exception):
    pass


class QCRun:
    def __init__(self, device, minutes, on_finish=None, stop_take_ok=False, on_pending=None,
                 logs_root=None):
        self.dev = dict(device)
        self.logs_root = Path(logs_root) if logs_root else None   # None: keep no logs (tests)
        self.ip = device["ip"]
        self.cpu_id = device["cpu_id"]
        self.minutes = int(minutes)
        self.on_finish = on_finish
        self.on_pending = on_pending    # persists / clears the "QC may have takes on this board" record
        self.stop_take_ok = stop_take_ok
        self.id = time.strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.steps = [{"key": k, "title": t, "why": w, "state": "pending", "summary": "",
                       "details": [], "data": {}} for k, t, w in STEPS]
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self.active = True              # from creation: two starts can't race to make two runs
        self.verdict = None             # "pass" | "fail" | "cancelled"
        self.headline = ""
        self.started = None
        self.finished = None
        self.prompt = None              # {"title","text","deadline"} while the operator must act
        self.live = None                # soak numbers for the UI
        self.frame_jpeg = None          # a small snapshot for the card
        self.frame_caption = ""
        self.frame_seq = 0
        self.named_as = None
        self.named_note = ""
        self.report_file = None
        # the QC window (see the module doc) and the take QC is measuring
        self.window = None              # {"boot_id","mono_start","mono_last","pre_rids"}
        self._mono_ref = None           # laptop monotonic at the window start
        self.take_run = None            # its current name (a clock step can rename it)
        self.take_rid = None            # its recording_id, which never changes
        self.take_dir = None
        self._deliv = None              # the take as delivery decodes it (see _delivered)
        self.ctx = {}                   # facts gathered along the way, for the report

    @property
    def stem(self):
        """The name this run's report and logs share: <started>_<id>_<cpu>."""
        return f"{self.id}_{re.sub(r'[^A-Za-z0-9]', '', self.cpu_id)[:12] or 'unknown'}"

    @property
    def logs_dir(self):
        return self.logs_root / self.stem if self.logs_root else None

    # ------------------------------------------------------------------ public
    def start(self):
        self.started = time.time()
        self._thread = threading.Thread(target=self._main, name=f"qc-{self.ip}", daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def to_dict(self):
        # prompt/live are swapped whole (never mutated in place) by the run thread:
        # read each reference once, so a value cleared mid-read can't be half-used
        prompt, live = self.prompt, self.live
        with self._lock:
            return {
                "id": self.id, "cpu_id": self.cpu_id, "ip": self.ip,
                "name": self.dev.get("name"), "camera_id": self.dev.get("camera_id"),
                "model": self.dev.get("model"), "version": self.dev.get("version"),
                "minutes": self.minutes, "active": self.active, "verdict": self.verdict,
                "headline": self.headline, "started": self.started, "finished": self.finished,
                "steps": [{k: s[k] for k in ("key", "title", "why", "state", "summary", "details")}
                          for s in self.steps],
                "prompt": dict(prompt) if prompt else None,
                "live": dict(live) if live else None,
                "has_frame": self.frame_jpeg is not None, "frame_seq": self.frame_seq,
                "frame_caption": self.frame_caption,
                "named_as": self.named_as, "named_note": self.named_note,
                "report_file": self.report_file,
                "has_logs": bool((self.ctx.get("logs") or {}).get("files")),
                "cancelling": self._cancel.is_set() and self.active,
            }

    def report(self):
        d = self.to_dict()
        with self._lock:
            steps = [{k: s[k] for k in ("key", "title", "state", "summary", "details", "data")}
                     for s in self.steps]
        return {
            "report_id": f"{self.stem}.json",
            "tool": "starcap-qc", "verdict": self.verdict, "headline": self.headline,
            "started": self.started, "finished": self.finished, "soak_minutes": self.minutes,
            "limits": LIMITS, "named_as": self.named_as, "station": socket.gethostname(),
            "board": {"cpu_id": self.cpu_id, "ip": self.ip, "name": d["name"],
                      "device_id": self.dev.get("device_id"), "model": d["model"],
                      "version": d["version"], "camera_id": d["camera_id"],
                      "boot": self.dev.get("boot")},
            "steps": steps, "context": _jsonable(self.ctx),
        }

    # ------------------------------------------------------------------ plumbing
    def _step(self, key):
        return next(s for s in self.steps if s["key"] == key)

    def _set(self, key, **kw):
        with self._lock:
            self._step(key).update(kw)

    def _detail(self, key, text):
        with self._lock:
            self._step(key)["details"].append(text)

    def _note(self, key, text):
        """A running step's progress line, shown while it works."""
        self._set(key, summary=text)

    def _sleep(self, seconds):
        if self._cancel.wait(max(0.0, seconds)):
            raise Cancelled()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _status(self, timeout=6.0):
        return board.status(self.ip, timeout=timeout)

    def _ssh(self, script, timeout=60):
        return board.ssh(self.ip, script, timeout=timeout)

    # ------------------------------------------------------------------ the run
    def _main(self):
        failed = []
        try:
            for key, title, _why in STEPS:
                self._check_cancel()
                if key == "cleanup":
                    continue                     # runs in finally, whatever happened
                if key == "button" and failed:
                    self._skip_rest("fix the failures above first, then run QC again")
                    break
                self._set(key, state="running")
                try:
                    res = getattr(self, "_t_" + key)()
                except (StepFail, board.BoardError) as e:
                    summary = e.summary if isinstance(e, StepFail) else str(e)
                    self._set(key, state="fail", summary=summary)
                    for d in getattr(e, "details", []) or []:
                        self._detail(key, d)
                    failed.append((key, title, summary))
                    if key == "connect":
                        self._skip_rest("the board didn't answer")
                        break
                    if key == "button":
                        self._skip_rest("no soak without a working button")
                        break
                    if key == "soak":
                        self._skip_rest("the soak failed, so there is no good take to analyse")
                        break
                    continue
                state, summary = res if isinstance(res, tuple) else ("pass", res or "")
                self._set(key, state=state, summary=summary)
            self.verdict = "fail" if failed else "pass"
        except Cancelled:
            self.verdict = "cancelled"
        except Exception as e:                        # noqa: BLE001  a bug must still end the run cleanly
            traceback.print_exc()
            cur = next((s for s in self.steps if s["state"] == "running"), None)
            msg = f"internal error ({type(e).__name__}: {e}); this is a QC tool bug"
            if cur:
                self._set(cur["key"], state="fail", summary=msg)
            failed.append((cur["key"] if cur else "?", cur["title"] if cur else "QC", msg))
            self.verdict = "fail"
        finally:
            self.prompt = None
            self._save_journal()
            self._cleanup()
            cleanup = self._step("cleanup")
            if cleanup["state"] == "fail":
                failed.append(("cleanup", cleanup["title"], cleanup["summary"]))
                if self.verdict == "pass":
                    self.verdict = "fail"        # a board can't pass with a test take left on it
            with self._lock:
                for s in self.steps:
                    if s["state"] in ("pending", "running"):
                        s["state"] = "skip"
                        s["summary"] = "stopped" if self.verdict == "cancelled" else "not run"
            if self.verdict == "pass":
                warns = [s["title"] for s in self.steps if s["state"] == "warn"]
                self.headline = ("Every check passed." if not warns else
                                 "Passed. Worth a look: " + ", ".join(warns) + ".")
            elif self.verdict == "cancelled":
                self.headline = "Stopped before the end." + (
                    " The test take was removed." if cleanup["state"] == "pass" else
                    f" {cleanup['summary']}" if cleanup["state"] == "fail" else "")
            else:
                self.headline = "  ·  ".join(f"{t}: {m}" for _k, t, m in failed[:3])
            self.finished = time.time()
            if self.live:
                self.live = dict(self.live, done=True)
            # the report is written BEFORE the run reads as finished: anything watching
            # `active` (the page, a rename, a test) must never see a finished run whose
            # report doesn't exist yet
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception as e:                # noqa: BLE001
                    print(f"[qc] couldn't save the report: {e}")
            self.active = False

    def _skip_rest(self, why):
        with self._lock:
            for s in self.steps:
                if s["state"] == "pending" and s["key"] != "cleanup":
                    s["state"] = "skip"
                    s["summary"] = why

    # ================================================================== 1-7: quick checks
    def _t_connect(self):
        st = self._status()
        self.ctx["status_at_start"] = _trim_status(st)
        r = self._ssh("cat /proc/sys/kernel/random/boot_id; "
                      "systemctl show stereocam -p MainPID -p NRestarts -p ActiveState", timeout=30)
        lines = r.stdout.strip().splitlines()
        boot_id = lines[0].strip() if lines else ""
        kv = dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln)
        if not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
            raise StepFail("couldn't read the board's boot id over SSH")
        if kv.get("ActiveState") != "active":
            raise StepFail(f"the recorder service is {kv.get('ActiveState') or 'not running'}")
        self.ctx.update(boot_id=boot_id, main_pid=kv.get("MainPID"),
                        nrestarts=_int(kv.get("NRestarts")), version_at_start=st.get("version"))
        self.dev["name"] = st.get("name") or self.dev.get("name")
        # things that would end or steal the QC take, refused up front with the reason
        if st.get("record_gate") not in ("manual", None):
            raise StepFail(f"RECORD_GATE={st.get('record_gate')}: this board stops every take while "
                           "ethernet is plugged in (the shipped setting is manual)")
        if _port_open("127.0.0.1", OPM_LAN_PORT):
            raise StepFail("opm-lan is running on this laptop, and it stops the recording on every "
                           "docked board. Close it for QC")
        server = (st.get("upload") or {}).get("server")
        if server:
            raise StepFail(f"the board found an upload server ({server.get('name') or server.get('ip')}); it "
                           "would upload and then delete the test take. Stop that server for QC")
        user = (board.opmname.login_for(self.ip) or ("?",))[0]
        return (f"{self.dev.get('name') or 'unnamed'} · {_short_model(self.dev.get('model'))} · "
                f"v{st.get('version') or '?'} · logged in as {user}")

    def _t_software(self):
        self._note("software", "checking against GitHub…")
        latest, src, stale = _latest_version()
        info = board.dock_check.probe({"ip": self.ip, "name": self.dev.get("name") or self.ip})
        label, _colour, issues = board.dock_check.assess(info, latest)
        ver = info.get("VERSION") or self.dev.get("version") or "?"
        self.ctx["software"] = {"version": ver, "latest": latest, "latest_from": src,
                                "label": label, "issues": [t for t, _a in issues]}
        texts = [" ".join(t.split()) for t, _a in issues]
        for t in texts:
            self._detail("software", t)
        if stale and latest:
            self._detail("software", f"(the latest version came from a stale fetch: {src})")
        if label == "READY":
            return f"v{ver}, the latest; auto-update on; tracking main"
        if label == "IMPLICIT":
            return ("warn", f"v{ver}, the latest, ready to ship; but: {texts[0] if texts else 'see below'}")
        if latest and ver != latest and any(a == "update" for _t, a in issues):
            raise StepFail(f"v{ver} is behind main ({latest}). The board updates itself while docked; "
                           "wait for it to restart, then run QC again")
        raise StepFail(f"not ready to ship: {texts[0] if texts else label}")

    def _t_storage(self):
        boot = self.dev.get("boot") or "unknown"
        if boot != "emmc":
            raise StepFail(f"boots from {'an SD card' if boot == 'sd' else 'an unknown medium'}, "
                           "not eMMC: flash the eMMC (station B1)")
        pw = shlex.quote(board.sudo_password(self.ip))
        r = self._ssh(
            'm=$(findmnt -n -o SOURCE,OPTIONS /media/sdcard 2>/dev/null); echo "MNT=$m"; '
            'df -B1 --output=size,avail /media/sdcard 2>/dev/null | tail -1 | sed "s/^ */DF=/"; '
            't=/media/sdcard/.qc_write_test.$$; '
            'if dd if=/dev/zero of=$t bs=1M count=8 conv=fsync status=none 2>/dev/null; '
            'then echo WRITE=ok; else echo WRITE=fail; fi; rm -f $t; '
            # the SD card's own disk: a card that was once a boot card keeps a loader at
            # sector 64, and an R76S then hangs at boot. A clean card is zero there.
            'src=$(findmnt -n -o SOURCE /media/sdcard 2>/dev/null); '
            'disk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -1); echo "DISK=$disk"; '
            # root reads the device, but the FILE is written by this (pi) shell: /tmp is
            # sticky with fs.protected_regular, so root may not open pi's mktemp file
            't=$(mktemp); '
            f'if [ -n "$disk" ] && echo {pw} | sudo -S -p "" dd if=/dev/$disk bs=512 skip=64 count=1984 '
            'status=none 2>/dev/null > $t; then echo "LOADERSIZE=$(stat -c%s $t)"; '
            'echo "LOADER=$(tr -d \'\\000\' < $t | wc -c)"; fi; rm -f $t; '
            # eMMC wear (JEDEC life_time, pre_eol_info): 0x01 = fine
            'for b in /sys/block/mmcblk*boot0; do [ -e "$b" ] || continue; e=${b%boot0}; '
            'echo "EMMC=$(basename $e)"; echo "LIFE=$(cat $e/device/life_time 2>/dev/null)"; '
            'echo "EOL=$(cat $e/device/pre_eol_info 2>/dev/null)"; done', timeout=60)
        out = r.stdout
        mnt = _grab(out, "MNT")
        df = _grab(out, "DF").split()
        if not mnt:
            raise StepFail("no SD card mounted at /media/sdcard: insert one, or re-seat it")
        if "rw" not in mnt.split()[-1].split(","):
            raise StepFail(f"the SD card is mounted read-only ({mnt})")
        try:
            total, avail = int(df[0]), int(df[1])
        except (IndexError, ValueError):
            raise StepFail("couldn't read the SD card's size") from None
        gb = 1e9
        # only trust the count when the whole area was read; anything less is "unknown"
        loader = _grab(out, "LOADER") if _grab(out, "LOADERSIZE") == str(1984 * 512) else ""
        self.ctx["storage"] = {"mount": mnt, "total_b": total, "free_b": avail, "sd_disk": _grab(out, "DISK"),
                               "stale_loader_bytes": loader, "emmc": _grab(out, "EMMC"),
                               "emmc_life": _grab(out, "LIFE"), "emmc_eol": _grab(out, "EOL")}
        if _grab(out, "WRITE") != "ok":
            raise StepFail("writing to the SD card failed")
        if total / gb < LIMITS["sd_min_total_gb"]:
            raise StepFail(f"the SD card is only {total / gb:.0f} GB (use 32 GB or bigger)")
        if avail / gb < LIMITS["sd_min_free_gb"]:
            raise StepFail(f"only {avail / gb:.1f} GB free on the SD card; the soak needs "
                           f"{LIMITS['sd_min_free_gb']:.0f} GB")
        if loader.isdigit() and int(loader) > 0:
            raise StepFail("the SD card still carries a bootloader from an old image (sectors 64-2047): "
                           "the board can hang at boot. Wipe it with format-sd-storage and run again")
        eol = _grab(out, "EOL").lower()
        if _grab(out, "LIFE"):
            self._detail("storage", f"eMMC {_grab(out, 'EMMC')} wear: life_time {_grab(out, 'LIFE')}, "
                                    f"pre_eol {eol or '?'}")
        note = f"eMMC boot · SD card {total / gb:.0f} GB, {avail / gb:.0f} GB free, writes OK"
        warns = []
        if not loader.isdigit():
            warns.append("couldn't read the SD card's boot area, so a stale bootloader wasn't ruled out")
        if eol and eol not in ("0x01", "0x00"):
            warns.append(f"eMMC pre-EOL is {eol} (worn)")
        if warns:
            return ("warn", note + " · " + "; ".join(warns))
        return note

    def _t_camera(self):
        st = self._status()
        cam = st.get("camera") or {}
        if cam.get("error"):
            raise StepFail(f"camera error: {cam['error']}")
        if not cam.get("device"):
            raise StepFail("no camera found")
        cid = cam.get("camera_id")
        self.dev["camera_id"] = cid
        if not cid:
            raise StepFail(f"the camera has no unit ID burned in ({(cam.get('card') or 'unnamed').strip()}): "
                           "flash the camera firmware (station A2)")
        usb = cam.get("usb") or {}
        speed = usb.get("speed_mbps")
        if speed is None:
            raise StepFail("couldn't read the camera's USB link speed")
        if usb.get("usb2_fallback") or speed < LIMITS["usb_mbps"]:
            raise StepFail(f"the camera is on a {speed}M USB link, not USB 3 (5000M), so it would record "
                           "a flat 30 fps. Re-seat the camera cable and run QC again")
        w, h, tgt = cam.get("width"), cam.get("height"), cam.get("fps_target")
        if (w, h) != (LIMITS["frame_width"], LIMITS["frame_height"]) or tgt != LIMITS["fps_nominal"]:
            raise StepFail(f"the camera runs {w}x{h} @ {tgt} fps, expected "
                           f"{LIMITS['frame_width']}x{LIMITS['frame_height']} @ {LIMITS['fps_nominal']}")
        fw = self._camera_firmware()
        # the true capture rate: frames counted over a window (the board's own fps
        # figure is a rounded 2 s estimate). Also proves a dead camera dead: it keeps
        # reporting its last device and card, but its counter stops. Both samples are
        # taken the same way, back to back with their clock reading, so the request
        # latency cancels out instead of stretching the count over a short window.
        self._note("camera", "counting frames…")
        cam = self._status().get("camera") or {}
        t0 = time.monotonic()
        f0 = cam.get("total_frames")
        i0 = (cam.get("imu_live") or {}).get("total_samples")
        b0 = (cam.get("imu_live") or {}).get("bad_frames")
        self._sleep(LIMITS["fps_window_s"])
        cam2 = self._status().get("camera") or {}
        dt = time.monotonic() - t0
        f1 = cam2.get("total_frames")
        i1 = (cam2.get("imu_live") or {}).get("total_samples")
        b1 = (cam2.get("imu_live") or {}).get("bad_frames")
        if f0 is None or f1 is None or f1 < f0:
            raise StepFail("the camera's frame counter isn't advancing (the camera restarted or died)")
        fps = (f1 - f0) / dt
        self.ctx["camera"] = {"camera_id": cid, "card": cam.get("card"), "usb": usb, "firmware": fw,
                              "fps_measured": fps, "exposure_us": (cam2.get("imu_live") or {}).get("exposure_us")}
        if fps < LIMITS["fps_min"]:
            raise StepFail(f"the camera delivers {fps:.1f} fps (needs {LIMITS['fps_min']})")
        hz = None
        if i0 is not None and i1 is not None:
            hz = (i1 - i0) / dt
            self.ctx["camera"]["imu_hz_live"] = hz
            if hz < LIMITS["imu_min_hz"]:
                raise StepFail(f"the camera's IMU delivers {hz:.0f} Hz (needs {LIMITS['imu_min_hz']:.0f})")
        if b0 is not None and b1 is not None and b1 > b0:
            self._detail("camera", f"{b1 - b0} frames with an unreadable IMU strip in {dt:.0f} s")
        if fw.get("ok") is False:
            raise StepFail(f"camera firmware is the '{fw['build']}' build, which jitters and fails the "
                           "customer's timing spec under changing light: reflash it (station A2)")
        summary = f"camera {cid} · " + (f"firmware {fw['build']}" if fw.get("build") else "firmware build unknown") \
            + f" · USB 3 · {fps:.2f} fps" + (f" · IMU {hz:.0f} Hz" if hz else "")
        if not fw.get("build"):
            return ("warn", summary + f" ({fw.get('why') or 'not one of the known builds'})")
        return summary

    def _camera_firmware(self):
        """Which DECXIN firmware build is fitted, read from sysfs by camfw/dockscan's
        own script and grader (never opens the camera)."""
        ds = board.dockscan()
        if ds is None:
            return {"build": None, "why": "camera/camfw/dockscan.py not found"}
        try:
            r = self._ssh("python3 - <<'QCPY'\n" + ds.REMOTE + "\nQCPY", timeout=30)
            cams = json.loads(r.stdout.strip().splitlines()[-1]).get("cameras") or []
        except (ValueError, IndexError, board.BoardError):
            return {"build": None, "why": "couldn't read the camera's USB descriptors"}
        if not cams:
            return {"build": None, "why": "no DECXIN on the USB bus"}
        build, hits = ds.grade(cams[0])
        prof = ds.PROFILES.get(build) or {}
        return {"build": build, "markers": hits, "ok": prof.get("ok"), "bcd": cams[0].get("bcd"),
                "verdict": prof.get("verdict")}

    def _t_calibration(self):
        self._note("calibration", "asking the dashboard…")
        res = calibration.check(self.dev.get("camera_id"), LIMITS)
        self.ctx["calibration"] = res.get("data") or {}
        for d in res.get("details") or []:
            self._detail("calibration", d)
        if res["state"] == "fail":
            raise StepFail(res["summary"])
        return (res["state"], res["summary"])

    def _t_wifi(self):
        model = (self.dev.get("model") or "").lower()
        if "nanopi" not in model and "r76s" not in model:
            return ("skip", "this board type has no WiFi module")
        self._note("wifi", "scanning…")
        r = self._ssh(
            'w=""; for n in /sys/class/net/*; do if [ -e "$n/phy80211" ] || [ -d "$n/wireless" ]; '
            'then w=$(basename "$n"); break; fi; done; echo "DEV=$w"; [ -n "$w" ] || exit 0; '
            'echo "DRV=$(basename "$(readlink -f /sys/class/net/$w/device/driver 2>/dev/null)")"; '
            'b=0; for r in /sys/class/rfkill/*; do [ "$(cat $r/type 2>/dev/null)" = wlan ] || continue; '
            '[ "$(cat $r/soft 2>/dev/null)" = 1 ] && b=1; [ "$(cat $r/hard 2>/dev/null)" = 1 ] && b=2; done; '
            'echo "RF=$b"; echo "CARRIER=$(cat /sys/class/net/$w/carrier 2>/dev/null)"; '
            'c=$(nmcli -t -f GENERAL.CONNECTION dev show "$w" 2>/dev/null | sed -n "s/^GENERAL.CONNECTION://p" | head -1); '
            'echo "SSID=$c"; '
            # a recorder can run its own hotspot on this radio; that is "carrier up" too,
            # but proves nothing about receiving, so it must not count as connected
            '[ -n "$c" ] && [ "$c" != "--" ] && echo "MODE=$(nmcli -t -g 802-11-wireless.mode connection show "$c" 2>/dev/null)"; '
            'echo "Q=$(awk -v i="$w:" \'$1==i{gsub(/[.]/,"",$3); print $3; exit}\' /proc/net/wireless 2>/dev/null)"; '
            'echo "SEEN=$(timeout 25 nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null | grep -c .)"',
            timeout=50)
        out = r.stdout
        dev, rf = _grab(out, "DEV"), _grab(out, "RF")
        self.ctx["wifi"] = {k.lower(): _grab(out, k) for k in ("DEV", "DRV", "RF", "CARRIER", "SSID", "MODE", "Q", "SEEN")}
        if not dev:
            raise StepFail("no WiFi interface: the module is missing or not detected (station B4)")
        if rf == "2":
            raise StepFail(f"the WiFi radio ({dev}) is hard-blocked")
        if rf == "1":
            raise StepFail(f"the WiFi radio ({dev}) is switched off in software (rfkill unblock wlan)")
        seen = _int(_grab(out, "SEEN"))
        ssid = _grab(out, "SSID")
        drv = _grab(out, "DRV") or "?"
        q = _grab(out, "Q")
        ap = _grab(out, "MODE") == "ap"
        heard = f"{seen} network{'' if seen == 1 else 's'}"
        if _grab(out, "CARRIER") == "1" and ssid not in ("", "--") and not ap:
            return (f"{dev} ({drv}) connected to {ssid}" + (f", link {q}/100" if q else "")
                    + (f" · hears {heard}" if seen else ""))
        if seen > 0:
            return f"{dev} ({drv}) hears {heard}" + (" (it is running a hotspot)" if ap else " (not joined to one)")
        if ap:
            return ("warn", f"{dev} ({drv}) is running a hotspot ({ssid}) and can't scan: reception not "
                            "confirmed. Check it joins a network before it ships")
        raise StepFail(f"{dev} is present but hears no networks at all: check the antenna (station B4)")

    def _t_image(self):
        import cv2
        st = self._status()
        if (st.get("camera") or {}).get("recording"):
            # a take is running: the preview then drops to 2 fps and a viewer costs
            # recording fps, so it is stopped first (only with the operator's say-so)
            self._ensure_idle("image")
        self._note("image", "grabbing frames…")
        frames = board.preview_frames(self.ip, count=4, timeout=20)
        decoded = [cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR) for f in frames]
        decoded = [d for d in decoded if d is not None]
        if len(decoded) < 2:
            raise StepFail("couldn't get two frames from the camera preview")
        h, w = decoded[0].shape[:2]
        if (w, h) != (LIMITS["frame_width"], LIMITS["frame_height"]):
            raise StepFail(f"the camera frame is {w}x{h}, expected {LIMITS['frame_width']}x{LIMITS['frame_height']}")
        eyes = [checks.raw_eyes(d) for d in decoded]
        metrics = [checks.eye_metrics(e) for e in eyes[-1]]
        motion = [max(checks.frame_motion(eyes[i][k], eyes[i + 1][k]) for i in range(len(eyes) - 1))
                  for k in (0, 1)]
        left, right = eyes[-1]
        self._store_frame(cv2.hconcat([left, right]), "live view, left eye | right eye")
        self.ctx["image"] = {"metrics": dict(zip(("left", "right"), metrics)),
                             "motion": dict(zip(("left", "right"), motion))}
        for nm, m, mo in zip(("left", "right"), metrics, motion):
            self._detail("image", f"{nm} eye: mean {m['mean']:.0f}, black {m['p1']:.0f}, white {m['p99']:.0f}, "
                                  f"contrast {m['michelson']:.2f}, change {mo:.2f}")
        problems, notes = checks.judge_eyes(metrics, motion, LIMITS)
        if problems:
            raise StepFail("; ".join(problems))
        if notes:
            return ("warn", "; ".join(notes))
        return "both eyes live, exposed normally"

    # ================================================================== 8-11: the take
    def _open_window(self):
        """Note what is on the card and the board's clock, right before QC can cause a
        take. From here on, a take this boot stamps inside the window is QC's (see the
        module doc), and the window is saved to disk so a QC run cut short by a crash
        or a logout is still cleaned up the next time this board docks."""
        takes, mono = board.list_takes(self.ip)
        self._mono_ref = time.monotonic()
        self.window = {"boot_id": self.ctx.get("boot_id"), "mono_start": mono, "mono_last": mono,
                       "pre_rids": sorted({t["rid"] for t in takes if t.get("rid")}),
                       "pre_runs": sorted(t["run"] for t in takes)}
        self.ctx["pre_existing_takes"] = [t["run"] for t in takes]
        self._persist_window()

    def _board_mono(self):
        """The board's monotonic clock now, extrapolated from the window start with the
        laptop's own monotonic clock (no extra round trip, immune to NTP steps)."""
        return self.window["mono_start"] + (time.monotonic() - self._mono_ref)

    def _persist_window(self, final=False):
        if not self.window:
            return
        self.window["mono_last"] = self._board_mono()
        if self.on_pending:
            try:
                self.on_pending(self, None if final else {
                    "cpu_id": self.cpu_id, "ip": self.ip, "name": self.dev.get("name"),
                    "run_id": self.id, "stem": self.stem, "since": self.started, "minutes": self.minutes,
                    "window": dict(self.window)})
            except Exception as e:                    # noqa: BLE001
                print(f"[qc] couldn't save the pending-cleanup record: {e}")

    def _claim(self, run_name, by):
        """Remember the take QC is measuring, by recording_id as well as by name."""
        self.take_run = run_name
        self.ctx["take_started_by"] = by
        try:
            takes, _mono = board.list_takes(self.ip)
            t = next((t for t in takes if t["run"] == run_name), None)
            if t:
                self.take_rid, self.take_dir = t.get("rid"), t["path"]
        except board.BoardError:
            pass                                      # by name for now; found by rid later

    def _same_take(self, run_name):
        """Is the take now called `run_name` the one QC is measuring (renamed), rather
        than a new take? By recording_id, or by the rename record the recorder keeps."""
        try:
            takes, _mono = board.list_takes(self.ip)
        except board.BoardError:
            return False                              # can't prove it's the same: treat as not
        t = next((t for t in takes if t["run"] == run_name), None)
        if not t:
            return False
        if self.take_rid:
            return t.get("rid") == self.take_rid
        if t.get("renamed_from") == self.take_run:
            self.take_rid = t.get("rid")
            return True
        return False

    def _locate_take(self):
        """The QC take's folder now: its name if that still exists, else wherever its
        recording_id moved to (a clock step renames it)."""
        takes, _mono = board.list_takes(self.ip)
        t = (next((t for t in takes if self.take_rid and t.get("rid") == self.take_rid), None)
             or next((t for t in takes if t["run"] == self.take_run), None))
        if not t:
            raise StepFail(f"the take {self.take_run} is no longer on the SD card")
        if t["run"] != self.take_run:
            self._detail("footage", f"the take was renamed {self.take_run} → {t['run']} (clock sync)")
        self.take_run, self.take_dir = t["run"], t["path"]
        self.take_rid = self.take_rid or t.get("rid")
        return t

    def _t_button(self):
        st = self._status()
        panel = st.get("panel") or {}
        if not panel.get("button_present"):
            raise StepFail("the board doesn't detect a button" + (f" ({panel['error']})" if panel.get("error") else ""))
        mode = panel.get("button_mode")
        if mode == "off":
            raise StepFail("the button is disabled in the recorder's settings (BUTTON=off)")
        self._ensure_idle("button")
        self._open_window()
        p0 = _int(panel.get("presses"))
        deadline = time.time() + LIMITS["button_wait_s"]
        self.prompt = {"title": "Press the StarCap's button once",
                       "text": "A short, firm press (don't hold it). This also starts the soak recording.",
                       "deadline": deadline}
        try:
            # a few seconds of grace after the countdown: a press in the last poll
            # interval still counts instead of starting a take nobody is watching
            while time.time() < deadline + 3:
                self._sleep(0.4)
                if time.time() > deadline:
                    self.prompt = None
                try:
                    st = self._status(timeout=3)
                except board.BoardError:
                    continue
                panel = st.get("panel") or {}
                if _int(panel.get("presses")) > p0:
                    self.prompt = None
                    self.ctx["button"] = {"presses_before": p0, "presses_after": panel.get("presses"), "mode": mode}
                    # in toggle/start mode that press starts a take
                    for _ in range(10):
                        cam = st.get("camera") or {}
                        if cam.get("recording") and cam.get("dir"):
                            self._claim(cam["dir"], "button")
                            break
                        self._sleep(0.5)
                        try:
                            st = self._status(timeout=4)
                        except board.BoardError:
                            st = {}
                    return f"press seen ({mode} mode)" + (", and it started the take" if self.take_run else "")
        finally:
            self.prompt = None
        raise StepFail(f"no press seen in {LIMITS['button_wait_s']} s. If it was pressed, check the "
                       "button's cable")

    def _t_soak(self):
        total_s = self.minutes * 60 + SOAK_EXTRA_S
        if not self.window:
            self._open_window()                   # never start a take QC couldn't later find
        if not self.take_run:
            self._note("soak", "starting the take…")
            try:
                board.http_json(self.ip, "/api/record/start", method="POST", timeout=25)
            except board.BoardError:
                pass                              # it answers late or not at all; status is the truth
            for _ in range(40):
                self._sleep(0.5)
                try:
                    cam = self._status().get("camera") or {}
                except board.BoardError:
                    continue
                if cam.get("recording") and cam.get("dir"):
                    self._claim(cam["dir"], "qc")
                    break
            else:
                st = self._status()
                why = (st.get("panel") or {}).get("led_state")
                raise StepFail("the recorder refused to start a take"
                               + (": SD card problem" if why == "fault_storage" else ""))
        self._detail("soak", f"take {self.take_run}, started by {self.ctx.get('take_started_by')}")
        self._note("soak", f"recording {_mmss(total_s)}…")
        st = self._status()
        cam = st.get("camera") or {}
        temps, fps_low, misses = [], [], 0
        up_last = (st.get("system") or {}).get("uptime") or 0
        frames_last = _int(cam.get("rec_frames"))
        t0 = time.monotonic()
        last_ok = t0
        last_persist = t0
        while True:
            el = time.monotonic() - t0
            if el >= total_s:
                break
            self._sleep(min(2.0, total_s - el))
            el = time.monotonic() - t0
            if time.monotonic() - last_persist > 30:
                self._persist_window()            # keeps the saved window covering the soak
                last_persist = time.monotonic()
            try:
                st = self._status(timeout=4)
            except board.BoardError:
                misses += 1
                if time.monotonic() - last_ok > 25:
                    raise StepFail(f"the board stopped answering {_mmss(el)} into the soak (rebooted or hung)")
                continue
            last_ok = time.monotonic()
            cam = st.get("camera") or {}
            sysd = st.get("system") or {}
            up = sysd.get("uptime") or 0
            if up and up + 5 < up_last:
                raise StepFail(f"the board rebooted {_mmss(el)} into the soak")
            up_last = max(up_last, up)
            if st.get("version") != self.ctx.get("version_at_start"):
                raise StepFail(f"the recorder updated itself mid-soak ({self.ctx.get('version_at_start')} → "
                               f"{st.get('version')}); run QC again")
            if not cam.get("recording"):
                why = (st.get("panel") or {}).get("led_state")
                raise StepFail(f"the take stopped by itself {_mmss(el)} into the soak"
                               + {"fault_storage": " (SD card fault)", "updating": " (updating)"}.get(why, ""))
            if cam.get("error"):
                raise StepFail(f"camera error {_mmss(el)} into the soak: {cam['error']}")
            frames = _int(cam.get("rec_frames"))
            if frames + 30 < frames_last:
                raise StepFail(f"the take restarted itself {_mmss(el)} into the soak (camera or SD dropped out)")
            frames_last = frames
            if cam.get("dir") and cam.get("dir") != self.take_run:
                # a new folder is either the same take renamed by a clock step, or the
                # recorder ending the take and resuming into a new one (camera or SD
                # dropout). Only its recording_id tells them apart: the frame counter
                # can't, when the restart lands between two polls.
                if self._same_take(cam["dir"]):
                    self._detail("soak", f"the take was renamed {self.take_run} → {cam['dir']} (clock sync)")
                    self.take_run = cam["dir"]
                else:
                    raise StepFail(f"the take restarted itself {_mmss(el)} into the soak, as {cam['dir']} "
                                   "(camera or SD dropped out)")
            t_all = [v for v in (sysd.get("temps") or {}).values() if isinstance(v, (int, float))]
            if t_all:
                temps.append(max(t_all))
            fps = cam.get("capture_fps")
            if isinstance(fps, (int, float)) and el > 10 and fps < LIMITS["fps_min"]:
                fps_low.append(round(el))
            with self._lock:
                self.live = {"t": el, "total_s": total_s, "fps": fps, "dropped": _int(cam.get("dropped")),
                             "rec_frames": frames, "temp": temps[-1] if temps else None,
                             "temp_max": max(temps) if temps else None, "imu_hz": cam.get("imu_hz")}
        # hottest moment of the run: is the CPU being throttled right now?
        throttled = self._throttled()
        self._note("soak", "stopping the take…")
        try:
            board.http_json(self.ip, "/api/record/stop", method="POST", timeout=30)
        except board.BoardError:
            pass
        self._wait_not_recording(60)
        r = self._ssh("cat /proc/sys/kernel/random/boot_id; "
                      "systemctl show stereocam -p MainPID -p NRestarts", timeout=30)
        lines = r.stdout.strip().splitlines()
        kv = dict(ln.split("=", 1) for ln in lines[1:] if "=" in ln)
        if lines and lines[0].strip() != self.ctx.get("boot_id"):
            raise StepFail("the board rebooted during the soak (its boot id changed)")
        if kv.get("MainPID") != self.ctx.get("main_pid") or _int(kv.get("NRestarts")) != self.ctx.get("nrestarts"):
            raise StepFail("the recorder service restarted during the soak")
        tmax = max(temps) if temps else None
        self.ctx["soak"] = {"seconds": total_s, "temp_max": tmax, "fps_low_at_s": fps_low[:50],
                            "rec_frames": frames_last, "status_misses": misses, "throttled": throttled}
        if throttled:
            raise StepFail(f"the CPU was being throttled at the end of the soak ({throttled}); "
                           "check the fan and heat sink (stations B2/B3)")
        if tmax is not None and tmax >= LIMITS["temp_fail_c"]:
            raise StepFail(f"reached {tmax:.1f} °C, the throttle point is {LIMITS['temp_fail_c']:.0f} °C; "
                           "check the fan and heat sink (stations B2/B3)")
        if len(fps_low) > LIMITS["soak_fps_dips_max"]:
            raise StepFail(f"the live frame rate fell under {LIMITS['fps_min']} fps {len(fps_low)} times")
        note = f"{_mmss(total_s)} recorded · {frames_last} frames" + \
               (f" · hottest {tmax:.1f} °C" if tmax is not None else "")
        if tmax is not None and tmax >= LIMITS["temp_warn_c"]:
            return ("warn", note + f" (over {LIMITS['temp_warn_c']:.0f} °C: check the fan)")
        return note

    def _t_timing(self):
        self._locate_take()
        d = self._delivered()
        if not d["available"]:
            return self._timing_from_recorder_csv(d["why"])
        # the customer's frame timeline: each video frame's exposure time as the delivery
        # decodes it from the IMU strip (one per frame, gaps filled or refused by its rules)
        parts = [np.diff(np.asarray(seg["exposures_us"], dtype=np.float64) / 1e6) for seg in d["segments"]]
        res = checks.interval_timing(parts, LIMITS["fps_nominal"], LIMITS["p99_tol_ms"] / 1e3,
                                     LIMITS["timing_max_out_pct"])
        filled = sum(int(seg.get("filled") or 0) for seg in d["segments"])
        no_header = sum(int(seg.get("no_header") or 0) for seg in d["segments"])
        self.ctx["timing"] = {"source": "delivered", "decoder": d["source"], "camera_clock": res,
                              "filled_headers": filled, "frames_without_header": no_header,
                              "recorder_csv": d.get("recorder_csv")}
        self._detail("timing", f"judged on the footage's IMU strip, decoded by the delivery's own code "
                               f"({d['source']})")
        if res.get("ok"):
            iv = res["interval_ms"]
            self._detail("timing", f"camera clock: median {iv['p50']:.3f} ms, p1 {iv['p1']:.3f}, "
                                   f"p99 {iv['p99']:.3f}, worst {iv['max']:.1f} ms, {res['late_gaps']} late frames")
        if no_header:
            self._detail("timing", f"{no_header} frame(s) carried no readable strip header "
                                   f"({filled} filled by the delivery's gap rules)")
        rc = d.get("recorder_csv") or {}
        if rc.get("max_interval_ms", 0) > 1000.0 / LIMITS["fps_nominal"] * 1.5:
            self._detail("timing", f"the recorder's own frames.csv has a {rc['max_interval_ms']:.0f} ms gap "
                                   "(its live log drops rows under load; the footage itself is complete)")
        if not res["pass"]:
            raise StepFail(res.get("reason") or "frame timing is outside the spec")
        if res["avg_fps"] < LIMITS["fps_min"]:
            raise StepFail(f"the take averaged {res['avg_fps']:.2f} fps (needs {LIMITS['fps_min']})")
        return (f"{res['outside_pct']:.2f}% off by >10 ms (limit {LIMITS['timing_max_out_pct']:g}%) · "
                f"p99 deviation {res['p99_dev_ms']:.2f} ms · {res['avg_fps']:.2f} fps over {res['frames']} frames")

    def _timing_from_recorder_csv(self, why):
        """Fallback when the delivery's decoder isn't available on this laptop: judge the
        recorder's own frames.csv. Stricter than it needs to be (its live log drops rows
        under load, which reads as late frames), and says so."""
        csv = self._read_take_file("frames.csv", max_mb=40)
        try:
            rows = np.genfromtxt(io.StringIO(csv), delimiter=",", names=True)
        except ValueError as e:
            raise StepFail(f"frames.csv doesn't parse ({e})") from None
        names = rows.dtype.names or ()
        if rows.size < 100 or "exposure_start_us" not in names:
            raise StepFail(f"frames.csv holds only {rows.size} frames")
        exp = checks.unwrap_u32(rows["exposure_start_us"]) / 1e6
        d = np.diff(exp)
        dup = int(np.sum(d == 0))
        dup_pct = 100.0 * dup / max(1, d.size)
        if dup_pct > LIMITS["dup_stamps_max_pct"]:
            raise StepFail(f"{dup_pct:.2f}% of the camera's frame timestamps repeat: the exposure "
                           f"({(self.ctx.get('camera') or {}).get('exposure_us') or '?'} µs) is too long or the "
                           "IMU strip is broken, so the frame timing can't be trusted")
        if dup:
            exp = exp[np.concatenate(([True], d != 0))]
        res = checks.interval_timing([np.diff(exp)], LIMITS["fps_nominal"], LIMITS["p99_tol_ms"] / 1e3,
                                     LIMITS["timing_max_out_pct"])
        self.ctx["timing"] = {"source": "recorder frames.csv", "why": why, "camera_clock": res,
                              "rows": int(rows.size), "duplicate_stamps": dup}
        self._detail("timing", f"judged on the recorder's frames.csv, not on the delivered data: {why}")
        if res.get("ok"):
            iv = res["interval_ms"]
            self._detail("timing", f"camera clock: median {iv['p50']:.3f} ms, p1 {iv['p1']:.3f}, "
                                   f"p99 {iv['p99']:.3f}, worst {iv['max']:.1f} ms, {res['late_gaps']} late frames")
        if not res["pass"]:
            raise StepFail(res.get("reason") or "frame timing is outside the spec")
        if res["avg_fps"] < LIMITS["fps_min"]:
            raise StepFail(f"the take averaged {res['avg_fps']:.2f} fps (needs {LIMITS['fps_min']})")
        return ("warn", f"{res['outside_pct']:.2f}% off by >10 ms · p99 deviation {res['p99_dev_ms']:.2f} ms · "
                        f"{res['avg_fps']:.2f} fps (from the recorder's frames.csv: the delivery decoder isn't "
                        "available here)")

    def _delivered(self):
        """The QC take as the delivery pipeline decodes it, once per run: the strip files
        are downloaded over the recorder's own route into a temporary folder, decoded with
        the delivery's own code (delivery.py), and deleted again. Nothing but logs is kept."""
        if self._deliv is not None:
            return self._deliv
        mods, source = delivery.load()
        if mods is None:
            self._deliv = {"available": False, "why": f"the delivery decoder isn't available ({source})"}
            return self._deliv
        listing = board.http_json(self.ip, "/api/recordings", timeout=20)
        entry = next((r for r in listing.get("recordings") or [] if r.get("run") == self.take_run), None)
        if not entry:
            raise StepFail(f"the recorder doesn't list the take {self.take_run}")
        strips = sorted(f.get("name") for f in entry.get("files") or []
                        if re.fullmatch(r"imustrip_\d{3}\.mp4", str(f.get("name") or "")))
        if not strips:
            raise StepFail("the take has no imustrip_*.mp4: no IMU strip to decode")
        segments = []
        with tempfile.TemporaryDirectory(prefix="starcap-qc-") as tmp:
            witness = Path(tmp) / "frames.csv"
            witness.write_text(self._read_take_file("frames.csv", max_mb=40))
            recorder_csv = _csv_gap_ms(witness)
            for name in strips:
                self._check_cancel()
                self._note("timing", f"decoding {name} the way delivery does…")
                dest = Path(tmp) / name
                board.download(self.ip, self.take_run, name, dest)
                try:
                    seg = delivery.decode_strip(mods, dest, witness)
                except delivery.Refused as e:
                    raise StepFail(f"delivery would refuse this footage: {e}") from None
                finally:
                    dest.unlink(missing_ok=True)
                seg["name"] = name
                segments.append(seg)
        self._deliv = {"available": True, "source": source, "segments": segments, "recorder_csv": recorder_csv}
        return self._deliv

    def _t_footage(self):
        self._locate_take()
        final = self._wait_finalized()
        self.ctx["final_status"] = {k: final.get(k) for k in
                                    ("duration_s", "rec_frames", "dropped_pts_gaps", "streams_ok",
                                     "imu_samples", "imu_hz", "bytes", "files")}
        problems, notes = [], []
        if final.get("corrupt"):
            problems.append("the recorder marked this take corrupt (.corrupt)")
        if final.get("streams_ok") is not True:
            problems.append("the recorder's own check says the streams are not OK")
        files = final.get("files") or {}
        streams = {}
        for name, info in sorted(files.items()):
            m = re.match(r"^(left|right|imustrip)_(\d{3})\.mp4$", name)
            if not m:
                continue
            streams.setdefault(m.group(1), {})[m.group(2)] = info or {}
            if (info or {}).get("error"):
                problems.append(f"{name}: {info['error']}")
        for s in ("left", "right", "imustrip"):
            if s not in streams:
                problems.append(f"no {s}_*.mp4 in the take")
        segs = sorted(set().union(*[set(v) for v in streams.values()])) if streams else []
        if segs and segs != [f"{i:03d}" for i in range(len(segs))]:
            problems.append(f"files are missing from the take (have {', '.join(segs)})")
        for seg in segs:
            counts = {s: (streams.get(s, {}).get(seg) or {}).get("frames") for s in ("left", "right", "imustrip")}
            if len(set(counts.values())) > 1:
                problems.append(f"file {seg}: the streams hold different frame counts {counts}")
        per_eye = sum(_int((streams.get("left", {}).get(seg) or {}).get("frames")) for seg in segs)
        rec_frames = _int(final.get("rec_frames"))
        if rec_frames and per_eye:
            off = 100.0 * abs(per_eye - rec_frames) / rec_frames
            if off > LIMITS["video_vs_rec_frames_pct"]:
                problems.append(f"the files hold {per_eye} frames per eye but the recorder recorded {rec_frames}")
        lo, hi = LIMITS["file_fps_range"]
        for seg in segs:
            for s in ("left", "right"):
                info = streams.get(s, {}).get(seg) or {}
                if info and not info.get("error"):
                    if info.get("codec") != "hevc":
                        problems.append(f"{s}_{seg}.mp4 is {info.get('codec')}, not HEVC")
                    fr = _fraction(info.get("avg_frame_rate"))
                    if fr is not None and not lo <= fr <= hi:
                        problems.append(f"{s}_{seg}.mp4 runs at {fr:.2f} fps")
        for name, (w, h) in self._probe_dims().items():
            if (w, h) != (LIMITS["eye_width"], LIMITS["eye_height"]):
                problems.append(f"{name} is {w}x{h}, expected {LIMITS['eye_width']}x{LIMITS['eye_height']}")
        eyes = self._eye_correction()
        if not eyes.get("ok"):
            problems.append(eyes.get("why") or "left_/right_ files aren't mapped to the physical eyes")
        d = self._deliv or {}
        if d.get("available"):
            delivered = sum(int(seg.get("frames") or 0) for seg in d["segments"])
            if per_eye and delivered != per_eye:
                problems.append(f"the decoded IMU strip covers {delivered} frames but the eyes hold {per_eye}")
            imu = self._imu_from_delivered(d)
        else:
            # fallback: the recorder's own CSVs (the delivery decoder isn't available here)
            rows = (self.ctx.get("timing") or {}).get("rows")
            if rows and per_eye:
                off = 100.0 * abs(per_eye - rows) / per_eye
                if off > LIMITS["csv_rows_fail_pct"]:
                    problems.append(f"frames.csv has {rows} rows for {per_eye} video frames, so the frame "
                                    "timing above doesn't describe this footage")
                elif off > LIMITS["csv_rows_warn_pct"]:
                    notes.append(f"frames.csv has {rows} rows for {per_eye} video frames")
            imu = self._imu_check()
        if imu.get("fail"):
            problems.append(imu["fail"])
        elif imu.get("warn"):
            notes.append(imu["warn"])
        self._pair_snapshot()
        self._detail("footage", f"{len(segs)} file(s) per stream, {per_eye} frames per eye "
                                f"({rec_frames} recorded), {_int(final.get('dropped_pts_gaps'))} pts gaps, "
                                f"{final.get('duration_s') or 0:.0f} s")
        if problems:
            raise StepFail(problems[0], problems[1:6])
        summary = (f"{len(segs)} file(s) per stream, all finalised · HEVC 1920×1200 @ 60 · "
                   f"eyes un-swapped · IMU {imu.get('rate_hz', 0):.0f} Hz, g {imu.get('gravity_ms2', 0):.2f}")
        if notes:
            return ("warn", summary + " · " + "; ".join(notes))
        return summary

    # ================================================================== helpers
    def _store_frame(self, bgr, caption):
        import cv2
        small = cv2.resize(bgr, (960, 300), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 84])
        if ok:
            with self._lock:
                self.frame_jpeg = buf.tobytes()
                self.frame_caption = caption
                self.frame_seq += 1

    def _ensure_idle(self, key):
        """No other take may run while QC tests. One the operator agreed to stop is
        stopped (it stays on the card); otherwise QC refuses rather than end it."""
        st = self._status()
        cam = st.get("camera") or {}
        if not cam.get("recording"):
            return
        if not self.stop_take_ok:
            raise StepFail("the board is recording a take; start QC again and agree to stop it")
        self._note(key, f"stopping the take that was running ({cam.get('dir')})…")
        try:
            board.http_json(self.ip, "/api/record/stop", method="POST", timeout=30)
        except board.BoardError:
            pass
        self._wait_not_recording(60)
        self._detail(key, f"stopped the take that was running ({cam.get('dir')}); it is kept on the card")
        self.ctx["stopped_existing_take"] = cam.get("dir")

    def _wait_not_recording(self, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                st = self._status(timeout=5)
                if not (st.get("camera") or {}).get("recording"):
                    return st
            except board.BoardError:
                pass
            self._sleep(1.0)
        raise StepFail(f"the recorder didn't stop the take within {timeout} s")

    def _wait_finalized(self):
        """The recorder checks every file of a finished take in the background
        (ffprobe, then final_status.json). Wait for it: it takes ~12 s per recorded
        minute. A take renamed meanwhile is followed by its recording_id."""
        limit = 120 + 25 * self.minutes
        t_end = time.time() + limit
        self._note("footage", "waiting for the recorder to check the files…")
        while time.time() < t_end:
            d = shlex.quote(self.take_dir)
            r = self._ssh(f'cd {d} 2>/dev/null || exit 3; [ -e .finalizing ] && {{ echo BUSY; exit 0; }}; '
                          '[ -f final_status.json ] || { echo BUSY; exit 0; }; '
                          '[ -e .corrupt ] && echo CORRUPT; echo JSON; cat final_status.json', timeout=40)
            if r.returncode == 3:
                self._locate_take()               # renamed, or gone (that raises)
                continue
            if "JSON" in r.stdout.splitlines():
                body = r.stdout.split("JSON\n", 1)[1]
                try:
                    final = json.loads(body)
                except ValueError:
                    raise StepFail("final_status.json doesn't parse") from None
                final["corrupt"] = "CORRUPT" in r.stdout.splitlines()
                return final
            self._sleep(3.0)
        raise StepFail(f"the recorder hadn't finished checking the take after {limit // 60} min")

    def _throttled(self):
        """On a `performance` policy, scaling_max_freq below the hardware max can only
        be the thermal governor pulling it down."""
        try:
            r = self._ssh('for p in /sys/devices/system/cpu/cpufreq/policy*; do '
                          'echo "$(basename $p) $(cat $p/scaling_governor) $(cat $p/scaling_max_freq) '
                          '$(cat $p/cpuinfo_max_freq)"; done', timeout=20)
        except board.BoardError:
            return ""
        hit = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[1] == "performance" and parts[2].isdigit() and parts[3].isdigit():
                if int(parts[2]) < int(parts[3]):
                    hit.append(f"{parts[0]} capped at {int(parts[2]) // 1000} of {int(parts[3]) // 1000} MHz")
        return "; ".join(hit)

    def _read_take_file(self, name, max_mb):
        p = shlex.quote(f"{self.take_dir}/{name}")
        r = self._ssh(f'[ -f {p} ] || {{ echo MISSING; exit 3; }}; '
                      f's=$(stat -c%s {p}); [ "$s" -le {int(max_mb * 1e6)} ] || {{ echo "TOOBIG=$s"; exit 4; }}; '
                      f'cat {p}', timeout=120)
        if r.returncode == 3:
            raise StepFail(f"the take has no {name}")
        if r.returncode == 4:
            raise StepFail(f"{name} is unexpectedly large ({r.stdout.strip()})")
        if r.returncode != 0:
            raise StepFail(f"couldn't read {name}: {(r.stderr or '').strip()[:120]}")
        return r.stdout

    def _probe_dims(self):
        """Width x height of every eye file, from the container header (cheap: no
        frame counting, final_status already counted them)."""
        d = shlex.quote(self.take_dir)
        r = self._ssh(f'cd {d} || exit 3; for f in left_*.mp4 right_*.mp4; do [ -f "$f" ] || continue; '
                      'echo "$f $(ffprobe -v error -select_streams v:0 -show_entries stream=width,height '
                      '-of csv=p=0:s=x "$f" 2>/dev/null)"; done', timeout=60)
        out = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and "x" in parts[1]:
                w, _, h = parts[1].partition("x")
                if w.isdigit() and h.isdigit():
                    out[parts[0]] = (int(w), int(h))
            elif len(parts) == 1:
                out[parts[0]] = (0, 0)
        return out

    def _eye_correction(self):
        """The recorder un-swaps the DECXIN's eyes when it writes left_/right_, and says
        so in the take's device_info.json. That record, not image matching, is what
        proves the eye order: on raw fisheye frames the two lenses' optical-centre
        offset outweighs true disparity beyond ~1 m, so a feature match on a far wall
        reads 'swapped' on a correct camera."""
        try:
            raw = self._read_take_file("device_info.json", max_mb=1)
            info = json.loads(raw)
        except (StepFail, ValueError):
            return {"ok": False, "why": "the take has no readable device_info.json"}
        ec = info.get("eye_correction") or {}
        self.ctx["eye_correction"] = ec
        self.ctx["recording_id"] = info.get("recording_id")
        if not ec.get("applied") or ec.get("left_file_eye") != "left" or ec.get("right_file_eye") != "right":
            return {"ok": False, "why": "the recording isn't eye-corrected: left_/right_ may hold the wrong eyes"}
        return {"ok": True}

    def _pair_snapshot(self):
        """One frame of the recorded left_ and right_ files, side by side, for the card:
        what the customer will actually receive."""
        import cv2
        d = shlex.quote(self.take_dir)
        try:
            r = self._ssh(f'cd {d} || exit 3; t=$(mktemp -d); for e in left right; do '
                          'ffmpeg -v error -y -ss 20 -i ${e}_000.mp4 -frames:v 1 -vf scale=640:-2 -q:v 4 $t/$e.jpg '
                          '&& echo "B64_$e=$(base64 -w0 $t/$e.jpg)"; done; rm -rf "$t"', timeout=60)
        except board.BoardError:
            return
        imgs = []
        for e in ("left", "right"):
            m = re.search(rf"^B64_{e}=(.+)$", r.stdout, re.M)
            if not m:
                return
            img = cv2.imdecode(np.frombuffer(base64.b64decode(m.group(1)), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return
            imgs.append(img)
        if imgs[0].shape == imgs[1].shape:
            self._store_frame(cv2.hconcat(imgs), "recorded left_000 | right_000")

    def _imu_from_delivered(self, d):
        """The IMU exactly as delivered: the samples the delivery decoded from the strip,
        segment by segment (each file's clock is its own; no gap is invented between files)."""
        ts, acc, gyr = [], [], []
        offset = 0.0
        for seg in d["segments"]:
            smp = seg.get("samples") or []
            if not smp:
                continue
            t = np.asarray([x[0] for x in smp], dtype=np.float64) / 1e6
            step = float(np.median(np.diff(t))) if t.size > 1 else 0.0
            ts.append(t - t[0] + offset)
            offset = ts[-1][-1] + step
            acc.append(np.asarray([x[1] for x in smp], dtype=np.float64))
            gyr.append(np.asarray([x[2] for x in smp], dtype=np.float64))
        if not ts:
            return {"fail": "the footage's IMU strip carried no samples"}
        return self._judge_imu(np.concatenate(ts), np.concatenate(acc), np.concatenate(gyr), "delivered")

    def _judge_imu(self, t, accel, gyro, source):
        res = checks.imu_stats(t, accel, gyro, still_dps=LIMITS["imu_still_dps"])
        res["source"] = source
        self.ctx["imu"] = res
        if not res.get("ok"):
            return {"fail": res.get("reason") or "the IMU data is unusable"}
        if res["rate_hz"] < LIMITS["imu_min_hz"]:
            res["fail"] = f"the IMU recorded {res['rate_hz']:.0f} Hz (needs {LIMITS['imu_min_hz']:.0f})"
        elif res["accel_flat"]:
            res["fail"] = "the accelerometer is stuck on one value"
        elif res["gyro_flat"]:
            res["fail"] = "the gyroscope is stuck on one value"
        elif res["nonincreasing"]:
            res["fail"] = f"{res['nonincreasing']} IMU timestamps repeat or go backwards"
        elif res["max_gap_ms"] > LIMITS["imu_gap_fail_ms"]:
            res["fail"] = (f"the IMU stream has a {res['max_gap_ms']:.0f} ms hole "
                           f"(delivery refuses over {LIMITS['imu_gap_fail_ms']:.0f} ms)")
        elif abs(res["gravity_ms2"] - checks.G) > LIMITS["gravity_tol"]:
            res["fail"] = (f"gravity reads {res['gravity_ms2']:.2f} m/s² at rest (should be "
                           f"{checks.G:.2f} ± {LIMITS['gravity_tol']}): the IMU is miscalibrated or broken")
        elif res["rate_hz"] < LIMITS["imu_low_hz"]:
            res["warn"] = f"IMU at {res['rate_hz']:.0f} Hz (delivery wants {LIMITS['imu_low_hz']:.0f})"
        elif res["max_gap_ms"] > LIMITS["imu_gap_warn_ms"]:
            res["warn"] = f"the IMU stream has a {res['max_gap_ms']:.0f} ms gap"
        return res

    def _imu_check(self):
        """Fallback: the recorder's own imu.csv (when the delivery decoder isn't here)."""
        csv = self._read_take_file("imu.csv", max_mb=150)
        try:
            a = np.genfromtxt(io.StringIO(csv), delimiter=",", names=True)
        except ValueError as e:
            return {"fail": f"imu.csv doesn't parse ({e})"}
        need = ("ts_us", "ax_ms2", "ay_ms2", "az_ms2", "gx_dps", "gy_dps", "gz_dps")
        if a.size < 100 or not all(n in (a.dtype.names or ()) for n in need):
            return {"fail": f"imu.csv holds only {a.size} usable rows"}
        return self._judge_imu(checks.unwrap_u32(a["ts_us"]) / 1e6,
                               np.column_stack([a["ax_ms2"], a["ay_ms2"], a["az_ms2"]]),
                               np.column_stack([a["gx_dps"], a["gy_dps"], a["gz_dps"]]), "recorder imu.csv")

    def _keep_logs(self, take):
        files = save_take_logs(self.ip, take, self.logs_dir / take["run"])
        logs = self.ctx.setdefault("logs", {"dir": str(self.logs_dir), "files": []})
        logs["files"] += [f"{take['run']}/{f}" for f in files]

    def _save_journal(self):
        """The recorder's own log for the length of this run, kept with the report.
        Best effort: a board that never answered has nothing to give."""
        if not self.logs_dir or not self.ctx.get("boot_id"):
            return
        minutes = math.ceil((time.time() - (self.started or time.time())) / 60.0) + 1
        try:
            r = self._ssh(f'journalctl -u stereocam --since "-{minutes} min" --no-pager -o short-iso 2>&1 '
                          '| tail -c 4000000', timeout=60)
        except board.BoardError:
            return
        if r.stdout.strip():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            (self.logs_dir / "stereocam.log").write_text(r.stdout)
            self.ctx.setdefault("logs", {"dir": str(self.logs_dir), "files": []})["files"].append("stereocam.log")

    def _cleanup(self):
        """Stop and delete every take QC caused, keeping its logs. Runs after every
        run, whatever happened, and never raises. A failure here fails the run."""
        if not self.window:
            self._set("cleanup", state="skip", summary="QC didn't record anything")
            return
        self._set("cleanup", state="running", summary="removing the test take…")
        self._persist_window()
        try:
            state, summary, settled = remove_qc_takes(self.ip, self.window, self.minutes,
                                                      note=lambda s: self._note("cleanup", s),
                                                      keep=self._keep_logs if self.logs_dir else None)
        except Exception as e:                        # noqa: BLE001  cleanup must never raise
            state, summary, settled = "fail", f"couldn't finish removing the test take ({e}); it will be retried", False
        self._set("cleanup", state=state, summary=summary)
        if settled:
            self._persist_window(final=True)          # nothing left that a retry could fix


def remove_qc_takes(ip, window, minutes, note=lambda s: None, budget_s=None, keep=None):
    """Stop, then delete, every take QC caused on this board -> (state, summary, settled).
    `settled` is False only when a retry could still help (the board stopped answering,
    a delete didn't go through); the caller keeps the window on disk until then.
    `keep(take)`, when given, is called once per take right before it is deleted (after
    the recorder's own post-take check), to copy its logs off the board. A failure to
    keep the logs never keeps the footage: it is reported, and the take still goes.

    A take is QC's when its device_info.json carries the QC window's boot id and a
    start time (board CLOCK_MONOTONIC) inside the window, and its recording_id was not
    already on the card when the window opened. That positively identifies the takes
    QC caused, whatever their names now are, including ones it never claimed (a take
    the recorder restarted by itself; one a late button press started), and it can
    never match the operator's own footage, which either predates the window or was
    recorded after it (later, or on another boot). Used by a finishing QC run and by
    the clean-up of an interrupted one."""
    lo = window["mono_start"] - 2.0
    hi = window["mono_last"] + 10.0
    pre = set(window.get("pre_rids") or [])

    def is_qc(t):
        sm = t.get("started_mono")
        return (t.get("boot_id") == window["boot_id"] and isinstance(sm, (int, float))
                and lo <= sm <= hi and t.get("rid") not in pre)

    budget = budget_s if budget_s is not None else 120 + 25 * minutes
    t_end = time.time() + budget
    # 1) a QC take still recording is stopped first (the delete route refuses an active take)
    _stop_if_qc_recording(ip, is_qc)
    deleted, kept, keep_errors = [], set(), []
    takes = []
    while True:
        try:
            takes, _mono = board.list_takes(ip)
        except board.BoardError:
            if time.time() > t_end:
                return ("fail", "couldn't list the takes on the board to remove the test take; "
                                "it will be retried when the board is back", False)
            time.sleep(3)
            continue
        mine = [t for t in takes if is_qc(t)]
        if not mine:
            break
        if time.time() > t_end:
            return ("fail", f"couldn't delete the test take{'s' if len(mine) > 1 else ''} "
                            f"{', '.join(t['run'] for t in mine)}; it will be retried", False)
        progressed = False
        for t in mine:
            # the recorder's own post-take check must not have files vanish under it;
            # but a marker left by a finalize that died never clears, so the last
            # stretch of the budget deletes regardless
            if t.get("finalizing") and time.time() < t_end - 30:
                note(f"waiting for the recorder to finish checking {t['run']}…")
                continue
            if keep and t.get("rid") not in kept:
                note(f"saving the logs of {t['run']}…")
                try:
                    keep(t)
                except Exception as e:            # noqa: BLE001  the footage goes regardless
                    keep_errors.append(f"{t['run']}: {e}")
                kept.add(t.get("rid"))
            try:
                code = board.delete_run(ip, t["run"])
            except board.BoardError:
                continue
            if code == 200:
                deleted.append(t["run"])
                progressed = True
            elif code == 409:
                _stop_if_qc_recording(ip, is_qc)  # it is being recorded: stop it, then retry
            # 404: renamed under us; the next listing finds it by its recording_id
        if not progressed:
            time.sleep(3)
    # a take that appeared after the window opened but carries no identity (no
    # device_info.json) is never deleted blind: it may be QC's, so the run fails and a
    # person looks. A retry can't settle it either, so it is not retried.
    pre_runs = set(window.get("pre_runs") or [])
    unidentified = [t["run"] for t in takes
                    if t.get("boot_id") is None and t.get("started_mono") is None
                    and t["run"] not in pre_runs and t["run"] not in deleted]
    if unidentified:
        return ("fail", f"found {', '.join(unidentified)} on the card with no device_info.json; it may be a "
                        "test take, so check it and delete it by hand before this StarCap ships"
                        + (f" (deleted {', '.join(deleted)})" if deleted else ""), True)
    if not deleted:
        return "skip", "QC didn't leave a take on the card", True
    msg = (f"deleted the test take{'s' if len(deleted) > 1 else ''} {', '.join(deleted)} from the SD card"
           + ("" if not keep else
              "; its logs are kept on this laptop" if not keep_errors else
              f"; couldn't keep its logs ({'; '.join(keep_errors)[:160]})"))
    return "pass", msg, True


# A take's logs: everything in its folder but the video. Copied to the laptop before
# the take is deleted, so a failure can still be looked into after the footage is gone.
LOG_FILES = ("frames.csv", "imu.csv", "metrics.csv", "device_info.json", "eye_correction.json",
             "final_status.json")
GZIP_LOGS = ("frames.csv", "imu.csv")      # tens of MB of numbers: kept gzipped


def save_take_logs(ip, take, dest):
    """Copy a take's log files (no video) into `dest` on this laptop -> [file names].
    One SSH round trip: tar+gzip on the board, base64 over the wire."""
    d = shlex.quote(take["path"])
    names = " ".join(LOG_FILES)
    r = board.ssh(ip, f'cd {d} 2>/dev/null || exit 3; set --; for f in {names}; do '
                      '[ -f "$f" ] && set -- "$@" "$f"; done; [ $# -gt 0 ] || exit 4; '
                      'tar czf - "$@" | base64 -w0', timeout=180)
    if r.returncode == 3:
        raise board.BoardError("its folder is gone")
    if r.returncode == 4:
        return []
    if r.returncode != 0:
        raise board.BoardError((r.stderr or "tar failed").strip()[:120])
    try:
        raw = base64.b64decode(r.stdout.strip(), validate=True)
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (ValueError, tarfile.TarError) as e:
        raise board.BoardError(f"the logs came back unreadable ({e})") from None
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    with tf:
        for m in tf.getmembers():
            name = os.path.basename(m.name)
            if not m.isfile() or name not in LOG_FILES:
                continue                          # only the files asked for, never a path
            data = tf.extractfile(m).read()
            if name in GZIP_LOGS:
                (dest / (name + ".gz")).write_bytes(gzip.compress(data))
                saved.append(name + ".gz")
            else:
                (dest / name).write_bytes(data)
                saved.append(name)
    return sorted(saved)


def _stop_if_qc_recording(ip, is_qc):
    try:
        st = board.status(ip, timeout=6)
        cam = st.get("camera") or {}
        if not cam.get("recording"):
            return
        takes, _mono = board.list_takes(ip)
        cur = next((t for t in takes if t["run"] == cam.get("dir")), None)
        if cur is None or not is_qc(cur):
            return                                    # not QC's: never stop someone else's take
        board.http_json(ip, "/api/record/stop", method="POST", timeout=30)
    except board.BoardError:
        return
    for _ in range(60):
        time.sleep(1)
        try:
            if not (board.status(ip, timeout=6).get("camera") or {}).get("recording"):
                return
        except board.BoardError:
            continue


# ---------------------------------------------------------------------- helpers
_LATEST = {"at": 0.0, "val": (None, "", False)}
_LATEST_LOCK = threading.Lock()


def _latest_version():
    """origin/main's VERSION after a fetch (dock-check's yardstick), cached 5 min."""
    with _LATEST_LOCK:
        if time.time() - _LATEST["at"] > 300 or not _LATEST["val"][0]:
            _LATEST["val"] = board.dock_check.latest_version(board.dock_check.LOCAL_REPO)
            _LATEST["at"] = time.time()
        return _LATEST["val"]


def _csv_gap_ms(path):
    """{rows, max_interval_ms} of a recorder frames.csv on the camera clock (info only)."""
    try:
        rows = np.genfromtxt(str(path), delimiter=",", names=True)
        exp = checks.unwrap_u32(rows["exposure_start_us"])
        return {"rows": int(rows.size), "max_interval_ms": float(np.max(np.diff(exp)) / 1e3) if exp.size > 1 else 0.0}
    except (ValueError, KeyError, IndexError, TypeError):
        return {}


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _grab(out, key):
    m = re.search(rf"^{re.escape(key)}=(.*)$", out or "", re.M)
    return m.group(1).strip() if m else ""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _fraction(s):
    try:
        n, _, d = str(s).partition("/")
        return float(n) / float(d) if d else float(n)
    except (ValueError, ZeroDivisionError):
        return None


def _short_model(m):
    return re.sub(r"^(FriendlyElec|FriendlyARM|Xunlong|Rockchip)\s+", "", m or "", flags=re.I).strip() or "?"


def _mmss(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _trim_status(st):
    cam = st.get("camera") or {}
    return {"version": st.get("version"), "device_id": st.get("device_id"), "name": st.get("name"),
            "record_gate": st.get("record_gate"), "autostart": st.get("autostart"),
            "camera": {k: cam.get(k) for k in ("camera_id", "card", "width", "height", "fps_target",
                                               "capture_fps", "usb", "error")},
            "panel": st.get("panel"), "temps": (st.get("system") or {}).get("temps")}


def _jsonable(o):
    """numpy scalars and the like -> plain JSON types, so a report always writes."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o
