#!/usr/bin/env bash
# Build cpuid.bin (the SRAM program) and cpuid_loader.bin (a MiniLoaderAll
# with it as the 0x471 stage). Needs gcc-aarch64-linux-gnu.
set -euo pipefail
cd "$(dirname "$0")"
CC=aarch64-linux-gnu-gcc
$CC -Os -ffreestanding -nostdlib -fno-pic -fno-stack-protector -mgeneral-regs-only \
    -mstrict-align -fno-builtin -Wall -Wextra -T cpuid.ld -Wl,--build-id=none \
    -o cpuid.elf start.S cpuid.c
aarch64-linux-gnu-objcopy -O binary cpuid.elf cpuid.bin
LOADER="${1:-../../../flashing/nanopi/emmc/nanopiIMAGE/rk3576-starcap-latest-20260930/MiniLoaderAll.bin}"
python3 -I mkloader.py "$LOADER" cpuid.bin cpuid_loader.bin
