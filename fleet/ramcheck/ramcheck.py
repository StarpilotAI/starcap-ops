#!/usr/bin/env python3
"""
RAM Check — live debug-UART viewer for NanoPi R76S boards, with a RAM verdict
per power-on and the board's cpu_id.

    ./run.sh                    -> http://127.0.0.1:8776
    ./run.sh --port 9000        -> different port
    ./run.sh --no-browser       -> just serve
    ./run.sh --tty /dev/ttyUSB1 -> a specific adapter (default: first ttyUSB*, then ttyACM*)
    ./run.sh --replay FILE      -> parse a saved log and print the verdict (no serial)
                                   exit 0 pass, 1 FAULTY RAM, 2 warn, 3 no output, 4 unreadable

Wiring (3.3 V USB-TTL, e.g. a CH340): the three holes on the R76S underside next
to the USB-C, silkscreened RX / TX / GND, at 1500000 8N1.
  board TX  -> adapter RX      (needed: this is the boot log)
  board GND -> adapter GND     (needed)
  board RX  -> adapter TX      (optional: lets the tool log in and read the cpu_id)

Then pull the board's power and plug it back in (a normal boot, NOT maskrom).
The RAM program prints its verdict in the first second of every power-on.

Where the cpu_id comes from:
  - a board that boots: the kernel's "Serial : ..." line, or, with board RX
    wired, a serial-console login (pi/12345, then root/fa) that reads
    /proc/device-tree/serial-number, the same value the recorder uses.
  - a board that dies at the RAM check never gets far enough to say it, so type
    its starcap number and the tool looks the cpu_id up in opm-name's
    board_registry.json and the tracker's database (read-only).

Every power-on is saved under logs/ with its verdict.
"""
import argparse
import codecs
import errno
import fcntl
import glob
import json
import os
import queue
import re
import select
import sqlite3
import sys
import termios
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bootlog  # noqa: E402

LOG_DIR = os.path.join(HERE, "logs")
REGISTRY = os.path.join(HERE, "..", "opm-name", "board_registry.json")
TRACKER_DB = os.path.join(HERE, "..", "tracker", "data", "tracker.db")
# USB-TTL adapters first: a board's own USB gadget can show up as ttyACM, and
# "/dev/ttyACM0" sorts before "/dev/ttyUSB0".
TTY_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
BAUD = termios.B1500000
LOOKUP_TTL_S = 60.0      # registry/tracker answers are reused this long
LOGINS = [("pi", "12345"), ("root", "fa")]
# The leading "echo;" puts the answers on lines of their own, away from the echo
# of this command line (which contains "CPUID=$(" and must not parse as a result).
READ_CMD = ("echo; echo CPUID=$(tr -d '\\000' </proc/device-tree/serial-number); "
            "echo STARCAP_NAME=$(cat /home/*/.config/starcap/device_name "
            "/root/.config/starcap/device_name 2>/dev/null | head -1)\n")
SHELL_PROMPT = re.compile(r"[\w~\]/] ?[$#]$")
NO_ADAPTER = "no USB-TTL adapter plugged in"


# ---------------------------------------------------------------- lookups
def lookup(name=None, cpu=None, registry=REGISTRY, tracker_db=TRACKER_DB):
    """Registry + tracker facts for a starcap name or a cpu_id. Read-only.

    The tracker mirrors the dashboard, which owns names, so it wins; opm-name's
    registry fills in boards the tracker has never seen."""
    out = {"name": name, "cpu_id": cpu, "sources": []}
    try:
        with open(registry, encoding="utf-8") as f:
            reg = json.load(f)
        reg = reg.get("boards", reg) if isinstance(reg, dict) else {}
        hits = [(k, v) for k, v in reg.items() if isinstance(v, dict) and (
            (cpu and k == cpu) or (not cpu and name and str(v.get("name", "")).lower() == name))]
        if hits:
            # A name can sit on two cpus (a board renamed, a re-burn): newest wins.
            k, v = max(hits, key=lambda kv: kv[1].get("last_seen") or 0)
            out["cpu_id"], out["name"], out["model"] = k, v.get("name"), v.get("model")
            out["sources"].append("board_registry")
            if len(hits) > 1:
                out["other_cpu_ids"] = sorted(h[0] for h in hits if h[0] != k)
    except (OSError, ValueError):
        pass
    try:
        db = sqlite3.connect(f"file:{tracker_db}?mode=ro", uri=True, timeout=1)
        try:
            q = "select cpu_id, name, camera_id, board from starcaps where "
            row = None
            if cpu:
                row = db.execute(q + "cpu_id=?", (cpu,)).fetchone()
            elif name:
                rows = db.execute(q + "lower(name)=? order by created_at desc", (name,)).fetchall()
                row = rows[0] if rows else None
                others = {r[0] for r in rows[1:]} | set(out.get("other_cpu_ids", []))
                if row and out["cpu_id"] and out["cpu_id"] != row[0]:
                    others.add(out["cpu_id"])   # the registry remembers it on another cpu
                others.discard(row[0] if row else None)
                if others:
                    out["other_cpu_ids"] = sorted(others)
            if row:
                out["cpu_id"], out["name"], out["camera"], out["board"] = row
                out["sources"].append("tracker")
                dev = db.execute("select last_seen, software_version, status from devices "
                                 "where cpu_id=? order by last_seen desc limit 1",
                                 (row[0],)).fetchone()
                if dev and dev[0]:
                    out["last_seen"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(dev[0])))
                    out["version"], out["status"] = dev[1], dev[2]
        finally:
            db.close()
    except sqlite3.Error:
        pass
    return out


def normalise_name(s):
    """'95', 'sc95', 'StarCap 95', '#95' -> 'starcap95'. Anything else is kept as
    lowercase letters, digits and '-', so a typed label is always a safe filename."""
    s = re.sub(r"[^a-z0-9-]", "", (s or "").lower())[:40]
    m = re.fullmatch(r"(?:starcap|sc)?-?0*(\d+)", s)
    if m:
        return "starcap" + m.group(1)
    return s or None


# ---------------------------------------------------------------- login
class AutoLogin:
    """Types the serial-console login of one power-on to make the board print its
    cpu_id: each username once (pi, then root), each prompt answered once, one
    command, one 'exit'. It never answers the same prompt twice, so a board whose
    RX is not wired (nothing echoes back) gets one 'pi' and nothing else."""

    def __init__(self, logins=LOGINS):
        self.logins = logins
        self.step = None             # None -> user -> pass -> cmd -> done
        self.tries = 0               # usernames sent
        self.user = None
        self._answered = None        # (line count, prompt) last answered

    def react(self, boot, partial):
        """bytes to type for the board's current unterminated line, or None."""
        if self.step == "done":
            return None
        if boot.cpu_id:
            was = self.step
            self.step = "done"
            return b"exit\n" if was == "cmd" else None   # don't leave a shell logged in
        p = bootlog.clean(partial).strip()
        key = (len(boot.lines), p)
        if not p or key == self._answered:
            return None
        out = None
        if p.endswith("login:"):
            if self.tries >= len(self.logins):
                self.step = "done"
                return None
            self.user = self.logins[self.tries][0]
            self.tries += 1
            self.step, out = "user", self.user + "\n"
        elif p.endswith("Password:") and self.step == "user":
            self.step, out = "pass", self.logins[self.tries - 1][1] + "\n"
        elif self.step == "pass" and ("@" in p or len(p) == 1) and SHELL_PROMPT.search(p):
            self.step, out = "cmd", READ_CMD
        if out is None:
            return None
        self._answered = key
        return out.encode()

    def state(self):
        return {"step": self.step, "tries": self.tries, "user": self.user}


# ---------------------------------------------------------------- state
class Hub:
    """The current power-on, past ones, and the browsers listening.

    Everything that changes state or emits runs under self.lock, so an SSE client
    that takes a snapshot and subscribes under the lock misses and repeats nothing."""

    def __init__(self, log_dir=LOG_DIR, lookup_fn=lookup):
        self.lock = threading.RLock()
        self.split = bootlog.Splitter()
        self.bot = AutoLogin()
        self.boot_id = 0
        self.port = None
        self.port_error = None
        self.can_write = False
        self.label = None            # starcap name typed in the page
        self.label_boot = None       # power-on the label belongs to (None = the next one)
        self.history = []            # newest first, dicts saved to logs/
        self.log_dir = log_dir
        self._clients = []
        self._clients_lock = threading.Lock()
        self._lookup = lookup_fn
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._sent_verdict = None
        os.makedirs(log_dir, exist_ok=True)
        self._load_history()

    @property
    def boot(self):
        return self.split.boot

    @property
    def finished(self):
        return not self.split.open

    def _load_history(self):
        for p in sorted(glob.glob(os.path.join(self.log_dir, "*.json")), reverse=True)[:50]:
            try:
                with open(p, encoding="utf-8") as f:
                    self.history.append(json.load(f))
            except (OSError, ValueError):
                pass

    # -- broadcast
    def subscribe(self):
        """-> (queue, snapshot), taken together so no event falls in between."""
        q = queue.Queue(maxsize=2000)
        q.dead = False
        with self.lock:
            snap = self.snapshot()
            with self._clients_lock:
                self._clients.append(q)
        return q, snap

    def unsubscribe(self, q):
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def emit(self, kind, data):
        msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()
        with self._clients_lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    # A stalled browser missed events: drop it; EventSource reconnects
                    # and gets a fresh snapshot instead of a silently wrong page.
                    q.dead = True
                    self._clients.remove(q)

    # -- identity
    def cached_lookup(self, name=None, cpu=None):
        key = (name, cpu)
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < LOOKUP_TTL_S:
            return hit[1]
        res = self._lookup(name=name, cpu=cpu)
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), res)
        return res

    def warm(self):
        """Do the slow (sqlite) lookups outside self.lock, so the serial reader is
        never stuck behind the tracker database."""
        b, label = self.boot, self.label
        if b is not None and b.cpu_id:
            self.cached_lookup(cpu=b.cpu_id)
        elif label:
            self.cached_lookup(name=label)

    def identity(self, b=None):
        """cpu_id the board said itself, else the typed label's registry entry."""
        b = b if b is not None else self.boot
        said = b.cpu_id if b else None
        ident = {"board_cpu_id": said, "board_name": b.name if b else None}
        if said:
            lk = ident["lookup"] = self.cached_lookup(cpu=said)
            if self.label and lk.get("name") and lk["name"] != self.label:
                ident["mismatch"] = f"Board says cpu {said} = {lk['name']}, but you typed {self.label}"
        elif self.label:
            ident["lookup"] = self.cached_lookup(name=self.label)
        return ident

    def snapshot(self):
        b = self.boot
        info = None
        if b:
            info = {"id": b.id, "started": b.started, "stage": b.stage,
                    "stages": bootlog.STAGES, "stage_labels": bootlog.STAGE_LABEL,
                    "verdict": b.verdict(self.finished), "finished": self.finished,
                    "identity": self.identity(b), "lines": b.lines[-400:],
                    "login": self.bot.state()}
        return {"port": self.port, "port_error": self.port_error, "boot": info,
                "label": self.label, "can_write": self.can_write,
                "history": self.history[:30]}

    def _verdict_event(self, b, finished):
        return {"id": b.id, "stage": b.stage, "verdict": b.verdict(finished),
                "identity": self.identity(b), "finished": finished}

    # -- serial side
    def set_port(self, path, error=None, can_write=False):
        with self.lock:
            if (path, error, can_write) != (self.port, self.port_error, self.can_write):
                self.port, self.port_error, self.can_write = path, error, can_write
                self.emit("state", self.snapshot())

    def port_lost(self, path):
        with self.lock:
            self._apply(self.split.detach(
                f"The USB-TTL adapter ({path}) dropped off during this power-on, "
                "so the end of the log is missing"))
            self.port, self.can_write = None, False
            self.port_error = f"{path} disappeared, waiting for the adapter"
            self.emit("state", self.snapshot())

    def on_data(self, text, now):
        """Text from the wire. -> bytes the auto-login wants typed, or None."""
        self.warm()
        with self.lock:
            self._apply(self.split.feed(text, now))
            b = self.boot
            reply = None
            if b is not None and self.can_write:
                reply = self.bot.react(b, self.split.buf)
            if b is not None:
                ev = self._verdict_event(b, self.finished)
                if ev != self._sent_verdict:     # not once per read, only on change
                    self._sent_verdict = ev
                    self.emit("verdict", ev)
            return reply

    def on_tick(self, now):
        self.warm()
        with self.lock:
            self._apply(self.split.tick(now))

    def _apply(self, events):
        closed = False
        for e in events:
            kind, b = e[0], e[1]
            if kind == "boot":
                self.boot_id += 1
                b.id = self.boot_id
                b.started = time.strftime("%Y-%m-%d %H:%M:%S")
                b.stamp = time.strftime("%Y%m%d-%H%M%S")
                self.bot = AutoLogin()
                # A label typed for an earlier board must not name this one: the
                # next board on the bench is usually a different starcap.
                if self.label and self.label_boot is not None:
                    self.label = None
                self.label_boot = b.id if self.label else None
                self.emit("boot", {"id": b.id, "started": b.started})
            elif kind == "line":
                self.emit("line", {"id": b.id, "text": e[2]})
            elif kind == "final":
                self.save(b)
                ev = self._verdict_event(b, True)
                self._sent_verdict = ev
                self.emit("verdict", ev)
                closed = True
        if closed:
            self.emit("state", self.snapshot())

    # -- records
    def save(self, b):
        """Write logs/<stamp>_<name>.{log,json} for a power-on and put it in the
        history. Saving the same power-on again (it printed more, or it was
        relabelled) rewrites its record, renaming the files if the name changed."""
        if not b.lines and not b.noise_bytes:
            return
        v = b.verdict(True)
        ident = self.identity(b)
        lk = ident.get("lookup") or {}
        name = normalise_name(lk.get("name") or self.label) or "unknown"
        base = b.base
        if base is None or not base.endswith("_" + name):
            base = os.path.join(self.log_dir, f"{b.stamp}_{name}")
            n = 2
            while os.path.exists(base + ".json") or os.path.exists(base + ".log"):
                base = os.path.join(self.log_dir, f"{b.stamp}-{n}_{name}")
                n += 1
            if b.base:
                for ext in (".log", ".json"):
                    try:
                        os.replace(b.base + ext, base + ext)
                    except OSError:
                        pass
            b.base = base
        lines = b.lines or [f"(no text: {b.noise_bytes} bytes of line noise)"]
        if b.dropped:
            lines = lines + [f"({b.dropped} more lines not kept)"]
        with open(base + ".log", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        cpu = ident.get("board_cpu_id") or lk.get("cpu_id")
        rec = {"started": b.started, "name": name, "label": self.label, "cpu_id": cpu,
               "cpu_from": "board" if ident.get("board_cpu_id") else ("lookup" if cpu else None),
               "camera": lk.get("camera"),
               "status": v["status"], "title": v["title"], "details": v["details"],
               "log": os.path.basename(base + ".log")}
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        if b.rec is None:
            b.rec = rec
            self.history.insert(0, rec)
            del self.history[200:]
        else:
            b.rec.clear()
            b.rec.update(rec)                # same dict the history holds
        self.emit("history", self.history[:30])

    def set_label(self, name):
        label = normalise_name(name)
        if label:
            self.cached_lookup(name=label)   # the sqlite read, outside the lock
        with self.lock:
            self.label = label
            b = self.boot
            self.label_boot = b.id if (label and b is not None) else None
            # Typed after the power-on was judged: relabel that record.
            if b is not None and b.rec is not None and self.finished:
                self.save(b)
            self.emit("state", self.snapshot())
            return label


# ---------------------------------------------------------------- serial
def find_port(patterns=TTY_GLOBS):
    """First adapter by pattern order, then by number (ttyUSB2 before ttyUSB10)."""
    def num(p):
        m = re.search(r"(\d+)$", p)
        return (int(m.group(1)) if m else -1, p)
    for pat in patterns:
        found = sorted(glob.glob(pat), key=num)
        if found:
            return found[0]
    return None


def open_port(path):
    """-> (fd, can_write). Opening read-write sends nothing by itself: the
    adapter's TX idles high whether or not anything has it open, and DTR/RTS are
    not wired, so it is harmless on an adapter whose TX goes nowhere."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        can_write = True
    except PermissionError:
        fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)   # may raise it too
        can_write = False
    try:
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = BAUD
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)
        # A second reader (screen, minicom) would steal half the bytes and wreck the
        # verdict: claim the port. root can still open it.
        fcntl.ioctl(fd, termios.TIOCEXCL)
    except OSError:
        os.close(fd)
        raise
    return fd, can_write


def pump(hub, fd, path, stop=None):
    """Read until the port goes away (unplugged, hung up, re-enumerated)."""
    p = select.poll()
    p.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    while not (stop and stop.is_set()):
        ready = p.poll(200)
        if not ready:
            if not os.path.exists(path):
                return
            continue
        mask = ready[0][1]
        if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL) and not mask & select.POLLIN:
            return
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            return                           # EIO: the adapter is gone
        if not data:
            return                           # hung up
        reply = hub.on_data(dec.decode(data), time.monotonic())
        if reply:
            try:
                os.write(fd, reply)
            except OSError:
                pass


def reader(hub, patterns=TTY_GLOBS, stop=None):
    while not (stop and stop.is_set()):
        path = find_port(patterns)
        if not path:
            hub.set_port(None, NO_ADAPTER)
            time.sleep(0.5)
            continue
        try:
            fd, can_write = open_port(path)
        except PermissionError:
            hub.set_port(None, f"no permission on {path}: run ./install.sh once (it adds a udev "
                               f"rule for USB-TTL adapters), then unplug and replug the adapter. "
                               f"For now: sudo chmod a+rw {path}")
            time.sleep(1)
            continue
        except OSError as e:
            msg = e.strerror or str(e)
            if e.errno == errno.EBUSY:
                msg = "busy: another program (screen, minicom, another RAM Check) has it open"
            hub.set_port(None, f"{path}: {msg}")
            time.sleep(1)
            continue
        hub.set_port(path, None, can_write)
        try:
            pump(hub, fd, path, stop)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if not (stop and stop.is_set()):
            hub.port_lost(path)


def ticker(hub, stop=None):
    """Close a power-on once the line goes quiet, so a hung board gets a final verdict."""
    while not (stop and stop.is_set()):
        time.sleep(0.25)
        hub.on_tick(time.monotonic())


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def hub(self):
        return self.server.hub

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            with self.hub.lock:
                snap = self.hub.snapshot()
            self._json(snap)
        elif u.path == "/api/lookup":
            q = parse_qs(u.query)
            cpu = (q.get("cpu") or [""])[0].strip().lower()
            self._json(self.hub.cached_lookup(
                name=normalise_name((q.get("name") or [""])[0]),
                cpu=cpu if re.fullmatch(r"[0-9a-f]{16}", cpu) else None))
        elif u.path == "/api/log":
            name = os.path.basename((parse_qs(u.query).get("f") or [""])[0])
            p = os.path.join(self.hub.log_dir, name)
            if not name.endswith(".log") or not os.path.isfile(p):
                return self._json({"error": "no such log"}, 404)
            with open(p, encoding="utf-8", errors="replace") as f:
                self._json({"lines": f.read().splitlines()})
        elif u.path == "/events":
            self._events()
        else:
            self._json({"error": "not found"}, 404)

    def _events(self):
        q, snap = self.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(f"event: state\ndata: {json.dumps(snap)}\n\n".encode())
            self.wfile.flush()
            while not q.dead:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except OSError:                      # the browser went away
            pass
        finally:
            self.hub.unsubscribe(q)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if u.path == "/api/label":
            name = body.get("name")
            label = self.hub.set_label(name if isinstance(name, str) else None)
            self._json({"ok": True, "label": label})
        else:
            self._json({"error": "not found"}, 404)


def serve(hub, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.hub = hub
    return srv


# ---------------------------------------------------------------- replay
REPLAY_EXIT = {"pass": 0, "fail": 1, "warn": 2, "nooutput": 3}
SEVERITY = ["pass", "nooutput", "warn", "fail"]


def replay(path):
    """Print the verdict of each power-on in a saved log. Exit code = the worst one."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"cannot read {path}: {e.strerror or e}", file=sys.stderr)
        return 4
    sp = bootlog.Splitter()
    boots = []
    for ev in sp.feed(text if text.endswith("\n") else text + "\n", 0.0) + sp.tick(1e9):
        if ev[0] == "boot":
            boots.append(ev[1])
    if not boots:
        print("No output in this log")
        return REPLAY_EXIT["nooutput"]
    worst = "pass"
    for i, b in enumerate(boots, 1):
        v = b.verdict(True)
        if len(boots) > 1:
            print(f"power-on {i}:")
        print(v["title"])
        for d in v["details"]:
            print("  -", d)
        if b.cpu_id:
            print("  cpu_id:", b.cpu_id)
        if SEVERITY.index(v["status"]) > SEVERITY.index(worst):
            worst = v["status"]
    return REPLAY_EXIT[worst]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8776)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tty", metavar="PATH", help="adapter device or glob (default: %(default)s)")
    ap.add_argument("--replay", metavar="FILE", help="print the verdict for a saved log and exit")
    a = ap.parse_args()
    if a.replay:
        sys.exit(replay(a.replay))
    url = f"http://127.0.0.1:{a.port}"
    hub = Hub()
    try:
        srv = serve(hub, a.port)
    except OSError:
        print(f"already running? opening {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    threading.Thread(target=reader, args=(hub, (a.tty,) if a.tty else TTY_GLOBS), daemon=True).start()
    threading.Thread(target=ticker, args=(hub,), daemon=True).start()
    print(f"RAM Check on {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        with hub.lock:
            if hub.boot is not None and not hub.finished:
                hub.save(hub.boot)


if __name__ == "__main__":
    main()
