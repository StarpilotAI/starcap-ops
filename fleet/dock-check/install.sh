#!/usr/bin/env bash
# dock-check (is a docked board current, auto-updating and on main?) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== dock-check"
need_cmd python3 python3
need_cmd sshpass sshpass
need_cmd ssh     openssh-client
need_cmd git     git
install_deps
need_recorder_clone
link_cmd dock-check "$HERE/dock_check.py"
