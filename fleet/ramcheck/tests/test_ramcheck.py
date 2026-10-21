"""ramcheck: records, identity, auto-login, the serial reader (on a pty, never a real
adapter) and the HTTP/SSE server (on a free port, never 8776)."""
import fcntl
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import bootlog  # noqa: E402
import ramcheck  # noqa: E402
from ramcheck import AutoLogin, Hub, READ_CMD  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
CPU161 = "dda6ac2324f7074f"


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8", errors="replace") as f:
        return f.read()


GOOD = fixture("good_lpddr5_fresh_board.log")
DQ12 = fixture("starcap95_dq12.log")
UNKNOWN = fixture("starcap161_unknown_device.log")
BOOTED = GOOD + "Starting kernel ...\r\n[    0.000000] Booting Linux on physical CPU 0x0\r\n"


class FakeLookup:
    def __init__(self):
        self.calls = []

    def __call__(self, name=None, cpu=None):
        self.calls.append((name, cpu))
        if cpu == CPU161 or name == "starcap161":
            return {"name": "starcap161", "cpu_id": CPU161, "camera": "152", "sources": ["tracker"]}
        return {"name": name, "cpu_id": cpu, "sources": []}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class HubCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ramcheck-test-")
        self.lookup = FakeLookup()
        self.hub = Hub(log_dir=self.dir, lookup_fn=self.lookup)
        self.t = 5000.0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, text, step=0.002):
        replies = []
        for raw in text.splitlines(keepends=True):
            r = self.hub.on_data(raw, self.t)
            if r:
                replies.append(r)
            self.t += step
        return replies

    def wait(self, seconds):
        end = self.t + seconds
        while self.t < end:
            self.t += 0.25
            self.hub.on_tick(self.t)

    def files(self, ext):
        return sorted(f for f in os.listdir(self.dir) if f.endswith(ext))


class Records(HubCase):
    def test_saved_on_quiet_with_verdict(self):
        self.feed(UNKNOWN)
        self.assertEqual(self.files(".json"), [])
        self.wait(5)
        (j,) = self.files(".json")
        self.assertTrue(j.endswith("_unknown.json"))
        rec = load(os.path.join(self.dir, j))
        self.assertEqual((rec["status"], rec["title"]), ("fail", "FAULTY RAM"))
        self.assertEqual(rec["log"], j[:-5] + ".log")
        with open(os.path.join(self.dir, rec["log"])) as f:
            self.assertIn("unknown device", f.read())
        self.assertEqual(self.hub.history[0], rec)

    def test_label_after_the_fact_relabels_and_renames(self):
        self.feed(UNKNOWN)
        self.wait(5)
        self.assertEqual(self.hub.set_label("161"), "starcap161")
        (j,) = self.files(".json")
        (lg,) = self.files(".log")
        self.assertTrue(j.endswith("_starcap161.json"))
        rec = load(os.path.join(self.dir, j))
        self.assertEqual((rec["name"], rec["cpu_id"], rec["cpu_from"]), ("starcap161", CPU161, "lookup"))
        self.assertEqual(rec["log"], lg)
        self.assertEqual(len(self.hub.history), 1)
        self.assertIs(self.hub.history[0], self.hub.boot.rec)
        self.assertEqual(self.hub.history[0]["name"], "starcap161")

    def test_label_is_sanitised_into_a_filename(self):
        self.hub.set_label("../../etc/pass wd")
        self.feed(UNKNOWN)
        self.wait(5)
        (j,) = self.files(".json")
        self.assertEqual(j.split("_", 1)[1], "etcpasswd.json")
        self.assertTrue(all(os.path.isfile(os.path.join(self.dir, f)) for f in os.listdir(self.dir)))

    def test_normalise_name(self):
        n = ramcheck.normalise_name
        for s in ("95", "starcap95", "StarCap 95", "sc95", "#95", " starcap095 "):
            self.assertEqual(n(s), "starcap95", s)
        self.assertEqual(n("WALEEDTEST1"), "waleedtest1")
        self.assertEqual(n("a/b\\c..d"), "abcd")
        self.assertIsNone(n("  "))
        self.assertIsNone(n(None))

    def test_two_power_ons_in_one_second_dont_overwrite(self):
        self.feed(DQ12)
        self.wait(3)
        self.feed(UNKNOWN)
        self.wait(5)
        self.assertEqual(len(self.files(".json")), 2)
        self.assertEqual(len(self.hub.history), 2)
        self.assertEqual([r["title"] for r in self.hub.history], ["FAULTY RAM", "FAULTY RAM"])

    def test_reopened_power_on_rewrites_its_own_record(self):
        self.feed(BOOTED + "NanoPi-R76S login: \r\n")
        self.wait(10)
        self.assertEqual(self.hub.history[0]["status"], "pass")
        self.feed("[  612.2] Kernel panic - not syncing: Oops\r\n")
        self.wait(10)
        self.assertEqual(len(self.files(".json")), 1)
        self.assertEqual(len(self.hub.history), 1)
        self.assertEqual(self.hub.history[0]["title"], "RAM OK at boot, then CRASHED")

    def test_noise_only_power_on_is_saved(self):
        cut = "\x00" * 25
        self.feed(cut)                   # the cut of whatever board was on at start
        self.wait(bootlog.MERGE_S + 2)
        self.assertEqual(self.files(".log"), [])
        self.feed(cut)                   # a board that printed nothing, unplugged
        self.wait(bootlog.MERGE_S + 2)
        (lg,) = self.files(".log")
        with open(os.path.join(self.dir, lg)) as f:
            self.assertIn("25 bytes of line noise", f.read())
        self.assertEqual(self.hub.history[0]["status"], "nooutput")

    def test_history_reloads(self):
        self.feed(UNKNOWN)
        self.wait(5)
        again = Hub(log_dir=self.dir, lookup_fn=self.lookup)
        self.assertEqual(again.history[0]["title"], "FAULTY RAM")


class Identity(HubCase):
    def test_lookup_is_cached_not_run_per_read(self):
        self.hub.set_label("starcap161")
        self.feed(DQ12 + UNKNOWN)
        self.wait(5)
        with self.hub.lock:
            for _ in range(50):
                self.hub.snapshot()
        self.assertEqual(self.lookup.calls, [("starcap161", None)])

    def test_board_said_cpu_beats_label_and_flags_mismatch(self):
        self.hub.set_label("95")
        self.feed(BOOTED + "\r\nCPUID=" + CPU161 + "\r\n")
        ident = self.hub.identity()
        self.assertEqual(ident["board_cpu_id"], CPU161)
        self.assertIn("you typed starcap95", ident["mismatch"])

    def test_verdict_events_only_on_change(self):
        q, _ = self.hub.subscribe()
        self.feed(DQ12)
        msgs = [q.get_nowait().decode() for _ in range(q.qsize())]
        self.assertEqual(len([m for m in msgs if m.startswith("event: line")]), 28)
        self.assertLess(len([m for m in msgs if m.startswith("event: verdict")]), 14)
        # A booting kernel prints hundreds of lines that change nothing: no verdicts.
        self.feed(BOOTED)
        q.queue.clear()
        self.feed("".join(f"[    2.{i:06d}] some driver: probed\r\n" for i in range(200)))
        msgs = [q.get_nowait().decode() for _ in range(q.qsize())]
        self.assertEqual(len([m for m in msgs if m.startswith("event: line")]), 200)
        self.assertEqual([m for m in msgs if m.startswith("event: verdict")], [])

    def test_final_verdict_event_says_finished(self):
        q, _ = self.hub.subscribe()
        self.feed(UNKNOWN)
        self.wait(5)
        evs = [q.get_nowait().decode() for _ in range(q.qsize())]
        last = [json.loads(m.split("data: ", 1)[1]) for m in evs if m.startswith("event: verdict")][-1]
        self.assertTrue(last["finished"])
        self.assertEqual(last["verdict"]["status"], "fail")

    def test_stalled_client_is_dropped_not_silently_lossy(self):
        q, _ = self.hub.subscribe()
        for i in range(2100):
            self.hub.emit("line", {"id": 1, "text": str(i)})
        self.assertTrue(q.dead)
        self.assertNotIn(q, self.hub._clients)


class Login(HubCase):
    def setUp(self):
        super().setUp()
        self.hub.can_write = True
        self.feed(BOOTED)

    def test_full_conversation(self):
        on = lambda s: self.hub.on_data(s, self.t)  # noqa: E731
        self.assertEqual(on("\r\nNanoPi-R76S login: "), b"pi\n")
        self.assertEqual(on("pi\r\nPassword: "), b"12345\n")
        self.assertIsNone(on("\r\n"))
        self.assertEqual(on("\r\nLogin incorrect\r\nNanoPi-R76S login: "), b"root\n")
        self.assertEqual(on("root\r\nPassword: "), b"fa\n")
        self.assertIsNone(on("\r\nWelcome to Ubuntu\r\n# of updates\r\n"))
        self.assertEqual(on("\x1b[?2004h\x1b[01;31mroot@NanoPi-R76S\x1b[00m:~# "), READ_CMD.encode())
        # the shell echoes the command: no cpu_id from that
        self.assertIsNone(on(READ_CMD.replace("\n", "\r\n")))
        self.assertIsNone(self.hub.boot.cpu_id)
        self.assertEqual(on("\r\nCPUID=" + CPU161 + "\r\nSTARCAP_NAME=starcap161\r\nroot@NanoPi-R76S:~# "),
                         b"exit\n")
        self.assertEqual((self.hub.boot.cpu_id, self.hub.boot.name), (CPU161, "starcap161"))
        self.assertIsNone(on("exit\r\nlogout\r\n\r\nNanoPi-R76S login: "))
        self.assertEqual(self.hub.bot.state()["step"], "done")

    def test_no_echo_means_one_username_and_silence(self):
        sent = [self.hub.on_data("\r\nNanoPi-R76S login: ", self.t)]
        for i in range(20):
            sent.append(self.hub.on_data("", self.t + i))
        self.wait(30)
        sent.append(self.hub.on_data("[   40.1] usb 1-1: new device\r\n", self.t))
        self.assertEqual([s for s in sent if s], [b"pi\n"])

    def test_gives_up_after_both_logins(self):
        on = lambda s: self.hub.on_data(s, self.t)  # noqa: E731
        sent = [on("\r\nNanoPi-R76S login: "), on("pi\r\nPassword: "),
                on("\r\nLogin incorrect\r\nNanoPi-R76S login: "), on("root\r\nPassword: "),
                on("\r\nLogin incorrect\r\nNanoPi-R76S login: "), on("x\r\nNanoPi-R76S login: ")]
        self.assertEqual(sent, [b"pi\n", b"12345\n", b"root\n", b"fa\n", None, None])

    def test_kernel_serial_means_no_login(self):
        self.feed("[    1.2] rockchip-cpuinfo cpuinfo: Serial\t\t: " + CPU161 + "\r\n")
        self.assertIsNone(self.hub.on_data("\r\nNanoPi-R76S login: ", self.t))

    def test_read_only_adapter_never_types(self):
        self.hub.can_write = False
        self.assertIsNone(self.hub.on_data("\r\nNanoPi-R76S login: ", self.t))

    def test_new_power_on_gets_a_fresh_bot(self):
        self.assertEqual(self.hub.on_data("\r\nNanoPi-R76S login: ", self.t), b"pi\n")
        self.t += 3
        self.feed(BOOTED)
        self.assertEqual(self.hub.on_data("\r\nNanoPi-R76S login: ", self.t), b"pi\n")

    def test_same_prompt_answered_once(self):
        b, bot = bootlog.Boot(), AutoLogin()
        self.assertEqual(bot.react(b, "R76S login: "), b"pi\n")
        self.assertIsNone(bot.react(b, "R76S login: "))


class SerialReader(unittest.TestCase):
    """The reader on ptys behind ttyUSB* symlinks in a temp dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ramcheck-tty-")
        self.logs = os.path.join(self.dir, "logs")
        self.hub = Hub(log_dir=self.logs, lookup_fn=FakeLookup())
        self.stop = threading.Event()
        self.masters = []

    def tearDown(self):
        self.stop.set()
        for m in self.masters:
            try:
                os.close(m)
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def pty(self, name):
        m, s = os.openpty()
        path = os.ttyname(s)
        os.close(s)                       # the reader opens it by name, like a real adapter
        self.masters.append(m)
        link = os.path.join(self.dir, name)
        os.symlink(path, link)
        return m, link, path

    def start(self):
        threading.Thread(target=ramcheck.reader,
                         args=(self.hub, (os.path.join(self.dir, "ttyUSB*"),), self.stop),
                         daemon=True).start()

    def lines(self):
        return self.hub.boot.lines if self.hub.boot else []

    def test_reads_types_and_survives_reenumeration(self):
        m0, link0, _ = self.pty("ttyUSB0")
        self.start()
        self.assertTrue(wait_for(lambda: self.hub.port == link0))
        self.assertTrue(self.hub.can_write)
        os.write(m0, BOOTED.encode())
        self.assertTrue(wait_for(lambda: self.hub.boot is not None and self.hub.boot.stage == "kernel"))
        os.write(m0, b"\r\nNanoPi-R76S login: ")
        got = b""
        end = time.monotonic() + 3
        while b"\n" not in got and time.monotonic() < end:
            try:
                got += os.read(m0, 100)
            except BlockingIOError:
                time.sleep(0.02)
        self.assertEqual(got, b"pi\n")
        # The board's USB is plugged: the adapter drops and comes back as ttyUSB1.
        os.unlink(link0)
        m1, link1, _ = self.pty("ttyUSB1")
        os.close(m0)
        self.masters.remove(m0)
        self.assertTrue(wait_for(lambda: self.hub.port == link1))
        first = self.hub.boot
        os.write(m1, DQ12.encode())
        self.assertTrue(wait_for(lambda: self.hub.boot is not first and self.hub.boot.errors == 2))
        self.assertIn("dropped off", " ".join(first.notes))

    def test_port_disappears_mid_boot(self):
        m0, link0, _ = self.pty("ttyUSB0")
        self.start()
        self.assertTrue(wait_for(lambda: self.hub.port == link0))
        os.write(m0, "".join(DQ12.splitlines(keepends=True)[:9]).encode())
        self.assertTrue(wait_for(lambda: len(self.lines()) == 9))
        os.unlink(link0)
        os.close(m0)
        self.masters.remove(m0)
        self.assertTrue(wait_for(lambda: self.hub.port is None and self.hub.finished))
        v = self.hub.boot.verdict(True)
        self.assertEqual(v["status"], "warn")          # not FAULTY: the log was cut
        self.assertTrue(wait_for(lambda: self.hub.port_error == ramcheck.NO_ADAPTER))
        self.assertTrue(wait_for(lambda: len(self.hub.history) == 1))

    def test_read_only_adapter(self):
        m0, link0, path = self.pty("ttyUSB0")
        os.chmod(path, 0o444)
        self.start()
        self.assertTrue(wait_for(lambda: self.hub.port == link0))
        self.assertFalse(self.hub.can_write)
        os.write(m0, UNKNOWN.encode())
        self.assertTrue(wait_for(lambda: self.hub.boot is not None and self.hub.boot.errors == 2))

    def test_no_permission_says_run_install(self):
        p = os.path.join(self.dir, "ttyUSB0")
        open(p, "w").close()
        os.chmod(p, 0)
        if os.access(p, os.R_OK):
            self.skipTest("running as root")
        self.start()
        self.assertTrue(wait_for(lambda: self.hub.port_error and "./install.sh" in self.hub.port_error))
        self.assertIsNone(self.hub.port)

    def test_busy_port_is_named(self):
        m0, link0, path = self.pty("ttyUSB0")
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        try:
            self.start()
            self.assertTrue(wait_for(lambda: self.hub.port_error and "busy" in self.hub.port_error))
        finally:
            # A pty keeps its tty (and the flag) while the master lives; a real
            # adapter's tty is freed when the other program exits. Same effect:
            fcntl.ioctl(fd, termios.TIOCNXCL)
            os.close(fd)
        self.assertTrue(wait_for(lambda: self.hub.port == link0))

    def test_no_adapter(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.hub.port_error == ramcheck.NO_ADAPTER))

    def test_find_port_order(self):
        for n in ("ttyACM0", "ttyUSB10", "ttyUSB2"):
            open(os.path.join(self.dir, n), "w").close()
        pats = (os.path.join(self.dir, "ttyUSB*"), os.path.join(self.dir, "ttyACM*"))
        self.assertEqual(ramcheck.find_port(pats), os.path.join(self.dir, "ttyUSB2"))


class Http(HubCase):
    def setUp(self):
        super().setUp()
        self.srv = ramcheck.serve(self.hub, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data) if data else None

    def test_state_and_label(self):
        self.assertNotEqual(self.port, 8776)
        code, s = self.req("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertIsNone(s["boot"])
        code, r = self.req("POST", "/api/label", json.dumps({"name": "161"}))
        self.assertEqual((code, r), (200, {"ok": True, "label": "starcap161"}))
        self.assertEqual(self.req("POST", "/api/label", "{not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/label", "[1]")[0], 400)
        self.assertEqual(self.req("POST", "/api/label", json.dumps({"name": 5}))[1]["label"], None)

    def test_lookup_and_log(self):
        code, r = self.req("GET", "/api/lookup?name=161")
        self.assertEqual(r["cpu_id"], CPU161)
        self.feed(UNKNOWN)
        self.wait(5)
        log = self.hub.history[0]["log"]
        code, r = self.req("GET", "/api/log?f=" + log)
        self.assertEqual(code, 200)
        self.assertIn("ERR", r["lines"])
        self.assertEqual(self.req("GET", "/api/log?f=../../../etc/passwd")[0], 404)
        self.assertEqual(self.req("GET", "/api/log?f=" + log[:-4] + ".json")[0], 404)

    def test_events_stream(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/events")
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")

        def event():
            kind = data = None
            while True:
                ln = r.fp.readline().decode().rstrip("\n")
                if ln.startswith("event: "):
                    kind = ln[7:]
                elif ln.startswith("data: "):
                    data = json.loads(ln[6:])
                elif ln == "" and kind:
                    return kind, data

        self.assertEqual(event()[0], "state")
        self.hub.on_data(UNKNOWN.splitlines(keepends=True)[0], self.t)
        kinds = [event() for _ in range(3)]
        self.assertEqual([k for k, _ in kinds], ["boot", "line", "verdict"])
        self.assertEqual(kinds[0][1]["id"], 1)
        self.assertTrue(kinds[1][1]["text"].startswith("DDR e10b4dfeaf"))
        self.assertEqual(set(kinds[2][1]), {"id", "stage", "verdict", "identity", "finished"})
        c.close()


class Replay(unittest.TestCase):
    def run_replay(self, path):
        return subprocess.run([sys.executable, os.path.join(APP, "ramcheck.py"), "--replay", path],
                              capture_output=True, text=True, timeout=30)

    def test_fixture_exit_codes(self):
        for name, code, title in [("starcap95_dq12.log", 1, "FAULTY RAM"),
                                  ("starcap161_unknown_device.log", 1, "FAULTY RAM"),
                                  ("good_lpddr5_fresh_board.log", 2, "RAM OK, boot stopped at U-Boot")]:
            p = self.run_replay(os.path.join(FIX, name))
            self.assertEqual(p.returncode, code, name)
            self.assertEqual(p.stdout.splitlines()[0], title)

    def test_other_exit_codes(self):
        d = tempfile.mkdtemp(prefix="ramcheck-replay-")
        try:
            def write(name, text):
                p = os.path.join(d, name)
                with open(p, "w") as f:
                    f.write(text)
                return p
            self.assertEqual(self.run_replay(os.path.join(d, "missing.log")).returncode, 4)
            self.assertEqual(self.run_replay(write("empty.log", "")).returncode, 3)
            self.assertEqual(self.run_replay(write("noise.log", "\x00\x00\n")).returncode, 3)
            self.assertEqual(self.run_replay(write("ok.log", BOOTED + "NanoPi-R76S login: \n")).returncode, 0)
            # Two power-ons in one capture: each is reported, the worst sets the code.
            p = self.run_replay(write("two.log", BOOTED + "NanoPi-R76S login: \n" + UNKNOWN))
            self.assertEqual(p.returncode, 1)
            self.assertIn("power-on 2:", p.stdout)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()


import maskrom  # noqa: E402


class Maskrom(unittest.TestCase):
    """The Read CPU ID button: finding a board in maskrom and sending cpuid/."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.hub = Hub(log_dir=os.path.join(self.dir, "logs"), lookup_fn=FakeLookup())

    def fake_sysfs(self, vendor, bcd):
        d = os.path.join(self.dir, "sys", "1-1")
        os.makedirs(d, exist_ok=True)
        for k, v in {"idVendor": vendor, "idProduct": "350e", "busnum": "1",
                     "devnum": "14", "bcdDevice": bcd}.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")
        return os.path.join(self.dir, "sys")

    def test_find_board_modes(self):
        dev = maskrom.find_board(self.fake_sysfs("2207", "0100"))
        self.assertEqual((dev["id"], dev["mode"], dev["node"]),
                         ("2207:350e", "maskrom", "/dev/bus/usb/001/014"))
        self.assertEqual(maskrom.find_board(self.fake_sysfs("2207", "0223"))["mode"], "loader")
        self.assertIsNone(maskrom.find_board(self.fake_sysfs("1a86", "0264")))

    def run_job(self, dev, send):
        self.hub.set_usb(dev)
        ok, msg = self.hub.start_cpuid(send=send)
        for _ in range(100):
            if self.hub.cpuid_job["state"] != "running":
                break
            time.sleep(0.01)
        return ok, msg

    def test_sends_only_in_maskrom_with_access(self):
        calls = []
        send = lambda: (calls.append(1), (True, "Sent."))[1]  # noqa: E731
        base = {"id": "2207:350e", "node": "/dev/bus/usb/001/014"}
        self.assertFalse(self.hub.start_cpuid(send=send)[0])              # nothing on USB
        self.assertIn("loader mode", self.run_job(dict(base, mode="loader", writable=True), send)[1])
        self.assertIn("install.sh", self.run_job(dict(base, mode="maskrom", writable=False), send)[1])
        self.assertEqual(calls, [])
        ok, _ = self.run_job(dict(base, mode="maskrom", writable=True), send)
        self.assertTrue(ok)
        self.assertEqual(calls, [1])
        self.assertEqual(self.hub.snapshot()["cpuid_job"]["state"], "done")

    def test_failure_is_reported(self):
        dev = {"id": "2207:350e", "node": "x", "mode": "maskrom", "writable": True}
        self.run_job(dev, lambda: (False, "upgrade_tool: Download boot failed"))
        self.assertEqual(self.hub.cpuid_job["state"], "error")
        self.assertIn("failed", self.hub.snapshot()["cpuid_job"]["msg"])
