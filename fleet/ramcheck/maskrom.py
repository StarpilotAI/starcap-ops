"""
Find a Rockchip board on USB and send it the cpuid/ program (see cpuid/README.md).

The board's boot ROM shows up as 2207:xxxx. In MASKROM it reports bcdDevice
1.00; once a loader runs from its RAM it re-enumerates as 2.xx ("loader" mode).
Only maskrom accepts the cpuid program. A dead-RAM R76S falls into maskrom by
itself after its failed RAM check, so plugging the A-to-A cable is usually enough.
"""
import glob
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CPUID_DIR = os.path.join(HERE, "cpuid")
LOADER = os.path.join(CPUID_DIR, "cpuid_loader.bin")
UPGRADE_TOOL = os.path.join(HERE, "..", "..", "flashing", "nanopi", "emmc", "assets",
                            "upgrade_tool_v2.44_for_linux", "upgrade_tool")
SYSFS = "/sys/bus/usb/devices"


def _read(d, name):
    try:
        with open(os.path.join(d, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_board(sysfs=SYSFS):
    """The first Rockchip device on USB as a dict, or None."""
    for d in sorted(glob.glob(os.path.join(sysfs, "*"))):
        if _read(d, "idVendor") != "2207":
            continue
        bus, dev = _read(d, "busnum"), _read(d, "devnum")
        if not (bus and dev):
            continue
        node = f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}"
        bcd = _read(d, "bcdDevice")
        return {"id": f"2207:{_read(d, 'idProduct')}", "node": node,
                "mode": "maskrom" if bcd.startswith("01") else "loader",
                "writable": os.access(node, os.W_OK)}
    return None


def send_cpuid(timeout=60):
    """Run `upgrade_tool db cpuid_loader.bin`. -> (ok, message).

    The program prints on the UART; RAM Check's reader shows it. upgrade_tool
    itself only proves the boot ROM accepted it."""
    if not os.path.exists(LOADER):
        try:
            subprocess.run([os.path.join(CPUID_DIR, "build.sh")], check=True,
                           capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"cpuid_loader.bin missing and build.sh failed: {e}"
    if not os.path.exists(UPGRADE_TOOL):
        return False, f"upgrade_tool not found at {UPGRADE_TOOL}"
    try:
        p = subprocess.run([UPGRADE_TOOL, "db", LOADER], cwd=os.path.dirname(UPGRADE_TOOL),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "upgrade_tool timed out"
    except OSError as e:
        return False, f"could not run upgrade_tool: {e}"
    out = (p.stdout + p.stderr).replace("\x1b[1A\x1b[2K", "")
    if "Download boot ok" in out:
        return True, "Sent. The chip prints its cpu_id on the serial line."
    tail = " ".join(l.strip() for l in out.strip().splitlines()[-3:])
    return False, f"upgrade_tool: {tail or 'no output'}"
