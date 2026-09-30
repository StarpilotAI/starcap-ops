#!/usr/bin/env python3
"""Which camera? — ask, do not guess.

Every tool in this fleet used to auto-pick "the stereo camera" by card name and
take the first match. That is wrong the moment two are docked, and it is silently
wrong: you get a calibration, a take, or a bench run against a camera you did not
mean, and nothing in the output says which one it was.

It is also wrong across replugs. Node numbers are not stable — a camera that was
/dev/video0 comes back as /dev/video4 after a re-enumeration, and anything
holding the old number is talking to a node that no longer exists.

So: enumerate, and when the answer is ambiguous, ASK.

    dev = camselect.choose()                 # prompts only if ambiguous
    dev = camselect.choose("/dev/video4")    # explicit wins, never prompts
    for c in camselect.cameras(): ...        # to build your own picker
"""
import glob
import os
import re
import subprocess
import sys

# Capture nodes only. A UVC camera also exposes a metadata node (video5 next to
# video4) which enumerates fine, carries the same card name, and cannot produce a
# frame. The discriminator is the node's own "Device Caps" — NOT the top-level
# "Capabilities", which is the whole device's union and says Video Capture for
# the metadata node too.
_DEVCAPS = re.compile(r"Device Caps.*?:\s*0x[0-9a-f]+\s*\n(.*?)(?=\n\w|\Z)",
                      re.S | re.I)


class Cam:
    __slots__ = ("dev", "card", "bus", "formats")

    def __init__(self, dev, card, bus, formats):
        self.dev, self.card, self.bus, self.formats = dev, card, bus, formats

    @property
    def stereo(self):
        """A 4000-wide mode is the fleet's stereo pair (160 strip + 2x1920)."""
        return any(w >= 3840 for w, _h in self.formats)

    @property
    def best(self):
        return max(self.formats, default=(0, 0))

    def __repr__(self):
        w, h = self.best
        return f"{self.dev}  {self.card}" + (f"  {w}x{h}" if w else "")


def _run(*a, timeout=6):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout or ""
    except Exception:                                        # noqa: BLE001
        return ""


def cameras():
    """Every capture-capable /dev/videoN, in node order."""
    out = []
    for dev in sorted(glob.glob("/dev/video*"),
                      key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        info = _run("v4l2-ctl", "-d", dev, "--info")
        caps = _DEVCAPS.search(info)
        if not caps or "Video Capture" not in caps.group(1):
            continue
        card = re.search(r"Card type\s*:\s*(.+)", info)
        bus = re.search(r"Bus info\s*:\s*(.+)", info)
        sizes = set()
        for m in re.finditer(r"Size: Discrete (\d+)x(\d+)",
                             _run("v4l2-ctl", "-d", dev, "--list-formats-ext")):
            sizes.add((int(m.group(1)), int(m.group(2))))
        if not sizes:
            continue                      # enumerates but offers no frame size
        out.append(Cam(dev, (card.group(1).strip() if card else "?"),
                       (bus.group(1).strip() if bus else "?"), sorted(sizes)))
    return out


def describe(cams=None):
    """Numbered lines for a menu, stereo cameras marked."""
    cams = cameras() if cams is None else cams
    lines = []
    for i, c in enumerate(cams, 1):
        w, h = c.best
        tag = "  [stereo]" if c.stereo else ""
        lines.append(f"  {i}) {c.dev:14} {c.card[:38]:38} "
                     f"{(f'{w}x{h}' if w else '?'):>10}{tag}")
    return lines


def choose(preferred=None, prompt=True, require_stereo=False, why=""):
    """Resolve a device path.

    `preferred` (a flag, an env var, a settings value) always wins and never
    prompts — a script that names its camera must stay non-interactive. Otherwise
    one candidate is used silently and several are put to the operator.

    Returns None if there is nothing to pick or the operator declines.
    """
    if preferred:
        return preferred if str(preferred).startswith("/dev/") else f"/dev/video{preferred}"
    cams = cameras()
    if require_stereo:
        cams = [c for c in cams if c.stereo] or cams
    if not cams:
        print("no capture-capable /dev/video* found", file=sys.stderr)
        return None
    if len(cams) == 1:
        return cams[0].dev
    if not prompt or not sys.stdin.isatty():
        # Non-interactive and ambiguous: say which one and why, never pick mutely.
        print(f"[camselect] {len(cams)} cameras present, using {cams[0].dev} "
              f"({cams[0].card}) — pass --device to choose", file=sys.stderr)
        return cams[0].dev
    print(f"\n{len(cams)} cameras are connected{(' — ' + why) if why else ''}:")
    for line in describe(cams):
        print(line)
    while True:
        try:
            r = input(f"which one? [1-{len(cams)}, or a /dev/videoN] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not r:
            continue
        if r.startswith("/dev/"):
            return r
        if r.isdigit() and 1 <= int(r) <= len(cams):
            return cams[int(r) - 1].dev
        print("  not one of those")


if __name__ == "__main__":
    cams = cameras()
    if not cams:
        print("no capture-capable /dev/video* found")
    else:
        print(f"{len(cams)} camera(s):")
        print("\n".join(describe(cams)))
