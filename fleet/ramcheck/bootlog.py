"""
Boot-log parser for a NanoPi R76S (RK3576) debug UART.

Turns the raw console text of ONE power-on into a verdict about the board's RAM
(Boot), and cuts a live, timed stream of console text into power-ons (Splitter).
Both are pure: no serial port, no clock. Callers pass the time in.

What the console prints, in order, and what each part proves:

  DDR e10b4dfeaf ... fwver: v1.13      the RAM program (DDR blob) starts
  LPDDR5, 2736MHz                      it identified the chip
  channel[0] ... Size=2048MB           ...and sized each half (channel)
  ch:0 dq15 fail,write:..,read:..      a data-line test failed  -> FAIL
  unknown device / ... soldering ...   the chip did not answer  -> FAIL
  error / ERR                          this attempt gave up
  pd/pu vd_ddr                         it power-cycles the RAM and retries once
  out                                  training finished        -> RAM OK
  U-Boot SPL board init ... DRAM: 4 GiB ... Starting kernel ... login:

Signatures seen on real boards (2026-10):
  161, 138, 163  unknown device + "it may be ch0 soldering abnormality", twice, hang
  95             LPDDR5 found, then ch0 DQ12 reads 1 where 0 was written, twice, hang
  good board     LPDDR5 2736MHz 2x2048MB -> out -> U-Boot -> DRAM: 4 GiB
"""
import re

BANNER = re.compile(r"\bDDR ([0-9a-f]{10}) .*?fwver: (v[\d.]+)")
DRAM_TYPE = re.compile(r"^(LPDDR\dX?|DDR\d), (\d+)MHz")
CHANNEL = re.compile(r"^channel\[(\d+)\].*?Size=(\d+)MB")
DQ_FAIL = re.compile(r"ch:(\d+) dq(\d+) fail,write:(0x[0-9a-fA-F]+),read:(0x[0-9a-fA-F]+)")
SOLDER = re.compile(r"may be (ch\d+) soldering abnormality")
UNAVAILABLE = re.compile(r"channel (\d+) unavailable")
UBOOT = re.compile(r"^U-Boot 20\d\d")
DRAM_SIZE = re.compile(r"^DRAM:\s+([\d.]+ [GM]iB)")
KERNEL = re.compile(r"Starting kernel|Booting Linux on physical CPU|Linux version \d")
# The fused SoC serial, which the recorder uses as cpu_id. rockchip-cpuinfo prints it
# at boot ("cpuinfo: Serial\t\t: <16 hex>", only at a verbose console loglevel), and
# the serial-console login prints it as "CPUID=<16 hex>". Both are anchored to the
# whole line: the shell's echo of the login's own command line contains "CPUID=$(",
# and "PreSerial: 0, raw, ..." from U-Boot must not count either.
SERIAL = re.compile(r"(?:\bSerial\s*:\s*|^CPUID=)([0-9a-f]{16})$")
NAME = re.compile(r"^STARCAP_NAME=(starcap\w+)$")
LOGIN = re.compile(r"login:\s*$")
CRASH = re.compile(r"Kernel panic|Internal error: Oops|SError Interrupt|Bad page state|"
                   r"Unable to handle kernel|BUG: soft lockup|BUG: Bad page")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")   # colours / bracketed paste in shell prompts

STAGES = ["power", "ram", "spl", "uboot", "kernel", "login"]
STAGE_LABEL = {"power": "Power on", "ram": "RAM check", "spl": "Bootloader (SPL)",
               "uboot": "U-Boot", "kernel": "Linux", "login": "Login prompt"}
MAX_LINES = 20000        # a board left panicking for an hour must not eat the laptop's RAM
MAX_CRASHES = 20

# Power-on boundaries (Splitter). Seconds.
RETRY_GAP_S = 1.5        # "ERR" then the blob's own retry banner arrives faster than this
MERGE_S = 6.0            # noise this long before a banner is that power-on's start
PARTIAL_S = 0.5          # an unterminated noise burst (a cut's NULs) counts after this
# A real power cut sprays ~25 bytes (NULs and the odd 0xE0) with no newline, and
# the next power-on starts clean at its banner. 1-3 byte glitches also turn up on
# an idle line now and then: those are not power events.
MIN_BURST = 4
# Silence that closes a power-on, by the furthest stage it reached. The RAM program
# prints without pause, so 4 s there is a hang; a quiet kernel can be silent for a
# long time before the login prompt.
QUIET_S = {"power": 4.0, "ram": 4.0, "spl": 30.0, "uboot": 30.0, "kernel": 60.0, "login": 5.0}


def clean(line):
    """Drop the NULs, CRs, colour codes and stray control bytes a power-on sprays on
    the wire (bytes that were not valid UTF-8 arrive as U+FFFD and go too)."""
    line = ANSI.sub("", line.replace("\x00", "").replace("\r", ""))
    return "".join(ch for ch in line
                   if ch == "\t" or (ch >= " " and ch != "\x7f" and not "\x80" <= ch <= "\x9f"
                                     and ch != "�"))


def is_noise(raw):
    """Bytes but no words: what a power cut or a glitch puts on the wire."""
    if not raw.strip(" \t\r\n"):
        return False                      # a blank line is not noise
    return len(re.findall(r"[A-Za-z0-9]", clean(raw))) < 2


def bad_bits(write, read):
    """[(bit, value_read)] for each bit of a DQ test word that came back different."""
    w, r = int(write, 16), int(read, 16)
    diff = w ^ r
    return [(bit, r >> bit & 1) for bit in range(diff.bit_length()) if diff >> bit & 1]


class Boot:
    """Everything one power-on said, and what it means."""

    def __init__(self):
        self.lines = []
        self.dropped = 0             # lines past MAX_LINES (parsed, not kept)
        self.noise_bytes = 0         # line noise seen (power cut / glitches)
        self.attempts = 0            # DDR banners seen (1 + retries)
        self.blob = None             # "e10b4dfeaf v1.13"
        self.dram_type = None        # "LPDDR5"
        self.dram_mhz = None
        self.channels = {}           # {0: 2048, 1: 2048}
        self.dq_fails = []           # [(ch, dq, write, read)]
        self.unknown_device = False
        self.solder_hint = set()     # {"ch0"}
        self.unavailable = set()
        self.errors = 0              # attempts that ended in ERR
        self.trained = False         # "out" after a banner
        self.stage = "power"
        self.dram_size = None        # U-Boot's "DRAM: 4 GiB"
        self.cpu_id = None
        self.name = None
        self.crashes = []
        self.cut = False             # the Splitter saw the power go (noise then silence, or a new banner)
        self.notes = []              # extra verdict lines from the reader ("adapter dropped off")
        # Filled in by the live reader (ramcheck.Hub), not by parsing.
        self.id = None
        self.started = None
        self.stamp = None
        self.base = None             # logs/<stamp>_<name> once saved
        self.rec = None              # the history record once saved

    def _reach(self, stage):
        if STAGES.index(stage) > STAGES.index(self.stage):
            self.stage = stage

    def feed(self, raw):
        line = clean(raw).strip()
        if not line:
            return
        if len(self.lines) < MAX_LINES:
            self.lines.append(line)
        else:
            self.dropped += 1
        m = BANNER.search(line)
        if m:
            self.attempts += 1
            self.blob = f"{m.group(1)} {m.group(2)}"
            self._reach("ram")
            return
        m = DRAM_TYPE.search(line)
        if m:
            self.dram_type, self.dram_mhz = m.group(1), int(m.group(2))
        m = CHANNEL.search(line)
        if m:
            self.channels[int(m.group(1))] = int(m.group(2))
        for m in DQ_FAIL.finditer(line):
            f = (int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4).lower())
            if f not in self.dq_fails:
                self.dq_fails.append(f)
        if "unknown device" in line:
            self.unknown_device = True
        for m in SOLDER.finditer(line):
            self.solder_hint.add(m.group(1))
        m = UNAVAILABLE.search(line)
        if m:
            self.unavailable.add(int(m.group(1)))
        if line == "ERR":
            self.errors += 1
        if line == "out" and self.attempts:
            self.trained = True
        if "U-Boot SPL board init" in line or line.startswith("U-Boot SPL 20"):
            self.trained = self.trained or bool(self.attempts)
            self._reach("spl")
        if UBOOT.search(line):
            self._reach("uboot")
        m = DRAM_SIZE.search(line)
        if m:
            self.dram_size = m.group(1)
        if KERNEL.search(line):
            self._reach("kernel")
        m = SERIAL.search(line)
        if m:
            self.cpu_id = m.group(1)
        m = NAME.search(line)
        if m:
            self.name = m.group(1)
        if LOGIN.search(line):
            self._reach("login")
        if CRASH.search(line) and len(self.crashes) < MAX_CRASHES:
            self.crashes.append(line)

    def noise(self, raw):
        """A noise burst that belongs to this power-on: count it, keep any residue."""
        self.noise_bytes += len(raw.strip("\r\n"))
        self.feed(raw)

    def prompt(self, partial):
        """An unterminated line (a login prompt never ends in a newline)."""
        if LOGIN.search(clean(partial)):
            self._reach("login")

    @property
    def text_seen(self):
        return bool(self.lines)

    def _ram_reasons(self):
        why = []
        if self.unknown_device:
            ch = ", ".join(sorted(self.solder_hint)) or "ch0"
            why.append(f"RAM chip not detected at all (Rockchip's hint: {ch} solder joints)")
        # Several dq tests trip over the same broken line: one reason per (ch, bit, value).
        bits = {}
        for ch, dq, w, r in self.dq_fails:
            found = bad_bits(w, r)
            if not found:
                why.append(f"ch{ch} dq{dq} test failed (wrote {w}, read {r})")
            for bit, got in found:
                bits.setdefault((ch, bit, got), []).append(f"dq{dq}: wrote {w}, read {r}")
        for (ch, bit, got), tests in bits.items():
            why.append(f"ch{ch} data line DQ{bit} reads {got} where {1 - got} was written "
                       f"(test {'; '.join(tests)})")
        for ch in sorted(self.unavailable):
            why.append(f"channel {ch} unavailable")
        if not why and self.errors:
            why.append("RAM training ended in ERR")
        return why

    def verdict(self, finished=False):
        """{'status': pass|fail|warn|running|nooutput, 'title', 'details': [...]}.

        `finished` = the line went quiet (or the power went), so a board stuck
        mid-way is final. Until then only proof is final: the blob gives up after
        its one retry, so two ERRs are FAULTY RAM even while the line is live."""
        d = []
        if self.dram_type:
            d.append(f"RAM chip: {self.dram_type} at {self.dram_mhz} MHz")
        if self.channels:
            sizes = ", ".join(f"ch{c} {s} MB" for c, s in sorted(self.channels.items()))
            d.append(f"Halves found: {sizes} (total {sum(self.channels.values())} MB)")
        if self.blob:
            d.append(f"RAM program: {self.blob}")
        notes = list(self.notes)
        if self.dropped:
            notes.append(f"{self.dropped} later lines not kept (log over {MAX_LINES} lines)")
        tries = [f"failed {self.errors} of {self.attempts} attempts"] if self.attempts else []

        why = self._ram_reasons()
        if why and not self.trained:
            if not (finished or self.errors >= 2):
                return {"status": "running", "title": "RAM failing… (the RAM program retries once)",
                        "details": why + tries + d + notes}
            return {"status": "fail", "title": "FAULTY RAM",
                    "details": why + tries + d + notes +
                               ["Hardware fault on the board: RMA. Reflashing cannot fix it."]}

        if self.trained or self.stage in ("spl", "uboot", "kernel", "login"):
            d.insert(0, "RAM check passed")
            if not self.attempts:
                d.append("The RAM check itself is not in this log (it started mid-boot); "
                         "the board got past it, so it passed")
            if self.dram_size:
                d.append(f"U-Boot sees DRAM: {self.dram_size}")
            d += notes
            if why:
                return {"status": "warn", "title": "RAM MARGINAL: failed, then passed on retry",
                        "details": why + tries + [f"crash: {c}" for c in self.crashes[:5]] + d +
                                   ["The RAM failed its test once and passed the retry: an "
                                    "intermittent fault. RMA it, or Soak it before trusting it."]}
            if self.crashes:
                return {"status": "warn", "title": "RAM OK at boot, then CRASHED",
                        "details": [f"crash: {c}" for c in self.crashes[:5]] + d +
                                   ["Passed the power-on test but crashed later; this is the "
                                    "intermittent pattern starcap112 had. Soak it before trusting it."]}
            if self.stage == "login":
                return {"status": "pass", "title": "RAM OK, booted to Linux", "details": d}
            if not finished:
                return {"status": "running",
                        "title": f"RAM OK, booting… ({STAGE_LABEL[self.stage]})", "details": d}
            where = (f"at {STAGE_LABEL[self.stage]}" if self.stage != "ram"
                     else "before the bootloader")
            if self.cut:
                return {"status": "pass", "title": f"RAM OK (power pulled {where})",
                        "details": d + ["The power went before the boot finished; the RAM "
                                        "check itself passed."]}
            return {"status": "warn", "title": f"RAM OK, boot stopped {where}",
                    "details": d + ["The RAM is fine; the stop is elsewhere (eMMC/image)."]}

        if self.attempts:
            if not finished:
                return {"status": "running", "title": "Checking RAM…", "details": d + notes}
            how = ("the power went" if self.cut else
                   "it hung mid-check, or the log was cut off")
            return {"status": "warn", "title": "RAM check did not finish",
                    "details": d + notes +
                               [f"The RAM program started but never said pass or fail: {how}. "
                                "Power-cycle it again and leave it until the verdict shows."]}

        if self.lines:
            if not finished:
                return {"status": "running", "title": "Reading boot log…", "details": notes}
            return {"status": "nooutput", "title": "No RAM check in this log",
                    "details": notes + ["Text arrived, but no RAM program and no boot stage: the "
                                        "log started mid-boot, or the adapter is at the wrong "
                                        "baud (needs 1500000)"]}
        if finished:
            return {"status": "nooutput", "title": "Power seen, but no boot log",
                    "details": notes + ["Check: board TX -> adapter RX and GND -> GND",
                                        "Holding MASKROM skips the RAM check, so it prints nothing",
                                        "A board that dies before the RAM check (boot ROM hang) "
                                        "is silent too"]}
        return {"status": "running", "title": "Waiting for boot log…", "details": []}


class Splitter:
    """Cuts a timed stream of console text into power-ons.

    feed(text, now) and tick(now) return events, oldest first:
      ("boot", boot)          a new power-on began
      ("line", boot, text)    a line was added to that power-on
      ("reopen", boot)        a closed power-on printed again (a booted board idle at
                              its login prompt for minutes, then a kernel message)
      ("final", boot)         the power-on is over or went quiet: judge it, save it

    The rules, from what real boards do:
      - Only a DDR banner starts a power-on, except the blob's own retry: "ERR" and
        straight away a second banner (with "pd/pu vd_ddr") is the SAME power-on.
      - Text without a banner can only come from a board that was already running,
        so it continues the current power-on, however long it was quiet.
      - Noise (bytes, no words: a power cut sprays NULs) is held back. A banner
        within MERGE_S takes it as its start. Noise followed by silence is the
        current power-on's power cut the first time (marks it `cut`), and after
        that a power-on that printed nothing (a "nooutput" record): a dead board
        prints nothing at power-on, so its unplug is all we ever see of it. So
        one cut never makes a record of its own. Bursts under MIN_BURST bytes
        are glitches, never power events, and the first cut after the tool
        starts belongs to whatever board was already on.
    """

    def __init__(self):
        self.boot = None             # current Boot
        self.open = False            # current boot still listening (not judged final)
        self.buf = ""                # unterminated last line
        self.last_rx = None          # time of the last byte
        self.last_line_at = None     # time of the last line with words
        self.pending = []            # noise lines not yet given to a power-on
        self.pending_at = None
        self.fresh = False           # adapter reconnected: the next text is a new power-on
        self.virgin = True           # no power event seen since the tool started

    def feed(self, text, now):
        ev = []
        self.last_rx = now
        self.buf += text
        *done, self.buf = self.buf.split("\n")
        if len(self.buf) > 4096:     # wrong baud: garbage that never says newline
            done.append(self.buf)
            self.buf = ""
        for raw in done:
            self._line(raw, now, ev)
        if self.boot is not None and self.buf.strip() and not is_noise(self.buf):
            self._flush_pending(ev)  # a live prompt: the board is up, held noise was its
            self.boot.prompt(self.buf)
        return ev

    def tick(self, now):
        ev = []
        if self.last_rx is None:
            return ev
        quiet = now - self.last_rx
        if self.buf and quiet >= PARTIAL_S and is_noise(self.buf):
            self._hold(self.buf, self.last_rx)
            self.buf = ""
        if self.pending and now - self.pending_at >= MERGE_S and quiet >= PARTIAL_S:
            size = sum(len(r.strip("\r\n")) for r in self.pending)
            if size < MIN_BURST:                 # a glitch: keep it in the log, nothing more
                if self.boot is not None:
                    self._flush_pending(ev)
                self.pending, self.pending_at = [], None
            elif self.boot is None and self.virgin:
                self.pending, self.pending_at = [], None   # cut of a board on before we listened
            elif self.boot is not None and not self.boot.cut and not self.fresh:
                self._flush_pending(ev)          # the current power-on's power cut
                self.boot.cut = True
                self._close(ev)
            else:                                # a power-on that printed nothing
                self._new(ev)
                self._close(ev)
            if size >= MIN_BURST:
                self.virgin = False
        if self.open and quiet >= QUIET_S[self.boot.stage]:
            self._close(ev)
        return ev

    def detach(self, note=None):
        """The adapter went away. Its partial line is gone, and whatever arrives
        after it comes back is a new power-on: an adapter re-enumerating is
        usually the board's own USB being plugged, i.e. a power event."""
        ev = []
        if self.open and note:
            self.boot.notes.append(note)
        self._close(ev)
        self.buf = ""
        self.fresh = True
        return ev

    # -- internals
    def _line(self, raw, now, ev):
        text = clean(raw).strip()
        if not text and not is_noise(raw):
            return                               # blank line
        m = BANNER.search(text)
        if m and m.start():
            # "pi-r76s login: <NULs>DDR ..." : the prompt is the old power-on's tail.
            head = text[:m.start()].strip()
            if head and is_noise(head):
                self._hold(head, now)
            elif head and self.boot is not None:
                self._flush_pending(ev)
                self._add(head, ev)
            text = text[m.start():]
        if m:
            b = self.boot
            retry = (b is not None and self.open and not self.fresh and b.attempts == 1
                     and b.lines and b.lines[-1] == "ERR"
                     and now - self.last_line_at < RETRY_GAP_S)
            if not retry:
                self._new(ev)
            self._flush_pending(ev)
            self._add(text, ev)
            self.last_line_at = now
            return
        if is_noise(raw):
            self._hold(raw, now)
            return
        if self.boot is None or self.fresh:
            self._new(ev)                        # the log starts mid-boot
        elif not self.open:
            self.open = True
            ev.append(("reopen", self.boot))
        self.boot.cut = False                    # it is evidently still powered
        self._flush_pending(ev)
        self._add(text, ev)
        self.last_line_at = now

    def _hold(self, raw, now):
        if not self.pending:
            self.pending_at = now
        self.pending.append(raw)

    def _add(self, text, ev):
        before = len(self.boot.lines)
        self.boot.feed(text)
        if len(self.boot.lines) > before:
            ev.append(("line", self.boot, self.boot.lines[-1]))

    def _flush_pending(self, ev):
        for raw in self.pending:
            before = len(self.boot.lines)
            self.boot.noise(raw)
            if len(self.boot.lines) > before:
                ev.append(("line", self.boot, self.boot.lines[-1]))
        self.pending, self.pending_at = [], None

    def _new(self, ev):
        if self.open:
            self.boot.cut = True                 # a new power-on while the last was live
            self._close(ev)
        self.boot = Boot()
        self.open = True
        self.fresh = False
        self.virgin = False
        ev.append(("boot", self.boot))
        self._flush_pending(ev)

    def _close(self, ev):
        if self.open:
            self.open = False
            ev.append(("final", self.boot))
