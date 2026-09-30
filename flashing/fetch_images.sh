#!/usr/bin/env bash
# fetch_images.sh — get the flashing images that git does not carry.
#
# The scripts, loaders, docs and small partitions of flashing/ are in git. The
# five big images (~30 GB) are not: they live in a PRIVATE Cloud Storage bucket,
# and IMAGES.sha256 (committed) says exactly which bytes belong where.
#
#   ./fetch_images.sh                 download every missing or wrong image
#   ./fetch_images.sh nanopi/sd       only the images under these folders
#   ./fetch_images.sh --check         verify local images against the manifest (full sha256)
#   ./fetch_images.sh --upload        push local images the bucket does not have yet
#
# Every download is checked against the manifest before it replaces anything, and
# gets its recorded mtime back: the flash scripts pick the NEWEST image beside
# them, so dates are part of correctness here, not cosmetics.
#
# Needs the Google Cloud CLI logged in as someone with access to the bucket:
#   gcloud auth login
# FLASH_BUCKET overrides the bucket (default below).
#
# THESE IMAGES CONTAIN stereocam/update_token, a GitHub token with admin + push on
# the recorder repo. The bucket must stay private. Never make an object public or
# hand out a signed URL to anyone outside the team.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$HERE/IMAGES.sha256"
BUCKET="${FLASH_BUCKET:-gs://starpilot-starcap-flashing}"

GCLOUD="$(command -v gcloud || true)"
[ -n "$GCLOUD" ] || [ ! -x "$HOME/google-cloud-sdk/bin/gcloud" ] || GCLOUD="$HOME/google-cloud-sdk/bin/gcloud"

MODE=fetch; FILTERS=()
for a in "$@"; do
  case "$a" in
    --check)  MODE=check ;;
    --upload) MODE=upload ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) FILTERS+=("${a%/}") ;;
  esac
done

[ -f "$MANIFEST" ] || { echo "!! no $MANIFEST"; exit 1; }
if [ "$MODE" != check ] && [ -z "$GCLOUD" ]; then
  echo "!! needs the Google Cloud CLI (gcloud). Install: https://cloud.google.com/sdk/docs/install"
  echo "   then log in once:  gcloud auth login"
  exit 1
fi

wanted() {
  [ ${#FILTERS[@]} -eq 0 ] && return 0
  local f; for f in "${FILTERS[@]}"; do case "$1" in "$f"/*|"$f") return 0 ;; esac; done
  return 1
}

sha_ok() { [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }

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
      if ! "$GCLOUD" storage cp "$BUCKET/$path" "$local_f.part"; then
        echo "  !! download failed: $path"; rm -f "$local_f.part"; fail=1; continue
      fi
      if ! sha_ok "$local_f.part" "$sha"; then
        echo "  !! sha256 mismatch after download, discarded: $path"; rm -f "$local_f.part"; fail=1; continue
      fi
      mv "$local_f.part" "$local_f" && touch -d "@$mtime" "$local_f"
      echo "  ok       $path"; did=$((did+1))
      ;;
    upload)
      if "$GCLOUD" storage ls "$BUCKET/$path" >/dev/null 2>&1; then
        echo "  in bucket $path"; continue
      fi
      if ! sha_ok "$local_f" "$sha"; then
        echo "  !! local copy missing or not the manifest's bytes, not uploading: $path"; fail=1; continue
      fi
      echo "  uploading $path ($((bytes/1000000)) MB)"
      "$GCLOUD" storage cp "$local_f" "$BUCKET/$path" || { echo "  !! upload failed: $path"; fail=1; continue; }
      did=$((did+1))
      ;;
  esac
done < "$MANIFEST"

[ "$MODE" = check ] || echo "done: $did transferred"
exit "$fail"
