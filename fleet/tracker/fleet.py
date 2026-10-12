"""
fleet.py: where the caps are right now, and which ones to chase.

A cap is out from its check-out until someone logs its return. Out in the field it is
usually offline (no WiFi, and only some carry a modem), so silence on its own means
little. What matters is how long it has been since we last heard from it, counted
from the check-out when it went quiet before it even left (a cap that last
heartbeated in the office a week ago has been silent for as long as it has been out,
not for a week).

"Online" is judged at `heard_at`, when sync last read the heartbeats: a heartbeat
newer than ONLINE_S (2 min) can only be seen right after a sync, never 8 minutes
into the 10 between two.

The operator's name appears here and nowhere else: it is shown so a missing cap can
be chased. Their phone number never reaches this tool.
"""
import re

import calib
import clock
import config

FLEET_NAME = re.compile(r"starcap\d+")     # bench and test boards (WALEEDTEST1, NANOPITEST_USBPOWER) aren't fleet


def last_heard(devices):
    """{cpu_id: newest heartbeat of any of its devices}."""
    out = {}
    for d in devices:
        if d.get("last_seen") is not None:
            out[d["cpu_id"]] = max(d["last_seen"], out.get(d["cpu_id"], d["last_seen"]))
    return out


def out_now(open_items, starcaps, devices, kamerat_out, lenses, now):
    """One entry per cap with an open check-out (its earliest, if it somehow has two),
    longest out first. days_out and silent_h are unrounded."""
    names = {r["cpu_id"]: r["name"] for r in starcaps}
    heard = last_heard(devices)
    first = {}
    for i in open_items:
        if i["cpu_id"] not in first or i["checkout_at"] < first[i["cpu_id"]]["checkout_at"]:
            first[i["cpu_id"]] = i
    out = []
    for cpu, i in first.items():
        cap = names.get(cpu) or cpu
        h = heard.get(cpu)
        quiet_since = i["checkout_at"] if h is None else max(h, i["checkout_at"])
        out.append({"cap": cap, "camera": lenses.key(i["camera_id"]) if i.get("camera_id") else None,
                    "checkout_at": i["checkout_at"], "days_out": (now - i["checkout_at"]) / clock.DAY_S,
                    "last_heard": h, "silent_h": (now - quiet_since) / 3600,
                    "operator": (kamerat_out.get(cap) or {}).get("operator_name"), "task": i.get("task")})
    return sorted(out, key=lambda e: (e["checkout_at"], calib.natural_key(e["cap"])))


def shown(e):
    return dict(e, days_out=round(e["days_out"], 1), silent_h=round(e["silent_h"], 2))


def snapshot(starcaps, devices, open_items, blocked, heard_at):
    """Fleet caps (named starcapN; sync already dropped the dragons), how many are out,
    online when the heartbeats were read, blocked by a critical ticket, and in the
    office (everything not out)."""
    named = {r["cpu_id"] for r in starcaps if FLEET_NAME.fullmatch(r.get("name") or "")}
    heard = last_heard(devices)
    out = named & {i["cpu_id"] for i in open_items}
    online = sum(1 for c in named if c in heard and heard_at - heard[c] < config.ONLINE_S)
    return {"total": len(named), "out": len(out), "office": len(named) - len(out), "online": online,
            "blocked": len(set(blocked))}


def view(now, heard_at, starcaps, devices, open_items, kamerat_out, blocked, lenses):
    """GET /api/field. Time out and silence run to `now`; online is as of `heard_at`."""
    out = out_now(open_items, starcaps, devices, kamerat_out, lenses, now)
    silent = sorted((e for e in out if e["silent_h"] > config.SILENT_H), key=lambda e: -e["silent_h"])
    return {"fleet": snapshot(starcaps, devices, open_items, blocked, heard_at),
            "long_out": [shown(e) for e in out if e["days_out"] > config.LONG_TRIP_DAYS],
            "silent": [shown(e) for e in silent],
            "out": [shown(e) for e in out]}
