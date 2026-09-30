#!/usr/bin/env python3
"""How many of the cameras in cameras.txt are actually live on the dashboard.

Answers one question: of the list I am about to hand over, how many are
calibrated AND fully uploaded - and for the rest, what is the ONE thing wrong.

Deliberately outside the calibration repo. That repo lets exactly one file name
the dashboard API (its CLAUDE.md, "Cutover, authorised 2026-08-25"); this is a
local read-only checker, so it lives here instead of widening that allowlist.
It never writes: no POST, no PUT, no --fix. Nothing here can change the fleet.

    ./check.py                 check every camera in cameras.txt
    ./check.py 103 110 86      check these instead
    ./check.py --quiet         one summary line, for a glance

Configuration is the same as every other starcap tool, in the same order:
    DASHBOARD_URL     or ~/.config/starcap/dashboard_url
    STARCAP_PASSWORD  or ~/.config/starcap/dashboard_password
    STARCAP_PROJECT   optional; a second project's camera 51 is not ours
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CALIBRATION_PATH = "/api/ingest/camident/calibration"
WHITEBALANCE_PATH = "/api/ingest/camident/whitebalance"

# A RE-LENSED UNIT IS A DIFFERENT ID. Camera 31 that has been re-lensed is stored
# as "31.1", and a flat lookup on the bare number reports it missing - the exact
# false alarm that once made 67 good cameras read as uncalibrated. So each number
# is tried bare first, then as revisions, and whichever answers is the one this
# lens actually lives under. The id that answered is printed, never assumed.
REVISIONS = ("", ".1", ".2", ".3")

# THE MODEL IS THE TELL. A camera the old production pipeline calibrated stores
# K/R/T/dist and an rmse and nothing else - no model, no method, no evidence. A
# camera THIS bench calibrated stores model=fisheye_kb4 plus an evidence bundle.
# Both are "a calibration on the dashboard", and calling them the same thing is
# how a unit that still carries the pinhole fit this repo exists to REPLACE gets
# counted as done. So they are separate states and the legacy one names itself.
NEW_MODEL = "fisheye_kb4"

# One line per state, worst first, so the summary reads as a worklist.
STATES = [
    ("absent",          "not uploaded - nothing on the dashboard"),
    ("legacy",          "still on the OLD pinhole calibration - needs this bench"),
    ("no_evidence",     "fisheye fit stored, evidence bundle never declared"),
    ("partial",         "evidence bundle INCOMPLETE - upload died partway"),
    ("verdict_missing", "bundle complete, but no verdict was recorded"),
    ("unverified",      "bundle complete, verdict is not an unqualified PASS"),
    ("error",           "could not be checked"),
    ("live",            "live - calibrated, uploaded, field ready"),
]
LABEL = dict(STATES)


def _setting(env: str, filename: str) -> str | None:
    value = os.environ.get(env, "").strip()
    if value:
        return value
    path = Path.home() / ".config" / "starcap" / filename
    return path.read_text().strip() if path.is_file() else None


def _get(base: str, password: str, path: str, camera: str):
    """The stored document for one id, or None on 404. Network failure raises."""
    url = "{}{}?camera={}".format(base.rstrip("/"), path, camera)
    request = urllib.request.Request(url, headers={"x-starcap-password": password})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    return body if body.get("ok") else None


def _when(epoch_ms) -> str:
    """calibrated_at is epoch MILLISECONDS, not a date string. Sliced as text it
    prints "1784039894", which reads like an id and dates nothing."""
    try:
        return datetime.fromtimestamp(
            float(epoch_ms) / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return "date unknown"


def _fov(fov) -> float | None:
    """Horizontal FOV of the left eye. fov_deg is per-eye - {"camera0": {"h": ...}} -
    so a bare float() on it raises, and the two eyes agree to ~0.2 deg anyway."""
    if isinstance(fov, dict):
        eye = fov.get("camera0") or fov.get("camera1") or {}
        if isinstance(eye, dict):
            try:
                return float(eye.get("h"))
            except (TypeError, ValueError):
                return None
    return None


def classify(base: str, password: str, project: str | None, number: str) -> dict:
    """What the dashboard holds for one camera number."""
    row = {"asked": number, "id": None, "state": "absent", "note": "",
           "files": 0, "wb": None, "fov": None}
    try:
        stored = None
        found = []
        for revision in REVISIONS:
            candidate = "{}{}".format(number, revision)
            if project:
                candidate = "{}:{}".format(project, candidate)
            body = _get(base, password, CALIBRATION_PATH, candidate)
            calibration = (body or {}).get("calibration")
            if calibration:
                found.append((candidate, calibration))
        # PREFER THE FISHEYE DOC, don't just take the first id that answers. A
        # re-lensed unit can have BOTH - a stale pinhole fit under the bare number
        # and this bench's fit under "N.1" - and stopping at the bare one reports
        # a camera that was done today as still needing doing.
        for candidate, calibration in found:
            if str(calibration.get("model") or "").strip() == NEW_MODEL:
                stored, row["id"] = calibration, candidate
                break
        else:
            if found:
                stored, row["id"] = found[0][1], found[0][0]
    except Exception as error:                                    # noqa: BLE001
        row["state"], row["note"] = "error", "{}: {}".format(
            type(error).__name__, error)
        return row

    if not stored:
        return row

    # Mirrors the dashboard's own field-ready rule, and the calibration repo's
    # upload_audit: stored numbers are not the same thing as a finished upload.
    evidence = stored.get("evidence") or {}
    verdict = (evidence.get("acceptance") or {}).get("verdict")
    files = evidence.get("files") or []
    row["files"] = len(files)
    row["fov"] = _fov(stored.get("fov_deg"))
    if str(stored.get("model") or "").strip() != NEW_MODEL:
        row["state"] = "legacy"
        row["note"] = "calibrated {}".format(_when(stored.get("calibrated_at")))
    elif not files:
        row["state"] = "no_evidence"
    elif evidence.get("status") != "complete":
        row["state"] = "partial"
        row["note"] = "status={}".format(evidence.get("status"))
    elif verdict is None:
        row["state"] = "verdict_missing"
    elif str(verdict).strip().upper() != "PASS":
        row["state"] = "unverified"
        row["note"] = "verdict={}".format(verdict)
    else:
        row["state"] = "live"

    # WHITE BALANCE, reported but never judged here. The dashboard tile needs
    # BOTH geometry and colour before it goes green, and colour is measured on a
    # different bench entirely - so a camera can be "live" by this tool and still
    # read amber over there. Naming the gap beats implying there isn't one.
    try:
        row["wb"] = bool(_get(base, password, WHITEBALANCE_PATH, row["id"]))
    except Exception:                                             # noqa: BLE001
        row["wb"] = None
    return row


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    wanted = [a for a in argv if not a.startswith("-")]
    if not wanted:
        listing = HERE / "cameras.txt"
        if not listing.is_file():
            print("no cameras.txt beside this script, and none given on the "
                  "command line", file=sys.stderr)
            return 2
        wanted = [line.split("#")[0].strip()
                  for line in listing.read_text().splitlines()]
        wanted = [w for w in wanted if w]

    base = _setting("DASHBOARD_URL", "dashboard_url")
    if not base:
        print("no dashboard configured: set DASHBOARD_URL or write "
              "~/.config/starcap/dashboard_url", file=sys.stderr)
        return 2
    try:
        password = _setting("STARCAP_PASSWORD", "dashboard_password")
    except Exception:                                             # noqa: BLE001
        password = None
    if not password:
        print("no dashboard password: set STARCAP_PASSWORD or write "
              "~/.config/starcap/dashboard_password", file=sys.stderr)
        return 2
    project = (os.environ.get("STARCAP_PROJECT", "").strip() or None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(
            lambda n: classify(base, password, project, n), wanted))

    live = [r for r in rows if r["state"] == "live"]
    if quiet:
        print("{}/{} live on {}".format(len(live), len(rows), base))
        return 0 if len(live) == len(rows) else 1

    print()
    print("  {}/{} CALIBRATED AND UPLOADED".format(len(live), len(rows)))
    print("  {}".format(base))
    print()

    width = max(len(r["asked"]) for r in rows)
    for state, description in STATES:
        group = [r for r in rows if r["state"] == state]
        if not group:
            continue
        mark = "OK" if state == "live" else "!!"
        print("  {} {} - {}".format(mark, len(group), description))
        for r in sorted(group, key=lambda r: int(r["asked"])
                        if r["asked"].isdigit() else 0):
            shown = r["id"] or r["asked"]
            # The id that ANSWERED, when it is not the number that was asked for:
            # "110 -> 110.1" is the difference between a missing camera and a
            # re-lensed one, and it is the whole reason this is not a flat lookup.
            rename = "" if shown == r["asked"] else "  (stored as {})".format(shown)
            extra = []
            if state == "live" and r["wb"] is False:
                extra.append("NO white balance - tile stays amber")
            if r["note"]:
                extra.append(r["note"])
            if state == "live" and r["files"]:
                extra.append("{} files".format(r["files"]))
            if r.get("fov"):
                extra.append("{} deg".format(round(float(r["fov"]))))
            print("       {:<{w}}{}{}".format(
                r["asked"], rename,
                ("  - " + "; ".join(extra)) if extra else "", w=width))
        print()

    no_wb = [r for r in live if r["wb"] is False]
    if no_wb:
        print("  note: {} live camera(s) have no white balance on file, so their "
              "dashboard\n        tile will not be green until colour is measured: "
              "{}".format(len(no_wb), ", ".join(r["asked"] for r in no_wb)))
        print()
    return 0 if len(live) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
