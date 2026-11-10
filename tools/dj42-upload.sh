#!/usr/bin/env bash
# DJ42の音源・映像をサーバの /assets へ上げる(Fly volume /data/assets 等)。
# ローカルの assets/ は .gitignore 対象なので、本番はこのアップロードで届ける。
#
# 使い方:
#   SOLUNA_ADMIN=xxx BASE=https://sound.solun.art ./tools/dj42-upload.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
ADMIN="${SOLUNA_ADMIN:?SOLUNA_ADMIN を設定してください}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ASSETS="$(dirname "$HERE")/assets"

shopt -s nullglob
files=("$ASSETS"/dj42-*.m4a "$ASSETS"/dj42-*.mp4 "$ASSETS"/dj42-*.jpg)
if [ ${#files[@]} -eq 0 ]; then
  echo "assets/dj42-* が見つかりません。先に tools/dj42-build-setlist.py と映像コピーを実行してください。"
  exit 1
fi

for f in "${files[@]}"; do
  name="$(basename "$f")"
  printf "→ %-32s " "$name"
  curl -fsS -X POST "${BASE}/api/upload?name=${name}" \
    -H "x-soluna-admin: ${ADMIN}" \
    --data-binary @"$f" >/dev/null && echo "OK"
done
echo "完了。確認: curl -H 'x-soluna-admin: ...' ${BASE}/api/assets"
