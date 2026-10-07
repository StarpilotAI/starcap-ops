#!/usr/bin/env python3
"""
StarCap QC: dock a finished StarCap over ethernet, run the acceptance tests, and
name it once everything is green.

    python3 webui.py        ->  http://127.0.0.1:8772     (run.sh opens the browser)

This file is the web server, the dock scanner, the naming gate, the reports, and the
safety net for test takes. The tests themselves live in qc.py (what is run, in what
order, and when a board passes) and checks.py (the pure measurements), so they can
be exercised without a board.

Naming is opm-name's own code (../opm-name/opmname.py): the same dashboard endpoint,
the same duplicate-name check, the same "next free number" survey. This tool only
adds the rule that a board has to pass QC here before it gets a name here.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import board
import qc

HOST = "127.0.0.1"
PORT = int(os.environ.get("STARCAP_QC_PORT", "8772"))
HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
REPORTS = HERE / "reports"

# --------------------------------------------------------------------------- #
# dock scanner: which StarCaps are on this laptop's ethernet right now
# --------------------------------------------------------------------------- #
# A board shows up two ways. The recorder answers the UDP roll-call (fast, every
# pass), and anything that answers a ping on the dock link is swept up every
# SWEEP_EVERY_S, so a board whose recorder is down or crash-looping still appears,
# as "recorder not answering", instead of silently not being there.
SCAN_EVERY_S = 3.0
SWEEP_EVERY_S = 30.0

DEVICES = {}            # ip -> device dict (see board.describe)
IFACE = {"links": [], "down": []}
SCAN_LOCK = threading.Lock()
_stop = threading.Event()
_sweep_now = threading.Event()


def scanner_loop():
    last_sweep = 0.0
    pool = ThreadPoolExecutor(max_workers=16)
    while not _stop.is_set():
        try:
            links = board.wired_links()
            found = board.roll_call(links)
            if links and (time.time() - last_sweep > SWEEP_EVERY_S or _sweep_now.is_set()):
                _sweep_now.clear()
                last_sweep = time.time()
                for ip in board.ping_sweep(links):
                    found.setdefault(ip, {"ip": ip, "via": "sweep"})
            with SCAN_LOCK:
                known = dict(DEVICES)
            # Keep a board that was seen a moment ago: one missed roll-call reply is
            # not an unplug, and a card that blinks out mid-test is worse than one
            # that lingers a few seconds after the cable is pulled.
            for ip, d in known.items():
                if ip not in found and time.time() - d.get("seen", 0) < 12:
                    found[ip] = {"ip": ip, "via": d.get("via", "roll-call")}
            fresh = dict(zip(found, pool.map(lambda ip: _describe(ip, known.get(ip)), found)))
            with SCAN_LOCK:
                DEVICES.clear()
                DEVICES.update({ip: d for ip, d in fresh.items() if d})
                IFACE["links"] = [{"iface": i, "ip": own, "net": str(net)} for i, own, net in links]
                IFACE["down"] = [] if links else board.wired_status()
            recover_pending()
        except Exception as e:                       # noqa: BLE001  never let the scanner die
            print(f"[scan] {type(e).__name__}: {e}")
        _stop.wait(SCAN_EVERY_S)


def _describe(ip, previous):
    try:
        return board.describe(ip, previous)
    except Exception as e:                           # noqa: BLE001  one odd board must not sink the pass
        print(f"[scan] {ip}: {type(e).__name__}: {e}")
        return previous


def devices_snapshot():
    with SCAN_LOCK:
        return sorted((dict(d) for d in DEVICES.values()),
                      key=lambda d: (d.get("name") or "~", d["ip"]))


# --------------------------------------------------------------------------- #
# pending clean-ups: QC runs that may still have a test take on a board
# --------------------------------------------------------------------------- #
# A run saves its QC window (qc.py: boot id + monotonic span) the moment it could
# cause a take, and clears it once its clean-up is settled. If the app dies first (a
# crash, a logout, a kill) the record survives here, and the next time that board is
# on the dock its test takes are found by that window and removed. The window can
# only match takes recorded during that QC run, so the operator's own footage is
# never at risk, whenever the board comes back.
PENDING_FILE = REPORTS / ".pending_cleanup.json"
PENDING = {}            # cpu_id -> record (see QCRun._persist_window)
PENDING_STATE = {}      # cpu_id -> {"state": "running"|"waiting"|"failed", "message", "at"}
PENDING_LOCK = threading.Lock()
RECOVER_RETRY_S = 60.0


def _pending_load():
    try:
        data = json.loads(PENDING_FILE.read_text())
        if isinstance(data, dict):
            PENDING.update({k: v for k, v in data.items() if isinstance(v, dict) and v.get("window")})
    except (OSError, ValueError):
        pass


def _pending_save():
    REPORTS.mkdir(exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(PENDING, indent=2, sort_keys=True))
    tmp.replace(PENDING_FILE)


def on_pending(run, record):
    """qc.QCRun's hook: a record to keep, or None once nothing of QC's is left."""
    with PENDING_LOCK:
        if record is None:
            if PENDING.pop(run.cpu_id, None) is not None:
                _pending_save()
            PENDING_STATE.pop(run.cpu_id, None)
        else:
            PENDING[run.cpu_id] = record
            _pending_save()


def recover_pending():
    """Finish the clean-up of interrupted runs on boards that are on the dock now."""
    with SCAN_LOCK:
        docked = {d.get("cpu_id"): d for d in DEVICES.values() if d.get("cpu_id")}
    with PENDING_LOCK:
        todo = []
        for cpu, rec in PENDING.items():
            st = PENDING_STATE.get(cpu) or {}
            if cpu not in docked or st.get("state") == "running":
                continue
            if time.time() - st.get("at", 0) < RECOVER_RETRY_S and st:
                continue
            with RUNS_LOCK:
                run = RUNS.get(cpu)
            if run and run.active:
                continue                             # that run owns the board; it cleans up itself
            PENDING_STATE[cpu] = {"state": "running", "message": "removing the test take…", "at": time.time()}
            todo.append((cpu, dict(rec), docked[cpu]["ip"]))
    for cpu, rec, ip in todo:
        threading.Thread(target=_recover_one, args=(cpu, rec, ip), daemon=True,
                         name=f"recover-{ip}").start()


def _recover_one(cpu, rec, ip):
    # its logs go where the interrupted run's own report would have put them
    logs = REPORTS / (str(rec.get("stem") or f"recovered_{time.strftime('%Y%m%d-%H%M%S')}_{cpu[:12]}"))
    try:
        state, summary, settled = qc.remove_qc_takes(
            ip, rec["window"], int(rec.get("minutes") or 5),
            keep=lambda t: qc.save_take_logs(ip, t, logs / t["run"]))
    except Exception as e:                           # noqa: BLE001
        state, summary, settled = "fail", f"{type(e).__name__}: {e}", False
    with PENDING_LOCK:
        if settled:
            PENDING.pop(cpu, None)
            _pending_save()
            PENDING_STATE[cpu] = {"state": "done" if state != "fail" else "failed",
                                  "message": summary, "at": time.time()}
        else:
            PENDING_STATE[cpu] = {"state": "waiting", "message": summary, "at": time.time()}
    print(f"[recover] {rec.get('name') or cpu} ({ip}): {state}: {summary}")


def pending_snapshot():
    with PENDING_LOCK:
        out = []
        for cpu, rec in PENDING.items():
            st = PENDING_STATE.get(cpu) or {}
            out.append({"cpu_id": cpu, "name": rec.get("name"), "since": rec.get("since"),
                        "state": st.get("state") or "waiting for the board",
                        "message": st.get("message") or "dock this StarCap to remove its test take"})
        # settled ones stay listed briefly, so the result is seen
        for cpu, st in PENDING_STATE.items():
            if cpu not in PENDING and st.get("state") in ("done", "failed") and time.time() - st["at"] < 120:
                out.append({"cpu_id": cpu, "name": None, "since": None, "state": st["state"],
                            "message": st.get("message")})
        return out


# --------------------------------------------------------------------------- #
# QC runs: one per board, keyed by cpu_id so a board that changes IP is the same run
# --------------------------------------------------------------------------- #
RUNS = {}               # cpu_id -> qc.QCRun
RUNS_LOCK = threading.Lock()


def _device(ip):
    with SCAN_LOCK:
        d = DEVICES.get(ip)
        return dict(d) if d else None


def api_start(body):
    ip = str(body.get("ip") or "").strip()
    try:
        minutes = int(body.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes not in qc.SOAK_CHOICES_MIN:
        return {"ok": False, "error": f"soak length must be one of {qc.SOAK_CHOICES_MIN} minutes"}
    dev = _device(ip)
    if not dev:
        return {"ok": False, "error": "that board is no longer docked"}
    cpu = dev.get("cpu_id")
    if not cpu:
        return {"ok": False, "error": dev.get("error") or "couldn't identify the board yet; give it a few seconds"}
    with RUNS_LOCK:
        cur = RUNS.get(cpu)
        if cur and cur.active:
            return {"ok": False, "error": "a QC run is already going on this board"}
    with PENDING_LOCK:
        pending = cpu in PENDING
    if pending:
        # an earlier run's test take may still be on it: that clean-up goes first,
        # or this run would count that take as the operator's and keep it
        recover_pending()
        return {"ok": False, "error": "an earlier QC run on this board was interrupted, and its test take is "
                                      "being removed now. Start QC again in a minute"}
    with RUNS_LOCK:
        cur = RUNS.get(cpu)
        if cur and cur.active:
            return {"ok": False, "error": "a QC run is already going on this board"}
        run = qc.QCRun(dev, minutes, on_finish=_save_report, on_pending=on_pending,
                       stop_take_ok=bool(body.get("stop_take")), logs_root=REPORTS)
        RUNS[cpu] = run
        run.start()
    return {"ok": True, "run": run.to_dict()}


def api_cancel(body):
    cpu = str(body.get("cpu_id") or "")
    with RUNS_LOCK:
        run = RUNS.get(cpu)
    if not run or not run.active:
        return {"ok": False, "error": "no QC run in progress on that board"}
    run.cancel()
    return {"ok": True}


def api_dismiss(body):
    """Clear a finished run's card back to 'ready to test'."""
    cpu = str(body.get("cpu_id") or "")
    with RUNS_LOCK:
        run = RUNS.get(cpu)
        if run and not run.active:
            RUNS.pop(cpu, None)
    return {"ok": True}


def runs_snapshot():
    with RUNS_LOCK:
        runs = list(RUNS.items())
    return {cpu: r.to_dict() for cpu, r in runs}


# --------------------------------------------------------------------------- #
# reports (laptop only): one JSON per QC run, in ./reports/
# --------------------------------------------------------------------------- #
_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.json$")   # never a dotfile


def _write_json(path, obj):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)                                 # atomic: a crash can't leave half a report


def _save_report(run):
    REPORTS.mkdir(exist_ok=True)
    rep = run.report()
    _write_json(REPORTS / rep["report_id"], rep)
    run.report_file = rep["report_id"]


def _update_report(name, **fields):
    path = REPORTS / name
    try:
        rep = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    rep.update(fields)
    _write_json(path, rep)


def history(limit=60):
    """The newest QC reports, summarised for the history table."""
    if not REPORTS.is_dir():
        return []
    out = []
    paths = [p for p in REPORTS.glob("*.json") if _REPORT_ID.match(p.name)]
    for p in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        b = r.get("board") or {}
        out.append({"file": p.name, "finished": r.get("finished"), "verdict": r.get("verdict"),
                    "logs": (REPORTS / p.stem).is_dir(),
                    "name": r.get("named_as") or b.get("name"), "cpu_id": b.get("cpu_id"),
                    "camera_id": b.get("camera_id"), "minutes": r.get("soak_minutes"),
                    "headline": r.get("headline")})
    return out


def api_open_logs(body):
    """Open a run's logs folder in the file manager (this server only ever listens on
    127.0.0.1, so it is the operator's own desktop)."""
    name = str(body.get("file") or "")
    if not _REPORT_ID.match(name):
        return {"ok": False, "error": "no such report"}
    folder = (REPORTS / name).with_suffix("")
    if not folder.is_dir() or REPORTS.resolve() not in folder.resolve().parents:
        return {"ok": False, "error": "this run kept no logs (it never recorded)"}
    try:
        subprocess.Popen(["xdg-open", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"ok": False, "error": f"couldn't open a file manager ({e}); the logs are in {folder}"}
    return {"ok": True, "path": str(folder)}


# --------------------------------------------------------------------------- #
# naming: opm-name's logic, gated on a passed QC
# --------------------------------------------------------------------------- #
# The "next free number" survey is a dashboard read. It is refreshed by its own thread
# and the page only ever reads the cached copy: a slow or dead dashboard must never
# hold up the 1 s page poll (that would freeze the countdown and the Stop button).
_SURVEY = {"at": 0.0, "data": None, "error": None}
SURVEY_EVERY_S = 30.0
_survey_now = threading.Event()


def survey_loop():
    while not _stop.is_set():
        try:
            data = board.opmname.name_survey()
            _SURVEY.update(data=data, error=None, at=time.time())
        except Exception as e:                        # noqa: BLE001
            _SURVEY.update(error=str(e)[:200], at=time.time())
        _survey_now.wait(SURVEY_EVERY_S)
        _survey_now.clear()


def api_rename(body):
    cpu = str(body.get("cpu_id") or "").strip()
    name = str(body.get("name") or "").strip()
    if not cpu or not name:
        return {"ok": False, "error": "a board and a name are required"}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        return {"ok": False, "error": "use letters, digits, '-', '_' or '.' only (no spaces), e.g. starcap162"}
    with RUNS_LOCK:
        run = RUNS.get(cpu)
    if not run or run.active or run.verdict != "pass":
        return {"ok": False, "error": "this board has not passed QC; it can only be named after a green run"}
    if not body.get("force"):
        try:
            pre = board.opmname.check_rename(name, cpu)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "error": f"couldn't check the name against the dashboard: {e}"}
        if pre["taken_by"] or (pre["current_name"] and pre["current_name"].lower() != name.lower()):
            return {"ok": False, "needs_confirm": True, "name": name,
                    "taken_by": pre["taken_by"], "current_name": pre["current_name"]}
    try:
        res = board.opmname.set_authoritative_name(cpu, name)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": str(e)}
    stored = res.get("assigned_name") or name
    taken = res.get("taken_from") or []
    run.named_as = stored
    run.named_note = (f"That name was taken off {len(taken)} other board{'s' if len(taken) > 1 else ''}, "
                      "which is now unnamed." if taken else "")
    if run.report_file:
        _update_report(run.report_file, named_as=stored, named_at=time.time(),
                       previous_name=res.get("previous_name"), name_taken_from=taken)
    _survey_now.set()                                 # the next free number just moved
    return {"ok": True, "name": stored, "previous_name": res.get("previous_name"), "taken_from": taken}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def api_state():
    with SCAN_LOCK:
        iface = dict(IFACE)
    return {"devices": devices_snapshot(), "runs": runs_snapshot(), "dock": iface,
            "names": _SURVEY["data"], "soak_choices": qc.SOAK_CHOICES_MIN,
            "dashboard": {"url": board.opmname.DASHBOARD_URL,
                          "ok": _SURVEY["error"] is None, "error": _SURVEY["error"]},
            "pending": pending_snapshot(), "history": history()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, ctype, data, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", (WEB_DIR / "index.html").read_bytes())
            if path == "/icon.svg":
                return self._send(200, "image/svg+xml", (HERE / "icon.svg").read_bytes())
            if path == "/api/ping":
                return self._json({"app": "starcap-qc"})
            if path == "/api/state":
                return self._json(api_state())
            if path == "/api/frame":
                cpu = (parse_qs(url.query).get("cpu_id") or [""])[0]
                with RUNS_LOCK:
                    run = RUNS.get(cpu)
                jpg = run.frame_jpeg if run else None
                if not jpg:
                    return self._json({"error": "no frame"}, 404)
                return self._send(200, "image/jpeg", jpg)
            if path == "/api/report":
                name = (parse_qs(url.query).get("file") or [""])[0]
                if not _REPORT_ID.match(name) or not (REPORTS / name).is_file():
                    return self._json({"error": "no such report"}, 404)
                return self._send(200, "application/json", (REPORTS / name).read_bytes(),
                                  {"Content-Disposition": f'inline; filename="{name}"'})
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                        # noqa: BLE001  answer, never hang the page
            try:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        routes = {"/api/qc/start": api_start, "/api/qc/cancel": api_cancel,
                  "/api/qc/dismiss": api_dismiss, "/api/rename": api_rename,
                  "/api/open_logs": api_open_logs,
                  "/api/rescan": lambda _b: (_sweep_now.set(), {"ok": True})[1]}
        fn = routes.get(path)
        try:
            if not fn:
                return self._json({"error": "not found"}, 404)
            return self._json(fn(self._body()))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                        # noqa: BLE001  answer, never hang the page
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


SHUTDOWN_GRACE_S = 20.0


def _on_signal(signum, _frame):
    # SIGTERM (kill, logout) and SIGHUP (the session going away) unwind like Ctrl-C,
    # so active runs get to stop their takes; whatever doesn't finish is in the
    # pending-cleanup file and is completed the next time the board docks
    raise KeyboardInterrupt


def main():
    _pending_load()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    threading.Thread(target=scanner_loop, daemon=True, name="scanner").start()
    threading.Thread(target=survey_loop, daemon=True, name="survey").start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"StarCap QC running at http://{HOST}:{PORT}")
    print(f"   naming through {board.opmname.DASHBOARD_URL}")
    if PENDING:
        print(f"   {len(PENDING)} interrupted run(s) still to clean up; done as their boards dock")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        _survey_now.set()
        with RUNS_LOCK:
            live = [r for r in RUNS.values() if r.active]
        for r in live:                                # stop their takes; the rest is recorded on disk
            r.cancel()
        t_end = time.time() + SHUTDOWN_GRACE_S
        for r in live:
            r.join(timeout=max(0.0, t_end - time.time()))


if __name__ == "__main__":
    main()
