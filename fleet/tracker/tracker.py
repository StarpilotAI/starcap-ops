#!/usr/bin/env python3
"""
StarCap Tracker: how the OpenAI StarCap fleet did yesterday, on one page.

    python3 tracker.py                    ->  http://127.0.0.1:8774  (run.sh opens the browser)
    python3 tracker.py --sync-once        pull from production once, print a table, exit
    python3 tracker.py --demo             serve the page on tests/fixtures/api, no database
    python3 tracker.py --summary [DATE]   print the morning summary (add --write to append it)
    python3 tracker.py --rebuild          empty the cache (keeps your notes), pull everything again

This file is the web server and the two background loops. Everything else lives in
its own module (SPEC.md has the map): sync.py pulls the dashboard's Postgres and
Redis into a local SQLite cache every SYNC_EVERY_S, api.py turns that cache into the
page's JSON, and web/ draws it.

Production is only ever READ. sources.py runs every SQL statement inside a read-only
transaction and sends Redis nothing outside a read allowlist, so this server can be
left running all day without any way to change the dashboard. The only files it
writes are its own cache (data/) and, once a morning, the Slack drafts file.
"""
import argparse
import json
import mimetypes
import signal
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import api
import clock
import config
import sync
from store import Store

APP_ID = "starcap-tracker"
STATIC = {"index.html", "app.css", "app.js", "charts.js", "icon.svg"}


# --------------------------------------------------------------------------- #
# background loops
# --------------------------------------------------------------------------- #
class Loops:
    """The sync loop and the morning-summary loop.

    Sync runs at start-up and then every SYNC_EVERY_S; POST /api/sync just wakes it
    early, so two syncs never overlap. The summary loop looks once a minute: after
    SUMMARY_AT it writes yesterday's summary into the Slack drafts file, once per day,
    but only from data synced this morning (a laptop that slept all night would
    otherwise summarise a day whose footage it never saw arrive)."""

    SUMMARY_FRESH_S = 30 * 60
    SUMMARY_NEEDS = ("items", "takes", "redis_small")   # check-outs, takes, tickets
    RESYNC_S = 5 * 60

    def __init__(self, store, syncer):
        self.store = store
        self.syncer = syncer
        self.next_at = None
        self._wake = threading.Event()
        self._stop = threading.Event()

    def start(self):
        for target in (self._sync_loop, self._summary_loop):
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def request_sync(self):
        if self.syncer.running:
            return False
        self._wake.set()
        return True

    def state(self):
        return {"running": self.syncer.running, "next_at": self.next_at}

    def _sync_loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.syncer.run_once()
            except Exception as e:              # a bug in one run must not end the loop
                log(f"sync crashed: {e}")
            self.next_at = clock.now() + config.SYNC_EVERY_S
            self._wake.wait(config.SYNC_EVERY_S)
        self.next_at = None

    def _summary_loop(self):
        while not self._stop.wait(60):
            try:
                self._maybe_write_summary()
            except Exception as e:
                log(f"morning summary failed: {e}")

    def _maybe_write_summary(self, now=None):
        """Write yesterday's summary if it's past SUMMARY_AT, it isn't written yet, and the
        data is fresh. Returns what it did (for the tests): None, "sync" or the date."""
        now = clock.now() if now is None else now
        if datetime.fromtimestamp(now, config.TZ).strftime("%H:%M") < config.SUMMARY_AT:
            return None
        day = clock.shift_day(clock.today(now), -1)
        if self.store.summary_get(day):
            return None
        sync_ = self.store.kv_get("sync") or {}
        if not self._fresh(sync_, now):
            # Ask for one pass (a laptop that slept would otherwise wait out the rest of
            # its 10 minutes), but not every minute: if sync is failing, more passes only
            # spend Upstash requests, and the page's sync chip already shows the error.
            if now - (sync_.get("last_run_at") or 0) > self.RESYNC_S and self.request_sync():
                return "sync"
            return None
        api.write_summary(self.store, {"date": day})
        log(f"morning summary for {day} added to {config.SLACK_FILE}")
        notify("StarCap daily summary", f"Yesterday's summary is in {config.SLACK_FILE.name}")
        return day

    def _fresh(self, sync_, now):
        """The steps the summary stands on all succeeded within SUMMARY_FRESH_S. Not
        last_ok_at: one Upstash hiccup in the device logs must not hold the summary back."""
        src = sync_.get("sources") or {}
        return all((src.get(k) or {}).get("ok") and now - src[k]["at"] <= self.SUMMARY_FRESH_S
                   for k in self.SUMMARY_NEEDS)


def notify(title, body):
    try:
        subprocess.run(["notify-send", "-a", "StarCap Tracker", title, body],
                       timeout=5, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def log(msg):
    print(f"[{datetime.now(config.TZ):%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _q(query, name):
    vals = query.get(name)
    return vals[0] if vals else None


class Handler(BaseHTTPRequestHandler):
    server_version = "StarCapTracker"
    store = None        # set in main()
    loops = None
    demo = False

    # -- plumbing ---------------------------------------------------------- #
    def log_message(self, fmt, *args):    # one line per API error only, not per request
        pass

    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _file(self, path):
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise api.BadRequest("request too large")
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            raise api.BadRequest("body is not JSON")
        if not isinstance(body, dict):
            raise api.BadRequest("body must be a JSON object")
        return body

    def _api(self, fn):
        try:
            self._json(fn())
        except api.BadRequest as e:
            self._json({"error": str(e)}, 400)
        except api.NotFound as e:
            self._json({"error": str(e)}, 404)
        except api.Conflict as e:
            self._json({"error": str(e)}, 409)
        except Exception as e:
            log(f"{self.command} {self.path}: {type(e).__name__}: {e}")
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # -- routes ------------------------------------------------------------- #
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self._file(config.WEB_DIR / "index.html")
        if path.lstrip("/") in STATIC:
            return self._file(config.WEB_DIR / path.lstrip("/"))
        if path == "/api/ping":
            return self._json({"app": APP_ID, "demo": self.demo})
        if path.startswith("/demo/"):
            name = path[len("/demo/"):]
            if not self.demo or "/" in name or not name.endswith(".json"):
                return self._json({"error": "not found"}, 404)
            return self._file(config.DEMO_DIR / name)
        if self.demo:
            return self._json({"error": "demo mode: the page reads /demo/*.json"}, 404)

        s = self.store
        routes = {
            "/api/status": lambda: api.status(s, self.loops.state()),
            "/api/day": lambda: api.day(s, _q(q, "date")),
            "/api/day/cap": lambda: api.day_cap(s, _q(q, "date"), _q(q, "cap")),
            "/api/calibration": lambda: api.calibration(s, _q(q, "date")),
            "/api/maintenance": lambda: api.maintenance(s, _q(q, "date")),
            "/api/field": lambda: api.field(s),
            "/api/pipeline": lambda: api.pipeline(s),
            "/api/trends": lambda: api.trends(s, _int(_q(q, "days"), config.TREND_DAYS)),
            "/api/notes": lambda: api.notes(s, _q(q, "subject_type"), _q(q, "subject_id")),
            "/api/summary": lambda: api.summary(s, _q(q, "date")),
            "/api/units": lambda: api.units(s),
        }
        if path in routes:
            return self._api(routes[path])
        for prefix, fn in (("/api/cap/", api.cap), ("/api/camera/", api.camera)):
            if path.startswith(prefix) and len(path) > len(prefix):
                unit = path[len(prefix):]
                return self._api(lambda: fn(s, unit))
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if self.demo:
            return self._json({"error": "demo mode is read-only"}, 403)
        s = self.store
        if path == "/api/sync":
            started = self.loops.request_sync()
            return self._json({"started": started, "running": self.loops.syncer.running})
        if path == "/api/notes":
            return self._api(lambda: api.add_note(s, self._body()))
        if path.startswith("/api/notes/") and path.endswith("/delete"):
            note_id = path[len("/api/notes/"):-len("/delete")]
            return self._api(lambda: api.delete_note(s, _int(note_id, None)))
        if path == "/api/summary/write":
            return self._api(lambda: api.write_summary(s, self._body()))
        self._json({"error": "not found"}, 404)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        if default is None:
            raise api.BadRequest(f"not a number: {value!r}")
        return default


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def already_running(port):
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == APP_ID
    except Exception:
        return False


def serve(args):
    if already_running(args.port):
        print(f"StarCap Tracker is already running on http://{config.HOST}:{args.port}")
        return 0
    Handler.demo = args.demo
    loops = None
    if not args.demo:
        store = Store(args.db)
        loops = Loops(store, sync.Syncer(store))
        Handler.store, Handler.loops = store, loops
        loops.start()
    srv = ThreadingHTTPServer((config.HOST, args.port), Handler)
    srv.daemon_threads = True

    def shutdown(_sig, _frame):
        if loops:
            loops.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    mode = " (demo: fixtures, no database)" if args.demo else ""
    log(f"StarCap Tracker on http://{config.HOST}:{args.port}{mode}")
    srv.serve_forever()
    return 0


def sync_once(args):
    # --rebuild empties the cache inside the sync's own locks (notes survive), so it
    # can't wipe tables under the service's pass; sync.main is the same as this.
    return sync.main(["--db", args.db] + (["--full"] if args.full else [])
                     + (["--rebuild"] if args.rebuild else []))


def summary_cmd(args):
    store = Store(args.db)
    day = args.summary if args.summary != "yesterday" else clock.shift_day(clock.today(), -1)
    if args.write:
        res = api.write_summary(store, {"date": day, "force": args.force})
        print(f"appended to {res['path']}")
    else:
        print(api.summary(store, day)["text"])
    return 0


def main():
    ap = argparse.ArgumentParser(description="StarCap Tracker")
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--db", default=str(config.DB_PATH), help="local cache (default data/tracker.db)")
    ap.add_argument("--demo", action="store_true", help="serve the page on the test fixtures")
    ap.add_argument("--sync-once", action="store_true", help="pull once, print a table, exit")
    ap.add_argument("--full", action="store_true", help="with --sync-once: re-pull all history")
    ap.add_argument("--rebuild", action="store_true",
                    help="empty the cache (keeps your notes) and pull everything again")
    ap.add_argument("--summary", nargs="?", const="yesterday", metavar="DATE",
                    help="print the morning summary for DATE (default yesterday)")
    ap.add_argument("--write", action="store_true", help="with --summary: append it to the Slack drafts file")
    ap.add_argument("--force", action="store_true", help="with --write: even if that day was written")
    args = ap.parse_args()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if args.sync_once or args.rebuild:
            return sync_once(args)
        if args.summary:
            return summary_cmd(args)
        return serve(args)
    except api.Conflict as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
