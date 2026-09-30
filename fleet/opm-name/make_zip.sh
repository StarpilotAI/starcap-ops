#!/usr/bin/env bash
# Build the standalone opm-name zip — what you send to someone else's laptop.
# Ships the app + its one external script (tools/vnc_into_pi.sh, which lives a
# directory up in this repo) + the installer. Deliberately NOT shipped:
# board_registry.json (this station's own board log) and any credentials.
#
#     ./make_zip.sh [output-dir]        # default: ~/Desktop
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

OUT_DIR="${1:-$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/opm-name/web" "$STAGE/opm-name/tools"
cp opmname.py webui.py start_opm_name.sh install.sh README.md "$STAGE/opm-name/"
cp web/index.html "$STAGE/opm-name/web/"
cp ../vnc/vnc_into_pi.sh "$STAGE/opm-name/tools/"
cp ../../station/setup_dock_lan.sh "$STAGE/opm-name/"
chmod +x "$STAGE/opm-name/"{install.sh,start_opm_name.sh} "$STAGE/opm-name/tools/vnc_into_pi.sh"

ZIP="$OUT_DIR/opm-name.zip"
rm -f "$ZIP"
(cd "$STAGE" && zip -qr "$ZIP" opm-name)
echo "built: $ZIP"
unzip -l "$ZIP"
