"""
pipeline.py: is footage getting from the caps to the cloud?

Three hops, each with a heartbeat of its own: the copy station (opm-lan) pulls footage
off docked caps onto its staging SSD, the SSD uploader ships it to GCS, and the label
tick (a Vercel cron) hands uploaded clips on to labelling. An uploader is ok while its
heartbeat is fresh and clean, warn once it has gone quiet, and bad when it has been
silent for an hour, reports an error, or is stuck: its current upload has not moved
for UPLOAD_STUCK_MIN. That is how the IPv6 blackhole looked: a full send queue, no
error, no progress.

The copy station is different: it only runs while caps come back. On 2026-10-04..12 it
copied between 20:00 and 04:00 and went quiet every morning, and caps are copied at
check-in (return to first copy: p50 0 h). So silence alone means nothing; silence
while caps that recorded are back with nothing copied (`awaiting_copy`) is the fault.
A quiet station with nothing waiting is "idle".

Heartbeats are judged at `heard_at`, the moment sync read them from Redis, not at the
wall clock: the cache is refreshed every SYNC_EVERY_S (10 min), so a 5-minute stale
line measured from the wall clock would flag every healthy station between syncs,
and a laptop that slept all night would call the whole pipeline dead. How old the
cache itself is, the status bar says.

Times are epoch seconds; byte rates are bytes/s in and MB/s out, the uploader's own
"mbps" convention.
"""
import math

import clock
import config

RUN_STATES = ("pending", "uploading", "done", "error")


def health(age_s, error=None):
    """ok: seen within STATION_STALE_S and no error; warn: seen within STATION_DOWN_S;
    bad: silent longer, or never seen."""
    if age_s is None or age_s >= config.STATION_DOWN_S:
        return "bad"
    return "warn" if age_s >= config.STATION_STALE_S or error else "ok"


def _age(seen, now):
    return None if seen is None else max(0.0, now - seen)


def station_health(age_s, error, waiting):
    """ok/warn while it reports; once quiet, idle if nothing is waiting for it, else warn
    and, after STATION_DOWN_S, bad."""
    if age_s is not None and age_s < config.STATION_STALE_S:
        return "warn" if error else "ok"
    if not waiting:
        return "idle"
    return "warn" if age_s is not None and age_s < config.STATION_DOWN_S else "bad"


def awaiting_copy(items, copied, rec_starts, now):
    """Check-outs back for AWAIT_COPY_AFTER_S up to a day, with nothing copied
    (`copied` = item ids that have a take) although the recorder logged a rec_start
    during the trip (`rec_starts` = {cpu_id: [ts]}): footage stranded at the station."""
    out = []
    for i in items:
        back = i.get("returned_at")
        if back is None or i["id"] in copied or not now - clock.DAY_S <= back <= now - config.AWAIT_COPY_AFTER_S:
            continue
        if any(i["checkout_at"] <= t <= back for t in rec_starts.get(i["cpu_id"], ())):
            out.append(i)
    return out


def stranded_by(station, waiting):
    """The waiting check-outs this station can be blamed for: back after it went quiet,
    or within AWAIT_COPY_AFTER_S of its last heartbeat. A cap that came back while the
    station kept reporting for longer than that was skipped for its own reason (a dead
    card, never docked): the Day page flags that as footage_missing, not the station."""
    seen = station.get("last_seen")
    return [i for i in waiting if seen is None or i["returned_at"] > seen - config.AWAIT_COPY_AFTER_S]


def stations(rows, now, waiting):
    out = []
    for s in rows:
        age = _age(s.get("last_seen"), now)
        mine = stranded_by(s, waiting)
        activity = s.get("activity") or {}
        staging = next((d for d in s.get("drives") or () if d.get("role") == "staging"), None)
        out.append({"id": s.get("id"), "name": s.get("station_name") or s.get("id"),
                    "last_seen": s.get("last_seen"), "age_s": None if age is None else round(age),
                    "state": activity.get("state"), "message": activity.get("message"),
                    "staging_free_gb": round(staging["bytes_free"] / 1e9, 1) if staging else None,
                    "pending_review": s.get("pending_review"), "error": s.get("error"),
                    "waiting_caps": len(mine), "status": station_health(age, s.get("error"), mine)})
    return out


def stuck_minutes(uploader, progress, now):
    """Minutes the current upload's `sent` has not moved (sync stamps changed_at each
    time run or sent changes), or 0 when nothing is uploading."""
    cur, seen = uploader.get("current") or {}, progress.get(uploader.get("id")) or {}
    if not cur.get("total") or (cur.get("sent") or 0) >= cur["total"] or seen.get("changed_at") is None:
        return 0.0
    return max(0.0, now - seen["changed_at"]) / 60


def _current(cur):
    if not cur:
        return None
    total, sent = cur.get("total"), cur.get("sent")
    return {"run": cur.get("run"), "sent": sent, "total": total,
            "pct": round(100.0 * sent / total, 1) if total and sent is not None else None,
            "rate_mbps": round(cur["rate_bps"] / 1e6, 1) if cur.get("rate_bps") is not None else None}


def uploaders(rows, progress, now):
    out = []
    for u in rows:
        age = _age(u.get("last_seen"), now)
        detection = u.get("detection") or {}
        stuck_min = stuck_minutes(u, progress, now)
        stuck = stuck_min >= config.UPLOAD_STUCK_MIN
        runs = u.get("runs") or {}
        out.append({"id": u.get("id"), "name": u.get("name") or u.get("id"), "last_seen": u.get("last_seen"),
                    "age_s": None if age is None else round(age), "state": detection.get("state"),
                    "level": detection.get("level"), "message": detection.get("message"),
                    "runs": {k: runs.get(k, 0) for k in RUN_STATES}, "current": _current(u.get("current")),
                    "error": u.get("error"), "stuck": stuck, "stuck_min": round(stuck_min),
                    "status": "bad" if stuck or u.get("error") else health(age)})
    return out


def backlog(takes, now):
    """Takes (the caller passes the whole local history, HISTORY_DAYS) that never reached
    the cloud; those copied over a day ago are stuck, not just queued (copy to cloud
    p90 is ~17 h)."""
    left = [t for t in takes if t.get("uploaded_at") is None]
    stuck = [t for t in left if now - t["created_at"] > clock.DAY_S]

    def hours(rows):
        return round(sum(t.get("recorded_s") or 0.0 for t in rows) / 3600, 2)

    return {"takes": len(left), "hours": hours(left),
            "oldest_h": round((now - min(t["created_at"] for t in left)) / 3600, 2) if left else None,
            "over_24h": {"takes": len(stuck), "hours": hours(stuck)}}


def percentile(values, p):
    """Linear interpolation between closest ranks (numpy's default)."""
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def spread(hours):
    if not hours:
        return {"p50": None, "p90": None, "n": 0}
    return {"p50": round(percentile(hours, 50), 2), "p90": round(percentile(hours, 90), 2), "n": len(hours)}


def lag(takes, items, now):
    """Over the takes uploaded in the last 7 days: check-out to cloud (how long after a
    cap goes out its footage can be counted, the reason "yesterday" fills in during
    the day) and copy to cloud (the station and uploader alone).

    Not return to copy: docking records the return, so the copy usually comes first
    (344 of 538 items on 2026-10-12) and that lag reads 0 h whatever happens."""
    since = now - 7 * clock.DAY_S
    out_at = {i["id"]: i["checkout_at"] for i in items}
    done = [t for t in takes if t.get("uploaded_at") is not None and t["uploaded_at"] >= since]
    from_checkout = [max(0.0, t["uploaded_at"] - out_at[t["trip_item_id"]]) / 3600 for t in done
                     if t.get("trip_item_id") in out_at]
    to_cloud = [max(0.0, t["uploaded_at"] - t["created_at"]) / 3600 for t in done]
    return {"checkout_to_cloud_h": spread(from_checkout), "copy_to_cloud_h": spread(to_cloud)}


def label(tick, now):
    tick = tick or {}
    at = tick.get("at")
    return {"at": at, "age_min": None if at is None else round(max(0.0, now - at) / 60),
            "ok": tick.get("ok"), "waiting": (tick.get("label") or {}).get("waiting")}


def view(now, heard_at, station_rows, uploader_rows, progress, tick, takes, items, rec_starts):
    """GET /api/pipeline. Heartbeats (stations, uploaders, label tick) are aged at
    `heard_at`; backlog and lag at `now`. `takes` are every take in the local history,
    `items` the check-outs of the last 14 days (so every take uploaded this week has its
    check-out), `rec_starts` {cpu_id: [ts]} for the last few days."""
    copied = {t["trip_item_id"] for t in takes if t.get("trip_item_id")}
    waiting = awaiting_copy(items, copied, rec_starts, now)
    return {"stations": stations(station_rows, heard_at, waiting),
            "uploaders": uploaders(uploader_rows, progress, heard_at),
            "backlog": backlog(takes, now),
            "lag": lag(takes, items, now),
            "label": label(tick, heard_at)}


def problems(view_):
    """Short phrases for whatever in a pipeline view is not ok (the morning summary)."""
    out = []
    for s in view_["stations"]:
        if s["status"] in ("warn", "bad") and s.get("waiting_caps"):
            quiet = "not seen" if s["age_s"] is None else f"silent {_ago(s['age_s'])}"
            n = s["waiting_caps"]
            out.append(f"copy station {quiet} with footage from {n} cap{'' if n == 1 else 's'} waiting")
        elif s["error"]:
            out.append(f"copy station error: {str(s['error'])[:60]}")
    for s in view_["uploaders"]:
        if s.get("stuck"):
            out.append(f"{s['name']} stuck {s['stuck_min']} min")
        elif s["error"]:
            out.append(f"{s['name']} error: {str(s['error'])[:60]}")
        elif s["status"] != "ok":
            out.append(f"{s['name']} " + ("not seen" if s["age_s"] is None else f"silent {_ago(s['age_s'])}"))
    backlog_ = view_.get("backlog") or {}
    old = backlog_.get("over_24h") or {}
    if old.get("takes"):
        out.append(f"{old['takes']} takes ({old['hours']:.0f} h) copied but not in the cloud after a day, "
                   f"oldest {_ago(backlog_['oldest_h'] * 3600)}")
    if view_["label"]["ok"] is False:
        out.append("label tick failing")
    return out


def _ago(seconds):
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.0f} h" if seconds < 2 * clock.DAY_S else f"{seconds / clock.DAY_S:.0f} days"
