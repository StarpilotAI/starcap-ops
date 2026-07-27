#!/usr/bin/env python3
"""
fix_usb_enclosure.py — make the flaky-USB-enclosure workaround permanent.
=========================================================================

Applies two kernel quirks to ONE USB enclosure (by vendor:product id), now and across
every future reboot:

  c  = USB_QUIRK_NO_LPM   — no U1/U2 link power management for this device.
       LPM puts the USB 3 link to sleep between transfers, and the wake-up timing
       margin is far tighter at Gen 2 (10 Gbps) than Gen 1 (5 Gbps). Realtek bridges
       are notorious for missing that window, which is why the same enclosure can read
       at 200 MB/s when it happens to negotiate 5000 and collapse at 10000.
  u  = IGNORE_UAS         — use the older BOT transport instead of UAS. Slower ceiling,
       much more tolerant of a bridge that mishandles queued commands.

WHY IT IS NOT JUST A modprobe.d FILE
------------------------------------
`usbcore` is normally BUILT INTO the kernel, not a loadable module. A
`/etc/modprobe.d/*.conf` line saying `options usbcore quirks=...` is therefore read by
nobody and fails SILENTLY — it looks applied and does nothing. The `c` quirk has to go
on the kernel command line via the bootloader. `usb-storage` IS a module, so it can take
either route; this script puts both on the command line so there is one place to look.

Built-in code still exposes its parameters under /sys/module/<name>/parameters, and
those are writable, so the same quirks can be applied at RUNTIME with no reboot — they
take effect on the next enumeration, i.e. unplug and replug the drive.

USAGE
  sudo python3 fix_usb_enclosure.py                 # apply to 0bda:9210 (Ugreen RTL9210)
  sudo python3 fix_usb_enclosure.py 174c:55aa       # some other enclosure
  python3 fix_usb_enclosure.py --status             # show what is applied right now
  sudo python3 fix_usb_enclosure.py --remove        # undo the persistent change
  sudo python3 fix_usb_enclosure.py --yes           # skip the confirmation prompt
  sudo python3 fix_usb_enclosure.py --runtime-only  # this boot only, no bootloader edit

Safe to run twice: it rewrites its own tokens rather than appending duplicates, and it
backs up /etc/default/grub before touching it. Nothing here touches any drive's DATA —
it only changes how the kernel talks to the enclosure.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

DEFAULT_ID = "0bda:9210"        # Realtek RTL9210 — the Ugreen M.2 NVMe enclosure
GRUB_FILE = "/etc/default/grub"
GRUB_KEY = "GRUB_CMDLINE_LINUX_DEFAULT"
MODPROBE_FILE = "/etc/modprobe.d/99-usb-enclosure-quirks.conf"
RUNTIME = {"usbcore": "c", "usb_storage": "u"}     # /sys/module/<k>/parameters/quirks
ID_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")


def sh(cmd, timeout=300):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def need_root():
    if os.geteuid() != 0:
        sys.exit("This changes system config — re-run it with sudo:\n"
                 f"  sudo python3 {os.path.abspath(__file__)} {' '.join(sys.argv[1:])}")


# ---------------------------------------------------------------- inspection
def module_is_builtin(name):
    """True when `name` is compiled into the kernel rather than loadable. A modprobe.d
    `options` line for a built-in is read by nobody, so this decides whether the quirk
    can live in modprobe.d at all."""
    rc, _ = sh(f"lsmod | awk '{{print $1}}' | grep -qx {name}")
    return rc != 0


def runtime_quirks():
    out = {}
    for mod in RUNTIME:
        try:
            with open(f"/sys/module/{mod}/parameters/quirks") as fh:
                out[mod] = fh.read().strip()
        except OSError:
            out[mod] = None
    return out


def grub_cmdline():
    try:
        with open(GRUB_FILE) as fh:
            for line in fh:
                m = re.match(rf'\s*{GRUB_KEY}\s*=\s*"(.*)"\s*$', line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def connected(dev_id):
    """-> [(usb_path, speed)] for every connected instance of this vendor:product."""
    vid, pid = dev_id.lower().split(":")
    found = []
    for d in glob.glob("/sys/bus/usb/devices/*/"):
        def rd(f):
            try:
                with open(os.path.join(d, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return None
        if (rd("idVendor"), rd("idProduct")) == (vid, pid):
            found.append((os.path.basename(d.rstrip("/")), rd("speed") or "?"))
    return found


def show_status(dev_id):
    print(f"    target device      : {dev_id}")
    print(f"    usbcore            : "
          f"{'BUILT INTO KERNEL (modprobe.d would be ignored)' if module_is_builtin('usbcore') else 'loadable module'}")
    print(f"    usb_storage        : "
          f"{'built into kernel' if module_is_builtin('usb_storage') else 'loadable module'}")
    for mod, val in runtime_quirks().items():
        state = "(not set)" if val == "" else (val if val is not None else "unavailable")
        print(f"    runtime {mod:<11}: {state}")
    cl = grub_cmdline()
    print(f"    {GRUB_KEY}: {cl if cl is not None else 'could not read ' + GRUB_FILE}")
    try:
        active = " ".join(t for t in open("/proc/cmdline").read().split() if "quirks=" in t)
    except OSError:
        active = ""
    print(f"    active in /proc/cmdline: {active or '(none)'}")
    print(f"    modprobe.d file    : {'present' if os.path.exists(MODPROBE_FILE) else 'absent'}")
    inst = connected(dev_id)
    print(f"    connected now      : "
          + (", ".join(f"port {p} at {s} Mbps" for p, s in inst) if inst else "not plugged in"))


# ---------------------------------------------------------------- mutation
def strip_our_tokens(cmdline):
    """Drop any quirks tokens we manage, so re-running is idempotent and switching to a
    different vendor:product does not leave the old one behind."""
    return " ".join(t for t in cmdline.split()
                    if not re.match(r"^(usbcore|usb-storage|usb_storage)\.quirks=", t)).strip()


def apply_runtime(dev_id, remove=False):
    """Write the quirks to the live module parameters. Takes effect at the next
    enumeration, so the drive must be unplugged and replugged."""
    out = []
    for mod, flag in RUNTIME.items():
        want = "" if remove else f"{dev_id}:{flag}"
        try:
            with open(f"/sys/module/{mod}/parameters/quirks", "w") as fh:
                fh.write(want)
            out.append((mod, want or "(cleared)", True))
        except OSError as e:
            out.append((mod, f"{type(e).__name__}: {e}", False))
    return out


def apply_persistent(dev_id, remove=False, assume_yes=False):
    cl = grub_cmdline()
    if cl is None:
        print(f"  ! could not read {GRUB_KEY} from {GRUB_FILE} — skipping the persistent")
        print(f"    part. The runtime change above still applies until reboot.")
        return False
    new = strip_our_tokens(cl)
    if not remove:
        new = f"{new} usbcore.quirks={dev_id}:c usb-storage.quirks={dev_id}:u".strip()
    new = re.sub(r"\s+", " ", new).strip()
    if new == cl:
        print("  = bootloader already exactly as wanted; nothing to change.")
        return True

    print(f"\n  {GRUB_FILE}:")
    print(f"    before: {GRUB_KEY}=\"{cl}\"")
    print(f"    after : {GRUB_KEY}=\"{new}\"")
    if not assume_yes:
        try:
            if input("\n  Apply this bootloader change? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  skipped the bootloader edit (runtime change still applied).")
                return False
        except EOFError:
            print("  no tty to confirm on — re-run with --yes to apply non-interactively.")
            return False

    backup = f"{GRUB_FILE}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(GRUB_FILE, backup)
    print(f"  backed up {GRUB_FILE} -> {backup}")
    with open(GRUB_FILE) as fh:
        text = fh.read()
    text = re.sub(rf'(\s*{GRUB_KEY}\s*=\s*)".*"',
                  lambda m: f'{m.group(1)}"{new}"', text, count=1)
    with open(GRUB_FILE, "w") as fh:
        fh.write(text)

    # usb-storage is a module, so a modprobe.d line is a genuine second belt for the `u`
    # quirk (it also covers a usb-storage loaded from the initramfs). usbcore is NOT put
    # there: for a built-in, modprobe.d is silently ignored and would only mislead.
    if remove:
        if os.path.exists(MODPROBE_FILE):
            os.remove(MODPROBE_FILE)
            print(f"  removed {MODPROBE_FILE}")
    else:
        with open(MODPROBE_FILE, "w") as fh:
            fh.write("# managed by tools/fix_usb_enclosure.py — see that script for why\n"
                     f"options usb-storage quirks={dev_id}:u\n")
        print(f"  wrote {MODPROBE_FILE}")

    print("  running update-grub …")
    rc, out = sh("update-grub 2>&1 || grub-mkconfig -o /boot/grub/grub.cfg 2>&1")
    for line in out.strip().splitlines()[-4:]:
        print(f"      {line}")
    if rc != 0:
        print(f"  ! update-grub exited {rc} — the bootloader may NOT be updated.")
        print(f"    Your original is at {backup}")
        return False
    rc2, _ = sh("update-initramfs -u 2>&1")
    print(f"  update-initramfs: {'ok' if rc2 == 0 else f'exited {rc2} (non-fatal here)'}")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Permanently apply no-LPM + BOT quirks to a flaky USB enclosure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="default device is 0bda:9210 (Realtek RTL9210 / Ugreen M.2 NVMe)")
    ap.add_argument("device_id", nargs="?", default=DEFAULT_ID,
                    help="USB vendor:product, e.g. 0bda:9210")
    ap.add_argument("--status", action="store_true", help="show current state and exit")
    ap.add_argument("--remove", action="store_true", help="undo the change")
    ap.add_argument("--runtime-only", action="store_true",
                    help="this boot only; do not touch the bootloader")
    ap.add_argument("--yes", action="store_true", help="do not prompt")
    args = ap.parse_args()

    if not ID_RE.match(args.device_id):
        sys.exit(f"device id must look like 0bda:9210, got {args.device_id!r}")

    if args.status:
        print(f"\n  STATUS for {args.device_id}")
        show_status(args.device_id)
        return 0

    need_root()
    print(f"\n  {'REMOVING' if args.remove else 'APPLYING'} USB quirks for {args.device_id}")
    print("\n  before:")
    show_status(args.device_id)

    print("\n  runtime (live module parameters):")
    for mod, val, ok in apply_runtime(args.device_id, remove=args.remove):
        print(f"    {'OK ' if ok else 'ERR'} {mod}: {val}")

    persisted = True
    if not args.runtime_only:
        persisted = apply_persistent(args.device_id, remove=args.remove,
                                     assume_yes=args.yes)

    print("\n" + "=" * 68)
    if args.remove:
        print("  Quirks removed. UNPLUG AND REPLUG for the runtime change to take effect;")
        print("  the bootloader change applies from the next reboot.")
    else:
        print("  NEXT STEP — quirks apply at ENUMERATION, so:")
        print("     1. unplug the drive")
        print("     2. plug it back in")
        print("     3. verify:  lsusb -t | grep 'Mass Storage'    (want Driver=usb-storage)")
        print("                 sudo python3 drive_health.py      (want HEALTHY)")
        if not persisted:
            print("\n  NOTE: only the runtime change is in place — it is lost on reboot.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
