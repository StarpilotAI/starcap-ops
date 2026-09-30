# opm-name — StarCap fleet renamer

Dock recorders over ethernet, watch them appear in a table, type a name next to
each one. Renaming writes to the dashboard's name store keyed by the board's
`cpu_id` (stable across reflashes), and the recorder self-syncs its own local
name from there on its next heartbeat — so one click renames it **on the device
and on starcap.dev** at once.

## Install (fresh laptop)

```bash
unzip opm-name.zip && cd opm-name
./install.sh              # app + dependencies + launcher
./install.sh --network    # ...and set the wired NIC up for docking recorders
```

Then start it from the desktop icon, the app menu, or `opm-name`. It serves
**http://127.0.0.1:8771**.

Use `--network` unless this laptop is already a copy station. Recorders get their
IP *from this laptop*, so the wired NIC has to share a subnet — `--network` sets
it to `10.77.0.1/24` and raises the ARP cache limits. That subnet is deliberate:
the default `10.42.0.1/24` collides with the recorders' own hotspot address and
this laptop then can't resolve them.

## What's in the table

| | |
|---|---|
| **Rename** | type a name, press enter |
| **🔊 / 💡** | make the board announce itself so you know which physical unit the row is — a double beep on boards with a buzzer, a flash of all three status LEDs on the ones without (NanoPi) |
| **🖥️** | open the board's live desktop over VNC |
| **🗑️** | delete all recordings on the board (for clearing test takes before it ships) |

The identify and clear buttons stop and restart the recorder service for a few
seconds; any in-flight take is finalized cleanly first.

Below the live table is the **board log** — every board this laptop has ever
seen, kept across restarts, so a recorder's last-known name, version, model and
boot medium are still on hand when it's *not* docked (or won't boot).

## Mixed fleet

The fleet is two kinds of hardware and the tool sorts out which is which by
itself — no setting to pick:

| | Orange Pi 5 | NanoPi R76S |
|---|---|---|
| login | `orangepi` / `orangepi` | `pi` / `12345` |
| identify | buzzer (🔊) | onboard status LEDs (💡) — no buzzer on this hardware |

Every SSH tries the known logins and remembers which one each board answered on.
Boards with their own login: `PI_LOGINS="user:pass,user:pass" opm-name`.

## Where a rename actually goes

A name is not stored on the board. Renaming POSTs the board's `cpu_id` to the
dashboard (`/api/ingest/starcap-name`), the dashboard writes it — Postgres, with
Redis mirrored — and the recorder copies it into its own
`~/.config/starcap/device_name` from the reply to its next heartbeat, a second or
two later. So one click renames it on the device **and** on the Devices page, and
the dashboard is the only writer.

Nothing to configure: it defaults to `https://starcap.dev` with the station
password the copy-station tools already use, and on a copy station it picks up
opm-lan's own `dashboard_url` / `password`. To point it somewhere else, write
`~/.config/opm-name/env`:

```
STARCAP_DASHBOARD_URL=https://…
STARCAP_PASSWORD=…
```

If the laptop cannot reach the dashboard, the page says so in a banner and
renaming fails loudly — everything else (seeing docked boards, identifying, VNC,
clearing recordings) still works. Older installs may still have
`UPSTASH_REDIS_REST_*` in that env file; those are no longer read.

**Do not go back to writing the name store directly.** It was one `HSET` into
Upstash and that used to be the whole rename, but names moved to Postgres: the
write still succeeded, nothing read it, and the board never got the name — so a
rename silently did nothing and had to be redone by hand in the dashboard.

## Nothing shows up?

1. **Give it ~3 minutes.** A recorder takes that long from power-on to answering
   over the wire. Most "it's not showing up" is impatience.
2. `ip -br addr show <wired-iface>` — want `10.77.0.1/24`. If it says anything
   else, the wired NIC isn't sharing: `./install.sh --network`, or
   `nmcli connection up opm-lan`.
3. **Another network profile stealing the port.** This is the one that wastes an
   afternoon. Docking a recorder bounces the ethernet carrier, and on every
   bounce NetworkManager re-picks the highest-priority autoconnect profile for
   that NIC. If a plain DHCP-client profile wins, the shared subnet is gone and
   the recorder can never get an IP — it just never appears.

   ```bash
   nmcli -f NAME,TYPE,DEVICE,ACTIVE connection show     # who's on the wired NIC?
   nmcli -f connection.autoconnect-priority connection show opm-lan
   ```

   `install.sh --network` sets this up correctly (dock profile at priority 100,
   every other ethernet profile's autoconnect off), so it only bites on a laptop
   set up by hand.
4. Check the recorder got a lease at all:
   `journalctl -u NetworkManager -n 50 | grep DHCPACK`
