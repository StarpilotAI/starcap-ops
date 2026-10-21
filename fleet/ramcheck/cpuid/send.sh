#!/usr/bin/env bash
# Send the cpu_id program to ONE board in maskrom and let RAM Check show the
# result. Writes nothing to the board: it only runs from the chip's SRAM.
#
#   1. Serial cable on (board TX -> adapter RX, GND), RAM Check open.
#   2. Board unpowered, hold MASKROM, power on, release, A-to-A cable to this laptop.
#   3. sudo ./send.sh
#
# RAM Check then shows CPUID=<16 hex> and OTP=<raw bytes>. upgrade_tool will
# report a failure after that (the 0x472 stage has no RAM to run in); that is
# expected and harmless. Power-cycle the board afterwards.
set -euo pipefail
cd "$(dirname "$0")"
[ -f cpuid_loader.bin ] || ./build.sh
UT=../../../flashing/nanopi/emmc/assets/upgrade_tool_v2.44_for_linux
cd "$UT"
./upgrade_tool ld || true
exec ./upgrade_tool db "$OLDPWD/cpuid_loader.bin"
