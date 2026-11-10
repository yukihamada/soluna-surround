#!/usr/bin/env bash
# 会場ゾーン A〜F をサーバへ投入する(距離m → delay_ms はサーバが自動計算)。
# 既定は Echoa Ridge の暫定距離。実区画図(site plan)が届いたら ZONES_M を書き換える。
#
# 使い方:
#   SOLUNA_ADMIN=xxx BASE=https://sound.solun.art ./tools/dj42-zones.sh
#   # 距離を上書き:
#   SOLUNA_ADMIN=xxx BASE=... ZONES_M='{"A":0,"B":12,"C":26,"D":42,"E":60,"F":82}' ./tools/dj42-zones.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
ADMIN="${SOLUNA_ADMIN:?SOLUNA_ADMIN を設定してください}"
CH="${CH:-festival}"
# ステージからの距離[m]。A=最前柵 → F=最後方。Echoa Ridge 暫定。
ZONES_M="${ZONES_M:-{\"A\":0,\"B\":15,\"C\":30,\"D\":45,\"E\":60,\"F\":80}}"

echo "→ POST ${BASE}/api/zones?ch=${CH}  zones_m=${ZONES_M}"
curl -fsS -X POST "${BASE}/api/zones?ch=${CH}" \
  -H "x-soluna-admin: ${ADMIN}" -H "content-type: application/json" \
  -d "{\"zones_m\": ${ZONES_M}}"
echo
echo "完了。delay_ms は 距離/343*1000 + 15ms(Haas) で自動計算されます。"
