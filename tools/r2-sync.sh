#!/usr/bin/env bash
# SOLUNA Sound — push tracks/videos to the R2 bucket that SOLUNA_ASSET_BASE points at.
# Devices fetch /assets/<name> from the CDN first and fall back to the sync server,
# so run this after uploading through /admin (or drop files in assets/) and before PRELOAD.
#
#   tools/r2-sync.sh                 # sync ./assets (or $SOLUNA_DATA_DIR/assets)
#   tools/r2-sync.sh path/to/dir     # sync another directory
#   R2_BUCKET=my-bucket tools/r2-sync.sh
set -euo pipefail
BUCKET="${R2_BUCKET:-soluna-sound-assets}"
DIR="${1:-${SOLUNA_DATA_DIR:-$(dirname "$0")/..}/assets}"
WR="${WRANGLER:-wrangler}"            # use the global binary, never `npx wrangler` (hangs)
command -v "$WR" >/dev/null || { echo "wrangler not found (npm i -g wrangler)"; exit 1; }
n=0
for f in "$DIR"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f"); case "$name" in .*) continue;; esac
  case "${name##*.}" in
    mp3) ct=audio/mpeg;; m4a|aac) ct=audio/mp4;; wav) ct=audio/wav;; ogg|opus) ct=audio/ogg;; flac) ct=audio/flac;;
    mp4|m4v) ct=video/mp4;; webm) ct=video/webm;; mov) ct=video/quicktime;; *) ct=application/octet-stream;;
  esac
  echo "→ $BUCKET/$name ($ct)"
  "$WR" r2 object put "$BUCKET/$name" --file "$f" --content-type "$ct" \
        --cache-control "public, max-age=86400" --remote >/dev/null   # --remote: wrangler v4 defaults to a LOCAL bucket
  n=$((n+1))
done
echo "synced $n file(s) to r2://$BUCKET"
