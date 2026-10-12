# StarCap Tracker

How the OpenAI StarCap fleet did yesterday, on one page: who went out, who recorded,
how many hours meet OpenAI's rules, which cameras need calibrating today, what is
broken, what is lost, and whether the upload pipeline is moving.

```bash
./install.sh                     # deps, the `starcap-tracker` command, desktop icon, user service
starcap-tracker                  # or double-click "StarCap Tracker"  ->  http://127.0.0.1:8774
```

It runs as an always-on user service (`starcap-tracker.service`): it syncs every 10
minutes and, at 09:00, adds yesterday's summary to `~/Desktop/slack messages.md` for
you to edit and post. (Installed after 09:00, it writes that day's on its first pass.)

The first sync takes about a minute and pulls ~50 MB of device logs once; after that a
pass takes ~15 s and a few KB of logs. It needs `python3-psycopg2` (the installer asks
for it; or `sudo apt install python3-psycopg2`).

## What's on it

| Page | Answers |
|---|---|
| **Day** | Yesterday, the day before, or any date: caps out, caps that recorded, caps that got nothing, hours and **good hours**, timing pass rate, hours at 30 fps, hours on cameras past calibration. One row per cap with its flags; click it for its takes and events. |
| **Calibration** | Today's fits against the 10/day target (intrinsics + stereo per fit, camera-IMU separately); what to put on the bench today (cameras in the office, overdue ones in use first) and which to **catch when back** from the field; when the backlog clears; a 30-day forecast with the spikes; coverage. |
| **Maintenance** | The dashboard's tickets (open, new, closed, time to fix, repeat offenders), caps the data says are broken that have **no ticket yet**, and your own notes and statuses (repair / dead / lost / watch). |
| **Field** | Caps out more than 3 days, and caps out and silent for 24 h or more, with who has them; and **NanoPi QC**: which NanoPis never passed StarCap QC, the ones out in the field first. |
| **Pipeline** | Takes copied over a day ago that never reached the cloud, check-out → cloud time, the SSD uploader, and the copy station, which is only flagged when it is quiet while returned caps' footage waits (it runs 20:00–04:00). |
| **Trends** | 30 days of all of the above. |

### The rules it grades by

- **Good hours** = takes of at least 30 s, recorded at 60 fps, that pass OpenAI's timing
  rule: "99th percentile frame-to-frame interval within ±10 ms from nominal FPS". The
  dashboard measures that per take from `frames.csv` (`data.timing.score`, the share
  of intervals inside the band), so a take passes at score ≥ 99.
- **30 fps** takes are the camera having fallen back to USB 2. They clear OpenAI's 30 fps
  minimum but miss the 60 fps target, so they get their own bucket and a flag.
- **Calibration** lasts 30 days (the dashboard's `RECALIBRATION_DAYS`). A take recorded
  on a camera whose last fit was older than that counts as "expired-cal hours".
- **A take belongs to the day its cap was checked out**, not the day on the recorder's
  clock, which can be hours off until NTP lands. Takes with no check-out (bench, QC)
  are shown separately as off-trip.
- **Yesterday fills in during the day.** Footage reaches the cloud about 20 h after
  check-out (30 h at p90), so the Day page shows how many caps' footage has arrived.
- **Device logs** (reboots, camera drops, storage, "footage missing") only exist locally
  from the first sync minus 8 days; older days say so instead of looking clean.
- **Needs attention** lists caps with a bad flag, or warnings on 2 of the last 3 days,
  that have no open ticket and no repair/dead/lost status from you. Expired calibration
  is left to the Calibration page.

Every threshold is in `config.py` and can be overridden with an environment variable
(`STARCAP_TRACKER_CAL_PER_DAY=12`, ...).

QC status (✓ / ✗ / "no QC" on the Day page, a QC history on each cap page) is read from
StarCap QC's own reports on this laptop (`~/starcap-qc/reports`), and only shown for NanoPi
caps. QC sends nothing to the dashboard, so a QC run on another laptop doesn't show here.

## Where the numbers come from

The dashboard's own stores, **read-only**: Postgres (`recordings`, `trip_items`/`trips`,
`starcaps`, `cameras`, `calibration_versions`, `camera_calibrations`, `events`) and
Upstash Redis (maintenance tickets, check-outs, heartbeats, device event logs, copy
station and uploader status). Credentials are read from the dashboard's env file
(`~/starcap-dashboard/env.local`, then `~/Desktop/env.local`) and never copied.

Read-only is enforced, not just intended: every SQL statement runs inside a read-only
transaction behind a select/with-only guard, and Redis commands outside a read
allowlist are refused before they are sent. The only things this tool writes are its
cache in `data/` and the Slack drafts file.

## Your notes

Notes and statuses live only on this laptop, in `data/tracker.db`, next to the cache.
`starcap-tracker --rebuild` (or `python3 tracker.py --rebuild`) empties the cache and
pulls everything again **without** touching your notes. Don't delete `data/` to "reset"
it; copy `data/tracker.db` somewhere if you want a backup.

## Commands

```bash
python3 tracker.py                    # serve (the service does this)
python3 tracker.py --sync-once        # pull once and print a table
python3 tracker.py --rebuild          # empty the cache, keep notes, pull everything
python3 tracker.py --summary          # print yesterday's summary
python3 tracker.py --summary 2026-10-11 --write   # append it to the Slack drafts file
python3 tracker.py --demo             # the page on test fixtures, no database
python3 -m unittest discover -s tests -v
journalctl --user -u starcap-tracker -f            # the service's log
```

## Files

See [`SPEC.md`](SPEC.md) for the full contract. In short: `sources.py` (read-only
clients) → `sync.py` → `store.py` (SQLite) → pure views in `rules.py`, `calib.py`,
`maint.py`, `fleet.py`, `pipeline.py`, `summary.py` → `api.py` → `tracker.py`
(server and loops) → `web/`.
