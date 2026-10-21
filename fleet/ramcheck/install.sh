#!/usr/bin/env bash
# RAM Check installer: app-menu + Desktop launcher, and a udev rule so USB-TTL
# adapters are readable without sudo (the laptop user is not in `dialout`).
#
#   ./install.sh           install, or re-install after moving the checkout
#   ./install.sh remove    take the launcher and the udev rule back off
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== ramcheck"

RULE=/etc/udev/rules.d/60-starcap-usb-ttl.rules

if [ "${1:-}" = "remove" ]; then
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/ramcheck.desktop" \
        "$(_desktop_dir)/ramcheck.desktop"
  [ -f "$RULE" ] && sudo rm -f "$RULE" && sudo udevadm control --reload-rules
  echo "  removed."
  exit 0
fi

need_cmd python3 python3
install_deps
chmod +x "$HERE/run.sh" "$HERE/ramcheck.py"

# CH340, CH9102, CH341, CP210x, FTDI and PL2303: the common 3.3 V USB-TTL adapters.
if ! grep -q '2207' "$RULE" 2>/dev/null; then
  echo "  udev: making USB-TTL adapters readable without sudo ($RULE)"
  sudo tee "$RULE" >/dev/null <<'RULES'
# Written by starcap-ops/fleet/ramcheck/install.sh: USB-TTL adapters for board debug UARTs.
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="5523", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="2303", MODE="0666"
# Rockchip boot ROM / loader (maskrom), so RAM Check can send cpuid/ without sudo.
SUBSYSTEM=="usb", ATTR{idVendor}=="2207", MODE="0666"
RULES
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=tty --action=add
else
  echo "  udev: rule already present"
fi

launcher ramcheck "RAM Check" "$HERE/run.sh" utilities-system-monitor \
  "Live NanoPi debug-UART log with a RAM verdict and the board's cpu_id"
