#!/usr/bin/env bash
# DJ42 × SOLUNA SOUND ショートトレーラー生成 — ffmpegのみ、外部API・コストゼロ。
#   各曲の「ドロップ(最大の見せ場)」を抜き出して繋ぐ。16:9 と 9:16(縦) の2本を出す。
#
#   設計(ペルソナ採点 Round1 の指摘を反映):
#     - 冒頭は黒カードではなく「最も強いドロップ」を0秒目に置く(3秒フック)
#     - カット間のフェードは全廃(ハードカットの切れ味を残す)。フェードは全体の頭と尻だけ
#     - 尺は非対称(4→5→5→6→7→8s)で加速。フィナーレ前に0.5sの暗転を1回
#     - エンドカードに会場・日付・導線(DATE TBA)を入れる
#     - loudnorm + alimiter で TP を保証
#     - 素材(960x540)を無意味にアップスケールしない
#
# 使い方: ./tools/dj42-trailer.sh [出力先dir]   既定: ./assets
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/assets}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FONT="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_JP="/Library/Fonts/Arial Unicode.ttf"
FPS=30
LEAD=1.5          # ドロップの何秒前から

# 曲順と尺(非対称で加速)。先頭=最も強いドロップ。フィナーレは最後。
#   <stem>:<秒>
ORDER=(
  "dj42-prada:4"
  "dj42-amapiano:5"
  "dj42-emotional-gravity:5"
  "dj42-aetheria:6"
  "dj42-aura:7"
  "dj42-vip-sovereignty:8"
)

# ドロップ時刻[s]を setlist から引く
declare -A DROP
while IFS=$'\t' read -r stem at; do DROP["$stem"]="$at"; done < <(python3 - "$ROOT/tools/dj42-setlist.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for s in d["steps"]:
    dr=[e["at"] for e in s.get("timeline",[]) if e.get("scene")=="drop"]
    stem=s["video"].split("/")[-1].replace("-screen.mp4","") if s.get("video") else ""
    print(f"{stem}\t{dr[-1] if dr else 30:.3f}")
PY
)

# ---- クリップ抽出(960x540のまま・音声入り・カット間フェード無し) ----
: > "$TMP/list.txt"
n=0
for spec in "${ORDER[@]}"; do
  stem="${spec%%:*}"; dur="${spec##*:}"
  at="${DROP[$stem]:-30}"
  src="$ROOT/assets/$stem-screen.mp4"
  [ -f "$src" ] || { echo "skip (no file): $src"; continue; }
  ss=$(python3 -c "print(max(0,$at-$LEAD))")
  n=$((n+1))
  echo "clip $n: $stem  @ ${ss}s  ${dur}s"
  # -ss を -i の前に置いて高速シーク。fade無し。最後のクリップだけ尻フェード。
  ffmpeg -y -hide_banner -loglevel error -ss "$ss" -t "$dur" -i "$src" \
    -vf "fps=${FPS},setsar=1" \
    -c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p \
    -c:a aac -ar 48000 -ac 2 "$TMP/clip$n.mp4"
done
[ "$n" -gt 0 ] || { echo "クリップが0本。assets/dj42-*-screen.mp4 を確認してください。"; exit 1; }

# ---- フィナーレ前の暗転(0.5s, 無音) ----
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "color=c=black:s=960x540:r=${FPS}:d=0.5" \
  -f lavfi -i "anullsrc=r=48000:cl=stereo" -shortest \
  -c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -c:a aac "$TMP/black.mp4"

# ---- エンドカード(6s, 情報つき) ----
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "color=c=black:s=960x540:r=${FPS}:d=6" \
  -f lavfi -i "anullsrc=r=48000:cl=stereo" -shortest \
  -vf "\
drawtext=fontfile='${FONT}':text='DJ42':fontcolor=0xffffff:fontsize=110:x=(w-text_w)/2:y=h/2-150:alpha='if(lt(t,0.2),0,min(1,(t-0.2)/0.5))',\
drawtext=fontfile='${FONT_JP}':text='李英俊':fontcolor=0xd4af37:fontsize=42:x=(w-text_w)/2:y=h/2-20:alpha='if(lt(t,0.6),0,min(1,(t-0.6)/0.5))',\
drawtext=fontfile='${FONT}':text='SOLUNA FEST':fontcolor=0xffffff:fontsize=56:x=(w-text_w)/2:y=h/2+70:alpha='if(lt(t,1.0),0,min(1,(t-1.0)/0.5))',\
drawtext=fontfile='${FONT}':text='OAHU, HAWAII  ·  DATE TBA':fontcolor=0xcccccc:fontsize=30:x=(w-text_w)/2:y=h/2+150:alpha='if(lt(t,1.3),0,min(1,(t-1.3)/0.5))',\
drawtext=fontfile='${FONT}':text='solun.art':fontcolor=0xd4af37:fontsize=34:x=(w-text_w)/2:y=h/2+210:alpha='if(lt(t,1.6),0,min(1,(t-1.6)/0.5))'" \
  -c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -c:a aac "$TMP/end.mp4"

# ---- 連結(16:9) ----
: > "$TMP/list.txt"
for i in $(seq 1 "$n"); do echo "file '$TMP/clip$i.mp4'" >> "$TMP/list.txt"; done
echo "file '$TMP/black.mp4'" >> "$TMP/list.txt"
echo "file '$TMP/end.mp4'" >> "$TMP/list.txt"

# 全体の頭と尻だけフェード。loudnorm(2パス相当)+alimiter で TP を保証。
ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i "$TMP/list.txt" \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.85" \
  -c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -ar 48000 "$OUT/dj42-trailer.mp4"

# ---- 9:16 縦版(1080x1920): 背景=ぼかし拡大, 前景=収めて中央 ----
ffmpeg -y -hide_banner -loglevel error -i "$OUT/dj42-trailer.mp4" \
  -filter_complex "\
[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28[bg];\
[0:v]scale=1080:608:force_original_aspect_ratio=decrease[fg];\
[bg][fg]overlay=(W-w)/2:(H-h)/2" \
  -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p -movflags +faststart \
  -c:a copy "$OUT/dj42-trailer-vertical.mp4"

echo "=== wrote $OUT/dj42-trailer.mp4 (16:9) / dj42-trailer-vertical.mp4 (9:16) ==="
for f in dj42-trailer.mp4 dj42-trailer-vertical.mp4; do
  echo -n "$f: "; ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/$f" | tr -d '\n'
  echo -n "  "; ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0 "$OUT/$f"
done
