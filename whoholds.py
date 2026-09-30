#!/usr/bin/env python3
"""Which starcap is / was carrying a given camera?

Reads the live custody log straight from Upstash (the same store /custody renders).

    ./whoholds.py 26            # camera 26's check-out history, newest first
    ./whoholds.py 26 --since 2026-09-15
    ./whoholds.py 26 --watch    # poll every 5 min until it is checked out again

Written 2026-09-15 to settle an open question: the hand-pairing sheet listed
`79...C26` under نانو while `79...C144` was already listed under اورنج, and only ONE
cpu_id is named starcap79 — so one of the two device numbers is a transcription slip.
Camera 26 is attached to nobody, so the next check-out that carries it names the
NanoPi it is really fitted to.
"""
import argparse, datetime, json, os, sys, time, urllib.request

ENV = os.path.expanduser("~/starcap-dashboard/dashboard/.env.local")
TZ = datetime.timezone(datetime.timedelta(hours=3))          # Cairo, = fleet_tz_offset 180


def creds():
    env = {}
    with open(ENV) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    return env["UPSTASH_REDIS_REST_URL"].rstrip("/"), env["UPSTASH_REDIS_REST_TOKEN"]


URL, TOK = creds()


def cmd(*args):
    req = urllib.request.Request(
        URL, data=json.dumps([str(a) for a in args]).encode(),
        headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())["result"]


def events(since_ms=0):
    ids = cmd("ZRANGE", "starcap:kamerat:events", "+inf", since_ms, "BYSCORE", "REV")
    out = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        for raw in cmd("MGET", *[f"starcap:kamerat:event:{x}" for x in chunk]):
            if raw:
                out.append(json.loads(raw) if isinstance(raw, str) else raw)
    out.sort(key=lambda e: -e.get("created_at", 0))
    return out


def rows(camera, since_ms=0):
    want = str(camera)
    for ev in events(since_ms):
        for it in ev.get("items") or []:
            if str(it.get("camera")) == want:
                yield ev, it
                break


def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def show(camera, since_ms, limit):
    found = list(rows(camera, since_ms))
    if not found:
        print(f"camera {camera}: no custody events in range")
        return None
    print(f"camera {camera} — {len(found)} custody events (Cairo time)\n")
    for ev, it in found[:limit]:
        who = ev.get("operator_name") or ev.get("name") or ""
        dest = it.get("destination") or ev.get("destination") or ""
        print(f"  {stamp(ev['created_at'])}  {ev.get('type'):8} {it.get('headset'):12} {who:24} {dest}")
    newest = found[0]
    return newest[1].get("headset"), newest[0].get("created_at")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("camera")
    p.add_argument("--since", help="YYYY-MM-DD (Cairo)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--watch", action="store_true", help="poll until a NEW event appears")
    p.add_argument("--every", type=int, default=300, help="seconds between polls (--watch)")
    a = p.parse_args()

    since = 0
    if a.since:
        y, m, d = (int(x) for x in a.since.split("-"))
        since = int(datetime.datetime(y, m, d, tzinfo=TZ).timestamp() * 1000)

    latest = show(a.camera, since, a.limit)
    if not a.watch:
        return
    baseline = latest[1] if latest else 0
    print(f"\nwatching camera {a.camera} — polling every {a.every}s, Ctrl-C to stop")
    while True:
        time.sleep(a.every)
        fresh = [(ev, it) for ev, it in rows(a.camera, baseline + 1)]
        if fresh:
            ev, it = fresh[0]
            print(f"\n*** camera {a.camera} {ev.get('type')} on {it.get('headset')} "
                  f"at {stamp(ev['created_at'])} by {ev.get('operator_name') or ''} ***")
            return


if __name__ == "__main__":
    sys.exit(main())
