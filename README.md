# starcap-ops

The ops toolbox for the StarCap fleet: the copy-station laptop, the boards, the
drives, flashing, and the cameras. One folder per tool, each with its own
`install.sh`.

```bash
./install.sh              # list the tools
./install.sh all          # install everything
./install.sh camfw vnc    # install just these
```

An installer only installs the apt packages that are missing, puts commands in
`~/.local/bin`, and writes launchers for the GUI tools. Re-run it after a `git pull`
or after moving the checkout. `NO_APT=1` only reports what's missing.

The **footage copiers are not here**. `opm-lan` (ethernet) and `orangepi-manager`
(SD card) live in the `starcap-dashboard` repo. Read [`CLAUDE.md`](CLAUDE.md) for the
network layout, the logins, and a fresh-laptop checklist.

## Which tool when

| I want to… | tool | run |
|---|---|---|
| set up a fresh copy-station laptop | `station/` | `sudo station/setup_copy_station.sh` |
| copy footage off SD cards | `station/` | `opm (SD card copier)` launcher |
| find every board on the LAN and open its page | `fleet/starcapfinder/` | always-on service, http://127.0.0.1:8090 |
| rename boards as they dock | `fleet/opm-name/` | `opm-name` launcher |
| make sure a docked board is current, auto-updating and on main | `fleet/dock-check/` | `dock-check` |
| hold a board on a specific recorder branch or commit | `fleet/pin-to-branch/` | `pin-to-branch --branch <ref>` |
| see a board's desktop | `fleet/vnc/` | `vnc-into-pi <ip>` |
| grade a board's true fps / snapshot its hardware | `fleet/board-grading/` | `measure-fps <ip> <label> 90`, `board-fingerprint <ip>` |
| silence the boot beep, fix the timezone, check a camera's USB link | `fleet/pi-fixes/` | see its `install.sh` output |
| check a drive's footage is really in the cloud before formatting | `storage/ssd-verify/` | `check-ssd`, `ssd-audit` |
| count recorded hours on a drive or in the cloud | `storage/ssd-verify/` | `check-drive-hours`, `check-cloud-hours` |
| check a drive or enclosure is healthy | `storage/drive-health/` | `drive-health`, `fix-usb-enclosure` |
| turn an SD card into plain ext4 storage | `storage/sd-format/` | `format-sd-storage` |
| flash a NanoPi R76S (SD or eMMC) | `flashing/nanopi-r76s/` | see its [README](flashing/nanopi-r76s/README.md) |
| rescue an Orange Pi 5 over maskrom | `flashing/orangepi5/` | `force_sd_boot.sh`, `check_onboard_storage.sh` |
| tell which firmware family a DECXIN camera is on | `camera/camfw/` | `Camera Firmware` / `Camera Firmware LAN` launchers |
| A/B-test a camera firmware for frame jitter | `camera/camfw/` | `Firmware Jitter Bench` launcher |
| hand-focus a lens barrel | `camera/lensfocus/` | `Lens Barrel Focus` launcher |
| check which cameras are calibrated and uploaded | `camera/calibcheck/` | `Calibration Upload Check` launcher |
| check a camera's JPEGs will pass the RK hardware decoder | `camera/fingerprint/` | `camera-fingerprint` |
| log a vin board's eMMC speed | `inventory/vin-tracker/` | `vin-check`, `Vin board tracker` launcher |
| see who has held a camera | `inventory/whoholds/` | `whoholds <camera>` |

## Layout

```
station/      the copy-station laptop itself
fleet/        tools that talk to boards over the dock ethernet
storage/      SSDs and SD cards plugged into the laptop
flashing/     putting an OS + recorder onto a board
camera/       DECXIN stereo cameras on the bench
inventory/    who has what, and board records
lib/          install_common.sh, shared by every install.sh
```

Tools write their output (CSV, TSV, reports) into the folder they run in, and all
of it is gitignored. The few files a tool reads back, like
`fleet/opm-name/board_registry.json` and `inventory/vin-tracker/pi_emmc_speeds.csv`,
stay next to that tool.

## Elsewhere

- `orangepi-recorder`: the recorder firmware the fleet self-updates from.
- `starcap-dashboard`: starcap.dev plus both footage copiers.
- `wide-fov-stereo-calibration`: the arm calibration bench.
- `lenscheck`, `camrig`: lens grading and live UVC tuning, each in its own repo.
- `~/laptop-recorder`: the board recording pipeline, runnable on the laptop.
