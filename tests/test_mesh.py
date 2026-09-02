#!/usr/bin/env python3
"""ゼロコンフィグPiメッシュの検証: 選挙ロジック・ビーコン・/api/nodes(report/list/assign)・
node.json 永続・discover ループバック。音デバイス/Pi不要。  python3 tests/test_mesh.py"""
import asyncio, json, os, socket, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiohttp
import discover
import agent
from play import Player
from _server import ServerProc, PORT, ADMIN

ok, ng = [], []
def check(name, cond, detail=""):
    (ok if cond else ng).append(name); print(f"  {'✅' if cond else '❌'} {name} {detail}")

BASE = f"http://127.0.0.1:{PORT}"

# ---- 1) 選挙: 優先順位 = 有線 > 稼働時間 > ホスト名(小さい方) ----
a = {"host": "soluna-b", "eth": 1, "up": 5}
b = {"host": "soluna-a", "eth": 0, "up": 900}
c = {"host": "soluna-a", "eth": 1, "up": 5}
d = {"host": "soluna-ab", "eth": 1, "up": 5}
check("有線が稼働時間に勝つ", agent.prio_key(a) > agent.prio_key(b))
check("同条件ならホスト名が小さい方", agent.prio_key(c) > agent.prio_key(a))
check("'soluna-a' が 'soluna-ab' に勝つ(前方一致でも)", agent.prio_key(c) > agent.prio_key(d))
me = {"host": "soluna-c", "eth": 0, "up": 10}
act, tgt = agent.decide([], [], me)
check("誰もいない → server", act == "server")
act, tgt = agent.decide([], [a, b], me)
check("強い候補がいる → wait(その候補)", act == "wait" and tgt["host"] == "soluna-b", str(tgt))
act, tgt = agent.decide([], [{"host": "soluna-z", "eth": 0, "up": 1}], me)
check("弱い候補だけ → server", act == "server")
act, tgt = agent.decide([{"host": "srv1", "ip": "10.0.0.5", "port": 8900, "eth": 1, "up": 3}], [a], me)
check("健康なサーバがあれば必ず node", act == "node" and tgt["host"] == "srv1")
act, _ = agent.decide([{"host": "srv1", "ip": "10.0.0.5"}], [], me, force_server=True)
check("force-server は常に server", act == "server")
act, _ = agent.decide([], [me], me)
check("自分自身の候補ビーコンは無視", act == "server")
check("pick_server_url", agent.pick_server_url({"ip": "10.0.0.5", "port": 8900}) == "ws://10.0.0.5:8900")

# ---- 2) ビーコン parse ----
bcn = discover.make_beacon("server", 8900, "pi-1", eth=True, up=42)
pb = discover.parse_beacon(json.dumps(bcn).encode(), ("10.1.2.3", 8901))
check("beacon round-trip(ip付与)", pb and pb["host"] == "pi-1" and pb["ip"] == "10.1.2.3" and pb["eth"] == 1 and pb["up"] == 42, str(pb))
check("非JSONは捨てる", discover.parse_beacon(b"\x00\x01garbage") is None)
check("soluna以外のJSONは捨てる", discover.parse_beacon(b'{"hello":1}') is None)
check("不正roleは捨てる", discover.parse_beacon(b'{"soluna":1,"role":"boss"}') is None)
env_path = os.path.join(tempfile.mkdtemp(), "node.env")
open(env_path, "w").write("# c\nSERVER=ws://10.0.0.9:8900\nPINNED=\"1\"\n")
env = discover.read_env(env_path)
check("read_env", env == {"SERVER": "ws://10.0.0.9:8900", "PINNED": "1"}, str(env))
check("read_env 無いファイル → {}", discover.read_env(env_path + ".nope") == {})

# ---- 3) ビーコン ループバック送受信 ----
def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
bport = free_udp_port()
rx = discover.beacon_socket(bport)
tx = discover.beacon_socket(bport, bind=False)
got = {}
def _listen():
    for b in discover.listen_beacons(rx, 1.5, want=("server", "candidate")):
        got[b["host"]] = b
th = threading.Thread(target=_listen); th.start()
time.sleep(0.2)
discover.send_beacon(tx, discover.make_beacon("candidate", 8900, "pi-x", 0, 7), port=bport, dests=["127.0.0.1"])
discover.send_beacon(tx, discover.make_beacon("server", 8900, "pi-y", 1, 99), port=bport, dests=["127.0.0.1"])
th.join()
check("loopback で candidate/server ビーコンを受信", set(got) == {"pi-x", "pi-y"} and got["pi-y"]["role"] == "server", str(sorted(got)))
rx.close(); tx.close()

# ---- 4) play.py node.json 永続 + assign 反映 ----
nj = os.path.join(tempfile.mkdtemp(), "node.json")
p = Player("C", zone="A"); p.node_json = nj
ch = p.apply_assign({"t": "assign", "zone": "c", "pos": "L", "gain_db": -3})
check("assign → zone/pos/gain 即時反映", ch and p.zone == "C" and p.pos == "L" and (p.gl, p.gr) == (1.0, 0.0) and p.gain_db == -3.0)
check("assign → node.json 保存", Player.load_node_json(nj) == {"zone": "C", "pos": "L", "gain_db": -3.0}, str(Player.load_node_json(nj)))
check("同じ内容の assign は changed=False", p.apply_assign({"zone": "C", "pos": "L", "gain_db": -3}) is False)
check("無い node.json → {}", Player.load_node_json(nj + ".nope") == {})
check("report に host が乗る", p.report().get("host") == socket.gethostname())


# ---- 5) サーバ: /api/nodes/report → /api/nodes → assign push ----
async def server_part():
    async with aiohttp.ClientSession() as s:
        H = {"x-soluna-admin": ADMIN}
        rep = {"host": "pi-test-1", "ip": "10.9.9.1", "role": "node", "up_min": 12, "temp": 51.2, "load": 0.3,
               "disk_free_mb": 12000, "audio": "snd_rpi_hifiberry_dac", "node": "active", "server": "inactive",
               "eth": 1, "agent": "v7"}
        r = await s.post(f"{BASE}/api/nodes/report", json=rep)
        check("nodes/report 200(無認証・LAN)", r.status == 200, str(r.status))
        r = await s.post(f"{BASE}/api/nodes/report", json=rep)
        check("nodes/report 連打は 429(IPごと1req/s)", r.status == 429, str(r.status))
        r = await s.get(f"{BASE}/api/nodes")
        check("GET /api/nodes 無認証 403", r.status == 403)
        r = await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)
        d = await r.json()
        n = [x for x in d["nodes"] if x["host"] == "pi-test-1"]
        check("GET /api/nodes に箱が並ぶ(stale=False・audio/temp)", r.status == 200 and n and n[0]["stale"] is False
              and n[0]["audio"] == "snd_rpi_hifiberry_dac" and n[0]["temp"] == 51.2 and n[0]["ws"] is False, str(n))
        check("meta.ap は AP 無しなら None", d["meta"]["ap"] is None)
        h = await (await s.get(f"{BASE}/health")).json()
        check("/health に role=server と nodes 数", h.get("role") == "server" and h.get("nodes") == 1, str({k: h.get(k) for k in ("role", "nodes")}))
        await asyncio.sleep(1.0)                    # レート制限(1req/s)を跨ぐ
        r = await s.post(f"{BASE}/api/nodes/report", json={"role": "node"})
        check("host 無しは 400", r.status == 400, str(r.status))

        # AP 情報は server 役の箱の報告からだけ拾う
        await asyncio.sleep(1.0)
        r = await s.post(f"{BASE}/api/nodes/report", json={"host": "pi-srv", "role": "server", "ap": {"ssid": "SOLUNA", "psk": "abc123abc123"}})
        d = await (await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)).json()
        check("meta.ap = server 箱の AP(ssid/psk)・一覧側からは消す", d["meta"]["ap"] == {"ssid": "SOLUNA", "psk": "abc123abc123"}
              and all("ap" not in x for x in d["nodes"]), str(d["meta"]))

        # ノードWS(host=pi-test-1)に assign が届く
        ws = await s.ws_connect(f"{BASE}/audio?role=listen&ch=mesh&zone=A&pos=C&host=pi-test-1")
        first = json.loads((await ws.receive()).data)
        check("接続直後は config(割当なし)", first.get("t") == "config")
        r = await s.post(f"{BASE}/api/nodes/assign?ch=mesh", json={"host": "pi-test-1", "zone": "c", "pos": "l", "gain_db": -3},
                         headers=H)
        b = await r.json()
        check("assign 保存+push=1", b["ok"] and b["pushed"] == 1 and b["cfg"] == {"zone": "C", "pos": "L", "gain_db": -3.0}, str(b))
        m = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
        check("ノードWSに {t:assign,zone:C,pos:L,gain_db:-3}", m == {"t": "assign", "zone": "C", "pos": "L", "gain_db": -3.0}, str(m))
        st = await (await s.get(f"{BASE}/status")).json()
        check("status の by_zone が割当後のゾーンに動く", st["channels"]["mesh"]["by_zone"].get("C") == 1, str(st["channels"]["mesh"]["by_zone"]))
        d = await (await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)).json()
        n = [x for x in d["nodes"] if x["host"] == "pi-test-1"][0]
        check("/api/nodes に cfg と ws=True", n["cfg"]["zone"] == "C" and n["ws"] is True, str(n["cfg"]))
        r = await s.post(f"{BASE}/api/nodes/assign?ch=mesh", json={"host": "pi-test-1", "pos": "X"}, headers=H)
        check("pos 不正は 400", r.status == 400)
        r = await s.post(f"{BASE}/api/nodes/assign?ch=mesh", json={"host": "pi-test-1", "zone": "B"})
        check("assign 無認証 403", r.status == 403)
        await ws.close()

        # 再接続 → 保存済みの割当が接続時に再送される(node.json が消えても復元)
        ws2 = await s.ws_connect(f"{BASE}/audio?role=listen&ch=mesh&zone=A&pos=C&host=pi-test-1")
        msgs = []
        for _ in range(3):
            try:
                msgs.append(json.loads((await asyncio.wait_for(ws2.receive(), 1.5)).data))
            except asyncio.TimeoutError:
                break
        ass = [m for m in msgs if m.get("t") == "assign"]
        check("再接続時に assign 再送", ass and ass[0]["zone"] == "C", str([m.get("t") for m in msgs]))
        await ws2.close()

        # 割当解除
        r = await s.post(f"{BASE}/api/nodes/assign?ch=mesh", json={"host": "pi-test-1", "clear": True}, headers={"x-soluna-admin": ADMIN})
        d = await (await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)).json()
        n = [x for x in d["nodes"] if x["host"] == "pi-test-1"][0]
        check("clear → cfg 空", (await r.json())["cfg"] is None and n["cfg"] == {})

        # 割当だけあって報告の無い箱も一覧に出る
        await s.post(f"{BASE}/api/nodes/assign?ch=mesh", json={"host": "pi-ghost", "zone": "D"}, headers=H)
        d = await (await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)).json()
        g = [x for x in d["nodes"] if x["host"] == "pi-ghost"]
        check("未報告でも割当済みの箱は stale で一覧に", g and g[0]["stale"] is True and g[0]["cfg"]["zone"] == "D")

        # stale 判定(閾値は env で 1 秒に)
        await asyncio.sleep(1.3)
        d = await (await s.get(f"{BASE}/api/nodes?ch=mesh", headers=H)).json()
        n = [x for x in d["nodes"] if x["host"] == "pi-test-1"][0]
        check("SOLUNA_NODE_STALE_S 超で stale=True", n["stale"] is True, f"age={n['age_s']}")
        h = await (await s.get(f"{BASE}/health")).json()
        check("/health nodes は stale を数えない", h.get("nodes") == 0, str(h.get("nodes")))

        # /api/state に node_cfg が乗る(ホットスタンバイで割当も引き継ぐ)
        d = await (await s.get(f"{BASE}/api/state", headers=H)).json()
        check("api/state export に node_cfg", d["channels"]["mesh"].get("node_cfg", {}).get("pi-ghost", {}).get("zone") == "D")

with ServerProc(env={"SOLUNA_NODE_STALE_S": "1", "SOLUNA_MDNS": "0"}):
    asyncio.run(server_part())

# ---- AP policy(上流が切れた瞬間にAPへ化けて迷子にならない)

from agent import should_raise_ap, should_retry_upstream, DEFAULT_AP_PSK

check("AP: 上流接続中は立てない", should_raise_ap("connected", 0, 1000, True) is False)

check("AP: 保存済み上流なし → 即立てる", should_raise_ap("disconnected", 990, 1000, False) is True)

check("AP: 上流あり・猶予中(30s) → まだ", should_raise_ap("disconnected", 970, 1000, True, grace=120) is False)

check("AP: 上流あり・猶予超え(130s) → 立てる", should_raise_ap("disconnected", 870, 1000, True, grace=120) is True)

check("AP: 既にAP → 立て直さない", should_raise_ap("ap", 0, 1000, True) is False)

check("AP再試行: 上流プロファイル無し → しない", should_retry_upstream(0, None, 5000, False) is False)

check("AP再試行: AP開始から600s未満 → しない", should_retry_upstream(1000, None, 1500, True, every=600) is False)

check("AP再試行: 600s経過 → する", should_retry_upstream(1000, None, 1601, True, every=600) is True)

check("AP再試行: 直前の試行から測る", should_retry_upstream(1000, 1601, 1900, True, every=600) is False)

check("AP PSK 既定値は既知(ラベル方式)", DEFAULT_AP_PSK == "solunasound")
from agent import AP_SECURITY_DEFAULT, AP_ALLOWED_TCP, AP_ALLOWED_UDP
check("AP 既定=open(パスワード無し)", AP_SECURITY_DEFAULT == "open")
check("AP 閘門: TCPは 80/8900 のみ(SSH不可)", set(AP_ALLOWED_TCP) == {"80", "8900"})
check("AP 閘門: UDPは DNS/DHCP/mDNS/ビーコン", set(AP_ALLOWED_UDP) == {"53", "67", "5353", "8901"})


print(f"\n== PASS {len(ok)} / FAIL {len(ng)} ==")
if ng:
    print("failed:", ng)
sys.exit(1 if ng else 0)
