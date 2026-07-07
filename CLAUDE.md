# CLAUDE.md — StarCap copy-station & fleet ops

This repo is the **copy-station + fleet-operations toolkit** for the StarCap footage
system. It holds two things:

- **`opm-lan/`** — the LAN copier: pulls recordings off the recorder Orange Pis over
  ethernet and writes them to an SSD, with a web UI.
- **`tools/`** — standalone diagnostics/fixes for the fleet, the copy-station PC, and
  the SSDs (SD-card health, cloud-hours checks, ARP fix, boot-buzzer fix, etc.).

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
   opm-lan  (this repo)  ── rsync-over-ssh ──▶  SSD drive (exFAT), then →
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

## opm-lan — the LAN copier

- **Run it:** `cd opm-lan && ./start_opm_lan.sh` → web UI on **http://localhost:8770**.
  The launcher frees port 8770 and (re)starts the server. It is a plain Python process
  — editing the `.py` does nothing until you restart it.
- **Stack:** stdlib + PyYAML (optional) + external `sshpass`, `ssh`, `rsync`, `ffmpeg`.
- **How it works:** discover recorders on the LAN (ping-sweep the subnet) → identify each
  over SSH (reads `device_info.json`) → inventory recordings → rsync each session to the
  SSD in parallel (`max_workers`) → optional verify/preview/contact-sheet.
- **Add-devices-mid-copy:** the UI has a **"➕ Add connected devices"** button that
  enqueues newly-docked recorders into a running copy job without cancelling it
  (`/api/add`; a dynamic worker pool drains an add-queue).
- **Progress:** per-session copied bytes are summed for the % (so a destination folder
  that already holds an older copy of that recorder does NOT inflate the bar).

### `opm-lan/config.yaml` — EDIT PER MACHINE
- `destination:` — the SSD mount path. **This changes per machine / per drive**
  (e.g. `/media/<user>/<DriveLabel>`). Set it before copying.
- `ssh.user`/`ssh.password` — recorder login (**`orangepi` / `orangepi`**, fleet-wide).
- `lan.iface`/`scan_subnet` — leave **empty**; opm-lan auto-detects the wired subnet
  from the copy-station NIC (see networking below).
- `analysis.password` — the upload password (**`starupload`**).
- `opm_dir` — path to the dashboard's `orangepi-manager` (only needed for
  contact-sheet/estimate analysis; adjust or ignore).

> ⚠️ `config.yaml` contains the `orangepi` and `starupload` passwords in plaintext.
> Fine for a private repo, but rotate/redact if this repo is ever shared.

---

## tools/  (run from the copy-station, mostly SSH into pis)

| Tool | What it does |
|---|---|
| `check_ssd.py` | Verify an SSD's recordings against the cloud — layout/wrapper-proof (handles weird nested folders via `--any-key`; `--also-device`). The authoritative "is this footage actually uploaded" check. |
| `check_cloud_hours.py` / `check_drive_hours.py` | Tally recorded hours from the cloud vs a drive (hours come from opm contact sheets, not raw uploads). |
| `fix_boot_buzzer.sh` | Silence the recorders' long boot BEEP. Patches the pi's U-Boot boot script (`/boot/boot.cmd`→`boot.scr`) to drive the buzzer GPIO (pin15/GPIO0_D4) low before Linux starts. Idempotent; `--scan` hits the whole /24; `--reboot` optional. |
| `sdcard_health.sh` / `sdcard_probe.sh` | Read-speed / health profiling of a recorder's SD card (bad/slow cards are a recurring cause of flapping + slow copies). |
| `vnc_into_pi.sh <ip>` | VNC into a recorder's live desktop over ethernet (also wired into opm-lan as a "🖥️ VNC" button per online recorder). Installs/starts `x11vnc`, works around this fleet's Mali GPU driver needing a DRM-forced HDMI connector to render anything, opens TigerVNC fullscreen, and cleans everything up on the pi when you close the viewer. |

ARP-cache thresholds (raising the PC's `gc_thresh` so a /24 ping-sweep doesn't evict
live pis — fixes pis flickering in/out of opm-lan) and the UGREEN USB-SATA UAS
workaround used to be separate scripts here; the ARP fix is now inlined into
`setup_copy_station.sh` (see below), and the UGREEN workaround was dropped as unused.

Most tools SSH to pis with `sshpass -p orangepi ssh orangepi@<ip>`. Sudo on the pi also
uses the password `orangepi` (feed via `sudo -S`).

---

## Networking — the copy-station wired setup (IMPORTANT)

The recorders run a WiFi hotspot in NetworkManager "shared" mode, which is **always
`10.42.0.1/24`**. If the copy-station's wired share is *also* `10.42.0.1/24` (the NM
default), a docked pi's gateway collides with the PC's → the PC can't ARP-resolve the pi
→ can't copy it. So the copy-station's wired NIC is moved off that subnet:

```bash
# put the wired NIC on 10.77.0.1/24 (NAT-share so pis get internet too)
nmcli connection modify <wired-conn> ipv4.method shared ipv4.addresses 10.77.0.1/24
# docked pis then get 10.77.0.x; opm-lan auto-detects this subnet.
```

- Verify: `ip -br addr show <iface>` → `10.77.0.1/24`. If NM regenerates the connection
  (netplan), re-apply and consider persisting it properly.
- Also raise ARP thresholds so the sweep doesn't evict pis — `setup_copy_station.sh`
  does this (writes `/etc/sysctl.d/99-arp-cache.conf`, raises `gc_thresh1/2/3`).

On a **fresh laptop** the wired NIC name differs (e.g. `eno1`, `enp*`) — set the right
connection name in the nmcli command above.

---

## Conventions / credentials

- Recorder (pi) login: **`orangepi` / `orangepi`** (also the pi's sudo password).
- Upload password: **`starupload`**. Dashboard password: **`starchild`**.
- No passwordless sudo anywhere — feed the password to `sudo -S`.
- Recorders are named by **cpu_id** on the dashboard; the local `~/.config/starcap/
  device_name` on a pi can be stale/wrong — trust the dashboard.

---

## Setup on a fresh Ubuntu laptop (checklist)

1. Clone this repo.
2. `sudo ./setup_copy_station.sh` — installs opm-lan's packages (sshpass, rsync,
   ffmpeg, PyYAML, opencv/numpy), shares the wired NIC on `10.77.0.1/24` (see
   Networking), and raises the ARP-cache thresholds. Safe to re-run.
3. Edit `opm-lan/config.yaml` → set `destination:` to your SSD mount.
4. `cd opm-lan && ./start_opm_lan.sh` → open http://localhost:8770, dock a recorder over
   ethernet, and it should appear.

---

## Related repos (not here)
- `github.com/StarpilotAI/orangepi-recorder` — recorder firmware (fleet self-updates
  from its `main`; `stereocam` service). Notable recent work: boot-buzzer U-Boot fix, and
  **v1.23.0** = recorders broadcast to the dashboard over a USB modem (not just
  ethernet/WiFi) via a `link_up()` = ethernet ∪ wl* WiFi ∪ USB-modem gate.
- `starcap-dashboard` — the Next.js dashboard on Vercel.
