#!/usr/bin/env bash
# fetch_images.sh — get the flashing images that git does not carry.
#
# The scripts, loaders, docs and small partitions of flashing/ are in git. The
# five big images (~30 GB) are not: they live in the Google Drive folder
# "StarCap flashing images" (shared with starpilot.ai only; link in README.md),
# and IMAGES.sha256 (committed) says exactly which bytes belong where.
#
#   ./fetch_images.sh --from ~/Downloads   place images you downloaded from the
#                                          Drive link (searched by name + size)
#   ./fetch_images.sh                      download missing images with rclone
#   ./fetch_images.sh nanopi/sd            only the images under these folders
#   ./fetch_images.sh --check              full sha256 check of the local images
#   ./fetch_images.sh --upload             push local images the Drive lacks (rclone)
#
# Nothing replaces a file until it matches the manifest's sha256, and every image
# gets its recorded mtime back: the flash scripts pick the NEWEST image beside
# them, so dates are part of correctness here, not cosmetics.
#
# rclone needs a one-time login:  rclone config create gdrive drive scope=drive
# FLASH_REMOTE overrides the rclone path (default below).
#
# THESE IMAGES CONTAIN stereocam/update_token, a GitHub token with admin + push on
# the recorder repo. Share the Drive folder with starpilot.ai only, never
# "Anyone with the link".

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$HERE/IMAGES.sha256"
REMOTE="${FLASH_REMOTE:-gdrive:StarCap flashing images}"

RCLONE="$(command -v rclone || true)"
[ -n "$RCLONE" ] || [ ! -x "$HOME/.local/bin/rclone" ] || RCLONE="$HOME/.local/bin/rclone"

MODE=fetch; FILTERS=(); FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check)  MODE=check ;;
    --upload) MODE=upload ;;
    --from)   MODE=place; FROM="${2:?--from needs a folder}"; shift ;;
    -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) FILTERS+=("${1%/}") ;;
  esac
  shift
done

[ -f "$MANIFEST" ] || { echo "!! no $MANIFEST"; exit 1; }
if { [ "$MODE" = fetch ] || [ "$MODE" = upload ]; } && [ -z "$RCLONE" ]; then
  echo "!! no rclone. Either download the images from the Drive link in README.md and run"
  echo "     $0 --from ~/Downloads"
  echo "   or install rclone (https://rclone.org/install/) and log in once:"
  echo "     rclone config create gdrive drive scope=drive"
  exit 1
fi
[ "$MODE" != place ] || [ -d "$FROM" ] || { echo "!! not a folder: $FROM"; exit 1; }

wanted() {
  [ ${#FILTERS[@]} -eq 0 ] && return 0
  local f; for f in "${FILTERS[@]}"; do case "$1" in "$f"/*|"$f") return 0 ;; esac; done
  return 1
}

sha_ok() { [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }

# --from: the Drive web UI flattens folders, so look for the manifest's relative
# path first, then any file with the same name AND byte size.
find_in_from() {
  local path="$1" bytes="$2" c
  [ -f "$FROM/$path" ] && { echo "$FROM/$path"; return; }
  while IFS= read -r c; do
    [ "$(stat -c %s "$c")" = "$bytes" ] && { echo "$c"; return; }
  done < <(find "$FROM" -type f -name "$(basename "$path")" 2>/dev/null)
}

fail=0; did=0
while read -r sha bytes mtime path; do
  case "$sha" in ''|'#'*) continue ;; esac
  wanted "$path" || continue
  local_f="$HERE/$path"
  case "$MODE" in
    check)
      if sha_ok "$local_f" "$sha"; then echo "  ok       $path"
      elif [ -f "$local_f" ]; then echo "  CORRUPT  $path (sha256 does not match)"; fail=1
      else echo "  missing  $path"; fail=1; fi
      ;;
    fetch)
      # A present file of the right size is trusted without a 10 GB re-hash;
      # --check is the full proof.
      if [ -f "$local_f" ] && [ "$(stat -c %s "$local_f")" = "$bytes" ]; then
        echo "  have     $path"; continue
      fi
      echo "  fetching $path ($((bytes/1000000)) MB)"
      mkdir -p "$(dirname "$local_f")"
      if ! "$RCLONE" copyto "$REMOTE/$path" "$local_f.part" --progress </dev/null 2> >(grep -v client_id >&2); then
        echo "  !! download failed: $path"; rm -f "$local_f.part"; fail=1; continue
      fi
      if ! sha_ok "$local_f.part" "$sha"; then
        echo "  !! sha256 mismatch after download, discarded: $path"; rm -f "$local_f.part"; fail=1; continue
      fi
      mv "$local_f.part" "$local_f" && touch -d "@$mtime" "$local_f"
      echo "  ok       $path"; did=$((did+1))
      ;;
    place)
      if [ -f "$local_f" ] && [ "$(stat -c %s "$local_f")" = "$bytes" ]; then
        echo "  have     $path"; continue
      fi
      src="$(find_in_from "$path" "$bytes")"
      if [ -z "$src" ]; then echo "  not in $FROM: $(basename "$path") ($bytes bytes)"; fail=1; continue; fi
      echo "  checking $src"
      if ! sha_ok "$src" "$sha"; then echo "  !! sha256 mismatch, not used: $src"; fail=1; continue; fi
      mkdir -p "$(dirname "$local_f")"
      mv "$src" "$local_f" && touch -d "@$mtime" "$local_f"
      echo "  ok       $path"; did=$((did+1))
      ;;
    upload)
      if "$RCLONE" lsf "$REMOTE/$path" </dev/null 2>/dev/null | grep -q .; then
        echo "  on Drive $path"; continue
      fi
      if ! sha_ok "$local_f" "$sha"; then
        echo "  !! local copy missing or not the manifest's bytes, not uploading: $path"; fail=1; continue
      fi
      echo "  uploading $path ($((bytes/1000000)) MB)"
      "$RCLONE" copyto "$local_f" "$REMOTE/$path" --drive-chunk-size 256M </dev/null 2> >(grep -v client_id >&2) || { echo "  !! upload failed: $path"; fail=1; continue; }
      did=$((did+1))
      ;;
  esac
done < "$MANIFEST"

[ "$MODE" = check ] || echo "done: $did transferred"
exit "$fail"
