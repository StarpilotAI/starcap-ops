#!/usr/bin/env python3
"""
opm-name web UI — dock recorders over ethernet, see them appear live with their
IP, and type a name next to each one. Renaming POSTs to the dashboard's station
endpoint, which is the only thing that may write a name (see opmname.py's header
for what happens when a tool writes the store itself) — the board then self-syncs
its own local name from the reply to its next heartbeat, so one call renames it
both on the device and on the dashboard.

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
REGISTRY = {}           # cpu_id -> {cpu_id, name, hostname, version, boot, wifi, modem, last_ip, first_seen, last_seen}
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
    for k in ("name", "hostname", "version", "model", "wifi_driver",
              "modem_usb", "modem_vendor", "modem_product"):
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
    # modem follows the same rule as wifi: "none" is a real answer worth storing,
    # and a board whose dongle is pulled must be able to go online -> none, so it
    # is not subject to the never-clobber loop above.
    if dev.get("modem") and e.get("modem") != dev["modem"]:
        e["modem"] = dev["modem"]; changed = True
    # Coupled to `modem` for the same reason wifi_ssid is coupled to `wifi`: a
    # default route only means anything while the modem is up, so a board that
    # loses it must stop advertising one. Guarded on dev["modem"] so a failed
    # probe never wipes what we knew.
    if dev.get("modem"):
        route = bool(dev.get("modem_route")) if dev["modem"] == "online" else False
        if e.get("modem_route", False) != route:
            e["modem_route"] = route; changed = True
        # Internet is tri-state and stored as such: None ("could not test") must
        # never be flattened into False, or a modem we simply failed to reach
        # would be recorded as one with no service — the exact wrong repair.
        net = dev.get("modem_internet") if dev["modem"] == "online" else None
        if e.get("modem_internet") != net:
            e["modem_internet"] = net; changed = True
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
# The name survey costs one dashboard read, and /api/status is polled continuously, so
# it is cached. Kept deliberately stale-tolerant: on a failed lookup the LAST good
# survey is returned rather than None, because a blank "next free number" in the header
# is worse than one that is thirty seconds old. The failure is REMEMBERED though — it is
# the only signal that renaming is currently impossible (see api_status).
_SURVEY = {"at": 0.0, "data": None, "error": None}
SURVEY_TTL_S = 30.0


def name_survey_cached(force=False):
    now = time.time()
    if force or _SURVEY["data"] is None or now - _SURVEY["at"] > SURVEY_TTL_S:
        try:
            _SURVEY["data"] = opmname.name_survey()
            _SURVEY["at"] = now
            _SURVEY["error"] = None
        except Exception as e:                            # noqa: BLE001
            _SURVEY["error"] = str(e)[:200]
    return _SURVEY["data"]


def api_status():
    survey = name_survey_cached()
    return {"devices": devices_snapshot(), "iface": IFACE,
            "registry": registry_snapshot(),
            "names": survey,
            # "Can we reach the dashboard" replaced "are the Redis credentials set" as the
            # one thing worth a warning bar, because the dashboard is now the only writer
            # of names: a bench with no route to it can still scan, buzz and wipe boards,
            # but cannot rename one. Reported off the survey poll rather than a health
            # check of its own — that poll is already a real read of the very table a
            # rename writes to.
            "dashboard": {"url": opmname.DASHBOARD_URL,
                          "ok": _SURVEY["error"] is None, "error": _SURVEY["error"]}}


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
    cpu_id = dev["cpu_id"]
    # Look before writing. Two boards sharing a name is not cosmetic: the dashboard's
    # camera<->headset registry is KEYED by name, so the second board silently files
    # its camera under the first one's key and one of them stops being tracked. The
    # check is skipped on `force` (the operator confirmed in the UI) and skipped if the
    # table cannot be read — a dead lookup must not block a rename that would work.
    if not body.get("force"):
        try:
            pre = opmname.check_rename(name, cpu_id)
        except Exception:                                 # noqa: BLE001
            pre = None
        if pre and (pre["taken_by"] or (pre["current_name"] and pre["current_name"] != name)):
            holders = []
            with REG_LOCK:
                for c in pre["taken_by"]:
                    e = REGISTRY.get(c) or {}
                    holders.append({"cpu_id": c, "last_ip": e.get("last_ip"),
                                    "last_seen": e.get("last_seen"),
                                    "version": e.get("version"),
                                    "known": bool(e)})
            return {"ok": False, "needs_confirm": True, "ip": ip, "name": name,
                    "cpu_id": cpu_id, "holders": holders,
                    "current_name": pre["current_name"]}
    try:
        res = opmname.set_authoritative_name(cpu_id, name)
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": str(e)}
    # Show the name the DASHBOARD stored, not the one that was typed: it is normalised
    # server-side, and the optimistic row below is the whole reason a rename that went
    # nowhere used to look like it had worked.
    stored = res.get("assigned_name") or name
    with SCAN_LOCK:
        _renamed_locally[ip] = stored
        for d in DEVICES:
            if d["ip"] == ip:
                d["name"] = stored
    with REG_LOCK:
        e = REGISTRY.get(cpu_id)
        if e is not None and e.get("name") != stored:
            e["name"] = stored
            _registry_save()
    name_survey_cached(force=True)      # this rename just moved the highest/gaps
    return {"ok": True, "ip": ip, "cpu_id": cpu_id, "name": stored,
            "previous_name": res.get("previous_name"),
            # Boards that just LOST this name (normally none). Surfaced because the
            # dashboard resolves a duplicate by taking the name off the other unit, and
            # that is the one outcome invisible from the bench.
            "taken_from": res.get("taken_from") or []}


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
    # ../vnc/ in the starcap-ops checkout; ./tools/ in the standalone install,
    # which ships the script alongside opm-name instead of a whole repo
    script = next((p for p in (SCRIPT_DIR.parent / "vnc" / "vnc_into_pi.sh",
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
    print(f"   renaming through {opmname.DASHBOARD_URL} (override: STARCAP_DASHBOARD_URL)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
