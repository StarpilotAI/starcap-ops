#!/usr/bin/env python3
"""
opm-lan web UI — same UI as opm, but recordings come over ETHERNET, not SD cards.

It reuses opmlan.py's engine (discover / identify / copy / verify / delete over
SSH) and serves the SAME API opm's web UI speaks, so the frontend is opm's, almost
verbatim. A background scanner keeps the recorder list live; a copy runs as a
background job and the page polls /api/status for per-recorder progress.

Run:   python3 webui.py        →  http://127.0.0.1:8770
No root needed — there are no card mounts; the transport is rsync-over-SSH.
"""
import copy as _copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import opmlan

HOST = "127.0.0.1"
SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"
CFG = opmlan.load_config()
PORT = int(os.environ.get("OPM_LAN_PORT", str(CFG.get("port", 8770))))

# --------------------------------------------------------------------------- #
# background scanner — keeps the recorder list live (cheap), like opm's detect
# --------------------------------------------------------------------------- #
RECORDERS = []          # list of card dicts (slot/ip/device/...)
IFACE = None
SCAN_LOCK = threading.Lock()
_scan_stop = threading.Event()


def scanner_loop():
    global RECORDERS, IFACE
    while not _scan_stop.is_set():
        try:
            cards, iface = opmlan.detect_recorders(CFG)
            for c in cards:
                c["size_h"] = opmlan._fmt_size(c.get("size"))
            with SCAN_LOCK:
                RECORDERS = cards
                IFACE = iface
        except Exception as e:                           # noqa: BLE001
            print(f"[scan] {e}")
        _scan_stop.wait(4)


def recorders():
    with SCAN_LOCK:
        return [dict(c) for c in RECORDERS]


def recorder_by_slot(slot):
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return None
    return next((c for c in recorders() if c["slot"] == slot), None)


# --------------------------------------------------------------------------- #
# background analysis (contact sheets + dashboard report) — reuses opm's stack
# --------------------------------------------------------------------------- #
ANALYSIS_STATUS = {"enabled": True, "total": 0, "analyzed": 0, "reported": 0,
                   "pending": 0, "errors": 0, "current": None, "reason": None,
                   "rollup": None}
_analysis_stop = threading.Event()


def analysis_rollup(analysis):
    """Truthful CUMULATIVE state read from the on-disk markers. The sweep's own
    `analyzed`/`reported` counters are per-pass (reset every sweep, and `reported`
    only counts THIS pass's uploads), so they can't answer 'how much is actually on
    the dashboard'. The markers can. Classify every session:
      uploaded    — reported_at set  → contact sheet is on the dashboard
      built       — sheet built, not yet reported (transient; next pass uploads it)
      unfinalized — sheet_error       → undecodable/aborted recording (row, no thumbnail)
      queued      — not processed yet"""
    total = uploaded = built = unfinalized = queued = 0
    try:
        for sd, _cid, _run in analysis.iter_sessions(CFG["destination"]):
            total += 1
            try:
                m = analysis.read_marker(sd)
            except Exception:                              # noqa: BLE001 - a bad marker != crash
                m = {}
            if m.get("reported_at"):
                uploaded += 1
            elif m.get("sheet"):
                built += 1
            elif m.get("sheet_error"):
                unfinalized += 1
            else:
                queued += 1
    except Exception as e:                                 # noqa: BLE001
        print(f"[analysis] rollup: {e}")
    return {"total": total, "uploaded": uploaded, "built": built,
            "unfinalized": unfinalized, "queued": queued}


def _analysis_loop():
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    analysis = opmlan.load_analysis(CFG)
    if not analysis:
        ANALYSIS_STATUS.update(enabled=False, reason=opmlan._ANALYSIS.get("reason"))
        print(f"[analysis] disabled: {opmlan._ANALYSIS.get('reason')}")
        return
    print("[analysis] contact-sheet builder + dashboard reporter on (reusing opm)")
    while not _analysis_stop.is_set():
        try:
            analysis.sweep(CFG, status=ANALYSIS_STATUS, build_sheets=not JOB.running)
            # cumulative truth from the markers, computed once per sweep (not per poll)
            ANALYSIS_STATUS["rollup"] = analysis_rollup(analysis)
        except Exception as e:                           # noqa: BLE001
            print(f"[analysis] {e}")
        _analysis_stop.wait(3)


# --------------------------------------------------------------------------- #
# shared job state (one job at a time) — same shape as opm's Job
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.starting = False
        self.mode = None
        self.do_delete = False
        self.started_at = None
        self.finished_at = None
        self.cards = {}
        self.log = []
        self.error = None
        self.io = {"read_mbps": 0.0, "write_mbps": 0.0}
        self.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}
        self.cancel = threading.Event()
        self.procs = set()
        self.procs_lock = threading.Lock()
        # recorders connected AFTER the session started and added via /api/add;
        # the run loop drains these into its work queue as worker slots free up.
        self.add_lock = threading.Lock()
        self.pending_adds = []

    def append_log(self, msg):
        self.log.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running, "starting": self.starting,
                "cancelling": self.cancel.is_set(), "mode": self.mode,
                "do_delete": self.do_delete, "started_at": self.started_at,
                "finished_at": self.finished_at, "error": self.error,
                "io": dict(self.io), "overall": dict(self.overall),
                "cards": {str(k): _copy.deepcopy(v) for k, v in self.cards.items()},
                "log": self.log[-300:],
            }


JOB = Job()
SECTOR = 512

# --------------------------------------------------------------------------- #
# copy-history ledger — one JSONL record per recorder per job: what got docked
# when, how much data it actually copied (new bytes, not pre-existing dest
# bytes), and onto WHICH SSD (mount + volume label + fs UUID, so records stay
# attributable after the drive is remounted elsewhere). Lives on this laptop
# (not the SSD) so one search covers every drive ever used. Machine-local,
# git-ignored. Searched via /api/history?q=<text>&day=YYYY-MM-DD.
# --------------------------------------------------------------------------- #
HISTORY_FILE = SCRIPT_DIR / "copy_history.jsonl"
HIST_LOCK = threading.Lock()


def history_append(rec):
    with HIST_LOCK:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")


def history_load():
    out = []
    try:
        with HIST_LOCK, open(HISTORY_FILE) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue                    # a torn/corrupt line never hides the rest
    except OSError:
        pass
    return out


def ssd_info(dest):
    """Identity of the drive behind `dest`: mountpoint, volume label, fs UUID.
    JSON output from findmnt/lsblk — labels with spaces ('Extreme SSD') survive."""
    info = {"path": None, "label": None, "uuid": None}
    try:
        fs = json.loads(subprocess.run(
            ["findmnt", "-J", "-o", "TARGET,SOURCE", "--target", os.path.abspath(dest)],
            text=True, capture_output=True).stdout)["filesystems"][0]
        info["path"] = fs.get("target")
        src = fs.get("source") or ""
        if src.startswith("/dev/"):
            bd = json.loads(subprocess.run(
                ["lsblk", "-J", "-o", "LABEL,UUID", src],
                text=True, capture_output=True).stdout)["blockdevices"][0]
            info["label"] = bd.get("label")
            info["uuid"] = bd.get("uuid")
    except Exception:                              # noqa: BLE001 - identity is best-effort
        pass
    if not info["label"]:                          # unlabeled fs / not a block device
        info["label"] = os.path.basename(info["path"] or os.path.abspath(dest)) or "internal disk"
    return info


def api_history(device, day_from, day_to, q=""):
    """Filter the ledger. `device` is an EXACT device/card_id match (the UI's
    dropdown — substring would make starcap1 swallow starcap10..19/100),
    `day_from`/`day_to` an optional inclusive date range (either end alone
    works; both empty = all time). `q` stays as a free substring filter
    (device/ip/ssd-label) for curl use. Also returns every distinct device the
    ledger has ever seen, so the UI dropdown can include odd ones (slotN,
    hostname ids) beyond the standard starcap1..100."""
    recs = history_load()
    all_devices = sorted({(r.get("device") or r.get("card_id") or "") for r in recs} - {""},
                         key=str.lower)
    device = (device or "").strip().lower()
    day_from, day_to = (day_from or "").strip(), (day_to or "").strip()
    q = (q or "").strip().lower()
    if device:
        recs = [r for r in recs
                if (r.get("device") or r.get("card_id") or "").lower() == device]
    if q:
        recs = [r for r in recs
                if q in " ".join(str(r.get(k) or "") for k in ("device", "card_id", "ip")).lower()
                or q in str((r.get("ssd") or {}).get("label") or "").lower()]
    if day_from:
        recs = [r for r in recs if (r.get("date") or "") >= day_from]
    if day_to:
        recs = [r for r in recs if (r.get("date") or "") <= day_to]
    recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return {"records": recs[:500], "count": len(recs),
            "bytes_copied": sum(r.get("bytes_copied") or 0 for r in recs),
            "devices": len({r.get("device") or r.get("card_id") for r in recs}),
            "all_devices": all_devices, "truncated": len(recs) > 500}


def _diskstats():
    out = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) >= 14:
                    out[p[2]] = (int(p[5]), int(p[9]))
    except OSError:
        pass
    return out


def dev_for_path(path):
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    src = subprocess.run(["findmnt", "-nro", "SOURCE", "--target", p],
                         text=True, capture_output=True).stdout.strip()
    return os.path.basename(src) if src.startswith("/dev/") else ""


def _fmt_eta(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def sampler_loop(iface, dest_dev, cfg, stop_evt):
    """Twice a second: read MB/s = ethernet RX on the wired link (the analog of opm
    reading the card), write MB/s = destination disk writes, plus per-recording copy
    progress, smoothed per-recorder rate + ETA, and an overall ETA — all from the LOCAL
    destination growth, exactly like opm (the dest is local here too)."""
    flatten = cfg["copy"].get("flatten", True)
    rate = {}
    prev_rx = opmlan.net_rx_bytes(iface)
    prev, prev_t = _diskstats(), time.monotonic()
    while not stop_evt.is_set():
        time.sleep(0.5)
        cur, now = _diskstats(), time.monotonic()
        dt = max(1e-3, now - prev_t)
        rx = opmlan.net_rx_bytes(iface)
        read_mbps = max(0, rx - prev_rx) / 1e6 / dt
        wr = max(0, cur.get(dest_dev, (0, 0))[1] - prev.get(dest_dev, (0, 0))[1])
        write_mbps = wr * SECTOR / 1e6 / dt
        prev, prev_t, prev_rx = cur, now, rx

        with JOB.lock:
            JOB.io = {"read_mbps": read_mbps, "write_mbps": write_mbps}
            active = [(v["slot"], v.get("dest"), v["bytes_total"], v["sessions"],
                       v["stage"], v.get("namemaps", {}))
                      for v in JOB.cards.values()
                      if v["stage"] in ("copying", "verifying", "deleting") and v.get("dest")]

        tot_remaining = tot_rate = 0.0
        for slot, dest, bytot, sessions, stage, namemaps in active:
            done = 0                       # sum of THIS job's sessions only, so a
            sdone = 0                      # pre-existing dest folder can't inflate %
            for s in sessions:
                nm = namemaps.get(s["path"], {})
                sp = os.path.join(opmlan.dest_path_for(dest, s["path"], flatten),
                                  nm.get(s["name"], s["name"]))
                cb = opmlan.dir_stats(sp)[0] if os.path.isdir(sp) else 0
                done += cb
                sd = s["bytes"] > 0 and cb >= s["bytes"]
                if sd:
                    sdone += 1
                with JOB.lock:
                    s["copied"], s["done"] = cb, sd

            st = rate.get(slot)
            inst = max(0.0, (done - st["bytes"]) / dt) if st else 0.0
            ema = (0.4 * st["ema"] + 0.6 * inst) if st else inst
            rate[slot] = {"bytes": done, "ema": ema}

            pct = min(99, int(done * 100 / bytot)) if bytot else 0
            remaining = max(0, bytot - done)
            eta = ""
            if stage == "copying" and ema > 1e5 and remaining > 0:
                eta = _fmt_eta(remaining / ema)
            if stage == "copying":
                tot_remaining += remaining
                tot_rate += ema
            with JOB.lock:
                if slot in JOB.cards:
                    JOB.cards[slot]["progress"].update(
                        percent=pct, bytes_done=done, sessions_done=sdone, eta=eta,
                        rate_mbps=round(ema / 1e6, 1))

        overall_eta = ""
        if tot_rate > 1e5 and tot_remaining > 0:
            overall_eta = _fmt_eta(tot_remaining / tot_rate)
        with JOB.lock:
            JOB.overall = {"eta": overall_eta, "rate_mbps": round(tot_rate / 1e6, 1),
                           "remaining": int(tot_remaining)}


def build_card(cfg, c):
    """Inventory one recorder (bytes/files/sessions/dest-names) and return its
    JOB.cards progress entry. Used to seed a job AND to add a recorder mid-session."""
    inv = opmlan.remote_inventory(cfg, c["ip"])
    info = opmlan.identify(cfg, c["ip"], force=True)
    device = info.get("name") or ""
    # best-guess destination for this recorder, so each session's PRE-existing
    # dest bytes can be snapshotted now — the copy-history ledger subtracts them
    # to report bytes actually written THIS job (an earlier copy of the same
    # session must not count again). process_recorder may suffix the cid on a
    # rare name collision; then pre reads as 0 and the ledger just over-counts
    # that one record slightly.
    dest_card = os.path.join(cfg["destination"], opmlan.card_id_for(info, c["slot"], cfg))
    flatten = cfg["copy"].get("flatten", True)
    tb = tf = 0
    sessions, namemaps = [], {}
    for p in cfg["copy_paths"]:
        pinfo = (inv.get("paths") or {}).get(p) or {}
        if not pinfo.get("exists"):
            continue
        tb += pinfo.get("bytes", 0)
        tf += pinfo.get("files", 0)
        runs = [s["name"] for s in pinfo.get("sessions", [])]
        namemaps[p] = opmlan.dest_session_names(runs, device)
        for s in pinfo.get("sessions", []):
            s = dict(s)
            s["path"] = p
            s["copied"] = 0
            s["done"] = False
            dsp = os.path.join(opmlan.dest_path_for(dest_card, p, flatten),
                               namemaps[p].get(s["name"], s["name"]))
            s["pre"] = opmlan.dir_stats(dsp)[0] if os.path.isdir(dsp) else 0
            sessions.append(s)
    return {
        "slot": c["slot"], "stage": "pending", "status": "pending",
        "card_id": device or f"slot{c['slot']}", "device": device, "disk": c["ip"],
        "label": c.get("label"), "size_h": opmlan._fmt_size(tb),
        "mounted_at": c["ip"], "bytes_total": tb, "files_total": tf,
        "dirs_total": len(sessions), "sessions": sessions, "namemaps": namemaps,
        "dest": None, "src": c["ip"],
        "progress": {"percent": 0, "bytes_done": 0, "sessions_done": 0,
                     "eta": "", "del_percent": 0, "rate_mbps": 0.0},
        "copied": [], "missing": [], "deleted": False, "error": None,
    }


def run_job(no_delete, slots, recheck=False):
    cfg = CFG
    try:
        cards = recorders()
        usable = [c for c in cards if c.get("eligible")]
        if slots:
            usable = [c for c in usable if c["slot"] in slots]
        entries = {c["slot"]: build_card(cfg, c) for c in usable}
    except Exception as e:                                # noqa: BLE001
        with JOB.lock:
            JOB.running = False
            JOB.starting = False
            JOB.error = str(e)
            JOB.append_log(f"!! could not start job: {e}")
        return

    do_delete = not no_delete
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    assigned, lock = set(), threading.Lock()

    with JOB.lock:
        JOB.running = True
        JOB.starting = False
        JOB.mode = ("re-copy + verify" if recheck
                    else "copy + delete" if do_delete else "copy")
        JOB.do_delete = do_delete
        JOB.started_at = ts
        JOB.finished_at = None
        JOB.error = None
        JOB.log = []
        JOB.io = {"read_mbps": 0.0, "write_mbps": 0.0}
        JOB.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}
        JOB.cancel.clear()
        JOB.cards = {}
    with JOB.add_lock:
        JOB.pending_adds = []
    with JOB.procs_lock:
        JOB.procs.clear()
    with JOB.lock:
        JOB.cards.update(entries)
        JOB.append_log(f"Starting {JOB.mode.upper()} on {len(usable)} recorder(s); "
                       f"delete={'yes' if do_delete else 'no'}")

    stop_evt = threading.Event()
    dest_dev = dev_for_path(cfg["destination"])
    iface = IFACE or opmlan.wired_iface_subnet(cfg)[0]   # self-heal if scanner hasn't set it yet
    threading.Thread(target=sampler_loop, args=(iface, dest_dev, cfg, stop_evt),
                     daemon=True).start()

    def on_stage(slot, s, info=None):
        with JOB.lock:
            c = JOB.cards.get(slot)
            if c is None:
                return
            if bool(info) and "total" in info:
                c["progress"]["sheet"] = {"done": info.get("done"), "total": info.get("total"),
                                          "session": info.get("session")}
                return
            c["stage"] = s
            if info and info.get("dest"):
                c["dest"] = info["dest"]
            if info and info.get("src"):
                c["src"] = info["src"]
            JOB.append_log(f"[slot {slot}] {s}")

    def do_one(card):
        res = opmlan.process_recorder(card, cfg, ts, do_delete, assigned, lock,
                                      on_stage=on_stage, stop=lambda: JOB.cancel.is_set(),
                                      procs=(JOB.procs, JOB.procs_lock),
                                      verify_copy=do_delete, recheck=recheck)
        with JOB.lock:
            cur = JOB.cards.get(res["slot"])
            if cur is not None:
                cur.update(status=res["status"], card_id=res["card_id"],
                           copied=res["copied"], missing=res["missing"],
                           deleted=res["deleted"], error=res["error"], stage="done")
                if res["status"] == "ok":
                    cur["progress"].update(percent=100, sessions_done=cur["dirs_total"],
                                           bytes_done=cur["bytes_total"], eta="")
                    for s in cur["sessions"]:
                        s["done"], s["copied"] = True, s["bytes"]
                if res["deleted"]:
                    cur["progress"]["del_percent"] = 100
            JOB.append_log(f"[slot {res['slot']}] {res['status']} - "
                           f"copied {len(res['copied'])}, missing {len(res['missing'])}"
                           f"{', deleted source' if res['deleted'] else ''}")
            if res["error"]:
                JOB.append_log(f"[slot {res['slot']}] !! {res['error']}")
            # snapshot what the ledger needs while still under the lock
            hist = None
            if cur is not None:
                copied_b = sum(max(0, (s.get("copied") or 0) - (s.get("pre") or 0))
                               for s in cur["sessions"])
                new_n = sum(1 for s in cur["sessions"] if (s.get("pre") or 0) < s["bytes"])
                hist = {"mode": JOB.mode, "job_started": JOB.started_at,
                        "sessions_total": len(cur["sessions"]), "sessions_new": new_n,
                        "bytes_total": cur["bytes_total"], "bytes_copied": copied_b,
                        "ip": cur.get("disk")}
        # record to the copy-history ledger (outside the lock — file + subprocess I/O);
        # skip only a cancel that never copied a byte
        if hist and not (res["status"] == "cancelled" and hist["bytes_copied"] == 0):
            now = datetime.now()
            try:
                history_append(dict(hist, ts=int(time.time()),
                                    date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"),
                                    device=res.get("device") or "", card_id=res["card_id"],
                                    status=res["status"], deleted=res["deleted"],
                                    dest=res.get("dest"), ssd=ssd_info(cfg["destination"])))
            except Exception as e:                        # noqa: BLE001 - ledger never kills a copy
                with JOB.lock:
                    JOB.append_log(f"[history] could not record slot {res['slot']}: {e}")
        return res

    try:
        N = cfg["copy"].get("max_workers", 4) if cfg["copy"].get("parallel", True) else 1
        N = max(1, N)
        pending = deque(usable)                            # dynamic work queue
        with ThreadPoolExecutor(max_workers=N) as ex:
            futures = {}
            while not JOB.cancel.is_set():
                with JOB.add_lock:                         # recorders added mid-session
                    while JOB.pending_adds:
                        pending.append(JOB.pending_adds.pop(0))
                while len(futures) < N and pending:         # fill free worker slots
                    c = pending.popleft()
                    futures[ex.submit(do_one, c)] = c
                if not futures and not pending:
                    with JOB.add_lock:                      # one last look for a late add
                        if JOB.pending_adds:
                            continue
                    break                                    # idle + empty → session done
                done, _ = wait(list(futures), timeout=1, return_when=FIRST_COMPLETED)
                for f in done:
                    futures.pop(f, None)
            # On cancel or completion, the `with` block's exit waits for any in-flight
            # copies to wind down (process_recorder honors stop()).
    except Exception as e:                                # noqa: BLE001
        with JOB.lock:
            JOB.error = str(e)
            JOB.append_log(f"!! job error: {e}")
    finally:
        stop_evt.set()
        with JOB.lock:
            JOB.running = False
            JOB.starting = False
            JOB.finished_at = datetime.now().strftime("%Y%m%d-%H%M%S")
            JOB.io = {"read_mbps": 0.0, "write_mbps": 0.0}
            JOB.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}
            JOB.append_log("Job finished.")


# --------------------------------------------------------------------------- #
# preview helpers (one-frame thumbnail / short clip, streamed over SSH)
# --------------------------------------------------------------------------- #
_SAFE_SEG = re.compile(r"^[A-Za-z0-9._-]+$")
_VID_EXT = (".mp4", ".mov", ".mkv", ".avi")


def _remote_session_dir(slot, session):
    """(ip, remote_session_dir) for (slot, source-run-name), validated; or (None, None)."""
    if not session or not _SAFE_SEG.match(session) or session in (".", ".."):
        return None, None
    card = recorder_by_slot(slot)
    if not card:
        return None, None
    ip = card["ip"]
    for p in CFG["copy_paths"]:
        rdir = p.rstrip("/") + "/" + session
        r = opmlan._ssh(CFG, ip, f"test -d {opmlan._q(rdir)} && echo yes", timeout=10)
        if "yes" in (r.stdout or ""):
            return ip, rdir
    return None, None


def session_videos(slot, session):
    ip, rdir = _remote_session_dir(slot, session)
    if not ip:
        return []
    r = opmlan._ssh(CFG, ip, f"cd {opmlan._q(rdir)} && for f in *; do "
                             f"[ -f \"$f\" ] && printf '%s\\t%s\\n' \"$(stat -c%s \"$f\")\" \"$f\"; done", timeout=15)
    out = []
    for line in (r.stdout or "").splitlines():
        try:
            sz, name = line.split("\t", 1)
        except ValueError:
            continue
        if name.lower().endswith(_VID_EXT):
            out.append({"file": name, "bytes": int(sz) if sz.isdigit() else 0})
    return sorted(out, key=lambda v: v["file"])


def _resolve_remote_video(slot, session, fname):
    ip, rdir = _remote_session_dir(slot, session)
    if not ip:
        return None, None
    if fname:
        if not _SAFE_SEG.match(fname) or not fname.lower().endswith(_VID_EXT):
            return None, None
        return ip, rdir + "/" + fname
    vids = session_videos(slot, session)
    first = next((v for v in vids if v["bytes"] > 0), None)
    return (ip, rdir + "/" + first["file"]) if first else (None, None)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, path, ctype):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return self._json({"error": f"missing file: {path}"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file_range(self, path, ctype):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            start = int(s) if s.isdigit() else 0
            end = int(e) if e.isdigit() else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _preview(self, want):
        q = parse_qs(urlparse(self.path).query)
        ip, rfile = _resolve_remote_video(q.get("slot", [None])[0],
                                          q.get("session", [None])[0],
                                          q.get("file", [None])[0])
        if not ip:
            return self._json({"error": "no video"}, 404)
        if want == "thumb":
            t = opmlan.remote_thumb(CFG, ip, rfile)
            return self._serve_file_range(str(t), "image/jpeg") if t \
                else self._json({"error": "no preview"}, 404)
        clip = opmlan.remote_preview(CFG, ip, rfile)
        return self._serve_file_range(str(clip), "video/mp4") if clip \
            else self._json({"error": "transcode failed"}, 500)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                  # noqa: BLE001
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/detect":
            return self._json(api_detect())
        if path == "/api/config":
            return self._json(api_config_get())
        if path == "/api/status":
            return self._json(JOB.snapshot())
        if path == "/api/analysis":
            return self._json(dict(ANALYSIS_STATUS))
        if path == "/api/update":
            return self._json({"available": False})       # opm-lan has no git self-update
        if path == "/api/thumb":
            return self._preview("thumb")
        if path == "/api/video":
            return self._preview("video")
        if path == "/api/videos":
            q = parse_qs(urlparse(self.path).query)
            return self._json({"videos": session_videos(q.get("slot", [None])[0],
                                                         q.get("session", [None])[0])})
        if path == "/api/listdir":
            q = parse_qs(urlparse(self.path).query)
            return self._json(api_listdir(q.get("path", [None])[0]))
        if path == "/api/history":
            q = parse_qs(urlparse(self.path).query)
            g = lambda k: q.get(k, [""])[0]
            return self._json(api_history(g("device"), g("from"), g("to"), g("q")))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            return self._json(api_config_set(self._body()))
        if path == "/api/precheck":
            return self._json(api_precheck(self._body()))
        if path == "/api/run":
            return self._json(api_run(self._body()))
        if path == "/api/cancel":
            return self._json(api_cancel())
        if path == "/api/add":
            return self._json(api_add())
        if path == "/api/eject":
            return self._json(api_eject(self._body()))
        if path == "/api/open":
            return self._json(api_open(self._body()))
        if path == "/api/vnc":
            return self._json(api_vnc(self._body()))
        if path in ("/api/update/check", "/api/update/apply"):
            return self._json({"ok": False, "available": False,
                               "message": "opm-lan has no self-update"}, 200)
        return self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# API implementations
# --------------------------------------------------------------------------- #
def api_detect():
    cards = recorders()
    return {"cards": cards, "count": len(cards),
            "eligible": sum(1 for c in cards if c.get("eligible")),
            "is_root": True, "iface": IFACE}


def api_config_get():
    placeholder = (not CFG["copy_paths"]
                   or any(opmlan.PLACEHOLDER in p for p in CFG["copy_paths"]))
    return {
        "destination": CFG["destination"],
        "copy_paths": CFG["copy_paths"],
        "exclude": CFG.get("exclude", []),
        "delete_source": CFG["after_copy"].get("delete_source", False),
        "verify": CFG["after_copy"].get("verify", True),
        "require_label": "identified recorder (device_info.json)"
                         if (CFG.get("safety") or {}).get("require_identified", True) else None,
        "identify_mode": CFG["identify"].get("mode"),
        "parallel": CFG["copy"].get("parallel", True),
        "is_root": True, "placeholder": placeholder,
    }


def api_config_set(body):
    if "copy_paths" in body:
        CFG["copy_paths"] = [str(p).strip() for p in body["copy_paths"] if str(p).strip()]
    if body.get("destination"):
        CFG["destination"] = os.path.expanduser(str(body["destination"]).strip())
    if "delete_source" in body:
        CFG["after_copy"]["delete_source"] = bool(body["delete_source"])
    opmlan.save_config(CFG)
    return api_config_get()


def list_mounts():
    out, seen = [], set()
    try:
        data = json.loads(subprocess.run(
            ["lsblk", "-J", "-e", "7", "-o", "NAME,MOUNTPOINT,LABEL,TRAN,FSTYPE"],
            text=True, capture_output=True).stdout)
    except Exception:                                  # noqa: BLE001
        return out

    def walk(nodes, ptran=""):
        for n in nodes:
            tran = n.get("tran") or ptran
            mp = n.get("mountpoint")
            if (mp and mp.startswith("/") and n.get("fstype")
                    and mp not in seen and not mp.startswith(("/boot", "/snap"))):
                seen.add(mp)
                free, total = opmlan.disk_free(mp)
                out.append({"path": mp, "label": n.get("label") or n.get("name"),
                            "tran": tran, "free": free, "total": total})
            walk(n.get("children") or [], tran)
    walk(data.get("blockdevices", []))
    return out


def api_listdir(path):
    home = os.path.expanduser("~")
    if not os.path.isdir(home):
        home = "/home"
    path = os.path.realpath(path or home)
    if not os.path.isdir(path):
        path = home if os.path.isdir(home) else "/"
    dirs, err = [], None
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if not name.startswith(".") and os.path.isdir(full):
                dirs.append({"name": name, "path": full})
    except OSError as e:
        err = str(e)
    free, total = opmlan.disk_free(path)
    return {"path": path, "parent": (os.path.dirname(path) if path != "/" else None),
            "dirs": dirs, "free": free, "total": total, "home": home, "error": err,
            "writable": os.access(path, os.W_OK), "mounts": list_mounts()}


def api_precheck(body):
    """Per-recorder data size + free space on the pi and on the destination, plus a
    fits verdict + the session breakdown (which runs are already copied). Same shape
    opm's precheck returns — sourced over SSH."""
    slots = {int(s) for s in (body.get("slots") or []) if str(s).strip().lstrip("-").isdigit()}
    cards = [c for c in recorders()
             if c.get("eligible") and (not slots or c["slot"] in slots)]
    dest_free, dest_total = opmlan.disk_free(CFG["destination"])
    flatten = CFG["copy"].get("flatten", True)
    per, need = [], 0
    for c in cards:
        ip = c["ip"]
        inv = opmlan.remote_inventory(CFG, ip)
        info = opmlan.identify(CFG, ip)
        device = info.get("name") or ""
        cid = opmlan.card_id_for(info, c["slot"], CFG)
        dest_card = os.path.join(CFG["destination"], cid)
        if not inv:
            per.append({"slot": c["slot"], "card_id": cid, "device": device,
                        "label": c.get("label"), "missing_warn": [], "disk": ip,
                        "mounted_at": ip, "dest_card": dest_card, "bytes": 0,
                        "new_bytes": 0, "files": 0, "present_count": 0, "new_count": 0,
                        "sessions": [], "src_free": 0, "src_total": 0,
                        "healthy": False, "health_msg": "recorder unreachable over SSH"})
            continue
        missing_warn = [p for p in CFG.get("warn_if_missing", [])
                        if not ((inv.get("paths") or {}).get(p) or {}).get("exists")]
        cb = cf = newb = present = 0
        sessions, src_free, src_total = [], 0, 0
        # eMMC-booted recorders redirect actual recording storage to an inserted
        # SD card at /media/sdcard/stereo (see orangepi-recorder's
        # storage_ready()) — its free/total should win over copy_paths[0]'s
        # stats (the eMMC itself, now just an unused/empty local dir there). On
        # a normal SD-booted board this path never exists, so it's a no-op and
        # the loop below falls straight through to the prior first-found logic.
        sdcard_info = (inv.get("paths") or {}).get("/media/sdcard/stereo") or {}
        for p in CFG["copy_paths"]:
            pinfo = (inv.get("paths") or {}).get(p) or {}
            if not pinfo.get("exists"):
                continue
            cb += pinfo.get("bytes", 0)
            cf += pinfo.get("files", 0)
            if not sdcard_info.get("exists"):
                src_free = src_free or pinfo.get("free", 0)
                src_total = src_total or pinfo.get("total", 0)
            runs = [s["name"] for s in pinfo.get("sessions", [])]
            namemap = opmlan.dest_session_names(runs, device)
            for s in pinfo.get("sessions", []):
                s = dict(s)
                s["path"] = p
                dsp = os.path.join(opmlan.dest_path_for(dest_card, p, flatten),
                                   namemap.get(s["name"], s["name"]))
                ds = opmlan.dir_stats(dsp) if os.path.isdir(dsp) else (0, 0, 0)
                s["present"] = (s["bytes"] > 0 and ds[0] >= s["bytes"] and ds[1] >= s["files"])
                if s["present"]:
                    present += 1
                else:
                    newb += s["bytes"]
                sessions.append(s)
        if sdcard_info.get("exists"):
            src_free, src_total = sdcard_info.get("free", 0), sdcard_info.get("total", 0)
        need += newb
        per.append({"slot": c["slot"], "card_id": cid, "device": device,
                    "label": c.get("label"), "missing_warn": missing_warn, "disk": ip,
                    "mounted_at": ip, "dest_card": dest_card, "bytes": cb,
                    "new_bytes": newb, "files": cf, "present_count": present,
                    "new_count": len(sessions) - present, "sessions": sessions,
                    "src_free": src_free, "src_total": src_total,
                    "healthy": True, "health_msg": None})
    dest_fs = opmlan._fs_type(CFG["destination"])
    fat32 = dest_fs.lower() in {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}
    oversize = []
    if fat32:                                          # FAT32 can't store a file >= 4 GiB
        healthy_slots = {x["slot"] for x in per if x.get("healthy")}
        for c in cards:
            if c["slot"] not in healthy_slots:         # don't SSH-find unreachable recorders
                continue
            for p in CFG["copy_paths"]:
                r = opmlan._ssh(CFG, c["ip"], f"find {opmlan._q(p)} -type f -size +4294967295c "
                                f"-printf '%s\\t%p\\n' 2>/dev/null", timeout=30)
                for line in (r.stdout or "").splitlines():
                    try:
                        sz, fp = line.split("\t", 1)
                        oversize.append({"slot": c["slot"], "file": os.path.basename(fp),
                                         "bytes": int(sz)})
                    except ValueError:
                        pass
    unhealthy = [{"slot": c["slot"], "card_id": c["card_id"], "msg": c["health_msg"]}
                 for c in per if not c.get("healthy", True)]
    return {"destination": CFG["destination"],
            "dest_free": dest_free, "dest_total": dest_total, "dest_fs": dest_fs,
            "dest_is_fat32": fat32, "fat_oversize": oversize,
            "total_new": sum(c["new_count"] for c in per),
            "total_present": sum(c["present_count"] for c in per),
            "need": need, "fits": need <= dest_free, "after_free": dest_free - need,
            "tight": need <= dest_free and (dest_free - need) < 5 * (1024 ** 3),
            "unhealthy": unhealthy, "all_healthy": not unhealthy,
            "cards": per, "is_root": True,
            "placeholder": (not CFG["copy_paths"]
                            or any(opmlan.PLACEHOLDER in p for p in CFG["copy_paths"]))}


def api_run(body):
    with JOB.lock:
        if JOB.running or JOB.starting:
            return {"error": "A job is already running."}
    if not CFG["copy_paths"] or any(opmlan.PLACEHOLDER in p for p in CFG["copy_paths"]):
        return {"error": f"Set real copy_paths first (still contains {opmlan.PLACEHOLDER})."}
    cards = recorders()
    if not any(c.get("eligible") for c in cards):
        return {"error": "No recorders detected on the cable."}
    recheck = bool(body.get("recheck"))
    no_delete = True if recheck else bool(body.get("no_delete"))
    slots = body.get("slots") or None
    if slots:
        slots = {int(s) for s in slots if str(s).strip().lstrip("-").isdigit()}
    if not body.get("force"):
        pre = api_precheck({"slots": list(slots) if slots else []})
        if not pre["fits"]:
            return {"error": f"Not enough space: need {opmlan._fmt_size(pre['need'])}, only "
                             f"{opmlan._fmt_size(pre['dest_free'])} free at {CFG['destination']}. "
                             f"Free space or change the destination.", "precheck": pre}
    with JOB.lock:
        if JOB.running or JOB.starting:
            return {"error": "A job is already running."}
        JOB.starting = True
    threading.Thread(target=run_job, args=(no_delete, slots, recheck), daemon=True).start()
    return {"started": True, "no_delete": no_delete, "recheck": recheck}


def api_add():
    """Add newly-connected eligible recorders to the RUNNING session's queue without
    cancelling. In-progress copies are untouched; freed worker slots pick up the added
    recorders, which follow the running job's mode (delete/recheck)."""
    with JOB.lock:
        if not JOB.running:
            return {"error": "No copy session is running — use Start."}
        have = set(JOB.cards.keys())
    new = [c for c in recorders() if c.get("eligible") and c["slot"] not in have]
    if not new:
        return {"added": 0, "error": "No new connected recorders to add."}
    added = []
    for c in new:
        try:
            entry = build_card(CFG, c)
        except Exception as e:                            # noqa: BLE001
            with JOB.lock:
                JOB.append_log(f"[add] slot {c['slot']}: inventory failed: {e}")
            continue
        with JOB.lock:
            if not JOB.running:
                return {"added": len(added), "error": "Session ended before adding."}
            if c["slot"] in JOB.cards:                    # raced with another add
                continue
            JOB.cards[c["slot"]] = entry
        with JOB.add_lock:
            JOB.pending_adds.append(c)
        added.append(entry.get("device") or f"slot{c['slot']}")
    with JOB.lock:
        if added:
            JOB.append_log(f"Added {len(added)} recorder(s) to the session: {', '.join(added)}")
    return {"added": len(added), "devices": added}


def api_cancel():
    with JOB.lock:
        if not JOB.running:
            return {"error": "no job is running"}
        JOB.cancel.set()
        JOB.append_log("Cancellation requested — stopping safely…")
    with JOB.procs_lock:
        procs = list(JOB.procs)
    for p in procs:
        try:
            p.terminate()
        except Exception:                              # noqa: BLE001
            pass
    return {"ok": True, "message": "cancelling — stopping after the current file"}


def api_eject(body):
    """No unmount over LAN — nothing is mounted. This just acknowledges the recorder
    is done and safe to unplug (the SD-card 'Safe remove' analog)."""
    slot = body.get("slot")
    if slot is None or not str(slot).strip().lstrip("-").isdigit():
        return {"error": "invalid slot"}
    slot = int(slot)
    card = recorder_by_slot(slot)
    name = (card and (card.get("device") or card.get("label") or card["ip"])) or f"slot {slot}"
    return {"ok": True, "slot": slot, "unmounted": [], "lazy": [],
            "message": f"{name} — copy done, safe to disconnect (nothing is mounted over LAN)."}


def api_open(body):
    """Open a recorder's files in the desktop file manager over SFTP. Auto-clears any
    stale host key for the IP first (a reused DHCP address otherwise throws the
    'REMOTE HOST IDENTIFICATION HAS CHANGED' error that needs a manual ssh-keygen -R).
    Uses sshfs when available (no host-key check at all + password fed in → zero
    prompts); else falls back to the native gvfs sftp:// URL (one password prompt)."""
    ip = body.get("ip")
    card = next((c for c in recorders() if c.get("ip") == ip), None)
    if not ip or not card:
        return {"ok": False, "msg": "recorder not found / not online"}
    name = card.get("device") or card.get("label") or ip
    user = CFG["ssh"]["user"]
    home = f"/home/{user}"
    # the fix for the user's pain: drop the stale key so a reused IP never errors
    subprocess.run(["ssh-keygen", "-R", ip], capture_output=True)
    if shutil.which("sshfs"):
        mnt = os.path.expanduser(f"~/opm-lan-mounts/{opmlan._sanitize(name)}")
        os.makedirs(mnt, exist_ok=True)
        if not os.path.ismount(mnt):
            cmd = ["sshfs", f"{user}@{ip}:{home}", mnt,
                   "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "password_stdin", "-o", "reconnect", "-o", "ServerAliveInterval=5"]
            r = subprocess.run(cmd, input=str(CFG["ssh"]["password"]) + "\n",
                               text=True, capture_output=True, timeout=30)
            if not os.path.ismount(mnt):
                return {"ok": False, "msg": f"mount failed: {(r.stderr or '').strip()[:140]}"}
        subprocess.Popen(["xdg-open", mnt])
        return {"ok": True, "msg": f"opened {name} in Files ({mnt})", "method": "sshfs"}
    # no sshfs: native file-manager sftp (stale key already cleared above)
    subprocess.Popen(["xdg-open", f"sftp://{user}@{ip}{home}/"])
    return {"ok": True, "msg": f"opening {name} in Files — accept the key / enter the "
            f"password ({CFG['ssh']['password']}) once", "method": "gvfs"}


def api_vnc(body):
    """Open a recorder's live desktop over VNC, via tools/vnc_into_pi.sh, running in
    the background so this request returns immediately. That script installs/starts
    x11vnc on the pi (working around this fleet's GPU driver needing a DRM-forced
    HDMI connector to render at all), opens TigerVNC here, and — once that window is
    closed — cleans up everything it changed on the pi (kills x11vnc, un-forces the
    HDMI connector) so it never affects a later launch or the pi's normal behavior.
    One instance per IP: a second click while one's still open is a no-op (the
    script itself guards this with a lock file)."""
    ip = body.get("ip")
    card = next((c for c in recorders() if c.get("ip") == ip), None)
    if not ip or not card:
        return {"ok": False, "msg": "recorder not found / not online"}
    script = Path(__file__).resolve().parent.parent / "tools" / "vnc_into_pi.sh"
    if not script.exists():
        return {"ok": False, "msg": f"missing {script}"}
    log_path = f"/tmp/vnc_into_pi_{ip.replace('.', '_')}.log"
    with open(log_path, "ab") as logf:
        subprocess.Popen(["bash", str(script), ip], stdout=logf, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "msg": f"opening VNC to {ip} — a TigerVNC window should appear "
            f"shortly (log: {log_path})"}


def main():
    if not (WEB_DIR / "index.html").exists():
        sys.exit(f"missing web asset: {WEB_DIR / 'index.html'}")
    os.makedirs(CFG["destination"], exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=_analysis_loop, daemon=True).start()
    print(f"opm-lan web UI -> http://{HOST}:{PORT}   (pulling to {CFG['destination']})")
    print("Plug recorders into the shared cable/switch; they appear automatically. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
