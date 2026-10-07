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
| make a laptop's ethernet port accept docked boards | `station/` | `sudo station/setup_dock_lan.sh`, then `setup_dock_lan.sh --check` |
| find every board on the LAN and open its page | `fleet/starcapfinder/` | always-on service, http://127.0.0.1:8090 |
| QC a finished StarCap end to end, then name it | **its own repo**: [StarpilotAI/starcap-qc](https://github.com/StarpilotAI/starcap-qc) | `StarCap QC` launcher, or `starcap-qc` |
| rename boards as they dock | `fleet/opm-name/` | `opm-name` launcher |
| make sure a docked board is current, auto-updating and on main | `fleet/dock-check/` | `dock-check` |
| hold a board on a specific recorder branch or commit | `fleet/pin-to-branch/` | `pin-to-branch --branch <ref>` |
| see a board's desktop | `fleet/vnc/` | `vnc-into-pi <ip>` |
| grade a board's true fps / snapshot its hardware | `fleet/board-grading/` | `measure-fps <ip> <label> 90`, `board-fingerprint <ip>` |
| silence the boot beep, fix the timezone, check a camera's USB link | `fleet/pi-fixes/` | see its `install.sh` output |
| turn an SD card into plain ext4 storage | `storage/sd-format/` | `format-sd-storage` |
| flash a board (NanoPi or Orange Pi, SD or eMMC) | `flashing/` | see its [README](flashing/README.md); images via `flashing/fetch_images.sh` |
| live-tune a camera's v4l2 controls in the browser | `camera/camrig/` | `camrig` launcher |
| see a DECXIN live at native resolution | `camera/camrig/` | `CamView` launcher, or press ± |
| tell which firmware family a DECXIN camera is on | `camera/camfw/` | `Camera Firmware` / `Camera Firmware LAN` launchers |
| check a camera's JPEGs will pass the RK hardware decoder | `camera/fingerprint/` | `camera-fingerprint` |
| see who has held a camera | `inventory/whoholds/` | `whoholds <camera>` |

## Layout

```
station/      the laptop's dock ethernet port
fleet/        tools that talk to boards over the dock ethernet
storage/      SD cards plugged into the laptop
flashing/     every board image + flash script (images: private bucket, not git)
camera/       DECXIN stereo cameras on the bench
inventory/    who has what, and board records
lib/          install_common.sh, shared by every install.sh
```

Tools write their output (CSV, TSV, reports) into the folder they run in, and all
of it is gitignored. The few files a tool reads back, like
`fleet/opm-name/board_registry.json`, stay next to that tool.

## Elsewhere

- `orangepi-recorder`: the recorder firmware the fleet self-updates from.
- `starcap-dashboard`: starcap.dev plus both footage copiers.
- `wide-fov-stereo-calibration`: the arm calibration bench.
- `lenscheck`: lens grading, in its own repo.
- `~/laptop-recorder`: the board recording pipeline, runnable on the laptop.
