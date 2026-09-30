# SD card route — NanoPi R76S, recorder 1.50.0

For a NanoPi with **no eMMC** (or when you don't want to deal with maskrom). Same OS,
same recorder 1.50.0, same self-assigning identity as the eMMC route in the folder above
— it is literally the same partition set, just materialised as one flat disk image
instead of pushed partition-by-partition over USB.

**Needs a 16 GB or larger card** (the image is 11.64 GiB; anything under ~13 GB won't fit).

## The easy way (Linux) — use this

```bash
sudo ./flash_sd.sh /dev/sdX
```

Run it with no arguments to list removable disks first. It is the only script you need:

1. refuses anything that isn't a removable whole disk
2. unmounts the card and destroys **both** GPTs (primary + backup) so no stale table survives
3. streams the image straight out of the `.zip` — the 11.6 GB never has to exist on your disk
4. **grows the last partition to fill the card** and resizes its filesystem
5. verifies what actually landed

Step 4 is the one that matters — see the warning below.

## balenaEtcher / Raspberry Pi Imager (macOS, Windows)

Point it at `nanopi-r76s-starcap-1.50.0-sd-20260822.img.zip` — it reads the zip directly
(single file inside, which is why it's packed that way).

⚠️ **But then the card is NOT expanded, and the board gets only ~1.6 GB of recording
space.** `parameter.txt` marks the last partition `userdata:grow`, which `upgrade_tool`
honours when it flashes eMMC — but nothing does that for a written card. The GPT in this
image says `userdata` ends where its payload ends, and `systemd-growfs` only grows a
*filesystem* to fill its *partition*, never the partition itself. The rest of the card is
left unallocated on purpose.

So after Etcher (Windows/macOS), plug the card into any Linux box and run:

    sudo ./flash_sd.sh --expand-only /dev/sdX

It grows the last partition to fill the card and runs the same verify step, without
rewriting the image. (Older notes mention an `expand_sd.sh`: it never existed, this is it.)

## The trap that wastes an afternoon

**The BootROM checks SPI → eMMC → SD and boots the FIRST valid bootloader it finds.**
A board that already has a working eMMC install will **ignore the card entirely** — it is
not a bad card or a bad image. That is fine for the case this folder exists for (a board
with no eMMC), but if you're testing on a board that *does* have one, use the eMMC route
(`../emmc/`) instead. `../../opi/rescue/force_sd_boot.sh` is RK3588 (Orange Pi) only and
does NOT work on the R76S.

## First boot

Slow, 2–5 min: grows the filesystem, regenerates SSH host keys, self-assigns a random
`pi-<hex>` device_id, starts the desktop. User is `pi`. Recorder web UI on `:8080`.

Note that on SD boot the recorder writes recordings into the overlay upper — i.e. onto the
card itself — so an unexpanded card fills almost immediately.

## Same known issue as the eMMC route

The baked-in native WB kernel does **not** survive the first OTA (no gstreamer dev headers
in the image). Fix per board, from `../emmc/README.md`:

```bash
sudo apt install libgstreamer-plugins-base1.0-dev
sudo APP_USER=pi bash ~/orangepi-recorder/stereocam/post_update.sh
sudo systemctl restart stereocam
```

## Provenance

Built 2026-08-22 by `../rebuild/build_nanopi_sd_image.sh` from
`../emmc/nanopiIMAGE/rk3576-starcap-latest-20260822` — the same verified image set the eMMC
route uses. Layout parsed from that package's `parameter.txt`, never hardcoded.

Verified after building: `RKNS` bootloader magic at sector 64, 9-partition GPT matching
`parameter.txt`, the `userdata` partition **byte-identical** to the verified
`userdata.img` (sha256 `b370f28d…9e735f`, the 08-22 build; the zip was rebuilt 08-23 from `920031f2…`), and the zip's compressed data checked with
`unzip -t`.

Heads-up if you script this: `build_nanopi_sd_image.sh` **exits 1 even when it succeeds**
— its `EXIT` trap ends on a `[ "$PARTIAL" -eq 1 ]` test that is false on the success path,
and bash propagates that as the exit status. Check for the output file, not `$?`.
