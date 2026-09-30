#!/usr/bin/env bash
# pin-to-branch (hold a board on a specific recorder ref) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== pin-to-branch"
need_cmd sshpass sshpass
need_cmd ssh     openssh-client
need_cmd git     git
need_cmd rsync   rsync
install_deps
need_recorder_clone
link_cmd pin-to-branch "$HERE/pin_to_branch.sh"
