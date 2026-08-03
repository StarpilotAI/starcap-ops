# starcap-ops

Copy-station + fleet-operations toolkit for the StarCap footage system.

- **`tools/`** — copy-station / fleet diagnostics and fixes (SSD cloud-verify, SD-card
  health, ARP fix, boot-buzzer fix, etc.).
- **`setup_copy_station.sh`** — provisions a fresh Ubuntu laptop into a copy station.

The **copiers live in the `starcap-dashboard` repo**, not here: `opm-lan/` (ethernet,
web UI on `http://localhost:8770` via `./start_opm_lan.sh`) and `orangepi-manager/`
(SD card, "opm"). This repo carried `opm-lan/` as a git submodule until 2026-08-03,
when that copy was deleted for having drifted behind the dashboard's — edit the
copiers in `starcap-dashboard`.

**Start here → [`CLAUDE.md`](CLAUDE.md)**: the full setup, network layout (the
`10.77.0.1/24` wired subnet), credentials, and a fresh-laptop checklist.

> ⚠️ `starcap-dashboard/opm-lan/config.yaml` holds operational passwords (`orangepi`,
> `starupload`) in plaintext and machine-specific paths — private repo only; adjust
> `destination:` per machine.
