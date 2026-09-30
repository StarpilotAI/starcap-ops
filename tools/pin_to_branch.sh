#!/usr/bin/env bash
# pin_to_branch.sh — put a docked StarCap board on a specific recorder branch and
# hold it there, then prove it still records.
#
# Deploys a CLEAN plain tree (git archive | tar, so NO .git ever lands on the
# board) and writes a systemd drop-in that pins UPDATE_BRANCH and, by default,
# turns the self-updater OFF. Read the two traps below before changing either.
#
# ---------------------------------------------------------------------------
# TRAP 1: the updater DEFAULTS TO ON. updater.py:
#     ENABLED = os.environ.get("UPDATE_ENABLE", "1") not in ("0", "false", "no")
# so an UNSET variable means enabled. "Not configured" is not "off" — the unit
# has to say "0" explicitly or a board self-updates to main on first ethernet.
#
# TRAP 2: pin UPDATE_BRANCH even when updates are off. If anyone re-enables
# updates later, an unpinned board tracks main and silently overwrites whatever
# you deployed here — the updater sees main's higher VERSION as "newer" and takes
# it. (History note, 2026-08-10: this default used to be feat/wb-and-face-blur,
# which WAS 1.37.2. That branch merged into main at v1.39.1 and has since tracked
# forward — both main and feat/wb-and-face-blur are now 1.40.1 — so a BRANCH name
# can no longer freeze a version. To pin a specific version, pin an immutable
# tag/commit; the default below now does exactly that.)
#
# TRAP 3: post_update.sh carries RETIRED_BRANCHES (currently " nano_pi ") and
# DROPS an UPDATE_BRANCH pin whose value is in that list, reverting the board to
# main on the next OTA. If you pin a branch and it keeps reverting, look there
# before assuming drift. Value match only — other branch names are safe.
#
# A plain tree (no .git) is deliberate: a real checkout is a landmine, because
# the updater overlays tarballs on top of it and a later `git checkout .` snaps
# the board back to whatever stale commit HEAD sat on.
#
# What is PRESERVED across a run: recordings/ (excluded from the rsync), the
# device id (~/.config/starcap/device_id — outside the tree), SSH host keys, and
# every per-device Environment= line already in the unit (we only add a drop-in,
# never rewrite stereocam.service).
#
# Usage:
#   ./pin_to_branch.sh                                  # auto-find the docked board
#   ./pin_to_branch.sh --host 10.77.0.66
#   ./pin_to_branch.sh --branch main --updates on       # back to stock behaviour
#   ./pin_to_branch.sh --host 10.77.0.66 --yes
#
#   --host IP        target board (default: sweep the dock subnet for exactly one)
#   --branch NAME    recorder branch (default: feat/wb-and-face-blur)
#   --repo PATH      local clone to export from (default: ~/orangepi-recorder)
#   --updates on|off self-updater state afterwards (default: off)
#   --yes            skip the confirmation prompt
#   --no-verify      skip the post-deploy recording check
set -euo pipefail

# Default pins the IMMUTABLE v1.37.2 commit, not a moving branch: the old
# feat/wb-and-face-blur default drifted to 1.40.1 once it merged into main (see
# TRAP 2), so a branch name can no longer freeze a version. 32b865f is the commit
# that released VERSION 1.37.2. Pass --branch main --updates on for stock
# always-latest behaviour instead.
BRANCH="32b865f"
REPO="$HOME/orangepi-recorder"
HOST=""
UPDATES="off"
AUTO_YES=0
VERIFY=1
SUBNET="10.77.0"
PI_USER="${PI_USER:-pi}"
PI_PASS="${PI_PASS:-12345}"

while [ $# -gt 0 ]; do
  case "$1" in
    --host)     shift; HOST="${1:?--host needs a value}" ;;
    --branch)   shift; BRANCH="${1:?--branch needs a value}" ;;
    --repo)     shift; REPO="${1:?--repo needs a value}" ;;
    --updates)  shift; UPDATES="${1:?--updates needs on|off}" ;;
    --yes)      AUTO_YES=1 ;;
    --no-verify) VERIFY=0 ;;
    -h|--help)  sed -n '1,45p' "$0"; exit 0 ;;
    *)          echo "unknown option: $1"; exit 1 ;;
  esac
  shift
done

case "$UPDATES" in on|off) ;; *) echo "--updates must be 'on' or 'off'"; exit 1 ;; esac
command -v sshpass >/dev/null || { echo "!! sshpass not installed (apt install sshpass)"; exit 1; }
[ -d "$REPO/.git" ] || { echo "!! $REPO is not a git clone — pass --repo"; exit 1; }

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"
SSH="sshpass -p $PI_PASS ssh $SSH_OPTS"
# NOTE: filter ssh's known-hosts warning WITHOUT letting grep decide the exit
# status — `grep -v` returns 1 when every line is filtered out, so piping
# straight into it makes any silent-but-successful remote command look failed.
rsh() {
  local out st
  out="$($SSH "$PI_USER@$HOST" "$@" 2>&1)"; st=$?
  [ -n "$out" ] && printf '%s\n' "$out" | grep -v "Warning: Permanently added" || true
  return $st
}

# ---- find the board -------------------------------------------------------
if [ -z "$HOST" ]; then
  echo "== no --host given, sweeping ${SUBNET}.0/24 =="
  FOUND=()
  for i in $(seq 2 254); do
    ( ping -c1 -W1 "${SUBNET}.$i" >/dev/null 2>&1 && echo "${SUBNET}.$i" ) &
  done > /tmp/.pin_sweep.$$ 2>/dev/null
  wait 2>/dev/null || true
  mapfile -t FOUND < <(sort -V /tmp/.pin_sweep.$$ 2>/dev/null); rm -f /tmp/.pin_sweep.$$
  case "${#FOUND[@]}" in
    0) echo "!! no board answered on ${SUBNET}.0/24 — is it docked and powered?"; exit 1 ;;
    1) HOST="${FOUND[0]}"; echo "   found: $HOST" ;;
    *) echo "!! more than one host answered; pass --host explicitly:"; printf '     %s\n' "${FOUND[@]}"; exit 1 ;;
  esac
fi

$SSH "$PI_USER@$HOST" true >/dev/null 2>&1 || { echo "!! cannot ssh to $PI_USER@$HOST (try PI_USER=orangepi, or check PI_PASS)"; exit 1; }

# ---- resolve the ref (branch, tag, or commit) -----------------------------
# A branch must be fetched to be current; a tag or commit is immutable and is the
# only way to freeze a specific VERSION (see TRAP 2 — branch names drift). Try to
# fetch $BRANCH as a branch; if that fails but it already resolves locally (a tag,
# or a historical commit like the v1.37.2 pin default), deploy that directly
# rather than bailing out.
echo "== resolving $BRANCH =="
if git -C "$REPO" fetch origin "$BRANCH" --quiet 2>/dev/null; then
  REF="origin/$BRANCH"
  git -C "$REPO" rev-parse --verify "$REF^{commit}" >/dev/null 2>&1 || REF="FETCH_HEAD"
elif git -C "$REPO" rev-parse --verify "$BRANCH^{commit}" >/dev/null 2>&1; then
  REF="$(git -C "$REPO" rev-parse --verify "$BRANCH^{commit}")"
  echo "   ('$BRANCH' is not a remote branch — pinning to local ref $REF)"
else
  echo "!! '$BRANCH' is neither a remote branch nor a local tag/commit"; exit 1
fi
HEAD_LINE="$(git -C "$REPO" log --oneline -1 "$REF")"

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
git -C "$REPO" archive "$REF" | tar -x -C "$STAGE"
NEW_VER="$(cat "$STAGE/stereocam/VERSION" 2>/dev/null || true)"
[ -n "$NEW_VER" ] || { echo "!! exported tree has no stereocam/VERSION — wrong branch or repo?"; exit 1; }

CUR_VER="$(rsh 'cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null' || true)"
DEV_ID="$(rsh 'cat ~/.config/starcap/device_id 2>/dev/null' || true)"

cat <<PLAN

== target ==
  board     : $HOST   (${DEV_ID:-unknown device})
  installed : ${CUR_VER:-unknown}
  deploying : $NEW_VER   ($BRANCH)
  commit    : $HEAD_LINE
  updater   : $UPDATES $([ "$UPDATES" = off ] && echo "(UPDATE_ENABLE=0)" || echo "(tracking $BRANCH)")
  preserved : recordings/, device_id, ssh host keys, existing unit Environment=
PLAN

if [ "$AUTO_YES" -ne 1 ]; then
  read -r -p "Type 'deploy' to continue: " C
  [ "$C" = "deploy" ] || { echo "aborted — nothing changed."; exit 1; }
fi

# ---- back up, deploy ------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
echo "== backing up the current tree =="
rsh "tar czf ~/orangepi-recorder-${CUR_VER:-unknown}-backup-${STAMP}.tar.gz --exclude=recordings -C ~ orangepi-recorder 2>/dev/null; ls -lh ~/orangepi-recorder-${CUR_VER:-unknown}-backup-${STAMP}.tar.gz"

echo "== stopping recorder =="
rsh "echo $PI_PASS | sudo -S systemctl stop stereocam.service 2>/dev/null; true"

echo "== deploying $NEW_VER =="
rsync -rlptD --delete --exclude='recordings/' -e "$SSH" "$STAGE/" "$PI_USER@$HOST:/home/$PI_USER/orangepi-recorder/" \
  2>&1 | grep -v "Warning: Permanently added" || true

# Marker for the verify step: only takes that START after this instant belong to
# the version we just deployed. Without it the verifier happily reports the
# PREVIOUS version's take and calls the deploy good.
DEPLOY_TS="$(rsh 'date +%s' | tr -dc '0-9')"

echo "== writing drop-in =="
if [ "$UPDATES" = "off" ]; then ENABLE_LINE="Environment=UPDATE_ENABLE=0"; else ENABLE_LINE="Environment=UPDATE_ENABLE=1"; fi
rsh "echo $PI_PASS | sudo -S mkdir -p /etc/systemd/system/stereocam.service.d 2>/dev/null
echo $PI_PASS | sudo -S bash -c 'cat > /etc/systemd/system/stereocam.service.d/20-branch-pin.conf <<CONF
# Written by starcap-ops/tools/pin_to_branch.sh on ${STAMP}.
# updater.py defaults UPDATE_ENABLE to \"1\", so \"0\" must be explicit — unset is NOT off.
# UPDATE_BRANCH is pinned regardless, so re-enabling updates cannot silently pull main.
[Service]
${ENABLE_LINE}
Environment=UPDATE_BRANCH=${BRANCH}
CONF'
# Remove every drop-in these ops tools have ever written. systemd merges drop-ins
# in LEXICAL order and the last Environment= for a variable wins, so a leftover
# 20-pin-commit.conf (UPDATE_ENABLE=0) would override 20-branch-pin.conf and
# silently keep updates off — the deploy would look fine and do the opposite.
echo $PI_PASS | sudo -S rm -f /etc/systemd/system/stereocam.service.d/20-no-autoupdate.conf \
                              /etc/systemd/system/stereocam.service.d/20-pin-commit.conf 2>/dev/null
echo $PI_PASS | sudo -S systemctl daemon-reload 2>/dev/null
echo $PI_PASS | sudo -S systemctl start stereocam.service 2>/dev/null; true"

# ---- verify ---------------------------------------------------------------
echo "== verifying =="
sleep 12
GOT_VER="$(rsh 'cat ~/orangepi-recorder/stereocam/VERSION 2>/dev/null')"
echo "  VERSION : $GOT_VER $([ "$GOT_VER" = "$NEW_VER" ] && echo OK || echo "MISMATCH (wanted $NEW_VER)")"
echo "  service : $(rsh 'systemctl is-active stereocam.service')  NRestarts=$(rsh 'systemctl show stereocam -p NRestarts --value')"
rsh 'systemctl show stereocam -p Environment --value' | tr ' ' '\n' | grep -E "UPDATE_" | sed 's/^/  /'
rsh '[ -d ~/orangepi-recorder/.git ] && echo "  !! .git PRESENT — a plain tree was expected" || echo "  tree    : plain (no .git) OK"'

[ "$GOT_VER" = "$NEW_VER" ] || { echo "!! deployed version does not match — investigate before trusting this board."; exit 1; }

# Assert the EFFECTIVE environment matches intent. Printing it is not enough: a
# leftover drop-in later in lexical order silently wins, so a run can report
# success while the updater state is the opposite of what was asked for.
WANT_EN=$([ "$UPDATES" = off ] && echo 0 || echo 1)
EFF="$(rsh 'systemctl show stereocam -p Environment --value')"
echo "$EFF" | tr ' ' '\n' | grep -qx "UPDATE_ENABLE=$WANT_EN" \
  || { echo "!! UPDATE_ENABLE is not $WANT_EN in the effective environment — check for another drop-in:"; \
       rsh 'ls -1 /etc/systemd/system/stereocam.service.d/ 2>/dev/null'; exit 1; }
echo "$EFF" | tr ' ' '\n' | grep -qx "UPDATE_BRANCH=$BRANCH" \
  || { echo "!! UPDATE_BRANCH is not $BRANCH in the effective environment."; exit 1; }
echo "  asserted: UPDATE_ENABLE=$WANT_EN, UPDATE_BRANCH=$BRANCH"
[ "$VERIFY" -eq 1 ] || { echo "done (verification skipped)."; exit 0; }

echo "== waiting for a take from the NEW version to complete (up to 150s) =="
rsh "for i in \$(seq 1 30); do
       journalctl -u stereocam --no-pager --since \"@$DEPLOY_TS\" | grep -q '\\[rec\\] STOP' && break
       sleep 5
     done
     journalctl -u stereocam --no-pager --since \"@$DEPLOY_TS\" | grep -E '\\[rec\\] (START|STOP)' | tail -6" > /tmp/.pin_takes.$$ || true
cat /tmp/.pin_takes.$$

# NOTE: pass the log by PATH, not on stdin — `python3 -` already consumes stdin to
# read the program from the heredoc, so a `< file` redirect here is swallowed and
# the parser silently sees zero lines (and reports "no completed take yet" for a
# take that did complete).
python3 - "$GOT_VER" "/tmp/.pin_takes.$$" <<'PY' || true
import sys, re, datetime
ver = sys.argv[1]; lines = open(sys.argv[2]).read().splitlines()
starts, best = {}, None
for l in lines:
    # take-id must EXCLUDE ':' — with (\S+) the greedy match swallows the colon,
    # and because the frames group is optional the regex still succeeds (matching
    # it empty) instead of backtracking. Result: every STOP parsed with frames=None
    # and the verifier reported "no completed take" for takes that completed.
    m = re.search(r'^(\w+ \d+ [\d:]+).*\[rec\] (START|STOP) ([^:\s]+)(?:: (\d+) frames, (\d+) dropped)?', l)
    if not m: continue
    t = datetime.datetime.strptime(m.group(1), "%b %d %H:%M:%S")
    if m.group(2) == "START": starts[m.group(3)] = t
    elif m.group(4) is not None and m.group(3) in starts:
        dur = (t - starts[m.group(3)]).total_seconds()
        if dur > 0: best = (int(m.group(4)), int(m.group(5)), dur)
if not best:
    print("\n  no completed take yet — check manually: journalctl -u stereocam | grep '\\[rec\\] STOP'")
else:
    f, d, s = best
    print(f"\n  RESULT {ver}: {f} frames in {s:.0f}s = {f/s:.1f} fps, {d} dropped ({d/(f+d)*100:.1f}%)")
    print("  0 frames => test the CAMERA standalone before blaming the recorder:")
    print("    sudo systemctl stop stereocam && gst-launch-1.0 v4l2src num-buffers=100 \\")
    print("      ! image/jpeg,width=4000,height=1200 ! multifilesink location=/tmp/f%03d.jpg")
PY
rm -f /tmp/.pin_takes.$$
echo "done."
