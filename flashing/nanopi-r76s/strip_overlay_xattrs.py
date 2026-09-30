#!/usr/bin/env python3
"""strip_overlay_xattrs.py — make an overlayfs UPPER layer re-homeable.

WHY THIS EXISTS
---------------
On the NanoPi R76S, `/` is an overlayfs: lower = rootfs (p8), upper = /data/root
(p9). When you clone a board by copying its /data/root into a golden image with
`rsync -aHAX`, the `-X` faithfully copies the overlay's OWN bookkeeping xattrs:

    trusted.overlay.origin   - a file handle pointing at the SOURCE board's lower
    trusted.overlay.impure   - "this dir has copied-up entries"
    trusted.overlay.opaque   - "this dir fully masks the lower dir" (INTENTIONAL)

`origin` encodes the source board's p8 filesystem. On ANY other board the lower
has a different UUID, so on mount overlayfs decodes that handle, gets ESTALE
(-116), prints "failed to verify upper root origin", and REFUSES to assemble the
root. The board then falls back to the bare read-only lower and nothing that
needs to write can start (logind, sddm, the recorder) -> blinking cursor, no
desktop. This bit every board flashed from the 2026-08-01 golden image.

THE FIX
-------
Strip the copy-up/index bookkeeping (origin, impure, nlink, metacopy) from the
whole upper. KEEP opaque and redirect - those express intentional masking/renames
the source board actually meant. With origin gone, overlayfs treats the upper as
authoritative native content and, on first mount, re-sets a fresh origin against
the NEW board's lower. Self-heals per board.

USAGE
  sudo python3 strip_overlay_xattrs.py /path/to/upper_root
      e.g. the `root/` dir inside a mounted userdata.img, or /data/root on a board

Requires CAP_SYS_ADMIN (trusted.* xattrs) -> run as root. Idempotent.
"""
import os, sys

STRIP = {"trusted.overlay.origin", "trusted.overlay.impure",
         "trusted.overlay.nlink", "trusted.overlay.metacopy"}

def main():
    if len(sys.argv) != 2:
        sys.exit("usage: sudo python3 strip_overlay_xattrs.py <upper_root_dir>")
    root = sys.argv[1]
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")
    stripped, kept, errs = {}, {}, 0

    def handle(p):
        nonlocal errs
        try:
            names = os.listxattr(p, follow_symlinks=False)
        except OSError:
            return
        for n in names:
            if not n.startswith("trusted.overlay"):
                continue
            if n in STRIP:
                try:
                    os.removexattr(p, n, follow_symlinks=False)
                    stripped[n] = stripped.get(n, 0) + 1
                except OSError:
                    errs += 1
            else:
                kept[n] = kept.get(n, 0) + 1

    handle(root)                       # the upper root dir itself carries origin
    for dp, dns, fns in os.walk(root):
        for name in list(dns) + fns:
            handle(os.path.join(dp, name))

    print("stripped:", stripped or "none")
    print("kept:    ", kept or "none")
    print("errors:  ", errs)
    if errs:
        sys.exit(1)

if __name__ == "__main__":
    main()
