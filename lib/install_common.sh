# install_common.sh — sourced by every tool's install.sh. Not run on its own.
#
# A tool's install.sh declares what it needs, then calls install_deps:
#
#   source "$(dirname "${BASH_SOURCE[0]}")/../../lib/install_common.sh"
#   need_cmd sshpass sshpass          # command -> apt package that provides it
#   need_py  numpy   python3-numpy    # python module -> apt package
#   install_deps                      # one `sudo apt install` for whatever is missing
#   launcher "starcapfinder" "StarCap Finder" "$HERE/run.sh" camera-web "Find every StarCap"
#   link_cmd dock-check "$HERE/dock_check.py"
#
# Environment:
#   NO_APT=1       report missing packages instead of installing them
#   NO_DESKTOP=1   skip the Desktop icon (the app-menu entry is still written)
#
# Everything is idempotent: re-running an install.sh after a `git pull` or after
# moving the checkout rewrites the launchers to the current absolute paths, which
# is the only reason the .desktop files are generated rather than committed.

set -euo pipefail

_MISSING_APT=()

_add_pkg() {
  local p
  for p in "${_MISSING_APT[@]:-}"; do [ "$p" = "$1" ] && return 0; done
  _MISSING_APT+=("$1")
}

# need_cmd <command> <apt-package>
need_cmd() { command -v "$1" >/dev/null 2>&1 || _add_pkg "$2"; }

# need_py <module> <apt-package>. Checked with `python3 -s` so a pip copy in
# ~/.local cannot hide a missing apt one (this laptop has both OpenCVs).
need_py() { python3 -s -c "import $1" >/dev/null 2>&1 || _add_pkg "$2"; }

install_deps() {
  [ "${#_MISSING_APT[@]}" -eq 0 ] && { echo "  deps: all present"; return 0; }
  if [ "${NO_APT:-0}" = 1 ]; then
    echo "  deps MISSING (NO_APT=1, not installing): sudo apt install ${_MISSING_APT[*]}"
    return 0
  fi
  echo "  deps: installing ${_MISSING_APT[*]}"
  sudo apt-get install -y "${_MISSING_APT[@]}"
}

_desktop_dir() {
  local d; d="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
  [ -d "${d:-}" ] || d="$HOME/Desktop"
  echo "$d"
}

# launcher <file-id> <Name> <Exec> <Icon> <Comment> [Terminal:true|false]
# Writes ~/.local/share/applications/<file-id>.desktop and the same on the Desktop.
launcher() {
  local id="$1" name="$2" exec_="$3" icon="$4" comment="$5" term="${6:-false}"
  local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$apps"
  cat > "$apps/$id.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$name
Comment=$comment
Exec=$exec_
Icon=$icon
Terminal=$term
Categories=Utility;
StartupNotify=true
EOF
  chmod +x "$apps/$id.desktop"
  command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
  if [ "${NO_DESKTOP:-0}" != 1 ]; then
    local dd; dd="$(_desktop_dir)"
    if [ -d "$dd" ]; then
      cp "$apps/$id.desktop" "$dd/$id.desktop"
      # GNOME will not launch an untrusted desktop icon ("Allow Launching").
      gio set "$dd/$id.desktop" metadata::trusted true 2>/dev/null || true
    fi
  fi
  echo "  launcher: $name"
}

# link_cmd <name> <target>: put <name> on PATH via ~/.local/bin. A wrapper, not
# a symlink: several tools find their data next to themselves via $0/__file__,
# and through a symlink that would be ~/.local/bin.
link_cmd() {
  mkdir -p "$HOME/.local/bin"
  chmod +x "$2"
  rm -f "$HOME/.local/bin/$1"
  printf '#!/bin/sh\nexec "%s" "$@"\n' "$2" > "$HOME/.local/bin/$1"
  chmod +x "$HOME/.local/bin/$1"
  echo "  command: $1"
}

# The fleet tools read the recorder repo to know what "latest" is.
need_recorder_clone() {
  local r="${RECORDER_REPO:-$HOME/orangepi-recorder}"
  if [ -d "$r/.git" ]; then echo "  recorder clone: $r"; return 0; fi
  echo "  recorder clone MISSING at $r. Get it with:"
  echo "    git clone git@github.com:StarpilotAI/orangepi-recorder.git $r"
}
