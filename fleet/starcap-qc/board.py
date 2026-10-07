"""
board.py: how StarCap QC reaches a docked board.

Nothing here is new plumbing. Discovery is dock-check's (the same "docked means on
this laptop's ethernet" rule, the same UDP roll-call and ping sweep), and SSH,
identity and naming are opm-name's (both fleet logins, the cpu_id probe, the
dashboard name API). They are imported from their folders next to this one, so a
fix in either tool lands here too.
"""
import http.client
import importlib
import json
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _dep in ("opm-name", "dock-check"):
    _p = str(HERE.parent / _dep)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dock_check  # noqa: E402
import opmname  # noqa: E402

API_PORT = 8080

_DOCKSCAN = {"mod": None, "tried": False}
_DOCKSCAN_LOCK = threading.Lock()


def dockscan():
    """camera/camfw/dockscan.py, the firmware-build grader (its on-board sysfs script,
    PROFILES and grade()), or None. Imported rather than copied so the two can never
    disagree about which build is which. dockscan imports opm-lan at the top for its
    own fleet sweep, which QC never calls, so a laptop without opm-lan gets a stand-in
    module for that import. Locked: two QC runs starting together must both get it."""
    with _DOCKSCAN_LOCK:
        if _DOCKSCAN["tried"]:
            return _DOCKSCAN["mod"]
        _DOCKSCAN["tried"] = True
        camfw = HERE.parent.parent / "camera" / "camfw"
        if not (camfw / "dockscan.py").is_file():
            return None
        if str(camfw) not in sys.path:
            sys.path.insert(0, str(camfw))
        try:
            importlib.import_module("opmlan")         # the real one, when this laptop has it
        except Exception:                             # noqa: BLE001
            sys.modules.setdefault("opmlan", types.ModuleType("opmlan"))
        try:
            mod = importlib.import_module("dockscan")
        except Exception as e:                        # noqa: BLE001
            print(f"[qc] camera firmware grading unavailable: {e}")
            return None
        _DOCKSCAN["mod"] = mod
        return mod


wired_links = dock_check.wired_links
wired_status = dock_check.wired_status


def roll_call(links):
    """Boards whose recorder answers the UDP roll-call over the dock link -> {ip: {...}}."""
    if not links:
        return {}
    return dock_check.roll_call(timeout=1.2, links=links)


def ping_sweep(links):
    """Every address that answers a ping on the dock link. Catches a board whose
    recorder is down, which the roll-call cannot."""
    return list(dock_check.ping_sweep(links))


# --------------------------------------------------------------------------- #
# the recorder's HTTP API (:8080)
# --------------------------------------------------------------------------- #
class BoardError(RuntimeError):
    """A board did not answer the way a healthy recorder does. The message is shown
    to the operator as is, so it says what happened in plain words."""


def http_json(ip, path, method="GET", body=None, timeout=6.0):
    """One call to the recorder. POSTs without a body send none at all: the recorder
    never reads a body on its record/delete routes and speaks keep-alive, so stray
    bytes would be parsed as the start of the next request. urllib also sends
    `Connection: close`, so nothing is ever left on a shared connection."""
    data = None
    headers = {"Connection": "close"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://{ip}:{API_PORT}{path}", data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace").strip()[:200]
        raise BoardError(f"{path} answered HTTP {e.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as e:
        reason = getattr(e, "reason", None) or type(e).__name__
        raise BoardError(f"no answer from the recorder on {ip}:{API_PORT} ({reason})") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise BoardError(f"{path} sent something that is not JSON") from None


def status(ip, timeout=6.0):
    return http_json(ip, "/api/status", timeout=timeout)


def download(ip, run, name, dest, timeout=120.0):
    """One file of a finished take, over the recorder's own download route (binary-safe,
    unlike SSH text), checked against Content-Length -> bytes written."""
    req = urllib.request.Request(
        f"http://{ip}:{API_PORT}/download/{urllib.parse.quote(run)}/{urllib.parse.quote(name)}",
        headers={"Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            want = r.headers.get("Content-Length")
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
    except urllib.error.HTTPError as e:
        raise BoardError(f"couldn't download {name}: HTTP {e.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as e:
        raise BoardError(f"couldn't download {name} ({getattr(e, 'reason', None) or type(e).__name__})") from None
    if want is not None and str(got) != want:
        raise BoardError(f"{name} came through short ({got} of {want} bytes)")
    return got


def delete_run(ip, run, timeout=60.0):
    """The recorder's own delete route for one take -> its HTTP status: 200 deleted,
    404 no take by that name (gone, or renamed), 409 it is the take being recorded.
    Raises BoardError only when the recorder did not answer at all."""
    req = urllib.request.Request(
        f"http://{ip}:{API_PORT}/api/recordings/{urllib.parse.quote(run)}/delete",
        headers={"Connection": "close"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as e:
        raise BoardError(f"no answer from the recorder on {ip}:{API_PORT} "
                         f"({getattr(e, 'reason', None) or type(e).__name__})") from None


# Every take on the board with its identity. A take's NAME can change under us: the
# recorder renames every take of this boot when NTP steps the clock by a minute or more
# (app.py _apply_clock_step), stopped takes included. Its recording_id (device_info.json,
# written and fsynced at start) never changes, and a renamed take also records
# `renamed_from`. So takes are tracked by recording_id, never by name alone.
_TAKES_PY = r'''
import json, os
roots = ["/media/sdcard/multivideo", "/media/sdcard/stereo",
         os.path.expanduser("~/orangepi-recorder/recordings/multivideo"),
         os.path.expanduser("~/orangepi-recorder/recordings/stereo")]
out = []
for root in roots:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        continue
    for n in names:
        d = os.path.join(root, n)
        if not os.path.isdir(d):
            continue
        info = {}
        try:
            with open(os.path.join(d, "device_info.json")) as f:
                info = json.load(f)
        except (OSError, ValueError):
            pass
        out.append({"run": n, "path": os.path.realpath(d), "rid": info.get("recording_id"),
                    "renamed_from": info.get("renamed_from"), "boot_id": info.get("boot_id"),
                    "started_mono": info.get("started_mono"),
                    "finalizing": os.path.exists(os.path.join(d, ".finalizing"))})
with open("/proc/uptime") as f:
    print("MONO=" + f.read().split()[0])
print("TAKES=" + json.dumps(out))
'''


def list_takes(ip):
    """-> (takes, board_monotonic_s): every take on the board as {run, path, rid,
    renamed_from, boot_id, started_mono, finalizing}, and the board's CLOCK_MONOTONIC
    now (what the recorder stamps as started_mono). Raises BoardError when it can't be
    read: an inventory that silently came back empty would make every take look new,
    or none."""
    r = ssh(ip, "python3 - <<'QCTAKES'\n" + _TAKES_PY + "\nQCTAKES", timeout=40)
    takes = mono = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("TAKES="):
            try:
                takes = json.loads(line[6:])
            except ValueError:
                takes = None
        elif line.startswith("MONO="):
            try:
                mono = float(line[5:])
            except ValueError:
                mono = None
    if takes is None or mono is None:
        raise BoardError("couldn't list the takes on the board")
    return takes, mono


def preview_frames(ip, count=3, timeout=20.0, max_part=8_000_000):
    """Read `count` JPEG frames off /preview.mjpg, then close the connection.

    It is the raw camera frame (4000x1200: IMU strip | right eye | left eye). The
    recorder has no single-snapshot route, and every open preview stream holds one of
    its server threads until the client goes away, so this always closes. It is only
    used while the board is idle (8 fps); during a take the stream drops to 2 fps and
    a viewer costs recording fps."""
    frames = []
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((ip, API_PORT), timeout=min(8.0, timeout))
    except OSError as e:
        raise BoardError(f"couldn't open the camera preview ({e})") from None
    try:
        sock.settimeout(5.0)
        sock.sendall(f"GET /preview.mjpg HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode())
        f = sock.makefile("rb")
        status_line = f.readline()
        if b" 200 " not in status_line:
            raise BoardError(f"preview answered {status_line.decode(errors='replace').strip()[:60]}")
        while f.readline() not in (b"\r\n", b"\n", b""):
            pass                                          # response headers
        while len(frames) < count and time.monotonic() < deadline:
            length = None
            line = f.readline()
            if not line:
                break
            while line not in (b"\r\n", b"\n", b""):
                k, _, v = line.decode(errors="replace").partition(":")
                if k.strip().lower() == "content-length":
                    try:
                        length = int(v.strip())
                    except ValueError:
                        length = None
                line = f.readline()
            if length is None:
                continue                                  # the boundary line; headers follow
            if not 0 < length <= max_part:
                raise BoardError(f"preview frame of {length} bytes makes no sense")
            data = f.read(length)
            if len(data) == length and data[:2] == b"\xff\xd8":
                frames.append(data)
    except (OSError, socket.timeout) as e:
        if not frames:
            raise BoardError(f"the camera preview stalled ({e})") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return frames


# --------------------------------------------------------------------------- #
# SSH (opm-name's: both fleet logins, cached per IP)
# --------------------------------------------------------------------------- #
def ssh(ip, script, timeout=30):
    """Run a shell script on the board -> CompletedProcess. Raises BoardError when no
    fleet login gets a shell at all; a script that runs and fails is returned."""
    r = opmname._ssh(ip, script, timeout=timeout)
    if r.returncode == 255 or opmname.login_for(ip) is None:
        msg = (r.stderr or "").strip().splitlines()
        raise BoardError("couldn't log in over SSH"
                         + (f" ({msg[-1][:160]})" if msg else "")
                         + " (tried orangepi/orangepi and pi/12345)")
    return r


def sudo_password(ip):
    login = opmname.login_for(ip) or opmname._resolve_login(ip)
    if not login:
        raise BoardError("couldn't log in over SSH (tried orangepi/orangepi and pi/12345)")
    return login[1]


# --------------------------------------------------------------------------- #
# the device card the UI shows for each docked board
# --------------------------------------------------------------------------- #
_IDENT = {}             # ip -> (fetched_at, identify_for_rename result, device_id)
IDENT_TTL_S = 60.0      # a good answer is kept a minute (it costs an SSH round trip)
IDENT_RETRY_S = 10.0    # a failed one is retried soon: a board that just booted has no SSH yet


def _identity(ip, device_id):
    """opm-name's identity probe (cpu_id, model, boot medium, wifi), cached, and
    refreshed at once when a different board takes over the IP (device_id changes)."""
    now = time.time()
    hit = _IDENT.get(ip)
    if hit and hit[2] == device_id and now - hit[0] < (IDENT_TTL_S if hit[1].get("ok") else IDENT_RETRY_S):
        return hit[1]
    try:
        ident = opmname.identify_for_rename(ip, timeout=25)
    except Exception as e:                            # noqa: BLE001
        ident = {"ok": False, "error": str(e)[:160]}
    _IDENT[ip] = (now, ident, device_id)
    return ident


def describe(ip, previous=None):
    """One docked board, as the UI's card needs it, or None when nothing at that
    address answers like a StarCap (a switch on the dock answers pings too).
    Never raises."""
    dev = {"ip": ip, "seen": time.time()}
    try:
        st = status(ip, timeout=3.0)
    except BoardError as e:
        st = None
        dev["api_error"] = str(e)
    if st is None and previous and time.time() - previous.get("seen", 0) < 12:
        # one missed status poll: keep showing what we knew, marked stale
        keep = dict(previous)
        keep["stale"] = True
        return keep
    if st:
        cam = st.get("camera") or {}
        sysd = st.get("system") or {}
        dev.update({
            "device_id": st.get("device_id"), "name": st.get("name"),
            "hostname": st.get("hostname"), "version": st.get("version"),
            "camera_id": cam.get("camera_id"), "camera_error": cam.get("error"),
            "fps": cam.get("capture_fps"), "recording": bool(cam.get("recording")),
            "rec_duration": cam.get("duration"), "soc_temp": sysd.get("soc_temp"),
            "uptime": sysd.get("uptime"),
        })
    ident = _identity(ip, dev.get("device_id"))
    if ident.get("ok"):
        dev.update({"cpu_id": ident.get("cpu_id"), "model": ident.get("model"),
                    "boot": ident.get("boot"), "wifi": ident.get("wifi")})
        dev.setdefault("name", ident.get("name"))
        if not dev.get("version"):
            dev["version"] = ident.get("version")
    else:
        dev["error"] = ident.get("error") or "couldn't identify the board over SSH"
    if not st:
        if not ident.get("ok"):
            return None                               # neither the recorder nor a fleet login answers
        dev["error"] = dev.get("api_error")
        dev["recorder_down"] = True                   # a StarCap whose recorder is down: show it
    return dev
