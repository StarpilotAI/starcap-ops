# focusbarrel — live barrel-focus bench

Screw the lens barrel in or out and watch, live, whether you are getting sharper
or softer. No jig, no printed chart: point the camera at something with print on
it (a box works) and the tool finds the peak with you.

    cd ~/lensfocus && ./run.sh          # opens http://127.0.0.1:8150

## Using it

1. Point the camera at your target and get it roughly framed.
2. Click the small aiming view to drop the ROI on the printed part. Clicking the
   other half of the frame switches eyes as well as moving the box.
3. Screw the barrel. Watch the border and the arrow:

   | border | means |
   |---|---|
   | grey **SWEEPING** | it has not seen a peak yet — keep turning, both ways |
   | amber **OFF PEAK** | it knows where the peak is; ↗ keep going, ↘ turn back |
   | green **AT THE PEAK** | you are within a few % of the best point seen — stop |
   | red **BAD FRAMING** | too dark / blown out / nothing in the ROI to measure |
   | red **TOO NOISY TO CALL** | the reading jitters as much as the focus change — add light |

4. Land on green, then lock the barrel down.

Press **R** to reset the peak (do it whenever you move the camera or change the
light), **P** for focus peaking, **T** for a tone whose pitch rises as you
approach the peak — useful when you want to watch the lens instead of the screen.

## The thing to understand about the green light

There is no absolute "sharp enough" number for an arbitrary scene, so everything
here is relative to the best reading seen since the last reset. That has one
consequence you cannot design away: **the tool cannot know where the peak is
until you have turned past it.** It stays grey and says SWEEPING until the
readings have spanned at least 1.20×, and only then arms the green verdict. So
sweep through the peak and come back to it — do not creep up on it and trust the
first green you see.

Watch the **sweep span** in the Numbers panel. It is the honesty check:

- **2× or more** — a good target, the verdict means something.
- **under 1.2×** — the target is too weak and the tool says so. A general room
  scene has content at every distance, so something is always in focus and the
  number barely moves; measured on this camera a whole room swung only ~1.4×
  across the entire focus range, and not even monotonically. Fill the ROI with
  print on **one** flat face at **one** distance.

## Flicker: the thing that actually decides whether this works

Mains lighting ripples at **twice** the supply frequency, so 50 Hz mains flickers
at 100 Hz — a 10.0 ms period. A shutter that is not a whole multiple of that
catches a different slice of the ripple every frame, and the resulting contrast
wobble lands straight on the sharpness metric. The tool snaps the metered
exposure to whole cycles automatically (reading `power_line_frequency` to know
whether that is 10.0 or 8.3 ms).

Measured on this bench, same camera, same scene, same lens:

| shutter | metric jitter |
|---|---|
| 48.5 ms (2.42 cycles) | 11.5% |
| 40.0 ms (4.00 cycles) | 2.8% |
| in-app, after snapping | **0.13%** |

This is worth understanding because **it masquerades as a dark room**. The
symptoms — a jumpy number, a verdict that will not settle, readings that swing
with nothing moving — all say "add light", and adding light is not the fix.
Luma itself barely moves (1.4%); what moves is contrast, which is what the metric
is made of. A luma check will not catch it.

One consequence: flicker-safe shutters come in 10.0 ms steps, and 10.0 ms is the
only one that also holds 30 fps. In a dim room you are choosing between 15 fps
with a clean reading and 30 fps with a flickering one. Take the clean one.

## When it says TOO NOISY TO CALL

This is not the tool giving up on you, it is the tool refusing to make something
up. Below a certain light level the sharpness reading jitters frame to frame by
as much as a real focus change moves it, and at that point no peak can be
distinguished from luck.

Measured on this bench, same room, same camera, only the ROI brightness changing:

| ROI luma | frame-to-frame jitter | verdict |
|---|---|---|
| 116 | 0.6% | ±3% band, normal |
| 73 | 2–3% | usable |
| 46 | 4–12% | **no verdict** — and it invented a 1.5× "sweep" with nothing moving |

So if you see it, the fix is light on the target, not a different barrel
position. Everything else — the live view, peaking, the trace — keeps working;
only the green verdict is withheld.

Two related things the tool does so noise cannot fake a result: `best` and
`lowest` are taken from the **median of the last 2 seconds**, not from the
instantaneous value (a plain peak-hold latches onto the top of the noise and
leaves you chasing a number no barrel position can reach), and a peak you have
not come near in 45 s is flagged as possibly stale.

## What it pins, and why that matters more than the metric

Every auto knob moves the sharpness reading on its own — auto exposure re-meters
as you turn the barrel, and the ISP `sharpness` control is a digital edge
enhancer, so left free you are measuring the ISP, not the glass. On startup the
tool pins exposure, white balance, gain, gamma, backlight compensation and
sharpness, and leaves them pinned.

The exposure is metered **off the image**, by binary-searching the shutter until
the ROI reaches luma 120. It has to be, because on this camera exposure cannot be
read back: `exposure_time_absolute` returns whatever was last *written* to it and
never what auto is doing — with that control reading `1` throughout, the ROI sat
at luma 2.0 in Manual and 98.8 in Aperture Priority. Anything that "meters" by
letting auto settle and then reading the control is locking a stale register.

**Re-meter** re-runs that after a lighting change. **30 fps** caps the shutter to
hold the frame rate (see below).

## Frame rate

Measured on this camera: a shutter at or below **10.0 ms streams 30 fps**, and
anything from 12.0 ms up **halves it to 15**. Frame size barely changes across
that step (~310 KB either way), so this is the sensor changing its frame
interval, not the USB throughput ceiling that bites these cameras elsewhere.

Nothing breaks at 15 fps, it just feels half as responsive under your fingers.
If metering lands just past the knee the tool trims the shutter back to it and
says so; if the room is dark enough that trimming would underexpose, it keeps the
correct exposure and tells you to add light.

## Stereo

The buffer is `[data strip][right lens][left lens]`, and there is **one** v4l2
control set for both eyes — nothing here can be tuned per eye. Both eyes are
measured at the same ROI and tracked separately, which is what you want: one eye
is the barrel you are turning, the other sits still as a control.

Do not read the L/R ratio as a verdict on the glass. The eyes are offset by the
stereo baseline and so frame different content, which alone moves the ratio
further than a real defect does — in the aiming view above, one eye read luma 117
and the other 44 on the same frame.

## Options

    ./run.sh --device /dev/video2     # pick a camera (two identical ones are
                                      # indistinguishable by name — use the USB
                                      # port printed at startup)
    ./run.sh --host 0.0.0.0           # reach it from a phone on the LAN
    ./run.sh --no-lock                # leave the auto modes alone (readings drift)
    ./run.sh --max-exposure 100       # start in 30 fps mode
    ./run.sh --roi 0.4                # bigger ROI
    ./run.sh --wb-k 5000              # different white balance setpoint

## Swapping cameras, and second launches

**Hot-swapping is fine.** If the camera stops delivering frames the tool
re-discovers it and re-opens within a few seconds, then re-meters and clears the
peak — a different lens means every reading held against the old one is void. You
do not need to restart it to change cameras.

`/dev/videoN` numbering is *not* stable and is never assumed: the tool asks each
node whether it can actually capture, so a camera that comes back as `video1`
(with `video2` as its metadata node) is picked up automatically. With two cameras
attached, pick by the USB port printed at startup — identical cameras are
indistinguishable by name.

**Launching it twice** just opens the existing one in a browser tab; it will not
start a second copy or disturb the running one.

## If it will not start

`camera busy` — only one process can hold a `/dev/video*` node. camrig, camview
and the lenscheck bench all take it exclusively; the tool names the process that
has it. Close that first.

`port ... is in use by something that is not focusbarrel` — something else has
8150. Use `--port`.

Note that `run.sh` execs into python, so the process shows up as
`focusbarrel.py`, not `lensfocus` — match on that when hunting a stray instance.

Needs `v4l-utils` and `python3-opencv`, both already installed here.
