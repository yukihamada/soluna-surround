#!/usr/bin/env python3
"""SOLUNA Sound プロトコル検証(実測): サーバを子プロセスで起動して叩く。
    python3 tests/test_protocol.py
"""
import asyncio, json, struct, time, sys, os
import aiohttp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _server import ServerProc, PORT, ADMIN, DATA_DIR

BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/audio"
HEADER = struct.Struct("<3sBBBIId")
ok, ng = [], []

def check(name, cond, detail=""):
    (ok if cond else ng).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")

async def listener(session, zone, inbox):
    ws = await session.ws_connect(f"{WS}?role=listen&ch=festival&zone={zone}")
    async def reader():
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                inbox.append(json.loads(msg.data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                inbox.append(("bin", msg.data))
    task = asyncio.create_task(reader())
    return ws, task

async def main():
    async with aiohttp.ClientSession() as s:
        # 1) 3台接続 → config 受信
        boxes = [[], [], []]
        conns = []
        for i, z in enumerate(["A", "C", "F"]):
            conns.append(await listener(s, z, boxes[i]))
        await asyncio.sleep(0.3)
        for i in range(3):
            cfgs = [m for m in boxes[i] if isinstance(m, dict) and m.get("t") == "config"]
            check(f"listener{i} config受信", len(cfgs) == 1,
                  f"zones={cfgs[0]['zones'] if cfgs else None}")
        cfg = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "config"][0]
        check("既定ゾーンF遅延 = 80/343*1000+15", abs(cfg["zones"]["F"] - (80/343*1000+15)) < 0.2,
              f"F={cfg['zones']['F']}ms")

        # 2) ping/pong クロック同期
        ws0 = conns[0][0]
        t0 = time.time() * 1000
        await ws0.send_str(json.dumps({"t": "ping", "c": 12345.0}))
        await asyncio.sleep(0.2)
        pongs = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "pong"]
        check("pong受信+cエコー", len(pongs) == 1 and pongs[0]["c"] == 12345.0)
        check("pongサーバ時刻が現実的", pongs and abs(pongs[0]["s"] - t0) < 500,
              f"diff={abs(pongs[0]['s']-t0):.1f}ms" if pongs else "")

        # 3) 認証: トークン無し → 403
        r = await s.post(f"{BASE}/api/cue?ch=festival", json={"url": "/assets/x.mp3"})
        check("admin無トークン=403", r.status == 403)

        # 4) キュー一斉配信 (lead=2, サーバ時計基準)
        t_req = time.time()
        r = await s.post(f"{BASE}/api/cue?ch=festival",
                         json={"url": "/assets/opening.mp3", "lead": 2, "gain": 0.8},
                         headers={"x-soluna-admin": ADMIN})
        body = await r.json()
        check("cue API 200", r.status == 200, str(body.get("cue", {}).get("id")))
        await asyncio.sleep(0.3)
        cues = [[m for m in b if isinstance(m, dict) and m.get("t") == "cue"] for b in boxes]
        check("全3台がcue受信", all(len(c) == 1 for c in cues))
        ats = {c[0]["at"] for c in cues if c}
        check("3台のatが完全一致", len(ats) == 1, f"at={ats}")
        at = ats.pop()
        check("at ≈ サーバnow+2s", 1.5 < at - t_req < 2.5, f"lead実測={at-t_req:.2f}s")

        # 5) 途中参加 → 接続時に進行中cueを受信
        late_box = []
        late = await listener(s, "B", late_box)
        await asyncio.sleep(0.3)
        late_cues = [m for m in late_box if isinstance(m, dict) and m.get("t") == "cue"]
        check("途中参加者がcue受信", len(late_cues) == 1 and late_cues[0]["at"] == at)

        # 6) ゾーン更新 → 全員に config 再配信
        r = await s.post(f"{BASE}/api/zones?ch=festival",
                         json={"zones": {"A": 15, "B": 60.7, "C": 102.5}},
                         headers={"x-soluna-admin": ADMIN})
        check("zones API 200", r.status == 200)
        await asyncio.sleep(0.3)
        cfg2 = [m for m in boxes[1] if isinstance(m, dict) and m.get("t") == "config"]
        check("ゾーン更新が再配信される", len(cfg2) == 2 and cfg2[-1]["zones"]["B"] == 60.7)

        # 7) ライブPCM: push→mono抽出+playAt付与
        push = await s.ws_connect(f"{WS}?role=push&ch=festival")
        await push.send_str(json.dumps({"t": "hello", "map": ["L", "R", "C"], "sr": 48000}))
        nsamp = 960
        pcm = b"".join(struct.pack("<hhh", 100, 200, 300) * 1 for _ in range(nsamp))
        frame = HEADER.pack(b"SL2", 2, 3, 0, 7, nsamp, 0.0) + pcm
        await push.send_bytes(frame)
        await asyncio.sleep(0.3)
        bins = [m for m in boxes[0] if isinstance(m, tuple) and m[0] == "bin"]
        check("リスナーがバイナリ受信", len(bins) == 1)
        if bins:
            magic, ver, nchan, _p, seq, ns, play_at = HEADER.unpack_from(bins[0][1], 0)
            mono = struct.unpack_from(f"<{ns}h", bins[0][1], HEADER.size)
            check("mono抽出(zone=AはL=idx0)", nchan == 1 and ns == nsamp and mono[0] == 100,
                  f"sample0={mono[0]}")
            check("playAt=未来のサーバ時刻", time.time() < play_at < time.time() + 1.5,
                  f"lead={play_at-time.time():.2f}s")

        # 8) cue stop
        r = await s.post(f"{BASE}/api/cue?ch=festival", json={"stop": True},
                         headers={"x-soluna-admin": ADMIN})
        await asyncio.sleep(0.3)
        stops = [m for m in boxes[2] if isinstance(m, dict) and m.get("t") == "cue_stop"]
        check("cue_stop配信", len(stops) == 1)

        # 9) align API: base_ms 一括トリムが config で全員に届く
        r = await s.post(f"{BASE}/api/align?ch=festival", json={"base_ms": -40},
                         headers={"x-soluna-admin": ADMIN})
        check("align API 200", r.status == 200)
        await asyncio.sleep(0.3)
        cfg3 = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "config"]
        check("base_ms=-40 が再配信される", cfg3 and cfg3[-1].get("base_ms") == -40.0)

        # 10) 低遅延 lead: hello {lead:0.08} → playAt の先読みが縮む(ハウスPA融合)
        fb = []
        fus = await listener(s, "B", fb)
        push2 = await s.ws_connect(f"{WS}?role=push&ch=fusion")
        await push2.send_str(json.dumps({"t": "hello", "map": ["L", "R", "C"],
                                         "sr": 48000, "lead": 0.08}))
        await asyncio.sleep(0.1)
        fus2_box = []
        ws_f = await s.ws_connect(f"{WS}?role=listen&ch=fusion&zone=B")
        async def rd():
            async for msg in ws_f:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    fus2_box.append(msg.data)
        rt = asyncio.create_task(rd())
        await asyncio.sleep(0.1)
        t_send = time.time()
        await push2.send_bytes(HEADER.pack(b"SL2", 2, 3, 0, 1, 960, 0.0) + b"\0" * (960*3*2))
        await asyncio.sleep(0.2)
        check("fusion: フレーム受信", len(fus2_box) == 1)
        if fus2_box:
            *_, play_at2 = HEADER.unpack_from(fus2_box[0], 0)
            lead_meas = play_at2 - t_send
            check("fusion: lead≈0.08s(<0.15)", 0.0 < lead_meas < 0.15,
                  f"lead実測={lead_meas*1000:.0f}ms")
        await push2.close(); await ws_f.close(); rt.cancel()
        await fus[0].close(); fus[1].cancel()

        # 10b) zones_m: 距離[m]→delay自動計算
        r = await s.post(f"{BASE}/api/zones?ch=festival",
                         json={"zones_m": {"A": 0, "B": 34.3}},
                         headers={"x-soluna-admin": ADMIN})
        b = await r.json()
        check("zones_m: 34.3m → 115.0ms", b["zones"]["B"] == 115.0, str(b["zones"]))

        # 10c) /api/assets 一覧
        os.makedirs(os.path.join(DATA_DIR, "assets"), exist_ok=True)
        with open(os.path.join(DATA_DIR, "assets", "_test.mp3"), "wb") as f:
            f.write(b"\x00" * 128)
        r = await s.get(f"{BASE}/api/assets?ch=festival",
                        headers={"x-soluna-admin": ADMIN})
        aj = await r.json()
        names = [a["name"] for a in aj["assets"]]
        check("assets一覧に_test.mp3", "_test.mp3" in names, str(names))
        os.remove(os.path.join(DATA_DIR, "assets", "_test.mp3"))

        # 10d) light API: パターン一斉配信+途中参加+stop
        r = await s.post(f"{BASE}/api/light?ch=festival",
                         json={"pattern": "wave", "colors": ["#d4af37", "#7fc9a2"],
                               "bpm": 128, "brightness": 0.8},
                         headers={"x-soluna-admin": ADMIN})
        lb = await r.json()
        check("light API 200", r.status == 200 and lb["light"]["pattern"] == "wave")
        await asyncio.sleep(0.3)
        lgt = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "light"]
        check("全端末がlight受信", len(lgt) == 1 and lgt[0]["bpm"] == 128.0)
        late2_box = []
        late2 = await listener(s, "D", late2_box)
        await asyncio.sleep(0.3)
        l2 = [m for m in late2_box if isinstance(m, dict) and m.get("t") == "light"]
        check("途中参加者がlight受信", len(l2) == 1 and l2[0]["pattern"] == "wave")
        r = await s.post(f"{BASE}/api/light?ch=festival",
                         json={"pattern": "nope"}, headers={"x-soluna-admin": ADMIN})
        check("不正pattern=400", r.status == 400)
        r = await s.post(f"{BASE}/api/light?ch=festival", json={"stop": True},
                         headers={"x-soluna-admin": ADMIN})
        await asyncio.sleep(0.3)
        ls_ = [m for m in boxes[1] if isinstance(m, dict) and m.get("t") == "light_stop"]
        check("light_stop配信", len(ls_) == 1)
        await late2[0].close(); late2[1].cancel()

        # 10e) geo API: ステージ座標の登録+config配信+clear
        r = await s.post(f"{BASE}/api/geo?ch=festival",
                         json={"lat": 21.3352, "lng": -158.0879},
                         headers={"x-soluna-admin": ADMIN})
        gb = await r.json()
        check("geo API 200", r.status == 200 and gb["geo"]["lat"] == 21.3352)
        await asyncio.sleep(0.3)
        gcfg = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "config"
                and m.get("geo")]
        check("geo入りconfig再配信", len(gcfg) >= 1 and gcfg[-1]["geo"]["lng"] == -158.0879)
        r = await s.post(f"{BASE}/api/geo?ch=festival", json={"bad": 1},
                         headers={"x-soluna-admin": ADMIN})
        check("geo不正body=400", r.status == 400)

        # 10f) preload: 再生せず全端末へ配布+途中参加にも配布
        r = await s.post(f"{BASE}/api/cue?ch=festival",
                         json={"url": "/assets/opening.mp3", "preload": True},
                         headers={"x-soluna-admin": ADMIN})
        check("preload API 200", r.status == 200 and (await r.json())["preloaded"])
        await asyncio.sleep(0.3)
        pls = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "preload"]
        check("preload配信", len(pls) == 1 and pls[0]["url"] == "/assets/opening.mp3")
        pl_box = []
        pl_conn = await listener(s, "A", pl_box)
        await asyncio.sleep(0.3)
        pl2 = [m for m in pl_box if isinstance(m, dict) and m.get("t") == "preload"]
        check("途中参加にもpreload", len(pl2) == 1)
        await pl_conn[0].close(); pl_conn[1].cancel()

        # 10g) show runner: セットリスト→NEXTで曲+ライトが束で発火
        r = await s.post(f"{BASE}/api/show?ch=festival",
                         json={"steps": [
                             {"label": "op", "url": "/assets/opening.mp3",
                              "light": {"pattern": "beat", "colors": ["#ff0000", "#00ff00"], "bpm": 100}},
                             {"label": "mid", "video": "/assets/x.mp4"}]},
                         headers={"x-soluna-admin": ADMIN})
        check("show set 200", r.status == 200 and (await r.json())["steps"] == 2)
        b0 = len(boxes[0])
        r = await s.post(f"{BASE}/api/show?ch=festival", json={"next": True},
                         headers={"x-soluna-admin": ADMIN})
        nb = await r.json()
        check("show NEXT=step1", nb.get("i") == 1 and nb.get("label") == "op")
        await asyncio.sleep(0.3)
        new_msgs = boxes[0][b0:]
        got_cue = any(isinstance(m, dict) and m.get("t") == "cue"
                      and str(m.get("id", "")).startswith("show1") for m in new_msgs)
        got_light = any(isinstance(m, dict) and m.get("t") == "light"
                        and m.get("bpm") == 100.0 for m in new_msgs)
        check("NEXTで曲+ライト同時配信", got_cue and got_light)
        st2 = await (await s.get(f"{BASE}/status")).json()
        shw = st2["channels"]["festival"]["show"]
        check("status.show 進行表示", shw["i"] == 1 and shw["total"] == 2 and shw["next"] == "mid")
        r = await s.post(f"{BASE}/api/show?ch=festival", json={"next": True},
                         headers={"x-soluna-admin": ADMIN})
        r = await s.post(f"{BASE}/api/show?ch=festival", json={"next": True},
                         headers={"x-soluna-admin": ADMIN})
        check("最後のNEXTはdone", (await r.json()).get("done") is True)

        # 10h) ゾーン指定キュー(ウォークテスト): zonesフィールドが配信される
        r = await s.post(f"{BASE}/api/cue?ch=festival",
                         json={"url": "/assets/opening.mp3", "zones": ["b"], "lead": 2},
                         headers={"x-soluna-admin": ADMIN})
        cz = (await r.json())["cue"]
        check("cue.zones=['B']正規化", cz.get("zones") == ["B"])
        await s.post(f"{BASE}/api/cue?ch=festival", json={"stop": True},
                     headers={"x-soluna-admin": ADMIN})   # 後段のstatus検査のため停止
        await asyncio.sleep(0.2)

        # 10i) /flags 印刷ページ(既定6ゾーンの新規chで)
        r = await s.get(f"{BASE}/flags?ch=flagstest")
        body_txt = await r.text()
        check("flags 200+QR入り6旗", r.status == 200
              and body_txt.count('section class="flag"') == 6
              and 'class="qr"' in body_txt)

        # 11) play.py の遅延計算(ユニット)
        sys.path.insert(0, "/Users/yuki/workspace/soluna-surround")
        try:
            from play import Player
            p = Player("L", zone="B")
            p.zones = {"B": 100.0}; p.base_ms = -20.0
            check("play.py delay_sec = (100-20)/1000", abs(p.delay_sec() - 0.08) < 1e-9)
            p.base_ms = -200.0
            check("play.py delay 負値は0クリップ", p.delay_sec() == 0.0)
        except Exception as e:
            check("play.py import/unit", False, str(e))

        # ---- v6: 観測性・冗長性・配布 ----
        # 13) /health
        r = await s.get(f"{BASE}/health")
        hj = await r.json()
        check("/health 200 + version", r.status == 200 and hj["ok"] and hj["version"], str(hj.get("version")))
        # 14) 端末レポート → /status devices 集計
        await ws0.send_str(json.dumps({"t": "report", "st": "playing", "ctx": "running", "bat": 0.15, "cue": "x"}))
        await conns[1][0].send_str(json.dumps({"t": "report", "st": "preloaded", "ctx": "suspended", "bat": 0.9}))
        await conns[2][0].send_str(json.dumps({"t": "report", "st": "failed"}))
        await asyncio.sleep(0.3)
        dv = (await (await s.get(f"{BASE}/status")).json())["channels"]["festival"]["devices"]
        check("devices: playing=1 preloaded=1 failed=1", dv["playing"] == 1 and dv["preloaded"] == 1 and dv["failed"] == 1, str(dv))
        check("devices: low_battery=1 ctx_suspended=1", dv["low_battery"] == 1 and dv["ctx_suspended"] == 1)
        check("devices: unknown=未報告端末数", dv["unknown"] == 1, f"unknown={dv['unknown']}")
        check("devices: by_zone_playing A=1", dv["by_zone_playing"].get("A") == 1, str(dv["by_zone_playing"]))
        # 15) ゾーン別音量補正
        r = await s.post(f"{BASE}/api/zones?ch=festival", json={"gains_db": {"A": 3, "F": -20}},
                         headers={"x-soluna-admin": ADMIN})
        zb = await r.json()
        check("gains_db: A=3, F=-12にクリップ", zb["zone_gain_db"] == {"A": 3.0, "F": -12.0}, str(zb["zone_gain_db"]))
        await asyncio.sleep(0.3)
        cfgg = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "config"][-1]
        check("config に zone_gain_db + asset_base キー", cfgg.get("zone_gain_db", {}).get("A") == 3.0 and "asset_base" in cfgg)
        # 16) 単調クロック: pong s と /status epoch が同じ時計(±50ms)
        await ws0.send_str(json.dumps({"t": "ping", "c": 1.0}))
        await asyncio.sleep(0.2)
        pong_s = [m for m in boxes[0] if isinstance(m, dict) and m.get("t") == "pong"][-1]["s"]
        st_ms = (await (await s.get(f"{BASE}/status")).json())["server_epoch_ms"]
        check("pong/statusが同一クロック", 0 <= st_ms - pong_s < 300, f"diff={st_ms-pong_s:.1f}ms")
        # 17) assets Cache-Control / HTML no-cache
        with open(os.path.join(DATA_DIR, "assets", "_cc.mp3"), "wb") as f:
            f.write(b"\x00" * 64)
        r = await s.get(f"{BASE}/assets/_cc.mp3")
        check("assets: Cache-Control public", r.status == 200 and "public" in r.headers.get("Cache-Control", ""), r.headers.get("Cache-Control"))
        r = await s.get(f"{BASE}/")
        check("client html: no-cache", r.headers.get("Cache-Control") == "no-cache")
        os.remove(os.path.join(DATA_DIR, "assets", "_cc.mp3"))
        # 18) state export → 別チャンネルへ import(ホットスタンバイ)
        r = await s.get(f"{BASE}/api/state", headers={"x-soluna-admin": ADMIN})
        ex = await r.json()
        check("state export 200 + festival含む", r.status == 200 and "festival" in ex["channels"])
        r = await s.get(f"{BASE}/api/state")
        check("state export 無トークン=403", r.status == 403)
        imp = {"channels": {"standby": dict(ex["channels"]["festival"], cue={"id": "hand", "at": time.time()+5, "gain": 1.0, "loop": False, "url": "/assets/x.mp3"})}}
        sb_box = []
        sb = await listener(s, "A", sb_box)   # festival側で受けるのではなく standby chへ
        await sb[0].close(); sb[1].cancel()
        sb_ws = await s.ws_connect(f"{WS}?role=listen&ch=standby&zone=A")
        sb_in = []
        async def sbr():
            async for msg in sb_ws:
                if msg.type == aiohttp.WSMsgType.TEXT: sb_in.append(json.loads(msg.data))
        sbt = asyncio.create_task(sbr())
        await asyncio.sleep(0.2)
        r = await s.post(f"{BASE}/api/state", json=imp, headers={"x-soluna-admin": ADMIN})
        check("state import 200", r.status == 200)
        await asyncio.sleep(0.3)
        got = [m for m in sb_in if m.get("t") == "cue"]
        check("import後に接続中端末へcue再配信(曲中復帰)", len(got) == 1 and got[0]["id"] == "hand")
        cfgs_sb = [m for m in sb_in if m.get("t") == "config"]
        check("import後にconfig再配信(zones引継ぎ)", len(cfgs_sb) >= 2 and cfgs_sb[-1]["zones"]["B"] == 115.0)
        # 19) channel delete: 接続中=409 / 切断後=200 / 未知=404
        r = await s.delete(f"{BASE}/api/channel?ch=standby", headers={"x-soluna-admin": ADMIN})
        check("channel delete 接続中=409", r.status == 409)
        await sb_ws.close(); sbt.cancel(); await asyncio.sleep(0.2)
        r = await s.delete(f"{BASE}/api/channel?ch=standby", headers={"x-soluna-admin": ADMIN})
        check("channel delete 切断後=200", r.status == 200)
        r = await s.delete(f"{BASE}/api/channel?ch=nope", headers={"x-soluna-admin": ADMIN})
        check("channel delete 未知=404", r.status == 404)
        check("state.json から standby が消えている", "standby" not in json.load(open(os.path.join(DATA_DIR, "state.json"))))

        # 12) /status
        st = await (await s.get(f"{BASE}/status")).json()
        c = st["channels"]["festival"]
        check("status: listeners=4", c["listeners"] == 4, f"by_zone={c['by_zone']}")
        check("status: cue=None(停止後)", c["cue"] is None)

        await push.close()
        for ws, task in conns + [late]:
            await ws.close(); task.cancel()

    print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
    if ng:
        print("FAILED:", ng); sys.exit(1)

if __name__ == "__main__":
    with ServerProc():
        asyncio.run(main())
