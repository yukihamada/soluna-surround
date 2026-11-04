#!/usr/bin/env bash
# DJ42 (李英俊 / LEE YOUNGJUN) スクリーンカード生成 — 外部API・コストゼロ。
# /screen (プロジェクター/LEDウォール) に流す動画を作る。ffmpegのみ。
#   ./tools/dj42-screen.sh [出力先dir]   既定: ./assets
# 生成物:
#   dj42-intro.mp4        開演カード(名前フラッシュ→定着, 4s, 1920x1080)
#   dj42-nowplaying.mp4   曲中のロアサード(ループ可, 8s)
set -euo pipefail
OUT="${1:-$(cd "$(dirname "$0")/.." && pwd)/assets}"
mkdir -p "$OUT"
FONT="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_JP="/Library/Fonts/Arial Unicode.ttf"
W=1920; H=1080; FPS=30

# ---- 1) イントロ: 黒→「DJ42」がフラッシュ→定着、下に本名 ----
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "color=c=black:s=${W}x${H}:r=${FPS}:d=4" \
  -vf "\
drawtext=fontfile='${FONT}':text='DJ42':fontcolor=white:fontsize=380:\
x=(w-text_w)/2:y=(h-text_h)/2-40:\
alpha='if(lt(t,0.15),0, if(lt(t,0.35),1, if(lt(t,0.5),0.15, 1)))',\
drawtext=fontfile='${FONT_JP}':text='李英俊':fontcolor=0xE8E8E8:fontsize=90:\
x=(w-text_w)/2:y=(h/2)+210:alpha='if(lt(t,0.9),0,(t-0.9)/0.8)',\
drawtext=fontfile='${FONT}':text='LEE YOUNGJUN':fontcolor=0x9A9A9A:fontsize=54:\
x=(w-text_w)/2:y=(h/2)+330:alpha='if(lt(t,1.2),0,(t-1.2)/0.8)'" \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$OUT/dj42-intro.mp4"

# ---- 2) ロアサード: 曲中に常時出せる小さめカード(ループ) ----
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "color=c=black:s=${W}x${H}:r=${FPS}:d=8" \
  -vf "\
drawtext=fontfile='${FONT}':text='DJ42':fontcolor=white:fontsize=120:\
x=120:y=h-260:alpha='0.85+0.15*sin(2*PI*t/2)',\
drawtext=fontfile='${FONT_JP}':text='李英俊':fontcolor=0xCCCCCC:fontsize=44:\
x=124:y=h-110,\
drawtext=fontfile='${FONT}':text='NOW PLAYING':fontcolor=0x777777:fontsize=36:\
x=w-text_w-120:y=h-180" \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$OUT/dj42-nowplaying.mp4"

ls -la "$OUT"/dj42-*.mp4
