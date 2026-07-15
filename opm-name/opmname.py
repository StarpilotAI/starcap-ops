"""
opm-name engine — bulk rename fleet recorders as they dock over ethernet.

Reuses opm-lan's LAN discovery (same wired-subnet auto-detect, same ping-sweep)
so it behaves identically to opm-lan for "what's currently docked". Renaming
itself is the same mechanism as tools/name_pi.sh: read the board's cpu_id over
SSH, then HSET the name straight into the same Upstash Redis `starcap:names`
hash the dashboard's setName()/API route uses. That's the dashboard's single
source of truth for names (keyed by cpu_id, stable across reflashes) — the
recorder itself self-syncs its local ~/.config/starcap/device_name from there
on its next heartbeat (~1s cadence), so writing to Redis is sufficient to
rename it "on the device and on the dashboard" in one step; nothing needs to be
SSHed onto the board to set a name directly.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "opm-lan"))
import opmlan  # noqa: E402  (reuse discover() / wired_iface_subnet() — same LAN behavior as opm-lan)

SSH_USER = os.environ.get("PI_USER", "orangepi")
SSH_PASS = os.environ.get("PI_PASS", "orangepi")

_ENV_FILE = Path.home() / "starcap-dashboard" / "dashboard" / ".env.local"


def _load_redis_creds():
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and tok:
        return url, tok
    if _ENV_FILE.is_file():
        for line in _ENV_FILE.read_text().splitlines():
            m = re.match(r'^UPSTASH_REDIS_REST_URL=(.*)$', line)
            if m: url = m.group(1).strip().strip('"')
            m = re.match(r'^UPSTASH_REDIS_REST_TOKEN=(.*)$', line)
            if m: tok = m.group(1).strip().strip('"')
    return url, tok


REDIS_URL, REDIS_TOKEN = _load_redis_creds()


def _ssh(ip, script, timeout=10):
    return subprocess.run(
        ["sshpass", "-p", SSH_PASS, "ssh",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=/dev/null",
         "-o", f"ConnectTimeout={min(timeout, 8)}",
         f"{SSH_USER}@{ip}", script],
        capture_output=True, text=True, timeout=timeout)


# Same defaults stereocam/app.py's BUZZER_CHIP/LINE/ACTIVE_LOW fall back to
# (pin 15 = GPIO0_D4) — see fix_boot_buzzer.sh. Zero orangepi-recorder code
# changes: this stops the systemd service (which releases the GPIO line and
# gracefully finalizes any in-flight take — the app already handles SIGTERM
# that way, same as a normal `systemctl restart`), grabs the pin directly with
# the exact same lgpio calls buzzer.py uses, beeps twice, then restarts the
# service. A few seconds of the board being briefly offline, nothing more.
_IDENTIFY_SCRIPT = r'''
set -e
echo '{pw}' | sudo -S systemctl stop stereocam 2>/dev/null
python3 - <<'PY'
import lgpio, time
CHIP, LINE, SILENT = 0, 28, 0   # active-low=0 default -> idle level is LOW
h = lgpio.gpiochip_open(CHIP)
lgpio.gpio_claim_output(h, LINE, SILENT)
for _ in range(2):
    lgpio.tx_pwm(h, LINE, 523, 50)   # C5 tone, matches the recorder's own "start" jingle
    time.sleep(0.15)
    lgpio.tx_pwm(h, LINE, 0, 0)
    lgpio.gpio_write(h, LINE, SILENT)
    time.sleep(0.13)
lgpio.gpio_free(h, LINE)
lgpio.gpiochip_close(h)
PY
echo '{pw}' | sudo -S systemctl start stereocam 2>/dev/null
'''


def identify_buzz(ip, timeout=20):
    """Beep the board's buzzer twice so whoever's renaming it can match the
    physical unit to its row in the UI. See _IDENTIFY_SCRIPT's comment — no
    orangepi-recorder code involved, just a temporary stop/beep/restart."""
    script = _IDENTIFY_SCRIPT.format(pw=SSH_PASS)
    r = _ssh(ip, script, timeout=timeout)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "failed").strip()[:200]}
    return {"ok": True}


_CPU_ID_SCRIPT = r'''
(cd ~/orangepi-recorder/stereocam 2>/dev/null && python3 -c "import uploader; print(uploader.cpu_id())" 2>/dev/null) && exit 0
v=$(tr -d '\0' < /proc/device-tree/serial-number 2>/dev/null || true)
if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
v=$(grep -i '^serial' /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | tr -d ' \t')
if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
echo ""
'''


def identify_for_rename(ip, timeout=10):
    """Cheap identity probe for the rename table: cpu_id (the rename key) +
    current device_name (for display). Much lighter than opm-lan's identify()
    — no recordings walk, no byte counts, nothing this tool doesn't need."""
    script = (
        f'echo "CPUID=$({_CPU_ID_SCRIPT})"; '
        f'echo "NAME=$(cat ~/.config/starcap/device_name 2>/dev/null)"; '
        f'echo "HOST=$(hostname)"; '
        f'echo "VER=$(cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null | head -1)"'
    )
    r = _ssh(ip, script, timeout=timeout)
    if r.returncode != 0 or "CPUID=" not in r.stdout:
        return {"ip": ip, "ok": False, "error": (r.stderr or "unreachable").strip()[:120]}
    grab = lambda k: (re.search(rf"^{k}=(.*)$", r.stdout, re.M) or [None, ""])[1].strip()
    cpu_id = grab("CPUID")
    if not cpu_id:
        return {"ip": ip, "ok": False, "error": "couldn't read a stable hardware id (not an RK3588 board?)"}
    return {"ip": ip, "ok": True, "cpu_id": cpu_id, "name": grab("NAME"), "hostname": grab("HOST"),
            "version": grab("VER")}


def discover_for_rename(cfg, timeout=10):
    """Ping-sweep (via opm-lan's own subnet auto-detect) + identify every live IP."""
    ips, iface = opmlan.discover(cfg)
    return [identify_for_rename(ip, timeout=timeout) for ip in ips], iface


def clear_recordings(ip, timeout=20):
    """Wipe dummy/test recordings off a board — for clearing out test takes
    made while checking field-readiness, before it actually goes out. Empties
    BOTH possible recording locations (doesn't know in advance which one this
    board uses): the local path (SD-booted boards) and /media/sdcard/stereo
    (eMMC-booted boards) — whichever doesn't apply is just a harmless no-op."""
    script = (
        'rm -rf ~/orangepi-recorder/recordings/stereo/*/ 2>/dev/null; '
        'rm -rf /media/sdcard/stereo/*/ 2>/dev/null; '
        'true'
    )
    r = _ssh(ip, script, timeout=timeout)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "failed").strip()[:200]}
    return {"ok": True}


def _redis_call(*args):
    if not (REDIS_URL and REDIS_TOKEN):
        raise RuntimeError("missing UPSTASH_REDIS_REST_URL/TOKEN — set env vars or "
                            "create ~/starcap-dashboard/dashboard/.env.local")
    r = subprocess.run(
        ["curl", "-sS", "-X", "POST", REDIS_URL,
         "-H", f"Authorization: Bearer {REDIS_TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(list(args))],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "curl failed")
    return r.stdout


def set_authoritative_name(cpu_id, name):
    """Same write name_pi.sh does: HSET into starcap:names, keyed by cpu_id.
    The board picks this up on its next heartbeat (~1s) and self-corrects its
    own local device_name to match — this one write is the whole rename."""
    now_ms = int(time.time() * 1000)
    entry = json.dumps({"name": name, "updated_at": now_ms})
    return _redis_call("HSET", "starcap:names", cpu_id, entry)
