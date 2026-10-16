"""
maint.py: maintenance tickets, what still needs one, and my own unit statuses.

Tickets live on the dashboard (Redis starcap:maint:*). A ticket is about a starcap
(its unit is the headset) or a camera (its unit is the camera), and is open or
resolved; re-opening deletes resolved_at. Reporting a broken camera opens BOTH: a
non-critical camera ticket that follows the lens (its headset is only provenance)
and a critical starcap ticket that blocks check-out until a working camera goes in.
That pairing is why "36 open (31 critical)" is normal.

"Needs attention" is the list nobody has written a ticket for yet: caps that raised a
bad or warn flag on a field day in the last NEEDS_ATTENTION_DAYS, minus caps with an
open ticket on the cap or on a camera it carried, minus caps I have already marked
repair / dead / lost.

My notes never leave this machine. A unit's current status is the newest note that
set one, and an 'ok' note clears it.
"""
import statistics
from collections import Counter, defaultdict

import calib
import clock
import config
import rules

PARKED = ("repair", "dead", "lost")      # my statuses for "known, being dealt with"
NOT_A_REPAIR = ("expired_cal",)         # the calibration page queues these, not a ticket
RECENT_RESOLVED = 20                     # how many closed tickets the page lists


def subject(t):
    """Legacy tickets have no subject: they were all about a starcap."""
    return t.get("subject") or "starcap"


def unit(t):
    return t.get("camera") if subject(t) == "camera" else t.get("headset")


def is_open(t):
    return t.get("status") != "resolved"


def resolved_by(t, end):
    return t.get("resolved_at") is not None and t["resolved_at"] < end


class OpenTickets:
    """Open tickets by unit (cap name, camera base), so "is anything open on this cap
    or the camera it carried" is two lookups."""

    def __init__(self, tickets):
        self.by_cap, self.by_camera = defaultdict(set), defaultdict(set)
        for t in tickets:
            if is_open(t) and unit(t) is not None:
                if subject(t) == "camera":
                    self.by_camera[calib.base_of(unit(t))].add(t["id"])
                else:
                    self.by_cap[unit(t)].add(t["id"])

    def on(self, cap, cameras=()):
        ids = set(self.by_cap.get(cap, ()))
        for c in cameras:
            ids |= self.by_camera.get(calib.base_of(c), set())
        return ids


# --------------------------------------------------------------------------- #
# ticket rows
# --------------------------------------------------------------------------- #
def ticket_row(t, now):
    """A ticket as the open list shows it. age_days runs to now while open, to the
    resolution once resolved."""
    end = now if is_open(t) or t.get("resolved_at") is None else t["resolved_at"]
    return {"id": t["id"], "subject": subject(t), "unit": unit(t), "headset": t.get("headset"),
            "camera": t.get("camera"), "category": t.get("category"), "critical": bool(t.get("critical")),
            "description": t.get("description"), "reporter": t.get("reporter_name"),
            "created_at": t["created_at"], "age_days": round((end - t["created_at"]) / clock.DAY_S, 1),
            "photos": t.get("photos") or 0}


def resolved_row(t, now):
    return dict(ticket_row(t, now), resolved_at=t.get("resolved_at"), resolution_note=t.get("resolution_note"))


def history_row(t, now):
    """A ticket on a cap or camera page, open or not."""
    return dict(resolved_row(t, now), status=t.get("status"))


def unit_tickets(tickets, now, cap=None, camera=None):
    """Every ticket whose unit is this cap (or whose camera is this base), newest first."""
    mine = [t for t in tickets
            if (cap is not None and subject(t) == "starcap" and t.get("headset") == cap)
            or (camera is not None and t.get("camera") is not None and calib.base_of(t["camera"]) == camera)]
    return [history_row(t, now) for t in sorted(mine, key=lambda t: -t["created_at"])]


def trip_tickets(tickets, cap, cameras, out_at, back_at, now):
    """Tickets that speak to one check-out: already open on the cap or a camera it
    carried when it went out ("before"), or filed from check-out until
    TRIP_TICKET_AFTER_H after the return ("after"). Oldest first."""
    bases = {calib.base_of(c) for c in cameras if c}
    until = (now if back_at is None else back_at) + config.TRIP_TICKET_AFTER_H * 3600
    out = []
    for t in sorted(tickets, key=lambda t: t["created_at"]):
        mine = ((subject(t) == "starcap" and t.get("headset") == cap)
                or (subject(t) == "camera" and t.get("camera") is not None and calib.base_of(t["camera"]) in bases))
        if not mine:
            continue
        if t["created_at"] < out_at:
            if is_open(t) or (t.get("resolved_at") or 0) > out_at:
                out.append(dict(history_row(t, now), when="before"))
        elif t["created_at"] <= until:
            out.append(dict(history_row(t, now), when="after"))
    return out


def open_list(tickets, now):
    """Critical first, then oldest first."""
    return [ticket_row(t, now) for t in sorted((t for t in tickets if is_open(t)),
                                                key=lambda t: (not t.get("critical"), t["created_at"]))]


def recent_resolved(tickets, end, now):
    done = sorted((t for t in tickets if not is_open(t) and resolved_by(t, end)), key=lambda t: -t["resolved_at"])
    return [resolved_row(t, now) for t in done[:RECENT_RESOLVED]]


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #
def stats(tickets, day, now, blocked, broken):
    """Open counts are as of now; opened / resolved are on `day`; the median time to
    resolve covers tickets resolved in the 30 days ending with `day`."""
    start, end = clock.day_bounds(day)
    since = clock.day_bounds(clock.shift_day(day, -29))[0]
    open_ = [t for t in tickets if is_open(t)]
    took = [(t["resolved_at"] - t["created_at"]) / clock.DAY_S for t in tickets
            if not is_open(t) and t.get("resolved_at") is not None and since <= t["resolved_at"] < end]
    return {"open": len(open_), "critical": sum(bool(t.get("critical")) for t in open_),
            "opened": sum(start <= t["created_at"] < end for t in tickets),
            "resolved": sum(t.get("resolved_at") is not None and start <= t["resolved_at"] < end for t in tickets),
            "median_resolve_days": round(statistics.median(took), 1) if took else None,
            "oldest_open_days": round(max((now - t["created_at"]) / clock.DAY_S for t in open_), 1) if open_ else None,
            "blocked_caps": len(set(blocked)), "broken_cams": len(set(map(str, broken)))}


def by_category(tickets, since, end):
    cats = defaultdict(Counter)
    for t in tickets:
        c = cats[t.get("category") or "other"]
        c["open"] += is_open(t)
        c["last_30d"] += since <= t["created_at"] < end
    rows = [{"category": k, "open": v["open"], "last_30d": v["last_30d"]} for k, v in cats.items()
            if v["open"] or v["last_30d"]]
    return sorted(rows, key=lambda r: (-r["open"], -r["last_30d"], r["category"]))


def repeat_offenders(tickets, since, end):
    """Units with two or more tickets created in the window, most tickets first."""
    recent = defaultdict(list)
    for t in tickets:
        if unit(t) is not None and since <= t["created_at"] < end:
            recent[(subject(t), str(unit(t)))].append(t["created_at"])
    still_open = Counter((subject(t), str(unit(t))) for t in tickets if is_open(t) and unit(t) is not None)
    rows = [{"unit": u, "subject": s, "tickets_30d": len(ts), "open": still_open[(s, u)], "last_at": max(ts)}
            for (s, u), ts in recent.items() if len(ts) >= 2]
    return sorted(rows, key=lambda r: (-r["tickets_30d"], -r["last_at"]))


def trend(tickets, days):
    """Per day: open at the day's end (created by then, not resolved by then), opened
    and resolved that day. Rebuilt from created_at / resolved_at, so it needs no
    history of its own."""
    out = []
    for day in days:
        start, end = clock.day_bounds(day)
        out.append({"date": day,
                    "open": sum(t["created_at"] < end and not resolved_by(t, end) for t in tickets),
                    "opened": sum(start <= t["created_at"] < end for t in tickets),
                    "resolved": sum(t.get("resolved_at") is not None and start <= t["resolved_at"] < end
                                    for t in tickets)})
    return out


# --------------------------------------------------------------------------- #
# needs attention
# --------------------------------------------------------------------------- #
def needs_attention(days, open_tickets, statuses):
    """`days` is [(date, cap-day rows)], oldest first. One entry per cap that raised a
    bad flag on any of those days, or warn flags on ATTENTION_REPEAT_DAYS of them, with
    its flags merged (the latest day's wording wins), its camera on the latest such
    day, and the days it was flagged. Measured on 2026-10-12: 55 caps raised something
    in three days, 22 of them a single warn-only day (a burst of reboots or camera
    drops), and those drowned out the 7 caps that were bad on all three."""
    caps = {}
    for day, rows in days:
        for r in rows:
            worrying = [f for f in r["flags"]
                        if f["severity"] in ("bad", "warn") and f["code"] not in NOT_A_REPAIR]
            if not worrying:
                continue
            e = caps.setdefault(r["cap"], {"cameras": set(), "flags": {}, "days": []})
            e["camera"] = r["camera"]
            e["cameras"].update(r["cameras"])
            e["flags"].update((f["code"], f) for f in worrying)
            e["days"].append(day)
    out = []
    for cap, e in caps.items():
        note = statuses.get(("cap", cap))
        if open_tickets.on(cap, e["cameras"]) or (note and note["status"] in PARKED):
            continue
        flags = sorted(e["flags"].values(), key=lambda f: list(rules.FLAGS).index(f["code"]))
        if flags[0]["severity"] != "bad" and len(set(e["days"])) < config.ATTENTION_REPEAT_DAYS:
            continue                    # a one-day blip: the Day page shows it, the list doesn't
        out.append({"cap": cap, "camera": e["camera"], "severity": flags[0]["severity"], "flags": flags,
                    "days": sorted(set(e["days"]))})
    return sorted(out, key=lambda e: (rules.severity_rank(e["severity"]), -len(e["days"]),
                                      calib.natural_key(e["cap"])))


# --------------------------------------------------------------------------- #
# my notes
# --------------------------------------------------------------------------- #
def note_row(n):
    return {"id": n["id"], "subject_type": n["subject_type"], "subject_id": n["subject_id"],
            "created_at": n["created_at"], "text": n.get("text"), "status": n.get("status")}


def current_statuses(notes):
    """{(subject_type, subject_id): the note that set its current status}. `notes` come
    newest first, as the store gives them; a unit whose newest status is 'ok' has none."""
    seen, out = set(), {}
    for n in notes:
        key = (n["subject_type"], n["subject_id"])
        if n.get("status") is None or key in seen:
            continue
        seen.add(key)
        if n["status"] != "ok":
            out[key] = n
    return out


# --------------------------------------------------------------------------- #
# in maintenance: what is actually going on with each unit
# --------------------------------------------------------------------------- #
VERDICT_RANK = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
QUIET_DAYS = 7                  # powered on after the ticket, then nothing for this long = stalled


def in_shop(tickets, now, heard_at, cap_cpu, items, heard, qc_by_cpu, statuses, trips, blocked, broken):
    """Every unit in maintenance (an open ticket, blocked from check-out, or a camera
    marked broken), with its tickets and what the data says it has done since:

    - cap_cpu {cap name: cpu_id}; items: check-outs (any age); heard {cpu_id: newest
      heartbeat}, online is judged at `heard_at`; qc_by_cpu: qc.by_cpu;
    - trips {cap: its last field day before the ticket, {"day", "checkout_at", "hours",
      "good_pct", "flags"}};
    - statuses: current_statuses.

    `verdict` is the one line that answers "what's up with it", worst first: out in the
    field anyway, went out since, failed / passed QC since, powered on since (someone is
    on it), or untouched since the ticket."""
    units = defaultdict(list)
    for t in tickets:
        if is_open(t) and unit(t) is not None:
            units[(subject(t), str(unit(t)))].append(t)
    for cap in blocked:
        units.setdefault(("starcap", str(cap)), [])
    for cam in broken:
        if not any(s == "camera" and calib.base_of(u) == str(cam) for s, u in units):
            units.setdefault(("camera", str(cam)), [])
    name_of = {cpu: name for name, cpu in cap_cpu.items()}
    rows = []
    for (subj, u), ts in units.items():
        ts = sorted(ts, key=lambda t: t["created_at"])
        since = ts[0]["created_at"] if ts else None
        row = {"subject": "camera" if subj == "camera" else "cap", "unit": u,
               "critical": any(t.get("critical") for t in ts), "blocked": subj == "starcap" and u in blocked,
               "broken": subj == "camera" and calib.base_of(u) in set(map(str, broken)),
               "since_at": since, "days": round((now - since) / clock.DAY_S, 1) if since else None,
               "tickets": [{"category": t.get("category"), "description": t.get("description"),
                            "reporter": t.get("reporter_name"), "critical": bool(t.get("critical")),
                            "photos": t.get("photos") or 0, "created_at": t["created_at"]} for t in ts]}
        if subj == "camera":
            out = [i for i in items if i.get("returned_at") is None and i.get("camera_id") is not None
                   and calib.base_of(str(i["camera_id"])) == calib.base_of(u)]
            row["on_cap"] = name_of.get(out[0]["cpu_id"]) if out else next((t.get("headset") for t in reversed(ts)), None)
            row["out_now"] = bool(out)
            row["verdict"] = (_v("bad", "In the field now", f"out on {row['on_cap']}") if out else
                              _v("info", "In the office", "marked broken" if row["broken"] else None))
        else:
            cpu = cap_cpu.get(u)
            mine = [i for i in items if cpu is not None and i["cpu_id"] == cpu]
            last_heard = heard.get(cpu)
            qc_since = next((r for r in qc_by_cpu.get(cpu, []) if since is None or r["finished_at"] >= since), None)
            row.update({
                "out_now": any(i.get("returned_at") is None for i in mine),
                "outs_since": sum(1 for i in mine if since is not None and i["checkout_at"] >= since),
                "last_heard": last_heard,
                "online": last_heard is not None and heard_at - last_heard < config.ONLINE_S,
                "qc_since": {"verdict": qc_since["verdict"], "at": qc_since["finished_at"],
                             "failed_steps": list(qc_since.get("failed_steps") or [])} if qc_since else None,
                "last_trip": trips.get(u),
                "camera": next((t.get("camera") for t in reversed(ts) if t.get("camera")), None),
            })
            row["verdict"] = _cap_verdict(row, since, now)
        my = statuses.get((row["subject"], u))
        row["my_status"] = {"status": my["status"], "text": my.get("text"), "since": my["created_at"]} if my else None
        rows.append(row)
    return sorted(rows, key=lambda r: (VERDICT_RANK[r["verdict"]["severity"]], not r["critical"],
                                       r["since_at"] if r["since_at"] is not None else now,
                                       calib.natural_key(r["unit"])))


def _v(severity, text, detail=None):
    return {"severity": severity, "text": text, "detail": detail}


def _cap_verdict(row, since, now):
    q = row["qc_since"]
    if row["out_now"]:
        return _v("bad", "Out in the field now", "checked out with the ticket still open")
    if row["outs_since"]:
        return _v("warn", f"Went out {row['outs_since']}× since the ticket", "it was used, so is it fixed?")
    if q and q["verdict"] == "fail":
        return _v("bad", "Failed QC since the ticket", ", ".join(q["failed_steps"]) or None)
    if q and q["verdict"] == "pass":
        return _v("ok", "Passed QC since the ticket", "fixed? close the ticket")
    if row["online"]:
        return _v("info", "Online now", "powered on, likely on the bench")
    if since is not None and row["last_heard"] is not None and row["last_heard"] >= since:
        quiet = (now - row["last_heard"]) / clock.DAY_S
        if quiet >= QUIET_DAYS:
            return _v("warn", f"Quiet for {int(quiet)} d", "was powered on after the ticket, nothing since")
        return _v("info", "Powered on since the ticket", "someone has had it on")
    if since is None:
        return _v("info", "Blocked, no open ticket", None)
    if now - since < 2 * clock.DAY_S:
        return _v("info", "Just reported", None)
    return _v("warn", "Untouched since the ticket", "not powered on since it was reported"
              if row["last_heard"] is not None else "never heard from")


def status_list(statuses):
    rows = [{"subject_type": k[0], "subject_id": k[1], "status": n["status"], "since": n["created_at"],
             "text": n.get("text")} for k, n in statuses.items()]
    return sorted(rows, key=lambda r: -r["since"])


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
def view(day, now, tickets, days, statuses, notes_recent, blocked, broken, shop=None):
    """GET /api/maintenance. `days` are the needs-attention field days (see
    needs_attention); `notes_recent` are store notes, newest first."""
    end = clock.day_bounds(day)[1]
    since = clock.day_bounds(clock.shift_day(day, -29))[0]
    trend_days = clock.days_between(clock.shift_day(day, 1 - config.TREND_DAYS), day)
    return {
        "date": day,
        "stats": stats(tickets, day, now, blocked, broken),
        "by_category": by_category(tickets, since, end),
        "open": open_list(tickets, now),
        "in_shop": shop or [],
        "recent_resolved": recent_resolved(tickets, end, now),
        "needs_attention": needs_attention(days, OpenTickets(tickets), statuses),
        "repeat": repeat_offenders(tickets, since, end),
        "blocked": sorted(set(blocked), key=calib.natural_key),
        "broken_cams": sorted(set(map(str, broken)), key=calib.natural_key),
        "my_status": status_list(statuses),
        "notes_recent": [note_row(n) for n in notes_recent],
        "links": {"tickets": config.DASHBOARD_URL + "/tickets", "file": config.DASHBOARD_URL + "/maintenance"},
        "trend": trend(tickets, trend_days),
    }
