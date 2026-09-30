# Orange Pi 5 — official Ubuntu 22.04 on NVMe

Done 2026-09-05. Board is a **plain Orange Pi 5** (RK3588S), not a 5B.
Drive: **Kingston SNV3S1000G 931.5 GB**.

## Final state

**On the NVMe:** a clone of the `opi_root` SD card — **Orange Pi 1.2.2 Jammy**
(Ubuntu 22.04, kernel `5.10.160-rockchip-rk3588`). This is Orange Pi's own
official image, not a third party build.

| | |
|---|---|
| partition | MBR, one ext4 partition @ sector 61440, 916 G (17 G used) |
| rootfs UUID | `0cf51f8d-fcbb-45cc-be6a-eff533087af4` (SD stays `b39814ad-…`) |
| `e2fsck` | clean, 210163 files |
| StarCap tuning | `isolcpus=7 nohz_full=7 rcu_nocbs=7 irqaffinity=0-3` preserved |
| overlays | `spi4-m0-cs1-spidev pwm15-m2 uart0-m2 wifi-ap6275p` preserved |

**In SPI NOR:** Orange Pi vendor `U-Boot 2017.09-orangepi (Aug 30 2024)`, byte
verified. Boot order `mmc0 mmc1 nvme scsi mtd2 mtd1 mtd0 usb0 pxe dhcp` — so
with no SD card inserted, the NVMe wins.

The clone was made **on the board** (mke2fs 1.46.5), which is why the
`orphan_file` trap never applied: that only bites when the filesystem is built
on this laptop with e2fsprogs 1.47.

## Measured facts about this board

**The M.2 slot is PCIe 2.0 x1.** Confirmed from the board itself:

    Kingston LnkCap: Speed 16GT/s, Width x4              <- drive capability
    Kingston LnkSta: Speed 5GT/s (downgraded), Width x1  <- what it actually gets

A Gen4 drive buys nothing here. Expect ~375 MB/s read. A cheap Gen3 performs
identically.

**Board reaches the LAN on the laptop's wired interface**, not WiFi:
laptop `enp2s0` = `10.77.0.1/24`, board came up on `10.77.0.151`
(ssh `orangepi`/`orangepi`). Scanning the 192.168.8.x WiFi subnet finds nothing.

## What did NOT work, and why it's still unexplained

Before this, the NVMe was flashed with **ubuntu-rockchip 22.04** (Joshua Riek)
and its matching `rkspi_loader.img` was written to SPI. Both were byte verified.
**The board would not boot it** — powered, static red LED, never reached
userspace (proven: that image enables ssh by default and DHCPs via
NetworkManager, ethernet was connected, nothing ever appeared on the LAN).

The cause was NOT:

- the drive or PCIe — Linux on this board sees the NVMe fine and `e2fsck`
  passed on it
- ext4 features — the working SD rootfs has the same `64bit` + `metadata_csum`
  as the ubuntu-rockchip one
- a bad SPI write — read back byte identical

So it was something specific to that U-Boot booting that NVMe. Not chased
further, because the vendor image was wanted anyway.

## Files here

| file | what |
|---|---|
| `spi_backup_before_20260905.bin` | **the vendor U-Boot currently on the board**, sha256 `20e58e2a…`. Restore from maskrom: `rkdeveloptool write 0 spi_backup_before_20260905.bin` |
| `rkspi_loader.img` | ubuntu-rockchip SPI loader, sha256 `b3a12823…`. Only if going back to that image. |
| `rkspi_loader_sata.img` | same, for M.2 **SATA** drives |
| `idbloader.img`, `u-boot.itb` | ubuntu-rockchip first stage / U-Boot proper |
| `rk3588_spl_loader.bin` | RK3588 maskrom DDR loader |

Maskrom: USB-C direct to the laptop (no hub), OTG port; power off; hold MaskRom;
apply power; hold 2-3 s; release. `lsusb` shows `2207:350b`.
Then `rkdeveloptool boot rk3588_spl_loader.bin` and check
`rkdeveloptool read-flash-info` reports **16 MB** before writing anything.

## Hardware fault found on the way

**The SD card reader's write path is broken.** Every write aborts at the USB
transport layer:

    [sdb] Add. Sense: Data phase CRC error detected
    I/O error, dev sdb, sector 0 op 0x1:(WRITE)

It fails on sector 0, reproducibly, across two cards and two reader slots.
Reads work fine. `dd` wedges in uninterruptible D-state and survives `kill -9`.
Not worn flash — a link integrity fault, so suspect the reader or its cable.
**One 128 GB card was left half written** and needs a full rewrite on working
hardware before reuse.
