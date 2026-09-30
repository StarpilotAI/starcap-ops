#!/usr/bin/env bash
# flashing (NanoPi R76S + Orange Pi 5/5B, SD and eMMC) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
#
# Installs what the flash scripts shell out to, puts a `flashing` shortcut on the
# Desktop, and says which of the five big images are still missing (git does not
# carry them; ./fetch_images.sh gets them from the private bucket).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/install_common.sh"
echo "== flashing"
need_cmd sgdisk        gdisk
need_cmd partprobe     parted
need_cmd resize2fs     e2fsprogs
need_cmd wipefs        util-linux
need_cmd unzip         unzip
need_cmd zstd          zstd
need_cmd xz            xz-utils
need_cmd lsusb         usbutils
need_cmd rkdeveloptool rkdeveloptool
need_cmd simg2img      android-sdk-libsparse-utils
need_cmd sshpass       sshpass
install_deps
find "$HERE" -name '*.sh' -exec chmod +x {} +
chmod +x "$HERE/nanopi/emmc/assets/upgrade_tool_v2.44_for_linux/upgrade_tool" "$HERE/nanopi/rebuild/strip_overlay_xattrs.py"

DD="$(_desktop_dir)"
if [ "${NO_DESKTOP:-0}" != 1 ] && [ -d "$DD" ]; then
  if [ -L "$DD/flashing" ] || [ ! -e "$DD/flashing" ]; then
    ln -sfn "$HERE" "$DD/flashing"; echo "  desktop: $DD/flashing -> $HERE"
  else
    echo "  desktop: $DD/flashing exists and is not a link; left alone"
  fi
fi

missing="$(grep -v '^#' "$HERE/IMAGES.sha256" | while read -r _ bytes _ path; do
  [ -f "$HERE/$path" ] && [ "$(stat -c %s "$HERE/$path")" = "$bytes" ] || echo "    $path"; done)"
if [ -n "$missing" ]; then
  echo "  images missing (get them with ./fetch_images.sh after 'gcloud auth login'):"
  echo "$missing"
else
  echo "  images: all 5 present"
fi
