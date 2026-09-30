#!/usr/bin/env python3
"""Fullscreen brightness ramp — the stimulus for the frame-jitter repro.

The camera stays BOLTED DOWN and only the light changes, so any jitter this
produces cannot be blamed on rotation or shake. Point the camera at this screen
in a dim room, run a take, then correlate.

Schedule (default, 75 s). Every phase is separable in the recorded video's own
luma trace, so no clock alignment with the board is needed:

    0-15 s   HOLD mid      constant  -> control: expect no events
   15-35 s   SLOW ramp     ~7 %/s    -> gentle, below the observed trigger
   35-40 s   HOLD bright   constant  -> control
   40-60 s   FAST ramps    ~150 %/s  -> matches event B (2.5 %/frame)
   60-75 s   HOLD mid      constant  -> control

    ./lightramp.py            # run the schedule
    ./lightramp.py --hold 128 # static grey, for the constant-light take
Esc or q quits.
"""
import argparse, time, tkinter as tk

SCHED = [("hold", 15, 128, 128), ("ramp", 10, 8, 255), ("ramp", 10, 255, 8),
         ("hold",  5, 235, 235),
         ("fast", 20, 8, 255),
         ("hold", 15, 128, 128)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=None, help="static grey level 0-255 and nothing else")
    ap.add_argument("--fast-period", type=float, default=2.0, help="seconds per dark->bright->dark cycle")
    a = ap.parse_args()

    root = tk.Tk(); root.title("lightramp")
    root.attributes("-fullscreen", True)
    cv = tk.Canvas(root, highlightthickness=0, bd=0); cv.pack(fill="both", expand=True)
    lbl = tk.Label(root, text="", font=("monospace", 14))
    root.bind("<Escape>", lambda e: root.destroy()); root.bind("q", lambda e: root.destroy())

    log = open("/tmp/lightramp.log", "w")
    t0 = time.time()

    def grey(v):
        v = max(0, min(255, int(v)))
        cv.configure(bg=f"#{v:02x}{v:02x}{v:02x}")
        return v

    if a.hold is not None:
        grey(a.hold)
        log.write(f"{t0:.3f} hold {a.hold}\n"); log.flush()
        root.mainloop(); return

    total = sum(d for _, d, _, _ in SCHED)
    def tick():
        t = time.time() - t0
        if t >= total:
            root.destroy(); return
        acc = 0
        for kind, dur, lo, hi in SCHED:
            if t < acc + dur:
                f = (t - acc) / dur
                if kind == "hold":
                    v = lo
                elif kind == "ramp":
                    v = lo + (hi - lo) * f
                else:                                  # fast triangle wave
                    p = ((t - acc) % a.fast_period) / a.fast_period
                    v = lo + (hi - lo) * (2 * p if p < 0.5 else 2 * (1 - p))
                v = grey(v)
                log.write(f"{time.time():.3f} {t:7.3f} {kind} {v}\n")
                break
            acc += dur
        root.after(16, tick)

    log.write(f"{t0:.3f} START\n"); log.flush()
    root.after(16, tick)
    root.mainloop()
    log.write(f"{time.time():.3f} END\n"); log.close()
    print(f"done, {time.time()-t0:.1f}s  log=/tmp/lightramp.log")

main()
