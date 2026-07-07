#!/usr/bin/env python3
"""
StarCap drive footage-hours measurer  (standalone, READ-ONLY, OFFLINE)
=====================================================================

Plug in an SSD and this tells you, for the videos on it:
  * total video STORAGE (GB / TB)
  * total footage HOURS

Hours are measured the ACCURATE way — from the ACTUAL video files (ffprobe
reads each clip's real duration) — NOT from opm's contact sheet (which caps
long runs and under-reports). No cloud, no dashboard, no password.

It is READ-ONLY: it only reads file sizes and video durations. It never writes
to or deletes anything on the drive.

USAGE
-----
  python3 check_drive_hours.py "/media/you/Extreme SSD"   # a specific drive
  python3 check_drive_hours.py                            # auto-detect drives
  Windows:  py check_drive_hours.py "E:\\"

REQUIREMENTS
------------
  - Python 3
  - ffprobe (part of ffmpeg) for EXACT hours:
        Ubuntu/Debian : sudo apt install ffmpeg
        Mac (brew)    : brew install ffmpeg
        Windows       : https://ffmpeg.org/download.html  (add to PATH)
  Without ffprobe it still works, falling back to a byte-based ESTIMATE
  (~9.0 GB/hour, calibrated from real StarCap footage).
"""
import concurrent.futures
import os
import shutil
import subprocess
import sys

VIDEO_EXT = (".mp4", ".mkv", ".mov")
# Fallback only: measured bitrate of real StarCap stereo-HEVC footage (~9.0 GB/h,
# per-chunk stdev 0.04 — very stable). Used solely when ffprobe isn't installed.
GB_PER_HOUR = 9.0
WORKERS = 8
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", ".Trashes",
             ".Spotlight-V100", ".fseventsd", "found.000", "lost+found"}


def find_videos(root):
    """-> [(path, bytes)] for every video file on the drive (read-only walk)."""
    vids = []
    for dirpath, dirs, files in os.walk(root):
        # skip system/recycle dirs and hidden (dot) dirs incl. Linux .Trash-1000
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not d.startswith("$") and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(VIDEO_EXT) and not f.startswith("."):
                p = os.path.join(dirpath, f)
                try:
                    vids.append((p, os.path.getsize(p)))
                except OSError:
                    vids.append((p, 0))
    return vids


def ffprobe_seconds(path):
    """Real playback duration of one video, or None if it couldn't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", path],
            capture_output=True, text=True, timeout=120)
        s = out.stdout.strip()
        return float(s) if s else None
    except Exception:
        return None


def external_drives():
    """Best-effort auto-detect of plugged-in drives (per OS)."""
    out = []
    if os.name == "nt":
        import ctypes
        sysd = (os.environ.get("SystemDrive", "C:") or "C:")[0].upper()
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not (mask & (1 << i)):
                continue
            letter = chr(ord("A") + i)
            if letter == sysd:
                continue
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) in (2, 3):
                out.append(root)
    else:
        for base in ("/Volumes", "/media", "/run/media", "/mnt"):
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                p = os.path.join(base, entry)
                if not os.path.isdir(p):
                    continue
                if base in ("/media", "/run/media"):
                    for sub in (os.listdir(p) if os.access(p, os.R_OK) else []):
                        sp = os.path.join(p, sub)
                        if os.path.isdir(sp):
                            out.append(sp)
                else:
                    out.append(p)
    return out


def human(b):
    return f"{b/1e12:.2f} TB" if b >= 1e12 else f"{b/1e9:.1f} GB"


def fmt_hours(h):
    return f"{h:.1f} h  ({int(h)}h {int(round((h-int(h))*60))}m)"


def measure(root):
    print(f"\nScanning {root} …")
    vids = find_videos(root)
    total_bytes = sum(b for _, b in vids)
    if not vids:
        print("  No video files found on this drive.")
        return
    print(f"  {len(vids)} video files · {human(total_bytes)} of video\n")

    if shutil.which("ffprobe"):
        secs = 0.0
        ok = failed = 0
        # per-device (top-level folder) breakdown
        dev_secs, dev_bytes = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(ffprobe_seconds, p): (p, b) for p, b in vids}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                p, b = futs[fut]
                d = fut.result()
                done += 1
                dev = os.path.relpath(p, root).split(os.sep)[0]
                if d:
                    secs += d
                    ok += 1
                    dev_secs[dev] = dev_secs.get(dev, 0.0) + d
                else:
                    failed += 1
                dev_bytes[dev] = dev_bytes.get(dev, 0) + b
                if done % 100 == 0 or done == len(vids):
                    print(f"  probed {done}/{len(vids)} videos…", end="\r", flush=True)
        print(" " * 50, end="\r")
        hours = secs / 3600
        gbph = (total_bytes / 1e9) / hours if hours > 0 else 0
        print("=" * 58)
        print(f"  VIDEO STORAGE : {human(total_bytes)}")
        print(f"  FOOTAGE HOURS : {fmt_hours(hours)}      (measured by ffprobe)")
        print("=" * 58)
        if failed:
            print(f"  note: {failed} file(s) couldn't be read (skipped)")
        print(f"  (bitrate on this drive: {gbph:.1f} GB/hour)")
        if len(dev_secs) > 1:
            print("\n  by device/folder:")
            for dev in sorted(dev_secs, key=lambda k: -dev_secs[k]):
                print(f"    {dev:24} {dev_secs[dev]/3600:6.1f} h   {human(dev_bytes.get(dev,0))}")
    else:
        hours = (total_bytes / 1e9) / GB_PER_HOUR
        print("=" * 58)
        print(f"  VIDEO STORAGE : {human(total_bytes)}")
        print(f"  FOOTAGE HOURS : ~{fmt_hours(hours)}   (ESTIMATE — ffprobe not found)")
        print("=" * 58)
        print(f"  Estimated from {GB_PER_HOUR} GB/hour. Install ffmpeg for an exact count:")
        print("    Ubuntu: sudo apt install ffmpeg   |   Mac: brew install ffmpeg")


def main():
    drives = sys.argv[1:] or external_drives()
    if not drives:
        sys.exit('No drive given and none auto-detected. Pass a path, e.g.:\n'
                 '  python3 check_drive_hours.py "/media/you/Extreme SSD"')
    for d in drives:
        if os.path.isdir(d):
            measure(os.path.abspath(d))
        else:
            print(f"\n  (skipping {d!r} — not a folder)")


if __name__ == "__main__":
    main()
