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
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import opmname

HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"
PORT = int(os.environ.get("OPM_NAME_PORT", "8771"))   # start_opm_name.sh honors this too
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


# --------------------------------------------------------------------------- #
# board log — a persistent registry of every board that has ever docked while
# opm-name was running, keyed by cpu_id (stable across reflashes). The point:
# when you're later diagnosing a pi that is NOT docked (or won't even boot),
# its last-known name/version/boot-medium are still on hand here. Stable
# fields (cpu_id, first_seen) are written once; volatile ones (version, boot
# medium, last ip, last_seen) refresh on every sighting — all for free, off
# the same scan the live table already does. Survives restarts in
# board_registry.json next to this script (machine-local, git-ignored).
# --------------------------------------------------------------------------- #
REGISTRY_FILE = SCRIPT_DIR / "board_registry.json"
REGISTRY = {}           # cpu_id -> {cpu_id, name, hostname, version, boot, wifi, last_ip, first_seen, last_seen}
REG_LOCK = threading.Lock()


def _registry_load():
    global REGISTRY
    try:
        REGISTRY = json.loads(REGISTRY_FILE.read_text())
    except (OSError, ValueError):
        REGISTRY = {}


def _registry_save():
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(REGISTRY, indent=2, sort_keys=True))
    tmp.replace(REGISTRY_FILE)          # atomic — a crash mid-write can't eat the log


def _registry_note(dev):
    """Upsert one scanned board; returns True if anything changed (worth saving).
    Empty/unknown probe values never clobber a previously-known good one.
    last_seen is quantized to the minute so a docked board doesn't rewrite the
    file every 4s scan pass."""
    cpu = dev.get("cpu_id")
    if not cpu:
        return False
    now = int(time.time())
    e = REGISTRY.setdefault(cpu, {"cpu_id": cpu, "first_seen": now})
    changed = "last_seen" not in e
    for k in ("name", "hostname", "version", "model", "wifi_driver"):
        if dev.get(k) and e.get(k) != dev[k]:
            e[k] = dev[k]; changed = True
    # wifi is recorded even when it says "none" — "this board has no radio" is a
    # real answer worth keeping, unlike an empty probe field. A board whose module
    # dies must be able to go connected -> none here, so this does not use the
    # never-clobber rule above.
    if dev.get("wifi") and e.get("wifi") != dev["wifi"]:
        e["wifi"] = dev["wifi"]; changed = True
    # SSID is coupled to `wifi` above, NOT to the never-clobber loop: it is only
    # meaningful while associated, so a board that drops off the network has to
    # stop showing the network it used to be on. Otherwise the registry keeps
    # reporting a live-looking SSID for a radio that is now idle or gone. Guarded
    # on dev["wifi"] so a failed probe (no wifi key at all) never wipes it.
    if dev.get("wifi"):
        ssid = dev.get("wifi_ssid", "") if dev["wifi"] == "connected" else ""
        if e.get("wifi_ssid", "") != ssid:
            e["wifi_ssid"] = ssid; changed = True
    if dev.get("boot") and dev["boot"] != "unknown" and e.get("boot") != dev["boot"]:
        e["boot"] = dev["boot"]; changed = True
    if e.get("last_ip") != dev["ip"]:
        e["last_ip"] = dev["ip"]; changed = True
    if now - e.get("last_seen", 0) >= 60 or changed:
        e["last_seen"] = now; changed = True
    return changed


def registry_snapshot():
    with REG_LOCK:
        return sorted((dict(e) for e in REGISTRY.values()),
                      key=lambda e: e.get("last_seen", 0), reverse=True)


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
            with REG_LOCK:
                if any([_registry_note(d) for d in devices if d.get("ok")]):
                    _registry_save()
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
            "registry": registry_snapshot(),
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
    with REG_LOCK:
        e = REGISTRY.get(dev["cpu_id"])
        if e is not None and e.get("name") != name:
            e["name"] = name
            _registry_save()
    return {"ok": True, "ip": ip, "cpu_id": dev["cpu_id"], "name": name}


def api_buzz(body):
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"ok": False, "error": "ip is required"}
    return opmname.identify_buzz(ip)


def api_vnc(body):
    """Open the board's live desktop — same 🖥️ button opm-lan has, same
    tools/vnc_into_pi.sh underneath (x11vnc on the pi + the DRM HDMI-force
    workaround, TigerVNC here, full cleanup on the pi when the window closes).
    Spawned detached so this request returns immediately; the script lock-guards
    per IP, so double-clicking while a window is already open is a no-op."""
    ip = (body.get("ip") or "").strip()
    with SCAN_LOCK:
        known = any(d["ip"] == ip and d.get("ok") for d in DEVICES)
    if not ip or not known:
        return {"ok": False, "error": "device not found / not online"}
    # ../tools/ in the starcap-ops checkout; ./tools/ in the standalone install,
    # which ships the script alongside opm-name instead of a whole repo
    script = next((p for p in (SCRIPT_DIR.parent / "tools" / "vnc_into_pi.sh",
                               SCRIPT_DIR / "tools" / "vnc_into_pi.sh") if p.exists()), None)
    if script is None:
        return {"ok": False, "error": "missing vnc_into_pi.sh"}
    log_path = f"/tmp/vnc_into_pi_{ip.replace('.', '_')}.log"
    # We already know which login this board answers on (the scan just used it) —
    # hand it over so the script doesn't re-probe the fleet logins from scratch.
    env = dict(os.environ)
    login = opmname.login_for(ip)
    if login:
        env["PI_USER"], env["PI_PASS"] = login
    with open(log_path, "ab") as logf:
        subprocess.Popen(["bash", str(script), ip], stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return {"ok": True}


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
        if path == "/api/vnc":
            return self._json(api_vnc(self._body()))
        return self._json({"error": "not found"}, 404)


def main():
    _registry_load()
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
