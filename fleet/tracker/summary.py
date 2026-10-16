"""
summary.py: the morning Slack summary, appended to the drafts file to edit and post.

One short Markdown block per day in Waleed's own voice: lower-case bullets, numbers
first, nothing that doesn't need saying. A problem gets a line only when it exists,
and a clean pipeline gets a single "pipeline: all good". A list of caps or cameras
names at most three, then "+N", so a bad day still fits on a phone screen.

text() is pure: the views api.py built go in, a string comes out. write() APPENDS to
SLACK_FILE and never rewrites it, because that file holds every draft Waleed has
kept, and records the date so each day goes in once.
"""
from datetime import date as Date

import clock
import config
import pipeline
import rules

MAX_NAMES = 3


class AlreadyWritten(Exception):
    """That day's summary is already in the drafts file."""


def day_name(day):
    """'2026-10-11' -> 'sun 11 oct'."""
    d = Date.fromisoformat(day)
    return f"{d.strftime('%a').lower()} {d.day} {d.strftime('%b').lower()}"


def names(items):
    """At most MAX_NAMES names, then '+N' for the rest."""
    items = list(items)
    more = len(items) - MAX_NAMES
    return ", ".join(items[:MAX_NAMES]) + (f" +{more}" if more > 0 else "")


def hours(h):
    """587.2 -> '587', 9.04 -> '9', 2.34 -> '2.3'."""
    if h >= 10:
        return f"{h:.0f}"
    s = f"{h:.1f}"
    return s[:-2] if s.endswith(".0") else s


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _kpis(day):
    return {k: v["value"] for k, v in day["kpis"].items()}


def _settled(day):
    a = day["arrival"]
    return not (a["still_out"] or a["waiting"])


# --------------------------------------------------------------------------- #
# lines
# --------------------------------------------------------------------------- #
def header(day):
    line = f"**starcap daily, {day_name(day['date'])}**"
    return line if _settled(day) else line + f" _({day['arrival']['pct']}% of footage in so far)_"


def day_lines(day):
    k, rows = _kpis(day), day["caps"]
    if not rows:
        return ["- no caps out"]
    nothing = [r["cap"] for r in rows if r["status"] in rules.NOTHING]
    unused = [r["cap"] for r in rows if r["status"] in rules.NOTHING and (r.get("use") or {}).get("verdict") == "unused"]
    out = [f"- {plural(k['caps_out'], 'cap')} out, {k['caps_recorded']} recorded"
           + (f", {len(nothing)} got nothing ({names(nothing)})" if nothing else "")
           + (f", {len(unused)} of them never switched on ({names(unused)})" if unused else "")]
    if k["hours_total"] > 0:
        out.append(f"- {hours(k['hours_total'])} h recorded, {hours(k['hours_good'])} h good ({k['good_pct']:.0f}%)"
                   + (f", timing pass {k['timing_pass_pct']:.1f}%" if k["timing_pass_pct"] is not None else ""))
    fps30 = sorted((r for r in rows if r["hours"]["fps30"] > 0), key=lambda r: -r["hours"]["fps30"])
    if fps30:                           # the same caps as the Day page's fps30_caps, worst first
        out.append(f"- 30 fps on {plural(len(fps30), 'cap')} ({hours(k['fps30_hours'])} h): "
                   + names(r["cap"] for r in fps30))
    if k["expired_cal_hours"] >= config.EXPIRED_CAL_MIN_H:
        cams = []
        for r in sorted(rows, key=lambda r: -r["expired_cal_hours"]):
            if r["expired_cal_hours"] > 0 and r["camera"] and r["camera"] not in cams:
                cams.append(r["camera"])
        out.append(f"- {hours(k['expired_cal_hours'])} h shot on cameras past calibration ({names(cams)})")
    return out


def before_line(before):
    k = _kpis(before)
    if not k["caps_out"]:
        return None
    state = "final" if _settled(before) else f"so far ({before['arrival']['pct']}% in)"
    return (f"- {day_name(before['date'])} {state}: {plural(k['caps_out'], 'cap')}, {hours(k['hours_total'])} h, "
            f"{hours(k['hours_good'])} h good")


def calib_line(cal, day, today):
    when = {clock.shift_day(today, -1): "yesterday", today: "today"}.get(day, day_name(day))
    parts = [f"{cal['today']['done']}/{cal['target']} {when}"]
    if cal["counts"]["overdue"]:
        clear = cal.get("clear_by")
        parts.append(f"{cal['counts']['overdue']} overdue"
                     + (f" (caught up by {day_name(clear)})" if clear and clear != today else ""))
    if cal.get("behind_from"):
        parts.append(f"falls behind {day_name(cal['behind_from'])}")
    if cal["spikes"]:
        spike = max(cal["spikes"], key=lambda s: s["due"])          # the biggest; the earliest of equals
        parts.append(f"heads up {spike['due']} come due {day_name(spike['date'])}")
    return "- calib: " + ", ".join(parts)


def tickets_line(maintenance):
    s = maintenance["stats"]
    parts = []
    if s["opened"]:
        parts.append(f"{s['opened']} new")
    if s["resolved"]:
        parts.append(f"{s['resolved']} closed")
    if s["open"]:
        parts.append(f"{s['open']} open" + (f" ({s['critical']} critical)" if s["critical"] else ""))
    return "- tickets: " + ", ".join(parts) if parts else None


def field_line(field):
    """Names, not counts: each of these is a cap someone has to go and find."""
    parts = []
    if field["long_out"]:
        parts.append(f"out {config.LONG_TRIP_DAYS:g}d+: "
                     + names(f"{e['cap']} ({int(e['days_out'])}d)" for e in field["long_out"]))
    named = {e["cap"] for e in field["long_out"]}
    silent = [e["cap"] for e in field["silent"] if e["cap"] not in named]
    if silent:
        parts.append(f"silent {config.SILENT_H:g}h+: " + names(silent))
    return "- field: " + " · ".join(parts) if parts else None


def pipeline_line(view):
    issues = pipeline.problems(view)
    return "- pipeline: " + (", ".join(issues) if issues else "all good")


def text(views):
    """views: {"date", "today", "day", "before", "calibration", "maintenance", "field",
    "pipeline"}, each an api view of that name (day = the summarised date, before =
    the day before it)."""
    lines = [header(views["day"]), *day_lines(views["day"]), before_line(views["before"]),
             calib_line(views["calibration"], views["date"], views["today"]),
             tickets_line(views["maintenance"]), field_line(views["field"]), pipeline_line(views["pipeline"])]
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# the drafts file
# --------------------------------------------------------------------------- #
def write(store, date, text, force=False):
    """Append `text` to SLACK_FILE (after a --- separator when the file has content)
    and record it for `date`. -> {"ok", "path", "written_at"}. Raises AlreadyWritten
    when that date was written before, unless force."""
    if store.summary_get(date) and not force:
        raise AlreadyWritten(f"the summary for {date} is already in {config.SLACK_FILE.name}")
    path = config.SLACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    has_content = path.exists() and path.stat().st_size > 0
    with open(path, "a", encoding="utf-8") as f:
        f.write(("\n\n---\n\n" if has_content else "") + text)
    store.summary_put(date, text)
    return {"ok": True, "path": str(path), "written_at": store.summary_get(date)["written_at"]}
