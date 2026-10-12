"""
Cairo calendar days and timestamps.

Everything stored is a float epoch second (UTC). Everything shown is a Cairo
calendar day ("2026-10-11") or a Cairo-local ISO string with its offset. Egypt
moves between +02:00 and +03:00, so never add a fixed offset: go through TZ.
"""
import time
from datetime import date, datetime, timedelta

from config import TZ

DAY_S = 86400.0


def now():
    return time.time()


def cairo_date(ts):
    """'YYYY-MM-DD' of the Cairo day an epoch second falls in."""
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


def today(at=None):
    return cairo_date(now() if at is None else at)


def shift_day(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def day_bounds(day):
    """[start, end) of a Cairo day as epoch seconds. Not always 86400 s apart (DST)."""
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    nxt = d + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ)
    return start.timestamp(), end.timestamp()


def days_between(first, last):
    """Every day from first to last, inclusive, oldest first."""
    out, d, stop = [], date.fromisoformat(first), date.fromisoformat(last)
    while d <= stop:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def iso(ts):
    """Cairo-local ISO 8601 with offset, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def weekday(day):
    return date.fromisoformat(day).strftime("%a")


def parse_ts(value):
    """Epoch seconds from an ISO string ('...+0300', '...Z'), epoch ms, or epoch s."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    s = str(value).strip()
    if s.isdigit():
        return parse_ts(int(s))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()
