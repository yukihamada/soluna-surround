#!/usr/bin/env bash
# DJ42ショーをサーバへ投入する。FOHが当日使う。
#   1) tools/dj42-build-setlist.py で実セットリスト(dj42-setlist.json)を生成(初回/曲追加時)
#   2) 音源は assets/dj42-*.m4a に配置済み(リポジトリ同梱)。別VMへは /api/upload で上げる
#   3) このスクリプトでセットリストを /api/show にPOST
#
# 使い方:
#   SOLUNA_ADMIN=xxx BASE=https://... ./tools/dj42-show.sh
#   # 別のセットリストを使う場合:
#   SOLUNA_ADMIN=xxx BASE=https://... ./tools/dj42-show.sh my-setlist.json
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
ADMIN="${SOLUNA_ADMIN:?SOLUNA_ADMIN を設定してください}"
CH="${CH:-festival}"
JSON="${1:-$(cd "$(dirname "$0")" && pwd)/dj42-setlist.json}"

if [ ! -f "$JSON" ]; then
  echo "セットリストが無いので生成します: $JSON"
  python3 "$(dirname "$0")/dj42-build-setlist.py"
fi

echo "→ POST ${BASE}/api/show?ch=${CH}  (steps from ${JSON})"
curl -fsS -X POST "${BASE}/api/show?ch=${CH}" \
  -H "x-soluna-admin: ${ADMIN}" -H "content-type: application/json" \
  --data-binary @"${JSON}"
echo
echo "完了。管理画面の「⏭ NEXT」で1ステップずつ、または auto:true で自動進行します。"
echo "各曲は step.timeline を持ち、曲中で演出が切り替わります(イントロ→ハープ→ドロップ等)。"
