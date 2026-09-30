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

TWO INDEPENDENT SOURCES OF TRUTH
--------------------------------
1. THE CLOUD (name + byte size, via /api/ssd/verify) — what GCS actually holds now.
2. THE UPLOAD RECEIPTS the uploaders leave ON THE DRIVE — see `_read_receipts`.
   Both uploaders write a receipt only AFTER an end-to-end MD5 check (they compute
   the MD5 of the bytes they streamed and compare it to the hash GCS returns), so a
   receipt is strong local evidence that those exact bytes once landed in GCS:
     • `ssd_upload_server` → a sibling `.<file>.uploaded` per file.
     • `upload_server` (LAN) → a run-level `.gcs_uploaded` = {file: gcs_object_path},
       which also records WHERE each file was written.

Crossing the two is what makes this deeper than either alone. A file that is absent
from the cloud but HAS a receipt is a **PHANTOM** — it is not "never uploaded", it
was verified into GCS and is now missing from the key we probed (deleted, or written
under a different prefix). That needs a completely different response from a plain
missing file, so it is counted and reported separately. Receipts that carry an object
path are also fed back in as extra candidate cloud keys in --any-key mode, so the
search looks exactly where the uploader says it put the bytes.

WHAT A RECEIPT DOES *NOT* PROVE (learned from a real drive, 2026-07-25)
A receipt says "these bytes were confirmed into GCS at the moment of upload". It does
NOT say the cloud still holds them, because the object key is
`<device>/<run>/<file>` — NOT content-addressed. If two different copies of the same
run exist (a complete one and a partial one, e.g. a copy taken while the pi was still
recording), both upload to the SAME key and whichever went last wins. So a receipt can
sit next to a cloud object of a different size, in either direction. That is why size
mismatches are split by DIRECTION (see check_drive_anykey) instead of being pooled
into "missing": cloud-shorter means the drive is the good copy, cloud-larger means the
drive is the fragment, and re-uploading the latter would destroy good cloud data.

Receipts are only ever READ. Use --ignore-receipts to check the cloud alone.

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


def _read_receipts(run_dir, all_names, data_files):
    """The uploaders' own local record of what they confirmed into GCS.

    -> {filename: object_path_or_""}  ("" = receipt exists but records no path)

    Two formats, both read-only, both written ONLY after the uploader's end-to-end
    MD5 check passed (2xx + every byte sent + GCS's returned hash matching the bytes
    streamed), so their presence means "these exact bytes were confirmed in GCS":

      • `.gcs_uploaded` — run-level JSON {filename: gcs_object_path}, written by the
        LAN `upload_server`. Carries the OBJECT PATH, so it tells us where the bytes
        went, not just that they went.
      • `.<file>.uploaded` — one empty-ish sibling per file, written by
        `ssd_upload_server`. No path recorded, so the value is "".

    `all_names` is the raw directory listing (dotfiles included — the data-file filter
    strips them, so they must be passed in separately). Falls back to listing the dir.
    Never raises: a drive with unreadable/corrupt markers just yields fewer receipts,
    which degrades this to a cloud-only check rather than failing the run."""
    if all_names is None:
        try:
            all_names = os.listdir(run_dir)
        except OSError:
            return {}
    present = set(all_names)
    receipts = {}
    if ".gcs_uploaded" in present:
        try:
            with open(os.path.join(run_dir, ".gcs_uploaded"), encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for name, obj in loaded.items():
                    receipts[str(name)] = str(obj) if obj else ""
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            pass                      # corrupt marker → just no receipts from it
    for f in data_files:
        if f".{f}.uploaded" in present:
            receipts.setdefault(f, "")
    return receipts


def _receipt_keys(receipts):
    """Cloud keys implied by receipts that recorded an object path. An object path is
    `<device>/<run>/<file>`, so its first two segments ARE the key the uploader used —
    the most authoritative candidate there is, since it's where the bytes actually
    went. Used to widen the --any-key search."""
    keys = set()
    for obj in receipts.values():
        parts = [p for p in str(obj).split("/") if p]
        if len(parts) >= 3:
            keys.add((parts[0], parts[1]))
    return keys


def candidate_keys(drive, r, extra_devices=()):
    """Every plausible <device_id>/<run> the cloud MIGHT have this run under — so a match
    under ANY one counts as 'in the cloud', regardless of folder layout. Covers:
      • device-on-top      (starcapN/<run>)
      • wrapper on top      (26_6/<dev>_<run>)  — the joined-prefix form the uploader writes
      • the recorder's own device_id / name from device_info.json  (pi-xxxx/<run>), and
        that sidecar's OWN run id  (pi-xxxx/20260721-211759) — the key the uploader uses
        when a run has no _manifest.json; the folder's shortened run never matches it
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
    # device_info.json's own device_id/run — what the uploader used when there is no
    # _manifest.json. Authoritative, and NOT reachable by crossing devs × run_folder:
    # its run is the full timestamp, not the folder's shortened form.
    if di.get("run"):
        for d in (di.get("device_id"), di.get("name")):
            if d:
                cands.add((str(d), str(di["run"])))
    # Receipts that recorded an object path say EXACTLY where the uploader wrote the
    # bytes — a stronger candidate than anything inferred from the folder layout.
    cands |= _receipt_keys(r.get("receipts") or {})
    return cands


def _parse_abcd(part):
    toks = part.split("_")
    if len(toks) >= 4 and TS_RE.match(toks[1]):
        return {"cpu_id": toks[0], "timestamp": toks[1],
                "name": "_".join(toks[2:-1]) or None, "human_time": toks[-1]}
    return None


def _identify(drive, run_dir, files, force_device=None, all_names=None, receipts=True):
    rel = os.path.relpath(run_dir, drive)
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    man = _read_manifest(run_dir)
    # Boards that write no _manifest.json write device_info.json instead (e.g. 1.26.5).
    # The uploader falls back to it for BOTH device_id and run, so we must too — the
    # folder name is only the DISPLAY name ("starcap61") and carries a shortened run
    # ("starcap61_0721-2117"), while the real key is the sidecar's device_id/run
    # ("pi-bce7e88eff/20260721-211759"). Inferring from the path instead probes a prefix
    # that never existed and reports the whole drive as PHANTOM (real drive, 2026-07-27).
    info = _read_device_info(run_dir) if not man else {}
    abcd = None
    for p in parts:
        abcd = _parse_abcd(p)
        if abcd:
            break
    run = man.get("run") or info.get("run")
    if not run and abcd:
        run = abcd["timestamp"]
    if not run:
        below = [p for p in parts[1:] if p.lower() != "stereo"]
        run = "_".join(below) if below else (parts[-1] if parts else "run")
    device_id = (man.get("device_id") or info.get("device_id")
                 or (abcd["cpu_id"] if abcd else None))
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
            "file_sizes": sizes, "bytes_total": total, "drive": drive,
            "receipts": _read_receipts(run_dir, all_names, files) if receipts else {}}


def find_runs(drive, force_device=None, receipts=True):
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
            # `files` (dotfiles included) is the receipt source — `data` has them
            # filtered out, so pass the raw listing rather than re-listing the dir.
            runs.append(_identify(drive, root, data, force_device=force_device,
                                  all_names=files, receipts=receipts))
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
def check_drive(drive, force_device=None, only_device=None, receipts=True):
    print(f"\nScanning {drive} …"
          + (f"  (device forced = {force_device})" if force_device else "")
          + (f"  (only device = {only_device})" if only_device else "")
          + ("" if receipts else "  (receipts ignored)"))
    runs = find_runs(drive, force_device=force_device, receipts=receipts)
    if only_device:                        # point at a whole drive, check just one folder
        runs = [r for r in runs if r["device_id"] == only_device]
    if not runs:
        print("  No StarCap runs found"
              + (f" for device {only_device!r}" if only_device else " on this drive") + ".")
        return True
    n_files = sum(len(r["files"]) for r in runs)
    gb = sum(r["bytes_total"] for r in runs) / 1e9
    print(f"  Found {len(runs)} runs · {n_files} files · {gb:.1f} GB")
    n_receipts = sum(len(r["receipts"]) for r in runs)
    if receipts:
        print(f"  Upload receipts on the drive: {n_receipts}/{n_files} files "
              f"({'.gcs_uploaded / .<file>.uploaded' if n_receipts else 'none found'})")

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
            sizes = c.get("cloud_sizes") or {}
            names = c.get("cloud_files") or []
            # keep name -> cloud byte size; the verify endpoint returns cloud_sizes
            # alongside cloud_files, so the size check needs no extra API calls.
            cloud[(c["device_id"], c["run"])] = {n: sizes.get(n) for n in names}
        print(f"  checked runs {i}..{i + len(specs[i:i+VERIFY_BATCH]) - 1}")

    # A missing file splits into two VERY different cases, so they're tracked apart:
    #   • PHANTOM — the uploader left a receipt (MD5-confirmed into GCS) but the object
    #     isn't under the key we probed. The bytes were uploaded; they're either
    #     deleted or under a different prefix. Re-uploading may be pointless — find the
    #     real key first (--any-key feeds the receipt's own object path back in).
    #   • MISSING — no receipt either. Genuinely never confirmed up. Must be uploaded.
    missing, phantom = [], []
    for r in runs:
        have = cloud.get((r["device_id"], r["run"]), {})
        for f in r["files"]:
            if f not in have:
                rec = r["receipts"].get(f)
                row = (r["device_id"], r["run"], f, r["file_sizes"].get(f, 0),
                       rec if rec else ("receipt, no path recorded" if rec == "" else ""))
                (phantom if rec is not None else missing).append(row)

    absent = missing + phantom
    print("\n" + "=" * 60)
    print(f"  RUNS              : {len(runs)}")
    print(f"  FILES ON SSD      : {n_files}")
    print(f"  FILES IN CLOUD    : {n_files - len(absent)}")
    # Only claim "no receipt either" when receipts were actually consulted.
    print(f"  MISSING (by name) : {len(missing)}"
          + ("   (no upload receipt either)" if receipts else ""))
    if receipts:
        print(f"  PHANTOM           : {len(phantom)}   (receipt says uploaded, cloud says no)")
    print("=" * 60)

    if phantom:
        pgb = sum(p[3] for p in phantom) / 1e9
        print(f"\n  ⚠ {len(phantom)} files / {pgb:.1f} GB have an upload RECEIPT but are not in "
              f"the cloud under the key checked.")
        print("    The uploader MD5-confirmed these into GCS, so the bytes did land — they are")
        print("    either deleted or under a different prefix. Re-run with --any-key (it will")
        print("    search the exact object path the receipt records) before concluding data loss.")
        for d, run, f, sz, why in phantom[:20]:
            print(f"    {d}/{run}/{f}  ({sz/1e6:.1f} MB)" + (f"  receipt → {why}" if why else ""))
        if len(phantom) > 20:
            print(f"    … and {len(phantom) - 20} more")

    if absent:
        mb = sum(m[3] for m in absent) / 1e9
        print(f"\n  ⚠ {len(absent)} files / {mb:.1f} GB NOT in the cloud — DO NOT FORMAT:")
        for d, run, f, sz, _why in absent[:40]:
            print(f"    {d}/{run}/{f}  ({sz/1e6:.1f} MB)")
        if len(absent) > 40:
            print(f"    … and {len(absent) - 40} more")
        out = os.path.join(os.path.expanduser("~"), "ssd_missing.tsv")
        with open(out, "w") as fh:
            fh.write("device\trun\tfile\tbytes\treason\treceipt\n")
            for d, run, f, sz in ((m[0], m[1], m[2], m[3]) for m in missing):
                fh.write(f"{d}\t{run}\t{f}\t{sz}\tmissing\t\n")
            for d, run, f, sz, why in phantom:
                fh.write(f"{d}\t{run}\t{f}\t{sz}\tphantom\t{why}\n")
        print(f"\n  Full missing list saved: {out}")
        return False

    print("\n  ✅ Every file on the SSD has a namesake in the cloud.")

    # Byte-size check. The /api/ssd/verify response already carried cloud_sizes, so we
    # compare every file's SSD size against its cloud size with NO extra API calls — no
    # per-file signed-URL lookups (the old /api/video-url path, which now 401s). A cloud
    # size of None means the endpoint listed the name but reported no size: present, but
    # its bytes couldn't be confirmed.
    print(f"\n  Byte-size check ({n_files} files, from the verify response):")
    results = []          # rows for the CSV
    mismatched = []
    unreceipted = 0
    for r in runs:
        have = cloud.get((r["device_id"], r["run"]), {})
        for f in r["files"]:
            local = r["file_sizes"].get(f, 0)
            remote = have.get(f)
            rec = r["receipts"].get(f)
            if remote is None:
                status = "UNKNOWN"
            elif remote == local:
                status = "MATCH"
            else:
                status = "MISMATCH"
                mismatched.append((r["device_id"], r["run"], f, local, remote))
            # A MISMATCH that also carries a receipt is the worst combination: the
            # uploader MD5-confirmed these bytes, yet the cloud object is a different
            # size — so the cloud copy was overwritten or truncated after the fact.
            if status == "MISMATCH" and rec is not None:
                status = "MISMATCH_RECEIPTED"
            if rec is None and remote is not None:
                unreceipted += 1
            results.append({"device": r["device_id"], "run": r["run"], "file": f,
                            "ssd_bytes": local,
                            "cloud_bytes": "" if remote is None else remote,
                            "status": status,
                            # md5-verified receipt / present but no receipt on this drive
                            "receipt": "none" if rec is None else (rec or "yes"),
                            })

    matched = sum(1 for row in results if row["status"] == "MATCH")
    unknown = sum(1 for row in results if row["status"] == "UNKNOWN")
    receipted_bad = sum(1 for row in results if row["status"] == "MISMATCH_RECEIPTED")
    print(f"    {matched}/{len(results)} sizes match"
          + (f", {len(mismatched)} MISMATCH" if mismatched else "")
          + (f" ({receipted_bad} of them WITH an upload receipt — cloud copy changed "
             f"after a verified upload)" if receipted_bad else "")
          + (f", {unknown} size not reported by cloud" if unknown else "") + ".")
    if receipts and unreceipted:
        # Not a fault: footage copied onto this drive after upload, uploaded from a
        # different machine, or markers that couldn't be written (read-only/full drive)
        # all look like this. It just means the cloud is the ONLY evidence for them.
        print(f"    {unreceipted} file(s) are in the cloud with NO receipt on this drive — "
              f"verified by name+size only, not by the uploader's MD5 check.")

    # write the full results to a CSV next to where you ran the script
    label = clean(os.path.basename(drive.rstrip("/\\")) or "drive")
    csv_path = os.path.join(os.getcwd(), f"ssd_sample_check_{label}.csv")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["device", "run", "file",
                                               "ssd_bytes", "cloud_bytes", "status",
                                               "receipt"])
            w.writeheader()
            w.writerows(results)
        print(f"    📄 Full results written: {csv_path}")
    except OSError as e:
        print(f"    (couldn't write CSV: {e})")

    if mismatched:
        print("\n  ⚠ SIZE MISMATCH — these cloud copies differ from the SSD; investigate before formatting:")
        for d, run, f, local, remote in mismatched[:40]:
            print(f"     {d}/{run}/{f}  ssd={local}  cloud={remote}")
        if len(mismatched) > 40:
            print(f"     … and {len(mismatched) - 40} more (see the CSV)")
        return False
    if unknown:
        # No real mismatches — the cloud just didn't report a size for some names, so those
        # can't be byte-confirmed. NOT a data problem; the names are all verified in the cloud.
        print(f"\n  ✓ No mismatches. The cloud reported no size for {unknown} file(s), so those "
              f"couldn't be byte-confirmed (all names verified present in the cloud).")
        return False
    print("\n  ✅ Sizes match — content is really there. SAFE TO FORMAT.")
    return True


def _emptied_wrappers(drive, runs):
    """Top-level folders on the drive that no longer hold ANY data file -> [name].

    These are almost always a dump/wrapper folder the footage was MOVED OUT of
    (`20-7/`, `26_6/`, `orangepi-da/` — see uploaded_data_processing.md). While the
    footage sat in `20-7/<dev>/stereo/<run>/`, the uploader's path fallback keyed it as
    `20-7/<dev>_<run>/` (device = the wrapper, run = every segment below it minus
    "stereo", hence the DOUBLED name). Moving the files to `<dev>/stereo/<run>/` changes
    every key derivable from the new path, but the CLOUD objects stay at the old key —
    so each file reads as PHANTOM under every candidate and looks like data loss.
    Real drive, STARSSD3 (2026-07-27): 801 files / 279 GB, all of it actually in GCS
    under `20-7/starcapNN_starcapNN_MMDD-HHMM/`. Feeding the emptied wrappers back in
    as extra prefixes recovers them with no flag to remember."""
    live = set()
    for r in runs:
        rel = [p for p in os.path.relpath(r["dir"], drive).split(os.sep) if p and p != "."]
        if rel:
            live.add(rel[0])
    out = []
    try:
        entries = sorted(os.listdir(drive))
    except OSError:
        return out
    for name in entries:
        if (name in live or name in SKIP_DIRS
                or name.startswith(".") or name.startswith("$")):
            continue
        if os.path.isdir(os.path.join(drive, name)):
            out.append(name)
    return out


def check_drive_anykey(drive, force_device=None, only_device=None, extra_devices=(),
                       receipts=True):
    """Robust check: for each run, try EVERY plausible cloud key and count a file as 'in
    the cloud' if it's found under ANY of them (name + size). Independent of folder
    layout, wrappers, merges or where you point it — the cross-key check, automated.

    Receipts feed in here twice: their recorded object paths become candidate keys (the
    uploader's own account of where the bytes went, so a re-keyed/moved object is still
    found), and a file absent under every key but holding a receipt is reported as a
    PHANTOM rather than as never-uploaded."""
    import collections
    drive = os.path.abspath(drive)
    print(f"\nScanning {drive} …  (any-key robust mode"
          + (f", also trying prefixes {list(extra_devices)}" if extra_devices else "")
          + ("" if receipts else ", receipts ignored") + ")")
    runs = find_runs(drive, force_device=force_device, receipts=receipts)
    if only_device:
        runs = [r for r in runs if r["device_id"] == only_device]
    if not runs:
        print("  No StarCap runs found."); return True
    n_files = sum(len(r["files"]) for r in runs)
    gb = sum(r["bytes_total"] for r in runs) / 1e9
    print(f"  Found {len(runs)} runs · {n_files} files · {gb:.1f} GB")
    n_receipts = sum(len(r["receipts"]) for r in runs)
    if receipts:
        print(f"  Upload receipts on the drive: {n_receipts}/{n_files} files")

    # A wrapper folder the footage was moved OUT of still names the cloud key — add it
    # automatically so nobody has to know to pass --also-device (see _emptied_wrappers).
    auto = [w for w in _emptied_wrappers(drive, runs) if w not in extra_devices]
    if auto:
        print(f"  emptied wrapper folder(s) on the drive — also trying as prefixes: {auto}")
    extra_devices = tuple(extra_devices) + tuple(auto)

    per_cands, allkeys = {}, set()
    n_reckeys = 0
    for r in runs:
        c = candidate_keys(drive, r, extra_devices)
        n_reckeys += len(_receipt_keys(r["receipts"]) - {(r["device_id"], r["run"])})
        per_cands[r["dir"]] = c
        allkeys |= c
    print(f"  trying {len(allkeys)} candidate cloud keys (name + size) …"
          + (f" ({n_reckeys} came from upload receipts)" if n_reckeys else ""))

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

    # A discrepancy is one of THREE things, and they call for opposite actions, so they
    # are never pooled into one "missing" number:
    #   ABSENT  — no object under any candidate key. Nothing in the cloud. Upload it.
    #             (with a receipt = PHANTOM: it was confirmed up once and has since gone)
    #   SHORT   — object exists but the cloud has FEWER bytes than the drive. The cloud
    #             copy is truncated/partial; THIS DRIVE holds the better copy. The
    #             at-risk amount is the DELTA, not the whole file.
    #   FRAG    — object exists and the cloud has MORE bytes than the drive. The cloud
    #             copy is the more complete one and the DRIVE holds the fragment. Not a
    #             backup risk; re-uploading from here would make the cloud WORSE.
    # Both SHORT and FRAG mean the same key was written by two different copies of the
    # same run (whoever uploaded last won), which is why a receipt can coexist with a
    # size mismatch — see _read_receipts.
    per = collections.defaultdict(lambda: {"runs": 0, "vids": 0, "ok": 0, "absent": 0,
                                           "short": 0, "frag": 0, "risk_gb": 0.0,
                                           "phantom": 0})
    rows = []
    n_absent = n_short = n_frag = n_phantom = 0
    for r in runs:
        union = {}                       # name -> cloud size, across ALL matching candidate keys
        for k in per_cands[r["dir"]]:
            for n, s in (cloud.get(k) or {}).items():
                if n not in union or union[n] is None:
                    union[n] = s
        dev = r["device_id"]; p = per[dev]; p["runs"] += 1
        for f in r["files"]:
            lsz = r["file_sizes"].get(f, 0); vid = f.endswith(".mp4")
            rec = r["receipts"].get(f)
            csz = union.get(f)
            if vid: p["vids"] += 1
            if f not in union:
                # In any-key mode this survived EVERY plausible key, including any path
                # a receipt records — the strongest evidence of an object that was
                # verified up and is now gone.
                n_absent += 1; p["absent"] += 1; p["risk_gb"] += lsz / 1e9
                if rec is not None:
                    n_phantom += 1; p["phantom"] += 1
                rows.append((dev, r["run"], f, lsz, "",
                             "phantom (receipt, absent from cloud)" if rec is not None else "absent",
                             rec or ""))
            elif csz is not None and csz != lsz:
                if csz < lsz:
                    n_short += 1; p["short"] += 1; p["risk_gb"] += (lsz - csz) / 1e9
                    why = f"cloud SHORT by {lsz - csz} bytes (drive is the better copy)"
                else:
                    n_frag += 1; p["frag"] += 1
                    why = f"cloud LARGER by {csz - lsz} bytes (drive holds a fragment)"
                if rec is not None:
                    why += " [has receipt → cloud key overwritten by another copy]"
                rows.append((dev, r["run"], f, lsz, csz, why, rec or ""))
            elif vid:
                p["ok"] += 1

    vids = sum(p["vids"] for p in per.values()); vok = sum(p["ok"] for p in per.values())
    risk_gb = sum(p["risk_gb"] for p in per.values())
    print("\n" + "=" * 72)
    print(f"  RUNS                     : {len(runs)}")
    print(f"  FILES ON SSD             : {n_files}")
    print(f"  ABSENT from the cloud    : {n_absent}"
          + (f"   (of which PHANTOM, had a receipt: {n_phantom})" if receipts else ""))
    print(f"  CLOUD SHORT (truncated)  : {n_short}   ← drive has more; cloud copy is partial")
    print(f"  CLOUD LARGER (drive frag): {n_frag}   ← cloud has more; NOT a backup risk")
    print(f"  VIDEOS: {vids}  fully matched {vok}")
    print(f"  AT RISK: {risk_gb:.2f} GB  (absent files in full + the SHORT deltas only)")
    print("=" * 72)
    print(f"\n  {'device':<22}{'runs':>5}{'vids':>6}{'matched':>8}{'ABSENT':>7}{'SHORT':>6}"
          f"{'FRAG':>5}{'riskGB':>8}")
    for dev in sorted(per):
        p = per[dev]
        need = p["absent"] + p["short"]          # only these two mean "drive is the authority"
        print(f"  {dev:<22}{p['runs']:>5}{p['vids']:>6}{p['ok']:>8}{p['absent']:>7}"
              f"{p['short']:>6}{p['frag']:>5}{p['risk_gb']:>8.2f}"
              + ("  <-- DO NOT FORMAT" if need else ("  (drive fragment only)" if p["frag"] else "")))
    if rows:
        out = os.path.join(os.path.expanduser("~"), "ssd_missing.tsv")
        with open(out, "w") as fh:
            fh.write("device\trun\tfile\tssd_bytes\tcloud_bytes\treason\treceipt\n")
            for d, run, f, sz, csz, why, rec in rows:
                fh.write(f"{d}\t{run}\t{f}\t{sz}\t{csz}\t{why}\t{rec}\n")
        if n_absent or n_short:
            print(f"\n  ⚠ {risk_gb:.2f} GB is NOT safely in the cloud — DO NOT FORMAT.")
        if n_phantom:
            print(f"  ⚠ {n_phantom} file(s) had an upload receipt yet no object under ANY candidate")
            print("    key. Those bytes were confirmed into GCS once — treat as deleted/moved in")
            print("    the cloud, NOT as never uploaded, and check the bucket before re-uploading.")
        if n_short:
            print(f"  ⚠ {n_short} cloud object(s) are SHORTER than the drive's copy. The same")
            print("    <device>/<run> key was written by two different copies of that run and the")
            print("    partial one landed last. Re-upload from THIS drive to restore the full bytes.")
        if n_frag:
            print(f"  ℹ {n_frag} cloud object(s) are LARGER than the drive's — the cloud holds the")
            print("    more complete copy and this drive holds a fragment. Do NOT re-upload those")
            print("    from here; it would overwrite good cloud data with the fragment.")
        print(f"  Full detail ({len(rows)} files): {out}")
        return not (n_absent or n_short)
    print("\n  ✅ Every file is in the cloud (name + size) under some key. SAFE.")
    if receipts and n_receipts < n_files:
        print(f"  (note: {n_files - n_receipts} file(s) had no upload receipt on this drive — "
              f"confirmed by name+size only.)")
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
    ap.add_argument("--ignore-receipts", action="store_true",
                    help="don't read the uploaders' local upload receipts (.gcs_uploaded / "
                         ".<file>.uploaded) — check the cloud alone. Receipts are normally used "
                         "to tell a never-uploaded file apart from one that was MD5-verified "
                         "into GCS and has since gone (PHANTOM), and to widen --any-key with "
                         "the object path the uploader actually wrote to.")
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
        use_receipts = not args.ignore_receipts
        if args.any_key:
            ok = check_drive_anykey(os.path.abspath(d), force_device=args.device,
                                    only_device=args.only, extra_devices=args.also_device,
                                    receipts=use_receipts)
        else:
            ok = check_drive(os.path.abspath(d), force_device=args.device,
                             only_device=args.only, receipts=use_receipts)
        all_ok = ok and all_ok
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
