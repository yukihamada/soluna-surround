#!/usr/bin/env python3
"""DJトークン認証 + 1000接続負荷試験(キュー配信の到達率/ばらつき/クロック安定)."""
import asyncio, json, time, sys, os
import aiohttp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server import ServerProc

PORT2 = 8932   # SOLUNA_DJ_TOKEN 付きサーバ
N_LOAD = int(os.environ.get("SOLUNA_LOAD_N", "1000"))
ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")

async def main():
    conn = aiohttp.TCPConnector(limit=0)     # 既定100だと1000WSがプール待ちで固まる
    async with aiohttp.ClientSession(connector=conn) as s:
        # --- DJ auth ---
        try:
            await s.ws_connect(f"ws://127.0.0.1:{PORT2}/audio?role=push&ch=festival")
            check("DJトークン無しpush拒否", False, "接続できてしまった")
        except aiohttp.WSServerHandshakeError as e:
            check("DJトークン無しpush=403", e.status == 403)
        push = await s.ws_connect(
            f"ws://127.0.0.1:{PORT2}/audio?role=push&ch=festival&token=dj-secret")
        check("DJトークン有りpush=OK", True)
        listen = await s.ws_connect(f"ws://127.0.0.1:{PORT2}/audio?role=listen&ch=festival")
        check("リスナーは常にオープン", True)
        await push.close(); await listen.close()

        # --- 1000接続負荷 ---
        N = N_LOAD
        conns, recv_at = [], {}
        t0 = time.time()
        async def connect_one(i):
            ws = await s.ws_connect(f"ws://127.0.0.1:{PORT2}/audio?role=listen&ch=load&zone=A")
            conns.append(ws)
            async def rd():
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        m = json.loads(msg.data)
                        if m.get("t") == "cue":
                            recv_at[i] = (time.time(), m["at"])
            return asyncio.create_task(rd())
        tasks = await asyncio.gather(*(connect_one(i) for i in range(N)))
        t_conn = time.time() - t0
        check(f"{N}接続確立", len(conns) == N, f"{t_conn:.1f}s")

        # ping応答が負荷下でも健全か(50台がping)
        async def ping_one(ws):
            t = time.time() * 1000
            await ws.send_str(json.dumps({"t": "ping", "c": t}))
        for ws in conns[:50]:
            await ping_one(ws)

        # キュー発火 → 全端末到達と配信ばらつきを実測
        t_fire = time.time()
        r = await s.post(f"http://127.0.0.1:{PORT2}/api/cue?ch=load",
                         json={"url": "/assets/x.mp3", "lead": 3},
                         headers={"x-soluna-admin": "test-admin-token"})
        check("負荷下cue API 200", r.status == 200)
        await asyncio.sleep(3.0)
        n_recv = len(recv_at)
        check(f"cue到達 {n_recv}/{N}", n_recv == N)
        if recv_at:
            times = [t for t, _ in recv_at.values()]
            spread = (max(times) - min(times)) * 1000
            first_delay = (min(times) - t_fire) * 1000
            ats = {a for _, a in recv_at.values()}
            check("全端末のat完全一致", len(ats) == 1)
            check("配信ばらつき < 1000ms", spread < 1000,
                  f"spread={spread:.0f}ms first={first_delay:.0f}ms")
            check("lead(3s)内に全端末到達", max(times) - t_fire < 3.0,
                  f"最遅={(max(times)-t_fire)*1000:.0f}ms")
        for ws in conns:
            await ws.close()
        for t in tasks:
            t.cancel()

    print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
    if ng:
        print("FAILED:", ng); sys.exit(1)

if __name__ == "__main__":
    with ServerProc(env={"SOLUNA_DJ_TOKEN": "dj-secret"}, port=PORT2):
        asyncio.run(main())
