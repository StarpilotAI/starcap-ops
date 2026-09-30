# Board grading

    ./board_fingerprint.sh <ip>              # read-only full hardware/software snapshot
    ./measure_fps.py <ip> <label> 90         # true capture fps from the frame counter, 90 s

`measure_fps.py` is the authority, NOT the dashboard's `capture_fps`. That one is a
rolling number that reads 60 about 64% of the time on a board whose true rate is 59.57.
Only the two-point frame-counter delta over at least 90 s separates a 60.13 board from
a 59.57 one.

Grading, measured 2026-08-25 on two R76S boards with the SAME physical camera:

    A grade   60.13 fps, dropped 0        (pi-9ab0796d2b)
    B grade   59.57 fps, dropped ~0.4/s   (pi-c64cacaaeb)

The bare-camera ceiling is 60.15 (v4l2-ctl, recorder stopped), so an A-grade board
loses nothing. Run 3 × 90 s before grading; single short runs are not trustworthy.
The difference is a real property of the board: 15 causes were eliminated,
including CPU load, thermal and DRAM bandwidth, so don't re-chase it.

`board_fingerprint.sh` logs in as `pi`/`12345` (NanoPi). Diff the output of a good
board against a slow one.
