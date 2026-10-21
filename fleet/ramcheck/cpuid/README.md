# cpuid: read a dead board's cpu_id in maskrom

A board whose RAM is dead can't boot far enough to say who it is, and with no
label and no SD card there is nothing else to go on. This makes the chip itself
print its cpu_id on the debug UART, the same value the recorder reports and the
tracker/registry know it by:

```
CPUID=ed013a06c1afc69f
OTP=<the 16 raw cpu-id fuse bytes>
```

RAM Check shows it as **ID READ** and looks the board up by that cpu_id.

**Status: PROVEN on hardware 2026-10-21.** First run, on starcap163 (dead RAM:
"unknown device"), printed `CPUID=ed013a06c1afc69f`, exactly the cpu_id the
tracker holds for starcap163, and `OTP=4e5948383900000000000000000f0a0b`, which
`serial_from_otp.py` turns into the same value. That board ran its failing RAM
check on a normal power-on and then fell into maskrom by itself, so the button
may not even be needed on a dead-RAM board.

## How it works

In maskrom the boot ROM accepts a first program over USB (request 0x471) and
runs it from on-chip SRAM. Normally that is the DDR blob, which is why its
output appears even on boards with dead RAM. Here it is `cpuid.c` instead:

1. sets up UART0 at 1500000 exactly as the stock DDR blob does (same clock,
   pin and UART register writes, copied from its disassembly),
2. reads the cpu-id fuses: non-secure OTP at 0x2a580000, byte 0x0a, 16 bytes
   (rk3576.dtsi `id@a`; U-Boot `CFG_CPUID_OFFSET 0xa`, `rockchip_rk3588_otp_read`
   with `ns_offset 0x1c0`; the DDR blob uses the same OTP sequence),
3. derives the serial like U-Boot's `rockchip_set_serialno()`: crc32 of the
   odd bytes, then crc32 of the even bytes seeded with the first, printed `%llx`,
4. prints it three times and returns to the boot ROM. Nothing is written.

`mkloader.py` swaps it into a copy of the stock `MiniLoaderAll.bin` and rebuilds
the RKNS `UsbHead` hashes (a changed image under the old header is silently
refused). `mkloader.py --selftest <stock>` proves the rebuild reproduces the stock
header and loader byte for byte.

## Use

```bash
./build.sh                 # needs gcc-aarch64-linux-gnu; writes cpuid_loader.bin
# serial cable on, RAM Check open; board: hold MASKROM, power on, A-to-A cable
sudo ./send.sh
```

`upgrade_tool` then reports a failure on the 0x472 stage (there is no RAM for it):
expected. Power-cycle the board afterwards.

## Validating on a healthy board (do this once)

```bash
ssh pi@<board> cat /proc/device-tree/serial-number        # the known cpu_id
ssh pi@<board> "sudo dd if=/sys/bus/nvmem/devices/rockchip-otp0/nvmem bs=1 skip=10 count=16 2>/dev/null | xxd -p"
./serial_from_otp.py <that hex>                            # must print the same cpu_id
```

Then the maskrom run must print that cpu_id, and an `OTP=` equal to that hex.

## If the first run fails

| RAM Check shows | likely cause |
|---|---|
| nothing at all | the UART setup differs from the blob's path on this board, or the boot ROM refused the header (`upgrade_tool` then fails at 0x471 itself) |
| `CPUID_ERROR=otp-timeout` | the OTP block needs a clock or a different read sequence at this stage |
| a CPUID that doesn't match | wrong fuse offset or derivation: compare `OTP=` with the nvmem bytes above |
