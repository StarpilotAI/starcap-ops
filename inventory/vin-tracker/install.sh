#!/usr/bin/env bash
# vin-tracker (eMMC speed per vin board + live CSV view) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== vin-tracker"
need_cmd python3  python3
need_cmd sshpass  sshpass
need_cmd fuser    psmisc
need_cmd xdg-open xdg-utils
install_deps
chmod +x "$HERE"/*.sh "$HERE/csv_viewer.py"
link_cmd vin-check "$HERE/vin_check.sh"
launcher vin-tracker "Vin board tracker" "$HERE/start_csv_viewer.sh" x-office-spreadsheet \
  "Live, editable view of pi_emmc_speeds.csv"
