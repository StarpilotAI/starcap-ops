# RAM Check

Live view of a NanoPi R76S debug UART, with a RAM verdict for every power-on and
the board's cpu_id. Use it on any R76S that won't boot, keeps resetting, or "stops
recording": the RAM program prints its verdict in the first second of a normal
power-on, before anything else can go wrong.

```bash
./install.sh        # once: launcher + udev rule so the adapter needs no sudo
./run.sh            # http://127.0.0.1:8776 (or the "RAM Check" launcher)
./run.sh --replay logs/<file>.log    # verdict for a saved log, no serial needed
```

## Wiring

A 3.3 V USB-TTL adapter (CH340 and similar) on the three holes on the board's
underside, next to the USB-C, silkscreened **RX / TX / GND**, at 1500000 8N1.

| board | adapter | why |
|---|---|---|
| TX | RX | the boot log (required) |
| GND | GND | required |
| RX | TX | optional: lets the tool log in (`pi`/`12345`, then `root`/`fa`) and read the cpu_id from a board that boots |

Then pull the board's power and plug it back in. Do a normal boot, not maskrom:
holding MASKROM skips the RAM check, so nothing prints.

## Reading the verdict

| verdict | what the log showed | meaning |
|---|---|---|
| **RAM OK, booted to Linux** | `LPDDR5, 2736MHz`, both `channel[n] ... Size=2048MB`, `out`, U-Boot `DRAM: 4 GiB`, a login prompt | RAM is fine |
| **FAULTY RAM**: chip not detected | `unknown device` + `it may be ch0 soldering abnormality`, twice, then a hang | the RAM program can't reach the chip (161, 138, 163) |
| **FAULTY RAM**: data line | `ch:0 dq15 fail,write:0x8000,read:0x9000` | one data line is stuck or bridged; the tool names the bit (95: ch0 DQ12) |
| **RAM OK at boot, then CRASHED** | a panic / Oops / SError / Bad page state after boot | the intermittent kind (starcap112): soak it before trusting it |
| **Power seen, but no boot log** | only a few garbage bytes | check board TX to adapter RX, not maskrom, or a board dead before the RAM check |

The RAM program retries once after power-cycling the RAM (`pd/pu vd_ddr`), so a
real fault fails twice in one power-on. FAULTY RAM is a hardware fault on the board
(the RAM chip or its solder joints): reflashing can't fix it, because a flash runs
the same RAM program first.

## Where the cpu_id comes from

- **A board that boots** reports it: the kernel's `Serial : …` line, or the
  serial-console login reading `/proc/device-tree/serial-number`, the same value
  the recorder uses.
- **A board that dies at the RAM check** never gets that far. Type the number on
  its sticker and the tool looks it up in `../opm-name/board_registry.json` and the
  tracker database (read-only), with its camera and when it was last seen. If the
  board reports a cpu_id that belongs to a different name, the page warns you: the
  sticker or the registry is wrong.

Every power-on is saved in `logs/` (gitignored) as `<time>_<name>.log` + `.json`.
