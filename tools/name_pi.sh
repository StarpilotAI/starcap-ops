#!/usr/bin/env bash
# name_pi.sh — force the authoritative dashboard name for a fleet Orange Pi.
#
# Names are keyed by the RK3588 SoC's fused serial (cpu_id — see cpu_id() in
# orangepi-recorder/stereocam/uploader.py), read straight off the board over SSH,
# NOT anything stored on its SD card. So the name assigned here follows the
# PHYSICAL board and survives any future SD-card swap/reflash of that unit — the
# dashboard's own guarantee (see starcap-dashboard/dashboard/src/lib/store.ts,
# K_NAMES: "authoritative cpu-id -> name table").
#
# Writes directly into the same Upstash Redis `starcap:names` hash the
# dashboard's setName()/API route uses, so it's live immediately (no dashboard
# login/session needed) and the board picks it up on its very next heartbeat.
#
# Usage:  ./name_pi.sh <pi-ip> <name>          e.g. ./name_pi.sh 10.77.0.207 starcap48
set -euo pipefail

IP="${1:?usage: $0 <pi-ip> <name>}"
NAME="${2:?usage: $0 <pi-ip> <name>}"
SSH_USER=orangepi
SSH_PASS=orangepi

ENV_FILE="$HOME/starcap-dashboard/dashboard/.env.local"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source <(grep -E '^UPSTASH_REDIS_REST_(URL|TOKEN)=' "$ENV_FILE")
fi
: "${UPSTASH_REDIS_REST_URL:?set UPSTASH_REDIS_REST_URL (or create $ENV_FILE with it)}"
: "${UPSTASH_REDIS_REST_TOKEN:?set UPSTASH_REDIS_REST_TOKEN (or create $ENV_FILE with it)}"

for bin in sshpass ssh curl python3; do
  command -v "$bin" >/dev/null || { echo "missing: $bin"; exit 1; }
done

echo "== clearing any stale SSH host key for $IP (SD-card swaps change it) =="
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$IP" >/dev/null 2>&1 || true

ssh_pi() { SSHPASS="$SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$SSH_USER@$IP" "$@"; }

echo "== reading cpu_id from $IP =="
# Prefer calling the recorder's own cpu_id() so we always match its exact logic
# (dt_serial -> cpu_serial -> OTP hash -> MAC fallback chain). Falls back to doing
# the same dt_serial/cpu_serial read by hand if orangepi-recorder isn't installed
# on this board yet.
CPU_ID="$(ssh_pi '
  (cd ~/orangepi-recorder/stereocam 2>/dev/null && python3 -c "import uploader; print(uploader.cpu_id())" 2>/dev/null) && exit 0
  v=$(tr -d "\0" < /proc/device-tree/serial-number 2>/dev/null || true)
  if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
  v=$(grep -i "^serial" /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | tr -d " \t")
  if [ -n "$v" ] && [ "$(echo "$v" | tr -d 0)" != "" ]; then echo "$v"; exit 0; fi
  echo ""
' | tail -1 | tr -d '[:space:]')"

if [ -z "$CPU_ID" ]; then
  echo "could not read a stable hardware id from $IP — is it an Orange Pi RK3588 board, reachable at $SSH_USER@$IP?"
  exit 1
fi
echo "   cpu_id: $CPU_ID"

NOW_MS=$(( $(date +%s%N) / 1000000 ))
ENTRY="$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1], 'updated_at': int(sys.argv[2])}))" "$NAME" "$NOW_MS")"

echo "== writing authoritative name '$NAME' for cpu_id $CPU_ID =="
RESP="$(curl -sS -X POST "$UPSTASH_REDIS_REST_URL" \
  -H "Authorization: Bearer $UPSTASH_REDIS_REST_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "import json,sys; print(json.dumps(['HSET','starcap:names',sys.argv[1],sys.argv[2]]))" "$CPU_ID" "$ENTRY")")"
echo "   $RESP"

echo "== verifying =="
curl -sS -X POST "$UPSTASH_REDIS_REST_URL" \
  -H "Authorization: Bearer $UPSTASH_REDIS_REST_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "import json,sys; print(json.dumps(['HGET','starcap:names',sys.argv[1]]))" "$CPU_ID")"
echo
echo "== done — $IP will show as '$NAME' on the dashboard within one heartbeat, and will keep showing as '$NAME' for THIS physical board even after future SD-card swaps =="
