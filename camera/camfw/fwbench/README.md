# Firmware jitter bench — runbook

Proves whether a camera firmware build causes the frame-timing fault, with a
controlled experiment: the same cameras, the same rig, the same stimulus, and
only the firmware changed between runs.

## Before you start

1. **Point the camera at the laptop screen.** The screen IS the stimulus. The
   screen should fill a decent part of the frame. The tool measures the scene
   itself and will tell you if the ramp never reached the camera — a run that
   says that cannot support a PASS, so fix the aim and redo it.
2. **Nothing else on the USB controller.** A phone charging on the same
   controller drops the camera below 60 fps and the run will refuse to start.
3. **Don't touch the screen during a run.** The control phase needs the light
   held steady; the ramp phase drives it. A run is ~2 minutes.

## Check the aim first — 15 seconds, saves an afternoon

```sh
cd ~/starcap-ops/camera/camfw
./fwbench.py probe
```

The scene probe is a narrow band at the left edge of the left eye, so a camera
aimed slightly off flags every run. Do this once per camera, before its first
measurement. It refuses if the screen is not reaching the camera and tells you
which way to aim.

## The protocol

5 cameras × 2 firmware builds × 2 repeats = 20 runs, ~2 min each plus flashing.

For each camera, on each firmware:

```sh
cd ~/starcap-ops/camera/camfw
./fwbench.py run --camera 4.1 --fw 114 --reps 2
```

`--camera` is the unit number as the camera reports it (4.1, 169, …).
`--fw` is your label for the build (114, 3, …). Then reflash and repeat with the
other `--fw`. Order does not matter; the tool never assumes one.

Check progress at any time — it lists what is still missing:

```sh
./fwbench.py status
```

Write the report when the matrix is full:

```sh
./fwbench.py report -o ~/Desktop/firmware_jitter_report.md
```

## What it does NOT take on trust

- **The firmware label.** The build is identified from the camera's own measured
  frame interval, and the run is flagged if that disagrees with the `--fw` you
  typed. That is how a flash that silently did not take gets caught — before it
  costs you the afternoon.
- **That the stimulus landed.** It measures the scene's brightness swing in both
  phases. Checking the camera's exposure instead does not work: on the failing
  build, AE corrects with gain and the exposure never moves.
- **The timestamps.** ~80 duplicate stamps follow every device open. A duplicate
  is a zero interval, which would read as a frame arriving infinitely early and
  manufacture the very fault being measured. They are dropped and counted, and a
  run with too many is marked untrustworthy.

## Reading a result

```
family=dither-16626  control=0.10/s  ramp=3.45/s  p99=29880us FAIL
scene luma swing (slow): control 1.9  ramp 31.5
```

- `family` — the build, read back from the hardware. Two are known so far:
  `static-16640` (holds one frame length) and `dither-16626` (dithers between
  two, the signature of a frame-timing loop only this build runs).
- `control` vs `ramp` — jitter events per second with AE idle vs AE working. The
  fault is light-driven, so the gap between these two columns IS the finding.
- `p99` — 99th percentile frame interval against the customer line, ±10 ms of
  nominal. This is the number that decides pass or fail.
- `scene luma swing (slow)` — proof the ramp reached the camera, measured after a
  half-second moving average. The average matters: an exposure unaligned to the
  mains half-period aliases 100 Hz room ripple into per-frame brightness, which
  put a dead-static control screen at a raw range of 16 against the ramp's 34.
  Smoothed, the same pair reads 1.9 against 31.5. If ramp is not several times
  control, the run is not evidence of anything.

Raw per-run JSON lands in `results/`, one file per run, timestamped so a repeat
never overwrites an earlier measurement. Delete a file to discard a run.

## What the report will not do

- Count a run whose stimulus never landed, or whose timestamps were dirty, toward
  the summary or the conclusion. Those are listed separately instead.
- Claim the firmware caused anything unless the SAME physical camera was measured
  on both builds. Otherwise it says so, and tells you to flash one unit both ways.
- Call a build "failing" on a split result. If two reps of one label disagree it
  reports that and asks for a repeat, rather than putting the label on both sides
  of the sentence.
