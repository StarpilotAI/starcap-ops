# StarCap drive & cloud tools

Three small, standalone Python scripts for checking StarCap footage. All are
**read-only** — they only read; none of them ever uploads, deletes, formats, or
changes anything on a drive or in the cloud. Safe to run anytime.

Copy this whole folder to any PC (Windows / Mac / Linux) and run from a terminal.

---

## Requirements

- **Python 3** (pre-installed on Mac/Linux; on Windows install from python.org and
  tick "Add Python to PATH").
- **ffmpeg / ffprobe** — needed for *exact* hours. Without it the hour-counting
  tools still run but fall back to a calibrated estimate (~9 GB/hour).
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Mac (Homebrew): `brew install ffmpeg`
  - Windows: download from https://ffmpeg.org/download.html and add to PATH

The two cloud tools also need the **dashboard password**. It's set near the top of
each file (`STARCAP_PASSWORD`), or pass it as an environment variable:
`STARCAP_PASSWORD=xxxx python3 <tool>.py`

---

## The three tools

### 1. `check_cloud_hours.py` — how much footage is in the cloud
Reports the **total video stored in the cloud (TB)** and the **real footage hours**,
measured from the actual videos (ffprobe), not opm's contact sheets. Also prints the
dashboard's own number for comparison.

```bash
python3 check_cloud_hours.py
```
Needs internet + the dashboard password. ffmpeg gives an exact bitrate; without it,
uses the calibrated 9 GB/hour.

### 2. `check_ssd.py` — is THIS drive (or one folder) safely in the cloud? (check before formatting)
Scans a drive, confirms every file has a copy in the cloud (by **name** and
**byte size**), and tells you whether it's safe to format. Lists anything missing.

**Replace `<...>` with your own path** — the quotes matter if the path has spaces.

```bash
# whole drive (auto-detects each device folder inside)
python3 check_ssd.py "<drive path>"
python3 check_ssd.py "/media/you/My Book"          # Linux
python3 check_ssd.py "/Volumes/Extreme SSD"        # Mac
py check_ssd.py "E:\"                               # Windows
python3 check_ssd.py                                # no path → auto-detect plugged drives
```

**Check just ONE folder** (fast — doesn't scan the rest of the drive):

```bash
# point straight AT the device folder (its name is in the path)
python3 check_ssd.py "<drive path>/<device>"
python3 check_ssd.py "/media/you/My Book/starcap24"
```

**Options:**

```bash
# --only <device> : point at the WHOLE drive but check only this one device
python3 check_ssd.py "/media/you/My Book" --only starcap24

# --device <name> : force the cloud key name (backup for odd/nested layouts, when the
#                   "Scanning…" line shows the wrong device name)
python3 check_ssd.py "<some folder>" --device starcap24
```

| you want to check… | path points at | flag |
|---|---|---|
| a whole drive (all devices) | the drive → `"/media/you/My Book"` | — |
| one device, fast | the device folder → `".../My Book/starcap24"` | — |
| one device on a big drive | the drive → `".../My Book"` | `--only starcap24` |
| a weird/nested layout | the folder | `--device starcap24` |

Needs internet + the dashboard password. Writes a `ssd_sample_check_<drive>.csv` of
the size-check, and (if anything is missing) a `ssd_missing.tsv` list.

**The keying rule — point at the level where the device folders sit directly inside.**
Each run's cloud path is `device_id/run`, taken from the FIRST folder below where you
point. So point where you can *see* `starcap1/`, `starcap24/`, `orangepi5-…/` right
there. If the devices are buried under an extra **wrapper** folder
(e.g. `My Book/Salah_28-6_starpilot1/starcap9/…`), point AT the wrapper
(`".../Salah_28-6_starpilot1"`), not above it — otherwise the wrapper's name becomes
the "device" and everything shows falsely "missing." (Pointing straight at a single
`.../starcapN` folder is handled automatically.)

> ✅ `MISSING (by name): 0` + sizes match → safe to format.
> ⚠ anything listed as missing → **do NOT format**, get those files uploaded first.

### 3. `check_drive_hours.py` — how many hours/GB are on THIS drive (offline)
Scans a plugged-in SSD and reports its **video storage (GB)** and **footage hours**,
measured by ffprobe. Works fully offline — no internet, no password.

```bash
python3 check_drive_hours.py "/media/you/Extreme SSD"
python3 check_drive_hours.py                       # auto-detect plugged drives
py check_drive_hours.py "E:\"                       # Windows
```

---

## Quick reference

| Tool | Answers | Internet? | Password? | ffmpeg? |
|------|---------|-----------|-----------|---------|
| `check_cloud_hours.py` | hours/TB **in the cloud** | yes | yes | for exact bitrate |
| `check_ssd.py` | is this drive **backed up** to the cloud? | yes | yes | no |
| `check_drive_hours.py` | hours/GB **on this drive** | no | no | for exact hours |

## Notes
- Hours are measured from the real videos (ffprobe duration), which is why the cloud
  tool reads higher than the dashboard — the dashboard only counts runs that opm
  measured and keyed correctly, while these tools count all the actual video.
- Drives should be formatted **exFAT** for StarCap. Always "Safely Eject" before
  unplugging (especially from Windows) so the drive doesn't come up read-only elsewhere.
