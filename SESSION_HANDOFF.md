# Session handoff — StarCap fleet ops work

Context for picking this up in a new session/tool. Repos involved:
- `~/starcap-ops` (private repo, `github.com/waleedatef45/starcap-ops`) — opm-lan + tools
- `~/starcap-dashboard` (team repo, `github.com/StarpilotAI/starcap-dashboard`) — Next.js dashboard + `orangepi-manager` (opm, SD-card tool)
- `~/orangepi-recorder` (team repo, `github.com/StarpilotAI/orangepi-recorder`) — the recorder firmware that runs on every pi

All three are real git repos on this laptop with working credentials (SSH keys already work — pushes succeeded all session). Everything described below is **already committed and pushed** unless marked "IN PROGRESS" at the bottom.

## 1. opm-lan VNC feature (`starcap-ops`)
- `tools/vnc_into_pi.sh <ip>` — one-shot script: installs/starts `x11vnc` on a pi (handles both a logged-in desktop and the bare LightDM greeter), works around this fleet's Mali/Panfork GPU driver needing a DRM-forced HDMI connector to render anything (`/sys/kernel/debug/dri/*/HDMI-*/force`), opens TigerVNC fullscreen, and on close undoes everything on the pi (kills x11vnc, un-forces HDMI). Lock file per-IP in `/tmp/vnc_into_pi/` so a second launch for the same IP no-ops.
- Wired into `opm-lan/webui.py` as `POST /api/add` — sorry, as `POST /api/vnc` — and a `🖥️ VNC` button in `opm-lan/web/index.html` per online recorder, launched as a detached background process.
- Root cause of "black screen, just a moving cursor" over VNC: GNOME Shell's compositor only renders once the kernel sees a real HDMI hotplug (confirmed by plugging in a real monitor once). The debugfs `force` trick fixes it without hardware.
- **Known constraint**: don't force `DP-*` connectors too — it made Xorg build a bogus combined extended layout and crashed x11vnc (signal 11). HDMI only.
- Fixed a real timing bug: the startup check used a fixed 2s sleep before `pgrep x11vnc`, which occasionally raced x11vnc's own `-bg` daemonize fork under load → false "failed to start". Now polls up to 5s in 0.5s steps.
- Also fixed: `xdg-open`'s Firefox launch (in both `vnc_into_pi.sh` and `opm-lan/start_opm_lan.sh`) wasn't detached from the terminal session — closing the terminal sent Firefox `SIGHUP` and killed the **entire browser**, not just the opened tab. Fixed with `setsid ... < /dev/null > /dev/null 2>&1`.

## 2. opm (SD-card tool, in `starcap-dashboard/orangepi-manager`) — ported fixes from opm-lan
Three real bugs fixed, all live-tested against real hardware (3 real SD cards + a real reader):
1. **Copy progress inflation**: `sampler_loop` computed `done = opm.dir_stats(dest)[0]` — the WHOLE destination folder, so a pre-existing copy made new jobs look done immediately. Fixed to sum only the current job's session paths (matching opm-lan's existing correct pattern).
2. **Contact-sheet status using stale per-pass counters**: ported opm-lan's `analysis_rollup()` (cumulative truth from on-disk markers, not the sweep's per-pass `analyzed`/`reported` which reset every pass).
3. **"Add connected devices" mid-copy re-added cards already copying**: `opm.detect_cards()` assigned slot numbers by **sort position** in the current scan (recomputed every call), so inserting a new card could shift already-copying cards' slot numbers, making them look "new". Fixed with a persistent slot-assignment cache keyed by USB by-path (`opm.py`'s new `_slot_for()`), matching the pattern opm-lan already used for IP-based stability. Also added a picker modal so "Add connected devices" lets you choose which detected cards to add instead of blindly adding all.

Needed Node.js/npm installed on this laptop (wasn't there before) to type-check/build-verify — `sudo apt install nodejs npm` (18.19.1), `npm install` in `starcap-dashboard/dashboard/`.

## 3. Connection-type feature (the big one — recorder + dashboard)
**Ask**: show which link (ethernet dock / WiFi dongle / USB modem) a recorder is actually using, on both the Devices card and the heartbeat log — previously only an IP was shown.

### Recorder side (`orangepi-recorder`)
- New `connection_info()` in `stereocam/uploader.py`. Went through **three iterations, all live-tested on starcap12**:
  1. v1 (WRONG): carrier-scan, ethernet > wifi > modem priority.
  2. v2 (WRONG): fixed priority to ethernet > modem > wifi (matching the REAL route-metric priority `WifiWatchdog` pins fleet-wide in `app.py`'s `_ensure_ethernet_priority()`: `ETH_METRIC=100 < USB_ETH_METRIC=400 < WIFI_METRIC=700`) — but still just a carrier scan + assumed ordering.
  3. **v3 (CORRECT, current)**: asks the kernel directly via `ip route get 8.8.8.8` (same question `lan_ip()` already answers via a UDP-connect trick, so the shown connection type always agrees with the shown IP) and classifies the returned interface. Falls back to the carrier-scan guess only if the route lookup fails outright (genuinely offline).
  - **Why v1/v2 were wrong in practice**: carrier=1 only means a link is UP, not that it has a working route — and two independent snapshots (the main heartbeat vs the separate `HeartbeatLogger` heartbeat, different timers) could show DIFFERENT results if carrier state flickered between them. Observed live: card said "USB modem", heartbeat log said "StarPilot-5G" for the same device near the same moment.
- Wired into both heartbeat paths: `_cloud_report()` (main heartbeat → Devices card) and `HeartbeatLogger` in `app.py` (the heartbeat log).
- **VERSION stayed at 1.23.2** for all three iterations (deliberate — no other fleet pi had picked up 1.23.2 yet when the later fixes landed, so no version churn). Only bumped once, 1.23.0 → 1.23.1 → 1.23.2 (that 1.23.2 bump was the "fix priority order" commit; the "use real routing table" commit did NOT bump again, per explicit instruction).
- **A real deployment mistake happened and was caught+fixed**: I manually SCP'd `uploader.py` to starcap12 multiple times but forgot `app.py` after the very first pass, so the heartbeat LOG kept showing stale data (missing `connection_type`) even though the CARD (fed by `uploader.py` alone) was correct. This was purely an out-of-band manual-SSH-patch mistake — the **real self-update mechanism can't have this bug**, since it downloads a full tarball of `main` and overlays every file atomically (`updater.py`'s `_extract()`), never per-file.
- Self-update only fires over a trusted link (wired ethernet or the `StarPilot-5G` WiFi — `update_link_ok()`), never modem/other WiFi.
- Commits on `orangepi-recorder` main: `5e015b8` (v1, +VERSION 1.23.1), `48684d9` (v2 priority fix, +VERSION 1.23.2), `93ede9b` (v3 ip-route-based fix, no version bump).

### Dashboard side (`starcap-dashboard`)
- `DeviceDoc` type (`store.ts`) + `mergeDeviceDocs()` carry `connection_type`/`connection_name` through.
- `/api/ingest/heartbeat` accepts + stores the two new fields (was previously a strict allowlist that silently dropped anything unrecognized — this is why just changing the recorder alone wouldn't have been enough).
- `/api/devices` exposes them; `/api/devices/[id]` already did via its `...doc` spread.
- Devices page card: 🔌/📶/📱 icon + name next to the IP.
- Heartbeat log row detail (`DeviceLog.tsx`'s `hbDetail()`): shows `connection_name`, falls back to the old `wifi` field for pre-existing events.
- Verified end-to-end locally before deploying: installed Node here, `npx tsc --noEmit` (clean) + `npm run build` (clean), then ran `npm run dev` against a `.env.local` pointed at the **real production Redis** (already existed on this laptop, had to add the pi's actual `dashboard_password` — `starchild_friend`, found in `~/orangepi-recorder/stereocam/dashboard_password` on the pi — as an additional comma-separated value in `.env.local`'s `STARCAP_PASSWORD`, since the ingest auth guard requires an exact match and the pi's password differed from what was already in `.env.local`).
- Also briefly redirected starcap12's `DASHBOARD_URL` (via a temporary systemd drop-in, `/etc/systemd/system/stereocam.service.d/override.conf`) to this laptop's local dev server for a real continuous-heartbeat test, then reverted it back to production (`https://starcap-dashboard.vercel.app`, the code default) afterward. **Confirmed reverted** — don't need to redo this.
- Commit on `starcap-dashboard` main: `21cd56a`.

## 4. Misc
- `orangepi-manager`'s `.gitignore` now excludes `.venv/` (start.sh creates one; wasn't ignored before — commit `623bd46`).
- Desktop shortcuts created on this laptop (`~/Desktop/opm-lan.desktop`, `~/Desktop/opm-sdcard.desktop`) launching `opm-lan/start_opm_lan.sh` and `starcap-ops/tools/launch_opm_sdcard.sh` respectively — not committed anywhere (they're just local `.desktop` files, correctly so).
- `~/starcap-dashboard/dashboard/.env.local` has real Redis creds + two comma-separated `STARCAP_PASSWORD` values (gitignored, don't touch).

## 5. NEW FINDING (10.77.0.12, unrelated pi) — real architectural bug, not yet fixed
While debugging why `10.77.0.12` kept falling off the dashboard's live grid even while
docked on ethernet: found this pi has a systemd drop-in
(`/etc/systemd/system/stereocam.service.d/*.conf`) with `Environment=WIFI_WATCHDOG=0`.
This disables `WifiWatchdog.run()` entirely — and since `_ensure_ethernet_priority()`
(the ETH_METRIC/USB_ETH_METRIC pinning fix, `app.py` line ~1836) is called *inside*
that same gated loop, disabling WiFi watchdog **also silently disables ethernet-vs-
modem route-metric pinning**, even though they're unrelated. Result: with a USB modem
also plugged in (even a dead one with no SIM), both `eth0` and the modem's interface
end up with the SAME route metric (100), so the kernel picks either one arbitrarily
per-connection — explaining the intermittent "wedged"/12s-timeout heartbeat failures
and the pi randomly vanishing from the dashboard.

**Live-reproduced and confirmed**: watched `ip route show default` before/after
reconnecting a SIM-less modem — modem's route came up at metric 100 (tied with eth0),
and after 20s+ waiting, zero `[wifi]`-prefixed log lines ever appeared in the journal
(confirmed via `journalctl -u stereocam | grep -i wifi` — completely empty history),
consistent with the entry guard (`if not WIFI_WATCHDOG or not self.networks: return`)
firing before any of the loop body (including the try/except that would otherwise
print `"[wifi] watchdog: <error>"`) ever runs.

**Not fixed yet** — options, pick up here:
1. Quick/local: just remove that pi's systemd override (`rm /etc/systemd/system/stereocam.service.d/*.conf && systemctl daemon-reload && systemctl restart stereocam`) — but figure out WHY someone disabled WIFI_WATCHDOG on this specific pi first, in case it was intentional (maybe wifi radio issues, maybe a test).
2. Proper/fleet-wide: this is a real code smell in `orangepi-recorder/stereocam/app.py` — `_ensure_ethernet_priority()` should NOT be gated behind `WIFI_WATCHDOG`/`self.networks`, since it's about ethernet-vs-USB-modem priority, not WiFi. Move it to its own always-on loop/thread, or at minimum call it before the `if not WIFI_WATCHDOG or not self.networks: return` early-exit. Any other fleet pi with WIFI_WATCHDOG disabled for any reason has this exact same latent bug.
3. Immediate mitigation for `10.77.0.12` specifically (no code change): just don't plug in the modem (it has no SIM anyway, provides zero value, and actively causes this). Confirmed ethernet-only works fine on this pi once the earlier DNS-hijack/wedge mess was cleared.

## 5b. UPDATE — section 5's bug fully diagnosed + fixed on 10.77.0.12 (but NOT in code yet)
Picked back up and fully resolved on the live pi. Two separate fixes, BOTH persistent
(survive reboot):

1. **Removed** `/etc/systemd/system/stereocam.service.d/*.conf` (the `WIFI_WATCHDOG=0`
   override) + `daemon-reload` + restart. Confirmed watchdog now runs and correctly
   pins metrics: `journalctl` showed `[wifi] ethernet Wired connection 1: metric 100`
   and `[wifi] USB modem/dongle Wired connection 2: metric 400` — no longer tied.
   **This was a pi-specific manual override, not present on other fleet pis by default**
   (code defaults `WIFI_WATCHDOG` to enabled).

2. **New second bug found**: even with metrics correctly unt tied, the pi STILL
   couldn't reach the dashboard (`curl → HTTP 000`, journal showed the same
   `DNS/connect wedged` error). Root cause: `getent hosts starcap-dashboard.vercel.app`
   resolved to a fake `192.168.50.1` — DNS itself was still poisoned. `/etc/resolv.conf`
   listed the modem's hijacking DNS server (`192.168.0.1`) BEFORE ethernet's real one
   (`10.77.0.1`), and resolvers stop at the first server that answers (even wrongly) —
   `_ensure_ethernet_priority()` in `app.py` only ever pins `ipv4.route-metric`, it
   NEVER touches `ipv4.dns-priority`. Fixed live via:
   `nmcli connection modify 'Wired connection 1' ipv4.dns-priority -100` (negative =
   NetworkManager uses ONLY that connection's DNS servers, excludes the modem's
   entirely) + `nmcli connection up 'Wired connection 1'`. Confirmed:
   `resolv.conf` now shows only `10.77.0.1`, DNS resolves real IPs, dashboard
   `curl → HTTP 200`, and the pi successfully round-tripped with the dashboard
   (`[cloud] remote record command: stop` seen in the journal — proof the heartbeat
   is landing and the dashboard is responding).

   **THIS ONE IS A REAL FLEET-WIDE CODE BUG, not pi-specific** — every pi runs the
   same `_ensure_ethernet_priority()` that never sets DNS priority, so ANY pi that
   ever has a USB modem plugged in alongside ethernet, where the modem's DNS
   misbehaves (dead SIM / captive portal, like this one), would hit the identical
   DNS-poisoning symptom. **Not yet fixed in the shared code** — `orangepi-recorder`
   still needs `_ensure_ethernet_priority()` in `app.py` (~line 1714) updated to also
   set `ipv4.dns-priority` (e.g. a negative value) on the onboard NIC's connection,
   mirroring the route-metric pinning it already does. This is the next real
   follow-up worth doing, on top of item 5's WIFI_WATCHDOG/ethernet-priority coupling
   fix (both are in the same function/area of app.py, could be done together).

## 6. Also done: gemini-cli now works on this laptop
`node`/`npm` via apt was only v18.19.1 — too old for `@google/gemini-cli` (needs
`>=20`, and it actually crashed on 18 with a SyntaxError on startup, not just a
warning). Fixed via `nvm` (installed to `~/.nvm`, doesn't touch the system apt Node
18 that `starcap-dashboard`'s build depends on): `nvm install 20`, then
`npm install -g @google/gemini-cli` under nvm's Node. Confirmed working:
`gemini --version` → `0.49.0` on Node `v20.20.2`. To use it in a fresh shell:
`source ~/.nvm/nvm.sh` first (or just open a new terminal — nvm's init should be in
`~/.bashrc` already from the install script).

## 7. IN PROGRESS — not finished, pick up here
**Task**: user tried the VNC button against a DIFFERENT pi, `10.77.0.12`, and it failed with `x11vnc: command not found`. Root cause found: this pi's `/etc/apt/sources.list` points **exclusively at `repo.huaweicloud.com/ubuntu-ports`**, and that mirror is dead — confirmed via `curl -o /dev/null -w '%{http_code}' http://repo.huaweicloud.com/ubuntu-ports/dists/jammy/Release` → **404**. This silently broke `apt-get update` inside `vnc_into_pi.sh`'s install step (`apt-get update -qq && apt-get install -y x11vnc` — the `&&` meant install never even ran once update failed), so the script barreled ahead trying to run a binary that was never installed.

**Already checked**: the OFFICIAL Ubuntu ports mirror (`ports.ubuntu.com/ubuntu-ports/dists/jammy/Release`) **ALSO returned 404** when tested from that pi — so it's not simply "swap to the official mirror," something else is going on (maybe a network/DNS/proxy quirk on that pi's link, or jammy dists layout differs, worth double checking with `curl -v` for more detail, or trying a different well-known mirror e.g. `mirror.kernel.org` or one of Ubuntu's regional mirrors, or checking if this pi can reach the internet at all right now beyond just these two hosts).

**Next steps**:
1. Re-verify basic internet connectivity from 10.77.0.12 (`curl -sI https://google.com` or similar) to rule out a general connectivity problem vs. mirror-specific issue.
2. Find a working Ubuntu jammy ports mirror reachable from this pi, fix `/etc/apt/sources.list` (currently 100% `repo.huaweicloud.com`), re-run `apt-get update`.
3. Once apt works, re-run `./tools/vnc_into_pi.sh 10.77.0.12` from this laptop to confirm x11vnc installs and VNC works end to end (same verification pattern used for starcap12 earlier).
4. **Worth flagging to the user**: this dead Huawei mirror is probably baked into the base OS image for the WHOLE FLEET (explains why these boards also default to `Asia/Shanghai` timezone — image likely built in China). If so, EVERY pi that hasn't had apt used on it recently may hit this same wall the next time anyone tries to install something fresh via SSH. Worth asking whether to fix the base image / push a fleet-wide sources.list fix via the recorder repo's provisioning, or just fix pi-by-pi as needed.

Nothing else is broken or pending — items 1–4 above (`SESSION_HANDOFF.md` section 5) are the only open thread.
