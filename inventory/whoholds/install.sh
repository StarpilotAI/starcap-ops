#!/usr/bin/env bash
# whoholds (camera custody history) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== whoholds"
need_cmd python3 python3
install_deps
link_cmd whoholds "$HERE/whoholds.py"
[ -f "$HOME/starcap-dashboard/dashboard/.env.local" ] \
  || echo "  note: reads UPSTASH_* from ~/starcap-dashboard/dashboard/.env.local (not present)"
