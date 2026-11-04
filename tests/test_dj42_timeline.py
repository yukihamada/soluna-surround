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

            # --- 5) stop で音・光とも停止 ---
            r = await s.post(f"{BASE}/api/dj42", json={"key": "dj42", "action": "stop"})
            check("stop 200", r.status == 200 and (await r.json()).get("stopped"))

            await ws.close()

    print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
    if ng:
        print("FAILED:", ng); sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
