#!/usr/bin/env python3
"""
The cpu_id U-Boot derives from the 16 cpu-id OTP bytes (board.c
rockchip_set_serialno), for checking the payload against a healthy board:

    ssh pi@<board> "sudo dd if=/sys/bus/nvmem/devices/rockchip-otp0/nvmem bs=1 skip=10 count=16 2>/dev/null | xxd -p"
    ./serial_from_otp.py <that hex>      # must equal its /proc/device-tree/serial-number

and the OTP= line the payload prints must equal the same hex.
"""
import sys
import zlib


def crc32_no_comp(crc, data):
    # zlib inverts before and after; U-Boot's crc32_no_comp does neither.
    return ~zlib.crc32(data, ~crc & 0xFFFFFFFF) & 0xFFFFFFFF


def serial(cpuid):
    low = bytes(cpuid[1 + 2 * i] for i in range(8))
    high = bytes(cpuid[2 * i] for i in range(8))
    lo = crc32_no_comp(0, low)
    return "%x" % ((crc32_no_comp(lo, high) << 32) | lo)


if __name__ == "__main__":
    print(serial(bytes.fromhex(sys.argv[1])))
