# Flashing — the only board images and scripts we use

The five big images are not in git. They are in the Google Drive folder
**StarCap flashing images** (shared with starpilot.ai accounts only). On a fresh checkout:

    ./install.sh                         # packages + the Desktop shortcut; lists missing images
    # download the images from the Drive folder in a browser, then:
    ./fetch_images.sh --from ~/Downloads # checks each one, puts it in place, fixes its date
    ./fetch_images.sh --check            # full sha256 proof against IMAGES.sha256

With rclone logged in (`rclone config create gdrive drive scope=drive`), plain
`./fetch_images.sh` downloads them directly (`./fetch_images.sh nanopi/sd` for one folder).


| board | medium | folder | run |
|---|---|---|---|
| NanoPi R76S | eMMC | `nanopi/emmc/` | board in maskrom (A-to-A cable, no hub): `sudo ./flash_nanopi_emmc.sh` |
| NanoPi R76S | SD card | `nanopi/sd/` | `sudo ./flash_sd.sh /dev/sdX` |
| Orange Pi 5B | eMMC | `opi/emmc/` | board in maskrom: `sudo ./flash_emmc.sh` |
| Orange Pi 5 | SD card | `opi/sd/` | `sudo ./flash_opi_sd.sh /dev/sdX` |

Versions: NanoPi recorder **1.50.0** (image 2026-08-22/23), Orange Pi recorder
**1.55.0** (SD 2026-08-30, 5B eMMC 2026-09-13). Each folder's README has the details.

Extras:
- `nanopi/rebuild/`: build a NanoPi SD image from the eMMC image set, clone a running
  board, and `strip_overlay_xattrs.py` (REQUIRED when building a golden userdata, or
  clones boot to a blinking cursor).
- `opi/rescue/`: `force_sd_boot.sh` (make an Orange Pi with an eMMC install boot from
  SD) and `check_onboard_storage.sh` (eMMC or SPI-only?). RK3588 only.
- `opi/spi-recovery/`: the SPI-NOR loaders, and `spi_backup_before_20260905.bin`, the
  ONLY copy of one board's SPI chip backup. Do not delete.
- `nanopi/sd/flash_sd.sh --expand-only /dev/sdX` grows a card written with Etcher.
- To wipe an ex-boot card into plain storage:
  `~/starcap-ops/storage/sd-format/format_sd_storage.sh`.

The flash scripts pick the NEWEST image next to them. Keep exactly one image per
folder, and copy with `cp -p` / `mv` so the dates survive.
