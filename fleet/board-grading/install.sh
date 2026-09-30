#!/usr/bin/env bash
# board-grading (fps grade + hardware fingerprint of a board) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== board-grading"
need_cmd python3 python3
need_cmd sshpass sshpass
need_cmd curl    curl
install_deps
chmod +x "$HERE/board_fingerprint.sh"
link_cmd measure-fps       "$HERE/measure_fps.py"
link_cmd board-fingerprint "$HERE/board_fingerprint.sh"
