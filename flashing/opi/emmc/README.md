# eMMC boot for the Orange Pi 5B — StarCap recorder

Boots the recorder from the board's onboard eMMC, freeing the SD slot to be pure
recording storage.

    opi5b-emmc-starcap-1.55.0-20260913c.img         THE GOLDEN IMAGE (10.7 GB)
    opi5b-emmc-starcap-1.55.0-20260913c.img.sha256
    flash_emmc.sh                                  probe / write / read back
    rk3588_spl_loader.bin                          RK3588 maskrom DDR loader
    sdcard-boot.scr / .cmd                         SD->eMMC boot handoff (see #8)
    install_sd_boot_handoff.sh                     puts it on a recording card

## Flash it

```bash
./flash_emmc.sh            # picks the newest .img, type `flash` to confirm
```

Board into maskrom first: unplug DC **and** USB-C, wait 10 s, hold MaskRom, apply
DC while holding, hold 2-3 s, release, plug USB-C into the laptop. If the board is
already running, `adb reboot loader` works instead — no button.

Then **unplug the USB-C from the laptop** (it back-powers the board), pull DC,
wait 10 s, SD slot empty, DC back on.

Login `orangepi` / `orangepi`. SSH is on. Web UI on :8080.

---

## What is in the golden image

Built by installing the recorder on a live board over SSH — NOT assembled offline —
then capturing the eMMC. Every claim below was observed working on hardware.

| | |
|---|---|
| Base | ubuntu-rockchip 22.04.5 **desktop** for orangepi-5b, kernel 5.10.0-1012-rockchip |
| Recorder | 1.55.0, **`stereocam.service`** enabled (the fleet daemon — dashboard heartbeat, device_id, LED, recording) |
| Update pinning | `UPDATE_BRANCH=main` + `UPDATE_ENABLE=1`, both explicit, in `stereocam.service.d/20-pin-main.conf` |
| WB plugin | `libgstwbcorrect.so` **compiled from source on real RK3588 silicon**; `gst-inspect-1.0 wbcorrect` loads it |
| opencv | installed natively by `install.sh` |
| `librga-dev` | present — without it the WB plugin silently falls back to the stale prebuilt (see below) |
| SD automount | rule rewritten to be `mmcblk`-number agnostic (see below) |
| Hostname | `firstboot-init.service` → `starpilot-<last 6 of SoC serial>` |
| Grow-to-fill | `starcap-growroot.service` → whole eMMC on first boot (see below) |
| Identity | no ssh host keys, blank machine-id, no `device_id`/`device_name` |
| Filesystem | `e2fsck -f` **exit 0** |

**Boot-tested on the docked board 2026-09-13**: comes up on eMMC
(`/dev/mmcblk0p1`, `mmcblk0boot0` present), grows to 29 GB, regenerates host keys,
names itself `starpilot-a20fdf`, recorder active, web UI reachable from the LAN.

**Validated on hardware 2026-09-13** with a DECXIN camera attached:
`capture_fps 60.1` at 4000x1200, camera identified as `63.1`, its white-balance
calibration pulled from the dashboard automatically, `device_id pi-f79604d91a`
self-assigned, board visible on the dashboard, SD card automounted at
`/media/sdcard` (117 GB) and the panel LED moved `fault_storage` -> `idle`.

**Still unproven:** an actual recorded take end-to-end (start/stop, file written,
upload), the IMU (none attached — status showed `/dev/ttyS0` absent), and genlock
under load. `capture_fps` is the camera delivering frames, not a graded take.

---

## Eight fixes / findings in this image that are NOT in the recorder repo

These will be lost on a rebuild (and #2 / #6 on the next OTA) unless pushed upstream.

### 1. `librga-dev` — the stale-prebuilt trap, root-caused

`post_update.sh` reports

    cannot build wbcorrect here: gstreamer-video-1.0 dev headers absent
    *** installed PREBUILT wbcorrect (full) but it is STALE

**even with `libgstreamer-plugins-base1.0-dev` installed.** The Rockchip-patched
`gstreamer-video-1.0.pc` carries `Requires.private: librga`, and `librga2` (the
runtime package) ships no `librga.pc` — only `librga-dev` does. So
`pkg-config --exists gstreamer-video-1.0` fails, the build is skipped, and the
board silently runs a stale plugin. One missing `.pc` file.

Add `librga-dev` to `install.sh`'s package list.

### 2. The SD-automount udev rule is board-specific and wrong here

The repo template keys on `KERNEL=="mmcblk0p1"` as "the SD slot". True on the
Orange Pi 5 and the NanoPi. **False on this 5B under the ubuntu-rockchip kernel,
where the numbering is reversed:**

    mmcblk0 = onboard eMMC   (has mmcblk0boot0/boot1)
    mmcblk1 = the SD card

The stock rule points the automount at the eMMC *root* partition and never mounts
the card — and `app.py`'s `storage_ready()` then refuses to record. Replaced with a
number-agnostic rule matching any `mmcblk[0-9]p[0-9]`, letting the automount
script's existing root-device guard exclude the eMMC. `root-on-emmc.sh` already
gets this right (it keys off the `boot0` marker); the udev rule should too.

### 3. `starcap-growroot.service` — a golden image cannot grow itself

The base ships `cloud-initramfs-growroot`, which normally grows the rootfs at boot.
**It refuses on a golden image**, because a golden image is GPT-inconsistent by
construction: it is smaller than the target eMMC, so its backup GPT header sits at
the end of the *image*, not the end of the *disk*. The kernel says so:

    GPT:Primary header thinks Alt. header is not at the end of the disk.
    GPT:20834815 != 61071359

growroot bails and the clone sits at the image size forever — 9.7 GB of a 29 GB
chip, silently, with everything else looking fine. **Observed on the first boot of
this very image.**

`starcap-growroot.sh` runs `sgdisk -e` (relocate the backup header to the true end
of the disk) before `growpart` + `resize2fs`, gated on `/var/lib/starcap/grown`.
Ordered `Before=stereocam.service` so recording never starts on a
filesystem about to be resized.

### 4. `firstboot-init.service` was never installed by `install.sh`

The fleet's naming script lives in the repo at `stereocam/net/firstboot-init.sh`
but nothing installs it — `install.sh` creates `orangepi-recorder.service` and
stops there, so every clone came up as `localhost.localdomain`. Installed to
`/usr/local/bin/` with its unit enabled. It also regenerates ssh host keys and
machine-id, which this base otherwise never does: Ubuntu generates host keys in the
openssh-server *postinst*, not at boot, and `ssh.service`'s
`ExecStartPre=/usr/sbin/sshd -t` only VALIDATES config — so a sanitised image with
no host keys would boot with sshd refusing to start and **no SSH at all**.

### 5. `install.sh` starts the WRONG service — the recorder looks installed but is half dead

`install.sh`'s "start-on-boot service" step installs `webui/orangepi-recorder.service`,
which runs `webui/app.py` — **just the local web UI**. The fleet's actual daemon is
`stereocam/stereocam.service` running `stereocam/app.py`, and that is what does the
dashboard heartbeat, self-assigns `device_id`, drives the panel LED and controls
recording. The repo ships it; nothing installs it.

Symptom: the board boots, the web UI answers on :8080, `install.sh` reports success
— and the board **never appears on the dashboard**, has no `device_id`, and its LED
stays dark. Observed exactly that on 2026-09-13.

The two also collide: `stereocam.service` serves its own HTTP on 8080, so
`orangepi-recorder.service` must be disabled, not just ignored. It additionally
carries `Conflicts=gdm.service` and stops the display manager on start.

Fixed in this image: `stereocam.service` installed + enabled with the fleet drop-in
(`UPDATE_BRANCH=main`, `UPDATE_ENABLE=1` — unset is NOT off, the updater defaults to
on/main, but an implicit pin is one `tools/pin_to_branch.sh` away from being
disabled), and `orangepi-recorder.service` disabled.


### 6. The USB-C port role-swaps and takes the camera (and the board) down

**This is the "runs fine for hours, then static red" fault.** Root-caused
2026-09-13 on `starpilot-a20fdf` after ~22 h uptime. The entire tail of the
journal is:

    16:14:15 kernel: xhci-hcd xhci-hcd.11.auto: remove, state 1
    16:14:15 kernel: usb usb8: USB disconnect, device number 1
    16:14:15 kernel: usb 8-1: USB disconnect, device number 2
    <journal ends — no shutdown sequence at all>

The camera enumerates on the **dual-role** controller:

    input: DECXIN Camera 4.1 as /devices/platform/usbdrd3_0/fc000000.usb/xhci-hcd.11.auto/usb8/8-1/...

and that port ships as:

    /sys/class/typec/port0/port_type      = [dual] source sink
    /sys/class/typec/port0/preferred_role = sink
    /sys/class/typec/port0-partner/type                        = not_ufp
    /sys/class/typec/port0-partner/supports_usb_power_delivery = no

So the Type-C Port Manager is set to `dual`, *prefers* to become a sink, and is
negotiating against a partner that reports `not_ufp` and speaks no PD — it never
settles, and retries. A role swap calls `dwc3_host_exit()`, which removes
`xhci-hcd.11.auto` **out from under a streaming UVC device**. The kernel wedges
in that teardown, and — critically — the wedge survives the watchdog:

    systemd[1]: Using hardware watchdog 'Synopsys DesignWare Watchdog', device /dev/watchdog
    systemd[1]: Set hardware watchdog to 44s
    [event] unclean_boot: board reset without a clean shutdown

The 44 s hardware watchdog is armed and still does not bring the board back — a
warm reset does not re-init the USB PHYs or the boot media, so the board sits at
**static red until DC is physically pulled**. That is the whole symptom, exactly.

Fixed here by pinning the port to source (host-only) so no swap can ever happen,
via `/etc/udev/rules.d/60-starcap-typec-host.rules`:

    ACTION=="add", SUBSYSTEM=="typec", KERNEL=="port[0-9]", ATTR{port_type}="source"

plus `starcap-typec-host.service` (oneshot, `Before=stereocam.service`) as a
belt-and-braces pass for a port that already exists when rules are applied.

**Verified on hardware before baking:** writing `source` moves the port to
`dual [source] sink`, `data_role` stays `[host]`, dwc3 `mode` stays `host`, and
the camera does **not** re-enumerate — the `/dev/v4l/by-path/` symlinks keep
their original timestamps. Forcing the port back to `dual` and re-triggering
udev re-pins it to `source`, so the rule is what does the work, not the write.

The durable version of this is `dr_mode = "host"` on `usb@fc000000` in the
device tree, which stops dwc3 registering a role switch at all. Not done here:
DT surgery on this board has already cost one unbootable image, and the sysfs
pin is verified and reversible. If it is ever done, note it also removes
`adb reboot loader` (that needs USB device mode) — the MaskRom button still works.

**Note the camera is on the dual-role port by long-standing habit, not by
mistake** — but `install.sh` expects it on the two fixed host controllers:

    CAM1=/dev/v4l/by-path/platform-fc800000.usb-usb-0:1:1.0-video-index0
    CAM2=/dev/v4l/by-path/platform-fc880000.usb-usb-0:1:1.0-video-index0

Both of those are empty on this board, so the recorder is falling through to
`/dev/video0`. Moving the camera to a USB-A port would also sidestep the fault
entirely and match the fleet — worth doing, independently of this fix.


### 7. The buzzer: polarity is ACTIVE-HIGH here, and `app.py`'s comment is not

Proved on hardware 2026-09-13 by trying the wrong one. `buzzer.py` sets the idle
level from one flag:

    self._silent = 1 if active_low else 0     # the "off" level

`app.py:1928` claims this panel is wired `+3.3V -> buzzer -> pin 15`, so HIGH
would be silent, and recommends `BUZZER_ACTIVE_LOW=1`. **On this board that is
backwards.** Setting it made the buzzer scream continuously; clearing it went
silent again. So on the deployed panel:

    pin 15 = GPIO0_D4 = gpiochip0 line 28 :  LOW = silent, HIGH = buzz

The shipped default (`BUZZER_ACTIVE_LOW` unset = 0) is therefore CORRECT and must
be left alone. `buzz.cmd` had it right all along — "GPIO0_D4 = header pin 15 = the
buzzer, active-high".

**Do not "fix" this by setting `BUZZER_ACTIVE_LOW=1`.** The READMEs in
`stereocam/` and `webui/` both recommend it; both describe a different wiring
variant than the fleet actually ships. That trap cost a live board a screaming
buzzer.

**The long boot buzz is a separate, still-open problem.** Pin 15 pulls HIGH at
boot and nothing can hold it low (`app.py:1929`), and HIGH = buzz here — so the
buzzer sounds from power-on until `stereocam` starts and parks the pin LOW,
roughly 10-15 s. `/boot.scr` exists to cut that short by driving line 28 low from
U-Boot (~2-3 s in). Its register math is correct for RK3588:

    GPIO0 base 0xfd8a0000; line 28 is in the HIGH half, so bit 12 of *_H,
    with the write-enable mask in bits 16-31
    DDR_H (0x0c) = 0x10001000   -> direction = output
    DR_H  (0x04) = 0x10000000   -> data = 0 (low = silent)

but it is **unconfirmed that U-Boot ever executes it** — that needs someone
watching HDMI at boot for `Found U-Boot script /boot.scr` and
`StarCap: buzzer GPIO0_D4 driven low`. If those lines do not appear, the script
is not being scanned and the boot buzz cannot be fixed this way.


### 8. A card in the SD slot stops the board booting — FIXED by repacking U-Boot

**CONFIRMED ON HARDWARE 2026-09-13:** with `uboot-emmc-first.itb` written to
sector 16384, the docked board boots normally **with the SD card inserted**.
The repacked FIT is accepted by the SPL (ATF + fdt load, board comes up), so the
hash recomputation is correct and no signature is enforced — both as analysed.

Reproduced repeatedly on the docked board: **card in at power-on -> static red;
pull the card, power on -> boots fine.** It is not the image, not the eMMC, and
not the calibration of anything. It is U-Boot's scan order:

    boot_targets = usb0 mmc1 nvme0 nvme1 scsi0 mmc0 mtd2 mtd1 mtd0 pxe dhcp
    mmc0 = /mmc@fe2e0000   bus-width 8, non-removable, mmc-hs400   = eMMC
    mmc1 = /mmc@fe2c0000   bus-width 4, cd-gpios                   = SD slot

(Alias mapping read out of the U-Boot FDT itself, not assumed — the two are
reversed relative to Linux, where the eMMC is mmcblk0 and the SD is mmcblk1.)

So U-Boot works over USB and then a 119 GB recording card before it ever looks at
the eMMC the OS lives on.

**THE FIX, and why the first reading of this was wrong.**

The original note here said the order could not be changed. That was half right:

1. U-Boot's environment backend is **`ENV_IS_NOWHERE`** — the only env driver name
   in the binary. `saveenv` exists as a command but has nowhere to persist, so
   `setenv boot_targets ...` genuinely cannot be made to stick. That half stands.
2. The FIT *looked* signed — `/configurations/conf/signature` with
   `algo = sha256,rsa2048`. **It is not signed.** That node carries only `algo`,
   `key-name-hint = "dev"` and `sign-images`; there is **no `value` property**, so
   it is an unsigned template left in the `.its` and nothing verifies a signature.
   Only the per-image `sha256` hashes are real — and those can be recomputed.

So the FIT was repacked. `uboot-emmc-first.itb` is the stock FIT with exactly one
string reordered inside the `uboot` image:

    from  boot_targets=usb0 mmc1 nvme0 nvme1 scsi0 mmc0 mtd2 mtd1 mtd0 pxe dhcp
    to    boot_targets=mmc0 usb0 mmc1 nvme0 nvme1 scsi0 mtd2 mtd1 mtd0 pxe dhcp

Same length, same token set, 60 bytes changed in total (32 for the string, 32 for
the recomputed `uboot` sha256) and nothing else. All five image hashes verify
after the edit; `atf-1/2/3` and `fdt` are untouched.

Mechanics worth keeping: the FIT header is only 1971 bytes and the image data is
**external**, based at **2048** (512-byte aligned, not 4). That base was confirmed
by reproducing the stored `uboot` sha256 exactly before anything was modified —
do that check first if this is ever redone, because guessing the base silently
produces a garbage hash.

Baked into the golden image, and deployable on its own without a 10.7 GB reflash:

    ./flash_uboot_only.sh              # write the eMMC-first U-Boot (sector 16384)
    ./flash_uboot_only.sh --rollback   # byte-exact stock U-Boot back

`uboot-ORIGINAL-rollback.itb` is the untouched original, kept beside it.

**Do NOT "fix" this by swapping in another bootloader.** All four blobs in
the old OPi5 eMMC builder's `bootloaders/` scan the SD before the eMMC, the fleet's own
`starcap-fleet-original.bin` included — its `boot_targets=mmc0 mmc1 ...` *looks*
like eMMC-first, but that bootloader's aliases are REVERSED (`mmc0 = fe2c0000` =
the SD). The addresses are fixed silicon — `fe2e0000` is the 8-bit non-removable
eMMC, `fe2c0000` the 4-bit cd-gpios SD — so always read the aliases out of the
bootloader you are actually holding.

**What was tried first and did not work**, so nobody repeats it:

A `boot.scr` on the SD card. U-Boot reaches the SD second and runs
`scan_dev_for_scripts`, so a script there could redirect the scan: U-Boot runs
`scan_dev_for_scripts` on each device (`boot_scripts = boot.scr.uimg boot.scr`
under `boot_prefixes = / /boot/`) and reaches the SD *second* — so a script there
runs early, and only has to redirect the scan:

    echo "StarCap: SD card present - handing boot to eMMC (mmc0)"
    setenv boot_targets mmc0
    run distro_bootcmd

Shipped here as `sdcard-boot.cmd` / `sdcard-boot.scr`, installed with
`./install_sd_boot_handoff.sh <sd-partition|mountpoint>`. `setenv` only has to
survive a few milliseconds in RAM, so `ENV_IS_NOWHERE` costs nothing here.
The fleet formats these cards **ext4** (`~/starcap-ops/storage/sd-format/format_sd_storage.sh`),
which U-Boot can read.

**UNVERIFIED, and it matters:** this assumes U-Boot gets far enough to READ the
card's filesystem. If it instead hangs in `mmc dev 1` (card init) or chokes on an
ext4 feature its limited driver does not implement, a script on that filesystem
can never run and this fix cannot work. Distinguishing the two takes one boot
with a display attached and the card in:

  * last line is `Found U-Boot script /boot.scr` or anything about scanning
    partitions -> U-Boot read the card, the handoff will work.
  * it stops at `Card did not respond to voltage select`, `mmc_init`, or simply
    hangs with no SD line at all -> it never reached the filesystem, and the
    handoff is useless. The remaining options are then a small **FAT** partition 1
    carrying `boot.scr` with ext4 data on partition 2 (U-Boot's FAT driver is far
    more robust than its ext4 one), or rebuilding and re-signing U-Boot.

Until it is confirmed, the operational workaround is the one that always works:
**boot with the slot empty, insert the card once the board is up.** The automount
rule (fix #2) picks up a hot-inserted card, so nothing is lost by doing this.

---

## Traps, all hit and paid for on 2026-09-12/13

1. **The board back-powers over USB-C.** Pulling DC while the USB cable is in the
   laptop does NOT switch it off, so a "cold power-cycle" is not cold and a failed
   boot proves nothing. **Watch the USB device number in `lsusb` — a genuine reset
   always increments it.** This invalidated a boot test and wasted a bootloader swap.
2. **A vendor loader reads 0xCC past 32 MiB.** Two things answer in "Loader" mode:
   `rk3588_spl_loader.bin` (full range) and the board's own loader, which is what
   `adb reboot loader` gives you — that one serves only the first 32 MiB and returns
   0xCC filler beyond. The *write* is fine; the *read* lies. Verification then fails
   from sector 65536 (= exactly 32 MiB) and reads precisely like the notorious
   "this eMMC only kept 32 MiB" hardware fault. `flash_emmc.sh` probes and swaps
   loaders via `reboot-maskrom` (`boot` is maskrom-only).
3. **A surviving backup GPT makes a perfect flash unbootable.** A factory board has
   Android = GPT, whose backup lives in the LAST sector of the chip. An image
   smaller than the eMMC never overwrites it, and with an MBR image U-Boot reads
   the last LBA and boots **Android's** partition table (`*** Using Backup GPT ***`).
   Linux is immune (wants a 0xEE protective MBR first); U-Boot is not.
4. **`rkdeveloptool boot` twice hangs forever** — the board re-enumerates mid-transfer
   and strands the USB handle.
5. **`pkill -f rkdeveloptool` kills your own shell.** Use `pkill -x`.
6. **`rkdeveloptool reset` is not a boot test.** It frequently re-enters maskrom,
   which looks exactly like a failed flash.
7. **systemd enable-links are ABSOLUTE symlinks.** Inside a mounted image they
   resolve against the HOST's `/etc`, so `test -e` reports a correctly-enabled unit
   as missing. Use `test -L`. (Same trap the SD flasher documents.)

---

## What did NOT work, so nobody repeats it

**The Orange Pi 5 image does not boot from eMMC on a 5B, and swapping the
bootloader does not fix it.** Three first-stage bootloaders under genuine cold
boots — the fleet's own `2017.09-orangepi`, Riek's 5B, Armbian's
`orangepi5b-vendor` — all static red, nothing on HDMI. Ruled out along the way:
`mmc@fe2e0000` is `status = "okay"` in **every** one of them, in both the SPL dtb
and the u-boot-proper dtb. So "the bootloader cannot see the eMMC" is not the
explanation, despite being the long-standing note in
`starcap-ops/emmc/clone_sd_to_emmc.sh` (since deleted).

**A hand-built offline image on the 5B base also failed.** Recorder transplanted
via chroot, `e2fsck` clean, flashed and byte-verified — did not boot. It differed
from the working base in three boot-relevant ways: merged dtb (pwm15 + uart0),
partition grown 7.7 → 12 GB, and `resize2fs`. Given trap-3 and the growroot finding
above, **the geometry change is the prime suspect, not the dtb.**

**Lesson that cost the most time:** the *server* variant of the base was boot-tested
and worked; the build was then done on the *desktop* variant without testing that it
boots. Always boot-test a base before building on it.

**Orange Pi's own images** (`orangepi.org` — refuses HTTPS, serves plain HTTP; 5B
images on Google Drive folder `1xhP1KeW_hL5Ka4nDuwBa8N40U8BN0AC9`) are kernel
**6.1.99** and were throttled to ~1 KB/s. Ranged GETs bypass the "Quota exceeded"
page if ever needed. Not used — 6.1 would be a migration away from the fleet's 5.10.

---

## Rebuilding from scratch

1. Flash the stock Orange Pi 5B Ubuntu 22.04 desktop image (deleted from here on
   2026-09-30 to save 7.7 GB; download it from orangepi.org again), boot it, complete the
   Ubuntu OEM wizard (or pre-create the user offline as was done here).
2. Enable SSH, copy the recorder tree to `/home/orangepi/orangepi-recorder`.
3. On the board:
   ```bash
   sudo apt-get install -y librga-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
   cd orangepi-recorder && sudo bash install.sh
   sudo env APP_USER=orangepi bash stereocam/post_update.sh   # MUST say "rebuilt ... (full kernel)"
   ```
4. Apply fixes 2, 3 and 4 above.
5. Sanitise (device_id, device_name, ssh host keys, machine-id, journal, caches),
   `poweroff`, capture over maskrom, `e2fsck` the capture, shrink via loop device
   with `sgdisk -e` afterwards, verify, checksum.
