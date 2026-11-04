#!/usr/bin/env python3
"""DJ42の曲別ショー定義(show-*.js)を SOLUNA Sound のセットリストJSONに変換する。

入力: ~/workspace/zamna-hawaii/production/soluna-light-20261025/crowd-view/
        show-<track>.js   (window.SHOW = {duration, bpm, seg:[{at,scene,title,desc}], states:[...]})
        audio/<track>.m4a
出力: tools/dj42-setlist.json  (POST /api/show にそのまま渡せる)
      assets/dj42-<track>.m4a  (既にコピー済みならスキップ)

各曲 = 1ステップ。曲中のシーン切替は step.timeline=[{at, light}, ...] として埋め込む。
シーン→ssパターンの対応:
  idle→pulse(金) / harp→audio / pulse→pulse / wave→wave+sweep / rainbow→beat(虹)
  message→solid(金) / drop→solid白→pulse / hold→solid(暗) / blackout→solid(黒) / fire→plasma(暖)
"""
import json
import os
import re
import sys

CROWD = os.path.expanduser(
    "~/workspace/zamna-hawaii/production/soluna-light-20261025/crowd-view")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

TRACKS = [  # (file stem, 表示名, 音源ファイル名, スクリーン映像)
    ("aetheria", "Aetheria", "dj42-aetheria.m4a", "dj42-aetheria-screen.mp4"),
    ("vip-sovereignty", "40Hz VIP Sovereignty", "dj42-vip-sovereignty.m4a", "dj42-vip-sovereignty-screen.mp4"),
    ("emotional-gravity", "Emotional Gravity", "dj42-emotional-gravity.m4a", "dj42-emotional-gravity-screen.mp4"),
    ("aura", "We Got the AURA", "dj42-aura.m4a", "dj42-aura-screen.mp4"),
    ("amapiano", "Quantum Afro-Amapiano", "dj42-amapiano.m4a", "dj42-amapiano-screen.mp4"),
    ("prada", "PRADA (Mainstage Techno Overdrive)", "dj42-prada.m4a", "dj42-prada-screen.mp4"),
]

GOLD = "#d4af37"

# 会場ゾーン: ステージからの距離順 A(最前)→F(最後方)。Echoa Ridge 暫定距離。
# 実区画図(site plan)が届いたら server の /api/zones で上書きする。
N_ZONES = 6
ZONE_DIST_M = {"A": 0, "B": 15, "C": 30, "D": 45, "E": 60, "F": 80}


def hsl_hex(h, s=0.9, l=0.55):
    """HSL(0..1) → #rrggbb。"""
    def f(n):
        k = (n + h * 12) % 12
        a = s * min(l, 1 - l)
        return round(255 * (l - a * max(-1, min(k - 3, 9 - k, 1))))
    return "#%02x%02x%02x" % (f(0), f(8), f(4))


def scene_light(scene, hue, bpm, text):
    """crowd-view のシーンを ss の light 定義へ。"""
    h = hue if isinstance(hue, (int, float)) else 43 / 360
    c1 = hsl_hex(h, 0.9, 0.55)
    c2 = hsl_hex((h + 0.5) % 1.0, 0.9, 0.55)
    if scene == "blackout":
        return {"pattern": "solid", "colors": ["#000000", "#000000"], "bpm": bpm}
    if scene == "hold":
        return {"pattern": "solid", "colors": [GOLD, GOLD], "bpm": bpm,
                "brightness": 0.12, "fade_ms": 2000}
    if scene == "pulse":
        return {"pattern": "pulse", "colors": [c1, c2], "bpm": bpm}
    if scene == "harp":
        return {"pattern": "audio", "colors": [GOLD, "#ffffff"], "bpm": bpm}
    if scene == "wave":
        # crowd-view と同じ「2拍でゾーン A→F を1周」。period_s = 2拍秒 / ゾーン数(6)。
        # 端末は自分のゾーンだけ光るので、サーバの sweep が前→後ろへ波を流す。
        period = round((120.0 / bpm) / N_ZONES, 3)
        return {"pattern": "wave", "colors": [c1, c2], "bpm": bpm,
                "zones_auto": {"mode": "sweep", "period_s": period, "width": 1}}
    if scene == "rainbow":
        return {"pattern": "beat", "colors": ["#ff2a00", "#00e0ff"], "bpm": bpm}
    if scene == "message":
        return {"pattern": "solid", "colors": [GOLD, GOLD], "bpm": bpm, "brightness": 0.5}
    if scene == "drop":
        return {"pattern": "solid", "colors": ["#ffffff", "#ffffff"], "bpm": bpm,
                "fade_ms": 80, "torch": True}
    if scene == "fire":
        return {"pattern": "plasma", "colors": ["#ff5a00", "#ffd070"], "bpm": bpm}
    # idle(既定): 金のゆっくり呼吸
    return {"pattern": "pulse", "colors": [GOLD, GOLD], "bpm": bpm}


def load_show(stem):
    path = os.path.join(CROWD, f"show-{stem}.js")
    src = open(path, encoding="utf-8").read()
    m = re.search(r"window\.SHOW\s*=\s*(\{.*\})\s*;?\s*$", src, re.S)
    if not m:
        raise SystemExit(f"window.SHOW not found in {path}")
    return json.loads(m.group(1))


def main():
    steps = []
    for stem, title, audio, video in TRACKS:
        sh = load_show(stem)
        bpm = sh.get("bpm") or 120
        # 各 seg の hue を states から引く(無ければ既定)
        hue_by_at = {round(s["at"], 3): s["st"].get("hue") for s in sh.get("states", [])}
        timeline = []
        for seg in sh["seg"]:
            at = round(float(seg["at"]), 3)
            light = scene_light(seg["scene"], hue_by_at.get(round(at, 3)), bpm, seg.get("title"))
            timeline.append({"at": at, "scene": seg["scene"],
                             "title": seg.get("title") or seg["scene"], "light": light})
        step = {
            "label": title,
            "url": f"/assets/{audio}",
            "dur": round(sh["duration"] / 1000.0, 3),
            "light": timeline[0]["light"] if timeline else {"pattern": "pulse"},
            "timeline": timeline,
        }
        if video:
            step["video"] = f"/assets/{video}"
        steps.append(step)
    out = {"auto": True, "gap": 1.0, "steps": steps}
    dest = os.path.join(HERE, "dj42-setlist.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"wrote {dest}: {len(steps)} steps, "
          f"{sum(len(s['timeline']) for s in steps)} timeline entries")
    for s in steps:
        print(f"  {s['label']:34s} {s['dur']:7.1f}s  {len(s['timeline'])} scenes")


if __name__ == "__main__":
    main()
