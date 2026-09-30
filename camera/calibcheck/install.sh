#!/usr/bin/env bash
# calibcheck (are these cameras calibrated and uploaded?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== calibcheck"
need_cmd python3 python3
install_deps
chmod +x "$HERE/run.sh" "$HERE/check.py"
[ -f "$HERE/cameras.txt" ] || printf '# one camera number per line\n' > "$HERE/cameras.txt"
launcher CalibCheck "Calibration Upload Check" "$HERE/run.sh" emblem-default \
  "Check how many cameras in cameras.txt are calibrated and fully uploaded"
[ -f "$HOME/.config/starcap/dashboard_password" ] || [ -n "${STARCAP_PASSWORD:-}" ] \
  || echo "  note: set STARCAP_PASSWORD or ~/.config/starcap/dashboard_password"
