#!/usr/bin/python3 -s
"""
Stereo Check web app: http://127.0.0.1:8778

  ./app.py                 serve and open the browser
  ./app.py --no-browser    just serve
  ./app.py --port 9000

Buttons for what stereo_check.py does on the command line, plus the robot bench:
when the arm bench (wide-fov-stereo-calibration, robot_calibration) is running it
exposes a localhost hook on :8781; "Robot: centre board + check" sends the arm to
the pose saved there with 📌, waits for it to arrive and settle, then checks.
"""
import argparse, glob, json, mimetypes, os, sys, threading, time, traceback, urllib.error, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import stereo_check as S

BENCH = "http://127.0.0.1:8781/stereo-check"
ARRIVE_TIMEOUT_S = 120
SETTLE_S = 2.0           # let the arm stop ringing before the shutter

_lock = threading.Lock()
job = {"running": False}


# ---------------------------------------------------------------- robot bench
def bench(path, post=False, timeout=2):
    """(http status, json) from the bench hook; (None, None) if the bench is not running."""
    req = urllib.request.Request(BENCH + path, data=b"" if post else None, method="POST" if post else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return None, None


def bench_state():
    code, st = bench("/status")
    if code is None:
        return {"ready": False, "reason": "robot bench not running (open Robot Bench)"}
    if not st.get("enabled"):
        return {"ready": False, "reason": "robot not enabled: click Enable Robot in the bench", **st}
    if not st.get("pose_saved"):
        return {"ready": False, "reason": "no pose saved: centre the board in the bench, then 📌 Save pose for Stereo Check", **st}
    if st.get("busy"):
        return {"ready": False, "reason": "the bench is running a trajectory", **st}
    return {"ready": True, "reason": "", **st}


def robot_to_pose(log):
    st = bench_state()
    if not st["ready"]:
        raise S.CheckError(st["reason"])
    code, r = bench("/goto", post=True)
    if code != 200:
        raise S.CheckError("bench refused: " + ((r or {}).get("message") or f"HTTP {code}"))
    log("robot: " + r.get("message", "moving"))
    t0 = time.time()
    while time.time() - t0 < ARRIVE_TIMEOUT_S:
        time.sleep(0.5)
        code, st = bench("/status")
        if code is None:
            raise S.CheckError("lost the robot bench while the arm was moving")
        if not st.get("moving"):
            if st.get("at_pose"):
                log(f"robot: at the pose, settling {SETTLE_S:g} s")
                time.sleep(SETTLE_S)
                return
            raise S.CheckError(f"the arm stopped {st.get('worst_joint_off_deg') or 0:.1f}° short of the pose"
                               " (blocked or stopped in the bench)")
    raise S.CheckError("the arm did not reach the pose in time")


# ---------------------------------------------------------------- jobs
def start_job(kind, fn):
    with _lock:
        if job.get("running"):
            return False
        job.clear()
        job.update(running=True, kind=kind, log=[], started=time.time(), result=None, error=None)

    def log(s):
        job["log"].append(time.strftime("%H:%M:%S ") + s)

    def work():
        try:
            job["result"] = fn(log)
            log("done")
        except S.CheckError as e:
            job["error"] = str(e)
            log("stopped: " + str(e))
        except Exception as e:                        # noqa: BLE001  show it, keep serving
            job["error"] = f"{type(e).__name__}: {e}"
            log(traceback.format_exc())
        finally:
            job["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


def public(r):
    """A result with its picture as a URL the page can load."""
    if not r:
        return r
    r = {k: v for k, v in r.items() if k != "calibrations"}
    if r.get("picture"):
        r["picture_url"] = "/files/" + os.path.relpath(r["picture"], S.OUT_ROOT) + f"?t={int(time.time())}"
    return r


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            cams = S.docked_cameras()
            unit = q.get("unit") or (next(iter(cams)) if len(cams) == 1 else None)
            return self.send(200, {
                "cameras": [{"unit": k, "dev": v, "held": S.busy(v)} for k, v in cams.items()],
                "unit": unit,
                "saved": sorted((os.path.basename(d)[3:] for d in glob.glob(os.path.join(S.OUT_ROOT, "cam*"))
                                 if os.path.isdir(d)), key=lambda u: (len(u), u)),
                "bench": bench_state(),
                "job": {**{k: v for k, v in job.items() if k != "result"}, "result": public(job.get("result"))},
                "runs": [{**r, "picture_url": "/files/" + os.path.relpath(r["picture"], S.OUT_ROOT)}
                         for r in S.runs(unit)] if unit else [],
                "spec_px": S.SPEC_PX, "out_root": S.OUT_ROOT})
        if u.path == "/api/run":
            f = os.path.join(S.run_folder(q.get("unit", ""), q.get("label", "")), "result.json")
            if not os.path.realpath(f).startswith(os.path.realpath(S.OUT_ROOT) + os.sep) or not os.path.exists(f):
                return self.send(404, {"error": "no such run"})
            return self.send(200, public(json.load(open(f))))
        if u.path == "/api/cases":
            unit = q.get("unit")
            if not unit:
                return self.send(400, {"error": "which camera?"})
            try:
                r = S.cases(unit, include_unnamed=q.get("all") == "1", log=lambda _s: None)
            except S.CheckError as e:
                return self.send(200, {"error": str(e), "runs": []})
            return self.send(200, r)
        if u.path.startswith("/files/"):
            f = os.path.realpath(os.path.join(S.OUT_ROOT, u.path[len("/files/"):]))
            if not f.startswith(os.path.realpath(S.OUT_ROOT) + os.sep) or not os.path.isfile(f):
                return self.send(404, {"error": "not found"})
            return self.send(200, open(f, "rb").read(), mimetypes.guess_type(f)[0] or "application/octet-stream")
        self.send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        b = self.body()
        if self.path == "/api/check":
            unit, label, robot = b.get("unit") or None, b.get("label") or None, bool(b.get("robot"))

            def fn(log):
                hook = (lambda _u: robot_to_pose(log)) if robot else None
                return S.check(unit, label, log=log, before_capture=hook)
            ok = start_job("robot" if robot else "check", fn)
        elif self.path == "/api/compare":
            unit, a, c = b.get("unit"), b.get("a"), b.get("b")
            if not (unit and a and c) or a == c:
                return self.send(400, {"error": "pick a camera and two different runs"})
            ok = start_job("compare", lambda log: S.compare(unit, a, c, log=log))
        elif self.path == "/api/cases":
            unit, everything = b.get("unit"), bool(b.get("all"))
            if not unit:
                return self.send(400, {"error": "which camera?"})
            ok = start_job("cases", lambda log: {"cases": S.cases(unit, everything, compute=True, log=log)})
        else:
            return self.send(404, {"error": "not found"})
        self.send(200 if ok else 409, {"ok": ok, "error": None if ok else "a check is already running"})

    def log_message(self, *_a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError:
        print(f"Stereo Check already running at {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    print(f"Stereo Check on {url}")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
