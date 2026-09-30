#!/usr/bin/env bash
# Install starcap-ops tools on this machine.
#
#   ./install.sh                 list the tools
#   ./install.sh all             install every tool
#   ./install.sh camfw lensfocus install just these (by folder name)
#   NO_APT=1 ./install.sh all    only report missing apt packages
#
# Each tool folder has its own install.sh and can be installed on its own; this
# only finds and runs them. Re-run after a `git pull` or after moving the
# checkout: launchers and commands are regenerated to point at the current path.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mapfile -t INSTALLERS < <(cd "$ROOT" && find . -mindepth 2 -maxdepth 3 -name install.sh \
                            -not -path './lib/*' | sed 's#^\./##' | sort)

name_of() { basename "$(dirname "$1")"; }

if [ $# -eq 0 ]; then
  echo "usage: ./install.sh all | <tool> [<tool> ...]"
  echo
  for i in "${INSTALLERS[@]}"; do
    printf '  %-16s %s\n' "$(name_of "$i")" "$(sed -n '2{s/^# //;s/ installer.$//;s/ installer — / — /;p}' "$ROOT/$i")"
  done
  exit 0
fi

want=("$@")
[ "${want[0]}" = all ] && want=()
ran=0; failed=()
for i in "${INSTALLERS[@]}"; do
  n="$(name_of "$i")"
  if [ ${#want[@]} -gt 0 ]; then
    hit=0; for w in "${want[@]}"; do [ "$w" = "$n" ] && hit=1; done
    [ $hit = 1 ] || continue
  fi
  echo
  if bash "$ROOT/$i"; then ran=$((ran+1)); else failed+=("$n"); fi
done

for w in "${want[@]:-}"; do
  [ -z "$w" ] && continue
  found=0; for i in "${INSTALLERS[@]}"; do [ "$(name_of "$i")" = "$w" ] && found=1; done
  [ $found = 1 ] || { echo "!! no tool named '$w' (run ./install.sh to list them)"; failed+=("$w"); }
done

echo
echo "installed: $ran   failed: ${#failed[@]} ${failed[*]:-}"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo "note: add ~/.local/bin to PATH for the commands (a new login shell usually does it)";;
esac
[ ${#failed[@]} -eq 0 ]
