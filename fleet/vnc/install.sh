#!/usr/bin/env bash
# vnc_into_pi (a docked board's live desktop) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== vnc"
need_cmd sshpass         sshpass
need_cmd ssh             openssh-client
need_cmd xtigervncviewer tigervnc-viewer
install_deps
link_cmd vnc-into-pi "$HERE/vnc_into_pi.sh"
