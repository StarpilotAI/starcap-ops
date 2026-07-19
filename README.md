# starcap-ops

Copy-station + fleet-operations toolkit for the StarCap footage system.

- **`opm-lan/`** — LAN copier: pulls recordings off the recorder Orange Pis over ethernet
  onto an SSD, with a web UI on `http://localhost:8770`. Run `cd opm-lan && ./start_opm_lan.sh`.
  Now a **git submodule** of [StarpilotAI/opm-lan](https://github.com/StarpilotAI/opm-lan) —
  the code lives there; this repo just pins a version. After cloning or pulling, run:
  ```bash
  git submodule update --init   # fetches opm-lan (needs access to the StarpilotAI org repo)
  ```
- **`tools/`** — copy-station / fleet diagnostics and fixes (SSD cloud-verify, SD-card
  health, ARP fix, boot-buzzer fix, etc.).

**Start here → [`CLAUDE.md`](CLAUDE.md)**: the full setup, network layout (the
`10.77.0.1/24` wired subnet), credentials, and a fresh-laptop checklist.

> ⚠️ `opm-lan/config.yaml` holds operational passwords (`orangepi`, `starupload`) in
> plaintext and machine-specific paths — private repo only; adjust `destination:` per machine.
