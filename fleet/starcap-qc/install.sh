#!/usr/bin/env bash
# StarCap QC installer: dependencies, the `starcap-qc` command, and the desktop icon.
#
#   ./install.sh          install, or re-install after moving the checkout
#   ./install.sh remove   take the launcher and the command back off
#
# The launcher points at THIS checkout (like starcapfinder), and the app imports
# opm-name and dock-check from their folders next to it, so a `git pull` updates all
# three at once. Re-run this after moving the checkout: the .desktop needs absolute paths.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== starcap-qc"

if [ "${1:-}" = "remove" ]; then
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/starcap-qc.desktop" \
        "$(_desktop_dir)/starcap-qc.desktop" "$HOME/.local/bin/starcap-qc"
  echo "  removed."
  exit 0
fi

need_cmd python3 python3
need_cmd sshpass sshpass          # every board login is a password login
need_cmd ssh     openssh-client
need_cmd ping    iputils-ping     # the dock sweep
need_cmd curl    curl             # run.sh's already-running check
need_cmd git     git              # "latest version" comes from origin/main
need_py  numpy   python3-numpy    # frame timing + image checks
need_py  cv2     python3-opencv   # decoding the preview frames for the image checks
install_deps

for dep in "$HERE/../opm-name/opmname.py" "$HERE/../dock-check/dock_check.py"; do
  [ -f "$dep" ] || { echo "  MISSING $dep: StarCap QC needs the whole starcap-ops checkout"; exit 1; }
done
need_recorder_clone
# frame timing and the IMU are judged on the footage exactly as the delivery decodes it,
# with the delivery's own code from starcap-dashboard (see delivery.py)
DASH="${STARCAP_DASHBOARD:-$HOME/starcap-dashboard}"
if [ -d "$DASH/.git" ]; then
  echo "  dashboard clone: $DASH (for the delivery's strip decoder)"
else
  echo "  dashboard clone MISSING at $DASH: QC will judge timing on the recorder's own CSVs"
  echo "  instead of the delivered data, and say so. Get it with:"
  echo "    git clone git@github.com:StarpilotAI/starcap-dashboard.git $DASH"
fi

chmod +x "$HERE/run.sh" "$HERE/webui.py"
link_cmd starcap-qc "$HERE/run.sh"
launcher starcap-qc "StarCap QC" "$HERE/run.sh" "$HERE/icon.png" \
  "Test a docked StarCap end to end, then name it"
echo "  open it from the desktop icon, the app menu, or: starcap-qc"
