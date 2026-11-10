#!/usr/bin/env python3
"""観客ページのマイク反応カラー(既定)と会場同期フォールバックの静的検査。

実ブラウザ検証は手元の Playwright で行う(CI には重い依存を足さない)。
ここでは「壊れたら気づく」最小条件だけを確認する。
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "client.html"), encoding="utf-8").read()

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")

check("マイク層がある", 'id="micLayer"' in html)
check("切替ボタン(マイク/同期)がある",
      'id="senseMic"' in html and 'id="senseSync"' in html)
check("既定はマイクモード", re.search(r"let senseMode\s*=\s*'mic'", html) is not None)
check("getUserMedia でマイク取得", "navigator.mediaDevices.getUserMedia" in html)
check("解析のみ(destinationへ繋がない)",
      "src.connect(micAnalyser)" in html and "micAnalyser.connect(ctx.destination)" not in html)
check("マイク不可→同期へフォールバック", "fallbackToSync(" in html)
check("参加タップ後にマイク開始", re.search(r"if \(senseMode === 'mic'\) startMicSense\(\)", html) is not None)
check("停止時にマイクを解放", re.search(r"function stop\(\)\{[\s\S]{0,300}?stopMicSense\(\)", html) is not None)
check("マイク中は会場ライトを重ねない", re.search(r"if \(senseMode === 'mic'\)\{[\s\S]{0,120}?return;", html) is not None)
check("切替UIはライトより手前(z-index)", "#senseRow{position:relative;z-index:101}" in html)
check("日英の文言がある", "senseMic:'🎤 マイクで色が変わる'" in html and "senseMic:'🎤 Color from mic'" in html)

print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
sys.exit(1 if ng else 0)
