#!/usr/bin/env python3
"""
Pull production into the local cache (store.py), read-only, one pass at a time.

    python3 sync.py            one incremental pass, then a per-step table
    python3 sync.py --full     re-pull every take of the last HISTORY_DAYS too
    python3 sync.py --rebuild  empty the cache first (notes and summaries are kept)

tracker.py runs `Syncer.run_once()` every SYNC_EVERY_S on a background thread, and
POST /api/sync starts one by hand.

Each source is its own step, isolated: a failing step records its error in the kv
`sync` status and the others still run, so a Redis outage never blanks the takes and
a Postgres outage never blanks the tickets. Every query and command goes through
sources.py, which refuses anything that could write.

Why the takes step is careful. `recordings.data` is a TOASTed JSON document (~6 KB
compressed, up to 50 KB) and the table has no index on created_at or updated_at, so
cost is all detoasting. Measured 2026-10-12: a 45-day aggregate over a few JSON paths
took 15-26 s, and the same 4-day window took 7.7 s and 25.7 s on two runs with one
`data->...` per column but 6.2 s and 3.9 s reading the document once. So:

  - each query reads `data` ONCE (`data || '{}'` in an un-flattened subquery; every
    `data->...` in a plain select detoasts the whole document again) and returns
    only the extracted fields, never the document;
  - the first pass walks HISTORY_DAYS in 2-day windows (23 queries, 31.5k takes in
    29 s on 2026-10-12), and a window the database cancels at statement_timeout is
    split in half and retried;
  - later passes re-pull only the last 2 days plus anything whose updated_at moved
    since the previous pass (minus 10 minutes of slack for slow writers): ~2k
    takes in 3-5 s.

Device events come from Upstash, which bills per command and per byte, so they are
fetched incrementally by seq (each log is a zset scored by the recorder's own
monotonic seq), many devices per pipelined request, and only for devices whose
heartbeat moved since the previous pass.
"""
import argparse
import fcntl
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import clock
import config
import sources
from store import Store

DAY_S = 86400.0
TAKES_WINDOW_S = 2 * DAY_S           # backfill window; halved on a statement timeout
TAKES_MIN_WINDOW_S = 3 * 3600.0      # ...but never below this
TAKES_RECENT_DAYS = 2                # re-pulled whole on every pass (updated_at catches the rest)
TAKES_UPDATED_SLACK_S = 600.0        # updated_at > previous pass - this
FUTURE_S = DAY_S                     # created_at upper bound slack (server clock vs ours)
PRUNE_DAYS = config.HISTORY_DAYS + 5

EVENT_TYPES = (
    "power_on", "rec_start", "rec_stop", "rec_resume", "rec_dead", "rec_corrupt", "rec_refused",
    "camera_lost", "camera_back", "camera_usb2", "camera_usb2_heal", "camera_usb2_giveup",
    "unclean_boot", "recovered", "disk_full", "storage_stalled", "storage_lost",
    # not counted, but they tell a crash from a clean or requested restart in a cap's timeline
    "power_off", "storage_back", "update", "remote_reboot",
)
EVENT_PAGE = 1000                    # ZRANGEBYSCORE ... LIMIT 0 1000, repeated while full
EVENT_BACKFILL_CHUNK = 500           # ZREVRANGE chunks for a device seen for the first time
EVENT_MSG_MAX = 500
EVENT_QUIET_SLACK_S = 900.0          # see _step_device_events: who can have new events
TICKETS_PER_MGET = 100

STEPS = ("starcaps", "cameras", "cal_versions", "cal_current", "items", "takes",
         "camera_events", "redis_small", "device_events", "prune")

K_LOGS = "starcap:logs:{}"


# --------------------------------------------------------------------------- #
# SQL. %(name)s placeholders (psycopg2); every statement passes check_read_only.
# --------------------------------------------------------------------------- #
SQL_STARCAPS = """
select cpu_id, name, camera_id, data->>'board' as board,
       extract(epoch from created_at)::float8 as created_at
  from starcaps
 where project_id = %(project)s
   and coalesce(data->>'kind', '') <> 'dragonCamera'
   and data->>'deleted_at' is null
"""

SQL_CAMERAS = """
select id, status, (data ? 'retired') as retired, extract(epoch from created_at)::float8 as created_at
  from cameras
 where project_id = %(project)s and not starts_with(id, 'dragon:')
"""

SQL_CAL_VERSIONS = """
select v.id, v.camera_id, v.camera_key, v.rev, v.method,
       extract(epoch from v.calibrated_at)::float8 as calibrated_at,
       extract(epoch from v.uploaded_at)::float8 as uploaded_at,
       extract(epoch from v.superseded_at)::float8 as superseded_at,
       v.verdict, v.evidence_status,
       v.doc->'rmse'->'mono0' as rmse_left, v.doc->'rmse'->'mono1' as rmse_right,
       v.doc->'rmse'->'stereo' as rmse_stereo, v.doc->'baseline_mm' as baseline_mm
  from calibration_versions v
  join cameras c on c.id = v.camera_id
 where c.project_id = %(project)s and not starts_with(v.camera_id, 'dragon:')
"""

SQL_CAL_CURRENT = """
select k.id, k.camera_id, k.kind, k.data->>'rev' as rev,
       extract(epoch from k.calibrated_at)::float8 as calibrated_at,
       k.data->>'method' as method, k.data->'evidence'->>'status' as evidence_status,
       k.data->'evidence'->'acceptance'->>'verdict' as verdict, k.data->>'fit_digest' as fit_digest
  from camera_calibrations k
  join cameras c on c.id = k.camera_id
 where c.project_id = %(project)s and k.kind in ('stereo', 'imu') and not starts_with(k.camera_id, 'dragon:')
"""

# task: the trip's work_kind (the L1 category, "Repair Services") first, then the
# item's own free-text task, then the trip's, skipping the "(per camera)" placeholder
# a trip carries when every item has its own. destination: the item's, then the trip's
# (the dashboard's own rule). A deleted (tombstoned) check-out is dropped, as the
# dashboard does.
SQL_ITEMS = """
select i.id, i.trip_id, i.starcap_id as cpu_id, i.camera_id,
       extract(epoch from i.created_at)::float8 as checkout_at,
       extract(epoch from i.returned_at)::float8 as returned_at,
       i.missing,
       coalesce(nullif(t.work_kind, ''), nullif(i.data->>'task', ''),
                nullif(nullif(t.data->>'task', ''), '(per camera)')) as task,
       coalesce(nullif(i.data->>'destination', ''), nullif(t.destination, '')) as destination
  from trip_items i
  join trips t on t.id = i.trip_id
  left join starcaps s on s.cpu_id = i.starcap_id
 where t.project_id = %(project)s
   and t.data->>'deleted_at' is null
   and coalesce(s.data->>'kind', '') <> 'dragonCamera'
   and not starts_with(coalesce(i.camera_id, ''), 'dragon:')
   and (i.created_at >= to_timestamp(%(since)s) or i.returned_at is null)
"""

SQL_TAKES = """
select id, cpu_id, trip_item_id, trip_id, created_at, updated_at, recorded_s, kept_s, settled,
       readiness_status,
       d->'analysis'->>'first_copied_at' as first_copied_at,
       d->'uploaded'->'uploaded_at' as uploaded_at,
       d->'analysis'->'device_info'->>'started_at' as started_at,
       coalesce((d->'analysis'->'device_info') ? 'renamed_from', false) as clock_fixed,
       d->'analysis'->'device_info'->>'boot_id' as boot_id,
       d->'analysis'->'device_info'->>'software_version' as software_version,
       d->'analysis'->'device_info'->>'board' as board,
       d->'analysis'->'device_info'->'camera'->>'unit' as camera_unit,
       d->'analysis'->'device_info'->'camera'->>'usb_path' as usb_path,
       d->'analysis'->'fault'->>'kind' as fault_kind,
       d->'readiness_summary'->'codes' as codes,
       d->'readiness'->'streams'->'left'->'fps' as fps_left,
       d->'readiness'->'streams'->'stereo'->'fps' as fps_stereo,
       d->'timing'->'nominal_fps' as nominal_fps,
       d->'timing'->'score' as score,
       d->'timing'->'p99_dev_ms' as p99_dev_ms,
       d->'timing'->'out_of_band_pct' as out_of_band_pct,
       d->'timing'->'drop_pct' as drop_pct
  from (select id, cpu_id, trip_item_id, trip_id,
               extract(epoch from created_at)::float8 as created_at,
               extract(epoch from updated_at)::float8 as updated_at,
               recorded_s, kept_s, settled, readiness_status,
               data || '{}'::jsonb as d
          from recordings
         where project_id = %(project)s and {where}
        offset 0) r
 where coalesce(d->'analysis'->>'copied_via', '') <> 'dragonCamera'
"""


def takes_sql(where):
    """SQL_TAKES with its WHERE filled in (not str.format: the SQL itself has braces)."""
    return SQL_TAKES.replace("{where}", where)


TAKES_BY_CREATED = "created_at >= to_timestamp(%(t0)s) and created_at < to_timestamp(%(t1)s)"
TAKES_BY_UPDATED = ("updated_at > to_timestamp(%(updated)s) and created_at >= to_timestamp(%(floor)s) "
                    "and created_at < to_timestamp(%(t0)s)")

SQL_CAMERA_EVENTS = """
select id, extract(epoch from at)::float8 as at, action,
       data->>'camera' as camera, data->>'headset' as headset,
       data->>'replaced' as replaced, data->>'source' as source
  from events
 where project_id = %(project)s
   and action in ('camera.attached', 'camera.detached')
   and at >= to_timestamp(%(since)s)
   and not starts_with(coalesce(data->>'camera', ''), 'dragon:')
"""


# --------------------------------------------------------------------------- #
# normalisation (pure; tested offline)
# --------------------------------------------------------------------------- #
def _num(v):
    """float, or None for anything that isn't a finite number (JSON null, '', 'n/a')."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _int(v):
    f = _num(v)
    return None if f is None else int(round(f))


def _bool(v):
    return None if v is None else bool(v)


def _str(v):
    """Ids that are numbers in old JSON (camera 51) and strings in new ("51") -> "51"."""
    if v is None or v == "":
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _ts(v):
    return clock.parse_ts(v)


def normalise_take(r):
    """One row of SQL_TAKES -> a `takes` row (SPEC §2)."""
    codes = r.get("codes")
    fps = _num(r.get("fps_left"))
    return {
        "id": r["id"], "cpu_id": r.get("cpu_id"), "trip_item_id": r.get("trip_item_id"),
        "trip_id": r.get("trip_id"),
        "created_at": _num(r.get("created_at")), "updated_at": _num(r.get("updated_at")),
        "first_copied_at": _ts(r.get("first_copied_at")), "uploaded_at": _ts(r.get("uploaded_at")),
        "started_at": _ts(r.get("started_at")), "clock_fixed": bool(r.get("clock_fixed")),
        "boot_id": r.get("boot_id") or None, "software_version": r.get("software_version") or None,
        "board": r.get("board") or None, "camera_unit": _str(r.get("camera_unit")),
        "usb_path": r.get("usb_path") or None,
        "recorded_s": _num(r.get("recorded_s")), "kept_s": _num(r.get("kept_s")),
        "settled": _bool(r.get("settled")), "readiness_status": r.get("readiness_status") or None,
        "codes": [str(c) for c in codes] if isinstance(codes, list) else [],
        "fault_kind": r.get("fault_kind") or None,
        "nominal_fps": _int(r.get("nominal_fps")),
        "fps": fps if fps is not None else _num(r.get("fps_stereo")),
        "p99_dev_ms": _num(r.get("p99_dev_ms")), "out_of_band_pct": _num(r.get("out_of_band_pct")),
        "score": _num(r.get("score")), "drop_pct": _num(r.get("drop_pct")),
    }


def normalise_item(r):
    return {
        "id": r["id"], "trip_id": r.get("trip_id"), "cpu_id": r.get("cpu_id"),
        "camera_id": _str(r.get("camera_id")), "checkout_at": _num(r.get("checkout_at")),
        "returned_at": _num(r.get("returned_at")), "missing": bool(r.get("missing")),
        "task": r.get("task") or None, "destination": r.get("destination") or None,
    }


def normalise_starcap(r):
    return {"cpu_id": r["cpu_id"], "name": r.get("name") or None, "camera_id": _str(r.get("camera_id")),
            "board": r.get("board") or None, "created_at": _num(r.get("created_at"))}


def normalise_camera(r):
    return {"id": _str(r["id"]), "status": r.get("status") or None, "retired": bool(r.get("retired")),
            "created_at": _num(r.get("created_at"))}


def normalise_cal_version(r):
    return {
        "id": r["id"], "camera_id": _str(r.get("camera_id")), "camera_key": _str(r.get("camera_key")),
        "rev": _int(r.get("rev")) or 0, "method": r.get("method") or None,
        "calibrated_at": _num(r.get("calibrated_at")), "uploaded_at": _num(r.get("uploaded_at")),
        "superseded_at": _num(r.get("superseded_at")), "verdict": r.get("verdict") or None,
        "evidence_status": r.get("evidence_status") or None,
        "rmse_left": _num(r.get("rmse_left")), "rmse_right": _num(r.get("rmse_right")),
        "rmse_stereo": _num(r.get("rmse_stereo")), "baseline_mm": _num(r.get("baseline_mm")),
    }


def cal_rev(rev, row_id):
    """A current calibration's lens revision: data.rev, else the id's rN suffix
    (cal_39r1 -> 1), else 0 (the original lens)."""
    n = _int(rev)
    if n is not None:
        return n
    m = re.search(r"r(\d+)$", row_id or "")
    return int(m.group(1)) if m else 0


def normalise_cal_current(r):
    return {
        "id": r["id"], "camera_id": _str(r.get("camera_id")), "kind": r.get("kind"),
        "rev": cal_rev(r.get("rev"), r["id"]), "calibrated_at": _num(r.get("calibrated_at")),
        "method": r.get("method") or None, "evidence_status": r.get("evidence_status") or None,
        "verdict": r.get("verdict") or None, "fit_digest": r.get("fit_digest") or None,
    }


def normalise_camera_event(r):
    return {"id": r["id"], "at": _num(r.get("at")), "action": r.get("action"),
            "camera": _str(r.get("camera")), "headset": r.get("headset") or None,
            "replaced": _str(r.get("replaced")), "source": r.get("source") or None}


def _json(raw):
    """Upstash REST returns stored JSON as a string; tolerate an already-parsed value."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _pairs(flat):
    """HGETALL's flat [field, value, field, value, ...] -> [(field, parsed value)]."""
    flat = flat or []
    return [(flat[i], _json(flat[i + 1])) for i in range(0, len(flat) - 1, 2)]


def natural_key(s):
    """starcap2 before starcap10; "43" before "43.1" before "100"."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", str(s)) if p]


def normalise_ticket(t, fallback_id=None):
    if not isinstance(t, dict):
        return None
    tid = t.get("id") or fallback_id
    if not tid:
        return None
    return {
        "id": str(tid), "subject": t.get("subject") or "starcap", "headset": t.get("headset") or None,
        "camera": _str(t.get("camera")), "category": t.get("category") or None,
        "critical": bool(t.get("critical")), "description": t.get("description") or "",
        "reporter_name": t.get("reporter_name") or None, "status": t.get("status") or "open",
        "created_at": _ts(t.get("created_at")), "resolved_at": _ts(t.get("resolved_at")),
        "resolution_note": t.get("resolution_note") or None,
        "replacement_camera": _str(t.get("replacement_camera")),
        "photos": len(t.get("photos") or []) if isinstance(t.get("photos"), list) else 0,
    }


def devices_from_fleet(flat):
    """`starcap:fleet` (field "<device_id>|self:<device_id>") -> one `devices` row per
    device_id. Should a device ever have two reporters, the freshest heartbeat wins."""
    best = {}
    for field, doc in _pairs(flat):
        if not isinstance(doc, dict):
            continue
        device_id = doc.get("device_id") or str(field).split("|", 1)[0]
        if not device_id:
            continue
        row = {
            "device_id": device_id, "cpu_id": doc.get("cpu_id") or None, "name": doc.get("name") or None,
            "last_seen": _ts(doc.get("last_seen")), "software_version": doc.get("software_version") or None,
            "board": doc.get("board") or None, "status": doc.get("status") or None,
            "recording": _bool(doc.get("recording")), "disk_free": _int(doc.get("disk_free")),
            "disk_total": _int(doc.get("disk_total")), "connection_type": doc.get("connection_type") or None,
            "camera": _str(doc.get("camera")), "clock_synced": _bool(doc.get("clock_synced")),
        }
        old = best.get(device_id)
        if old is None or (row["last_seen"] or 0) > (old["last_seen"] or 0):
            best[device_id] = row
    return sorted(best.values(), key=lambda d: d["device_id"])


def kamerat_out(flat):
    """`starcap:kamerat:out` -> {cap: {operator_name, checked_out_at, camera, destination}}.
    The operator's phone and id are dropped here and never stored."""
    out = {}
    for cap, doc in _pairs(flat):
        if not isinstance(doc, dict):
            continue
        out[str(cap)] = {"operator_name": doc.get("operator_name") or None,
                         "checked_out_at": _ts(doc.get("checked_out_at")),
                         "camera": _str(doc.get("camera")), "destination": doc.get("destination") or None}
    return dict(sorted(out.items(), key=lambda kv: natural_key(kv[0])))


def station_from_opm(field, doc):
    activity = doc.get("activity") if isinstance(doc.get("activity"), dict) else {}
    drives = [d for d in doc.get("drives") or [] if isinstance(d, dict)]
    return {
        "id": str(field), "station_name": doc.get("station_name") or None, "version": doc.get("version"),
        "last_seen": _ts(doc.get("last_seen")),
        "activity": {"state": activity.get("state"), "message": activity.get("message")},
        "drives": [{"role": d.get("role"), "bytes_free": _int(d.get("bytes_free")),
                    "bytes_total": _int(d.get("bytes_total"))} for d in drives],
        "pending_review": _int(doc.get("pending_review")), "error": doc.get("error") or None,
    }


def is_partner_uploader(field, doc):
    """The partner laptop uploads Dragon cameras, which are out of scope (SPEC §0): its id
    is `partner-*` and its runs name DRAGON drives; the StarCap SSD uploader's never do."""
    if str(field).startswith("partner-"):
        return True
    names = [v for r in doc.get("runs") or [] if isinstance(r, dict) for v in (r.get("device"), r.get("drive"))]
    cur = doc.get("current") if isinstance(doc.get("current"), dict) else {}
    names += [cur.get("device"), cur.get("drive")]
    return any("dragon" in str(n).lower() for n in names if n)


def uploader_from_ssd(field, doc, log_lines=20):
    det = doc.get("detection") if isinstance(doc.get("detection"), dict) else {}
    cur = doc.get("current") if isinstance(doc.get("current"), dict) else None
    runs = {}
    for r in doc.get("runs") or []:
        if isinstance(r, dict):
            key = r.get("status") or "unknown"
            runs[key] = runs.get(key, 0) + 1
    logs = [x for x in doc.get("logs") or [] if isinstance(x, dict)][-log_lines:]
    return {
        "id": str(field), "name": doc.get("name") or None, "version": doc.get("version"),
        "last_seen": _ts(doc.get("last_seen")),
        "detection": {"state": det.get("state"), "level": det.get("level"), "message": det.get("message")},
        "error": doc.get("error") or None,
        "current": None if cur is None else {
            "run": cur.get("run"), "sent": _int(cur.get("sent")), "total": _int(cur.get("total")),
            "rate_bps": _num(cur.get("rate_bps"))},
        "runs": {k: runs.get(k, 0) for k in ("pending", "uploading", "done", "error")}
                | {k: v for k, v in runs.items() if k not in ("pending", "uploading", "done", "error")},
        "logs": [{"at": _ts(x.get("t")), "level": x.get("level"), "msg": str(x.get("msg") or "")[:300]}
                 for x in logs],
    }


def uploader_progress(prev, uploaders, now):
    """{uploader_id: {run, sent, changed_at}}: changed_at only moves when the upload in
    flight moves (a different run, or more bytes sent), so a stuck upload shows how long
    it has been stuck."""
    prev = prev or {}
    out = {}
    for u in uploaders:
        cur = u.get("current") or {}
        run, sent = cur.get("run"), cur.get("sent")
        p = prev.get(u["id"])
        same = isinstance(p, dict) and p.get("run") == run and p.get("sent") == sent
        out[u["id"]] = {"run": run, "sent": sent, "changed_at": p.get("changed_at") if same else now}
    return out


def label_tick(doc):
    """`starcap:ship:label:tick` with times in seconds; `waiting` is also lifted to the
    top level, where the pipeline view reads it."""
    if not isinstance(doc, dict):
        return None
    label = doc.get("label") if isinstance(doc.get("label"), dict) else {}
    return {"at": _ts(doc.get("at")), "ok": _bool(doc.get("ok")), "last_ok_at": _ts(doc.get("last_ok_at")),
            "status": doc.get("status"), "reasons": doc.get("reasons") or [],
            "waiting": _int(label.get("waiting")),
            "label": {k: _int(label.get(k)) for k in ("waiting", "pending", "considered", "deferred")}}


def event_row(device_id, ev):
    """A device log member (parsed) -> a `device_events` row, or None if it is not one
    of EVENT_TYPES or is malformed."""
    if not isinstance(ev, dict) or ev.get("type") not in EVENT_TYPES:
        return None
    seq, ts = _int(ev.get("seq")), _ts(ev.get("ts"))
    if seq is None or ts is None:
        return None
    return {"device_id": device_id, "seq": seq, "ts": ts, "type": ev["type"],
            "msg": str(ev.get("msg") or "")[:EVENT_MSG_MAX]}


def windows(t0, t1, size):
    """[t0, t1) cut into windows of `size` seconds, NEWEST first (so the days a person
    looks at first land first)."""
    out, end = [], t1
    while end > t0:
        start = max(t0, end - size)
        out.append((start, end))
        end = start
    return out


# --------------------------------------------------------------------------- #
# the syncer
# --------------------------------------------------------------------------- #
class Syncer:
    """`run_once()` is safe from any thread: a second call while a pass is running (in
    this process, or in another process on the same database file) returns at once
    with `skipped: True`."""

    def __init__(self, store, pg=None, redis=None):
        self.store = store
        self._pg, self._redis = pg, redis
        self._own_pg = pg is None
        self._lock = threading.Lock()
        self._pg_down = None

    @property
    def running(self):
        return self._lock.locked()

    # ---------------------------------------------------------------- sources
    def _postgres(self):
        if self._pg_down:
            raise sources.SourceError(self._pg_down)
        try:
            if self._pg is None:
                self._pg = sources.Postgres.from_env()
            if hasattr(self._pg, "ensure"):
                self._pg.ensure()
        except sources.SourceError as e:
            self._pg_down = str(e)          # fail every later Postgres step fast, same reason
            raise
        return self._pg

    def _upstash(self):
        if self._redis is None:
            self._redis = sources.Redis.from_env()
        return self._redis

    def redis_stats(self):
        r = self._redis
        return r.stats() if r is not None and hasattr(r, "stats") else None

    # ---------------------------------------------------------------- running
    def run_once(self, full=False, rebuild=False):
        """One pass over every step -> the status dict also stored as kv `sync`.
        full: re-pull every take of the last HISTORY_DAYS. rebuild: empty the cache
        (notes and summaries survive) and pull everything, done under the same locks as
        a pass so it can never wipe tables under a sync that is still writing them.
        While another pass runs, returns at once with `skipped: True` and a `reason`."""
        if not self._lock.acquire(blocking=False):
            return {**(self.store.kv_get("sync") or {}), "skipped": True, "reason": "a sync is already running"}
        try:
            with _ProcessLock(self.store.path) as locked:
                if not locked:
                    return {**(self.store.kv_get("sync") or {}), "skipped": True,
                            "reason": "another process is syncing this database"}
                if rebuild:
                    self.store.clear_cache()
                return self._run(full or rebuild)
        finally:
            self._lock.release()

    def _run(self, full):
        self._pg_down = None
        prev = self.store.kv_get("sync") or {}
        status = {"last_run_at": time.time(), "last_ok_at": prev.get("last_ok_at"), "last_error": None,
                  "sources": dict(prev.get("sources") or {})}
        steps = {
            "starcaps": self._step_starcaps, "cameras": self._step_cameras,
            "cal_versions": self._step_cal_versions, "cal_current": self._step_cal_current,
            "items": self._step_items, "takes": lambda: self._step_takes(full),
            "camera_events": self._step_camera_events, "redis_small": self._step_redis_small,
            "device_events": self._step_device_events, "prune": self._step_prune,
        }
        try:
            for name in STEPS:
                t0 = time.monotonic()
                try:
                    rows, error = steps[name](), None
                except Exception as e:              # noqa: BLE001  one source must not sink the pass
                    rows, error = 0, sources.scrub(f"{type(e).__name__}: {e}")
                status["sources"][name] = {"ok": error is None, "at": time.time(), "rows": int(rows or 0),
                                           "took_s": round(time.monotonic() - t0, 2), "error": error}
                if error and status["last_error"] is None:
                    status["last_error"] = f"{name}: {error}"
                self.store.kv_set("sync", status)       # progress is visible while a long pass runs
        finally:
            if self._own_pg and self._pg is not None and hasattr(self._pg, "close"):
                self._pg.close()                    # don't hold a pooler slot for ten idle minutes
        if status["last_error"] is None:
            status["last_ok_at"] = time.time()
        self.store.kv_set("sync", status)
        return status

    # ---------------------------------------------------------------- Postgres steps
    def _query(self, sql, **params):
        return self._postgres().query(sql, {"project": config.PROJECT, **params})

    def _step_starcaps(self):
        return self.store.replace_all("starcaps", [normalise_starcap(r) for r in self._query(SQL_STARCAPS)])

    def _step_cameras(self):
        return self.store.replace_all("cameras", [normalise_camera(r) for r in self._query(SQL_CAMERAS)])

    def _step_cal_versions(self):
        rows = [normalise_cal_version(r) for r in self._query(SQL_CAL_VERSIONS)]
        return self.store.replace_all("cal_versions", rows)

    def _step_cal_current(self):
        rows = [normalise_cal_current(r) for r in self._query(SQL_CAL_CURRENT)]
        return self.store.replace_all("cal_current", rows)

    def _step_items(self):
        since = time.time() - config.HISTORY_DAYS * DAY_S
        rows = [normalise_item(r) for r in self._query(SQL_ITEMS, since=since)]
        # The window was pulled whole, so replace it: an item deleted upstream goes here too.
        return self.store.replace_where("items", rows, "checkout_at >= ? OR returned_at IS NULL", (since,))

    def _step_camera_events(self):
        since = time.time() - config.HISTORY_DAYS * DAY_S
        rows = [normalise_camera_event(r) for r in self._query(SQL_CAMERA_EVENTS, since=since)]
        return self.store.replace_where("camera_events", rows, "at >= ?", (since,))

    def _takes_range(self, t0, t1):
        """Takes created in [t0, t1); a window the database cancels is split in half."""
        try:
            raw = self._query(takes_sql(TAKES_BY_CREATED), t0=t0, t1=t1)
        except sources.QueryTimeout:
            if t1 - t0 <= TAKES_MIN_WINDOW_S:
                raise
            mid = (t0 + t1) / 2
            return self._takes_range(mid, t1) + self._takes_range(t0, mid)
        return [normalise_take(r) for r in raw]

    def _pull_range(self, t0, t1, size):
        """Pull every take created in [t0, t1) window by window, upserting as it goes,
        then delete local takes in that range that production no longer has."""
        ids = []
        for a, b in windows(t0, t1, size):
            rows = self._takes_range(a, b)
            self.store.upsert("takes", rows)
            ids += [r["id"] for r in rows]
        self.store.delete_takes_missing(ids, t0, t1)
        return len(ids)

    def _step_takes(self, full):
        now = time.time()
        cursor = self.store.kv_get("takes_cursor") or {}
        if full or not cursor.get("backfilled_at") or self.store.count("takes") == 0:
            n = self._pull_range(now - config.HISTORY_DAYS * DAY_S, now + FUTURE_S, TAKES_WINDOW_S)
            self.store.kv_set("takes_cursor", {"backfilled_at": now, "synced_at": now})
            return n
        recent = now - TAKES_RECENT_DAYS * DAY_S
        n = self._pull_range(recent, now + FUTURE_S, TAKES_WINDOW_S)
        # Older takes whose row changed since the last pass (readiness/timing workers,
        # late uploads). Anything older than the prune horizon would only be pruned again.
        updated = (cursor.get("synced_at") or now) - TAKES_UPDATED_SLACK_S
        raw = self._query(takes_sql(TAKES_BY_UPDATED), updated=updated,
                          floor=now - PRUNE_DAYS * DAY_S, t0=recent)
        n += self.store.upsert("takes", [normalise_take(r) for r in raw])
        self.store.kv_set("takes_cursor", {**cursor, "synced_at": now})
        return n

    # ---------------------------------------------------------------- Redis steps
    def _step_redis_small(self):
        r = self._upstash()
        keys = {
            "index": ["ZREVRANGE", "starcap:maint:index", 0, -1],
            "blocked": ["SMEMBERS", "starcap:maint:blocked"],
            "broken": ["SMEMBERS", "starcap:maint:broken_cam"],
            "retired": ["HKEYS", "starcap:cameras:retired"],
            "revs": ["SMEMBERS", "starcap:cameras:revs"],
            "out": ["HGETALL", "starcap:kamerat:out"],
            "fleet": ["HGETALL", "starcap:fleet"],
            "opm": ["HGETALL", "starcap:opm"],
            "ssd": ["HGETALL", "starcap:ssd"],
            "tick": ["GET", "starcap:ship:label:tick"],
        }
        got = dict(zip(keys, r.pipeline(list(keys.values()))))
        read_at = time.time()           # heartbeat ages are measured from here, not from "now"

        ids = [str(i) for i in got["index"] or []]
        mgets = [["MGET", *[f"starcap:maint:ticket:{i}" for i in ids[k:k + TICKETS_PER_MGET]]]
                 for k in range(0, len(ids), TICKETS_PER_MGET)]
        raw = [v for chunk in (r.pipeline(mgets) if mgets else []) for v in chunk or []]
        tickets = [t for t in (normalise_ticket(_json(v), i) for i, v in zip(ids, raw)) if t]
        devices = devices_from_fleet(got["fleet"])
        stations = [station_from_opm(f, d) for f, d in _pairs(got["opm"]) if isinstance(d, dict)]
        uploaders = [uploader_from_ssd(f, d) for f, d in _pairs(got["ssd"])
                     if isinstance(d, dict) and not is_partner_uploader(f, d)]
        now = time.time()

        self.store.replace_all("tickets", tickets)
        self.store.replace_all("devices", devices)
        kv = {
            "maint_blocked": sorted(map(str, got["blocked"] or []), key=natural_key),
            "maint_broken_cam": sorted(map(str, got["broken"] or []), key=natural_key),
            "cameras_retired": sorted(map(str, got["retired"] or []), key=natural_key),
            "cameras_revs": sorted((str(m).removeprefix("cam-") for m in got["revs"] or []), key=natural_key),
            "kamerat_out": kamerat_out(got["out"]),
            "stations": stations,
            "uploaders": uploaders,
            "uploader_progress": uploader_progress(self.store.kv_get("uploader_progress"), uploaders, now),
            "label_tick": label_tick(_json(got["tick"])),
            "heartbeats_at": read_at,
        }
        for key, value in kv.items():
            self.store.kv_set(key, value)
        return len(tickets) + len(devices)

    def _event_devices(self, now):
        """{device_id: last_seen} for devices heard from within EVENT_DEVICE_DAYS that
        belong to a known StarCap (a cpu_id in `starcaps`), so a vin board or a stray
        bench board costs nothing."""
        caps = {c["cpu_id"] for c in self.store.starcaps()}
        horizon = now - config.EVENT_DEVICE_DAYS * DAY_S
        return {d["device_id"]: d["last_seen"] for d in self.store.devices()
                if d["last_seen"] and d["last_seen"] >= horizon and (not caps or d["cpu_id"] in caps)}

    def _step_device_events(self):
        r, now = self._upstash(), time.time()
        cursor = self.store.kv_get("device_event_cursor") or {}
        stored = self.store.max_seq()       # fallback if the cursor was lost but rows remain
        cutoff = now - config.EVENT_BACKFILL_DAYS * DAY_S
        if (cursor.get("types"), cursor.get("backfill_days")) != (list(EVENT_TYPES), config.EVENT_BACKFILL_DAYS):
            # A changed type list or window re-backfills every device, so a newly added
            # type gets its last EVENT_BACKFILL_DAYS too (upserts make the overlap harmless).
            cursor, stored = {"types": list(EVENT_TYPES), "backfill_days": config.EVENT_BACKFILL_DAYS,
                              "seq": {}}, {}
            # Days before this have no device log, so the day view can say so instead of
            # reading "no reboots, no drops" off an empty table. Older rows survive a
            # re-backfill, hence the min.
            self.store.kv_set("events_since", min(self.store.kv_get("events_since") or cutoff, cutoff))
        seqs = cursor["seq"]
        last_seen = self._event_devices(now)
        rows = []

        # Known devices: everything after the highest seq already seen, a page at a time.
        # A recorder uploads its log over the same link it heartbeats on, so a device not
        # heard from since the previous pass (minus slack) has nothing new and is skipped.
        # The yardstick is the heartbeats' own newest last_seen, not this laptop's clock,
        # so a stale devices table (Redis step failed) can never make a device skipped
        # for good: the yardstick simply doesn't move until fresh heartbeats arrive.
        known = {d: max(int(seqs.get(d) or 0), int(stored.get(d) or 0))
                 for d in last_seen if d in seqs or d in stored}
        fresh = [d for d in last_seen if d not in known]
        seen_upto = cursor.get("seen_upto")
        pending = {d: s for d, s in known.items()
                   if seen_upto is None or last_seen[d] >= seen_upto - EVENT_QUIET_SLACK_S}
        while pending:
            cmds = [["ZRANGEBYSCORE", K_LOGS.format(d), f"({s}", "+inf", "LIMIT", 0, EVENT_PAGE]
                    for d, s in pending.items()]
            nxt = {}
            for (d, s), members in zip(pending.items(), r.pipeline(cmds)):
                events = [e for e in map(_json, members or []) if isinstance(e, dict)]
                rows += [x for x in (event_row(d, e) for e in events) if x]
                top = max([s] + [_int(e.get("seq")) or 0 for e in events])
                seqs[d] = top
                if len(members or []) >= EVENT_PAGE and top > s:
                    nxt[d] = top
            pending = nxt

        # First sight of a device: walk back from its newest event to EVENT_BACKFILL_DAYS.
        # (A log is capped at 5000 events, so a reboot-looping board may not reach that
        # far; it has thousands of events in what is there, so its flags still fire.)
        offsets = {d: 0 for d in fresh}
        while offsets:
            cmds = [["ZREVRANGE", K_LOGS.format(d), off, off + EVENT_BACKFILL_CHUNK - 1]
                    for d, off in offsets.items()]
            nxt = {}
            for (d, off), members in zip(offsets.items(), r.pipeline(cmds)):
                events = [e for e in map(_json, members or []) if isinstance(e, dict)]
                seqs[d] = max([int(seqs.get(d) or 0)] + [_int(e.get("seq")) or 0 for e in events])
                rows += [x for x in (event_row(d, e) for e in events) if x and x["ts"] >= cutoff]
                oldest = min([_ts(e.get("ts")) or 0 for e in events] or [0])
                if len(members or []) >= EVENT_BACKFILL_CHUNK and oldest >= cutoff:
                    nxt[d] = off + EVENT_BACKFILL_CHUNK
            offsets = nxt

        self.store.upsert("device_events", rows)
        cursor["seen_upto"] = max([seen_upto or 0, *last_seen.values()]) or None
        self.store.kv_set("device_event_cursor", cursor)
        return len(rows)

    # ---------------------------------------------------------------- housekeeping
    def _step_prune(self):
        pruned = self.store.prune(time.time() - PRUNE_DAYS * DAY_S)
        self.store.fill_starcap_boards()
        return sum(pruned.values())


class _ProcessLock:
    """flock on <db>.sync.lock, so the always-on service and a one-shot sync from the
    terminal never pull from production at the same time. No-op for an in-memory db."""

    def __init__(self, db_path):
        p = str(db_path)
        self.path = None if p == ":memory:" else Path(p + ".sync.lock")
        self.fd = None

    def __enter__(self):
        if self.path is None:
            return True
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_status(status, upstash=None, out=None):
    out = out or sys.stdout
    if status.get("skipped"):
        print(f"skipped: {status.get('reason')}", file=out)
        return
    print(f"{'step':<14} {'result':<6} {'rows':>8} {'seconds':>8}  error", file=out)
    for name in STEPS:
        s = status["sources"].get(name) or {}
        print(f"{name:<14} {'ok' if s.get('ok') else 'ERROR':<6} {s.get('rows', 0):>8} "
              f"{s.get('took_s', 0):>8.2f}  {s.get('error') or ''}", file=out)
    total = sum((status["sources"].get(n) or {}).get("took_s", 0) for n in STEPS)
    print(f"{'total':<14} {'':<6} {'':>8} {total:>8.2f}", file=out)
    if upstash:
        print(f"upstash: {upstash['requests']} requests, {upstash['commands']} commands, "
              f"{upstash['bytes_sent'] / 1024:.0f} KiB sent, {upstash['bytes_received'] / 1024:.0f} KiB received",
              file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull production into the tracker cache once (read-only).")
    ap.add_argument("--full", action="store_true", help="re-pull every take of the last HISTORY_DAYS")
    ap.add_argument("--rebuild", action="store_true", help="empty the cache first (keeps notes and summaries)")
    ap.add_argument("--db", default=str(config.DB_PATH), help="SQLite cache file (default data/tracker.db)")
    args = ap.parse_args(argv)
    store = Store(args.db)
    syncer = Syncer(store)
    status = syncer.run_once(full=args.full, rebuild=args.rebuild)
    print_status(status, syncer.redis_stats())
    return 0 if status.get("skipped") or status.get("last_error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
