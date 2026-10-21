"""bootlog: the per-power-on verdict (Boot) and the power-on boundaries (Splitter)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bootlog  # noqa: E402
from bootlog import Boot, Splitter  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8", errors="replace") as f:
        return f.read()


def lines(name):
    return fixture(name).splitlines(keepends=True)


def boot_of(text):
    b = Boot()
    for raw in text.splitlines():
        b.feed(raw)
    return b


GOOD = fixture("good_lpddr5_fresh_board.log")
DQ12 = fixture("starcap95_dq12.log")
UNKNOWN = fixture("starcap161_unknown_device.log")
CUT = "\x00" * 18 + "\ufffd" + "\x00" * 7       # a real power cut (0xE0 decodes to U+FFFD)
LOGIN_TAIL = ("Starting kernel ...\r\n[    0.000000] Booting Linux on physical CPU 0x0\r\n"
              "Ubuntu 24.04 LTS NanoPi-R76S ttyFIQ0\r\n")


def first_attempt(text):
    """Everything up to and including the first ERR."""
    out = []
    for raw in text.splitlines(keepends=True):
        out.append(raw)
        if raw.strip() == "ERR":
            break
    return "".join(out)


class Fixtures(unittest.TestCase):
    def test_dq12_is_faulty_with_one_deduped_reason(self):
        v = boot_of(DQ12).verdict(True)
        self.assertEqual(v["status"], "fail")
        self.assertEqual(v["title"], "FAULTY RAM")
        dq = [d for d in v["details"] if "DQ" in d]
        # dq7 (0x80 -> 0x1080) and dq15 (0x8000 -> 0x9000, twice) all mean DQ12 stuck at 1
        self.assertEqual(len(dq), 1)
        self.assertTrue(dq[0].startswith("ch0 data line DQ12 reads 1 where 0 was written"))
        self.assertIn("dq7", dq[0])
        self.assertIn("dq15", dq[0])
        self.assertIn("failed 2 of 2 attempts", v["details"])

    def test_unknown_device_is_faulty(self):
        b = boot_of(UNKNOWN)
        self.assertEqual((b.attempts, b.errors), (2, 2))
        v = b.verdict(True)
        self.assertEqual(v["status"], "fail")
        self.assertIn("RAM chip not detected at all (Rockchip's hint: ch0 solder joints)", v["details"])

    def test_good_board_cut_at_uboot(self):
        b = boot_of(GOOD)
        self.assertTrue(b.trained)
        self.assertEqual(b.stage, "uboot")
        self.assertEqual(b.channels, {0: 2048, 1: 2048})
        self.assertEqual(b.dram_size, "4 GiB")
        self.assertEqual(b.verdict(False)["status"], "running")
        v = b.verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertEqual(v["title"], "RAM OK, boot stopped at U-Boot")


class Verdicts(unittest.TestCase):
    def test_two_errs_are_final_even_while_live(self):
        # The blob gives up after its one retry: no need to wait for silence.
        self.assertEqual(boot_of(DQ12).verdict(False)["status"], "fail")

    def test_one_err_while_live_is_not_faulty_yet(self):
        b = boot_of(first_attempt(DQ12))
        v = b.verdict(False)
        self.assertEqual(v["status"], "running")
        self.assertNotIn("FAULTY", v["title"])
        # ...but a board that hung after that ERR is final
        self.assertEqual(b.verdict(True)["status"], "fail")

    def test_partial_capture_is_never_faulty(self):
        # Banner and chip sizing seen, then nothing: still running, and once quiet
        # it is "did not finish", not FAULTY RAM.
        b = boot_of("".join(lines("starcap95_dq12.log")[:9]))
        self.assertEqual(b.verdict(False)["status"], "running")
        v = b.verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertEqual(v["title"], "RAM check did not finish")
        b.cut = True
        self.assertIn("the power went", " ".join(b.verdict(True)["details"]))

    def test_marginal_failed_then_passed_on_retry(self):
        retry = GOOD.replace("DDR 2f85f4b2d4 cym 25/03/04-14:38.55,fwver: v1.09\r\nIn\r\n",
                             "DDR 2f85f4b2d4 cym 25/03/04-14:38.55,fwver: v1.09\r\nIn\r\npd/pu vd_ddr\r\n")
        b = boot_of(first_attempt(DQ12) + retry + LOGIN_TAIL + "NanoPi-R76S login: \r\n")
        v = b.verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertIn("MARGINAL", v["title"])
        self.assertTrue(any(d.startswith("ch0 data line DQ12") for d in v["details"]))

    def test_reached_login_is_pass(self):
        b = boot_of(GOOD + LOGIN_TAIL)
        b.prompt("NanoPi-R76S login: ")
        self.assertEqual(b.stage, "login")
        self.assertEqual(b.verdict(False)["status"], "pass")
        self.assertEqual(b.verdict(True)["title"], "RAM OK, booted to Linux")

    def test_crash_after_boot_is_warn(self):
        b = boot_of(GOOD + LOGIN_TAIL + "NanoPi-R76S login: \n"
                    "[  612.1] Unable to handle kernel paging request at virtual address dead\n"
                    "[  612.2] Kernel panic - not syncing: Oops: Fatal exception\n")
        v = b.verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertEqual(v["title"], "RAM OK at boot, then CRASHED")
        self.assertTrue(v["details"][0].startswith("crash: "))

    def test_power_pulled_after_ram_ok_is_pass(self):
        b = boot_of(GOOD)
        b.cut = True
        v = b.verdict(True)
        self.assertEqual(v["status"], "pass")
        self.assertEqual(v["title"], "RAM OK (power pulled at U-Boot)")

    def test_noise_only_is_nooutput(self):
        b = Boot()
        b.noise("\x00\x00\x00")
        self.assertEqual(b.lines, [])
        self.assertEqual(b.verdict(False)["status"], "running")
        v = b.verdict(True)
        self.assertEqual(v["status"], "nooutput")
        self.assertEqual(v["title"], "Power seen, but no boot log")

    def test_text_without_any_stage_is_nooutput(self):
        b = boot_of("some text\nmore text\n")
        self.assertEqual(b.verdict(True)["status"], "nooutput")
        self.assertEqual(b.verdict(True)["title"], "No RAM check in this log")

    def test_log_started_mid_boot_says_so(self):
        v = boot_of(LOGIN_TAIL + "NanoPi-R76S login: \n").verdict(True)
        self.assertEqual(v["status"], "pass")
        self.assertTrue(any("started mid-boot" in d for d in v["details"]))


class Parsing(unittest.TestCase):
    def test_bad_bits(self):
        self.assertEqual(bootlog.bad_bits("0x80", "0x1080"), [(12, 1)])
        self.assertEqual(bootlog.bad_bits("0x8000", "0x9000"), [(12, 1)])
        self.assertEqual(bootlog.bad_bits("0xff", "0xfe"), [(0, 0)])
        self.assertEqual(bootlog.bad_bits("0x1", "0x100000001"), [(32, 1)])   # past 32 bits too
        self.assertEqual(bootlog.bad_bits("0x5", "0x5"), [])

    def test_dq_fail_with_no_differing_bit_still_fails(self):
        v = boot_of("DDR e10b4dfeaf x 26/04/29,fwver: v1.13\nch:1 dq3 fail,write:0x8,read:0x8\n"
                    "error\nERR\n").verdict(True)
        self.assertEqual(v["status"], "fail")
        self.assertIn("ch1 dq3 test failed (wrote 0x8, read 0x8)", v["details"])

    def test_serial_from_kernel_and_login(self):
        b = boot_of("[    1.2] rockchip-cpuinfo cpuinfo: Serial\t\t: 9c27e6a31661bcad\r\n")
        self.assertEqual(b.cpu_id, "9c27e6a31661bcad")
        b = boot_of("\nCPUID=dda6ac2324f7074f\nSTARCAP_NAME=starcap161\n")
        self.assertEqual((b.cpu_id, b.name), ("dda6ac2324f7074f", "starcap161"))

    def test_serial_ignores_echo_and_garbage(self):
        from ramcheck import READ_CMD
        prompt = "pi@NanoPi-R76S:~$ "
        for junk in [prompt + READ_CMD,                        # the shell's echo of the command
                     READ_CMD[:70], READ_CMD[70:],             # ...wrapped at the terminal width
                     "CPUID=",                                 # file missing: empty answer
                     "STARCAP_NAME=",
                     "PreSerial: 0, raw, 0x2ad40000",          # U-Boot
                     "PreSerial: 0123456789abcdef",
                     "CPUID=0123456789abcdef0",                # 17 hex
                     "xCPUID=0123456789abcdef",
                     "CPUID=0123456789abcdeg"]:
            b = boot_of(junk)
            self.assertIsNone(b.cpu_id, junk)
            self.assertIsNone(b.name, junk)

    def test_clean_strips_wire_junk(self):
        self.assertEqual(bootlog.clean("\x00\x00DDR\r"), "DDR")
        self.assertEqual(bootlog.clean("\x1b[?2004h\x1b[01;32mpi@R76S\x1b[00m:~$ "), "pi@R76S:~$ ")
        self.assertEqual(bootlog.clean("a�b\x7fc\x85d"), "abcd")

    def test_is_noise(self):
        self.assertTrue(bootlog.is_noise("\x00\x00"))
        self.assertTrue(bootlog.is_noise("��}\r"))
        self.assertFalse(bootlog.is_noise("\r"))          # blank line
        self.assertFalse(bootlog.is_noise("In\r"))        # the blob's 2-letter line
        self.assertFalse(bootlog.is_noise("\x00DDR e10b4dfeaf"))

    def test_lines_are_capped(self):
        b = Boot()
        for i in range(bootlog.MAX_LINES + 5):
            b.feed(f"line {i}")
        b.feed("Kernel panic - not syncing")
        self.assertEqual(len(b.lines), bootlog.MAX_LINES)
        self.assertEqual(b.dropped, 6)
        self.assertEqual(len(b.crashes), 1)               # still parsed


class Run:
    """Feeds a Splitter like the serial reader would, with a fake clock."""

    def __init__(self):
        self.sp = Splitter()
        self.t = 1000.0
        self.events = []

    def feed(self, text, step=0.002):
        for raw in text.splitlines(keepends=True):
            self.events += self.sp.feed(raw, self.t)
            self.t += step
        return self

    def wait(self, seconds, step=0.25):
        end = self.t + seconds
        while self.t < end:
            self.t = min(end, self.t + step)
            self.events += self.sp.tick(self.t)
        return self

    def boots(self):
        return [e[1] for e in self.events if e[0] == "boot"]

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


class Boundaries(unittest.TestCase):
    def test_blob_retry_is_one_power_on(self):
        r = Run().feed(DQ12).wait(5)
        self.assertEqual(len(r.boots()), 1)
        b = r.boots()[0]
        self.assertEqual((b.attempts, b.errors), (2, 2))
        self.assertEqual(len(r.kinds("final")), 1)
        self.assertEqual(b.verdict(True)["status"], "fail")

    def test_retry_banner_after_a_pause_is_a_new_power_on(self):
        # Hung after one ERR, power pulled and replugged 4 s later.
        r = Run().feed(first_attempt(DQ12)).wait(4).feed(UNKNOWN)
        self.assertEqual(len(r.boots()), 2)

    def test_boot_rom_rerun_from_sd_is_one_power_on(self):
        # starcap95, 2026-10-21 14:19:46: fail, pass ("out"), then the boot ROM ran
        # the RAM program again from the SD card and it failed: one power-on.
        r = Run().feed(fixture("starcap95_fail_pass_fail_sd.log")).wait(10)
        self.assertEqual(len(r.boots()), 1)
        b = r.boots()[0]
        self.assertEqual((b.attempts, b.errors), (3, 2))
        v = b.verdict(True)
        self.assertEqual((v["status"], v["title"]), ("fail", "FAULTY RAM (intermittent)"))

    def test_pass_then_rerun_means_the_bootloader_died(self):
        # starcap95, 14:03:47: two clean passes, no SPL. The bootloader runs in RAM.
        one = fixture("good_lpddr5_fresh_board.log").split("U-Boot SPL board init")[0]
        r = Run().feed(one).feed(one.replace("In\n", "In\npd/pu vd_ddr\n", 1)).wait(10)
        self.assertEqual(len(r.boots()), 1)
        v = r.boots()[0].verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertIn("bootloader never started", v["title"])

    def test_power_cycle_after_a_hang_is_a_new_power_on(self):
        r = Run().feed(UNKNOWN).wait(10).feed("\x00\x00").wait(2).feed(DQ12).wait(5)
        a, b = r.boots()
        self.assertEqual(a.verdict(True)["status"], "fail")
        self.assertEqual(b.verdict(True)["status"], "fail")
        self.assertEqual((b.attempts, b.noise_bytes), (2, 2))   # the NULs open the next power-on
        self.assertNotIn("\x00", "".join(b.lines))

    def test_quick_replug_while_still_live_splits_and_marks_cut(self):
        r = Run().feed(GOOD).wait(1).feed(DQ12)
        a, b = r.boots()
        self.assertTrue(a.cut)
        self.assertEqual(r.kinds("final")[0][1], a)
        self.assertEqual(a.verdict(True)["title"], "RAM OK (power pulled at U-Boot)")

    def test_nul_glued_to_the_banner(self):
        r = Run().feed(UNKNOWN)          # the real capture starts "\x00DDR ..."
        b = r.boots()[0]
        self.assertTrue(b.lines[0].startswith("DDR e10b4dfeaf"))
        self.assertEqual(len(r.boots()), 1)

    def test_idle_at_login_then_panic_stays_in_the_same_power_on(self):
        r = Run().feed(GOOD + LOGIN_TAIL)
        r.events += r.sp.feed("NanoPi-R76S login: ", r.t)
        r.wait(300)                                   # five quiet minutes at the prompt
        self.assertEqual(len(r.kinds("final")), 1)
        b = r.boots()[0]
        self.assertEqual(b.verdict(True)["status"], "pass")
        r.feed("\n[  612.2] Kernel panic - not syncing: Oops: Fatal exception\n").wait(10)
        self.assertEqual(len(r.boots()), 1)
        self.assertEqual(len(r.kinds("reopen")), 1)
        self.assertEqual(len(r.kinds("final")), 2)
        self.assertEqual(b.verdict(True)["title"], "RAM OK at boot, then CRASHED")

    def test_noise_then_banner_within_merge_window_is_one_power_on(self):
        r = Run().feed("\x00\x00\x00").wait(4).feed(DQ12)
        self.assertEqual(len(r.boots()), 1)
        self.assertEqual(r.boots()[0].noise_bytes, 3)

    def test_first_cut_after_start_belongs_to_the_board_already_on(self):
        r = Run().feed(CUT).wait(bootlog.MERGE_S + 1)
        self.assertEqual(r.boots(), [])
        # ...after that, a cut with no power-on before it is a board that printed nothing
        r.feed(CUT).wait(bootlog.MERGE_S + 1)
        (b,) = r.boots()
        self.assertEqual(len(r.kinds("final")), 1)
        self.assertEqual(b.verdict(True)["status"], "nooutput")

    def test_first_noise_after_a_power_on_is_its_cut_not_a_record(self):
        r = Run().feed(GOOD + LOGIN_TAIL).feed("NanoPi-R76S login: \n").wait(10)
        r.feed(CUT).wait(30)                          # the power cut, slow replug
        (b,) = r.boots()
        self.assertTrue(b.cut)
        # A second cut with no banner before it: a power-on that printed nothing.
        r.feed(CUT).wait(30)
        self.assertEqual(len(r.boots()), 2)
        self.assertEqual(r.boots()[1].verdict(True)["status"], "nooutput")
        # ...and the next cut is that record's own, not another record.
        r.feed(CUT).wait(30)
        self.assertEqual(len(r.boots()), 2)

    def test_stray_bytes_are_not_power_events(self):
        r = Run().feed(DQ12).wait(10)
        for burst in ("\x00", "\x00\x00\x0f", "\x00"):   # seen on a real idle line
            r.feed(burst).wait(60)
        (b,) = r.boots()
        self.assertFalse(b.cut)
        self.assertEqual(b.noise_bytes, 5)
        self.assertEqual(len(r.kinds("final")), 1)

    def test_unterminated_noise_is_picked_up_by_tick(self):
        r = Run().feed(DQ12).wait(5)
        r.events += r.sp.feed(CUT, r.t)              # a cut's NULs: no newline ever
        r.wait(bootlog.MERGE_S + 2)
        self.assertEqual(r.sp.buf, "")
        self.assertTrue(r.boots()[0].cut)

    def test_real_timed_capture(self):
        """tests/fixtures/timed_cut_then_dq12.raw: a receive-only capture with a
        '### HH:MM:SS' stamp before each burst that followed 2 s of silence. The
        tool started with a board already on, its cut came 23 s later, the DQ12
        board's power-on 14 s after that, then three 1-3 byte glitches over 2 min."""
        import re
        with open(os.path.join(FIX, "timed_cut_then_dq12.raw"), "rb") as f:
            parts = re.split(rb"\n### (\d\d):(\d\d):(\d\d) [^\n]*\n", f.read())
        r = Run()
        r.t = 3600 * 12 + 60 * 30 + 24                 # when the capture opened the port
        for i in range(1, len(parts), 4):
            t = 3600 * int(parts[i]) + 60 * int(parts[i + 1]) + int(parts[i + 2])
            if r.t < t:
                r.wait(t - r.t)
            r.events += r.sp.feed(parts[i + 3].decode("utf-8", "replace"), t)
        r.wait(120)
        (b,) = r.boots()
        self.assertEqual((b.attempts, b.errors), (2, 2))
        self.assertEqual(b.verdict(True)["status"], "fail")
        self.assertEqual(len(r.kinds("final")), 1)
        self.assertFalse(b.cut)

    def test_prompt_then_cut_then_banner_on_one_line(self):
        r = Run().feed(GOOD + LOGIN_TAIL)
        r.events += r.sp.feed("NanoPi-R76S login: ", r.t)
        r.wait(10)
        r.events += r.sp.feed("\x00\x00" + DQ12, r.t + 3)
        a, b = r.boots()
        self.assertEqual(a.lines[-1], "NanoPi-R76S login:")
        self.assertTrue(b.lines[0].startswith("DDR e10b4dfeaf"))
        self.assertEqual(b.attempts, 2)

    def test_quiet_limits_by_stage(self):
        r = Run().feed("".join(lines("starcap95_dq12.log")[:5])).wait(3.5)
        self.assertEqual(r.kinds("final"), [])
        r.wait(1)
        self.assertEqual(len(r.kinds("final")), 1)    # RAM program silent 4 s = hung

        r = Run().feed(GOOD).wait(29)                 # U-Boot can sit loading a while
        self.assertEqual(r.kinds("final"), [])
        r.wait(2)
        self.assertEqual(len(r.kinds("final")), 1)

        r = Run().feed(GOOD + LOGIN_TAIL).wait(59)    # a quiet kernel up to the prompt
        self.assertEqual(r.kinds("final"), [])

    def test_adapter_lost_mid_boot(self):
        r = Run().feed("".join(lines("starcap95_dq12.log")[:9]))
        r.events += r.sp.detach("adapter dropped")
        a = r.boots()[0]
        self.assertEqual(len(r.kinds("final")), 1)
        v = a.verdict(True)
        self.assertEqual(v["status"], "warn")           # not FAULTY: we just lost the log
        self.assertIn("adapter dropped", v["details"])
        # Whatever comes after the adapter is back is a new power-on, banner or not.
        r.feed("LPDDR5, 2736MHz\nout\nU-Boot SPL board init\n")
        self.assertEqual(len(r.boots()), 2)

    def test_detach_when_already_closed_adds_no_note(self):
        r = Run().feed(DQ12).wait(5)
        r.events += r.sp.detach("adapter dropped")
        self.assertEqual(r.boots()[0].notes, [])
        self.assertEqual(len(r.kinds("final")), 1)


if __name__ == "__main__":
    unittest.main()


class MaskromId(unittest.TestCase):
    """Output of cpuid/ (the SRAM program sent in maskrom)."""

    def test_identified(self):
        b = Boot()
        for raw in ["\x00\r\n", "CPUID=ed013a06c1afc69f\r\n",
                    "OTP=0b30557a9fc4e90e33587da2c7ec1136\r\n", "CPUID=ed013a06c1afc69f\r\n"]:
            b.feed(raw)
        v = b.verdict(True)
        self.assertEqual((v["status"], b.cpu_id), ("id", "ed013a06c1afc69f"))
        self.assertEqual(b.otp_raw, "0b30557a9fc4e90e33587da2c7ec1136")

    def test_leading_zero_dropped_by_llx(self):
        b = Boot()
        b.feed("CPUID=d013a06c1afc69f\r\n")              # 15 digits
        self.assertEqual(b.cpu_id, "d013a06c1afc69f")

    def test_fuse_read_timeout_is_not_an_id(self):
        b = Boot()
        b.feed("CPUID_ERROR=otp-timeout\r\n")
        v = b.verdict(True)
        self.assertEqual(v["status"], "warn")
        self.assertIsNone(b.cpu_id)

    def test_echoed_command_is_not_an_id(self):
        b = Boot()
        b.feed("pi@NanoPi-R76S:~$ echo CPUID=$(tr -d '\\000' </proc/device-tree/serial-number)\r\n")
        self.assertIsNone(b.cpu_id)
