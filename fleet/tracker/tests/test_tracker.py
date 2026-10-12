"""Offline tests for tracker.py: the morning-summary loop and the HTTP routes.

    cd fleet/tracker && python3 -m unittest discover -s tests -v

The server is started for real on a free localhost port, on the test_api world (a
FakeStore), so routing, status codes and static-file handling are what a browser gets.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fakestore import at
from test_api import NOW, YDAY, world

import clock
import config
import tracker


class FakeSyncer:
    running = False

    def __init__(self):
        self.runs = 0

    def run_once(self, full=False):
        self.runs += 1


def synced(at_, ok=True):
    """A kv `sync` record whose summary steps last ran at `at_`."""
    src = {k: {"ok": ok, "at": at_, "rows": 1, "took_s": 1.0, "error": None} for k in tracker.Loops.SUMMARY_NEEDS}
    return {"last_run_at": at_, "last_ok_at": at_ if ok else None, "last_error": None, "sources": src}


class MorningSummary(unittest.TestCase):
    """Waleed's drafts file is never touched: SLACK_FILE points into a temp dir."""

    def setUp(self):
        self.store = world()
        self.loops = tracker.Loops(self.store, FakeSyncer())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.slack = Path(tmp.name) / "slack messages.md"
        patches = [mock.patch.object(config, "SLACK_FILE", self.slack),
                   mock.patch.object(tracker, "notify", lambda *_: None),
                   mock.patch.object(tracker, "log", lambda *_: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_at(self, hour, sync_at=None, ok=True):
        now = at("2026-10-12", hour)
        if sync_at is not None:
            self.store.kv_set("sync", synced(sync_at, ok))
        with mock.patch.object(clock, "now", return_value=now):
            return self.loops._maybe_write_summary(now)

    def test_waits_for_summary_time(self):
        self.assertIsNone(self.run_at(8.9, sync_at=at("2026-10-12", 8.8)))
        self.assertFalse(self.slack.exists())

    def test_writes_once_from_fresh_data(self):
        self.assertEqual(self.run_at(9.1, sync_at=at("2026-10-12", 9.0)), YDAY)
        self.assertIn("starcap daily", self.slack.read_text())
        size = self.slack.stat().st_size
        self.assertIsNone(self.run_at(9.2, sync_at=at("2026-10-12", 9.15)))            # already written
        self.assertEqual(self.slack.stat().st_size, size)

    def test_stale_data_asks_for_one_sync_not_one_a_minute(self):
        self.assertEqual(self.run_at(9.5, sync_at=at("2026-10-12", 6)), "sync")       # slept overnight
        self.loops._wake.clear()
        self.store.kv_set("sync", dict(synced(at("2026-10-12", 6)), last_run_at=at("2026-10-12", 9.49)))
        with mock.patch.object(clock, "now", return_value=at("2026-10-12", 9.52)):
            self.assertIsNone(self.loops._maybe_write_summary(at("2026-10-12", 9.52)))  # a pass just ran
        self.assertFalse(self.slack.exists())

    def test_a_failing_step_it_needs_holds_the_summary(self):
        self.assertNotEqual(self.run_at(9.5, sync_at=at("2026-10-12", 9.4), ok=False), YDAY)
        self.assertFalse(self.slack.exists())


class Http(unittest.TestCase):
    """Real HTTP against a server on a free port."""

    demo = False

    def setUp(self):
        tracker.Handler.store, tracker.Handler.demo = world(), self.demo
        tracker.Handler.loops = tracker.Loops(tracker.Handler.store, FakeSyncer())
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), tracker.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        p = mock.patch.object(clock, "now", return_value=NOW)
        p.start()
        self.addCleanup(p.stop)

    def get(self, path, data=None, raw=None):
        req = urllib.request.Request(self.base + path, data=raw if raw is not None else
                                     (json.dumps(data).encode() if data is not None else None),
                                     method="POST" if data is not None or raw is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()


class Routes(Http):
    def test_page_and_static_files(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css"),
                            ("/charts.js", "javascript"), ("/icon.svg", "image/svg+xml")):
            status, got, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, got, path)
            self.assertTrue(body, path)

    def test_nothing_outside_web_is_served(self):
        for path in ("/../config.py", "/web/../config.py", "/config.py", "/%2e%2e/config.py", "/data/tracker.db",
                     "/demo/../config.py", "/demo/day.json"):                     # demo is off here
            self.assertEqual(self.get(path)[0], 404, path)

    def test_api_answers_json(self):
        for path in ("/api/ping", "/api/status", "/api/day", "/api/day?date=2026-10-10", "/api/calibration",
                     "/api/maintenance", "/api/field", "/api/pipeline", "/api/trends?days=3", "/api/units",
                     "/api/cap/starcap1", "/api/camera/43", "/api/notes", "/api/summary"):
            status, ctype, body = self.get(path)
            self.assertEqual(status, 200, (path, body[:200]))
            self.assertIn("application/json", ctype)
            json.loads(body)

    def test_bad_input_is_a_4xx_not_a_500(self):
        cases = {"/api/day?date=yesterday": 400, "/api/day?date=2099-01-01": 400, "/api/trends?days=x": 200,
                 "/api/cap/starcap999": 404, "/api/camera/nope": 404, "/api/nope": 404}
        for path, code in cases.items():
            self.assertEqual(self.get(path)[0], code, path)
        self.assertEqual(self.get("/api/notes", raw=b"{not json")[0], 400)
        self.assertEqual(self.get("/api/notes", data=["a list"])[0], 400)
        self.assertEqual(self.get("/api/notes", data={"subject_type": "cap", "subject_id": "starcap1",
                                                      "text": "x", "status": "exploded"})[0], 400)
        self.assertEqual(self.get("/api/notes/abc/delete", data={})[0], 400)

    def test_sync_button_wakes_the_loop(self):
        status, _, body = self.get("/api/sync", data={})
        self.assertEqual((status, json.loads(body)["started"]), (200, True))
        self.assertTrue(tracker.Handler.loops._wake.is_set())

    def test_notes_round_trip(self):
        status, _, body = self.get("/api/notes", data={"subject_type": "cap", "subject_id": "starcap1",
                                                       "text": "sent to repair", "status": "repair"})
        self.assertEqual(status, 200)
        note = json.loads(body)
        listed = json.loads(self.get("/api/notes?subject_type=cap&subject_id=starcap1")[2])["notes"]
        self.assertEqual([n["id"] for n in listed], [note["id"]])
        self.assertEqual(self.get(f"/api/notes/{note['id']}/delete", data={})[0], 200)
        self.assertEqual(json.loads(self.get("/api/notes?subject_type=cap&subject_id=starcap1")[2])["notes"], [])


class Demo(Http):
    demo = True

    def test_fixtures_are_served_and_the_api_is_not(self):
        status, ctype, body = self.get("/demo/day.json")
        self.assertEqual(status, 200)
        self.assertIn("date", json.loads(body))
        self.assertEqual(self.get("/api/day")[0], 404)
        self.assertEqual(self.get("/api/sync", data={})[0], 403)                # demo is read-only
        self.assertEqual(self.get("/api/ping")[0], 200)


if __name__ == "__main__":
    unittest.main()
