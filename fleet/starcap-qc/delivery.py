"""
delivery.py: judge a QC take on the data the customer actually receives.

The recorder writes its own frames.csv and imu.csv live, from a LEAKY branch: under a
moment of load that branch drops rows, so those files can show a half-second hole
while the footage itself is complete. The delivery pipeline never reads them for the
data it ships. It decodes the IMU samples and every frame's exposure time from the
IMU STRIP inside the video (`imustrip_NNN.mp4`), one row per video frame, and only
consults the recorder's frames.csv as a witness for a missing header.

So QC runs that same decoder, the delivery's own code, on the test take's strip and
judges frame timing and the IMU on its output. The code is loaded from the
starcap-dashboard repo:

    delivery/imu_csv.py           resolve_timeline, sample_quality, _ffmpeg_luma, ...
    firstSample/lib/imu_strip.py  StripDecoder

from `origin/main` after a fetch (the working tree is routinely far behind), cached in
`.delivery_cache/`. With no checkout at all, QC falls back to the recorder's CSVs and
says so.

The PyAV per-packet reader the pipeline falls back to is not needed here: the fast
ffmpeg path is used, and it is only trusted when ffmpeg returns exactly one picture
per packet. A strip that doesn't (damaged packets) is reported as such.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".delivery_cache"
REPO = Path(os.environ.get("STARCAP_DASHBOARD", Path.home() / "starcap-dashboard"))
FILES = {"imu_csv": "delivery/imu_csv.py", "imu_strip": "firstSample/lib/imu_strip.py"}
REFRESH_S = 3600.0
KEEP_ROWS, KEEP_COLS = 96 * 8, 144        # what the delivery's strip reader crops

_LOCK = threading.Lock()
_STATE = {"at": 0.0, "mods": None, "source": None, "error": None}


class Refused(Exception):
    """The delivery pipeline would refuse this strip; the message says why."""


def _git(*args, timeout=60):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)


def _refresh_cache():
    """origin/main's copy of both files into CACHE -> a description of where they came from."""
    if not (REPO / ".git").exists():
        return None
    try:
        fetched = _git("fetch", "-q", "origin", "main", timeout=45).returncode == 0
        texts = {}
        for name, path in FILES.items():
            r = _git("show", f"origin/main:{path}", timeout=20)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            texts[name] = r.stdout
        rev = _git("rev-parse", "--short", "origin/main", timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    CACHE.mkdir(exist_ok=True)
    for name, text in texts.items():
        tmp = CACHE / f"{name}.py.tmp"
        tmp.write_text(text)
        tmp.replace(CACHE / f"{name}.py")
    (CACHE / "SOURCE").write_text(f"origin/main {rev}" + ("" if fetched else " (fetch failed: may be stale)"))
    return (CACHE / "SOURCE").read_text()


def _import(directory):
    """Import imu_strip and imu_csv from one directory, as private module names (so they
    can never shadow or be shadowed by anything else on sys.path)."""
    mods = {}
    for name in ("imu_strip", "imu_csv"):
        spec = importlib.util.spec_from_file_location(f"_qc_delivery_{name}", directory / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"_qc_delivery_{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


def load():
    """-> (modules dict | None, where they came from or why not). Cached for an hour."""
    with _LOCK:
        if _STATE["mods"] is not None and time.time() - _STATE["at"] < REFRESH_S:
            return _STATE["mods"], _STATE["source"]
        source = _refresh_cache()
        directory = CACHE if source else None
        if directory is None and all((CACHE / f"{n}.py").is_file() for n in FILES):
            directory, source = CACHE, ((CACHE / "SOURCE").read_text() if (CACHE / "SOURCE").exists()
                                        else "cached copy") + " (couldn't refresh)"
        if directory is None and all((REPO / p).is_file() for p in FILES.values()):
            CACHE.mkdir(exist_ok=True)
            for name, path in FILES.items():
                (CACHE / f"{name}.py").write_text((REPO / path).read_text())
            directory, source = CACHE, "the local working tree (couldn't read origin/main)"
        if directory is None:
            _STATE.update(mods=None, source=None, at=time.time(),
                          error=f"no starcap-dashboard checkout at {REPO}")
            return None, _STATE["error"]
        try:
            mods = _import(directory)
            for need in ("resolve_timeline", "sample_quality", "read_witness", "_ffmpeg_luma"):
                if not hasattr(mods["imu_csv"], need):
                    raise AttributeError(f"delivery/imu_csv.py has no {need}() any more")
        except Exception as e:                        # noqa: BLE001
            _STATE.update(mods=None, source=None, at=time.time(), error=f"couldn't load it: {e}")
            return None, _STATE["error"]
        _STATE.update(mods=mods, source=source, at=time.time(), error=None)
        return mods, source


def _probe(strip):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=width,nb_read_packets", "-of", "json", str(strip)],
                       capture_output=True, text=True, timeout=300)
    try:
        s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return int(s.get("width") or 0), int(s.get("nb_read_packets") or 0)
    except (ValueError, TypeError):
        return 0, 0


def decode_strip(mods, strip, witness_csv=None):
    """One imustrip_NNN.mp4 through the delivery's own decoder ->
    {"frames", "exposures_us" (one per video frame), "samples" [(ts_us, [a], [g])],
     "quality" {hz, max_gap_us, deficient}, "filled", "no_header", "clamp"}.
    Raises Refused with the delivery's own reason when it would not ship this strip."""
    ic, ist = mods["imu_csv"], mods["imu_strip"]
    width, packets = _probe(strip)
    if width <= 0 or packets <= 0:
        raise Refused(f"{Path(strip).name} has no decodable video stream")
    dec = ist.StripDecoder(width)
    exposures, per_frame = [], []
    try:
        for luma in ic._ffmpeg_luma(str(strip), width, packets, KEEP_ROWS, KEEP_COLS):
            dec._last_ts = None                   # no state across frames: the pipeline's rule
            got = dec.decode(luma, KEEP_COLS)
            exposure, samples = got if got is not None else (None, ())
            exposures.append(exposure[0] if exposure else None)
            per_frame.append(tuple(samples))
    except Exception as e:                        # noqa: BLE001  _FastPathShort and the like
        raise Refused(f"{Path(strip).name} doesn't decode frame for frame ({e}): damaged packets") from None
    witness = ic.read_witness(str(witness_csv)) if witness_csv else None
    try:
        values, kept, clamp, filled = ic.resolve_timeline(exposures, per_frame, witness, None, None,
                                                          Path(strip).name)
    except RuntimeError as e:
        raise Refused(str(e)) from None
    return {"frames": len(values), "packets": packets, "exposures_us": values, "samples": kept,
            "quality": ic.sample_quality(kept), "filled": filled, "clamp": clamp,
            "no_header": sum(1 for e in exposures if e is None)}
