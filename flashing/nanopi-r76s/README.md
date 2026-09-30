# NanoPi R76S flashing

The scripts that put the StarCap recorder onto a NanoPi R76S, plus the tools to
build and clone images. **The images are not in git** (12 GB each). The current
release bundle is `FLASH LATEST VERSION TRUST` (recorder 1.50.0), kept on the
SanDisk USB stick and at `~/Desktop/FLASH NANOPI`. Its scripts are copies of the
ones here.

| script | does |
|---|---|
| `flash_sd.sh /dev/sdX [image.zip]` | **SD route.** Streams the image straight out of the zip, grows the last partition to fill the card, and verifies. No Rockchip tooling needed. |
| `flash_sd.sh --expand-only /dev/sdX` | Only the grow + verify steps, for a card written with Etcher or `dd`. Without it the board gets ~1.6 GB of recording space. |
| `flash_nanopi_emmc.sh [image dir]` | **eMMC route.** Writes each partition over maskrom with `upgrade_tool` v2.44 (shipped in `assets/`). `--dry-run` prints the plan. |
| `build_nanopi_sd_image.sh` | Builds the flat SD `.img` from a per-partition image package. |
| `clone_nanopi_image.sh` | Images a running R76S (whole eMMC) over ssh, optionally sanitising its identity. |
| `strip_overlay_xattrs.py` | **Required when building a golden userdata.** Strips `trusted.overlay.origin`/`impure` xattrs; without it clones boot to a blinking cursor (the rsync `-X` bug). |

`./install.sh` installs the packages these need (gdisk, parted, e2fsprogs, unzip,
zstd, simg2img, usbutils, sshpass).

## Why there is no single .img for eMMC

The R76S is a 9-partition GPT board, so Rockchip ships an image as one file per
partition (`rootfs.img`, `userdata.img`, …) plus a `parameter.txt` describing the
layout. `flash_nanopi_emmc.sh` parses that and writes each partition in turn.

`/` is `overlay(lower=rootfs p8, upper=userdata p9)` and p8 is vendor-stock, so
**all StarCap customisation lives in `userdata.img`**. It is the only file a
golden set changes. `parameter.txt` marks it `userdata:grow`, which `upgrade_tool`
honours on eMMC; `dd` does not, which is why the SD route needs the expand step.

## Getting the board into maskrom

The R76S upgrade port is **USB-A: you need an A-to-A cable**. Plug it straight
into the laptop, with no hub in between.

1. Remove the SD card and any other USB peripherals from the board.
2. Hold the **Mask** key.
3. Power the board from **its own supply**, not bus power. DRAM training is the
   first heavy load, and a board that browns out fails it.
4. Keep holding until the status LED has been on for **at least 3 s**, then release.

`lsusb` should show `2207:350e`. Always power-cycle back into maskrom before a
retry: a board with a blank eMMC sits in maskrom by itself, so `Mode=Maskrom`
does not prove the chip was freshly reset.

## If maskrom fails with `471 index(1) err=-7`

It is intermittent and **almost always the A-to-A cable**. The same laptop and
USB bus later flashed fine. Re-seat or replace the cable before blaming the
board, the laptop or the image.

Tell from `dmesg` whether download-boot worked, without touching the board:

    bcdDevice= 1.00, Mfr=0, Product=0     <- bare maskrom
    device firmware changed               <- loader accepted
    bcdDevice= 2.23, Manufacturer: Rockchip, Product: USB download gadget

A `device descriptor read/64, error -71` between those is the handoff and is
harmless. **Never start a second `upgrade_tool` while a flash is running.**

Already ruled out, so don't re-test: the DDR blob (the package's loader and
Rockchip's generic v1.12.108), the USB port, stale maskrom state, board power,
and the tool version (v2.44 behaves the same as v2.30).

## A clean flash can still be a lie

Some R76S eMMCs ack every write but keep only the first 32 MiB. After flashing a
board that then misbehaves, read back a partition's magic bytes rather than
reflashing again.
