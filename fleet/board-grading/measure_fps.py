#!/usr/bin/env python3
"""Two-point fps over a board's :8080/api/status frame counter.

    measure_fps.py <board-ip> <label> <seconds>
"""
import json, sys, time, urllib.request
IP=sys.argv[1]; LABEL=sys.argv[2]; SECS=float(sys.argv[3])
def st(): return json.load(urllib.request.urlopen(f"http://{IP}:8080/api/status", timeout=5))
for _ in range(200):
    try:
        d=st()
        if d["camera"]["recording"] and (d["camera"]["duration"] or 0) > 12: break
    except Exception: pass
a=st(); t0=time.monotonic()
while time.monotonic()-t0 < SECS:
    try: st()
    except Exception: pass
b=st(); dt=time.monotonic()-t0
df=b["camera"]["total_frames"]-a["camera"]["total_frames"]
print("  %-16s TRUE fps %.3f  frames %d/%.1fs  dropped +%d  cpu %.0f%%  load %.2f  soc %sC" % (
  LABEL, df/dt, df, dt, b["camera"]["dropped"]-a["camera"]["dropped"],
  b["system"]["cpu_percent"], b["system"]["load_avg"][0], b["system"]["soc_temp"]))
