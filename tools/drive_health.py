#!/usr/bin/env python3
"""
drive_health.py — is this drive/enclosure fit to work with? Answer in ~30 seconds.
=================================================================================

Run this the moment you plug a drive in, BEFORE the audit, before draining, before
formatting. It tells you HEALTHY / MARGINAL / FAILING and, when something is wrong,
which fix to apply. The point is to never again lose an hour discovering mid-audit that
the enclosure cannot hold a read.

READ-ONLY and non-destructive: it reads a little data and counts kernel errors. It never
writes to the drive, never mounts or unmounts, never formats.

WHY (real cost, 2026-07-27)
---------------------------
A Ugreen RTL9210 (0bda:9210) USB-NVMe enclosure passed enumeration instantly every
time — `lsusb`, `lsblk`, even `ls` on the mount all looked perfect — while being unable
to complete sustained reads. Three drives went through it. What that cost:
  * an `ssd_audit.py` run blocked in D-state for minutes at a time (zero CPU, every read
    hitting the full 30 s SCSI timeout, then a bus reset);
  * an audit that "completed" having silently seen 4 of 24 device folders and called the
    drive clean;
  * another that scanned 274 GB of a 749 GB volume;
  * hours of unplug/replug/reboot guesswork.
Enumeration health tells you NOTHING about read health. That is the whole reason this
exists: only a real timed read, cross-checked against the kernel's error counters,
distinguishes "fine" from "about to waste your afternoon".

WHAT IT MEASURES
  1. transport      — negotiated link speed, driver (uas vs usb-storage), LPM state.
                      Gen 2 (10 Gbps) has much tighter LPM wake timing than Gen 1;
                      marginal bridges pass at 5000 and fail at 10000.
  2. error baseline — uas aborts / bus resets / I/O errors already in the kernel log.
  3. timed reads    — a sequential bulk read plus scattered small reads, each under a
                      hard timeout, run via `dd` in a subprocess. A blocked read cannot
                      be killed from inside Python (D state ignores signals), so the
                      read must be abandonable — hence the subprocess.
  4. error delta    — new kernel errors caused by our own reads. This is the tell.

USAGE
  python3 drive_health.py /media/you/STARSSD3      # a mounted drive
  python3 drive_health.py /dev/sda                 # a block device (no mount needed)
  python3 drive_health.py                          # every removable drive it can find

  exit 0 = HEALTHY,  1 = MARGINAL,  2 = FAILING

Linux only (it reads sysfs and the kernel log). Needs only Python 3.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time

BULK_MB = 64          # sequential read size
BULK_TIMEOUT = 25     # seconds; a healthy USB SSD does 64 MB in well under 2
SMALL_READS = 6       # scattered small reads, to catch region-specific failures
SMALL_TIMEOUT = 12
MIN_OK_MBPS = 20      # below this, something is retrying even if nothing errored

ERR_RE = re.compile(r"uas_eh_abort|uas_eh_device_reset|I/O error, dev sd|"
                    r"reset SuperSpeed|Synchronize Cache.*failed|MEDIUM_ERROR", re.I)


def sh(cmd, timeout=20):
    """-> (rc, stdout). Never raises; a timeout is reported as rc=124 like GNU timeout."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as e:
        return 1, str(e)


def kernel_errors():
    """-> count of transport/media error lines in this boot's kernel log."""
    rc, out = sh("journalctl -k -b --no-pager 2>/dev/null || dmesg 2>/dev/null", timeout=25)
    if rc == 124:
        return None
    return sum(1 for line in out.splitlines() if ERR_RE.search(line))


# ---------------------------------------------------------------- topology
def block_device_for(path):
    """-> (/dev/sdX, mountpoint or None) for a mount path or a device path."""
    if path.startswith("/dev/"):
        return re.sub(r"\d+$", "", path), None
    rc, out = sh(f"findmnt -n -o SOURCE,TARGET --target {path!r}")
    if rc == 0 and out.strip():
        src, _, tgt = out.split()[0], None, out.split()[-1]
        return re.sub(r"\d+$", "", src), tgt
    return None, None


def usb_info(dev):
    """-> dict describing the USB link behind /dev/sdX, or {} if it isn't USB.

    Walks up from the block device to the owning USB device so the port number is never
    hardcoded — it changes on every replug, which is exactly how a copy-pasted
    `/sys/bus/usb/devices/6-1/speed` goes stale and looks like the drive vanished.
    """
    name = os.path.basename(dev)
    real = os.path.realpath(f"/sys/block/{name}")
    info = {}
    node = real
    while node not in ("/", "/sys", ""):
        if os.path.exists(os.path.join(node, "idVendor")):
            def rd(f):
                try:
                    with open(os.path.join(node, f)) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            info = {
                "usb_path": os.path.basename(node),
                "vid_pid": f"{rd('idVendor')}:{rd('idProduct')}",
                "product": rd("product"), "manufacturer": rd("manufacturer"),
                "speed_mbps": rd("speed"), "max_power": rd("bMaxPower"),
            }
            for f in ("usb3_hardware_lpm_u1", "usb3_hardware_lpm_u2"):
                p = os.path.join(node, "power", f)
                if os.path.exists(p):
                    info[f] = rd(os.path.join("power", f))
            break
        node = os.path.dirname(node)
    if info:
        rc, out = sh("lsusb -t")
        m = [l for l in out.splitlines() if "Mass Storage" in l]
        info["driver"] = next((re.search(r"Driver=(\S+?),", l).group(1)
                               for l in m if re.search(r"Driver=(\S+?),", l)), "?")
    return info


# ---------------------------------------------------------------- read tests
def timed_read(dev, offset_mb, size_mb, timeout):
    """-> (ok, seconds, mbps). Uses dd in a subprocess so a read blocked in
    uninterruptible I/O can be ABANDONED — signals do not reach a D-state task, so an
    in-process read would hang this tool for the full SCSI timeout, repeatedly."""
    cmd = f"dd if={dev} of=/dev/null bs=1M count={size_mb} skip={offset_mb} 2>/dev/null"
    t0 = time.monotonic()
    rc, _ = sh(cmd, timeout=timeout)
    el = time.monotonic() - t0
    if rc == 124:
        return False, el, 0.0
    if rc != 0:
        return False, el, 0.0
    return True, el, (size_mb / el if el > 0 else 0.0)


def size_mb_of(dev):
    rc, out = sh(f"blockdev --getsize64 {dev} 2>/dev/null")
    try:
        return int(out.strip()) // (1024 * 1024)
    except (ValueError, AttributeError):
        return 0


def file_targets(mnt, want):
    """-> up to `want` real files on the mount, biggest-first, for the no-root fallback.

    Bounded and interruptible: os.walk here can itself block on a dying drive, so this
    runs under `timeout` in a subprocess rather than in-process.
    """
    rc, out = sh(f"timeout 20 find {mnt!r} -maxdepth 5 -type f "
                 f"\\( -name '*.mp4' -o -name '*.csv' \\) -size +8M 2>/dev/null | head -{want}",
                 timeout=25)
    return [l for l in out.splitlines() if l.strip()] if rc in (0, 124) else []


def read_probe(dev, mnt=None):
    """Bulk read at the start, then scattered reads across the whole device — including
    the very end, where the exFAT backup superblock lives. A bridge can serve the first
    sectors happily and die further in; a single read at offset 0 misses that entirely.

    Raw device reads need root. Without it we fall back to reading real FILES off the
    mount, which is a weaker probe (the filesystem may serve them from cache, and we
    cannot choose where on the platter they live) but still catches a bridge that cannot
    sustain a read. The report says which mode ran, so a clean result is never mistaken
    for stronger evidence than it is.
    """
    results = []
    raw_ok = os.access(dev, os.R_OK)
    if raw_ok:
        total = size_mb_of(dev)
        ok, el, mbps = timed_read(dev, 0, BULK_MB, BULK_TIMEOUT)
        results.append(("bulk @0", 0, BULK_MB, ok, el, mbps))
        if total > BULK_MB * 2:
            for i in range(SMALL_READS):
                # spread across the device, last one deliberately near the very end
                off = (int(total * (i + 1) / (SMALL_READS + 1))
                       if i < SMALL_READS - 1 else total - 4)
                ok, el, mbps = timed_read(dev, off, 4, SMALL_TIMEOUT)
                results.append((f"raw @{off // 1024}GiB", off, 4, ok, el, mbps))
        return results, "raw device"

    if not mnt:
        return [], "unavailable (no root for raw reads, and not mounted)"
    files = file_targets(mnt, SMALL_READS + 1)
    if not files:
        return [], "unavailable (no root, and no readable files found on the mount)"
    for i, f in enumerate(files):
        sz = BULK_MB if i == 0 else 4
        to = BULK_TIMEOUT if i == 0 else SMALL_TIMEOUT
        cmd = f"dd if={f!r} of=/dev/null bs=1M count={sz} 2>/dev/null"
        t0 = time.monotonic()
        rc, _ = sh(cmd, timeout=to)
        el = time.monotonic() - t0
        ok = rc == 0
        results.append((("bulk " if i == 0 else "") + os.path.basename(f)[:18],
                        0, sz, ok, el, (sz / el if ok and el > 0 else 0.0)))
    return results, "files on the mount (no root — weaker probe)"


# ---------------------------------------------------------------- verdict
def assess(usb, probes, err_before, err_after):
    """-> (verdict, [reasons], [remedies]). FAILING beats MARGINAL beats HEALTHY."""
    reasons, remedies = [], []
    failed = [p for p in probes if not p[3]]
    bulk = next((p for p in probes if p[0].startswith("bulk")), None)
    new_errs = None if (err_before is None or err_after is None) else err_after - err_before

    verdict = "HEALTHY"

    def worse(v):
        nonlocal verdict
        order = {"HEALTHY": 0, "MARGINAL": 1, "FAILING": 2}
        if order[v] > order[verdict]:
            verdict = v

    if not probes:
        worse("MARGINAL")
        reasons.append("could not run any read test — re-run with sudo for a raw-device "
                       "probe, or mount the drive first")
    if failed:
        worse("FAILING")
        reasons.append(f"{len(failed)}/{len(probes)} timed read(s) failed or timed out: "
                       + ", ".join(p[0] for p in failed))
    if new_errs:
        worse("FAILING" if new_errs >= 3 else "MARGINAL")
        reasons.append(f"our own reads produced {new_errs} new kernel transport error(s)")
    if bulk and bulk[3] and bulk[5] < MIN_OK_MBPS:
        worse("MARGINAL")
        reasons.append(f"bulk read only {bulk[5]:.0f} MB/s — something is retrying "
                       f"underneath even though the read completed")
    if err_before:
        worse("MARGINAL")
        reasons.append(f"{err_before} transport error(s) already in this boot's log "
                       f"(before we touched it)")

    if verdict != "HEALTHY" and usb:
        vp = usb.get("vid_pid")
        if usb.get("speed_mbps") == "10000":
            reasons.append("link negotiated 10000 Mbps (USB 3.2 Gen 2) — LPM wake timing "
                           "is far tighter here than at 5000, and marginal bridges pass "
                           "at Gen 1 while failing at Gen 2")
        if usb.get("driver") == "uas":
            remedies.append(f"disable LPM + force BOT for this device, then REPLUG:\n"
                            f"       echo -n '{vp}:c' | sudo tee /sys/module/usbcore/parameters/quirks\n"
                            f"       echo -n '{vp}:u' | sudo tee /sys/module/usb_storage/parameters/quirks")
        remedies.append("swap the cable — a plain USB 3.0 cable forces a 5 Gbps link, "
                        "which is often the whole fix (a better cable makes it WORSE here)")
        if usb.get("max_power") and usb["max_power"].rstrip("mA").isdigit() \
                and int(usb["max_power"].rstrip("mA")) > 500:
            remedies.append(f"try a POWERED hub — this enclosure asks for "
                            f"{usb['max_power']} against a 900 mA port ceiling")
        remedies.append("move the M.2 stick to a different enclosure — if the fixes above "
                        "do not take, the bridge is the fault and no software setting "
                        "will paper over it")
    return verdict, reasons, remedies


def check(path):
    dev, mnt = block_device_for(path)
    if not dev or not os.path.exists(dev):
        print(f"  ✗ could not resolve {path} to a block device")
        return 2
    usb = usb_info(dev)

    print(f"\n{'=' * 70}")
    print(f"  TARGET      : {path}   ->   {dev}" + (f"   (mounted at {mnt})" if mnt else ""))
    if usb:
        print(f"  ENCLOSURE   : {usb.get('manufacturer') or '?'} "
              f"{usb.get('product') or ''}  [{usb.get('vid_pid')}]")
        print(f"  LINK        : {usb.get('speed_mbps')} Mbps   driver={usb.get('driver')}"
              f"   port={usb.get('usb_path')}   maxpower={usb.get('max_power')}")
        lpm = [f"{k.split('_')[-1]}={v}" for k, v in usb.items() if "lpm" in k]
        if lpm:
            print(f"  LPM         : {' '.join(lpm)}")
    else:
        print("  ENCLOSURE   : not a USB device (internal disk?)")
    print(f"{'=' * 70}")

    err_before = kernel_errors()
    print(f"  transport errors already logged this boot: "
          f"{'unknown' if err_before is None else err_before}")
    print(f"  timed reads ({BULK_MB} MB sequential + {SMALL_READS} scattered) …")
    probes, mode = read_probe(dev, mnt)
    print(f"    probe mode: {mode}")
    for label, _off, sz, ok, el, mbps in probes:
        print(f"    {label:<14} {sz:>4} MB  "
              + (f"✓ {el:5.2f}s  {mbps:6.1f} MB/s" if ok
                 else f"✗ FAILED after {el:5.1f}s"))
    err_after = kernel_errors()
    if err_before is not None and err_after is not None:
        print(f"  new transport errors caused by these reads: {err_after - err_before}")

    verdict, reasons, remedies = assess(usb, probes, err_before, err_after)
    icon = {"HEALTHY": "✅", "MARGINAL": "⚠", "FAILING": "⛔"}[verdict]
    print(f"\n  {icon} {verdict}")
    for r in reasons:
        print(f"     - {r}")
    if verdict == "HEALTHY":
        print("     Reads are clean and fast. Safe to audit / drain / format.")
    else:
        print("\n  DO NOT trust an audit from this drive in its current state — an "
              "unreadable\n  folder is indistinguishable from a folder that isn't there. "
              "Fix, then re-run.")
    if remedies:
        print("\n  TRY, in this order:")
        for i, r in enumerate(remedies, 1):
            print(f"   {i}. {r}")
    return {"HEALTHY": 0, "MARGINAL": 1, "FAILING": 2}[verdict]


def removable_drives():
    out = []
    for d in sorted(glob.glob("/sys/block/sd*")):
        name = os.path.basename(d)
        if "usb" in os.path.realpath(d):
            out.append(f"/dev/{name}")
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Is this drive/enclosure fit to work with? Verdict in ~30 seconds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit 0 = HEALTHY,  1 = MARGINAL,  2 = FAILING")
    ap.add_argument("target", nargs="*",
                    help="mount path or /dev/sdX (default: every USB drive found)")
    args = ap.parse_args()
    targets = args.target or removable_drives()
    if not targets:
        print("No USB drives found. Pass a path explicitly.")
        return 1
    return max(check(t) for t in targets)


if __name__ == "__main__":
    sys.exit(main())
