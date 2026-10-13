# StarCap Tracker: build spec

The contract between the four parts of this tool. Each part is built against this
file, so if code and spec disagree, fix one of them; don't let them drift.

```
sources.py  ── read-only Postgres + Upstash clients
sync.py     ── pulls from sources every 10 min, normalises, writes store
store.py    ── local SQLite cache (data/tracker.db) + my notes + summaries
rules.py / calib.py / maint.py / fleet.py / pipeline.py / summary.py
            ── PURE functions: lists of row dicts in, view dicts out (no I/O)
api.py      ── loads rows from store, calls the pure functions, returns JSON-able dicts
tracker.py  ── HTTP server, background sync + morning-summary loops, CLI
web/        ── index.html + app.css + app.js + charts.js (vanilla, no CDN, no build)
```

Shared helpers already exist: `config.py` (every threshold and path) and `clock.py`
(Cairo days: `cairo_date`, `day_bounds`, `shift_day`, `days_between`, `iso`,
`parse_ts`, `today`, `now`). Use them; don't re-derive time zones or thresholds.

Python 3.12, stdlib only, plus `psycopg2` (apt `python3-psycopg2`), which only
`sources.py` imports. Match the style of `~/starcap-qc` (flat modules, module
docstrings that explain *why*, small functions, `unittest` tests).

## 0. Ground rules

- **Read-only toward production, always.** Postgres runs every query inside
  `BEGIN READ ONLY` (psycopg2 `set_session(readonly=True)`, never commit), and a
  guard refuses any statement that doesn't start with `select`/`with`. Upstash only
  sends commands from a read allowlist. No HTTP calls to starcap.dev. The only things
  this tool writes are its own SQLite file and the Slack draft file.
- **Never log or return secrets** (DATABASE_URL, tokens, operator phone numbers).
- A **day** is a Cairo calendar day, `"YYYY-MM-DD"`. Stored times are float epoch
  seconds UTC; API times are Cairo ISO strings via `clock.iso()`. Hours in the API
  are floats rounded to 2 decimals.
- Dragon units (`starcaps.data.kind = 'dragonCamera'`, `copied_via = 'dragonCamera'`,
  camera ids `dragon:*`) and the `vin` project are out of scope: drop them in sync.

## 1. Where the data comes from (verified live on 2026-10-12)

| Need | Source |
|---|---|
| takes | Postgres `recordings` (project_id='egypt'), one row per take |
| check-outs | `trip_items` (one per cap per check-out; `starcap_id` = **cpu_id**, `created_at` = check-out time, `returned_at`) joined to `trips` (`created_at` identical, `data.task`, `destination`) |
| names | `starcaps` (cpu_id, name, camera_id, data.kind, data.board) |
| cameras | `cameras` (id, status, data.retired, created_at; project 'egypt') |
| calibration history | `calibration_versions` (camera_id = base "88", camera_key = "88" or "88.1", rev, method, calibrated_at, uploaded_at, superseded_at, verdict PASS/NULL, evidence_status, doc.rmse{mono0,mono1,stereo}, doc.baseline_mm) |
| current calibration incl. camera-IMU | `camera_calibrations` (camera_id base, kind stereo / imu / wb / imu_noise, calibrated_at, data.rev, data.method, data.evidence.status, data.fit_digest) |
| camera swaps | `events` where action in ('camera.attached','camera.detached'), data {camera, headset, replaced, source} |
| tickets | Redis `starcap:maint:index` (zset, score = created ms) + `starcap:maint:ticket:<id>` (JSON string) |
| blocked caps / broken cameras | Redis sets `starcap:maint:blocked` (cap names), `starcap:maint:broken_cam` (camera ids) |
| retired cameras / lensed ids | Redis hash `starcap:cameras:retired` (keys = camera ids), set `starcap:cameras:revs` (members like `cam-61.1`) |
| who has a cap now | Redis hash `starcap:kamerat:out` (field = cap name, value JSON {operator_name, operator_phone, checked_out_at ms, camera, destination}) |
| heartbeats | Redis hash `starcap:fleet` (field `<device_id>\|self:<device_id>`, value JSON {device_id, cpu_id, name, last_seen ms, software_version, board, status, recording, disk_free, disk_total, connection_type, camera, clock_synced}) |
| device event logs | Redis zset `starcap:logs:<device_id>`, score = seq, member JSON {seq, ts ms, utc, type, msg, data} |
| copy station | Redis hash `starcap:opm` (field = station_id, value JSON {station_name, last_seen ms, activity{state,message}, drives[{role,bytes_free,bytes_total}], pending_review, error}) |
| uploaders | Redis hash `starcap:ssd` (field = uploader_id, value JSON {name, last_seen ms, detection{state,level,message}, runs[{run,status,bytes_total,bytes_done}], current{run,sent,total,rate_bps}, error, logs[]}) |
| label pipeline | Redis string `starcap:ship:label:tick` (JSON {at ms, ok, last_ok_at ms, label{waiting}}) |
| StarCap QC runs | local files `~/starcap-qc/reports/*.json` (config.QC_REPORTS_DIR): {report_id, verdict pass/fail/cancelled, started, finished, headline, station, board{cpu_id, name, model, camera_id, version}, steps[{key, title, state}]}. QC publishes nothing to the dashboard, so these files are the only record |

`recordings` field paths (all inside `data`):

| column | path |
|---|---|
| started_at | `analysis.device_info.started_at` ("2026-10-11T10:22:04+0300", recorder clock) |
| clock_fixed | `analysis.device_info ? 'renamed_from'` (NTP corrected the start) |
| boot_id, software_version, board | `analysis.device_info.{boot_id, software_version, board}` |
| camera_unit | `analysis.device_info.camera.unit` ("43.1"), fallback trip_items.camera_id |
| usb_path | `analysis.device_info.camera.usb_path` |
| fault_kind | `analysis.fault.kind` ('unrecoverable' or null) |
| codes | `readiness_summary.codes` (array; 'no_chunks' = no video) |
| fps | `readiness.streams.left.fps`, fallback `readiness.streams.stereo.fps` |
| nominal_fps, score, p99_dev_ms, out_of_band_pct, drop_pct | `timing.*` (absent until the readiness worker has run) |
| uploaded_at | `uploaded.uploaded_at` (ms) |
| first_copied_at | `analysis.first_copied_at` |
| copied_via | `analysis.copied_via` |

Plain columns: id, cpu_id, trip_item_id, trip_id, created_at (first copy, server clock),
updated_at, recorded_s, kept_s, settled, readiness_status (ready / warn / blocked / null).

Note `recordings` has no index on `updated_at`/`created_at` alone; `(project_id, ingested_at)`
is indexed. Never select the whole `data` column (one scan took 22 s).

## 2. Store (`store.py`)

`Store(path)` opens/creates SQLite (WAL, `check_same_thread=False`, one `threading.RLock`
around every call). Schema is created on open and versioned with `PRAGMA user_version`.
All loaders return `list[dict]` whose keys are exactly the columns below. JSON-ish
columns (`codes`, `photos`) come back already decoded. Booleans come back as `bool`.

### Cache tables (sync replaces/upserts; nobody else writes them)

| table | columns |
|---|---|
| `takes` | id PK, cpu_id, trip_item_id, trip_id, created_at, updated_at, first_copied_at, uploaded_at, started_at, clock_fixed, boot_id, software_version, board, camera_unit, usb_path, recorded_s, kept_s, settled, readiness_status, codes (list[str]), fault_kind, nominal_fps (int\|None), fps, p99_dev_ms, out_of_band_pct, score, drop_pct |
| `items` | id PK, trip_id, cpu_id, camera_id, checkout_at, returned_at, missing, task, destination |
| `starcaps` | cpu_id PK, name, camera_id, board, created_at |
| `cameras` | id PK, status, retired, created_at |
| `cal_versions` | id PK, camera_id, camera_key, rev, method, calibrated_at, uploaded_at, superseded_at, verdict, evidence_status, rmse_left, rmse_right, rmse_stereo, baseline_mm |
| `cal_current` | id PK, camera_id, kind, rev, calibrated_at, method, evidence_status, verdict, fit_digest |
| `camera_events` | id PK, at, action, camera, headset, replaced, source |
| `tickets` | id PK, subject, headset, camera, category, critical, description, reporter_name, status, created_at, resolved_at, resolution_note, replacement_camera, photos (int count) |
| `devices` | device_id PK, cpu_id, name, last_seen, software_version, board, status, recording, disk_free, disk_total, connection_type, camera, clock_synced |
| `device_events` | (device_id, seq) PK, ts, type, msg |
| `kv` | key PK, value (JSON), updated_at |
| `qc_runs` (schema v2) | id PK (report_id), cpu_id, name, model, camera_id, version, verdict, started_at, finished_at, headline, failed_steps (list of step titles that failed), station |

`kv` keys written by sync: `maint_blocked` (list[str]), `maint_broken_cam` (list[str]),
`cameras_retired` (list[str]), `cameras_revs` (list[str], e.g. "61.1"),
`kamerat_out` (dict cap → {operator_name, checked_out_at (s), camera, destination}; **no phone**),
`stations` (list of station dicts, times in s), `uploaders` (list of uploader dicts:
keep name, last_seen, detection, error, current, runs summarised to counts by status,
last 20 log lines), `uploader_progress` (dict uploader_id → {run, sent, changed_at}),
`label_tick` (dict, times in s), `heartbeats_at` (epoch s: when sync read the heartbeats;
api ages every heartbeat from here, so a healthy station doesn't look silent just because
the next sync is minutes away), `sync` (sync status, see §4), plus the sync's own cursors
`takes_cursor` and `device_event_cursor`.

Sync drops the partner uploader (id `partner-*`, or runs naming DRAGON drives): it ships
Dragon cameras.

### Loaders (what api.py calls)

```python
s.starcaps(); s.cameras(); s.cal_versions(); s.cal_current(); s.tickets(); s.devices()
s.items(t0, t1)                 # checkout_at in [t0, t1)
s.open_items()                  # returned_at is None
s.field_takes(t0, t1)           # takes whose ITEM checkout_at is in [t0, t1); each row also has
                                #   checkout_at, returned_at, item_camera_id
s.takes_created(t0, t1)         # created_at in [t0, t1), trip or not
s.takes_off_trip(t0, t1)        # trip_item_id is None and created_at in [t0, t1)
s.takes_for_camera(keys, t0, t1)   # camera_unit in keys, created_at in [t0, t1)
s.camera_events(t0, t1)
s.device_events(t0, t1, device_ids=None, types=None)
s.kv_get(key, default=None); s.kv_set(key, value)
```

### My notes (owned by this tool, never synced anywhere)

`notes`: id INTEGER PK, subject_type ('cap'|'camera'), subject_id (e.g. "starcap12", "43.1"),
created_at, text, status (None | 'ok' | 'watch' | 'repair' | 'dead' | 'lost'), deleted_at.

```python
s.notes(subject_type=None, subject_id=None, limit=200)   # newest first, deleted hidden
s.add_note(subject_type, subject_id, text, status=None) -> dict
s.delete_note(note_id) -> bool                             # soft delete
```

A unit's **current status** = the newest non-deleted note that has a status; 'ok' clears it.

### Summaries

`summaries`: date PK, text, written_at. `s.summary_get(date)`, `s.summary_put(date, text)`.

## 3. Rules (pure)

### Take bucket (`rules.bucket(take)`)

```
recorded_s < MIN_CLIP_S                          -> "short"
fault_kind == "unrecoverable" or "no_chunks" in codes -> "broken"
score is None and readiness_status is None       -> "pending"   (not analysed yet)
score is None                                    -> "untimed"   (analysed, no frame timing)
nominal_fps == 30                                -> "fps30"     (camera fell back to USB 2)
score < TIMING_PASS_SCORE                        -> "timing_fail"
otherwise                                        -> "good"
```
Display order: good, fps30, timing_fail, untimed, broken, short, pending.

### Field day

A take counts on the Cairo day its **trip item was checked out** (`checkout_at`), never
on the recorder clock (stale until NTP; one boot was 14 h off). Takes with no trip item
are "off-trip" (bench, QC, office) and are reported separately by `created_at` day.

### Cap-day row (one per cap checked out that day; merge several items of one cap)

- `status`: `out` if any of the cap's items for the day is not returned; else `ok` if it
  has takes; else `waiting` if returned < COPY_GRACE_H ago; else `footage_missing` if a
  `rec_start` event was seen in [first checkout, last return]; else `no_footage`.
- `hours`: total + per bucket. `good_pct` = good / total. `timing_pass_pct` =
  good / (good + timing_fail), None when 0.
- `fps`: recorded_s-weighted mean of take fps; `nominal_fps`: the most common nominal.
- `expired_cal_hours`: hours whose camera calibration was expired **at take start**: the
  camera key's latest PASS-or-NULL-verdict fit with calibrated_at ≤ started_at (fall back to
  checkout_at when started_at is missing or outside the trip window) is missing or ≥
  RECAL_DAYS old. Key = take.camera_unit, else item camera with its highest rev from
  `cameras_revs`.
- `events`: counts of power_on, unclean_boot, camera_lost, rec_dead, rec_corrupt,
  camera_usb2, storage (disk_full + rec_refused + storage_stalled + storage_lost) for the
  cap's devices (devices.cpu_id) in its window.
- `flags`: list of `{code, severity: bad|warn|info, label, detail}`:

| code | when | severity | label |
|---|---|---|---|
| no_footage | status no_footage | bad | No footage |
| footage_missing | status footage_missing | bad | Footage missing |
| no_clips | footage arrived but every take is under MIN_CLIP_S | bad | No usable takes |
| fps30 | hours.fps30 ≥ FLAG_MIN_H | bad | 30 fps |
| storage | events.storage > 0 | bad | Storage |
| timing | hours.timing_fail ≥ FLAG_MIN_H | warn | Timing |
| broken | hours.broken + hours.untimed ≥ FLAG_MIN_H | warn | Broken takes |
| short | short takes ≥ SHORT_TAKES_MIN and ≥ half of takes | warn | Short takes |
| reboots | events.power_on ≥ POWER_ON_LOOP_MIN (a loop), or events.unclean_boot ≥ REBOOTS_MIN | warn | Reboots |
| camera_drops | events.camera_lost ≥ CAMERA_DROPS_MIN | warn | Camera drops |
| expired_cal | expired_cal_hours ≥ EXPIRED_CAL_MIN_H | warn | Cal expired |
| old_firmware | version older than latest (a newer canary is not "old") | info | Old firmware |

The event thresholds were set from 548 real cap-days (2026-10-05..11): unclean boots are
0-2 a day on a healthy cap (operators cut the power), camera drops p90 is 3 while real
faults run 10-179, and a reboot loop logs 20 to 1046 power-ons a day.

`detail` is one plain sentence with the numbers, e.g. "2.1 h at 30 fps: camera on USB 2" (the USB
part only when a 30 fps take's usb_path is on bus 1- (NanoPi) or 7- (Orange Pi); old firmware
reports no path).

**Device logs.** `events_since` (kv, written by sync) is where the local device logs start.
A cap-day checked out before it has no event counts and no rec_start evidence: event flags
can't fire and an empty return reads no_footage "(no device log that far back)", never
footage_missing. The day view says `events_complete: false` so the page can say so.

Day KPIs: `caps_recorded` = caps with at least one take ≥ MIN_CLIP_S; `fps30_caps` = caps
with any 30 fps hours (the same caps `fps30_hours` sums), not just the flagged ones.
**latest** = highest version (compare as int tuples) run by ≥ LATEST_VERSION_MIN_CAPS distinct
caps in the last 7 days of takes.

### Calibration (`calib.py`)

- Active cameras: `cameras` rows, minus `dragon:*`, minus retired (cameras.retired or
  `cameras_retired`). Broken (`maint_broken_cam`) are listed separately, never planned.
- **Fitted lens**: highest rev among `cameras_revs` and `cal_current` rows for that base;
  key = base if rev 0 else "base.rev". **Last fit** = newest `cal_versions` row for that key
  with verdict PASS or NULL. `due_at` = last fit + RECAL_DAYS; `days_left` =
  ceil((due_at − now) / 1 day); state overdue (≤ 0), due_soon (≤ EXPIRY_WARN_DAYS), ok,
  never (no fit at all).
- **Usage**: `out_now` (camera on an open item or in kamerat_out), `on_cap`, `hours_7d`,
  `last_used` (from takes).
- **Plan** (capacity CAL_PER_DAY a day): for each day from today for PLAN_DAYS, capacity =
  CAL_PER_DAY (today: minus fits already done today); candidates = unplanned cameras with a
  fit and due_at < day end + PULL_FORWARD_DAYS, **except, today, cameras out in the field**
  (they can't be on the bench until their cap is back; later days assume they are). Order:
  cameras already past due first, those in rotation (out now or recorded this week) before
  idle ones, busiest first; then pulled-forward ones by due date. (Strict earliest-deadline-
  first put two idle cameras 31 days overdue with 0 h ahead of 11.1 with 27 h that week.)
  A camera planned after its due date is **late**. `never` cameras are not planned.
- **Catch when back**: overdue or due-soon cameras that are out now, most urgent first.
- **Backlog**: `clear_by` = first plan day that ends with nothing overdue (null if never
  within PLAN_DAYS); `behind_from` = first day after that with something overdue again
  (null while the plan keeps up). A backlog being worked down is not "falling behind".
- Today's fits: `cal_versions` with Cairo calibrated_at day = date. `first_ever` = no earlier
  version with the same camera_key. Bench name from method: basalt_kb4 → "Basalt",
  opencv_fisheye_kb4 → "Robot bench", NULL → "Charuco (old)". Camera-IMU fits = `cal_current`
  kind 'imu' calibrated that day.
- Coverage: valid = active cameras with state ok/due_soon; imu = cameras with a PASS-or-NULL
  `cal_current` imu row (camera 106's FAILED fit doesn't count); imu outdated when its
  calibrated_at < the stereo fit's.
- Spikes: forecast days with more than SPIKE_DAYS × CAL_PER_DAY cameras due (a day of 11-17
  due is absorbed by pulling forward; 51 on one day is not). The summary names the biggest.

### Maintenance (`maint.py`)

- Ticket unit = headset for subject 'starcap', camera for 'camera'. Age in days.
- Open list: critical first, then oldest first. Stats: open, critical, opened and resolved
  on the date, median days to resolve (tickets resolved in the last 30 days), oldest open.
- **In maintenance** (`in_shop`): one row per unit with an open ticket, plus blocked caps
  and broken cameras without one. Each carries its open tickets (oldest first), my status,
  its last field day before the oldest ticket (`last_trip`, from the day view), and a
  `verdict`, the first of: out in the field now (bad) · went out N× since the ticket (warn)
  · failed QC since (bad) · passed QC since (ok, "close the ticket?") · online now (info) ·
  powered on since (info; warn "Quiet for N d" if nothing for QUIET_DAYS = 7) · just
  reported (< 2 d, info) · untouched since the ticket (warn). A camera is "in the field
  now" (bad) when an open check-out carries it. Sorted worst verdict, critical, oldest.
- **Needs attention**: caps with a bad flag on any field day in
  [date − NEEDS_ATTENTION_DAYS + 1, date], or warn flags on ATTENTION_REPEAT_DAYS of them,
  with no open ticket on the cap or its camera, and my status not in (repair, dead, lost).
  `expired_cal` doesn't count (NOT_A_REPAIR: the calibration queue owns it). One entry per
  cap with merged flags and the days. (On 2026-10-12 the plain any-flag rule listed 55 caps,
  22 of them one-day warn blips; this rule lists 33, led by the 7 bad on all three days.)
- Repeat offenders: units with ≥ 2 tickets created in the last 30 days.
- Trend: open count at each day's end (created ≤ end and not resolved by end), opened,
  resolved.

### StarCap QC (`qc.py`)

- NanoPi caps only: a cap is NanoPi when its board says so or QC itself saw its cpu_id. An
  Orange Pi gets `qc: null` (the Orange Pi fleet predates QC), never "never".
- Status = the latest **finished** run (pass/fail); cancelled runs count for nothing. `never`
  = a NanoPi with no finished run. `passed_at` = its newest pass; `failed_steps` only when the
  latest run failed.
- Not a flag: "never QC'd" isn't a fault, so it stays out of needs-attention.

### Field watch (`fleet.py`)

- Out now = open items. `days_out` from checkout. `last_heard` = max devices.last_seen
  for the cap's cpu_id; `silent_h` = hours since max(last_heard, checkout_at).
- Long out: days_out > LONG_TRIP_DAYS. Silent: silent_h > SILENT_H.
- Operator name (from kamerat_out) is shown **only** here, to chase a missing cap.
- Fleet snapshot: total caps (named starcapN, so bench boards like WALEEDTEST1 are out), out, online (last_seen within ONLINE_S of
  `heartbeats_at`), blocked, in office.

### Pipeline (`pipeline.py`)

- Copy station: it only runs while caps come back (2026-10-04..12: 20:00-04:00, copying at
  check-in), so silence alone is normal. **Waiting** check-outs (`awaiting_copy`) = back
  between AWAIT_COPY_AFTER_S and a day ago, nothing copied, but a rec_start logged during
  the trip; a station is blamed only for those that came back after it went quiet, or
  within AWAIT_COPY_AFTER_S of its last heartbeat (`stranded_by`; a cap skipped while the
  station kept running is the cap's fault, flagged footage_missing on the Day page).
  Status: ok if seen < STATION_STALE_S ago (warn with an error); otherwise idle when
  nothing waits for it, warn while silent < STATION_DOWN_S, else bad. `waiting_caps` = how
  many it is blamed for.
- Uploader: ok if seen < STATION_STALE_S ago, warn if < STATION_DOWN_S, else bad; bad too
  when stuck (current.sent unchanged for ≥ UPLOAD_STUCK_MIN, from `uploader_progress`) or
  error set. Its detection `level` is shown, not graded ("no_data"/"error" is its idle state).
- Backlog: takes in the local history (HISTORY_DAYS) with no uploaded_at (count, hours, oldest
  age), and `over_24h` = those copied over a day ago (copy to cloud p90 is ~17 h, so these are stuck).
- Lag over takes uploaded in the last 7 days: check-out → cloud (uploaded_at − the item's
  checkout_at, why "yesterday" fills in during the day) and copy → cloud (uploaded_at −
  created_at), p50/p90 h. Not return → copy: docking records the return, so the copy often
  comes first and that lag reads 0 h.
- Label tick: age in minutes, ok, waiting.

### Summary (`summary.py`)

`summary.text(views)` → Markdown for the Slack drafts file, short and in Waleed's casual
voice: lower-case bullets, numbers first, no fluff. Covers yesterday (with how much has
arrived), the day before (complete), calibration (done vs target, overdue, next spike),
maintenance (opened / closed / open critical), field watch, pipeline problems. Example:

```
**starcap daily, sun 11 oct** _(82% of footage in so far)_
- 91 caps out, 84 recorded, 2 got nothing (starcap12, starcap40)
- 587 h recorded, 512 h good (87%), timing pass 99.1%
- 30 fps on 12 caps (46 h): starcap5, starcap9, starcap31 +9
- 9 h shot on cameras past calibration (48.1, 164)
- sat 10 oct final: 82 caps, 506 h, 471 h good
- calib: 10/10 yesterday, 24 overdue, heads up 51 come due thu 7 nov
- tickets: 4 new, 2 closed, 36 open (31 critical)
- field: out 3d+: starcap19 (4d) · silent 24h+: starcap40, starcap77, starcap90 +7
- pipeline: all good
```

`summary.write(store, date, text, force=False)` appends `\n\n---\n\n` + text to SLACK_FILE
(create if missing, never rewrite existing content), records it in `summaries`, and refuses a
second write for the same date unless force.

## 4. HTTP API (`api.py` builds these; `tracker.py` routes them)

Errors: `{"error": "message"}` with status 400/404/500. All GETs are cheap reads of the
local cache; only `/api/sync` touches production (read-only).

**`GET /api/ping`** → `{"app": "starcap-tracker"}`

**`GET /api/status`** →
```json
{"now": "2026-10-12T15:40:00+03:00", "today": "2026-10-12", "yesterday": "2026-10-11",
 "sync": {"running": false, "last_ok_at": "...", "last_error": null, "next_at": "...",
          "sources": {"takes": {"ok": true, "at": "...", "rows": 3486, "took_s": 6.1, "error": null}}},
 "config": {"cal_per_day": 10, "recal_days": 30, "min_clip_s": 30, "timing_pass_score": 99, "target_fps": 60},
 "dashboard_url": "https://starcap.dev", "has_data": true}
```

**`POST /api/sync`** → `{"started": true}` (or `{"started": false, "running": true}`)

**`GET /api/day?date=YYYY-MM-DD`** (default yesterday) →
```json
{"date": "2026-10-11", "weekday": "Sun", "is_today": false, "latest_version": "1.67.0",
 "events_from": "2026-10-04T17:00:00+03:00", "events_complete": true,
 "arrival": {"caps_out": 91, "arrived": 84, "still_out": 3, "waiting": 2, "no_footage": 1, "footage_missing": 1, "pct": 92},
 "kpis": {"caps_out": {"value": 91, "prev": 82}, "caps_recorded": {...}, "caps_no_footage": {...},
          "hours_total": {...}, "hours_good": {...}, "good_pct": {...}, "timing_pass_pct": {...},
          "fps30_hours": {...}, "fps30_caps": {...}, "expired_cal_hours": {...}},
 "hours": {"total": 587.0, "good": 512.3, "fps30": 46.0, "timing_fail": 5.1, "untimed": 2.0, "broken": 13.5, "short": 3.1, "pending": 5.0},
 "caps": [{"cap": "starcap34", "cpu_id": "2cfc...", "board": "Orange Pi 5", "camera": "43.1", "cameras": ["43.1"],
           "checkout_at": "...", "returned_at": "...", "task": "Repair Services", "status": "ok",
           "takes": 12, "clips": 9, "short_takes": 3,
           "hours": {"total": 6.2, "good": 5.9, "fps30": 0, "timing_fail": 0.1, "untimed": 0, "broken": 0, "short": 0.05, "pending": 0},
           "good_pct": 95.2, "timing_pass_pct": 99.8, "fps": 60.09, "nominal_fps": 60, "p99_dev_ms_max": 3.3,
           "boots": 2, "version": "1.67.0", "expired_cal_hours": 0, "cal_days_left": 12,
           "events": {"power_on": 2, "unclean_boot": 0, "camera_lost": 0, "rec_dead": 0, "rec_corrupt": 0, "camera_usb2": 0, "storage": 0},
           "flags": [{"code": "fps30", "severity": "bad", "label": "30 fps", "detail": "2.1 h at 30 fps: camera on USB 2"}],
           "severity": "bad", "open_tickets": 1, "my_status": null,
           "qc": {"status": "pass|fail|never", "at", "passed_at", "failed_steps": [], "runs": 2} | null}],
 "off_trip": {"takes": 37, "hours": 4.1, "caps": 9}}
```
`kpis.*.prev` is the same number for the day before (null when unknown). `caps` sorted by
severity (bad, warn, info, none), then hours desc.

**`GET /api/day/cap?date=&cap=`** → `{"date", "cap", "row": <cap-day row>, "takes": [{"id", "started_at",
"clock_fixed", "recorded_s", "bucket", "nominal_fps", "fps", "score", "p99_dev_ms", "out_of_band_pct",
"drop_pct", "camera", "boot": "f361", "version", "codes", "fault", "uploaded_at"}], "events": [{"at", "type", "msg"}]}`
(events: only the types counted in `events`, plus rec_start/rec_stop, newest last.)

**`GET /api/calibration?date=`** (default today) →
```json
{"date": "2026-10-12", "target": 10,
 "today": {"done": 9, "recal": 9, "new": 0, "imu": 0,
           "fits": [{"camera": "39.1", "base": "39", "rev": 1, "method": "basalt_kb4", "bench": "Basalt",
                     "calibrated_at": "...", "first_ever": false, "previous_at": "...",
                     "rmse_left": 0.26, "rmse_right": 0.23, "rmse_stereo": 0.31, "baseline_mm": 60.07,
                     "verdict": "PASS", "evidence": "complete"}],
           "imu_fits": [{"camera": "106", "calibrated_at": "..."}]},
 "counts": {"active": 248, "ok": 176, "due_soon": 24, "overdue": 24, "never": 9, "late_in_plan": 0, "retired": 2, "broken": 4},
 "plan_today": [<queue entry>],
 "catch_when_back": [<queue entry>],
 "queue": [{"camera": "48.1", "base": "48", "state": "overdue", "days_left": -12, "due_on": "2026-09-30",
            "last_cal_at": "...", "bench": "Robot bench", "out_now": true, "on_cap": "starcap77",
            "hours_7d": 31.2, "last_used": "2026-10-11", "plan_day": "2026-10-12", "late": true}],
 "never": [{"camera": "203", "created_at": "...", "hours_7d": 0, "last_used": null, "out_now": false}],
 "broken": ["100", "133", "199", "9"],
 "forecast": [{"date": "2026-10-12", "due": 3, "planned": 10, "overdue_end": 14}],
 "spikes": [{"date": "2026-11-07", "due": 51}],
 "clear_by": "2026-10-17", "behind_from": null,
 "history": [{"date": "2026-09-13", "recal": 10, "new": 0, "imu": 0}],
 "coverage": {"valid": 200, "active": 248, "valid_pct": 80.6, "imu": 1, "imu_pct": 0.4, "imu_outdated": 0}}
```
`queue` = overdue + due_soon, in plan order. `history` = last TREND_DAYS days.

**`GET /api/maintenance?date=`** (default yesterday) →
```json
{"date": "...",
 "stats": {"open": 36, "critical": 31, "opened": 4, "resolved": 2, "median_resolve_days": 3.5,
           "oldest_open_days": 40, "blocked_caps": 29, "broken_cams": 4},
 "by_category": [{"category": "hardware", "open": 20, "last_30d": 50}],
 "open": [{"id", "subject", "unit", "headset", "camera", "category", "critical", "description",
           "reporter", "created_at", "age_days", "photos"}],
 "in_shop": [{"subject": "cap"|"camera", "unit", "critical", "blocked", "broken", "since_at", "days",
              "tickets": [{"category", "description", "reporter", "critical", "photos", "created_at"}],
              "verdict": {"severity", "text", "detail"}, "my_status": {"status", "text", "since"} | null,
              # caps: "out_now", "outs_since", "last_heard", "online", "camera",
              #       "qc_since": {"verdict", "at", "failed_steps"} | null,
              #       "last_trip": {"day", "checkout_at", "hours", "good_pct", "flags"} | null
              # cameras: "on_cap", "out_now"
             }],
 "recent_resolved": [<ticket + resolved_at, resolution_note>],
 "needs_attention": [{"cap": "starcap5", "camera": "16", "severity": "bad", "flags": [<flag>], "days": ["2026-10-11"]}],
 "repeat": [{"unit": "starcap91", "subject": "starcap", "tickets_30d": 4, "open": 1, "last_at": "..."}],
 "blocked": ["starcap144"], "broken_cams": ["100"],
 "my_status": [{"subject_type": "cap", "subject_id": "starcap12", "status": "repair", "since": "...", "text": "..."}],
 "notes_recent": [<note>],
 "links": {"tickets": "https://starcap.dev/tickets", "file": "https://starcap.dev/maintenance"},
 "trend": [{"date": "...", "open": 36, "opened": 4, "resolved": 2}]}
```

**`GET /api/field`** →
```json
{"fleet": {"total": 172, "out": 104, "office": 68, "online": 27, "blocked": 29},
 "qc": {"available": true, "nanopi": 83, "passed": 63, "failed": 1, "never": 19,
        "not_passed": [{"cap", "status": "fail|never", "at", "failed_steps", "out_now", "runs"}]},
 "long_out": [{"cap", "camera", "checkout_at", "days_out", "last_heard", "silent_h", "operator", "task"}],
 "silent": [<same shape>],
 "out": [<same shape, every cap out now, longest first>]}
```

**`GET /api/pipeline`** →
```json
{"stations": [{"id", "name", "last_seen", "age_s", "state", "message", "staging_free_gb", "pending_review", "error",
               "waiting_caps", "status": "ok|idle|warn|bad"}],
 "uploaders": [{"id", "name", "last_seen", "age_s", "state", "level", "message",
                "runs": {"pending": 0, "uploading": 1, "done": 70, "error": 0},
                "current": {"run", "sent", "total", "pct", "rate_mbps"}, "error", "stuck": false, "stuck_min": 0, "status"}],
 "backlog": {"takes": 55, "hours": 32.2, "oldest_h": 693.3, "over_24h": {"takes": 54, "hours": 32.2}},
 "lag": {"checkout_to_cloud_h": {"p50": 18.4, "p90": 31.0, "n": 6768}, "copy_to_cloud_h": {"p50": 7.7, "p90": 10.2, "n": 6900}},
 "label": {"at": "...", "age_min": 14, "ok": true, "waiting": 44}}
```

**`GET /api/trends?days=30`** →
`{"days": [{"date", "caps_out", "caps_recorded", "hours_total", "hours_good", "timing_pass_pct",
"fps30_hours", "expired_cal_hours", "cal_fits", "open_tickets", "overdue_cameras", "partial": false}]}`
(oldest first, ending today; today has `partial: true`.)

**`GET /api/cap/<name>`** →
`{"cap", "cpu_id", "board", "version", "online", "last_heard", "out_now": {"checkout_at", "days_out", "camera"} | null,
"camera_now", "my_status", "qc": <as on day rows> | null,
"qc_runs": [{"at", "verdict", "headline", "failed_steps", "version", "camera"}, newest first],
"totals_30d": {"days_out", "hours", "good", "no_footage_days"},
"days": [{"date", "status", "camera", "hours": {...}, "takes", "flags"}], "tickets": [...],
"camera_history": [{"at", "action", "camera", "replaced"}], "notes": [...]}` (days newest first)

**`GET /api/camera/<id>`** (id may be "43" or "43.1") →
`{"camera", "base", "rev", "state", "days_left", "due_on", "last_cal_at", "retired", "broken",
"fits": [<fit, newest first>], "imu": {"calibrated_at", "outdated"} | null,
"usage": [{"date", "hours", "caps": ["starcap34"]}], "cap_history": [...], "tickets": [...], "notes": [...]}`

**Notes**: `GET /api/notes?subject_type=&subject_id=` → `{"notes": [...]}`;
`POST /api/notes` body `{"subject_type", "subject_id", "text", "status"}` → the note;
`POST /api/notes/<id>/delete` → `{"ok": true}`.
A note: `{"id", "subject_type", "subject_id", "created_at", "text", "status"}`.

**Summary**: `GET /api/summary?date=` → `{"date", "text", "written_at"}`;
`POST /api/summary/write` body `{"date", "force"}` → `{"ok", "path", "written_at"}` or 409 if already written.

**Units list for pickers**: `GET /api/units` → `{"caps": ["starcap1", ...], "cameras": ["1", "43.1", ...], "boards": {"starcap1": "NanoPi" | "Orange Pi", ...}}` (`qc.board_kind`: a cap QC has seen is a NanoPi; caps with no known board are left out). The web app loads it before the first page and tags every cap name with its board.

## 5. Web (`web/`)

Single page, hash routes: `#/day/2026-10-11`, `#/calibration`, `#/maintenance`, `#/field`,
`#/pipeline`, `#/trends`, `#/cap/starcap34`, `#/camera/43.1`. Default `#/day` = yesterday.
`?demo` makes app.js read `/demo/<name>.json` instead of `/api/...` (fixtures live in
`tests/fixtures/api/`, served by `tracker.py --demo`). Fixture names: `status`, `day`,
`day_cap`, `calibration`, `maintenance`, `field`, `pipeline`, `trends`, `cap`, `camera`,
`notes`, `summary`, `units`.

Look and feel: same family as StarCap QC (`~/starcap-qc/web/index.html`): dark, its color
tokens, system-ui text, tabular-nums for numbers. Calm by default; color only means status.
No external fonts, scripts or CDNs.
