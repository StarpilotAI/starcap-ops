#!/usr/bin/env python3
"""
opm-lan — Orange Pi LAN copier
==============================
Same job as opm, but the source is a recorder on the WIRED LAN instead of an SD
card in a USB reader. Per-recorder pipeline:

    discover (scan the cable) -> identify (SSH: device_info.json) -> copy (rsync
    over SSH) -> verify (checksum) -> [delete source from the pi] -> done

This file is the ENGINE + a tiny CLI, mirroring opm.py's shape so webui.py can
drive it exactly the way opm's webui drives opm.py. opm itself is untouched.

Why it's more than `scp -r`:
  * Recorder identity comes from device_info.json `name` (e.g. starcap25), so a
    recorder lands in its own stable folder and runs are renamed
    <device>_<MMDD-HHMM> — byte-for-byte the layout opm produces (so the
    dashboard keys the footage correctly).
  * The same source->dest mapping is shared by copy / verify / delete, so all
    three always agree (change it once, they all change).
  * Deletion is gated: never delete from a recorder we haven't POSITIVELY
    identified, and never until every file is checksum-confirmed on this PC.

Strictly LAN: discovery + transfer go over the wired interface; wifi is unused.
Stdlib + PyYAML (optional) + external `sshpass`, `ssh`, `rsync`, `ffmpeg`.
"""
import errno
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:                                  # degrade gracefully — UI can still set paths
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
PLACEHOLDER = "/REPLACE_ME"
THUMB_DIR = Path(tempfile.gettempdir()) / "opm-lan-thumbs"
_SESSION_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def _defaults():
    return {
        "destination": str(Path.home() / "opm-lan-pulls"),
        "copy_paths": ["/home/orangepi/orangepi-recorder/recordings/stereo"],
        "warn_if_missing": [], "exclude": [],
        "ssh": {"user": "orangepi", "password": "orangepi", "port": 22},
        "lan": {"iface": "", "scan_subnet": ""},
        "identify": {"mode": "device"},
        "after_copy": {"verify": True, "delete_source": False, "prune_empty_dirs": True,
                       # copied but never deleted from the recorder (protects identity/config)
                       "no_delete_paths": ["/home/orangepi/.config/starcap"]},
        "copy": {"parallel": True, "max_workers": 4, "flatten": True, "skip_empty": True},
        "preview": {"seconds": 20, "width": 480, "fps": 15, "crf": 34},
        "safety": {"require_identified": True},
        # opm's project dir — reused (read-only) for contact sheets + dashboard reporting,
        # so opm-lan's Analysis tab behaves exactly like opm's. opm is never modified.
        "opm_dir": str(Path.home() / "starcap-dashboard" / "orangepi-manager"),
        # same dashboard reporting opm uses (env OPM_DASHBOARD_URL / OPM_UPLOAD_PASSWORD override)
        "analysis": {"contact_sheet": True, "estimate": True,
                     "dashboard_url": "https://starcap.dev", "password": "starupload",
                     "thumb_w": 480, "sheet_max_mb": 10, "fps": 4, "width": 224, "eye": "left"},
    }


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path=None):
    cfg = _defaults()
    path = path or str(SCRIPT_DIR / "config.yaml")
    if yaml and os.path.exists(path):
        try:
            with open(path) as f:
                _merge(cfg, yaml.safe_load(f) or {})
        except (OSError, ValueError):
            pass
    env = os.environ.get("OPM_LAN_DESTINATION")
    if env and env.strip():
        cfg["destination"] = env.strip()
    cfg["destination"] = os.path.expanduser(cfg["destination"])
    od = os.environ.get("OPM_LAN_OPM_DIR")               # per-machine override for opm's dir
    if od is not None:
        cfg["opm_dir"] = od.strip()
    return cfg


def save_config(cfg, path=None):
    """Persist destination + copy_paths back to config.yaml (best-effort)."""
    path = path or str(SCRIPT_DIR / "config.yaml")
    if not yaml:
        return False
    try:
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = yaml.safe_load(f) or {}
        dest, home = cfg["destination"], os.path.expanduser("~")
        if dest == home or dest.startswith(home + os.sep):
            dest = "~" + dest[len(home):]                # keep the portable ~ form
        existing["destination"] = dest
        existing["copy_paths"] = cfg["copy_paths"]
        with open(path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)
        return True
    except (OSError, ValueError):
        return False


def _sanitize(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:64]


# Reuse opm's analysis stack (contact sheets + dashboard reporting) on the LOCAL copy,
# so opm-lan's Analysis tab is identical to opm's. opm is imported read-only, never
# modified. Returns the `analysis` module, or None if opm_dir / its deps are unavailable.
_ANALYSIS = {"tried": False, "mod": None, "reason": None}


def load_analysis(cfg):
    import sys
    if _ANALYSIS["tried"]:
        return _ANALYSIS["mod"]
    _ANALYSIS["tried"] = True
    if os.environ.get("OPM_LAN_NO_ANALYSIS"):            # ops/test kill-switch
        _ANALYSIS["reason"] = "disabled via OPM_LAN_NO_ANALYSIS"
        return None
    opm_dir = os.path.expanduser(cfg.get("opm_dir") or "")
    for cand in (opm_dir, str(SCRIPT_DIR.parent.parent / "starcap-dashboard" / "orangepi-manager")):
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    try:
        import analysis  # opm's module (pulls in contactsheet/dataquality/cloud)
        _ANALYSIS["mod"] = analysis
    except Exception as e:                               # noqa: BLE001
        _ANALYSIS["reason"] = str(e)
    return _ANALYSIS["mod"]


# --------------------------------------------------------------------------- #
# SSH / rsync transport
# --------------------------------------------------------------------------- #
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5", "-o", "BatchMode=no"]


def _ssh_argv(cfg, ip):
    s = cfg["ssh"]
    return (["sshpass", "-p", str(s["password"]), "ssh", "-p", str(s.get("port", 22))]
            + SSH_OPTS + [f'{s["user"]}@{ip}'])


def _rsh(cfg):
    """The -e remote-shell string rsync uses to reach the pi (with the password)."""
    s = cfg["ssh"]
    return ("sshpass -p " + str(s["password"]) + " ssh -p " + str(s.get("port", 22))
            + " " + " ".join(SSH_OPTS))


def _ssh(cfg, ip, remote_cmd, timeout=25, stdin=None):
    try:
        return subprocess.run(_ssh_argv(cfg, ip) + [remote_cmd], input=stdin,
                              text=True, capture_output=True, timeout=timeout)
    except Exception as e:                            # noqa: BLE001
        return subprocess.CompletedProcess([], 255, "", str(e))


def _ssh_binary(cfg, ip, remote_cmd, timeout=60):
    """SSH returning raw bytes on stdout (for streaming a thumbnail/clip back)."""
    try:
        return subprocess.run(_ssh_argv(cfg, ip) + [remote_cmd],
                              capture_output=True, timeout=timeout)
    except Exception as e:                            # noqa: BLE001
        return subprocess.CompletedProcess([], 255, b"", str(e).encode())


# --------------------------------------------------------------------------- #
# network: find the wired (Shared-mode) link and scan it
# --------------------------------------------------------------------------- #
def wired_iface_subnet(cfg):
    """(iface, subnet_cidr) for the cable this PC shares to the recorders. Honors a
    forced lan.iface/scan_subnet; else prefers an interface holding a 10.42.0.x
    (NetworkManager 'Shared') address, then any wired interface."""
    forced_if = (cfg.get("lan") or {}).get("iface") or ""
    forced_net = (cfg.get("lan") or {}).get("scan_subnet") or ""
    if forced_if and forced_net:
        return forced_if, forced_net
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:                                 # noqa: BLE001
        return (forced_if or None), (forced_net or None)
    best = None
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        iface, ip, bits = m.group(1), m.group(2), m.group(3)
        if iface == "lo" or iface.startswith(("wl", "docker", "veth", "br-")):
            continue
        if forced_if and iface != forced_if:
            continue
        net = ".".join(ip.split(".")[:3]) + ".0/" + bits
        if ip.startswith("10.42.0."):
            return iface, (forced_net or net)
        best = best or (iface, net)
    if best:
        return best[0], (forced_net or best[1])
    return (forced_if or None), (forced_net or None)


def net_rx_bytes(iface):
    """Cumulative RX bytes on `iface` from /proc/net/dev (the ethernet read-rate
    source — the analog of opm reading the card)."""
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if line.split(":")[0].strip() == iface:
                    return int(line.split(":")[1].split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def discover(cfg):
    """Ping-sweep the wired subnet; return (live_ips, iface)."""
    iface, subnet = wired_iface_subnet(cfg)
    if not subnet:
        return [], iface
    base = subnet.split("/")[0].rsplit(".", 1)[0]
    selfips = set(re.findall(r"\d+\.\d+\.\d+\.\d+",
                  subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True).stdout))
    procs = []
    for i in range(2, 255):
        ip = f"{base}.{i}"
        if ip in selfips:
            continue
        args = ["ping", "-c1", "-W1"] + (["-I", iface] if iface else []) + [ip]
        procs.append((ip, subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
    live, deadline = [], time.time() + 3
    for ip, p in procs:
        try:
            if p.wait(timeout=max(0, deadline - time.time())) == 0:
                live.append(ip)
        except subprocess.TimeoutExpired:
            p.kill()
    return live, iface


# --------------------------------------------------------------------------- #
# identify + inventory (the SSH equivalent of mounting + walking the card)
# --------------------------------------------------------------------------- #
# Runs ON the pi (it has python3 — it's the recorder). Walks copy_paths and emits
# the same per-session shape opm computes locally (name/date/time/types/bytes/files),
# plus device_info.json, hostname, machine-id and the recordings filesystem's free/total.
_REMOTE_INVENTORY = r'''
import json, os, re, sys
paths = sys.argv[1:]
SESS = re.compile(r"(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})")
def stats(path):
    tb = fc = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try: sz = os.lstat(os.path.join(root, f)).st_size
            except OSError: continue
            if sz > 0: tb += sz; fc += 1
    return tb, fc
def read(p):
    try:
        with open(p) as fh: return fh.read().strip()
    except OSError: return ""
out = {"hostname": read("/etc/hostname"), "machine_id": read("/etc/machine-id"),
       "device_info": {}, "paths": {}, "total_bytes": 0, "total_files": 0}
newest_di = None
for p in paths:
    info = {"exists": os.path.isdir(p), "bytes": 0, "files": 0,
            "free": 0, "total": 0, "sessions": [], "others": []}
    if info["exists"]:
        try:
            st = os.statvfs(p); info["free"] = st.f_bavail*st.f_frsize; info["total"] = st.f_blocks*st.f_frsize
        except OSError: pass
        try: names = sorted(os.listdir(p))
        except OSError: names = []
        for name in names:
            full = os.path.join(p, name)
            m = SESS.match(name)
            if os.path.isdir(full) and m:                  # a recording RUN dir (YYYYMMDD-HHMMSS)
                b, fc = stats(full)
                types = {}
                try:
                    for f in os.listdir(full):
                        fp = os.path.join(full, f)
                        try:
                            if not (os.path.isfile(fp) and os.path.getsize(fp) > 0): continue
                        except OSError: continue
                        stem = re.sub(r"(_\d+)?\.[^.]+$", "", f)
                        types[stem] = types.get(stem, 0) + 1
                except OSError: pass
                info["bytes"] += b; info["files"] += fc
                info["sessions"].append({"name": name,
                    "date": "%s-%s-%s" % (m.group(1), m.group(2), m.group(3)),
                    "time": "%s:%s:%s" % (m.group(4), m.group(5), m.group(6)),
                    "bytes": b, "files": fc, "types": types})
                di = os.path.join(full, "device_info.json")
                if (newest_di is None or name > newest_di[0]) and os.path.exists(di):
                    try:
                        with open(di) as fh: newest_di = (name, json.load(fh))
                    except (OSError, ValueError): pass
            else:                                          # non-session content (loose file or other dir)
                if os.path.isdir(full):
                    b, fc = stats(full)
                else:
                    try: b = os.lstat(full).st_size; fc = 1 if b > 0 else 0
                    except OSError: b = fc = 0
                if b > 0:
                    info["bytes"] += b; info["files"] += fc
                    info["others"].append(name)
    out["paths"][p] = info
    out["total_bytes"] += info["bytes"]; out["total_files"] += info["files"]
if newest_di: out["device_info"] = newest_di[1]
print(json.dumps(out))
'''


def remote_inventory(cfg, ip, timeout=60):
    """Full per-session inventory of a recorder over ONE SSH call (heavy; for
    precheck/run). Returns the parsed dict or {} on failure."""
    quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in cfg["copy_paths"])
    r = _ssh(cfg, ip, f"python3 - {quoted}", timeout=timeout, stdin=_REMOTE_INVENTORY)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


# light identify cache so the 2s detect polling doesn't re-walk each pi constantly
_ID_CACHE = {}          # ip -> (timestamp, info)
_ID_TTL = 12.0


def identify(cfg, ip, force=False):
    """Cheap-ish recorder identity for the detect table: device_info.json `name`,
    device_id, run count, total bytes (du), sw version. Cached briefly."""
    now = time.time()
    if not force:
        c = _ID_CACHE.get(ip)
        if c and now - c[0] < _ID_TTL:
            return c[1]
    rec0 = _q(cfg["copy_paths"][0] if cfg["copy_paths"]
              else "orangepi-recorder/recordings/stereo")           # single-quoted, injection-safe
    # eMMC-booted recorders redirect actual recording storage to an inserted SD
    # card at /media/sdcard/stereo (see orangepi-recorder's storage_ready()) —
    # copy_paths[0] is then a stale, empty local dir. Prefer the SD-card path
    # when it's actually there; on a normal SD-booted board it never exists, so
    # this is a no-op there and falls straight through to the old behavior.
    sdcard = _q("/media/sdcard/stereo")
    script = (
        f'echo "HOST=$(hostname)"; '
        f'echo "MID=$(cat /etc/machine-id 2>/dev/null)"; '
        # STABLE identity (survives deleting all recordings) — the recorder persists these
        # next to its config, independent of the per-run device_info.json.
        f'echo "NAME=$(cat ~/.config/starcap/device_name 2>/dev/null)"; '
        f'echo "DEVID=$(cat ~/.config/starcap/device_id 2>/dev/null)"; '
        f'echo "VER=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null | head -1)"; '
        f'if [ -d {sdcard} ]; then rec={sdcard}; else rec={rec0}; fi; '
        f'echo "RUNS=$(ls "$rec" 2>/dev/null | wc -l)"; '
        # bound the size probe so a huge recordings tree can't blow the SSH timeout and
        # make the recorder flap offline; an unknown size just shows as 0
        f'echo "BYTES=$(timeout 10 du -sb "$rec" 2>/dev/null | cut -f1)"; '
        f'di=$(ls -t "$rec"/*/device_info.json 2>/dev/null | head -1); '
        f'echo DIJSON_START; cat "$di" 2>/dev/null; echo DIJSON_END'
    )
    r = _ssh(cfg, ip, script, timeout=25)
    if r.returncode != 0 or "HOST=" not in r.stdout:
        info = {"ok": False, "ip": ip, "error": (r.stderr or "unreachable").strip()[:120]}
        _ID_CACHE[ip] = (now, info)
        return info
    out = r.stdout
    grab = lambda k: (re.search(rf"^{k}=(.*)$", out, re.M) or [None, ""])[1].strip() \
        if re.search(rf"^{k}=(.*)$", out, re.M) else ""
    di = {}
    mj = re.search(r"DIJSON_START\s*(.*?)\s*DIJSON_END", out, re.S)
    if mj and mj.group(1).strip():
        try:
            di = json.loads(mj.group(1))
        except ValueError:
            di = {}
    to_int = lambda s: int(s) if str(s).isdigit() else 0
    # prefer the recorder's stable config files; fall back to the newest run's
    # device_info.json (older recorders, or before the config files existed)
    name = grab("NAME") or di.get("name") or ""
    devid = grab("DEVID") or di.get("device_id") or None
    ver = grab("VER") or di.get("software_version") or None
    info = {"ok": True, "ip": ip, "name": _sanitize(name) if name else "",
            "device_id": devid, "hostname": di.get("hostname") or grab("HOST"),
            "machine_id": grab("MID"), "cpu": (di.get("hardware") or {}).get("cpu_serial"),
            "sw": ver, "runs": to_int(grab("RUNS")),
            "bytes": to_int(grab("BYTES"))}
    _ID_CACHE[ip] = (now, info)
    return info


def card_id_for(info, slot, cfg):
    """Stable per-recorder destination folder name — mirrors opm.card_id_for:
    device name (device_info.json `name`) > hostname-machineid > hostname > slotN."""
    if cfg["identify"].get("mode") == "slot":
        return f"slot{slot}"
    if info.get("name"):
        return _sanitize(info["name"])
    if info.get("machine_id"):
        return _sanitize(f"{info.get('hostname') or 'opi'}-{info['machine_id'][:8]}")
    if info.get("hostname"):
        return _sanitize(info["hostname"])
    return f"slot{slot}"


_SLOTS = {}          # stable recorder identity -> slot number (never reused, so a
_SLOT_NEXT = [1]     # given physical recorder keeps its slot across scans)


def _slot_for(key):
    if key not in _SLOTS:
        _SLOTS[key] = _SLOT_NEXT[0]
        _SLOT_NEXT[0] += 1
    return _SLOTS[key]


def _stable_key(info):
    """A stable identity for a recorder, falling back through device_id -> machine-id
    -> hostname -> ip, so two DHCP leases still collapse to one card even when
    device_info.json is missing."""
    return (info.get("device_id") or info.get("machine_id")
            or info.get("hostname") or info["ip"])


def detect_recorders(cfg):
    """opm.detect_cards analog: discover pis on the cable, identify each, and return
    'card' dicts in the SAME shape opm's detect produces (slot/disk/fstype/size/label/
    eligible/mounted_at/device/error). One card per physical recorder, with a STABLE
    slot so the UI's checkboxes don't jump when another recorder appears."""
    ips, iface = discover(cfg)
    by_id = {}
    for ip in ips:
        info = identify(cfg, ip)
        if not info.get("ok"):
            continue
        key = _stable_key(info)
        # if a recorder has two leases, keep one card (prefer a 10.42.0.x address)
        if key in by_id and not ip.startswith("10.42.0."):
            continue
        by_id[key] = info
    cards = []
    for key, info in by_id.items():
        cards.append({
            "ip": info["ip"], "disk": info["ip"],          # 'disk' column shows the IP
            "fstype": (f"v{info['sw']}" if info.get("sw") else "lan"),
            "size": info.get("bytes") or 0,
            "label": info.get("device_id") or "",          # 'label' column shows device_id
            "device": info.get("name") or "",
            "device_id": info.get("device_id"), "hostname": info.get("hostname"),
            "machine_id": info.get("machine_id"), "cpu": info.get("cpu"),
            "sw": info.get("sw"), "runs": info.get("runs") or 0,
            "mounted_at": info["ip"],                       # truthy = reachable
            "eligible": True, "error": None, "slot": _slot_for(key),
        })
    cards.sort(key=lambda c: c["slot"])
    return cards, iface


# --------------------------------------------------------------------------- #
# source -> dest name mapping (shared by copy / verify / delete) — from opm
# --------------------------------------------------------------------------- #
def _leaf(p):
    return os.path.basename(p.rstrip("/")) or p.strip("/")


def dest_path_for(dest, p, flatten):
    if flatten:
        return os.path.join(str(dest), _leaf(p))
    return os.path.join(str(dest), p.lstrip("/"))


def session_dest_name(session, device, seconds=False):
    if not device:
        return session
    m = _SESSION_RE.match(session)
    if not m:
        return f"{device}_{session}"
    tail = f"{m.group(4)}{m.group(5)}" + (m.group(6) if seconds else "")
    return f"{device}_{m.group(2)}{m.group(3)}-{tail}"


def dest_session_names(session_names, device):
    """{source_run: dest_folder} for a list of run names (those matching YYYYMMDD-HHMMSS).
    Identity without a device; <device>_<MMDD-HHMM> with one, keeping seconds when two
    runs share a minute so they never merge. Same algorithm as opm.dest_session_names."""
    names = [n for n in sorted(session_names) if _SESSION_RE.match(n)]
    if not device:
        return {n: n for n in names}
    groups = {}
    for n in names:
        groups.setdefault(session_dest_name(n, device), []).append(n)
    out = {}
    for _short, group in groups.items():
        secs = len(group) > 1
        for n in group:
            out[n] = session_dest_name(n, device, seconds=secs)
    return out


# --------------------------------------------------------------------------- #
# rsync flags by destination filesystem (from opm — local dest)
# --------------------------------------------------------------------------- #
_NONUNIX_FS = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos", "exfat",
               "exfat-fuse", "ntfs", "ntfs3", "ntfs-3g", "fuseblk", "hfs",
               "hfsplus", "udf", "iso9660"}


def _fs_type(path):
    p = os.path.abspath(str(path))
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    return subprocess.run(["findmnt", "-nro", "FSTYPE", "--target", p],
                          text=True, capture_output=True).stdout.strip()


def archive_flags(dest):
    if (_fs_type(dest) or "").strip().lower() in _NONUNIX_FS:
        return ["-rt", "--no-links", "--modify-window=2"]
    return ["-aHAX", "--numeric-ids"]


def _excludes(cfg):
    out = []
    for e in cfg.get("exclude", []):
        out += ["--exclude", e]
    return out


def _size_filter(cfg):
    return ["--min-size=1"] if cfg.get("copy", {}).get("skip_empty", True) else []


# --------------------------------------------------------------------------- #
# local-dest stats (identical to opm — the dest is local here too)
# --------------------------------------------------------------------------- #
def dir_stats(path):
    tb = fc = dc = 0
    if not os.path.exists(path):
        return (0, 0, 0)
    for root, dirs, files in os.walk(path):
        dc += len(dirs)
        for f in files:
            try:
                sz = os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue
            if sz > 0:
                tb += sz
                fc += 1
    return (tb, fc, dc)


def disk_free(path):
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    try:
        st = os.statvfs(p)
    except OSError:
        return (0, 0)
    return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)


def _fmt_size(n):
    if not n:
        return "?"
    f = float(n)
    for u in ["B", "K", "M", "G", "T"]:
        if f < 1024 or u == "T":
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024


# --------------------------------------------------------------------------- #
# copy / verify / delete (rsync OVER SSH; same job mapping for all three)
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


def _run_rsync(cmd, stop=None, procs=None, check=True):
    """Run an rsync (possibly cancellable). rsync is safe to interrupt: finished
    files stay intact, in-flight ones are temp files never renamed into place."""
    if stop is None:
        r = subprocess.run(cmd, text=True, capture_output=True)
        if check and r.returncode not in (0, 24):     # 24 = source vanished (benign)
            raise RuntimeError(f"rsync rc={r.returncode}: {(r.stderr or '').strip()}")
        return r
    p = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if procs is not None:
        with procs[1]:
            procs[0].add(p)
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if stop():
                    p.terminate()
                    try:
                        out, err = p.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill(); out, err = p.communicate()
                    raise Cancelled()
    finally:
        if procs is not None:
            with procs[1]:
                procs[0].discard(p)
    r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
    if check and r.returncode not in (0, 24):
        raise RuntimeError(f"rsync rc={r.returncode}: {(err or '').strip()}")
    return r


def _jobs(cfg, ip, dest, p, device, run_names, others=None):
    """(remote_src, local_dst, extra_flags) jobs for copy_path `p`, shared IDENTICALLY
    by copy / verify / delete. Mirrors opm._path_jobs:
      * no device  -> one whole-leaf job (copies everything verbatim)
      * device     -> one job per recording RUN (renamed via dest_session_names) PLUS,
                      if the leaf holds non-run content, one verbatim job for the rest
                      (runs excluded) so loose/config files are copied under their own
                      names, never duplicated.
    Returns (jobs, namemap)."""
    user = cfg["ssh"]["user"]
    base = f"{user}@{ip}:{p.rstrip('/')}/"
    leafdest = dest_path_for(dest, p, cfg["copy"].get("flatten", True))
    if not device:
        return [(base, leafdest + "/", [])], {n: n for n in run_names}
    namemap = dest_session_names(run_names, device)
    jobs = [(f"{user}@{ip}:{p.rstrip('/')}/{sess}/", os.path.join(leafdest, destname) + "/", [])
            for sess, destname in sorted(namemap.items())]
    if others:                                          # loose/config content -> verbatim
        excl = [f"--exclude=/{sess}" for sess in namemap]
        jobs.append((base, leafdest + "/", excl))
    return jobs, namemap


def do_copy(cfg, ip, dest, inv, device, stop=None, procs=None):
    """rsync each recording run from the pi -> dest/<leaf>/<renamed-run>/."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    af = archive_flags(dest)
    rsh = _rsh(cfg)
    copied, missing = [], []
    for p in cfg["copy_paths"]:
        info = (inv.get("paths") or {}).get(p) or {}
        if not info.get("exists"):
            missing.append(p)
            continue
        runs = [s["name"] for s in info.get("sessions", [])]
        jobs, _ = _jobs(cfg, ip, dest, p, device, runs, info.get("others"))
        for src, dst, rflags in jobs:
            Path(dst).mkdir(parents=True, exist_ok=True)
            cmd = (["rsync"] + af + rflags + ["--info=stats1", "-e", rsh]
                   + _size_filter(cfg) + _excludes(cfg) + [src, dst])
            _run_rsync(cmd, stop=stop, procs=procs)
        copied.append(p)
    return copied, missing


def verify(cfg, ip, dest, inv, paths, device, stop=None, procs=None):
    """Checksum dry-run over SSH; returns files that DON'T match (empty == verified)."""
    dest = Path(dest)
    af = archive_flags(dest)
    rsh = _rsh(cfg)
    failed = []
    for p in paths:
        info = (inv.get("paths") or {}).get(p) or {}
        runs = [s["name"] for s in info.get("sessions", [])]
        jobs, _ = _jobs(cfg, ip, dest, p, device, runs, info.get("others"))
        for src, dst, rflags in jobs:
            cmd = (["rsync"] + af + ["-c", "-n", "--out-format=%i %n", "-e", rsh]
                   + rflags + _size_filter(cfg) + _excludes(cfg) + [src, dst])
            # check=True (default): a dry-run that ERRORS (SSH drop, rc!=0/24) must RAISE,
            # not return empty stdout that reads as "nothing mismatched" → a delete-gate bypass.
            for line in _run_rsync(cmd, stop=stop, procs=procs).stdout.splitlines():
                if line.split(" ", 1)[0].startswith((">f", "<f", "cf")):
                    failed.append(line)
    return failed


def missing_on_dest(cfg, ip, dest, inv, paths, device, stop=None, procs=None):
    """Files not yet present (size+mtime) at the destination — fast pre-delete guard."""
    dest = Path(dest)
    af = archive_flags(dest)
    rsh = _rsh(cfg)
    miss = []
    for p in paths:
        info = (inv.get("paths") or {}).get(p) or {}
        runs = [s["name"] for s in info.get("sessions", [])]
        jobs, _ = _jobs(cfg, ip, dest, p, device, runs, info.get("others"))
        for src, dst, rflags in jobs:
            cmd = (["rsync"] + af + ["-n", "--out-format=%i %n", "-e", rsh]
                   + rflags + _size_filter(cfg) + _excludes(cfg) + [src, dst])
            # check=True (default): an errored pre-delete guard must RAISE (source kept),
            # never silently return "" that reads as "everything is confirmed on dest".
            for line in _run_rsync(cmd, stop=stop, procs=procs).stdout.splitlines():
                parts = line.split(" ", 1)
                if parts[0].startswith((">f", "<f", "cf")):
                    miss.append(parts[1] if len(parts) > 1 else parts[0])
    return miss


def delete_source(cfg, ip, dest, inv, paths, device, info, stop=None, procs=None):
    """Remove copied runs from the PI via rsync --remove-source-files, then prune the
    emptied run dirs. HARD GATE: only from a positively-identified recorder (readable
    device_info.json / device name) when safety.require_identified is set — the
    ethernet analog of opm's require_label gate. Never deletes until every file is
    confirmed on this PC."""
    require = (cfg.get("safety") or {}).get("require_identified", True)
    if require and not (info.get("device_id") or device):
        return {"deleted": False, "error": "delete refused — recorder not positively "
                "identified (no readable device_info.json); source kept."}
    miss = missing_on_dest(cfg, ip, dest, inv, paths, device, stop=stop, procs=procs)
    if miss:
        return {"deleted": False, "error": f"delete aborted — {len(miss)} file(s) not "
                f"confirmed on destination (e.g. {miss[0]}); source kept."}
    af = archive_flags(dest)
    rsh = _rsh(cfg)
    csum = [] if cfg["after_copy"].get("verify", True) else ["-c"]
    for p in paths:
        pinfo = (inv.get("paths") or {}).get(p) or {}
        runs = [s["name"] for s in pinfo.get("sessions", [])]
        jobs, namemap = _jobs(cfg, ip, dest, p, device, runs, pinfo.get("others"))
        for src, dst, rflags in jobs:
            cmd = (["rsync"] + af + csum + ["--remove-source-files", "-e", rsh]
                   + rflags + _size_filter(cfg) + _excludes(cfg) + [src, dst])
            _run_rsync(cmd, stop=stop, procs=procs)
        if cfg["after_copy"].get("prune_empty_dirs", True) and runs:
            # remove the now-empty run dirs on the pi (only the ones we copied)
            rmlist = " ".join("'" + (p.rstrip('/') + '/' + r).replace("'", "'\\''") + "'"
                              for r in namemap)
            _ssh(cfg, ip, f"for d in {rmlist}; do find \"$d\" -mindepth 1 -type d -empty -delete 2>/dev/null; "
                          f"rmdir \"$d\" 2>/dev/null; done", timeout=30)
    _ssh(cfg, ip, "sync", timeout=10)
    return {"deleted": True, "error": None}


# --------------------------------------------------------------------------- #
# per-recorder driver (mirror of opm.process_card)
# --------------------------------------------------------------------------- #
def process_recorder(card, cfg, ts, do_delete, assigned, lock, on_stage=None,
                     stop=None, procs=None, verify_copy=None, recheck=False):
    slot = card["slot"]
    ip = card["ip"]
    vmode = cfg["after_copy"].get("verify", True)
    if verify_copy is None:
        verify_copy = bool(vmode)
    checksum_verify = verify_copy and vmode in (True, "checksum")

    def stage(s, sinfo=None):
        if on_stage:
            on_stage(slot, s, sinfo)

    res = {"slot": slot, "card_id": f"slot{slot}", "device": "", "status": "ok",
           "copied": [], "missing": [], "deleted": False, "error": None, "dest": None}
    if stop and stop():
        res.update(status="cancelled")
        stage("done")
        return res
    try:
        stage("mounting")                                # = "connecting / SSH inventory"
        inv = remote_inventory(cfg, ip)
        if not inv:
            res.update(status="error", error="could not read the recorder over SSH")
            stage("done")
            return res
        info = identify(cfg, ip, force=True)
        device = info.get("name") or ""
        res["device"] = device
        cid = card_id_for(info, slot, cfg)
        with lock:
            if cid in assigned:
                cid = f"{cid}-{slot}"
            assigned.add(cid)
        res["card_id"] = cid
        dest = Path(cfg["destination"]) / cid
        res["dest"] = str(dest)
        stage("copying", {"dest": str(dest), "src": ip, "device": device})
        res["copied"], res["missing"] = do_copy(cfg, ip, dest, inv, device, stop=stop, procs=procs)

        if res["copied"]:
            # ---- RECORDER-DEPENDENT steps FIRST, so the pi is freed ASAP ----
            # verify re-reads the pi and delete removes from it; report + contact sheets
            # read only the LOCAL copy, so they run AFTER — the recorder can be unplugged
            # the moment the delete finishes.
            verify_ok = True
            if recheck:
                # "re-copy & verify": names+sizes presence check; never deletes.
                stage("verifying")
                miss = missing_on_dest(cfg, ip, dest, inv, res["copied"], device, stop=stop, procs=procs)
                if miss:
                    res.update(status="verify-failed",
                               error=f"{len(miss)} file(s) missing/size-mismatched on the "
                                     f"destination (e.g. {miss[0]}); source kept")
            else:
                if checksum_verify:
                    stage("verifying")
                    fails = verify(cfg, ip, dest, inv, res["copied"], device, stop=stop, procs=procs)
                    if fails:
                        res.update(status="verify-failed",
                                   error=f"{len(fails)} file(s) did not match; source kept")
                        verify_ok = False
                # SAFETY: delete only when verify passed — never remove from the pi otherwise
                if do_delete and verify_ok:
                    # never delete protected paths (e.g. ~/.config/starcap = recorder identity)
                    nd = {x.rstrip("/") for x in cfg["after_copy"].get("no_delete_paths", [])}
                    del_paths = [p for p in res["copied"] if p.rstrip("/") not in nd]
                    if del_paths:
                        stage("deleting")
                        d = delete_source(cfg, ip, dest, inv, del_paths, device, info, stop=stop, procs=procs)
                        res["deleted"] = d["deleted"]
                        if d["error"]:
                            res.update(status="delete-skipped", error=d["error"])

            # ---- pi no longer needed: report + contact sheets from the LOCAL copy ----
            # (safe to unplug the recorder now). Reuses opm's analysis; best-effort so a
            # missing/broken analysis dep never crashes the copy.
            analysis = load_analysis(cfg)
            if analysis:
                try:
                    stage("reporting")
                    analysis.report_metadata(dest, cfg)
                except Exception as e:                   # noqa: BLE001
                    print(f"[report] {e}")
                try:
                    stage("contactsheet")
                    analysis.build_card_sheets(dest, cfg, lambda i: stage("contactsheet", i))
                except Exception as e:                   # noqa: BLE001
                    print(f"[contact] {e}")
    except Cancelled:
        res.update(status="cancelled", error="cancelled (no source data lost)")
    except Exception as e:                               # noqa: BLE001
        res.update(status="error", error=str(e))
    finally:
        stage("done")
    return res


# --------------------------------------------------------------------------- #
# preview: one-frame thumbnail (readability check) + short playable clip, both
# streamed from the START of the remote file over SSH (ffmpeg stops early)
# --------------------------------------------------------------------------- #
def _thumb_key(ip, remote_path, kind, extra=""):
    return hashlib.sha1(f"{kind}:{ip}:{remote_path}:{extra}".encode()).hexdigest()


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def remote_thumb(cfg, ip, remote_path):
    """Cached 320px JPEG of ONE frame, decoded ON THE PI (its file is local/seekable,
    so an mp4 with its moov atom at the end works) and streamed back as a tiny JPEG —
    this is the 'is the video readable?' check. Returns the path or None."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{_thumb_key(ip, remote_path, 'thumb')}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    cmd = (f"ffmpeg -nostdin -loglevel error -i {_q(remote_path)} -frames:v 1 "
           f"-vf scale=320:-1 -f image2pipe -vcodec mjpeg pipe:1")
    data = _ssh_binary(cfg, ip, cmd, timeout=60).stdout or b""
    if data[:2] == b"\xff\xd8":                          # JPEG magic — decoded OK
        out.write_bytes(data)
        return out
    return None


def remote_preview(cfg, ip, remote_path):
    """Cached browser-playable H.264 of the first preview.seconds, transcoded ON THE PI
    (originals are HEVC) and streamed back as a fragmented mp4 (so it survives a pipe).
    Returns path or None."""
    pv = cfg.get("preview", {})
    secs, width = int(pv.get("seconds", 20) or 0), int(pv.get("width", 480))
    fps, crf = int(pv.get("fps", 15)), int(pv.get("crf", 34))
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{_thumb_key(ip, remote_path, 'prev', f'{secs}:{width}:{fps}:{crf}')}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    t = f"-t {secs} " if secs > 0 else ""
    cmd = (f"ffmpeg -nostdin -loglevel error {t}-i {_q(remote_path)} "
           f"-vf \"scale='min({width},iw)':-2\" -r {fps} -c:v libx264 -preset ultrafast "
           f"-crf {crf} -pix_fmt yuv420p -movflags frag_keyframe+empty_moov -an -f mp4 pipe:1")
    data = _ssh_binary(cfg, ip, cmd, timeout=240).stdout or b""
    if len(data) > 1000 and b"ftyp" in data[:64]:        # looks like an mp4
        out.write_bytes(data)
        return out
    return None


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cfg = load_config()
    cards, iface = detect_recorders(cfg)
    print(f"wired iface: {iface}   recorders: {len(cards)}")
    for c in cards:
        print(f"  slot {c['slot']}  {c['device'] or '(unnamed)'}  ip={c['ip']}  "
              f"{c['runs']} runs  {_fmt_size(c['size'])}  id={c['label']}  {c['fstype']}")
