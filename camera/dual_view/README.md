# dual_view: DECXIN multi-camera live viewer + recorder

From Youssef Mahran (Slack DM, 2026-10-08). One window: the stereo **DECXIN 168** (4000x1200) across the top, every
mono DECXIN below it side by side. `r` records ALL cameras at full res / 60 fps with low CPU; `q`/Esc quits.

    ./install.sh                          # apt: ffmpeg v4l-utils python3-opencv python3-numpy
    python3 dual_view.py                  # every connected DECXIN
    CAM_SKIP_PORTS=2-9 python3 dual_view.py   # leave a USB port out (e.g. a camera that keeps resetting)

Output: `~/Videos/<stamp>_<camera>.avi` (the camera's own MJPEG, `-c copy`, no re-encode) plus `<stamp>_<camera>.csv`
with CLOCK_MONOTONIC ns per frame to line the videos up. It prints `cameras: ...` at start, then each camera's fps every
5 s; expect ~60. CPU ~25-40% of one core for 2-3 cameras. Recording burns disk fast (measured ~37 MB/s for the 168 + ~12 MB/s per mono, ~220 GB/hour for all three).

Cameras are found by USB name (`DECXIN Camera 168` 4000x1200, `DECXIN Camera M` 2080x1200, `DCXIN Camera` 1920x1200)
and told apart by **USB port**: two "DECXIN Camera M" units share one name AND one serial number.

## Plugging in: USB 3 only

Every camera must enumerate at **5000 Mbit/s**; over USB 2 the 168 has no 4000x1200 mode and the mono only does 30 fps.

    for d in /sys/bus/usb/devices/*; do [ -f $d/product ] && grep -qiE "decxin|dcxin" $d/product && echo "$(cat $d/product) $(basename $d) $(cat $d/speed)"; done

- Use the **motherboard's** USB 3 ports; an add-on controller gave `xhci_hcd ... not part of TD` and no frames.
- Three cameras on PC port power made one reset in a loop (disconnect every 1-2 s) and stalled the others; use a
  **powered USB 3 hub** for a 3rd camera. USB-C AV adapters/docks usually only have a USB 2 hub inside.
- Enumerates but no frames: replug, or another port.

Per-camera stream test (W = 4000 for the 168, 2080 for Camera M, 1920 for DCXIN Camera), should print ~60 fps:

    v4l2-ctl -d /dev/videoN --set-fmt-video=width=W,height=1200,pixelformat=MJPG --set-parm=60 --stream-mmap --stream-count=180 --stream-to=/dev/null

## Exposure: auto on every camera

    for v in /sys/class/video4linux/video*; do [ "$(cat $v/index)" = 0 ] && v4l2-ctl -d /dev/$(basename $v) -c auto_exposure=3; done

- Mono stuck at ~50 fps in auto = 50 Hz anti-flicker holding exposure at 20 ms:
  `v4l2-ctl -d /dev/videoN -c power_line_frequency=0` (cost: possible banding under 50 Hz lights).
- For **SLAM** recordings the 168 must be in MANUAL exposure (auto desyncs camera and IMU). Auto is fine here.

## Verified on this laptop (2026-10-08)

168 on 2-1 + Camera M on 2-2 + Camera M on 8-1, all on laptop port power: 6 s record = 361/361/362 frames, 60.1 fps
each by the CSV stamps, max gap 22-33 ms, no USB resets. ffmpeg prints harmless `unable to decode APP fields`.
