#!/usr/bin/env bash
# DJ42ショーをサーバへ投入する。FOHが当日使う。
#   1) tools/dj42-screen.sh で映像を生成(初回のみ)
#   2) 音源を /api/upload で上げる(任意。無ければライト+映像のみで進行)
#   3) このスクリプトでセットリストを /api/show にPOST
#
# 使い方:
#   SOLUNA_ADMIN=xxx BASE=https://... ./tools/dj42-show.sh
#   # 音源を差し込む場合: 各stepの url を埋めたJSONを第1引数に渡す
#   SOLUNA_ADMIN=xxx BASE=https://... ./tools/dj42-show.sh my-setlist.json
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
ADMIN="${SOLUNA_ADMIN:?SOLUNA_ADMIN を設定してください}"
CH="${CH:-festival}"
JSON="${1:-$(cd "$(dirname "$0")" && pwd)/dj42-show.json}"

echo "→ POST ${BASE}/api/show?ch=${CH}  (steps from ${JSON})"
curl -fsS -X POST "${BASE}/api/show?ch=${CH}" \
  -H "x-soluna-admin: ${ADMIN}" -H "content-type: application/json" \
  --data-binary @"${JSON}"
echo
echo "完了。管理画面の「⏭ NEXT」で1ステップずつ、または auto:true で自動進行します。"
