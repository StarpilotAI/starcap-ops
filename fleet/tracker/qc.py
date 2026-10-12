"""
qc.py: has a StarCap been through StarCap QC (~/starcap-qc), and did it pass?

QC is the last station on the NanoPi line: all 157 runs on 2026-10-12 were NanoPi
R76S boards, and the Orange Pi fleet predates the tool. So QC status is only shown for
NanoPi caps; an Orange Pi gets None, not "never QC'd". A cap counts as NanoPi when its
board says so, or when QC itself saw it (7 caps have no board on record yet).

A cap's status is its latest finished run (pass or fail). Cancelled runs don't count
either way: a cap with only cancelled runs has never finished QC.

Pure functions: rows in, dicts out, times left as epoch seconds.
"""
import calib
import fleet

FINISHED = ("pass", "fail")


def is_nanopi(board):
    return "nanopi" in (board or "").lower()


def by_cpu(runs):
    """{cpu_id: its finished runs, newest first}."""
    out = {}
    for r in sorted(runs, key=lambda r: r.get("finished_at") or 0, reverse=True):
        if r.get("verdict") in FINISHED:
            out.setdefault(r["cpu_id"], []).append(r)
    return out


def qc_cpus(runs):
    """Every cpu_id QC has seen, finished or not (proof the board is a NanoPi)."""
    return {r["cpu_id"] for r in runs}


def status(cpu_id, board, runs_by_cpu, seen):
    """A cap's QC status, or None when it isn't a NanoPi:
    {"status": pass|fail|never, "at": that run's finish, "passed_at": newest pass,
     "failed_steps": what the latest run failed, "runs": finished runs}."""
    if not (is_nanopi(board) or cpu_id in seen):
        return None
    runs = runs_by_cpu.get(cpu_id, [])
    if not runs:
        return {"status": "never", "at": None, "passed_at": None, "failed_steps": [], "runs": 0}
    last = runs[0]
    passed = next((r["finished_at"] for r in runs if r["verdict"] == "pass"), None)
    return {"status": last["verdict"], "at": last["finished_at"], "passed_at": passed,
            "failed_steps": list(last.get("failed_steps") or []) if last["verdict"] == "fail" else [],
            "runs": len(runs)}


def history(cpu_id, runs_by_cpu):
    """A cap's finished QC runs for its page, newest first."""
    return [{"at": r["finished_at"], "verdict": r["verdict"], "headline": r.get("headline"),
             "failed_steps": list(r.get("failed_steps") or []), "version": r.get("version"),
             "camera": r.get("camera_id")} for r in runs_by_cpu.get(cpu_id, [])]


def overview(starcaps, runs, out_cpus, available):
    """GET /api/field's `qc`: every fleet NanoPi's status in three counts, and the ones
    without a pass (never or last run failed), the ones out in the field first."""
    grouped, seen = by_cpu(runs), qc_cpus(runs)
    rows = []
    for c in starcaps:
        if not fleet.FLEET_NAME.fullmatch(c.get("name") or ""):
            continue
        st = status(c["cpu_id"], c.get("board"), grouped, seen)
        if st is not None:
            rows.append(dict(st, cap=c["name"], out_now=c["cpu_id"] in out_cpus))
    counts = {s: sum(r["status"] == s for r in rows) for s in ("pass", "fail", "never")}
    not_passed = sorted((r for r in rows if r["status"] != "pass"),
                        key=lambda r: (not r["out_now"], r["status"] != "fail", calib.natural_key(r["cap"])))
    return {"available": available, "nanopi": len(rows), "passed": counts["pass"], "failed": counts["fail"],
            "never": counts["never"],
            "not_passed": [{k: r[k] for k in ("cap", "status", "at", "failed_steps", "out_now", "runs")}
                           for r in not_passed]}
