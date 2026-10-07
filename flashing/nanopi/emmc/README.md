# NanoPi R76S eMMC — recorder 1.65.0 (flashing/nanopi/emmc)

Put the StarCap recorder at **1.65.0**, tracking `main`, onto a NanoPi R76S.

> **2026-09-30: rebuilt on recorder 1.65.0** (`main` @ `5f5bc3b`), WB off (the 1.64.0+
> default; no `WB_ENABLE` line), updates on and pinned to `main`. Built OFFLINE from the
> previous image: the `main` tarball overlaid exactly as `updater.py` does, then the
> release's own `post_update.sh` run as root in a qemu-aarch64 chroot (only its board probe
> and LED probe pointed at fakes describing this board), so every native library was
> recompiled from 1.65.0 sources. Only `userdata.img` changed (every vendor partition is byte-identical to the previous set); the host kernel's overlay bookkeeping (77 `origin` + 22 `impure` xattrs and a `trusted.overlay.uuid`) was stripped from the upper so clones don't boot to a blinking cursor. `userdata.img` sha256 `d8f2933f…`. `e2fsck -fn` clean, ext4 features identical to the
> previous image. **Not yet boot-tested on hardware**: flash one board and check it records
> before trusting it fleet-wide. The previous image is kept at `~/rebuild-165/old-images/`.
> Build notes further down describe the PREVIOUS build and are kept as history.

**Two routes — same OS, same recorder, same partition set, pick by the hardware:**

| route | use when | how |
|---|---|---|
| **eMMC** (this folder) | the board has eMMC | `sudo ./flash_nanopi_emmc.sh` — needs maskrom + an A-to-A cable + a Linux x86-64 host |
| **SD card** (`../sd/`) | the board has **no eMMC**, or you'd rather avoid maskrom | `sudo ../sd/flash_sd.sh /dev/sdX` — no Rockchip tooling, 16 GB+ card |

The SD route is the same per-partition set assembled into one flat disk image, so both
produce identical boards. See `../sd/README.md` — it has one warning worth reading (a card
flashed with Etcher rather than `flash_sd.sh` is left unexpanded, giving the board only
~1.6 GB of recording space).

## eMMC route

```bash
sudo ./flash_nanopi_emmc.sh          # board in maskrom, A-to-A cable, no hub
```

No arguments needed — it auto-selects `nanopiIMAGE/rk3576-starcap-latest-20260930`.
Add `--dry-run` to print the write plan without touching the board.

Needs a Linux x86-64 host (`upgrade_tool` is closed-source x86-64) and **~12 GB free**.

### Verify

After writing, the script reads the eMMC back before it reboots the board: the GPT
header, every image up to 64 MiB in full, and 16 × 1 MiB spread across rootfs and
userdata (~90 MB, well under a minute). It ends with **VERIFY PASSED** or
**VERIFY FAILED**. On a failure the board is not rebooted. If kernel, boot, rootfs
and userdata fail while uboot/dtbo/resource pass, the eMMC is keeping only its
first 32 MiB: set the board aside, reflashing won't help. `--no-verify` skips the check.

### Several boards at once

Put each board, in maskrom, on **its own laptop port**, then flash one per terminal:

```bash
./flash_nanopi_emmc.sh --ports            # where each board is; flags boards sharing a link
sudo ./flash_nanopi_emmc.sh --port 1-1    # terminal 1
sudo ./flash_nanopi_emmc.sh --port 3-1    # terminal 2, ...
```

`--ports` prints a **SHARED LINK** warning when several boards sit behind one hub on one
laptop port. Those boards split a single 480 Mbit/s link, so flashing them together is about
as slow as flashing them one by one. Separate laptop ports don't share a link, even when
they're on the same USB controller. Without `--port`, the script refuses to run while more
than one board is plugged in. Each port keeps its own `upgrade_tool` log under
`/root/upgrade_tool/port-<P>/`.

---

## What this is

Built 2026-08-22 from `EMMCNANOPIFINAL` (the 2026-08-02 "FINAL FINAL" golden set).
The image is the **directory**, not one file: the R76S is a 9-partition GPT board, so
Rockchip ships one image per partition plus `parameter.txt` describing the layout.

`/` on this board is `overlay(lower=rootfs p8, upper=userdata p9)` and p8 holds zero
StarCap content — so **all customisation lives in `userdata.img`**, and that is the
only file that differs from the golden set. Everything else is vendor-stock and
hardlinked from it.

## What changed vs the golden set

| | golden (2026-08-02) | this build |
|---|---|---|
| recorder | 1.29.0 | **1.50.0** |
| source | overlay upper of board `pi-02fd094702` | clean `git archive origin/main` @ `8e9f754` |
| auto-updater | **OFF** (`20-no-autoupdate.conf`) | **ON**, pinned to `main` |
| WB kernel | stale prebuilt (`full-prebuilt-stale`) | native build baked in (`full`) |
| source board's `events.jsonl` | shipped (1.25 MB) | removed |

**1. Recorder 1.50.0, verified against the live tip of `main`.** All 113 tracked files
hash byte-identical to `origin/main` @ `8e9f754` ("rec: name the recording id after the
headset that shot it"). Exported with `git archive`, so it is a clean plain tree with no
`.git` — which is what lets the updater fast-forward it forever.

**2. The auto-updater is ON again, and this is the important one.** The golden image
disabled it to hold boards at 1.29.0, on the theory that 1.38.0's `stereocam/vision`
(YuNet face-blur + WB correction) caused a 60→20 fps regression. **That theory was
disproven on 2026-08-09**: board `pi-91c34bfc00` on 1.38.0 with `vision/` fully enabled
recorded ~47 fps (1607 frames / 274 dropped), while the *same board* on 1.29.0 and on
1.37.2 recorded **zero frames**. So the golden image was shipping boards that do not
record at all on R76S. Boards flashed from this build track `main` and get face blur and
WB correction.

**3. The WB kernel is the real native build, not the stale prebuilt.** `prebuilt/` on
`origin/main` does not match the committed sources — PR #23 (`fix/rk3576-full-wb-60fps`)
landed the chunk-claiming kernel in source and never regenerated the binary:

| file | `SOURCES.sha256` claims | actual on `main` |
|---|---|---|
| `gstwbcorrect.c` | `eeb59c87…` | `984fc247…` |
| `wb_kernel.c` | `c9915c8d…` | `4dd66b9f…` |

A board that cannot compile locally falls back to that old fixed-band binary. Measured
cost, same board / same camera / recording both times: **56.4 fps and 6.6% drops** on the
stale `.so` versus **60.10 fps and 0.00% drops** on the rebuilt one. So the natively-built
`vision/libgstwbcorrect.so`, `libwb.so`, `libimuwords.so` and `stereocam/libwbshim.so`
(aarch64, built on a noble R76S) are baked into this image and a flashed board runs the
`full` kernel from first boot.

⚠️ **This one is not durable across OTAs — see Known issues.**

**4. The source board's event log is gone.** The golden `userdata.img` shipped
`~/.config/starcap/events.jsonl` (1.25 MB of board `pi-02fd094702`'s history) plus its
`events_synced` marker, so every clone uploaded another board's events. Both removed.

`camera_cal.npz` is deliberately **kept** — it is a plausible default stereo calibration,
and per-camera WB calibration is fetched from the dashboard at runtime anyway.

## Still a template, not a twin

No `device_id`, no SSH host keys, empty `machine-id`. Each flashed board self-assigns all
three on first boot (`stereocam-firstboot.service` + systemd + the recorder), so you can
flash as many as you like from this one folder. `parameter.txt` marks the last partition
`userdata:grow` and the vendor rootfs ships `systemd-growfs@.service`, so the 6 GB
filesystem expands to fill whatever eMMC is really there.

To pre-assign an id instead, drop it in `~/.config/starcap/device_id` before first boot.

## Getting the board into maskrom (R76S specifics)

The R76S upgrade port is **USB-A — you need an A-to-A cable** (the M5 uses USB-C).
Cable **direct** to the laptop, no hub.

1. Remove the SD card and any other USB peripherals from the board
2. Hold the **Mask** key
3. Power the board on **from its own supply** — not bus power. DRAM training is the first
   heavy load and a browning-out board fails it.
4. Keep holding until the status LED has been on for **at least 3 s**, then release

`lsusb` should show `2207:350e` (Rockchip RK3576).

If download-boot times out (`471 index(1) err=-7`) — **re-seat or swap the A-to-A cable.**
That was the culprit; the board, the bus, the DDR blob and the tool version were all
cleared. Always power-cycle back into maskrom before a retry: a board with a blank eMMC
sits in maskrom by itself, so `Mode=Maskrom` does not prove the chip was freshly reset.

## First boot

Slow, 2–5 min: it grows the filesystem, regenerates SSH host keys, self-assigns a random
`pi-<hex>` device_id and starts the desktop. Login user is `pi`. Reach it on the LAN, or
plug ethernet into a laptop sharing `10.77.0.1/24`. Recorder web UI on `:8080`.

## Known issues

**The WB kernel fix is not durable across OTAs.** This image has no gstreamer dev headers,
so `post_update.sh` cannot recompile and, on the first OTA, overwrites the baked-in native
`.so` with the stale prebuilt — back to ~56 fps and 6.6% drops. Make it permanent per
board (~2 min, safe while recording):

```bash
sudo apt install libgstreamer-plugins-base1.0-dev     # purely additive, 0 upgraded
sudo APP_USER=pi bash ~/orangepi-recorder/stereocam/post_update.sh
sudo systemctl restart stereocam                      # only takes effect on restart
```

Check it took: `/api/status` → `wb_correct.kernel` should read `full`, not
`full-prebuilt-stale`. The real fleet fix is regenerating `prebuilt/` and committing it,
but `build_prebuilt.sh` **refuses to run on the R76S** — it has GStreamer 1.24 and the
fleet floor is 1.20.x (the Orange Pi 5 jammy boards), and a plugin built here is rejected
by every older board. It has to be built on the oldest fleet image.

**Measure fps only while a take is actually recording.** The stale binary's cost is
invisible at idle — an idle board reads a perfect 60.1 fps on it and only collapses once
the 3 HEVC encoders compete. Let a board settle past ~10 min before trusting any number.

**`stereocam-resizefs.service` fails with status 203 every boot** — it hardcodes
`/home/orangepi/orangepi-recorder/...` and the user here is `pi`. Harmless; systemd's
growfs does the resize. Inherited from the golden image.

## Provenance

- flasher + `assets/upgrade_tool_v2.44_for_linux/` — unchanged from `EMMCNANOPIFINAL`
- vendor-stock partitions (`rootfs/kernel/boot/uboot/resource/misc/dtbo/idbloader`) —
  unchanged, hardlinked from the golden set
- `userdata.img` — golden `userdata.img` with the four changes above
- rebuild tooling: `../rebuild/` (`build_nanopi_sd_image.sh`, `clone_nanopi_image.sh`);
  `../rebuild/strip_overlay_xattrs.py` is the overlay-xattr fix that made the golden set bootable

- built 2026-08-22 on this laptop from the docked NanoPi at 10.77.0.192
- `userdata.img` sha256: `920031f2ec468824e3807fdcb474ff8409c7dd6bd79c1ad5b340d01d43990b97` (the 2026-08-23
  rebuild with the gstreamer -dev fix baked in; `b370f28d…` was the 08-22 build, now deleted)
- every other file verified byte-identical by checksum against the golden set
