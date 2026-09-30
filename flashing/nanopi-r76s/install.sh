#!/usr/bin/env bash
# nanopi-r76s flashing (SD + eMMC over maskrom) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== nanopi-r76s"
need_cmd sgdisk    gdisk
need_cmd partprobe parted
need_cmd resize2fs e2fsprogs
need_cmd unzip     unzip
need_cmd zstd      zstd
need_cmd simg2img  android-sdk-libsparse-utils
need_cmd lsusb     usbutils
need_cmd sshpass   sshpass
install_deps
chmod +x "$HERE"/*.sh "$HERE/assets/upgrade_tool_v2.44_for_linux/upgrade_tool"
echo "  upgrade_tool v2.44 ships in assets/ (closed-source x86-64, no apt package)"
echo "  images are NOT in git: drop the image package in nanopiIMAGE/ or pass its path"
