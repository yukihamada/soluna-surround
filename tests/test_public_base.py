#!/usr/bin/env python3
"""公開ドメインの正本(SOLUNA_PUBLIC_BASE)が QR・案内ページに効いているか。

会場で配るQRが fly.dev や 127.0.0.1 を指すと、来場者が別ホストへ飛ぶ事故になる。
設定した時は必ずそのドメイン、未設定なら従来どおり request.host になることを確認する。
"""
import asyncio, json, os, sys
import aiohttp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server import ServerProc

PUB = "https://sound.solun.art"
ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")

async def with_public(port):
    base = f"http://127.0.0.1:{port}"
    with ServerProc(env={"SOLUNA_PUBLIC_BASE": PUB}, port=port):
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{base}/guide")
            t = await r.text()
            check("/guide 200", r.status == 200)
            check("/guide のQR/URLが公開ドメイン", PUB in t)
            check("/guide に fly.dev が無い", "fly.dev" not in t)
            check("/guide に 127.0.0.1 が無い", "127.0.0.1" not in t)

            r = await s.get(f"{base}/flags?ch=pubbase")
            t = await r.text()
            check("/flags 200", r.status == 200)
            check("/flags のQRが公開ドメイン", PUB in t and "127.0.0.1" not in t)

            r = await s.get(f"{base}/flags?ch=pubbase&gate=1")
            t = await r.text()
            check("入場QRも公開ドメイン", PUB in t and "127.0.0.1" not in t)

            r = await s.get(f"{base}/api/qr?ch=festival&zone=A")
            check("/api/qr 200", r.status == 200)
            check("/api/qr はSVG", r.headers.get("Content-Type", "").startswith("image/svg"))

            # 明示 base= は従来どおり上書きできる(印刷時の検証用)
            r = await s.get(f"{base}/guide?base=https://example.test")
            t = await r.text()
            check("?base= で上書きできる", "https://example.test" in t)

            # config でも公開ドメインを配る(クライアントのシェアQR用)
            ws = await s.ws_connect(f"ws://127.0.0.1:{port}/audio?role=listen&ch=festival&zone=A")
            cfg = None
            for _ in range(20):
                m = await ws.receive(timeout=2)
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("t") == "config":
                        cfg = d; break
            await ws.close()
            check("config.public_base を配信", bool(cfg) and cfg.get("public_base") == PUB, cfg and cfg.get("public_base"))

async def without_public(port):
    base = f"http://127.0.0.1:{port}"
    with ServerProc(env={"SOLUNA_PUBLIC_BASE": ""}, port=port):
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{base}/guide")
            t = await r.text()
            check("未設定なら request.host を使う", r.status == 200 and "127.0.0.1" in t)

port = int(os.environ.get("SOLUNA_TEST_PORT", "8931"))
asyncio.run(with_public(port + 20))
asyncio.run(without_public(port + 21))
print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
sys.exit(1 if ng else 0)
