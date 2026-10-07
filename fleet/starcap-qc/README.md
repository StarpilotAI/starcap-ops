# StarCap QC

The last station on the line. Dock a finished StarCap over ethernet, press **Start QC**,
and the tool tests it end to end. A unit can only be named here once it has passed
everything here, and naming is opm-name's own dashboard rename. (opm-name and the
dashboard can still name a board directly; this tool is the gate on the line, not a
lock on the fleet.)

```bash
./install.sh          # deps, the `starcap-qc` command, the desktop icon
starcap-qc            # or double-click "StarCap QC"  ->  http://127.0.0.1:8772
```

It needs the whole starcap-ops checkout: it imports `fleet/opm-name` (logins, cpu_id,
naming), `fleet/dock-check` (dock discovery, "is the software right") and
`camera/camfw/dockscan.py` (camera firmware build) from their folders, so a fix in
any of them lands here too. "Latest version" comes from `~/orangepi-recorder`'s
`origin/main`, fetched, exactly like dock-check.

## What it checks

The quick checks run first and all of them run, so every problem shows at once. If
any of them fails, QC stops there (no button prompt, no soak): fix it and run again.

| # | Check | Passes when |
|---|---|---|
| 1 | Board answers | SSH login works, the recorder service is up, `record_gate` is manual, no opm-lan on this laptop and no upload server on the network (either would end or steal the test take) |
| 2 | Recorder software | dock-check says READY: latest `origin/main` version, auto-update on, tracking main. IMPLICIT (works, but not pinned) is a warning |
| 3 | eMMC + SD card | boots from eMMC; SD card mounted read-write, 32 GB+, 5 GB free, a test write works, and **no old bootloader in sectors 64-2047** (that hangs an R76S at boot). If that area can't be read it is a warning, not a pass. eMMC wear is reported |
| 4 | Camera | unit ID burned in; **clean firmware build** (`static-16640`; the `dither-16626` build fails the timing spec under changing light, and an unrecognised build is a warning); USB 3 (5000M); 4000×1200 @ 60; a true ≥59.5 fps by frame counter; IMU ≥200 Hz |
| 5 | Calibration | the dashboard holds a KB4 calibration for this exact lens id (`63.1`, never the bare `63`) that is **field ready by the dashboard's own rule**: evidence complete, every required media category declared, the bench's verdict an unqualified PASS. FOV computed from it ≥ 110°×80°; baseline 55-65 mm; eyes the right way round. White balance is not required. An unfinished evidence upload says `upload_audit.py --fix`, not "recalibrate" |
| 6 | WiFi module | interface present, not rfkill-blocked, and it hears networks or is joined to one (a hotspot of its own doesn't count as joined) |
| 7 | Live image | both eyes show a live picture: not black, flat or frozen. If neither eye shows anything it fails too, with a hint (lens caps? lights off? a blank wall?) |

Then:

| # | Check | Passes when |
|---|---|---|
| 8 | Button | the operator presses it and the board's press counter moves. The press also starts the soak take (toggle mode) |
| 9 | Soak recording | 5 or 10 minutes (picked per run) + 15 s, so the take crosses a 5-minute file boundary. The take never stops or restarts by itself, no reboot (boot id), no recorder restart (PID), no recorder self-update, no CPU throttling, hottest zone under 85 °C (over 80 °C warns), live fps holds |
| 10 | Frame timing | the customer's spec (Robotics Data Requirements §2.4) on **the data the customer receives**: each video frame's exposure time as the delivery decodes it from the footage's IMU strip, with the delivery's own code. At most **1%** of frame intervals outside 16.67 ms ±10 ms, average ≥59.5 fps. A strip the delivery would refuse fails. (No starcap-dashboard checkout: it falls back to the recorder's `frames.csv` and warns) |
| 11 | Footage | the recorder's own post-take check (`final_status.json`) passed; every file finalised and none missing; left/right/imustrip agree file by file and with what the recorder recorded; HEVC 1920×1200 @ 60 per eye; the take is eye-corrected (left_ = physical left eye); the decoded strip covers every video frame; the IMU **as delivered** (decoded from the strip): ≥200 Hz (under 550 warns), no hole over 250 ms, gravity 9.81 ±0.8 at rest, neither the accelerometer nor the gyroscope stuck |
| 12 | Remove test take | the take's **logs are copied to this laptop**, then every take QC caused is stopped and deleted from the SD card, so no test footage is ever uploaded. **A run whose clean-up fails does not pass** |

A **warning** never blocks naming; it flags something worth a look that QC can't
decide on its own. Every pass/fail threshold is in `LIMITS` at the top of `qc.py`.

## Why the delivered data, not the recorder's CSVs

The recorder writes its own `frames.csv` and `imu.csv` live, from a branch that drops
rows under a moment of load. On starcap91 (2026-10-07) that left a 0.57 s hole in both
files while the footage, and the IMU strip inside it, were complete. The delivery
pipeline never ships those files: it decodes the IMU and every frame's exposure time
from the strip (`imustrip_NNN.mp4`) and only uses `frames.csv` as a witness for a
missing header. So QC downloads the strip, runs the delivery's own decoder on it
(`delivery/imu_csv.py` + `firstSample/lib/imu_strip.py`, taken from starcap-dashboard's
`origin/main` and cached in `.delivery_cache/`), judges timing and IMU on its output,
and deletes the strip again. A hole in the recorder's CSVs is reported as a note.

## Logs

The test footage is deleted; its logs are kept on this laptop, next to the run's
report: `reports/<run>/<take>/` holds `frames.csv.gz`, `imu.csv.gz`, `metrics.csv`,
`device_info.json`, `final_status.json` and `eye_correction.json`, and
`reports/<run>/stereocam.log` is the recorder's journal for the run. The **Logs**
button on a finished run (and in the history) opens that folder.

## Test takes, and why the operator's footage is safe

Right before the button prompt QC notes the board's boot id and its monotonic clock.
A take is QC's when the recorder stamped it (in its `device_info.json`) with that boot
id and a start time inside the QC window. So clean-up also removes takes QC never
claimed: one the recorder restarted by itself after a camera or SD dropout, or one a
late press started. Names are never trusted (the recorder renames takes when NTP
steps the clock), and a delete only counts once the take is really gone. A take from
outside the window (the operator's own footage, earlier, later, or on another boot)
can't match, so it is never stopped or deleted.

If the app dies mid-run (a crash, a logout, a kill), the window is already saved in
`reports/.pending_cleanup.json`, and the next time that board is on the dock the
test take is removed. A new QC run on that board waits for that clean-up first.

Other deliberate choices:

- **QC never ends a take it didn't start without asking.** If a board is mid-take,
  Start QC asks first; that take is stopped and kept on the card.
- **Eye order comes from the recorder's eye-correction record, not image matching.**
  On raw fisheye frames the two lenses' optical-centre offset outweighs real disparity
  beyond about 1 m, so a feature match against a far wall reads "swapped" on a good
  camera.
- **Camera-to-IMU is not measured per unit** (the bench doesn't produce it; delivery
  takes the IMU as the left camera's frame). QC reports that as a fact, not a fault.
- **Reports stay on this laptop**: one JSON per run in `reports/` (gitignored), with
  every number each check judged. The QC history table at the bottom of the page reads
  them back, and a name given after a pass is written into that run's report.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_checks.py` covers the measurements and the calibration rules,
  including camera 167's real document, whose FOV must match the bench to 0.01°.
- `tests/test_engine.py` runs the real engine against a simulated board
  (`tests/fakeboard.py`): the happy path, every way a board fails, cancels, clock-step
  renames, a take that restarts itself, deletes that fail, a stale `.finalizing`
  marker, and the clean-up of an interrupted run next to newer footage.

## Files

| file | what |
|---|---|
| `webui.py` | the web server, dock scanner, naming gate, reports, pending clean-ups |
| `qc.py` | the steps, their order, `LIMITS`, and the test-take clean-up |
| `checks.py` | pure measurements: frame timing, IMU, image |
| `calibration.py` | the dashboard calibration lookup, field-ready rule and FOV math |
| `board.py` | how a board is reached (reuses opm-name, dock-check, camfw) |
| `delivery.py` | loads the delivery's strip decoder and runs it on a QC take |
| `web/index.html` | the page |
| `run.sh`, `install.sh`, `icon.svg`, `icon.png` | launcher and installer |
