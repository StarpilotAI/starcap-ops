# Camera Firmware

Double-click **Camera Firmware** on the Desktop. Opens <http://127.0.0.1:8093>.

Answers one question about a docked DECXIN: **is it on the build that fails the
customer's frame-timing spec?**

| build | frame length | jitter | verdict |
|---|---|---|---|
| `dither-16626` | dithers 16626/16627 µs | 4.26 events/s | **FAILS p99** |
| `static-16640` | flat 16640 µs | 0.00 events/s | passes |

## The three buttons

- **Scan (instant)** — USB descriptors and v4l2 control ranges only. Milliseconds.
  Runs automatically when the page opens.
- **Measure & confirm** — captures ~11 s per camera and reads the frame length out
  of the sensor's own timestamps.
- **Measure & send to dashboard** — the same, then files it on `/cameras`.

Scan and measure are not the same claim. A scan describes the build a camera may
have **shipped** with; a measurement describes the one it is **running**. They have
agreed on every camera tested so far, but whether a reflash rewrites the
descriptors is still unsettled — so only measured gradings are sent by default,
and the page shows a warning row if the two ever disagree.

## Why there is no version number to read

These cameras report USB serial `01.00.000` on every unit of every build. There is
no version string anywhere — hence measuring behaviour instead.

## When a new firmware ships

Add it to `BUILDS` and move `LATEST` in the dashboard's
`dashboard/src/lib/firmware-core.ts`. Every already-graded camera re-sorts itself;
nothing needs re-grading and there is no migration.

Underlying tool: `../fwident.py` (`--measure`, `--push`, `--fleet`, `--json`).
