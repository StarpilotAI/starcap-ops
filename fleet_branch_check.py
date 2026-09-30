#!/usr/bin/env python3
"""
fleet_branch_check — is every StarCap actually on main?

Answers the question starcap28 failed on 2026-08-08: it was RUNNING main's code
while its git checkout sat on a five-week-old `feat/adaptive-wifi-uplink`. The
self-updater had been overlaying main tarballs on top of that stale checkout, so
everything looked fine until someone ran `git status` — and any routine
`git checkout .` / `git pull` there would have reverted the board to July code.

The three conditions a board must satisfy to be safely, permanently on main
(the pin-to-main recipe):

  1. NO .git in ~/orangepi-recorder      — a checkout is a landmine, not a config
  2. UPDATE_BRANCH=main set EXPLICITLY   — updater.py defaults to main, but
                                            depending on a default is how drift hides
  3. UPDATE_ENABLE=1 set EXPLICITLY      — same reasoning

Read-only by default. `--fix` applies exactly those three, nothing else.

Discovery reuses the recorder's own UDP roll-call (the same one starcapfinder
uses), so it finds whatever is docked right now. Boards that are off or
undocked simply aren't listed — absence here is NOT proof a board is clean.

Usage:
    ./fleet_branch_check.py                 # discover + report
    ./fleet_branch_check.py --fix           # discover + report + repair drifted
    ./fleet_branch_check.py --host 10.77.0.228 [--host ...]
    ./fleet_branch_check.py --timeout 3.0
"""
import argparse
import json
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

PEER_MAGIC = b"STARCAP_PEER_DISCOVER v1"
PEER_PORT = 18793
# Orange Pi 5 images use `orangepi`; NanoPi R76S images use `pi`. Try both.
SSH_USERS = ("orangepi", "pi")
REPO_DIR = "~/orangepi-recorder"

OK, WARN, BAD, DIM = "\033[32m", "\033[33m", "\033[31m", "\033[2m"
OFF, BOLD = "\033[0m", "\033[1m"


def local_broadcasts():
    """Every interface broadcast address — a board on the opm-lan cable never
    hears a broadcast sent only out the wifi interface."""
    outs = set()
    try:
        r = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if "brd" in parts:
                b = parts[parts.index("brd") + 1]
                if not b.startswith(("127.", "169.254.")):
                    outs.add(b)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    outs.add("255.255.255.255")
    return sorted(outs)


def roll_call(timeout=2.0):
    """Broadcast the peer magic; collect every recorder that answers."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    found = {}
    try:
        for b in local_broadcasts():
            try:
                s.sendto(PEER_MAGIC, (b, PEER_PORT))
            except OSError:
                pass
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
                d = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if d.get("device_id"):
                d.setdefault("ip", addr[0])
                found[d["device_id"]] = d
    finally:
        s.close()
    return list(found.values())


def ssh(host, user, cmd, timeout=20):
    """Key-auth only — never prompt, so one unreachable board can't hang the sweep."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={max(4, int(timeout / 3))}",
             f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return 255, "", "ssh failed/timed out"


PROBE = (
    f'cd {REPO_DIR} 2>/dev/null || exit 9; '
    'echo "VERSION=$(cat stereocam/VERSION 2>/dev/null)"; '
    'if [ -d .git ]; then '
    '  echo "GIT=yes"; echo "BRANCH=$(git branch --show-current 2>/dev/null)"; '
    '  echo "DIRTY=$(git status --short 2>/dev/null | wc -l)"; '
    'else echo "GIT=no"; fi; '
    'systemctl show stereocam -p Environment --value 2>/dev/null | tr " " "\\n" '
    '  | grep -E "^UPDATE_(BRANCH|ENABLE)=" || true'
)


def probe(board, timeout):
    host = board.get("ip")
    name = board.get("name") or board.get("hostname") or host
    for user in SSH_USERS:
        rc, out, err = ssh(host, user, PROBE, timeout=timeout)
        if rc == 0:
            info = {"name": name, "host": host, "user": user, "reachable": True}
            for line in out.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    info[k.strip()] = v.strip()
            return info
        if rc == 9:
            return {"name": name, "host": host, "user": user, "reachable": True,
                    "error": f"no {REPO_DIR}"}
    return {"name": name, "host": host, "user": None, "reachable": False,
            "error": "ssh unreachable (key auth)"}


def verdict(i):
    """-> (label, color, [reasons]). Pinned means all three conditions hold."""
    if not i.get("reachable"):
        return "UNREACHABLE", DIM, [i.get("error", "")]
    if i.get("error"):
        return "ERROR", BAD, [i["error"]]
    bad = []
    if i.get("GIT") == "yes":
        br = i.get("BRANCH") or "(detached)"
        d = i.get("DIRTY", "?")
        bad.append(f".git present on '{br}' ({d} modified) — a git op here reverts the board")
    if i.get("UPDATE_BRANCH") != "main":
        bad.append(f"UPDATE_BRANCH not explicitly main (got {i.get('UPDATE_BRANCH') or 'unset'})")
    if i.get("UPDATE_ENABLE") not in ("1", "true", "yes"):
        bad.append(f"UPDATE_ENABLE not explicitly on (got {i.get('UPDATE_ENABLE') or 'unset'})")
    if not bad:
        return "ON MAIN", OK, []
    # unset env still self-updates from main by default — drifted, not broken
    hard = any("git" in b for b in bad)
    return ("DRIFTED" if hard else "IMPLICIT"), (BAD if hard else WARN), bad


FIX = (
    f'cd {REPO_DIR} 2>/dev/null || exit 9; '
    'if [ -d .git ]; then tar czf ~/git-metadata-backup-$(date +%Y%m%d-%H%M%S).tar.gz .git '
    '   >/dev/null 2>&1; rm -rf .git; echo "removed .git (backed up)"; fi; '
    'sudo -n mkdir -p /etc/systemd/system/stereocam.service.d 2>/dev/null && '
    'printf "[Service]\\nEnvironment=UPDATE_BRANCH=main\\nEnvironment=UPDATE_ENABLE=1\\n" '
    '  | sudo -n tee /etc/systemd/system/stereocam.service.d/20-pin-main.conf >/dev/null && '
    'sudo -n systemctl daemon-reload && echo "pinned to main" '
    '  || echo "NOTE: .git handled, but the drop-in needs sudo (run with a password)"'
)


def main():
    ap = argparse.ArgumentParser(description="Check every docked StarCap is pinned to main")
    ap.add_argument("--host", action="append", default=[],
                    help="explicit IP (repeatable); skips discovery")
    ap.add_argument("--timeout", type=float, default=2.0, help="roll-call seconds")
    ap.add_argument("--fix", action="store_true",
                    help="repair drifted boards (remove .git, pin UPDATE_* to main)")
    a = ap.parse_args()

    if a.host:
        boards = [{"ip": h, "name": h, "device_id": h} for h in a.host]
    else:
        print(f"{DIM}roll-call on udp/{PEER_PORT}…{OFF}")
        boards = roll_call(a.timeout)
    if not boards:
        print("No StarCaps answered. They must be powered and on the same network.")
        print("Absence here is NOT proof a board is clean — only docked boards reply.")
        return 1

    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(lambda b: probe(b, 25), boards))

    print(f"\n{BOLD}{'board':<14}{'version':<10}{'branch state':<34}{'verdict'}{OFF}")
    print("-" * 78)
    drifted = []
    for i in sorted(infos, key=lambda x: str(x.get("name"))):
        label, col, reasons = verdict(i)
        git = "plain tree" if i.get("GIT") == "no" else (
            f".git @ {i.get('BRANCH') or '?'}" if i.get("GIT") == "yes" else "—")
        print(f"{str(i.get('name'))[:13]:<14}{str(i.get('VERSION') or '—'):<10}{git:<34}{col}{label}{OFF}")
        for r in reasons:
            print(f"{'':<24}{DIM}· {r}{OFF}")
        if label in ("DRIFTED", "IMPLICIT"):
            drifted.append(i)

    # A board we could not inspect is UNKNOWN, never "fine". Counting it as a pass
    # is how a checker quietly certifies a fleet it never actually looked at.
    unknown = [i for i in infos if verdict(i)[0] in ("UNREACHABLE", "ERROR")]
    checked = len(infos) - len(unknown)

    print()
    if unknown:
        print(f"{BAD}{len(unknown)} board(s) COULD NOT BE CHECKED "
              f"— state unknown, NOT a pass:{OFF}")
        for i in unknown:
            print(f"  {str(i.get('name')):<14}{i.get('error', '')}")
        print(f"{DIM}  (usually no SSH key installed: ssh-copy-id <user>@<ip>){OFF}")
        print()
    if not drifted:
        if checked:
            print(f"{OK}All {checked} INSPECTED board(s) are pinned to main.{OFF}")
        else:
            print(f"{BAD}Nothing was actually inspected.{OFF}")
        return 0 if (checked and not unknown) else 1
    print(f"{WARN}{len(drifted)} of {checked} inspected board(s) not properly pinned.{OFF}")
    if not a.fix:
        print("Re-run with --fix to repair them (removes .git after backing it up,")
        print("writes the UPDATE_BRANCH/UPDATE_ENABLE drop-in). Needs passwordless sudo.")
        return 1

    print(f"\n{BOLD}Fixing…{OFF}")
    for i in drifted:
        rc, out, err = ssh(i["host"], i["user"], FIX, timeout=60)
        status = out or err or f"rc={rc}"
        print(f"  {i['name']:<14}{status}")
    print("\nRe-run without --fix to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
