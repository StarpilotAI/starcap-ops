"""
StarCap Tracker settings: where things live, and every threshold a view uses.

The OpenAI numbers come from "Robotics Data Requirements - Human Egocentric
Videos" (2026-07-24), section 2: clips of 30 s to 30 min, 60 fps preferred, and
"99th percentile frame-to-frame interval within ±10 ms from nominal FPS". The
dashboard stores that last rule per take as data.timing.score, the percentage of
intervals inside the band, so a take passes at score >= 99.

The 30-day recalibration window is the dashboard's own RECALIBRATION_DAYS
(camident-core.ts), so this tool and /cameras agree on what is overdue.

Every knob can be overridden with an environment variable of the same name
prefixed STARCAP_TRACKER_ (e.g. STARCAP_TRACKER_CAL_PER_DAY=12).
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _num(name, default):
    raw = os.environ.get("STARCAP_TRACKER_" + name)
    return type(default)(raw) if raw not in (None, "") else default


HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
DEMO_DIR = HERE / "tests" / "fixtures" / "api"   # served at /demo/ by --demo
DATA_DIR = Path(os.environ.get("STARCAP_TRACKER_DATA", HERE / "data"))
DB_PATH = DATA_DIR / "tracker.db"

HOST = "127.0.0.1"
PORT = _num("PORT", 8774)

# Credentials are read from the dashboard's env file, never copied anywhere.
# First file that exists wins.
ENV_FILES = [
    Path(p).expanduser() for p in (
        os.environ.get("STARCAP_TRACKER_ENV", ""),
        "~/starcap-dashboard/env.local",
        "~/starcap-dashboard/.env.local",
        "~/Desktop/env.local",
    ) if p
]

DASHBOARD_URL = "https://starcap.dev"
PROJECT = "egypt"                    # recordings / trips / starcaps project_id
TZ = ZoneInfo("Africa/Cairo")        # a "day" is a Cairo calendar day

# --- OpenAI rules ------------------------------------------------------------
TARGET_FPS = 60
TIMING_PASS_SCORE = _num("TIMING_PASS_SCORE", 99.0)   # <= 1 % of intervals outside ±10 ms
MIN_CLIP_S = _num("MIN_CLIP_S", 30.0)

# --- calibration ---------------------------------------------------------------
RECAL_DAYS = _num("RECAL_DAYS", 30)
EXPIRY_WARN_DAYS = _num("EXPIRY_WARN_DAYS", 7)
CAL_PER_DAY = _num("CAL_PER_DAY", 10)        # what one person gets through on the bench
PULL_FORWARD_DAYS = _num("PULL_FORWARD_DAYS", 7)   # the plan may calibrate this early
PLAN_DAYS = _num("PLAN_DAYS", 30)
SPIKE_DAYS = _num("SPIKE_DAYS", 2)           # a spike: more due on one day than this many days of bench work

# --- day view ------------------------------------------------------------------
COPY_GRACE_H = _num("COPY_GRACE_H", 12.0)    # returned this recently with nothing in = still waiting
FLAG_MIN_H = _num("FLAG_MIN_H", 0.25)        # an hours-based flag needs at least this much
SHORT_TAKES_MIN = _num("SHORT_TAKES_MIN", 10)   # ...and a short-take flag at least this many
# Thresholds set from 548 real cap-days (2026-10-05..11): a typical cap logs 0-2 unclean
# boots a day (operators cut the power), p95 is 3; camera drops have p90 3 but real faults
# run 10-179; a reboot loop shows as power_on counts of 20 to 1046.
REBOOTS_MIN = _num("REBOOTS_MIN", 5)                 # unclean boots in a day
POWER_ON_LOOP_MIN = _num("POWER_ON_LOOP_MIN", 20)    # power-ons in a day = reboot loop
CAMERA_DROPS_MIN = _num("CAMERA_DROPS_MIN", 10)
EXPIRED_CAL_MIN_H = _num("EXPIRED_CAL_MIN_H", 0.1)   # under 6 min on an expired lens isn't worth a flag
NEEDS_ATTENTION_DAYS = _num("NEEDS_ATTENTION_DAYS", 3)
ATTENTION_REPEAT_DAYS = _num("ATTENTION_REPEAT_DAYS", 2)   # warn-only caps must repeat; one bad day is enough
LATEST_VERSION_MIN_CAPS = 5                  # a version counts as "latest" once this many caps run it

# --- field watch ---------------------------------------------------------------
LONG_TRIP_DAYS = _num("LONG_TRIP_DAYS", 3.0)
SILENT_H = _num("SILENT_H", 24.0)
ONLINE_S = 120                                # heartbeat newer than this = online now

# --- pipeline ------------------------------------------------------------------
STATION_STALE_S = 5 * 60
STATION_DOWN_S = 60 * 60
UPLOAD_STUCK_MIN = _num("UPLOAD_STUCK_MIN", 15.0)
AWAIT_COPY_AFTER_S = 3600     # a cap back this long with footage but nothing copied is waiting on the station

# --- sync ----------------------------------------------------------------------
SYNC_EVERY_S = _num("SYNC_EVERY_S", 600)
HISTORY_DAYS = _num("HISTORY_DAYS", 45)      # takes, trips and events kept locally
TREND_DAYS = 30
EVENT_BACKFILL_DAYS = _num("EVENT_BACKFILL_DAYS", 8)   # device logs pulled the first time (one-off ~50 MB)
EVENT_DEVICE_DAYS = 14                        # only devices heard from this recently

# --- StarCap QC ----------------------------------------------------------------------
# QC (~/starcap-qc) keeps one JSON report per run on the laptop it ran on and sends
# nothing to the dashboard, so this is the only place its verdicts can be read.
QC_REPORTS_DIR = Path(os.environ.get("STARCAP_TRACKER_QC_DIR", "~/starcap-qc/reports")).expanduser()

# --- morning summary -------------------------------------------------------------
SUMMARY_AT = os.environ.get("STARCAP_TRACKER_SUMMARY_AT", "09:00")
SLACK_FILE = Path(os.environ.get("STARCAP_TRACKER_SLACK_FILE",
                                  "~/Desktop/slack messages.md")).expanduser()
