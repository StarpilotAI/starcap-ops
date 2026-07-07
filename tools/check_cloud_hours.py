#!/usr/bin/env python3
"""
StarCap CLOUD footage-hours measurer  (standalone, READ-ONLY)
=============================================================

Reports how much footage is actually in the cloud (GCS), measured the ACCURATE
way — NOT from opm's contact sheets (which cap long runs and under-report):

  * total video STORAGE in the cloud (TB)
  * total footage HOURS  (true count)

How it works: the exact total video byte-count comes from the dashboard. The
only unknown is "how many bytes = one hour", and StarCap's HEVC bitrate is
near-constant, so it samples real cloud clips with ffprobe, measures the true
GB/hour, and multiplies. It also prints the dashboard's own (opm-based) number
for comparison.

READ-ONLY: it only reads. It never uploads, deletes, or changes anything.

USAGE
-----
  python3 check_cloud_hours.py
  STARCAP_PASSWORD=xxxx python3 check_cloud_hours.py     # if the password changed

CONFIG (edit the two constants, or set env vars)
  DASHBOARD_URL     default https://starcap.dev
  STARCAP_PASSWORD  the shared dashboard password

REQUIREMENTS
  - Python 3
  - ffprobe (ffmpeg) for an EXACT bitrate measurement. Without it, falls back to
    a calibrated 9.0 GB/hour (still close — the bitrate barely varies).
        Ubuntu: sudo apt install ffmpeg | Mac: brew install ffmpeg
"""
import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

# ---- CONFIG ----------------------------------------------------------------
DASHBOARD = os.environ.get("DASHBOARD_URL", "https://starcap.dev").rstrip("/")
PASSWORD = os.environ.get("STARCAP_PASSWORD", "starchild")
# ----------------------------------------------------------------------------

GB_PER_HOUR = 9.0       # fallback only (measured constant for StarCap footage)
SAMPLE = 150            # how many cloud clips to probe (bitrate + corrupt-share estimate)
WORKERS = 12
VERIFY_BATCH = 250
clean = lambda s: re.sub(r"[^A-Za-z0-9_.\-]+", "_", s)


def _get(path, timeout=120):
    r = urllib.request.Request(f"{DASHBOARD}{path}", headers={"X-Starcap-Password": PASSWORD})
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())


def _post(path, body, timeout=120):
    r = urllib.request.Request(f"{DASHBOARD}{path}", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-Starcap-Password": PASSWORD})
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())


def signed_url(obj, tries=3):
    q = urllib.parse.quote(obj)
    for i in range(tries):
        try:
            r = urllib.request.Request(f"{DASHBOARD}/api/video-url?path={q}",
                                       headers={"X-Starcap-Password": PASSWORD})
            with urllib.request.urlopen(r, timeout=30) as x:
                return json.loads(x.read()).get("url")
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.6 * (i + 1))


def ffprobe_classify(url):
    """Probe a clip and classify it:
       ('good', seconds) — decodable, real duration
       ('corrupt', 0)    — ffprobe ran but the file has no index (unfinalized/aborted
                           recording: 'moov atom not found') → undecodable
       ('error', 0)      — couldn't probe for another reason (network/timeout)
    We separate 'corrupt' from 'error' so a network blip isn't mistaken for corruption."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", url],
            capture_output=True, text=True, timeout=120)
        s = out.stdout.strip()
        if s:
            try:
                return ("good", float(s))
            except ValueError:
                return ("error", 0.0)
        err = (out.stderr or "").lower()
        if "moov atom not found" in err or "invalid data found" in err:
            return ("corrupt", 0.0)
        return ("error", 0.0)
    except Exception:
        return ("error", 0.0)


def human(b):
    return f"{b/1e12:.2f} TB" if b >= 1e12 else f"{b/1e9:.1f} GB"


def fmt_hours(h):
    return f"{h:.0f} h  ({int(h)}h {int(round((h-int(h))*60))}m)"


def main():
    print(f"Asking {DASHBOARD} for the cloud video total…")
    try:
        foot = _get("/api/footage")
    except Exception as e:
        raise SystemExit(f"Couldn't reach the dashboard: {e}\n"
                         f"Check DASHBOARD_URL / STARCAP_PASSWORD.")
    totals = foot.get("totals", {})
    cloud_bytes = totals.get("cloud_bytes", 0)
    dash_hours = totals.get("cloud_hours", 0)
    print(f"  cloud video storage : {human(cloud_bytes)}")
    if not cloud_bytes:
        raise SystemExit("Dashboard reports 0 cloud bytes — nothing to measure.")

    gbph = GB_PER_HOUR
    measured = False
    corrupt_frac = 0.0
    if shutil.which("ffprobe"):
        # sample real clip durations to measure the true bitrate
        runs = [(r["device_id"], r["run"]) for r in foot.get("runs", []) if r.get("in_cloud")]
        random.shuffle(runs)
        specs = [{"device_id": d, "run": r} for d, r in runs[:120]]
        chunks = []
        for i in range(0, len(specs), VERIFY_BATCH):
            try:
                resp = _post("/api/ssd/verify", {"runs": specs[i:i+VERIFY_BATCH]})
            except Exception:
                continue
            for c in resp.get("runs", []):
                sizes = c.get("cloud_sizes") or {}
                for n in (c.get("cloud_files") or []):
                    if n.startswith("stereo_") and n.endswith(".mp4") and sizes.get(n):
                        chunks.append((f"{clean(c['device_id'])}/{clean(c['run'])}/{n}", sizes[n]))
        random.shuffle(chunks)
        sample = chunks[:SAMPLE]
        print(f"  measuring bitrate from {len(sample)} real clips (ffprobe)…")

        def probe(obj_size):
            obj, size = obj_size
            u = signed_url(obj)
            if not u:
                return ("error", size, 0.0)
            status, secs = ffprobe_classify(u)
            return (status, size, secs)

        good_b = good_s = corrupt_b = 0.0
        good_n = corrupt_n = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for status, size, secs in ex.map(probe, sample):
                if status == "good":
                    good_b += size; good_s += secs; good_n += 1
                elif status == "corrupt":
                    corrupt_b += size; corrupt_n += 1
                # 'error' (network) → ignore, neither good nor corrupt
        if good_s > 0:
            gbph = (good_b / 1e9) / (good_s / 3600)
            measured = True
            print(f"  measured bitrate    : {gbph:.2f} GB/hour  (from {good_n} good clips)")
        # Estimate the corrupt (undecodable) share of cloud storage from the sample and
        # exclude it: those bytes are real data but have no playable duration. Corrupt
        # files are rare, so the sample often finds none — in which case their share is
        # below the sample's resolution and the number is unaffected.
        sampled = good_b + corrupt_b
        corrupt_frac = (corrupt_b / sampled) if sampled > 0 else 0.0
        if corrupt_n:
            print(f"  corrupt in sample   : {corrupt_n}/{good_n + corrupt_n} clips "
                  f"≈ {corrupt_frac*100:.2f}% of bytes — EXCLUDED from hours")
        else:
            print(f"  corrupt in sample   : none found — corrupt share below sample resolution (no adjustment)")

    good_bytes = cloud_bytes * (1 - corrupt_frac)     # drop the estimated corrupt share
    hours = (good_bytes / 1e9) / gbph
    tag = "measured by ffprobe, corrupt excluded" if measured else f"ESTIMATE @ {GB_PER_HOUR} GB/h (install ffmpeg for exact)"
    print("\n" + "=" * 60)
    print(f"  CLOUD VIDEO STORAGE : {human(cloud_bytes)}" + (f"  ({human(cloud_bytes-good_bytes)} corrupt, excluded)" if corrupt_frac > 0 else ""))
    print(f"  CLOUD FOOTAGE HOURS : {fmt_hours(hours)}   ({tag})")
    print("=" * 60)
    print(f"\n  for comparison, the dashboard's own number : {dash_hours:.0f} h")
    if hours > dash_hours * 1.1:
        print(f"  (the dashboard reads lower because it only counts opm-measured,")
        print(f"   correctly-keyed runs — this number reflects ALL video in the cloud.)")


if __name__ == "__main__":
    main()
