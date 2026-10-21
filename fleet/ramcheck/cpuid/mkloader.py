#!/usr/bin/env python3
"""
Make a maskrom loader whose 0x471 stage is our SRAM program instead of the DDR
blob, from a stock RK3576 MiniLoaderAll.bin.

    mkloader.py STOCK_LOADER PAYLOAD OUT        build
    mkloader.py --selftest STOCK_LOADER         prove the rebuild is byte-exact

Two layers, both checked by the selftest against the stock file:

  LDR file ("LDR " header, 0x39-byte entries, data, then a Rockchip CRC32 of
  everything before it: poly 0x04c10db7, MSB first, init 0, no final xor).

  UsbHead, the first 0x471 entry: an RKNS v2 header (u-boot tools/rkcommon.c,
  struct header0_info_v2). The boot ROM checks it:
    +0x78  image entries, 0x58 bytes each: size_and_off = sectors << 16 | offset
           in sectors from the header; +0x18 in each entry = sha256 of the image
    +0x600 sha256 of header[0:0x600]
  Image 0 is what 0x471 loads (normally the DDR blob), image 1 is what 0x472
  loads (usbplug). A changed image sent under the old header is refused
  silently, which is the trap this rebuild avoids.
"""
import hashlib
import struct
import sys

ENTRY = 0x39
IMG0 = 0x78
IMG_SZ = 0x58


def rkcrc32(data, crc=0):
    tab = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C10DB7) if c & 0x80000000 else (c << 1)
            c &= 0xFFFFFFFF
        tab.append(c)
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ tab[((crc >> 24) ^ b) & 0xFF]
    return crc


def parse(d):
    if d[:4] != b"LDR ":
        raise SystemExit("not a Rockchip LDR loader")
    hsize = struct.unpack_from("<H", d, 4)[0]
    off = 4 + 2 + 4 + 4 + 7 + 4
    groups = []
    for _ in range(3):                      # 471, 472, loader
        cnt, eoff, esz = struct.unpack_from("<BIB", d, off)
        off += 6
        ents = []
        for i in range(cnt):
            e = eoff + i * esz
            doff, dsz, _ = struct.unpack_from("<III", d, e + 45)
            ents.append({"at": e, "data": d[doff:doff + dsz]})
        groups.append(ents)
    if rkcrc32(d[:-4]) != struct.unpack("<I", d[-4:])[0]:
        raise SystemExit("stock loader CRC mismatch: not a clean MiniLoaderAll")
    return hsize, groups


def usbhead(template, img0, img1):
    h = bytearray(template)
    if h[:4] != b"RKNS":
        raise SystemExit("first 0x471 entry is not an RKNS header")
    s0, s1 = len(img0) // 512, len(img1) // 512
    struct.pack_into("<I", h, IMG0, (s0 << 16) | 4)
    struct.pack_into("<I", h, IMG0 + IMG_SZ, (s1 << 16) | (4 + s0))
    for i, img in enumerate((img0, img1)):
        at = IMG0 + i * IMG_SZ + 0x18
        h[at:at + 64] = hashlib.sha256(img).digest().ljust(64, b"\0")
    h[0x600:0x620] = hashlib.sha256(bytes(h[:0x600])).digest()
    return bytes(h)


def build(d, new471):
    """Same header and entries as `d`, with the 0x471 entries' data replaced."""
    hsize, groups = parse(d)
    groups[0] = [dict(e, data=x) for e, x in zip(groups[0], new471)]
    out = bytearray(d[:hsize])
    nents = sum(len(g) for g in groups)
    first = groups[0][0]["at"]
    if first != hsize:
        raise SystemExit("unexpected layout: entries do not follow the header")
    out += d[first:first + nents * ENTRY]
    pos = len(out)
    for g in groups:
        for e in g:
            rel = e["at"]                   # entry offset is the same in out
            struct.pack_into("<II", out, rel + 45, pos, len(e["data"]))
            pos += len(e["data"])
    for g in groups:
        for e in g:
            out += e["data"]
    out += struct.pack("<I", rkcrc32(out))
    return bytes(out)


def pad512(b):
    return b + b"\0" * (-len(b) % 512)


def main(argv):
    if argv[:1] == ["--selftest"]:
        d = open(argv[1], "rb").read()
        _, groups = parse(d)
        head, ddr = groups[0][0]["data"], groups[0][1]["data"]
        plug = groups[1][0]["data"]
        assert usbhead(head, ddr, plug) == head, "UsbHead rebuild differs from stock"
        assert build(d, [head, ddr]) == d, "loader rebuild differs from stock"
        print("selftest OK: UsbHead and the whole loader rebuild byte-for-byte")
        return 0
    stock, payload, out = argv
    d = open(stock, "rb").read()
    _, groups = parse(d)
    head = groups[0][0]["data"]
    plug = groups[1][0]["data"]
    img0 = pad512(open(payload, "rb").read())
    if len(img0) > 0x40000:
        raise SystemExit("payload too big for SRAM")
    new = build(d, [usbhead(head, img0, plug), img0])
    open(out, "wb").write(new)
    # Re-read our own output through the same checks.
    _, g = parse(new)
    h = g[0][0]["data"]
    assert h[0x600:0x620] == hashlib.sha256(h[:0x600]).digest()
    assert h[IMG0 + 0x18:IMG0 + 0x38] == hashlib.sha256(img0).digest()
    print(f"{out}: payload {len(img0)} bytes ({len(img0) // 512} sectors), loader {len(new)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
