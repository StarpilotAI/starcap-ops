# opm-lan — Orange Pi LAN copier

The **same app as opm**, bit for bit — same web UI, same detect → plan → copy →
progress → contact-sheets flow — except recordings come over **ethernet** instead
of an SD card in a reader. Plug a recorder into the switch/cable, it appears in the
UI, you click **Copy**, and it rsyncs the recordings onto your SSD, laid out exactly
the way opm lays them out (so the dashboard keys the footage correctly).

opm-lan is a **separate app**. It never modifies opm — it only *reuses* opm's
contact-sheet + dashboard-reporting code (read-only) so the Analysis tab behaves
identically.

---

## How the network works (set this once)

The recorders share one SSH login (`orangepi` / `orangepi`). To reach them over a
plain cable with **zero config on the pi**, this PC becomes the "router" for the
wired link:

1. **Network settings → the wired connection (`eno1`) → IPv4 → "Shared to other
   computers."**
2. Now this PC hands out addresses (`10.42.0.x`) **and internet** over that
   cable/switch. Any recorder you plug in just appears. Strictly LAN — wifi is never
   used.

---

## Run

```bash
./start_opm_lan.sh          # or:  python3 webui.py
```

Opens **http://127.0.0.1:8770**. The six panels mirror opm:

1. **Detected recorders** — every pi on the cable (IP, version, size, device id,
   online). Auto-refreshes.
2. **What to copy** — paths on the pi + the destination on this PC (with a Browse…
   picker that stars your external SSD).
3. **Plan & disk space** — what's new vs already-copied, and whether it fits.
4. **Run** — **Copy (keep source)** or the red **Copy + delete source** (checksum-
   verifies every file, then removes those recordings from the pi; confirms first;
   never deletes from a recorder it can't positively identify).
5. **Progress** — per-recorder bars, link/write MB/s, ETA, a cancel button, and per
   recorder a Re-copy & verify + Done (safe to disconnect).
6. **Contact sheets** — built after a copy and reported to the dashboard (reuses
   opm's analysis stack).

Thumbnails/previews are decoded **on the recorder** (one frame = the "is it
readable?" check) and streamed back, so they work even for huge clips.

Quick CLI check (no UI): `python3 opmlan.py` lists every recorder on the cable.

## Requirements

- `python3`, `sshpass`, `rsync`, `ssh`, `PyYAML`
  - `sudo apt install sshpass rsync openssh-client python3-yaml`
- Contact sheets (optional, same as opm): `ffmpeg` + `opencv-python-headless` +
  `numpy` **on this PC**. Without them the copy still works; the Analysis tab shows
  "off." (The *thumbnails* use the recorder's own ffmpeg, not this PC's.)

## Layout produced (identical to opm)

```
<destination>/<device-name>/stereo/<device>_<MMDD-HHMM>/…
e.g.            starcap9/stereo/starcap9_0625-2250/
```

Runs sharing a minute keep their seconds so two recordings never merge — exactly
opm's `dest_session_names`. Incremental + resumable: re-running only pulls new runs.

## Config (`config.yaml`)

Same keys as opm where they apply, plus the LAN/ssh bits:

| key | meaning |
|-----|---------|
| `ssh.user` / `ssh.password` / `ssh.port` | recorder login (shared across the fleet) |
| `copy_paths` | absolute paths on the pi to copy |
| `destination` | where pulls land on this PC (env `OPM_LAN_DESTINATION` overrides) |
| `lan.iface` / `lan.scan_subnet` | force the wired interface/subnet, or "" to auto-detect the Shared link |
| `after_copy.verify` / `delete_source` / `prune_empty_dirs` | same semantics as opm |
| `safety.require_identified` | never delete from a recorder without a readable `device_info.json` (the ethernet analog of opm's `require_label`) |
| `opm_dir` | opm's folder, reused read-only for contact sheets (env `OPM_LAN_OPM_DIR` overrides) |
| `analysis:` | dashboard URL + password for reporting (same as opm) |

## Files

- `opmlan.py` — engine: discover / identify / copy / verify / delete over SSH (CLI-runnable).
- `webui.py` — stdlib `http.server`, the same API opm's UI speaks, backed by the SSH engine.
- `web/index.html` — opm's single-page UI, relabeled for ethernet (vanilla JS, no build).
- `config.yaml`, `start_opm_lan.sh`.

## Notes

- **Strictly LAN.** Discovery and transfer go over the wired interface; wifi is unused.
- **One card per recorder** even if it holds two DHCP leases (deduped by `device_id`).
- **Delete is gated** like opm: checksum-verify → confirm every file is on this PC →
  only then remove from the pi, and only from a positively-identified recorder.
