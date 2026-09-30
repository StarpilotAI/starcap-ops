#!/usr/bin/env bash
# sd-format (wipe an SD card to plain ext4 storage) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== sd-format"
need_cmd parted     parted
need_cmd mkfs.ext4  e2fsprogs
need_cmd wipefs     util-linux
need_cmd udisksctl  udisks2
install_deps
link_cmd format-sd-storage "$HERE/format_sd_storage.sh"
