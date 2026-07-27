#!/usr/bin/env python3
"""
ssd_audit.py — is every recording on this drive really in the cloud?
====================================================================

Answers exactly one question, before you format a drive: for every data file on it, is
there an object in GCS with the same name and the same byte count? Anything that isn't
is reported, in full, with the reason.

READ-ONLY. It never writes to, moves, or deletes anything on the drive, and it holds no
GCS credentials — it asks the dashboard (`/api/ssd/verify`) to list what GCS holds under
a prefix. Its only side effect is an optional TSV report written where you point it.

WHY THIS EXISTS (read this before "fixing" a false alarm)
--------------------------------------------------------
The GCS key is `<device_id>/<run>/<file>` — not content-addressed. Which `(device_id,
run)` a given file was uploaded under depends on WHICH TOOL and WHICH BUILD did the
upload, and on where the folder sat on the drive AT THAT MOMENT. Both have changed
repeatedly. Two real cases, both of which read as catastrophic data loss to a checker
that probes one key:

  * STARSSD8 (2026-07-27): runs carry `device_info.json`, not `_manifest.json`. The
    uploader keys those off the sidecar (`pi-bce7e88eff/20260721-211759`); a checker
    that only read `_manifest.json` fell through to the path and probed
    `starcap61/starcap61_0721-2117` — a prefix that never existed. 870 files / 321 GB
    reported missing. All 870 were in GCS.
  * STARSSD3 (2026-07-27): 801 files / 279 GB uploaded while the footage still sat in a
    `20-7/` dump folder, so the pre-`d59abad` uploader's path fallback keyed them
    `20-7/starcapNN_starcapNN_0720-1719` (device = the wrapper, run = every segment
    below it minus "stereo" — hence the doubled name). The files were later moved to
    `starcapNN/stereo/<run>/`, which erased every local trace of that key. The objects
    never moved. All 801 were in GCS.

So: a file that is absent under the key you happen to derive is NOT evidence of loss.
This tool therefore treats key derivation as a search over NAMED SCHEMES (below), not
as a single guess, and only says "missing" when every scheme comes up empty.

HOW IT WORKS
------------
1. Scan the drive for run folders (deepest folder holding data files) and collect every
    identity token: both sidecars, the path segments, the drive's top-level folders.
2. Resolve a SCHEME per device folder, not per run: probe a couple of runs against every
    scheme, and whichever one fully matches is then applied to that folder's remaining
    runs. Runs the winner misses fall back to the full scheme sweep. This is what keeps
    the call count sane — a full sweep of every scheme for every run is `--full`.
3. Compare name AND byte size, and split failures BY DIRECTION, because the direction
    decides what you should do:
      ABSENT  — no object under any scheme. Not backed up.
      SHORT   — the cloud object has FEWER bytes. The drive holds the better copy.
      LARGER  — the cloud object has MORE bytes. The DRIVE holds a fragment; the cloud
                copy is the good one. Re-uploading from here would DESTROY good data.
    A file that is absent yet carries an upload receipt is a PHANTOM — the uploader
    MD5-confirmed those bytes into GCS once, so treat it as deleted/moved-in-the-cloud
    and go find the object, not as "never uploaded".

USAGE
  python3 ssd_audit.py /media/you/STARSSD3
  python3 ssd_audit.py /media/you/STARSSD3 --tsv ~/ssd_problems.tsv
  python3 ssd_audit.py /media/you/STARSSD3 --also-prefix 26_6   # extra former wrapper
  python3 ssd_audit.py /media/you/STARSSD3 --full               # every scheme, every run
  python3 ssd_audit.py /media/you/STARSSD3 --json               # machine-readable

  Exit code 0 = every file is in the cloud at a matching size. 1 = something is at risk.

CONFIG   DASHBOARD_URL (default https://starcap.dev), STARCAP_PASSWORD
Needs only Python 3.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DASHBOARD = os.environ.get("DASHBOARD_URL", "https://starcap.dev").rstrip("/")
PASSWORD = os.environ.get("STARCAP_PASSWORD", "starchild")

VERIFY_BATCH = 300          # runs per /api/ssd/verify call (endpoint caps at 1000)
SCAN_DEPTH = 6
PROBE_RUNS = 2              # runs per device folder used to resolve the scheme

DATA_EXT = (".mp4", ".mkv", ".mov", ".csv", ".json", ".txt", ".log")
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100",
             ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", "Backups.backupdb",
             "found.000", "lost+found"}
TS_RE = re.compile(r"\d{8}-\d{6}")          # a full run timestamp, e.g. 20260720-171901


# ---------------------------------------------------------------- drive scanning
def _read_json(run_dir, name):
    try:
        with open(os.path.join(run_dir, name), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _read_receipts(run_dir, listing, data_files):
    """The uploaders' own record of what they confirmed into GCS -> {file: object_path}.

    Only ever READ. Both uploaders write a receipt after an end-to-end MD5 check (they
    hash the bytes they streamed and compare against the hash GCS returns), so it means
    "these exact bytes landed in GCS at that moment" — not "GCS still holds them".

      `.gcs_uploaded`      run-level JSON {file: object_path} (LAN upload_server). The
                           path is gold: it says WHERE the bytes went.
      `.<file>.uploaded`   one sibling per file (ssd_upload_server). Records no path.
    """
    present = set(listing)
    out = {}
    if ".gcs_uploaded" in present:
        loaded = _read_json(run_dir, ".gcs_uploaded")
        if isinstance(loaded, dict):
            for name, obj in loaded.items():
                out[str(name)] = str(obj) if obj else ""
    for f in data_files:
        if f".{f}.uploaded" in present:
            out.setdefault(f, "")
    return out


def scan(drive, use_receipts=True):
    """-> [run dict] for every folder on the drive that directly holds data files.

    A run folder is a leaf: once a folder has data files we record it and stop
    descending, so `<device>/<run>/stereo/<files>` yields the stereo folder as the data
    directory with the run/device taken from the path above it.
    """
    drive = os.path.abspath(drive)
    runs = []
    for root, dirs, files in os.walk(drive):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS and not d.startswith("$") and not d.startswith(".")]
        if root[len(drive):].count(os.sep) >= SCAN_DEPTH:
            dirs[:] = []
        data = sorted(f for f in files
                      if f.lower().endswith(DATA_EXT) and not f.startswith("."))
        if not data:
            continue
        dirs[:] = []
        sizes = {}
        for f in data:
            try:
                sizes[f] = os.path.getsize(os.path.join(root, f))
            except OSError:
                sizes[f] = 0
        runs.append({
            "dir": root,
            "files": data,
            "sizes": sizes,
            "bytes": sum(sizes.values()),
            "tokens": _tokens(drive, root),
            "receipts": _read_receipts(root, files, data) if use_receipts else {},
        })
    return runs


def _tokens(drive, run_dir):
    """Every identity token this run folder offers, for the schemes to combine.

    Both sidecars are read: `_manifest.json` wins where present, but plenty of builds
    wrote only `device_info.json`, and the uploader falls back to it for BOTH device_id
    and run — so a checker that ignores it derives a prefix that was never used.
    """
    parts = [p for p in os.path.relpath(run_dir, drive).split(os.sep) if p and p != "."]
    man = _read_json(run_dir, "_manifest.json")
    info = _read_json(run_dir, "device_info.json")
    hw = info.get("hardware") if isinstance(info.get("hardware"), dict) else {}

    leaf = parts[-1] if parts else "run"
    # every segment below the device folder except "stereo" (a codec subfolder, not a
    # run) — the uploader's path fallback, and the source of the doubled-name form
    below = [p for p in parts[1:] if p.lower() != "stereo"]
    # the segment directly above the run, minus "stereo" — the device folder INSIDE a
    # wrapper, i.e. the "starcapNN" in `20-7/starcapNN/stereo/<run>`
    above = [p for p in parts[:-1] if p.lower() != "stereo"]
    m = TS_RE.search(leaf) or TS_RE.search(str(man.get("run") or info.get("run") or ""))
    cpu = man.get("cpu_id") or hw.get("cpu_serial") or hw.get("dt_serial")
    host = man.get("hostname") or info.get("hostname")

    return {
        "top": parts[0] if parts else None,          # drive's top-level folder
        "parent": above[-1] if above else None,      # device folder above the run
        "leaf": leaf,
        "path_run": "_".join(below) if below else leaf,
        "sidecar_dev": man.get("device_id") or info.get("device_id"),
        "sidecar_run": man.get("run") or info.get("run"),
        "name": man.get("name") or info.get("name"),  # friendly camera name (starcapNN)
        "host": host,
        "cpu": cpu,
        "hostid": f"{host}-{cpu[:8]}" if host and cpu else None,
        "ts": m.group(0) if m else None,
    }


def emptied_wrappers(drive, runs):
    """Top-level folders that hold NO data file any more -> [name].

    Almost always a dump/wrapper folder the footage was MOVED OUT of (`20-7/`, `26_6/`,
    `orangepi-da/`). The cloud objects keep the old wrapper key while the drive no longer
    mentions it anywhere — see the STARSSD3 case in the module docstring. Detecting these
    is what makes the wrapper schemes work with no flag to remember.
    """
    live = {r["tokens"]["top"] for r in runs}
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


# ---------------------------------------------------------------- key schemes
def build_schemes(wrappers=()):
    """-> [(name, fn)] where fn(tokens) -> (device_id, run) or None.

    Ordered best-evidence-first. Each is a NAMED, reusable derivation rather than a bare
    pair, so a scheme resolved from one run can be applied to its siblings (see resolve).
    """
    def s(dev, run):
        return (str(dev), str(run)) if dev and run else None

    schemes = [
        # what the current uploader does: sidecar device_id + sidecar run
        ("sidecar", lambda t: s(t["sidecar_dev"], t["sidecar_run"])),
        # the friendly camera name paired with each run form (opm card layouts)
        ("name+run", lambda t: s(t["name"], t["sidecar_run"])),
        ("name+leaf", lambda t: s(t["name"], t["leaf"])),
        ("name+ts", lambda t: s(t["name"], t["ts"])),
        # recorder hardware identities (pi-*, orangepi5-*, starpilot-*)
        ("cpu+run", lambda t: s(t["cpu"], t["sidecar_run"])),
        ("host+run", lambda t: s(t["host"], t["sidecar_run"])),
        ("hostid+run", lambda t: s(t["hostid"], t["sidecar_run"])),
        # the uploader's path fallback, and the doubled-name form it produces from a
        # `<wrapper>/<device>/stereo/<run>` layout
        ("path", lambda t: s(t["top"], t["path_run"])),
        ("path+leaf", lambda t: s(t["top"], t["leaf"])),
        ("doubled", lambda t: s(t["top"], f'{t["parent"]}_{t["leaf"]}')),
        ("device+leaf", lambda t: s(t["parent"], t["leaf"])),
        ("device+run", lambda t: s(t["parent"], t["sidecar_run"])),
    ]
    # A wrapper the footage has since been moved out of: nothing in the current path can
    # reach these, so they must be injected from the drive's top level (or --also-prefix).
    for w in wrappers:
        schemes += [
            (f"wrap[{w}]+doubled", lambda t, w=w: s(w, f'{t["parent"]}_{t["leaf"]}')),
            (f"wrap[{w}]+leaf", lambda t, w=w: s(w, t["leaf"])),
            (f"wrap[{w}]+pathrun", lambda t, w=w: s(w, t["path_run"])),
        ]
    return schemes


def receipt_keys(receipts):
    """(device, run) pairs recorded by receipts that carry an object path. An object path
    is `<device>/<run>/<file>`, so its first two segments ARE the key the uploader used —
    more authoritative than anything inferred, since it is where the bytes actually went.
    """
    keys = set()
    for obj in receipts.values():
        segs = [p for p in str(obj).split("/") if p]
        if len(segs) >= 3:
            keys.add((segs[0], segs[1]))
    return keys


# ---------------------------------------------------------------- cloud lookup
class Cloud:
    """Cached name->size listings from GCS, one entry per (device, run) prefix.

    Every prefix is listed at most once for the whole audit no matter how many runs or
    schemes ask for it. Listing is a metadata operation — it reads no object bytes, so it
    incurs no egress.
    """

    def __init__(self, quiet=False):
        self._seen = {}
        self.quiet = quiet
        self.calls = 0

    def fetch(self, keys):
        want = sorted({k for k in keys if k and k not in self._seen})
        if not want:
            return
        for i in range(0, len(want), VERIFY_BATCH):
            chunk = want[i:i + VERIFY_BATCH]
            body = {"runs": [{"device_id": d, "run": r} for d, r in chunk]}
            resp = _api("/api/ssd/verify", body)
            if not resp.get("ok"):
                raise SystemExit(f"dashboard refused verification: {resp.get('error')}")
            for c in resp.get("runs", []):
                sizes = c.get("cloud_sizes") or {}
                self._seen[(c["device_id"], c["run"])] = {
                    n: sizes.get(n) for n in (c.get("cloud_files") or [])}
            for k in chunk:                      # keys the endpoint didn't echo back
                self._seen.setdefault(k, {})
            self.calls += 1
        if not self.quiet:
            print(f"    listed {len(want)} new prefix(es) in GCS "
                  f"({self.n_prefixes} total so far)")

    def get(self, key):
        return self._seen.get(key) or {}

    @property
    def n_prefixes(self):
        return len(self._seen)


def _api(path, body, timeout=180):
    req = urllib.request.Request(
        f"{DASHBOARD}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Starcap-Password": PASSWORD})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"dashboard {path} -> HTTP {e.code} {e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"could not reach {DASHBOARD}{path}: {e}")


# ---------------------------------------------------------------- matching
def score(run, have):
    """-> how many of this run's files the prefix `have` holds at a MATCHING size.

    A size the endpoint didn't report counts as a match (name-only evidence) rather than
    as a mismatch — the alternative invents failures out of a reporting gap.
    """
    n = 0
    for f, local in run["sizes"].items():
        if f in have and (have[f] is None or have[f] == local):
            n += 1
    return n


def keys_for(run, subset):
    """-> [(scheme_name, (device, run))] this run could plausibly live under."""
    out = []
    for name, fn in subset:
        try:
            k = fn(run["tokens"])
        except (KeyError, TypeError):
            k = None
        if k:
            out.append((name, k))
    for k in receipt_keys(run["receipts"]):
        out.append(("receipt-path", k))
    return out


def _pick(run, cands, cloud):
    """Score every candidate and record the best, plus EVERY prefix that held something.

    Both matter: the best single prefix names the scheme (what you report and reason
    about), while the set of all matching prefixes is what files are checked against —
    a run whose files got split across two keys by an uploader restart then verifies
    fully instead of half-failing.
    """
    best, hits, matched = None, -1, []
    for name, k in cands:
        n = score(run, cloud.get(k))
        if n:
            matched.append(k)
        if n > hits:
            best, hits = (name, k), n
    run["scheme"], run["key"] = best if best else ("unkeyable", None)
    run["hits"], run["matched"] = max(hits, 0), matched
    return run["hits"]


def resolve(runs, schemes, cloud, full=False, quiet=False):
    """Pick the best key for every run, and record which scheme produced it.

    Runs sharing a device folder were almost always uploaded by the same tool under the
    same convention, so the folder is resolved ONCE — probe its biggest runs against
    every scheme — and the winner is then applied to the rest; only runs it fails to
    cover fall back to the full sweep. `full=True` sweeps every scheme for every run.

    Every phase fetches for ALL runs at once. Resolving run-by-run would issue one API
    call per run (278 calls on a real drive); batching makes it a handful.
    """
    groups = {}
    for r in runs:
        groups.setdefault(r["tokens"]["top"], []).append(r)
    if not quiet:
        mode = "every scheme, every run" if full else \
               f"probe {len(groups)} device folder(s), then apply the winner"
        print(f"  keying {len(runs)} runs — {mode}")

    subsets = {top: schemes for top in groups}
    if not full:
        # phase 1 — probe the biggest runs of each folder (most files, so a coincidental
        # match on a 1-file folder can't win the scheme for the whole group)
        probes = [r for g in groups.values()
                  for r in sorted(g, key=lambda r: -len(r["files"]))[:PROBE_RUNS]]
        cloud.fetch(k for r in probes for _, k in keys_for(r, schemes))
        for top, group in groups.items():
            won = []
            for p in sorted(group, key=lambda r: -len(r["files"]))[:PROBE_RUNS]:
                if _pick(p, keys_for(p, schemes), cloud) == len(p["files"]):
                    won.append(p["scheme"])
            if won:
                subsets[top] = [s for s in schemes if s[0] in won]

    # phase 2 — apply each folder's scheme(s) to all of its runs, in one fetch
    cloud.fetch(k for r in runs for _, k in keys_for(r, subsets[r["tokens"]["top"]]))
    for r in runs:
        _pick(r, keys_for(r, subsets[r["tokens"]["top"]]), cloud)

    # phase 3 — anything the folder's scheme didn't fully cover gets the full sweep
    weak = [r for r in runs if r["hits"] < len(r["files"])]
    if weak and not full:
        if not quiet:
            print(f"  {len(weak)} run(s) not fully covered — full scheme sweep on those")
        cloud.fetch(k for r in weak for _, k in keys_for(r, schemes))
        for r in weak:
            _pick(r, keys_for(r, schemes), cloud)


# ---------------------------------------------------------------- verdicts
def judge(runs, cloud):
    """-> [problem dict] for every file that isn't safely in the cloud.

    Split by DIRECTION (see the module docstring): ABSENT / SHORT / LARGER. A run's key
    is the best prefix found for it, but a file is checked against the UNION of every
    prefix that matched anything for that run, so a run split across two keys still
    verifies fully instead of half-failing.
    """
    problems = []
    for r in runs:
        have = {}
        for k in (r.get("matched") or [r.get("key")]):
            for n, sz in cloud.get(k).items():
                # keep the size closest to the drive's — a run split across two keys can
                # have a complete copy under one and a truncated one under the other
                if n not in have or (have[n] or 0) < (sz or 0):
                    have[n] = sz
        for f, local in sorted(r["sizes"].items()):
            remote = have.get(f)
            rec = r["receipts"].get(f)
            row = {"device": (r.get("key") or ("?", "?"))[0],
                   "run": (r.get("key") or ("?", "?"))[1],
                   "file": f, "ssd_bytes": local, "cloud_bytes": remote,
                   "scheme": r.get("scheme"), "dir": r["dir"],
                   "receipt": rec if rec else ("yes (no path)" if rec == "" else "")}
            if f not in have:
                row["verdict"] = "PHANTOM" if rec is not None else "ABSENT"
                row["risk"] = local
                row["note"] = ("receipt says it was MD5-confirmed into GCS — find the "
                               "object before re-uploading" if rec is not None
                               else "no object under any scheme, no receipt — upload it")
            elif remote is None or remote == local:
                continue
            elif remote < local:
                row["verdict"] = "SHORT"
                row["risk"] = local - remote
                row["note"] = "cloud copy is truncated — the DRIVE is the good copy"
            else:
                row["verdict"] = "LARGER"
                row["risk"] = 0
                row["note"] = ("cloud has MORE bytes — the drive holds a fragment; do "
                               "NOT re-upload from here")
            problems.append(row)
    return problems


# ---------------------------------------------------------------- reporting
def gb(n):
    return n / 1e9


def human(n):
    """Bytes in a unit that stays visible. A few thousand bytes at risk must not print as
    '0.00 GB' next to a DO-NOT-FORMAT warning — that reads like a contradiction."""
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def report(drive, runs, problems, cloud, tsv=None):
    n_files = sum(len(r["files"]) for r in runs)
    by = {}
    for p in problems:
        by.setdefault(p["verdict"], []).append(p)
    risk = sum(p["risk"] for p in problems)
    ok = n_files - len(problems) + len(by.get("LARGER", []))

    print("\n" + "=" * 72)
    print(f"  DRIVE            : {drive}")
    print(f"  RUNS / FILES     : {len(runs)} / {n_files}   "
          f"({gb(sum(r['bytes'] for r in runs)):.1f} GB)")
    print(f"  IN CLOUD (name+size): {ok}/{n_files}")
    for v, label in (("ABSENT", "never confirmed up — upload it"),
                     ("PHANTOM", "had a receipt — object moved or deleted"),
                     ("SHORT", "cloud truncated — drive is the good copy"),
                     ("LARGER", "drive is a fragment — do NOT re-upload")):
        rows = by.get(v, [])
        if rows:
            print(f"  {v:<16} : {len(rows):<6} {label}")
    print(f"  AT RISK          : {human(risk)}   "
          f"(ABSENT/PHANTOM in full + SHORT deltas)")
    print(f"  prefixes listed  : {cloud.n_prefixes} in {cloud.calls} API call(s)")
    print("=" * 72)

    schemes = {}
    for r in runs:
        schemes.setdefault(r.get("scheme") or "?", 0)
        schemes[r.get("scheme") or "?"] += 1
    print("\n  keying scheme that matched:")
    for name, n in sorted(schemes.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>5} run(s)  {name}")

    per_dev = {}
    for r in runs:
        d = (r.get("key") or ("?",))[0]
        e = per_dev.setdefault(d, {"runs": 0, "files": 0, "bad": 0, "risk": 0})
        e["runs"] += 1
        e["files"] += len(r["files"])
    for p in problems:
        e = per_dev.setdefault(p["device"], {"runs": 0, "files": 0, "bad": 0, "risk": 0})
        e["bad"] += 1
        e["risk"] += p["risk"]
    print(f"\n  {'device':<26}{'runs':>6}{'files':>7}{'bad':>6}{'at risk':>12}")
    for d, e in sorted(per_dev.items()):
        flag = "   <-- DO NOT FORMAT" if e["risk"] else ""
        print(f"  {str(d):<26}{e['runs']:>6}{e['files']:>7}{e['bad']:>6}"
              f"{human(e['risk']) if e['risk'] else '-':>12}{flag}")

    if problems:
        print(f"\n  ⚠ {len(problems)} file(s) need attention; {human(risk)} at risk.")
        for p in problems[:20]:
            print(f"    {p['verdict']:<8} {p['device']}/{p['run']}/{p['file']}  "
                  f"ssd={p['ssd_bytes']} cloud={p['cloud_bytes']}")
        if len(problems) > 20:
            print(f"    … and {len(problems) - 20} more")
        if by.get("PHANTOM"):
            print("\n  PHANTOMs were MD5-confirmed into GCS once. Before re-uploading, look "
                  "for\n  the object under another prefix (--full, --also-prefix <wrapper>).")
    if risk:
        print("\n  ⚠ DO NOT FORMAT — see above.")
    elif problems:
        print("\n  ✅ Nothing at risk (only LARGER rows: the cloud copy is the better one).")
    else:
        print("\n  ✅ Every file is in the cloud at a matching size. SAFE TO FORMAT.")

    if tsv:
        cols = ["verdict", "device", "run", "file", "ssd_bytes", "cloud_bytes", "risk",
                "scheme", "receipt", "note", "dir"]
        with open(tsv, "w", encoding="utf-8") as fh:
            fh.write("\t".join(cols) + "\n")
            for p in problems:
                fh.write("\t".join(str(p.get(c, "")) for c in cols) + "\n")
        print(f"\n  📄 report: {tsv}  ({len(problems)} row(s))")
    return risk == 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Audit that every recording on a drive is really in GCS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit 0 = nothing at risk;  exit 1 = something is not safely in the cloud")
    ap.add_argument("drive", help="the drive or folder to audit")
    ap.add_argument("--also-prefix", action="append", default=[], metavar="NAME",
                    help="extra GCS top-level prefix to try (a former wrapper/dump "
                         "folder, e.g. 26_6). Repeatable. Emptied top-level folders on "
                         "the drive are added automatically.")
    ap.add_argument("--only", metavar="DEVICE",
                    help="audit only runs whose resolved device key is DEVICE")
    ap.add_argument("--full", action="store_true",
                    help="try every scheme for every run (slower; skips the "
                         "per-device-folder shortcut)")
    ap.add_argument("--ignore-receipts", action="store_true",
                    help="ignore the drive's .uploaded/.gcs_uploaded markers and judge "
                         "on the cloud alone")
    ap.add_argument("--tsv", metavar="PATH", help="write the full problem list here")
    ap.add_argument("--json", action="store_true",
                    help="print the problem list as JSON instead of the report")
    args = ap.parse_args()

    drive = os.path.abspath(args.drive)
    if not os.path.isdir(drive):
        raise SystemExit(f"not a directory: {drive}")

    quiet = args.json
    if not quiet:
        print(f"\nAuditing {drive} …")
    runs = scan(drive, use_receipts=not args.ignore_receipts)
    if not runs:
        print("  No data files found — nothing to audit.")
        return 0
    if not quiet:
        n_rec = sum(len(r["receipts"]) for r in runs)
        n_files = sum(len(r["files"]) for r in runs)
        print(f"  {len(runs)} runs · {n_files} files · "
              f"{gb(sum(r['bytes'] for r in runs)):.1f} GB · {n_rec} upload receipt(s)")

    wrappers = list(dict.fromkeys(list(args.also_prefix) + emptied_wrappers(drive, runs)))
    if wrappers and not quiet:
        print(f"  wrapper prefixes in play: {wrappers}")
    schemes = build_schemes(wrappers)

    cloud = Cloud(quiet=quiet)
    resolve(runs, schemes, cloud, full=args.full, quiet=quiet)
    if args.only:
        runs = [r for r in runs if (r.get("key") or ("",))[0] == args.only]
        if not runs:
            raise SystemExit(f"no runs resolved to device {args.only!r}")
    problems = judge(runs, cloud)

    if args.json:
        json.dump({"drive": drive, "runs": len(runs),
                   "files": sum(len(r["files"]) for r in runs),
                   "at_risk_bytes": sum(p["risk"] for p in problems),
                   "problems": problems}, sys.stdout, indent=2)
        print()
        return 0 if not sum(p["risk"] for p in problems) else 1
    return 0 if report(drive, runs, problems, cloud, tsv=args.tsv) else 1


if __name__ == "__main__":
    sys.exit(main())
