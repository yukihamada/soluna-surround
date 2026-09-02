#!/usr/bin/env python3
"""ショーコントロール連携の検証: OSC in / タイムコード(LTC復号含む) / Art-Net・sACN out。
  python3 tests/test_showcontrol.py
サーバは子プロセス(tests/_server.py)。OSC/Art-Net/sACN は 127.0.0.1 の空きポートで実打。"""
import asyncio, json, os, socket, struct, sys, time
import numpy as np
import aiohttp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import showctl, ltc
from _server import ServerProc, ADMIN

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name); print(f"  {'✅' if cond else '❌'} {name} {detail}")

def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p

# ---------------------------------------------------------------- 1) OSC parse
m = showctl.build_osc_message("/soluna/cue", "/assets/x.mp3", 2.5, 3, True, False, b"ab")
addr, args = showctl.parse_osc_message(m)
check("osc: message s f i T F b", addr == "/soluna/cue" and args[0] == "/assets/x.mp3"
      and abs(args[1] - 2.5) < 1e-6 and args[2] == 3 and args[3] is True and args[4] is False
      and args[5] == b"ab", str(args))
items = showctl.parse_osc_packet(m)
check("osc: 単発は at=None", items[0][2] is None and items[0][0] == "/soluna/cue")
b = showctl.build_osc_bundle(None, showctl.build_osc_message("/soluna/stop"),
                             showctl.build_osc_message("/soluna/go"))
items = showctl.parse_osc_packet(b)
check("osc: immediate バンドル2件", [i[0] for i in items] == ["/soluna/stop", "/soluna/go"]
      and all(i[2] is None for i in items))
fut = time.time() + 12.5
b = showctl.build_osc_bundle(fut, showctl.build_osc_message("/soluna/cue", "/a.mp3"))
items = showctl.parse_osc_packet(b)
check("osc: 未来timetag → at(±2ms)", abs(items[0][2] - fut) < 0.002, f"{items[0][2]-fut:+.4f}")
b = showctl.build_osc_bundle(time.time() - 5, showctl.build_osc_message("/soluna/cue", "/a.mp3"))
check("osc: 過去timetag → 即時(None)", showctl.parse_osc_packet(b)[0][2] is None)
addr, args = showctl.parse_osc_message(b"/x\0\0,\0\0\0")
check("osc: 引数なし", addr == "/x" and args == [])

# ---------------------------------------------------------------- 2) timecode math
check("tc: 30fps 01:00:00:00 = 108000f", showctl.tc_to_frames("01:00:00:00", 30) == 108000)
check("tc: 25fps 00:00:01:00 = 25f", showctl.tc_to_frames("00:00:01:00", 25) == 25)
check("tc: 24fps 00:01:00:00 = 1440f", showctl.tc_to_frames("00:01:00:00", 24) == 1440)
# 29.97DF: 1分 = 1800-2 = 1798f、10分 = 17982f、1時間 = 107892f
check("tc: 29.97DF 00:01:00;02 = 1800f(;00;01欠番)", showctl.tc_to_frames("00:01:00;02", 29.97) == 1800)
check("tc: 29.97DF 00:10:00;00 = 17982f", showctl.tc_to_frames("00:10:00;00", 29.97) == 17982)
check("tc: 29.97DF 01:00:00;00 = 107892f", showctl.tc_to_frames("01:00:00;00", 29.97) == 107892)
# DF は 1時間で 3.6ms 実時間より短い(SMPTE の既知の残差: 86.4ms/日)
check("tc: 29.97DF 1時間 = 3599.9964s(既知の残差-3.6ms)", abs(showctl.frames_to_seconds(107892, 29.97) - 3599.9964) < 0.001,
      f"{showctl.frames_to_seconds(107892, 29.97):.4f}")
rt_ok = True
for fps in (24, 25, 30, 29.97):
    for f in (0, 1, 1799, 1800, 17981, 17982, 107891, 107892, 123456):
        tc = showctl.frames_to_tc(f, fps)
        if showctl.tc_to_frames(tc, fps) != f:
            rt_ok = False; print("   rt mismatch", fps, f, tc)
check("tc: frames→tc→frames 往復(24/25/30/29.97DF)", rt_ok)
check("tc: frames_to_tc DF表記 ';'", showctl.frames_to_tc(1800, 29.97) == "00:01:00;02")
try:
    showctl.tc_to_frames("1:2:3", 30); check("tc: 不正書式は ValueError", False)
except ValueError:
    check("tc: 不正書式は ValueError", True)
anchor = showctl.tc_anchor("01:00:00:00", 30, 1000.0)
check("tc_epoch: +10s", abs(showctl.tc_epoch(anchor, "01:00:10:00") - 1010.0) < 1e-9)
anchor = showctl.tc_anchor("00:09:59;28", 29.97, 1000.0)
# 00:09:59;28 → 17980f, 00:10:00;02 → 17984f (10分境界はドロップなし) → 4f/29.97
check("tc_epoch: DF 10分境界 4f = 0.1335s", abs(showctl.tc_epoch(anchor, "00:10:00;02") - (1000 + 4 * 1001 / 30000)) < 1e-6)

# ---------------------------------------------------------------- 3) LTC round trip
for fps, drop in ((24, False), (25, False), (30, False), (30, True)):
    w, tcs = ltc.ltc_stream("00:59:58:20", fps, drop, 12)
    for bs in (960, 333):
        dec = ltc.LTCDecoder(); got = []
        for i in range(137, w.size, bs):                    # 137 = フレーム途中から開始
            got += dec.feed(w[i:i + bs])
        want_fps = 29.97 if drop else fps
        valid = all(g["tc"] in tcs and g["fps"] == want_fps and g["drop"] == drop for g in got)
        # 先頭1フレームはビット長推定のウォームアップ、最後は末尾遷移が無いので 10 が期待値
        check(f"ltc: {want_fps}{'DF' if drop else ''} block={bs} 復号{len(got)}/12 全て正解",
              len(got) >= 9 and valid, f"{[g['tc'] for g in got[:2]]}")
        if got:
            # start は 1 フレーム長(80bit)刻みで並ぶ
            steps = np.diff([g["start"] for g in got])
            exp = 48000 / (30000 / 1001 if drop else fps)
            check(f"ltc: {want_fps} フレーム間隔 {exp:.1f}±2 samples", bool(np.all(np.abs(steps - exp) < 2)),
                  f"{steps[:3]}")
            break                                            # 間隔チェックは1ブロック長で十分
rng = np.random.default_rng(7)
w, tcs = ltc.ltc_stream("10:20:30:00", 25, False, 10)
wn = (w * 0.25 + 0.08 + rng.normal(0, 0.03, w.size)).astype(np.float32)   # 減衰+DCオフセット+ノイズ
dec = ltc.LTCDecoder(); got = []
for i in range(0, wn.size, 960):
    got += dec.feed(wn[i:i + 960])
check("ltc: 減衰+DC+ノイズでも復号", len(got) >= 7 and all(g["tc"] in tcs for g in got), f"{len(got)}")
b = ltc.ltc_frame_bits(1, 2, 3, 4, drop=True, fps=30)
check("ltc: 80bit 0の個数が偶数(パリティ)", b.count(0) % 2 == 0 and ltc.decode_bits(b) == (1, 2, 3, 4, True))

# ---------------------------------------------------------------- 4) DMX pattern math
L = {"pattern": "solid", "colors": ["#ff8000"], "brightness": 0.5, "bpm": 120, "speed": 1, "at": 0}
check("dmx: solid = 色×brightness", showctl.light_rgb(L, 1.0, 0.0) == [128, 64, 0])
L["pattern"] = "strobe"
# bpm120 → 2Hz(3Hz上限内): t=0.1 → 位相0.2=点灯、t=0.3 → 位相0.6=消灯
check("dmx: strobe 3Hz上限で点滅", showctl.light_rgb(L, 0.1, 0) == [128, 64, 0] and showctl.light_rgb(L, 0.3, 0) == [0, 0, 0])
L["bpm"] = 600
check("dmx: strobe bpm600 でも 3Hz に上限(光過敏対策)", showctl.light_rgb(L, 1 / 6 + 0.01, 0) == [0, 0, 0] and showctl.light_rgb(L, 0.01, 0) == [128, 64, 0])
L["bpm"] = 120
# pulse は拍頭で色2(c1)が全開 → c0 へ鋭く減衰(client.html と同じ式)
L["pattern"] = "pulse"; L["colors"] = ["#000000", "#ffffff"]; L["brightness"] = 1.0
check("dmx: pulse 拍頭で色2全開→減衰", showctl.light_rgb(L, 0.0, 0)[0] == 255 and showctl.light_rgb(L, 0.4, 0)[0] < 60)
L["pattern"] = "audio"
check("dmx: audio は pulse にフォールバック", showctl.light_rgb(L, 0.0, 0)[0] == 255)
fr = showctl.dmx_frame({"pattern": "solid", "colors": ["#0a0b0c"], "brightness": 1}, 0, 4)
check("dmx: 4灯 RGB 連続配置・以降0", fr[:12] == bytes([10, 11, 12] * 4) and fr[12:] == bytes(500) and len(fr) == 512)
pkt = showctl.artdmx_packet(0x0102, fr, 7)
check("artnet: ヘッダ Art-Net\\0 op=0x5000 ver14 seq/uni/net/len",
      pkt[:8] == b"Art-Net\0" and struct.unpack("<H", pkt[8:10])[0] == 0x5000 and pkt[10:12] == b"\x00\x0e"
      and pkt[12] == 7 and pkt[14] == 0x02 and pkt[15] == 0x01 and struct.unpack(">H", pkt[16:18])[0] == 512
      and pkt[18:] == fr and len(pkt) == 530)
sp = showctl.sacn_packet(3, fr, b"\x01" * 16, 9)
check("sacn: 638B ルート/フレーミング/DMP 長さ・universe・startcode",
      len(sp) == 638 and sp[4:16] == b"ASC-E1.17\0\0\0" and struct.unpack(">H", sp[16:18])[0] == (0x7000 | 622)
      and struct.unpack(">I", sp[18:22])[0] == 4 and struct.unpack(">H", sp[38:40])[0] == (0x7000 | 600)
      and struct.unpack(">I", sp[40:44])[0] == 2 and sp[108] == 100 and sp[111] == 9
      and struct.unpack(">H", sp[113:115])[0] == 3 and struct.unpack(">H", sp[115:117])[0] == (0x7000 | 523)
      and sp[117] == 0x02 and sp[125] == 0 and sp[126:138] == fr[:12])


# ---------------------------------------------------------------- 5) live server: OSC → cue, tc, Art-Net/sACN
async def live():
    osc_port, art_port, sacn_port = free_udp(), free_udp(), free_udp()
    art = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); art.bind(("127.0.0.1", art_port)); art.settimeout(2)
    sac = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sac.bind(("127.0.0.1", sacn_port)); sac.settimeout(2)
    env = {"SOLUNA_OSC_PORT": str(osc_port), "SOLUNA_ARTNET": "127.0.0.1:5", "SOLUNA_ARTNET_PORT": str(art_port),
           "SOLUNA_SACN": "127.0.0.1:2", "SOLUNA_SACN_PORT": str(sacn_port), "SOLUNA_DMX_FIXTURES": "4",
           "SOLUNA_DMX_CH": "sc"}
    with ServerProc(env=env, port=8937) as sp_:
        BASE = f"http://127.0.0.1:{sp_.port}"
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        def osc(addr, *args):
            udp.sendto(showctl.build_osc_message(addr, *args), ("127.0.0.1", osc_port))
        async with aiohttp.ClientSession() as s:
            H = {"x-soluna-admin": ADMIN}
            ws = await s.ws_connect(f"{BASE}/audio?role=listen&ch=sc&zone=B")
            async def recv_json(pred, timeout=3):
                end = time.time() + timeout
                while time.time() < end:
                    m = await asyncio.wait_for(ws.receive(), max(0.05, end - time.time()))
                    if m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if pred(d): return d
                return None
            await recv_json(lambda d: d.get("t") == "config")

            # OSC cue == HTTP cue(同じ do_cue): 形が一致し、lead が at に反映
            t0 = time.time()
            osc("/soluna/cue", "/assets/x.mp3", 2.0, 0.7, "ch=sc")
            cue = await recv_json(lambda d: d.get("t") == "cue")
            r = await s.get(f"{BASE}/status"); st = (await r.json())["channels"]["sc"]
            check("osc→cue: WS配信 url/gain/at≈now+2", cue is not None and cue["url"] == "/assets/x.mp3"
                  and abs(cue["gain"] - 0.7) < 1e-6 and 1.5 < cue["at"] - t0 < 2.6, str(cue))
            check("osc→cue: state.cue と同一(HTTP経路と共通の本体)", st["cue"]["id"] == cue["id"])
            r = await s.post(f"{BASE}/api/cue?ch=sc", json={"url": "/assets/x.mp3", "lead": 2, "gain": 0.7}, headers=H)
            hc = (await r.json())["cue"]
            check("http cue: OSC cue と同じキー集合", set(hc.keys()) == set(k for k in cue if k != "t"), str(set(hc)))
            await recv_json(lambda d: d.get("t") == "cue")

            # 未来 timetag バンドル → at
            fut = time.time() + 20
            udp.sendto(showctl.build_osc_bundle(fut, showctl.build_osc_message("/soluna/cue", "/assets/y.mp3", "ch=sc")),
                       ("127.0.0.1", osc_port))
            cue = await recv_json(lambda d: d.get("t") == "cue" and d.get("url") == "/assets/y.mp3")
            check("osc bundle timetag → cue.at(±5ms)", cue is not None and abs(cue["at"] - fut) < 0.005,
                  f"{cue and cue['at']-fut:+.4f}")
            osc("/soluna/stop", "ch=sc")
            check("osc stop → cue_stop", (await recv_json(lambda d: d.get("t") == "cue_stop")) is not None)

            # timecode: OSC /soluna/tc → /api/cue {"tc"} → at
            osc("/soluna/tc", "01:00:00:00", 30.0, "ch=sc")
            await asyncio.sleep(0.3)
            r = await s.get(f"{BASE}/status"); anchor = (await r.json())["channels"]["sc"]["tc"]
            check("osc tc → state.tc anchor(frames=108000,fps=30)", anchor and anchor["frames"] == 108000
                  and anchor["fps"] == 30.0 and not anchor["drop"], str(anchor))
            r = await s.post(f"{BASE}/api/cue?ch=sc", json={"url": "/assets/z.mp3", "tc": "01:00:10:00"}, headers=H)
            c = (await r.json())["cue"]
            check("cue by tc: at = anchor.epoch + 10s (±5ms)", abs(c["at"] - (anchor["epoch"] + 10.0)) < 0.005,
                  f"{c['at']-anchor['epoch']-10:+.4f}")
            await recv_json(lambda d: d.get("t") == "cue")
            r = await s.post(f"{BASE}/api/timecode?ch=sc", json={"tc": "00:09:59;28", "fps": 29.97}, headers=H)
            anchor = (await r.json())["tc"]
            check("api/timecode 29.97DF: drop=True frames=17980", anchor["drop"] is True and anchor["frames"] == 17980, str(anchor))
            r = await s.post(f"{BASE}/api/cue?ch=sc", json={"url": "/assets/z.mp3", "tc": "00:10:00;02"}, headers=H)
            c = (await r.json())["cue"]
            check("cue by tc DF: 4フレーム=0.1335s(±5ms)", abs(c["at"] - (anchor["epoch"] + 4 * 1001 / 30000)) < 0.005,
                  f"{c['at']-anchor['epoch']:+.4f}")
            await recv_json(lambda d: d.get("t") == "cue")
            r = await s.get(f"{BASE}/api/timecode?ch=sc", headers=H)
            nt = (await r.json())["now_tc"]
            check("GET /api/timecode now_tc は DF表記で進んでいる", nt and ";" in nt and nt >= "00:09:59;28", str(nt))
            r = await s.post(f"{BASE}/api/cue?ch=sc", json={"url": "/assets/z.mp3", "tc": "bad"}, headers=H)
            check("cue by tc 不正 → 400", r.status == 400)
            r = await s.post(f"{BASE}/api/cue?ch=notc", json={"url": "/x", "tc": "00:00:01:00"}, headers=H)
            check("tc 基準なし → 400", r.status == 400)
            r = await s.post(f"{BASE}/api/timecode?ch=sc", json={"tc": "01:00:00:00"})
            check("api/timecode 無認証 403", r.status == 403)

            # show: steps with tc + OSC go
            r = await s.post(f"{BASE}/api/timecode?ch=sc", json={"tc": "02:00:00:00", "fps": 25}, headers=H)
            anchor = (await r.json())["tc"]
            r = await s.post(f"{BASE}/api/show?ch=sc", json={"steps": [
                {"label": "open", "url": "/assets/a.mp3", "tc": "02:00:30:00", "light": {"pattern": "wave"}},
                {"label": "two", "url": "/assets/b.mp3", "lead": 1}]}, headers=H)
            check("show steps(tc付き)登録", (await r.json())["steps"] == 2)
            osc("/soluna/go", "ch=sc")
            cue = await recv_json(lambda d: d.get("t") == "cue" and d.get("url") == "/assets/a.mp3")
            light = await recv_json(lambda d: d.get("t") == "light")
            check("osc go → step1 cue.at = tc+30s(±5ms) + light wave", cue is not None
                  and abs(cue["at"] - (anchor["epoch"] + 30)) < 0.005 and light and light["pattern"] == "wave")
            osc("/soluna/show/goto", 2, "ch=sc"); await asyncio.sleep(0.2)
            osc("/soluna/go", "ch=sc")
            cue = await recv_json(lambda d: d.get("t") == "cue" and d.get("url") == "/assets/b.mp3")
            check("osc goto 2 → go = step2", cue is not None)
            osc("/soluna/light/stop", "ch=sc")
            await recv_json(lambda d: d.get("t") == "light_stop")

            # align / zone
            osc("/soluna/align", -12.5, "ch=sc")
            cfg = await recv_json(lambda d: d.get("t") == "config" and d.get("base_ms") == -12.5)
            check("osc align → config base_ms=-12.5", cfg is not None)
            osc("/soluna/zone", "q", 77.7, "ch=sc")
            cfg = await recv_json(lambda d: d.get("t") == "config" and d.get("zones", {}).get("Q") == 77.7)
            check("osc zone → zones.Q=77.7(大文字化)", cfg is not None)
            osc("/soluna/nope", "ch=sc"); await asyncio.sleep(0.1)
            r = await s.get(f"{BASE}/health")
            check("未知アドレスでもサーバは生きている", r.status == 200)

            # Art-Net / sACN out: pulse 点灯中にパケット、stop で消灯1回
            while True:                       # 消灯待機中は何も来ないことを確認(バッファを空に)
                try: art.recv(2048)
                except socket.timeout: break
            osc("/soluna/light", "pulse", "#ff0000", "#0000ff", 120.0, "ch=sc")
            await recv_json(lambda d: d.get("t") == "light")
            pk = await asyncio.get_event_loop().run_in_executor(None, lambda: art.recv(2048))
            check("artnet: ArtDmx 受信 header/op/universe=5/len=512", pk[:8] == b"Art-Net\0"
                  and struct.unpack("<H", pk[8:10])[0] == 0x5000 and pk[14] == 5 and pk[15] == 0
                  and struct.unpack(">H", pk[16:18])[0] == 512 and len(pk) == 530)
            rgb = pk[18:30]
            check("artnet: 4灯ぶん非ゼロRGB(pulse 赤→青)", any(rgb) and rgb[0] > 0, str(list(rgb)))
            sk = await asyncio.get_event_loop().run_in_executor(None, lambda: sac.recv(2048))
            check("sacn: E1.31 受信 universe=2 priority=100 startcode0", len(sk) == 638 and sk[4:16] == b"ASC-E1.17\0\0\0"
                  and struct.unpack(">H", sk[113:115])[0] == 2 and sk[108] == 100 and sk[125] == 0 and any(sk[126:138]))
            # 40Hz: 0.5秒で ≥15 パケット
            n = 0; end = time.time() + 0.5
            while time.time() < end:
                try: art.settimeout(0.2); art.recv(2048); n += 1
                except socket.timeout: break
            check("artnet: 40Hz 連続送出(0.5sで≥15)", n >= 15, f"n={n}")
            osc("/soluna/light/stop", "ch=sc")
            await recv_json(lambda d: d.get("t") == "light_stop")
            black = None; end = time.time() + 1.5
            while time.time() < end:
                try:
                    art.settimeout(0.3); pk = art.recv(2048)
                    if not any(pk[18:]): black = pk
                except socket.timeout: break
            check("artnet: stop 後に全0(消灯)を送る", black is not None)
            art.settimeout(0.5)
            try: art.recv(2048); silent = False
            except socket.timeout: silent = True
            check("artnet: 消灯後は沈黙", silent)
            await ws.close()
        udp.close(); art.close(); sac.close()

asyncio.run(live())

print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
if ng: print("FAILED:", ng); sys.exit(1)
