# CLAUDE.md — StarCap copy-station & fleet ops

This repo is the **copy-station + fleet-operations toolkit** for the StarCap footage
system. One folder per tool, grouped by job, each with its own `install.sh`
(`./install.sh` at the root lists them; [`README.md`](README.md) is the
"which tool when" index):

- **`station/`**: `setup_dock_lan.sh`, which makes a laptop's ethernet port accept docked boards
- **`fleet/`**: tools that talk to docked boards (dock-check, pin-to-branch,
  opm-name, starcapfinder, vnc, board-grading, pi-fixes)
- **`storage/`**: sd-format (turn an SD card into plain storage for a board)
- **`camera/`**: DECXIN bench tools (camrig + CamView, camfw, fingerprint)
- **`inventory/`**: whoholds
- **`lib/install_common.sh`**: the helper every `install.sh` sources

- **`flashing/`**: the ONLY board flashing: `nanopi/{emmc,sd,rebuild}`,
  `opi/{emmc,sd,rescue,spi-recovery}` (`~/Desktop/flashing` is a link to it). Git holds
  the scripts, loaders, docs and small partitions; the five big images (~30 GB) are
  gitignored and live in the Google Drive folder "StarCap flashing images" (shared with
  starpilot.ai only). `IMAGES.sha256` is the manifest; `fetch_images.sh --from <dir>`
  places browser downloads, plain `fetch_images.sh` uses rclone; both verify sha256 and
  restore mtimes (the flash scripts pick the NEWEST image beside them, so keep one per
  folder). The images carry `stereocam/update_token`: never share the folder as
  "Anyone with the link".

Not here on purpose: the SSD cloud-verify / hours / drive-health tools and the opm SD
launcher were retired after the opm-lan rework (2026-09-30).

Conventions for adding a tool: give it its own folder under the right group, with an
`install.sh` that sources `../../lib/install_common.sh` and declares its deps with
`need_cmd`/`need_py`. Resolve every path relative to the script, never
`/home/waleed/...`. Gitignore whatever it writes.

The **copiers are not here** — both live in the `starcap-dashboard` repo:
`opm-lan/` (the ethernet path) and `orangepi-manager/` (the SD-card path, "opm").
This folder used to carry `opm-lan/` as a git submodule of `StarpilotAI/opm-lan`;
that copy was **deleted on 2026-08-03** because it had drifted well behind the
dashboard's (it predated the `_transfer.json` landing stamps entirely). Don't
re-add it — edit the copiers in `starcap-dashboard`.

The goal of this repo is to reproduce the whole copy-station setup on another Ubuntu
laptop. Read this file first — it explains the moving parts, the network layout, the
conventions, and what to change per-machine.

---

## The StarCap system (the big picture)

```
Orange Pi 5 recorders (fleet: starcapNN)   ← 2 USB cameras, IMU, records to SD card
        │  (recordings/stereo/<session>/…: stereo_*.mp4, imu.csv, frames.csv, *.json)
        │  dock over ETHERNET
        ▼
   opm-lan  (in starcap-dashboard)  ── rsync-over-ssh ──▶  SSD drive (exFAT), then →
        ▼
   ssd_upload_server → Google Cloud Storage → dashboard (starcap.dev)
```

- **Recorders** run the `orangepi-recorder` firmware (separate repo:
  `github.com/StarpilotAI/orangepi-recorder`, service `stereocam`). They record to an SD
  card and, when on a trusted link, self-update from that repo's `main` and heartbeat
  the dashboard.
- **Dashboard**: `starcap.dev` (separate repo `starcap-dashboard`, Next.js on Vercel +
  Upstash Redis). Devices are keyed by **cpu_id** (stable across reflash); names live
  there and are pushed to each pi.
- **This copy-station**: an Ubuntu laptop/PC where recorders dock over ethernet so
  `opm-lan` can copy their footage off to an SSD for upload.

---

## opm-lan — the LAN copier (lives in `starcap-dashboard/opm-lan`)

- **Run it:** `cd ~/starcap-dashboard/opm-lan && ./start_opm_lan.sh` → web UI on
  **http://localhost:8770**. The launcher frees port 8770 and (re)starts the server. It
  is a plain Python process — editing the `.py` does nothing until you restart it.
- **Stack:** stdlib + PyYAML (optional) + external `sshpass`, `ssh`, `rsync`, `ffmpeg`.
- **How it works:** discover recorders on the LAN (ping-sweep the subnet) → identify each
  over SSH (reads `device_info.json`) → inventory recordings → rsync each session to the
  SSD in parallel (`max_workers`) → optional verify/preview/contact-sheet.
- **Add-devices-mid-copy:** the UI has a **"➕ Add connected devices"** button that
  enqueues newly-docked recorders into a running copy job without cancelling it
  (`/api/add`; a dynamic worker pool drains an add-queue).
- **Progress:** per-session copied bytes are summed for the % (so a destination folder
  that already holds an older copy of that recorder does NOT inflate the bar).

### What opm-lan shares with opm (and what it doesn't)

opm-lan imports some of opm's modules straight off disk, so a change there lands on
**both** copiers with no duplication: `transfer.py` (the `_transfer.json` landing
stamps) and `analysis.py`, which pulls in `contactsheet.py`, `dataquality.py` and
(lazily) `cloud.py`.

Everything else is a **separate file per copier** and must be changed twice: the copy
engines (`opm.py` vs `opmlan.py`), `webui.py`, `web/index.html`, and `config.yaml`.

Note the asymmetry: opm imports `transfer` hard at top level, while opm-lan's import is
lazy inside a `try/except` that only prints `[transfer] landing stamps off: …`. Break a
shared module and opm fails loudly while opm-lan silently stops stamping.

### `starcap-dashboard/opm-lan/config.yaml` — EDIT PER MACHINE
- `destination:` — the SSD mount path. **This changes per machine / per drive**
  (e.g. `/media/<user>/<DriveLabel>`). Set it before copying.
- `ssh.user`/`ssh.password` — recorder login (**`orangepi` / `orangepi`**). One pair
  for every board, so a NanoPi R76S (`pi` / `12345`) needs this edited before it
  can be copied — see Conventions below.
- `lan.iface`/`scan_subnet` — leave **empty**; opm-lan auto-detects the wired subnet
  from the copy-station NIC (see networking below).
- `analysis.password` — the upload password (**`starupload`**).
- `opm_dir` — path to `orangepi-manager` (opm). Safe to leave **empty** now that both
  live in `starcap-dashboard`: opm-lan falls back to the sibling directory, so it still
  finds opm's shared modules even with no `opm_dir` set.

> ⚠️ `config.yaml` contains the `orangepi` and `starupload` passwords in plaintext.
> Fine for a private repo, but rotate/redact if it is ever shared.

---

## Tools that ssh into boards

`fleet/opm-name`, `fleet/dock-check` and `fleet/vnc` probe **both** fleet logins.
The older one-shot scripts (`fleet/pi-fixes/fix_boot_buzzer.sh`, `fix_timezone.sh`) only know the Orange Pi login
`orangepi`/`orangepi`, and `fleet/board-grading/board_fingerprint.sh` only knows the
NanoPi one `pi`/`12345`. Sudo on the pi uses the same password, fed with `sudo -S`.

`fleet/opm-name/`'s `install.sh` + `make_zip.sh` package it as a standalone zip for a
laptop with no checkout. It is the one installer that copies the app out of the repo
(to `~/.local/share/opm-name`); the others point launchers at the checkout.

---

## Board firmware state — which tool to reach for

Two tools, opposite jobs. Reaching for the wrong one is how a board gets frozen or
silently regressed:

| Tool | Use it when |
|---|---|
| **`fleet/dock-check/dock_check.py`** | A board is docked and you want it **current + auto-updating + on main** before it ships. Reports and, on confirmation, fixes. This is the default. |
| `fleet/pin-to-branch/pin_to_branch.sh` | You deliberately want a board **off** latest-main: a canary on a feature branch, or frozen on a commit. `--branch` is required (its old default, v1.37.2, records 0 frames on the R76S) and `--updates` defaults to off, so it is the opposite of `dock_check.py`. |

(`fleet_branch_check.py`, the read-only sweep, was deleted on 2026-09-30: `dock_check.py`
reports the same things, and it was key-auth only, so every NanoPi read UNREACHABLE.)

`dock_check.py` specifics worth knowing:

- **"Docked" means on this laptop's ethernet, and that link is auto-detected** from the
  wired interfaces (not hardcoded to `10.77.0`, though that stays the documented copy-station
  setup). This matters because the UDP roll-call broadcasts out *every* interface, so a
  recorder on the office WiFi answers it too — and it is not docked. Restarting one of
  those would stop a take on a board in someone's hands. Verified 2026-08-12: with only
  WiFi up, the tool refuses and names the down NIC; with `enp2s0` up it saw the docked
  board and ignored the WiFi one. `--any-link` opts into including WiFi boards, `--host`
  bypasses discovery, `--subnet` forces a /24.
- **"Latest" comes from `origin/main` after a fetch, never from the local working
  tree.** The local clone is routinely many versions behind (checked 2026-08-12: tree
  at 1.39.5, `origin/main` at 1.49.1), and a yardstick that is behind reports an
  out-of-date board as fine.
- It **never copies code**. It fixes config and restarts `stereocam`, and the board
  self-updates from GitHub — `uploader.py` calls `maybe_update(force=not was_online)`,
  so the first pass after a restart forces a check. Hand-deploying stays in
  `pin_to_branch.sh`, and `dock_check.py` prints that command when a board has no
  route to `api.github.com`.
- It reads the **effective** environment (`systemctl show`), not a config file, and
  inventories `stereocam.service.d/*.conf` — drop-ins merge in lexical order, so a
  leftover `20-pin-commit.conf` silently beats a correct pin. It writes the same
  `20-pin-main.conf` filename the old `fleet_branch_check.py --fix` wrote, on purpose.
- It probes **both** logins (`orangepi`/`orangepi`, then `pi`/`12345`), so NanoPi
  boards are checked too.
- A board it could not inspect is reported UNKNOWN and **never** counted as a pass.
  Only docked, powered boards answer, so absence proves nothing.
- No token or no route to GitHub ⇒ autoupdate is a **no-op** even when `UPDATE_ENABLE=1`
  (`SelfUpdater.maybe_update` returns before any network call), so both are checked.

---

## Networking — the copy-station wired setup (IMPORTANT)

The recorders run a WiFi hotspot in NetworkManager "shared" mode, which is **always
`10.42.0.1/24`**. If the copy-station's wired share is *also* `10.42.0.1/24` (the NM
default), a docked pi's gateway collides with the PC's → the PC can't ARP-resolve the pi
→ can't copy it. So the copy-station's wired NIC is moved off that subnet:

```bash
sudo station/setup_dock_lan.sh [iface]   # set it up / repair it; safe to re-run
station/setup_dock_lan.sh --check        # no root: port state + which boards answer on which login
```

It shares the port on `10.77.0.1/24` as the NM profile **`starcap-dock`** (an old
`opm-lan` / `PI-Dock` profile on that port gets renamed), pins it to autoconnect
priority 100, turns autoconnect off for any other profile **on the same port** (a
DHCP-client profile winning a replug is how a docked board silently never gets an IP),
and raises the ARP thresholds (`/etc/sysctl.d/99-arp-cache.conf`) so the /24 sweep
doesn't evict live boards. `fleet/opm-name/install.sh --network` runs the same script.

- Verify: `ip -br addr show <iface>` → `10.77.0.1/24`.
- On a laptop with one ethernet port, an office DHCP profile on that port gets
  autoconnect off, so switch back by hand: `nmcli connection up <that profile>`.

On a **fresh laptop** the wired NIC name differs (e.g. `eno1`, `enp*`) — set the right
connection name in the nmcli command above.

---

## Conventions / credentials

- Recorder (pi) login — **the fleet is mixed hardware and each kind has its own**
  (the password is also that board's sudo password):

  | Board | Login | Identify hardware |
  |---|---|---|
  | Orange Pi 5 | `orangepi` / `orangepi` | buzzer, pin 15 |
  | NanoPi R76S | `pi` / `12345` | onboard `sys/wan/lan` LEDs — **no buzzer** |

  Tools that dock arbitrary boards must try both rather than assume: `opm-name`,
  `dock-check` and `vnc_into_pi.sh` do (they probe the logins and cache which one each
  board answered on). ⚠ **opm-lan does not** — its `config.yaml` carries a single
  `ssh.user`/`ssh.password`, so copying footage off a NanoPi needs that edited
  first (or the same auto-detect ported over).
- Upload password: **`starupload`**. Dashboard password: **`starchild`**.
- No passwordless sudo anywhere — feed the password to `sudo -S`.
- Recorders are named by **cpu_id** on the dashboard; the local `~/.config/starcap/
  device_name` on a pi can be stale/wrong — trust the dashboard.

---

## Setup on a fresh Ubuntu laptop (checklist)

1. Clone this repo.
2. `sudo station/setup_dock_lan.sh`: makes the ethernet port accept docked NanoPis
   and Orange Pis (see Networking). Safe to re-run. Then `./install.sh all`.
3. Clone `starcap-dashboard` — it holds both copiers (`opm-lan/` and
   `orangepi-manager/`).
4. Edit `starcap-dashboard/opm-lan/config.yaml` → set `destination:` to your SSD mount.
5. `cd starcap-dashboard/opm-lan && ./start_opm_lan.sh` → open http://localhost:8770,
   dock a recorder over ethernet, and it should appear.

---

## Related repos (not here)
- `github.com/StarpilotAI/orangepi-recorder` — recorder firmware (fleet self-updates
  from its `main`; `stereocam` service). Notable recent work: boot-buzzer U-Boot fix, and
  **v1.23.0** = recorders broadcast to the dashboard over a USB modem (not just
  ethernet/WiFi) via a `link_up()` = ethernet ∪ wl* WiFi ∪ USB-modem gate.
- `starcap-dashboard` — the Next.js dashboard on Vercel, **and both copiers**:
  `opm-lan/` (ethernet) and `orangepi-manager/` (SD card, "opm").
