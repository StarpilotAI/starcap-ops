#!/usr/bin/env python3
"""
StarCap SSD -> cloud checker  (standalone, read-only)
=====================================================

Answers ONE question before you format a drive: "is every recording on this SSD
already safely in the cloud?" It scans the drive, asks the StarCap dashboard which
files exist in Google Cloud Storage for each run, and reports anything that's
missing. It also spot-checks that a sample of videos are the right SIZE in the
cloud (not empty placeholders).

It is READ-ONLY: it never writes to or deletes anything on the SSD, and it has no
Google Cloud key — it only talks to the dashboard's API with the shared password.

USAGE
-----
  python3 check_ssd.py "<drive path>"      # check one whole drive
  python3 check_ssd.py                      # auto-detect plugged-in drives

  Check just ONE folder (e.g. a single device on a big HDD):
  python3 check_ssd.py "/media/you/My Book/starcap24"          # point at the device folder
  python3 check_ssd.py "/media/you/My Book" --only starcap24   # or filter a whole drive
  python3 check_ssd.py "<weird layout>" --device starcap24      # force the cloud key

  Windows:   py check_ssd.py "E:\\"
  Mac:       python3 check_ssd.py "/Volumes/Extreme SSD"
  Linux:     python3 check_ssd.py "/media/you/Extreme SSD"

CONFIG (edit the two constants just below, or set env vars)
  DASHBOARD_URL     the dashboard base URL   (default https://starcap.dev)
  STARCAP_PASSWORD  the shared dashboard password

Needs only Python 3 (no pip installs).
"""
import concurrent.futures
import csv
import json
import os
import re
import sys
import random
import threading
import time
import urllib.parse
import urllib.request

# ---- CONFIG ----------------------------------------------------------------
DASHBOARD = os.environ.get("DASHBOARD_URL", "https://starcap.dev").rstrip("/")
PASSWORD  = os.environ.get("STARCAP_PASSWORD", "starchild")
# ----------------------------------------------------------------------------

VERIFY_BATCH = 300     # runs per dashboard call (stays under the endpoint's cap)
SAMPLE_SIZES = 50      # how many random videos to byte-size-check
SIZE_WORKERS = 10      # how many size checks to run in parallel (1 = serial)
SCAN_DEPTH   = 6

DATA_EXT = (".mp4", ".mkv", ".mov", ".csv", ".json", ".txt", ".log")
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100",
             ".fseventsd", "found.000", "lost+found"}
TS_RE = re.compile(r"^\d{8}-\d{6}")
clean = lambda s: re.sub(r"[^A-Za-z0-9_.\-]+", "_", s)


# ---- drive scanning (mirrors the ssd_upload_server logic) ------------------
def _is_data_file(name):
    return name.lower().endswith(DATA_EXT) and not name.startswith(".")


def _read_manifest(run_dir):
    p = os.path.join(run_dir, "_manifest.json")
    try:
        return json.load(open(p, encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _read_device_info(run_dir):
    try:
        return json.load(open(os.path.join(run_dir, "device_info.json"), encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def candidate_keys(drive, r, extra_devices=()):
    """Every plausible <device_id>/<run> the cloud MIGHT have this run under — so a match
    under ANY one counts as 'in the cloud', regardless of folder layout. Covers:
      • device-on-top      (starcapN/<run>)
      • wrapper on top      (26_6/<dev>_<run>)  — the joined-prefix form the uploader writes
      • the recorder's own device_id / name from device_info.json  (pi-xxxx/<run>)
      • the run under a user-supplied extra prefix (e.g. --also-device 26_6), incl. the
        <dev>_<run> join — so footage MOVED out of a wrapper is still found.
    This is exactly the cross-key check done by hand; it makes the result independent of
    where you point it and of merges/moves."""
    run_dir = r["dir"]
    run_folder = os.path.basename(os.path.normpath(run_dir))
    parts = [p for p in os.path.relpath(run_dir, drive).split(os.sep) if p and p != "."]
    segs = [p for p in parts if p.lower() != "stereo"]     # drop the 'stereo' leaf level
    folder_devs = [s for s in segs[:-1]] or ([segs[0]] if segs else [])  # device/wrapper folders above the run
    di = _read_device_info(run_dir)
    man = _read_manifest(run_dir)
    devs = set(folder_devs)
    for x in (di.get("device_id"), di.get("name"), man.get("device_id"), r.get("device_id")):
        if x:
            devs.add(str(x))
    cands = set()
    for d in devs:
        cands.add((d, run_folder))
        cands.add((d, f"{d}_{run_folder}"))          # the doubled/joined-prefix form
    if len(segs) >= 3:                                # wrapper / device / run  →  wrapper/<dev>_<run>
        wrap, dev = segs[0], segs[1]
        cands.add((wrap, f"{dev}_{run_folder}"))
        cands.add((wrap, run_folder))
    for e in extra_devices:                           # user-supplied prefixes (e.g. a former wrapper)
        cands.add((e, run_folder))
        for d in folder_devs:
            cands.add((e, f"{d}_{run_folder}"))       # e.g. 26_6/<dev>_<run> after files were moved out of 26_6
    if man.get("device_id") and man.get("run"):
        cands.add((man["device_id"], man["run"]))
    return cands


def _parse_abcd(part):
    toks = part.split("_")
    if len(toks) >= 4 and TS_RE.match(toks[1]):
        return {"cpu_id": toks[0], "timestamp": toks[1],
                "name": "_".join(toks[2:-1]) or None, "human_time": toks[-1]}
    return None


def _identify(drive, run_dir, files, force_device=None):
    rel = os.path.relpath(run_dir, drive)
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    man = _read_manifest(run_dir)
    abcd = None
    for p in parts:
        abcd = _parse_abcd(p)
        if abcd:
            break
    run = man.get("run")
    if not run and abcd:
        run = abcd["timestamp"]
    if not run:
        below = [p for p in parts[1:] if p.lower() != "stereo"]
        run = "_".join(below) if below else (parts[-1] if parts else "run")
    device_id = man.get("device_id") or (abcd["cpu_id"] if abcd else None)
    if not device_id:
        # Normally the first folder below the path you pass IS the device folder. But if
        # you point the checker AT a single device folder, its runs sit under
        # <folder>/stereo/<run>, so the first segment is "stereo", not the device —
        # fall back to the folder you pointed at, so a single-folder check keys correctly.
        if parts and parts[0].lower() == "stereo":
            device_id = os.path.basename(os.path.normpath(drive)) or "device"
        else:
            device_id = parts[0] if parts else "device"
    if force_device:                       # explicit override (bulletproof for any layout)
        device_id = force_device
    sizes = {}
    total = 0
    for f in files:
        try:
            sz = os.path.getsize(os.path.join(run_dir, f))
        except OSError:
            sz = 0
        sizes[f] = sz
        total += sz
    return {"dir": run_dir, "device_id": device_id, "run": run, "files": files,
            "file_sizes": sizes, "bytes_total": total, "drive": drive}


def find_runs(drive, force_device=None):
    runs = []
    drive = os.path.abspath(drive)
    for root, dirs, files in os.walk(drive):
        # skip system/recycle dirs and ALL hidden (dot) dirs — e.g. Linux's
        # .Trash-1000, which otherwise gets scanned and yields bogus device keys.
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not d.startswith("$") and not d.startswith(".")]
        if root[len(drive):].count(os.sep) >= SCAN_DEPTH:
            dirs[:] = []
        data = sorted(f for f in files if _is_data_file(f))
        if data:
            runs.append(_identify(drive, root, data, force_device=force_device))
            dirs[:] = []
    return runs


def external_drives():
    """Best-effort auto-detect of plugged-in drives, per OS."""
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
            t = ctypes.windll.kernel32.GetDriveTypeW(root)
            if t in (2, 3):     # removable or fixed (USB SSDs enumerate as fixed)
                out.append(root)
    else:
        for base in ("/Volumes", "/media", "/run/media", "/mnt"):
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.listdir(base)):
                p = os.path.join(base, entry)
                if not os.path.isdir(p):
                    continue
                # /media/<user>/<label> and /run/media/<user>/<label> nest one deeper
                if base in ("/media", "/run/media"):
                    for sub in (os.listdir(p) if os.access(p, os.R_OK) else []):
                        sp = os.path.join(p, sub)
                        if os.path.isdir(sp):
                            out.append(sp)
                else:
                    out.append(p)
    return out


# ---- dashboard API ---------------------------------------------------------
def api_post(path, body, timeout=180):
    req = urllib.request.Request(f"{DASHBOARD}{path}", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-Starcap-Password": PASSWORD})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def cloud_size(obj_path, attempts=3):
    """Real byte size of an object in GCS via a signed read URL. Returns the size, or
    None if the lookup itself failed (network/timeout) after `attempts` tries — None
    means 'couldn't check', NOT 'file is missing'. Retries with backoff so a transient
    blip (common when firing many requests at once) doesn't show as a false mismatch."""
    q = urllib.parse.quote(obj_path)
    for i in range(attempts):
        try:
            req = urllib.request.Request(f"{DASHBOARD}/api/video-url?path={q}",
                                         headers={"X-Starcap-Password": PASSWORD})
            with urllib.request.urlopen(req, timeout=30) as r:
                url = json.loads(r.read()).get("url")
            if not url:
                return None
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
                return int(r.headers.get("Content-Length", 0))
        except Exception:
            if i == attempts - 1:
                return None
            time.sleep(1.0 * (i + 1))      # 1s, then 2s, before retrying
    return None


# ---- main ------------------------------------------------------------------
def check_drive(drive, force_device=None, only_device=None):
    print(f"\nScanning {drive} …"
          + (f"  (device forced = {force_device})" if force_device else "")
          + (f"  (only device = {only_device})" if only_device else ""))
    runs = find_runs(drive, force_device=force_device)
    if only_device:                        # point at a whole drive, check just one folder
        runs = [r for r in runs if r["device_id"] == only_device]
    if not runs:
        print("  No StarCap runs found"
              + (f" for device {only_device!r}" if only_device else " on this drive") + ".")
        return True
    n_files = sum(len(r["files"]) for r in runs)
    gb = sum(r["bytes_total"] for r in runs) / 1e9
    print(f"  Found {len(runs)} runs · {n_files} files · {gb:.1f} GB")

    # Each run maps to ONE cloud key: <device_id>/<run> (from the folder layout /
    # manifest). Point this at the level where the device folders are on top so the key
    # matches how the footage was uploaded (don't bury runs under extra wrapper folders).
    specs = [{"device_id": r["device_id"], "run": r["run"]} for r in runs]
    cloud = {}
    for i in range(0, len(specs), VERIFY_BATCH):
        resp = api_post("/api/ssd/verify", {"runs": specs[i:i + VERIFY_BATCH]})
        if not resp.get("ok"):
            print(f"  ✗ dashboard error: {resp.get('error')}")
            return False
        for c in resp.get("runs", []):
            cloud[(c["device_id"], c["run"])] = set(c.get("cloud_files") or [])
        print(f"  checked runs {i}..{i + len(specs[i:i+VERIFY_BATCH]) - 1}")

    missing = []
    for r in runs:
        have = cloud.get((r["device_id"], r["run"]), set())
        for f in r["files"]:
            if f not in have:
                missing.append((r["device_id"], r["run"], f, r["file_sizes"].get(f, 0)))

    print("\n" + "=" * 60)
    print(f"  RUNS              : {len(runs)}")
    print(f"  FILES ON SSD      : {n_files}")
    print(f"  FILES IN CLOUD    : {n_files - len(missing)}")
    print(f"  MISSING (by name) : {len(missing)}")
    print("=" * 60)

    if missing:
        mb = sum(m[3] for m in missing) / 1e9
        print(f"\n  ⚠ {len(missing)} files / {mb:.1f} GB NOT in the cloud — DO NOT FORMAT:")
        for d, run, f, sz in missing[:40]:
            print(f"    {d}/{run}/{f}  ({sz/1e6:.1f} MB)")
        if len(missing) > 40:
            print(f"    … and {len(missing) - 40} more")
        out = os.path.join(os.path.expanduser("~"), "ssd_missing.tsv")
        with open(out, "w") as fh:
            fh.write("device\trun\tfile\tbytes\n")
            for d, run, f, sz in missing:
                fh.write(f"{d}\t{run}\t{f}\t{sz}\n")
        print(f"\n  Full missing list saved: {out}")
        return False

    print("\n  ✅ Every file on the SSD has a namesake in the cloud.")

    mp4s = [(r, f) for r in runs for f in r["files"] if f.endswith(".mp4")]
    sample = random.sample(mp4s, min(SAMPLE_SIZES, len(mp4s)))
    print(f"\n  Byte-size spot check ({len(sample)} random videos, {SIZE_WORKERS} at a time):")

    plock = threading.Lock()       # keep live-feed lines from interleaving
    results = []                   # rows for the CSV

    def check_one(idx, r, f):
        local = r["file_sizes"].get(f, 0)
        dev, rn = r["device_id"], r["run"]
        obj = f"{clean(dev)}/{clean(rn)}/{f}"
        remote = cloud_size(obj)
        if remote is None:
            mark, status = "?", "UNKNOWN"
        elif remote == local:
            mark, status = "✓", "MATCH"
        else:
            mark, status = "✗", "MISMATCH"
        rs = f"{remote/1e6:.1f} MB" if remote else "n/a"
        with plock:
            print(f"    [{mark}] {dev}/{rn}/{f}  "
                  f"ssd={local/1e6:.1f} MB  cloud={rs}")
        return {"device": dev, "run": rn, "file": f,
                "ssd_bytes": local, "cloud_bytes": remote if remote is not None else "",
                "status": status}

    with concurrent.futures.ThreadPoolExecutor(max_workers=SIZE_WORKERS) as ex:
        futs = [ex.submit(check_one, i, r, f) for i, (r, f) in enumerate(sample)]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())

    # write the sample results to a CSV next to where you ran the script
    label = clean(os.path.basename(drive.rstrip("/\\")) or "drive")
    csv_path = os.path.join(os.getcwd(), f"ssd_sample_check_{label}.csv")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["device", "run", "file",
                                               "ssd_bytes", "cloud_bytes", "status"])
            w.writeheader()
            w.writerows(results)
        print(f"\n  📄 Sample results written: {csv_path}")
    except OSError as e:
        print(f"\n  (couldn't write CSV: {e})")

    matched = sum(1 for row in results if row["status"] == "MATCH")
    mismatched = [r for r in results if r["status"] == "MISMATCH"]
    unknown = [r for r in results if r["status"] == "UNKNOWN"]
    print(f"  {matched}/{len(results)} matched"
          + (f", {len(mismatched)} MISMATCH" if mismatched else "")
          + (f", {len(unknown)} couldn't-check" if unknown else "") + ".")

    if mismatched:
        print("\n  ⚠ SIZE MISMATCH — these cloud copies differ from the SSD; investigate before formatting:")
        for r in mismatched:
            print(f"     {r['device']}/{r['run']}/{r['file']}  ssd={r['ssd_bytes']}  cloud={r['cloud_bytes']}")
        return False
    if unknown:
        # No real mismatches — the size lookup just failed for some (network), so we
        # can't fully confirm those. NOT a data problem; re-run to nail it down.
        print(f"\n  ✓ No mismatches. Couldn't reach the cloud for {len(unknown)} sample(s) "
              f"(network blip) — re-run to confirm those. Names all verified in the cloud.")
        return False
    print("\n  ✅ Sizes match — content is really there. SAFE TO FORMAT.")
    return True


def check_drive_anykey(drive, force_device=None, only_device=None, extra_devices=()):
    """Robust check: for each run, try EVERY plausible cloud key and count a file as 'in
    the cloud' if it's found under ANY of them (name + size). Independent of folder
    layout, wrappers, merges or where you point it — the cross-key check, automated."""
    import collections
    drive = os.path.abspath(drive)
    print(f"\nScanning {drive} …  (any-key robust mode"
          + (f", also trying prefixes {list(extra_devices)}" if extra_devices else "") + ")")
    runs = find_runs(drive, force_device=force_device)
    if only_device:
        runs = [r for r in runs if r["device_id"] == only_device]
    if not runs:
        print("  No StarCap runs found."); return True
    n_files = sum(len(r["files"]) for r in runs)
    gb = sum(r["bytes_total"] for r in runs) / 1e9
    print(f"  Found {len(runs)} runs · {n_files} files · {gb:.1f} GB")

    per_cands, allkeys = {}, set()
    for r in runs:
        c = candidate_keys(drive, r, extra_devices)
        per_cands[r["dir"]] = c
        allkeys |= c
    print(f"  trying {len(allkeys)} candidate cloud keys (name + size) …")

    cloud = {}   # (device,run) -> {name: cloud_bytes}
    specs = [{"device_id": d, "run": rn} for d, rn in allkeys]
    for i in range(0, len(specs), VERIFY_BATCH):
        resp = api_post("/api/ssd/verify", {"runs": specs[i:i + VERIFY_BATCH]})
        if not resp.get("ok"):
            print(f"  ✗ dashboard error: {resp.get('error')}"); return False
        for c in resp.get("runs", []):
            sizes = c.get("cloud_sizes") or {}
            names = c.get("cloud_files") or []
            cloud[(c["device_id"], c["run"])] = {n: sizes.get(n) for n in names}

    per = collections.defaultdict(lambda: {"runs": 0, "vids": 0, "ok": 0, "miss": 0, "sizebad": 0, "miss_gb": 0.0})
    missing = []
    for r in runs:
        union = {}                       # name -> cloud size, across ALL matching candidate keys
        for k in per_cands[r["dir"]]:
            for n, s in (cloud.get(k) or {}).items():
                if n not in union or union[n] is None:
                    union[n] = s
        dev = r["device_id"]; p = per[dev]; p["runs"] += 1
        for f in r["files"]:
            lsz = r["file_sizes"].get(f, 0); vid = f.endswith(".mp4")
            if vid: p["vids"] += 1
            if f not in union:
                missing.append((dev, r["run"], f, lsz, "missing"))
                if vid: p["miss"] += 1; p["miss_gb"] += lsz / 1e9
            elif union[f] is not None and union[f] != lsz:
                missing.append((dev, r["run"], f, lsz, f"size {lsz}!={union[f]}"))
                if vid: p["sizebad"] += 1; p["miss_gb"] += lsz / 1e9
            elif vid:
                p["ok"] += 1

    vids = sum(p["vids"] for p in per.values()); vok = sum(p["ok"] for p in per.values())
    vbad = sum(p["miss"] + p["sizebad"] for p in per.values()); vgb = sum(p["miss_gb"] for p in per.values())
    print("\n" + "=" * 64)
    print(f"  RUNS                    : {len(runs)}")
    print(f"  FILES ON SSD            : {n_files}")
    print(f"  FILES MISSING (name/size): {len(missing)}")
    print(f"  VIDEOS: {vids}  in-cloud {vok}  MISSING {vbad}  ({vgb:.1f} GB of footage)")
    print("=" * 64)
    print(f"\n  {'device':<22}{'runs':>5}{'vids':>6}{'inCloud':>8}{'MISSING':>8}")
    for dev in sorted(per):
        p = per[dev]; bad = p["miss"] + p["sizebad"]
        print(f"  {dev:<22}{p['runs']:>5}{p['vids']:>6}{p['ok']:>8}{bad:>8}"
              + ("  <-- NOT BACKED UP" if bad else ""))
    if missing:
        out = os.path.join(os.path.expanduser("~"), "ssd_missing.tsv")
        with open(out, "w") as fh:
            fh.write("device\trun\tfile\tbytes\treason\n")
            for d, run, f, sz, why in missing:
                fh.write(f"{d}\t{run}\t{f}\t{sz}\t{why}\n")
        print(f"\n  ⚠ {vbad} videos / {vgb:.1f} GB of footage NOT in the cloud — DO NOT FORMAT.")
        print(f"  Full missing list ({len(missing)} files): {out}")
        return False
    print("\n  ✅ Every file is in the cloud (name + size) under some key. SAFE.")
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Check that every recording on a drive (or one folder) is in the cloud.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  python3 check_ssd.py "/media/you/My Book"                 # whole drive\n'
               '  python3 check_ssd.py "/media/you/My Book/starcap24"        # just that folder\n'
               '  python3 check_ssd.py "/media/you/My Book" --only starcap24 # one device on the drive\n'
               '  python3 check_ssd.py "/media/you/oddlayout" --device starcap24  # force the cloud key\n'
               '  python3 check_ssd.py "/media/you/My Book" --any-key            # robust: layout/wrapper-proof\n'
               '  python3 check_ssd.py "/media/you/My Book" --any-key --also-device 26_6  # also look under a former wrapper')
    ap.add_argument("paths", nargs="*", help="drive or folder path(s); auto-detect if none given")
    ap.add_argument("--device", help="force this name as the cloud device key for every run "
                    "found (use when pointing at a single device folder with an unusual layout)")
    ap.add_argument("--only", help="when pointing at a whole drive, check ONLY this device folder")
    ap.add_argument("--any-key", action="store_true",
                    help="ROBUST mode: try every plausible cloud key per run (device-on-top, "
                         "wrapper/joined, device_id from device_info) and count a file present if "
                         "found under ANY. Layout/wrapper/merge-proof. Verifies name + size.")
    ap.add_argument("--also-device", action="append", default=[], metavar="NAME",
                    help="extra prefix to also try in --any-key mode (e.g. a former wrapper like "
                         "'26_6', for footage moved out of it). Repeatable.")
    args = ap.parse_args()

    if not PASSWORD:
        sys.exit("Set STARCAP_PASSWORD (env var) or edit the PASSWORD constant.")
    drives = args.paths or external_drives()
    if not drives:
        sys.exit("No drive given and none auto-detected. Pass a path, e.g.:\n"
                 '  python3 check_ssd.py "/media/you/Extreme SSD"')
    all_ok = True
    for d in drives:
        if not os.path.isdir(d):
            print(f"\n  (skipping {d!r} — not a folder)")
            continue
        if args.any_key:
            ok = check_drive_anykey(os.path.abspath(d), force_device=args.device,
                                    only_device=args.only, extra_devices=args.also_device)
        else:
            ok = check_drive(os.path.abspath(d), force_device=args.device, only_device=args.only)
        all_ok = ok and all_ok
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
