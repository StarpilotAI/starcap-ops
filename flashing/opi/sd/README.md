# Orange Pi 5 SD card — StarCap recorder 1.65.0 (flashing/opi/sd)

> **2026-09-30: rebuilt on recorder 1.65.0** (`main` @ `5f5bc3b`), WB off (the 1.64.0+
> default; no `WB_ENABLE` line), updates on and pinned to `main`. Built OFFLINE from the
> previous image: the `main` tarball overlaid exactly as `updater.py` does, then the
> release's own `post_update.sh` run as root in a qemu-aarch64 chroot (only its board probe
> and LED probe pointed at fakes describing this board), so every native library was
> recompiled from 1.65.0 sources. `e2fsck -fn` clean, ext4 features identical to the
> previous image. **Not yet boot-tested on hardware**: flash one board and check it records
> before trusting it fleet-wide. The previous image is kept at `~/rebuild-165/old-images/`.
> Build notes further down describe the PREVIOUS build and are kept as history.


A flashable SD image that boots an **Orange Pi 5** straight into the recorder, and a
flasher that refuses to hand you a card that will not work.

    orangepi5-starcap-1.65.0-20260930.img.gz    the image  (2.23 GB, 16 GiB raw)
    orangepi5-starcap-1.65.0-20260930.img.gz.sha256
    flash_opi_sd.sh                             write it + verify it

This is the **Orange Pi 5 / RK3588** counterpart to `../../nanopi/`, which is
NanoPi R76S / RK3576. **The two are not interchangeable** — different SoC, different kernel,
different bootloader. Flashing the wrong one gives a board that never comes up.

---

## Flash a card

```bash
./flash_opi_sd.sh              # finds the image + the card, confirms, writes, verifies
./flash_opi_sd.sh --all        # every card it found, written in parallel
./flash_opi_sd.sh --list       # show what it can see, touch nothing
./flash_opi_sd.sh --verify-only            # check card(s) already written
./flash_opi_sd.sh <image> /dev/sdb /dev/sdc   # spell it out
```

Also: `--dry-run`, `--yes` (skip the typed confirmation, for batches), `--max-gb N`.

It picks up the newest image sitting next to it, and finds cards itself. Detection is
narrow on purpose — removable, whole disk, `usb`/`mmc` transport, non-zero size, under a
512 GB ceiling, no system mount, not the disk holding `/`. **With more than one candidate
it refuses to choose**: "SD cards are always /dev/sd-something" is also true of every USB
stick and USB hard drive, most of which report `removable=1` just like a reader does. Pass
`--all` or name the one you want. Every candidate is printed with its size, model and
current contents first, and you type `erase`.

Writing several cards decompresses the image once to scratch, then writes them
concurrently with independent exit codes, so a batch tells you *which* card failed.

**Each card shows its own live progress** — one refreshed line per card, plus a running
line during the decompress so the quiet minute before any writing starts doesn't look
like a hang:

```
== writing to 2 cards in parallel ==
  sdb       30%     5.1 GB/17.2 GB    122.0 MB/s  ETA 2m
  sdd       30%     5.2 GB/17.2 GB    121.7 MB/s  ETA 2m
```

Two things about that display are deliberate:

* **Progress comes from `/sys/block/<dev>/stat`, not from `dd`.** `dd status=progress`
  counts bytes handed to the *page cache*, so a card slower than the cache shows nothing
  for a second, then sits in `conv=fsync` emitting no records while the card grinds through
  the backlog — the line reads 0% for a minute and then jumps to done, or shows 100% with
  minutes of flushing left. Device counters are what the card actually took. They are
  cumulative since the card appeared, so a baseline is captured before writing; without it
  a card flashed twice in one session reads "32 GB of 16 GB".
* **A card that falls below 5 MB/s for three samples is flagged `SLOW — suspect card`.**
  Rate is computed from successive samples, not `dd`'s running average, because that is the
  only way to see a card collapse. A bad one writes fast into its small SLC cache and then
  drops to raw flash speed; the average still looks fine long after. One card here measured
  0.90 MB/s — a 5-hour write, no kernel I/O errors, nothing else to notice it by. Bin such
  a card rather than wait for it: a recorder writes ~8.8 GB/hour.

After writing it verifies — and this is the part worth
having, because a card can look perfectly written and still be useless:

| check | why it matters |
|---|---|
| `RKNS` at sector 64 | the Rockchip IDBlock lives OUTSIDE any partition. A file-level copy of the rootfs produces a card that mounts fine on a laptop and never boots. |
| u-boot FIT at sector 16384 | same reason, second stage. |
| `opi_root` label, one MBR partition | the Orange Pi layout. The NanoPi flasher looks for GPT + `rootfs`/`userdata` and would reject this image as broken. |
| recorder present + VERSION | `/home/orangepi/orangepi-recorder` (user `orangepi`, no `root/` prefix — the NanoPi path is different). |
| dtb is an Orange Pi 5 one | wrong device tree = no boot at all. |
| `UPDATE_BRANCH=main` and `UPDATE_ENABLE=1`, both explicit | unset is not off (the updater defaults to on/main), but an implicit pin is one `tools/pin_to_branch.sh` away from `UPDATE_ENABLE=0`. |
| `resized` flag cleared | without this the card boots at 16 GB and the rest of a 128 GB card is never used — silently, with recording "working". |
| `stereocam-resizefs` + `stereocam-firstboot` enabled | checked with `-L`, not `-e`: systemd enable-links are absolute, so inside a mounted image they resolve against the HOST's `/etc` and a correctly-enabled unit reads as missing. |
| no `device_name`, no `device_id`, no ssh host keys, blank `machine-id` | otherwise every card is a twin of the master. See naming below. |
| no `.git` in the tree | a checkout pins `UPDATE_BRANCH` to whatever branch it is on; this master carried a retired branch that `post_update.sh` cannot self-heal from. |
| `python3-opencv` present | decides whether white balance works at all. See below. |
| gstreamer dev headers present | warn-only, but it is what stops WB falling back to the stale committed prebuilt after the first OTA. See trap 5. |

`--verify-only /dev/sdX` checks a card you already wrote. `--dry-run` shows what it would do.

---

## What happens on first boot

1. **Hostname** — `firstboot-init.sh` sets `starpilot-<last 6 of the SoC serial>`, every boot,
   whenever it does not match. Clones rename themselves; nothing to prepare.
2. **SSH host keys + machine-id** — regenerated, because the image ships without them.
3. **Rootfs grows** — `stereocam-resizefs.service` runs `growpart` + `resize2fs` to fill the
   actual card, then writes `/var/lib/stereocam/resized` so it only happens once.
4. **The recorder is already current.** This image ships **1.55.0**, which was the head of
   `main` (`32ccd25`) when it was built, so unlike earlier masters it does not have to reach
   GitHub to become useful. It still self-updates when `main` moves ahead.
5. **It comes up with NO NAME.** That is deliberate — see below.

---

## Naming: the board comes up nameless, on purpose

Name it with **`opm-name`** (or the dashboard's ✎). Nothing else.

`opm-name` reads the board's `cpu_id` over SSH and writes the name into the dashboard's
`starcap:names` hash. The recorder then pulls its own name down on its next heartbeat. One
write, both places — nothing is SSHed onto the board to set a name.

**Why the image ships nameless.** `starcap:names` is keyed by `cpu_id`, which is derived from
the SoC and survives a reflash. A fresh board's `cpu_id` is not in that table, so no name
comes down — and if the image carried a `device_name`, that stale name would just sit there
forever. Every board flashed from earlier images came up wearing the master's name, and the
dashboard showed *"named X locally but isn't in the names table — click ✎ to assign it"* on
all of them. That is why naming kept having to be redone by hand.

A useful consequence: reflashing a board that **already** has a names-table entry gives it its
name back automatically, because its `cpu_id` did not change.

Note `device_id` does **not** come back — it is regenerated per board. The dashboard keys
device rows, logs and the command queue by `device_id`, so a reflashed board appears as a new
row and the old one lingers as offline for up to 90 days (`DEVICE_RETAIN_S`). Cosmetic.

---

## White balance — the failure that hides

If a board reports **"not calibrated"** while the dashboard's cameras page shows the camera as
calibrated, the `.npz` is fine and only the **bake** is missing. `export_wbc.py` derives the
correction kernel with `cv2.GaussianBlur`, so with no `python3-opencv` the `.wbc` never bakes
and the recorder sits at `wb_correct.mode=uncalibrated`, `cal_error: No module named 'cv2'`.

**The card this image was built from had exactly this fault** — and it is worth knowing why it
hid so well: on that board every `libopencv-*` runtime library was already installed, and only
the Python binding `python3-opencv` was missing. `apt-get install python3-opencv` resolved to a
single 1.6 MB package. Nothing else looked wrong.

This image has it installed (opencv **4.5.4**, numpy 1.21.5), verified by importing `cv2` and
running the actual `cv2.GaussianBlur` call `export_wbc.py` depends on. Clones bake their own
`.wbc` for whatever camera is attached. If you ever see it anyway:

```bash
sudo apt-get install -y python3-opencv && sudo systemctl restart stereocam
```

---

## Traps

1. **A board with a working eMMC install ignores your card.** BootROM order is SPI → eMMC → SD,
   first valid bootloader wins. Not a bad card. `../rescue/force_sd_boot.sh` overwrites the eMMC
   bootloader to force SD boot. Boards with no eMMC populated (16 MB SPI-NOR only — check with
   `../rescue/check_onboard_storage.sh`) boot from SD with no help.
2. **An ex-boot card that looks blank still hijacks the BootROM.** Sector 64 belongs to no
   partition, so `wipefs`, `mkfs`, `parted mklabel` and Windows format all leave the idbloader
   intact; the BootROM reads it, commits, finds no OS, and hangs. Use `~/starcap-ops/storage/sd-format/format_sd_storage.sh`.
3. **`stereocam/update_token` ships inside this image** — a committed PAT with admin+push on the
   recorder repo. Anyone you hand the `.img` to gets fleet write access in cleartext. Decide
   about this before distributing, especially outside the team. Still unrotated.
4. **The image is 16 GiB, not the size of your card.** That is correct — see first-boot step 3.
   If `resized` was not cleared, the card stays at 16 GiB. The flasher checks it.
5. **The committed `prebuilt/` wbcorrect on `main` is stale against its own sources.** Checked
   at 1.55.0: `prebuilt/SOURCES.sha256` records hashes that do not match the `gstwbcorrect.c` /
   `wb_kernel.c` actually in the tree. A board that cannot compile therefore installs a
   *stale-flagged* plugin, and `post_update.sh` says so in the log. Any master must be built
   somewhere `gcc` **and** the gstreamer dev headers exist, so the native plugin is compiled
   from the real sources — this one was (`.wbcorrect.mode=full`, no `.wbcorrect.err`). Do not
   "fix" this by shipping the prebuilt; rerun `vision/build_prebuilt.sh` on an aarch64 box and
   commit the result.
6. **A leftover `stereocam.service.d/override.conf` silently beats the repo template.** The card
   this was built from carried one from Jun 30 setting `WIFI_WATCHDOG=0`. systemd applies
   drop-ins *after* the main unit, and `override.conf` sorts after `20-pin-main.conf`, so that
   `0` would have won on every clone — quietly undoing the fleet-wide fix that named
   `WIFI_WATCHDOG=1` in the template precisely so hand-set zeros could be cleared. The unit
   refresh in `post_update.sh` only harvests `Environment=` from the **main unit file**, so it
   cannot see, keep, or clear anything in a drop-in. It was removed here. **When building a
   master, list `stereocam.service.d/` and keep only `20-pin-main.conf`.**
7. **`~/.local/share/Trash` is where gigabytes hide.** This card had **3.7 GB of H.265 test
   recordings** in the desktop trash, plus 675 MB of Claude Code CLI binaries under
   `~/.local/share/claude`. `du -sh /home/orangepi/*` shows none of it, because the shell glob
   skips dotfiles — the directory reads as "4.7 GB, nothing visible in it". Uncompressed video
   also does not deflate, so the tell is a bad compression ratio: the first capture of this
   image came out **6.34 GB**; after removing that junk and re-zeroing free space the *same*
   rootfs compressed to **2.23 GB**. Survey with `du -sx ~/.[!.]*` as well as `~/*`.

---

## What was verified on this image, and what was not

Built 2026-08-30 from the SD card of **starcap16** (`device_id pi-cbe20a833c`, camera 125,
recorder 1.49.4 on the card), then brought to 1.55.0. The card itself was **never written
to** — it was unmounted, checked, and mounted read-only for the whole build, and its
`e2fsck` block/inode counts were identical before and after (204377/7799904 files,
3327359/31242235 blocks, exit 0). starcap16 keeps its name, `device_id` and host keys.

**Verified:**

| | |
|---|---|
| Recorder | **1.55.0** — head of `main` (`32ccd25`) |
| OS | Orange Pi 1.2.2 Jammy, Ubuntu 22.04.5, kernel 5.10.160-rockchip-rk3588 |
| Boot chain | `RKNS`@64 and `d00dfeed`@16384 byte-identical to a card known to boot; the whole 30 MiB pre-partition gap copied verbatim (only MBR bytes 440-511 differ, the table itself) |
| Filesystem | `e2fsck -f` **exit 0**, no warnings; ext4 feature set, UUID and label identical to the source card |
| White balance | opencv 4.5.4 imports and `cv2.GaussianBlur` runs, under aarch64 |
| Native WB plugin | `libgstwbcorrect.so`, `libwb.so`, `libimuwords.so`, `libwbshim.so` all **recompiled from 1.55.0 sources** by `post_update.sh`; `gst-inspect-1.0 wbcorrect` loads it; `mode=full`, no error file |
| Sanitizer invariants | **21/21 pass**, including everything in the flasher's own check-set |

**Not verified — read this before trusting it in the field:**

* **Nobody has flashed this image to a card and booted it.** It is verified at source only.
  The check that matters is that the new board comes up **nameless** and then takes its name
  from `opm-name`.
* **No fps measurement exists for this image.** The 60.148 fps / 0-dropped figure in the
  previous README was measured on a *different* master (1.52.3) and does not carry over. Grade
  a board built from this image with `~/starcap-ops/fleet/board-grading/measure_fps.py`.
* The native plugin was compiled and load-tested **under qemu emulation**, not on real RK3588
  silicon. The objects are genuine aarch64 ELF and the build is deterministic (deleting one and
  re-running the hook reproduced the same hash), but emulation is not a board test.
* `flash_opi_sd.sh --verify-only` **cannot be pointed at a loop device** — it requires a whole
  disk and rejects `TYPE=loop`. The 21 checks above were run by applying its `verify_dev`
  check-set directly to the mounted image. After you write a real card, run the flasher's own
  `--verify-only /dev/sdX` on it.

---

## Rebuilding this image — read this first

**Do not sanitize or shrink the master card.** The 2026-08-26 rebuild destroyed a master twice:
once by cutting power during `make-image.sh`'s free-space zeroing (an hour of silent `dd` on a
128 GB card, and the interrupted step skips the `rm /EMPTY; sync` that follows), and once by
running `resize2fs` on the card itself — relocating millions of blocks on metadata a previous
power cut had already bruised, which produced four block groups with bad bitmap checksums, a
gutted Python stdlib (`json`, `http`, `logging`, `ctypes`, `urllib` gone), a missing
`stereocam/vision/`, and a 6.6 GB image that would have flashed broken boards.

**The method below avoids both, and is what produced this image.** It never writes a single
byte to the card, so the card stays a working board and stays a fallback. It also skips the
hour of zeroing entirely, because a fresh filesystem on a sparse file is already zero.

**And never truncate `e2fsck` output.** `tail -2` shows the block-count summary and hides
`********** WARNING: Filesystem still has errors **********` on the line above it. Check the
**exit code** instead: 0 = clean, 1 = fixed, 2 = fixed+reboot, 4 = errors left unfixed.

### The offline method

On a laptop, with the master card in a reader, root available, and `qemu-user-static` installed:

```bash
# 0. Protect the source. Unmount it, check it, remount READ-ONLY.
udisksctl unmount -b /dev/sdX1
sudo e2fsck -fn /dev/sdX1 ; echo "exit=$?"        # must be 0
sudo mount -o ro,noatime /dev/sdX1 /mnt/opi-src

# 1. Geometry from the source, never guessed.
cat /sys/block/sdX/sdX1/start                      # 61440 on this layout
sudo sfdisk -d /dev/sdX
sudo dumpe2fs -h /dev/sdX1 | grep -E 'features|UUID|volume name|Inode size'

# 2. Skeleton: 16 GiB sparse, with the pre-partition bootloader gap copied verbatim.
truncate -s 16G new.img
sudo dd if=/dev/sdX of=new.img bs=512 count=61440 conv=notrunc   # MBR + RKNS@64 + FIT@16384
sudo sfdisk --no-reread --no-tell-kernel new.img <<'EOF'
label: dos
label-id: 0x8f8a65b4
unit: sectors
start=61440, size=33492992, type=83
EOF

# 3. Filesystem — SAME UUID and label, and PIN THE FEATURE SET. See the warning below.
LOOP=$(sudo losetup -fP --show new.img)
sudo mkfs.ext4 -F -b 4096 -I 256 -m 3 -L opi_root -U <source-uuid> \
  -O has_journal,ext_attr,resize_inode,dir_index,filetype,extent,64bit,flex_bg,sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum \
  -O ^orphan_file,^metadata_csum_seed,^fast_commit,^casefold,^ea_inode,^quota,^project ${LOOP}p1
sudo dumpe2fs -h ${LOOP}p1 | grep features     # diff this against the source before continuing

# 4. Populate, then bring the recorder to main (VERSION LAST, like updater.py does).
sudo mount ${LOOP}p1 /mnt/opi-img
sudo rsync -aHAX --numeric-ids --sparse \
  --exclude='/proc/*' --exclude='/sys/*' --exclude='/dev/*' --exclude='/run/*' --exclude='/tmp/*' \
  --exclude='/media/*' --exclude='/mnt/*' --exclude='/lost+found' \
  --exclude='/home/orangepi/orangepi-recorder/recordings/*' \
  --exclude='/home/orangepi/.cache/*' --exclude='/var/cache/apt/archives/*' \
  --exclude='/var/log/journal/*' --exclude='/var/lib/dhcp/*' \
  /mnt/opi-src/ /mnt/opi-img/

# 5. opencv + the native rebuild, inside an aarch64 chroot.
sudo cp /usr/bin/qemu-aarch64-static /mnt/opi-img/usr/bin/
for d in proc sys dev dev/pts; do sudo mount --bind /$d /mnt/opi-img/$d; done
sudo cp /etc/resolv.conf /mnt/opi-img/etc/resolv.conf
sudo chroot /mnt/opi-img apt-get install -y --no-install-recommends python3-opencv
sudo chroot /mnt/opi-img bash /home/orangepi/orangepi-recorder/stereocam/post_update.sh
#   -> expect "rebuilt libgstwbcorrect.so (full kernel)". Put a no-op `udevadm` early in
#      PATH first: post_update.sh's SD-automount section would otherwise poke the LAPTOP's
#      udev. (root-on-emmc.sh happens to fall through to "no" in a chroot, but do not rely
#      on that.) `systemctl` calls are chroot no-ops — verify enable-links yourself after.

# 6. Sanitize the IMAGE (offline equivalent of make-image.sh), then zero + capture.
#    device_id, device_name, events.jsonl, camera_cal.* / KEEP wbcal + wifi_networks.json
#    ssh_host_*, machine-id, .git, journal, Trash, .local/share/claude, override.conf,
#    /var/lib/stereocam/resized, and the qemu binary + PATH shim.
sudo umount /mnt/opi-img/dev/pts /mnt/opi-img/dev /mnt/opi-img/sys /mnt/opi-img/proc /mnt/opi-img
sudo e2fsck -f -y ${LOOP}p1 ; echo "exit=$?"       # must be 0
sudo losetup -d $LOOP
pigz -6 -p 14 -c new.img > orangepi5-starcap-<ver>-<date>.img.gz
sha256sum orangepi5-starcap-<ver>-<date>.img.gz > ...img.gz.sha256
```

### The ext4 trap this method introduces

Building the filesystem on a modern laptop instead of on the board means **your `mkfs.ext4` is
newer than the board's kernel.** e2fsprogs 1.47 enables `orphan_file` (and
`metadata_csum_seed`) by default; **kernel 5.10 does not support `orphan_file`**, and the Orange
Pi runs 5.10.160. Accept the defaults and you get an image that mounts perfectly on the laptop
and gives a board that will not boot — with a clean `e2fsck` and every other check passing.

So: read the source's feature list with `dumpe2fs -h`, pass it to `mkfs.ext4` explicitly with
the unwanted features negated, and **diff the two feature lines before you populate anything.**
The set that matches this fleet is:

```
has_journal ext_attr resize_inode dir_index filetype extent 64bit flex_bg
sparse_super large_file huge_file dir_nlink extra_isize metadata_csum
```

### If you do rebuild on the board instead

`make-image.sh` still works, and is the higher-fidelity path (real systemd, real udev, real
silicon). Two things will bite:

* It zeroes free space so the image compresses, which on a 128 GB card with 96 GB free is about
  an hour with **no output**, and powering off early skips it silently — the other sanitize
  steps still complete, so the card looks finished, then compresses at ~1.5:1 into an 80 GB
  file. Wait for `DONE — this card is now a clean golden master`, then `sudo poweroff`.
* It uses plain `sudo` with `|| true` on every step. Run over a non-interactive SSH on a board
  whose sudo needs a password and every step fails **silently**, giving a master that is not
  sanitized. Pre-authenticate in the same session first.

Then capture the card offline **before** modifying it, and do any shrinking on the image file
through a loop device — never on the card.
