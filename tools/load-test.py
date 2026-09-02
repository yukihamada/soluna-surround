"""SOLUNA load test: N WebSocket listeners on channel 'loadtest' → ping RTT (burst + settled), then one CUE fanout.
   HOST=host:port SOLUNA_ADMIN=<token> python3 tools/load-test.py 2000
   Cleans up with DELETE /api/channel. Run it ON the server box to measure the server, from a laptop to measure the network."""
import asyncio, json, os, statistics, sys, time, urllib.request
import websockets
HOST=os.environ.get("HOST","127.0.0.1:8900"); N=int(sys.argv[1]) if len(sys.argv)>1 else 500
TOK=os.environ.get("SOLUNA_ADMIN") or open(os.environ.get("SOLUNA_ADMIN_FILE", "/opt/soluna/admin-token")).read().strip()
CH="loadtest"; got=[]; rtts=[]; rtts2=[]; fails=0
ping2=asyncio.Event(); ready2=asyncio.Semaphore(0)

async def client(i, ready, fire):
    global fails
    try:
        async with websockets.connect(f"ws://{HOST}/audio?role=listen&ch={CH}&zone=A", open_timeout=30, max_size=2**20) as ws:
            t0=time.time(); await ws.send(json.dumps({"t":"ping","c":int(t0*1000)}))
            while True:
                m=await asyncio.wait_for(ws.recv(), 30)
                if isinstance(m,str) and '"pong"' in m: rtts.append((time.time()-t0)*1000); break
            ready.release()
            await ping2.wait()
            t1=time.time(); await ws.send(json.dumps({"t":"ping","c":int(t1*1000)}))
            while True:
                m=await asyncio.wait_for(ws.recv(), 30)
                if isinstance(m,str) and '"pong"' in m: rtts2.append((time.time()-t1)*1000); break
            ready2.release()
            await fire.wait()
            while True:
                m=await asyncio.wait_for(ws.recv(), 30)
                if isinstance(m,str) and ('"preload"' in m or '"cue"' in m): got.append(time.time()); return
    except Exception as e:
        fails+=1
        if fails<=3: print("fail:", repr(e)[:120])

async def main():
    ready=asyncio.Semaphore(0); fire=asyncio.Event()
    t_conn=time.time()
    tasks=[asyncio.create_task(client(i,ready,fire)) for i in range(N)]
    for _ in range(N):
        try: await asyncio.wait_for(ready.acquire(), 60)
        except asyncio.TimeoutError: break
    t_all=time.time()-t_conn
    st=json.load(urllib.request.urlopen(f"http://{HOST}/status"))["channels"].get(CH,{})
    print(f"N={N} connected+synced={len(rtts)} fails={fails} in {t_all:.1f}s  server listeners={st.get('listeners')}")
    print(f"ping RTT ms: median={statistics.median(rtts):.1f} p95={sorted(rtts)[int(len(rtts)*.95)-1]:.1f} max={max(rtts):.1f}")
    await asyncio.sleep(5)   # let the connect burst settle, then measure steady-state RTT
    ping2.set()
    for _ in range(len(rtts)):
        try: await asyncio.wait_for(ready2.acquire(), 60)
        except asyncio.TimeoutError: break
    print(f"settled ping RTT ms: median={statistics.median(rtts2):.1f} p95={sorted(rtts2)[int(len(rtts2)*.95)-1]:.1f} max={max(rtts2):.1f}")
    req=urllib.request.Request(f"http://{HOST}/api/cue?ch={CH}", data=json.dumps({"url":"/assets/fest_demo.mp3","lead":30,"preload":True}).encode(),
        headers={"x-soluna-admin":TOK,"content-type":"application/json"}, method="POST")
    t_fire=time.time(); urllib.request.urlopen(req).read(); fire.set()
    await asyncio.wait(tasks, timeout=30)
    lat=sorted((t-t_fire)*1000 for t in got)
    if lat: print(f"CUE fanout: received={len(got)}/{len(rtts)}  first={lat[0]:.0f}ms median={lat[len(lat)//2]:.0f}ms last={lat[-1]:.0f}ms")
    for t in tasks: t.cancel()
    await asyncio.sleep(2)
    urllib.request.urlopen(urllib.request.Request(f"http://{HOST}/api/channel?ch={CH}", headers={"x-soluna-admin":TOK}, method="DELETE")).read()
asyncio.run(main())
