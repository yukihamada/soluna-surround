#!/usr/bin/env python3
"""DJ42リモコンの回帰テスト: 音・光・映像のON/OFFと曲中タイムライン(ハープ等)。

回帰の対象(実際に本番で起きた不具合):
  - 音OFF・映像OFF(光だけ運用)で do_show が UnboundLocalError('at') で落ちていた。
  - 音・映像OFFのとき、曲中タイムライン(ハープ→audio 等)が発火しなかった。

  python3 tests/test_dj42_timeline.py
サーバは子プロセス(tests/_server.py)。"""
import asyncio, json, os, sys, time
import aiohttp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server import ServerProc, ADMIN

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name); print(f"  {'✅' if cond else '❌'} {name} {detail}")


async def main():
    with ServerProc(port=8938) as sp_:
        BASE = f"http://127.0.0.1:{sp_.port}"
        async with aiohttp.ClientSession() as s:
            H = {"x-soluna-admin": ADMIN}
            ws = await s.ws_connect(f"{BASE}/audio?role=listen&ch=festival&zone=A")
            async def recv_json(pred, timeout=3):
                end = time.time() + timeout
                while time.time() < end:
                    m = await asyncio.wait_for(ws.receive(), max(0.05, end - time.time()))
                    if m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if pred(d): return d
                return None
            await recv_json(lambda d: d.get("t") == "config")

            # 曲: 音源URL + 光 + 曲中タイムライン(0.4sでハープ=audio、1.2sでpulse)
            step = {
                "label": "T", "url": "/assets/x.mp3", "dur": 30,
                "light": {"pattern": "pulse", "colors": ["#d4af37", "#d4af37"], "bpm": 120},
                "timeline": [
                    {"at": 0.4, "light": {"pattern": "audio", "colors": ["#d4af37", "#ffffff"], "bpm": 120}},
                    {"at": 1.2, "light": {"pattern": "pulse", "colors": ["#25b6f4", "#f46325"], "bpm": 120}},
                ],
                "stems": {"drums": "/assets/dj42-T-drums.m4a",
                          "vocals": "/assets/dj42-T-vocals.m4a"},
            }
            r = await s.post(f"{BASE}/api/show?ch=festival", json={"steps": [step]}, headers=H)
            check("show 登録", r.status == 200, str(r.status))

            # --- 1) 音ON・光ON: タイムラインがハープ(audio)へ進む ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "audio": True, "light": True, "video": False})
            body = await r.json()
            check("音ON・光ON: play 200", r.status == 200 and body.get("ok"), str(body))
            check("音ON・光ON: timeline 起動", body.get("timeline") == 2, str(body.get("timeline")))
            tl = await recv_json(lambda d: d.get("t") == "light" and d.get("pattern") == "audio", timeout=2)
            check("音ON・光ON: 0.4sでハープ(audio)発火", tl is not None, str(tl and tl.get("pattern")))

            # --- 2) 音OFF・映像OFF(光だけ): クラッシュせず、タイムラインも動く ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "audio": False, "light": True, "video": False})
            body = await r.json()
            check("音OFF・映像OFF: play 200(旧: UnboundLocalErrorで500)", r.status == 200 and body.get("ok"), str(body))
            check("音OFF・映像OFF: timeline 起動", body.get("timeline") == 2, str(body.get("timeline")))
            check("音OFF・映像OFF: cue は送らない", body.get("cue") is None and body.get("cue_stopped"), str(body))
            tl = await recv_json(lambda d: d.get("t") == "light" and d.get("pattern") == "audio", timeout=2)
            check("音OFF・映像OFF: 0.4sでハープ(audio)発火", tl is not None, str(tl and tl.get("pattern")))

            # --- 3) 光OFF: light_stop が届き、タイムラインは止まる ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "audio": True, "light": False, "video": False})
            body = await r.json()
            check("光OFF: play 200", r.status == 200 and body.get("ok"), str(body))
            check("光OFF: timeline は起動しない", body.get("timeline") is None, str(body.get("timeline")))
            check("光OFF: light_stopped", body.get("light_stopped") is True, str(body))
            ls = await recv_json(lambda d: d.get("t") == "light_stop", timeout=2)
            check("光OFF: light_stop 配信", ls is not None)

            # --- 4) 誤キーは403 ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "nope", "action": "play", "i": 0})
            check("誤キー403", r.status == 403, str(r.status))

            # --- 4b) ステム(方式C): 選んだパートだけを cue の url に載せる ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "stem": "drums",
                                   "audio": True, "light": True, "video": False})
            body = await r.json()
            check("ステムdrums: play 200", r.status == 200 and body.get("ok"), str(body))
            check("ステムdrums: stem=drums を返す", body.get("stem") == "drums", str(body.get("stem")))
            cue = await recv_json(lambda d: d.get("t") == "cue", timeout=2)
            check("ステムdrums: cue.url がステム音源", cue is not None and cue.get("url") == "/assets/dj42-T-drums.m4a",
                  str(cue and cue.get("url")))
            check("ステムdrums: 映像は送らない", cue is not None and "video" not in cue, str(cue))

            # --- 4c) 未知のステムは400 ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "stem": "nope",
                                   "audio": True, "light": True})
            check("未知ステム400", r.status == 400, str(r.status))

            # --- 4c2) 複数パート同時再生(方式C拡張): cue.urls に両方載る ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "stem": "drums,vocals",
                                   "audio": True, "light": True, "video": False})
            body = await r.json()
            check("複数パート: play 200", r.status == 200 and body.get("ok"), str(body))
            check("複数パート: stem をそのまま返す", body.get("stem") == "drums,vocals", str(body.get("stem")))
            cue = await recv_json(lambda d: d.get("t") == "cue", timeout=2)
            check("複数パート: cue.urls が2音源",
                  cue is not None and cue.get("urls") == ["/assets/dj42-T-drums.m4a", "/assets/dj42-T-vocals.m4a"],
                  str(cue and cue.get("urls")))
            check("複数パート: cue.url は先頭パート",
                  cue is not None and cue.get("url") == "/assets/dj42-T-drums.m4a", str(cue and cue.get("url")))

            # --- 4c3) 複数指定で1つでも未知なら400 ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "stem": "drums,nope",
                                   "audio": True, "light": True})
            check("複数パート: 未知混在400", r.status == 400, str(r.status))

            # --- 4d) full は通常の url のまま ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "stem": "full",
                                   "audio": True, "light": True, "video": False})
            body = await r.json()
            cue = await recv_json(lambda d: d.get("t") == "cue", timeout=2)
            check("ステムfull: cue.url は通常音源", cue is not None and cue.get("url") == "/assets/x.mp3",
                  str(cue and cue.get("url")))

            # --- 5) stop で音・光とも停止 ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "stop"})
            check("stop 200", r.status == 200 and (await r.json()).get("stopped"))

            # --- 6) ブース用: 曲を止めずにライトだけ変える ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "light", "pattern": "wave",
                                   "colors": ["#7cc4ff", "#c084fc"], "brightness": 0.8})
            body = await r.json()
            check("light: 200", r.status == 200 and body.get("ok"), str(body))
            check("light: pattern=wave", (body.get("light") or {}).get("pattern") == "wave", str(body.get("light")))
            lm = await recv_json(lambda d: d.get("t") == "light" and d.get("pattern") == "wave", timeout=2)
            check("light: light 配信", lm is not None, str(lm and lm.get("pattern")))

            # --- 6b) light_stop でライトだけ消す ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "light_stop"})
            body = await r.json()
            check("light_stop: 200", r.status == 200 and body.get("stopped"), str(body))
            ls = await recv_json(lambda d: d.get("t") == "light_stop", timeout=2)
            check("light_stop: light_stop 配信", ls is not None)

            # --- 6c) mute キルスイッチ: mute メッセージが配信される ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "mute", "on": True})
            body = await r.json()
            check("mute on: 200", r.status == 200 and body.get("mute") is True, str(body))
            mm = await recv_json(lambda d: d.get("t") == "mute", timeout=2)
            check("mute on: mute 配信", mm is not None and mm.get("on") is True, str(mm))
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "mute", "on": False})
            body = await r.json()
            check("mute off: 200", r.status == 200 and body.get("mute") is False, str(body))

            # --- 7) stop で音・光とも停止 ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "stop"})
            check("stop 200", r.status == 200 and (await r.json()).get("stopped"))

            # --- 8) ブース: オート進行 ON/OFF と曲間ギャップ ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "auto", "on": True, "gap": 2.5})
            body = await r.json()
            check("auto on: 200", r.status == 200 and body.get("auto") is True, str(body))
            check("auto on: gap=2.5", body.get("gap") == 2.5, str(body.get("gap")))
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "auto", "on": False})
            body = await r.json()
            check("auto off: 200", r.status == 200 and body.get("auto") is False, str(body))

            # --- 9) ブース: 先読み(preload) ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "preload", "i": 0})
            body = await r.json()
            check("preload: 200", r.status == 200 and body.get("preloaded"), str(body))
            pm = await recv_json(lambda d: d.get("t") == "preload", timeout=2)
            check("preload: preload 配信", pm is not None and pm.get("url") == "/assets/x.mp3", str(pm))

            # --- 10) ブース: 現在曲のループ/ゲイン(retune) ---
            r = await s.post(f"{BASE}/api/dj42",
                             json={"key": "dj42", "action": "play", "i": 0, "audio": True, "light": True})
            await r.json()
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "retune", "loop": True, "gain": 0.8})
            body = await r.json()
            check("retune: 200", r.status == 200 and body.get("ok"), str(body))
            check("retune: loop=True", body.get("loop") is True, str(body))
            check("retune: gain=0.8", abs((body.get("gain") or 0) - 0.8) < 1e-6, str(body.get("gain")))

            # --- 10b) retune: 再生中でなければ400 ---
            await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "stop"})
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "retune", "loop": True})
            check("retune: 停止中は400", r.status == 400, str(r.status))

            await ws.close()

    print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
    if ng:
        print("FAILED:", ng); sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
