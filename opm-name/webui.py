#!/usr/bin/env python3
"""
opm-name web UI — dock recorders over ethernet, see them appear live with their
IP, and type a name next to each one. Renaming writes straight into the
dashboard's authoritative name store (same mechanism as tools/name_pi.sh) — the
board self-syncs its own local name from there on its next heartbeat, so one
write renames it both on the device and on the dashboard.

Run:   python3 webui.py        →  http://127.0.0.1:8771
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import opmname

HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"
PORT = 8771
CFG = {"lan": {"iface": "", "scan_subnet": ""}}   # same auto-detect as opm-lan

# --------------------------------------------------------------------------- #
# background scanner — keeps the live-device table fresh without blocking requests
# --------------------------------------------------------------------------- #
DEVICES = []            # list of {ip, ok, cpu_id, name, hostname, error}
IFACE = None
SCAN_LOCK = threading.Lock()
_scan_stop = threading.Event()
_renamed_locally = {}   # ip -> optimistic name, so the table reflects a rename
                        # instantly instead of waiting for the next scan pass


def scanner_loop():
    global DEVICES, IFACE
    while not _scan_stop.is_set():
        try:
            devices, iface = opmname.discover_for_rename(CFG)
            with SCAN_LOCK:
                for d in devices:
                    over = _renamed_locally.get(d["ip"])
                    if over is not None:
                        d["name"] = over
                DEVICES = devices
                IFACE = iface
        except Exception as e:                           # noqa: BLE001
            print(f"[scan] {e}")
        _scan_stop.wait(4)


def devices_snapshot():
    with SCAN_LOCK:
        return [dict(d) for d in DEVICES]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def api_status():
    return {"devices": devices_snapshot(), "iface": IFACE,
            "redis_configured": bool(opmname.REDIS_URL and opmname.REDIS_TOKEN)}


def api_rename(body):
    ip = (body.get("ip") or "").strip()
    name = (body.get("name") or "").strip()
    if not ip or not name:
        return {"ok": False, "error": "ip and name are required"}
    with SCAN_LOCK:
        dev = next((d for d in DEVICES if d["ip"] == ip), None)
    if not dev or not dev.get("ok"):
        # re-probe once — the device may have just docked since the last scan pass
        dev = opmname.identify_for_rename(ip)
        if not dev.get("ok"):
            return {"ok": False, "error": dev.get("error", "device not found/reachable")}
    try:
        opmname.set_authoritative_name(dev["cpu_id"], name)
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": str(e)}
    with SCAN_LOCK:
        _renamed_locally[ip] = name
        for d in DEVICES:
            if d["ip"] == ip:
                d["name"] = name
    return {"ok": True, "ip": ip, "cpu_id": dev["cpu_id"], "name": name}


def api_buzz(body):
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"ok": False, "error": "ip is required"}
    return opmname.identify_buzz(ip)


def api_clear_recordings(body):
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"ok": False, "error": "ip is required"}
    return opmname.clear_recordings(ip)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, path, ctype):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return self._json({"error": f"missing file: {path}"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(api_status())
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rename":
            return self._json(api_rename(self._body()))
        if path == "/api/buzz":
            return self._json(api_buzz(self._body()))
        if path == "/api/clear_recordings":
            return self._json(api_clear_recordings(self._body()))
        return self._json({"error": "not found"}, 404)


def main():
    threading.Thread(target=scanner_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"opm-name running at http://{HOST}:{PORT}")
    if not (opmname.REDIS_URL and opmname.REDIS_TOKEN):
        print("!! UPSTASH_REDIS_REST_URL/TOKEN not found — renaming will fail until "
              "these are set (env vars, or ~/starcap-dashboard/dashboard/.env.local)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
