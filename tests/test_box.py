"""箱(Pi)のセットアップ画面+キャプティブポータル(boxctl.py)。
SOLUNA_BOX=1 + SOLUNA_BOX_DRYRUN=1(特権コマンドは実行せず記録) + SOLUNA_ETC=一時dir で server.py を起動。"""
import asyncio, json, os, socket, sys, tempfile
import aiohttp
sys.path.insert(0, os.path.dirname(__file__))
from _server import ServerProc, ADMIN

PORT = 8939
BASE = f"http://127.0.0.1:{PORT}"
ETC = tempfile.mkdtemp(prefix="soluna-etc-")
APP = tempfile.mkdtemp(prefix="soluna-app-")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    ok += bool(cond); fail += (not cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))


async def main():
    open(os.path.join(ETC, "node.env"), "w").write("SERVER=auto\nPINNED=0\nCH=festival\nPOS=C\nZONE=\nGAIN_DB=0\nDEVICE=auto\n")
    open(os.path.join(ETC, "agent.env"), "w").write("SOLUNA_AP=1\nSOLUNA_AP_SSID=SOLUNA\nSOLUNA_AP_BAND=bg\n")
    open(os.path.join(ETC, "server.env"), "w").write(f"PORT={PORT}\nSOLUNA_ADMIN={ADMIN}\n")
    open(os.path.join(APP, "admin-token"), "w").write(ADMIN)
    env = {"SOLUNA_BOX": "1", "SOLUNA_BOX_DRYRUN": "1", "SOLUNA_ETC": ETC, "SOLUNA_APP": APP,
           "SOLUNA_CAPTIVE": "1", "SOLUNA_SETUP_OPEN": "1"}
    with ServerProc(env=env, port=PORT):
        async with aiohttp.ClientSession() as s:
            # ---- captive portal: OS probes get the landing, not "Success"
            r = await s.get(f"{BASE}/hotspot-detect.html")
            body = await r.text()
            check("Apple probe → 200 landing (no 'Success')", r.status == 200 and "Success" not in body and "SOLUNA" in body
                  and r.headers.get("X-SOLUNA-Captive") == "1")
            r = await s.get(f"{BASE}/generate_204", allow_redirects=False)
            check("Android probe → 302 to /welcome", r.status == 302 and r.headers["Location"].endswith("/welcome"), str(r.status))
            r = await s.get(f"{BASE}/connecttest.txt", allow_redirects=False)
            check("Windows NCSI → 302", r.status == 302)
            r = await s.get(f"{BASE}/", headers={"Host": "captive.apple.com"})
            check("foreign Host on / → landing", r.status == 200 and "SOLUNA" in await r.text() and r.headers.get("X-SOLUNA-Captive") == "1")
            r = await s.get(f"{BASE}/", headers={"Host": f"127.0.0.1:{PORT}"})
            check("own Host on / → normal client page", r.status == 200 and r.headers.get("X-SOLUNA-Captive") is None
                  and 'id="nowPlaying"' in await r.text())
            r = await s.get(f"{BASE}/welcome")
            check("/welcome 200", r.status == 200 and "/setup" in await r.text())
            r = await s.get(f"{BASE}/setup")
            check("/setup 200 (setup.html)", r.status == 200 and "BOX SETUP" in await r.text())
            r = await s.get(f"{BASE}/api/preload", headers={"Host": "captive.apple.com"})
            check("API は captive の対象外", r.status == 200 and (await r.json()).get("asset_base", "x") is None)

            # ---- status
            r = await s.get(f"{BASE}/api/box")          # 127.0.0.1 → open
            st = await r.json()
            check("GET /api/box (open from localhost)", r.status == 200 and st["host"] == socket.gethostname()
                  and st["node"]["device"] == "auto" and st["role_mode"] == "auto" and st["ap"]["ssid"] == "SOLUNA", str(st)[:200])

            # ---- apply: node
            r = await s.post(f"{BASE}/api/box", json={"node": {"zone": "b", "pos": "l", "gain_db": -3.5, "device": "hw:1,0", "ch": "fest"}})
            j = await r.json()
            env_now = open(os.path.join(ETC, "node.env")).read()
            check("POST node → node.env 更新+ノード再起動", r.status == 200 and "node" in j["applied"]
                  and "ZONE=B\n" in env_now and "POS=L\n" in env_now and "GAIN_DB=-3.5\n" in env_now
                  and "DEVICE=hw:1,0\n" in env_now and "CH=fest\n" in env_now, env_now)
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("systemctl restart soluna-node が発行", ["systemctl", "restart", "soluna-node"] in cmds, str(cmds))
            check("status に反映", j["status"]["node"]["zone"] == "B" and j["status"]["node"]["gain_db"] == -3.5)

            # ---- apply: role
            r = await s.post(f"{BASE}/api/box", json={"role": {"mode": "server"}})
            j = await r.json()
            check("role=server → force-server + node→localhost", os.path.exists(os.path.join(ETC, "force-server"))
                  and f"SERVER=ws://127.0.0.1:{PORT}\n" in open(os.path.join(ETC, "node.env")).read()
                  and j["status"]["role_mode"] == "server")
            r = await s.post(f"{BASE}/api/box", json={"role": {"mode": "pinned", "server": "ws://10.0.0.5:8900"}})
            j = await r.json()
            n_env = open(os.path.join(ETC, "node.env")).read()
            check("role=pinned → SERVER=url PINNED=1", "SERVER=ws://10.0.0.5:8900\n" in n_env and "PINNED=1\n" in n_env
                  and j["status"]["role_mode"] == "pinned")
            r = await s.post(f"{BASE}/api/box", json={"role": {"mode": "pinned", "server": "http://bad"}})
            check("pinned に不正URL → 400", r.status == 400)
            r = await s.post(f"{BASE}/api/box", json={"role": {"mode": "auto"}})
            j = await r.json()
            check("role=auto → SERVER=auto", "SERVER=auto\n" in open(os.path.join(ETC, "node.env")).read() and j["status"]["role_mode"] == "auto")

            # ---- apply: AP
            r = await s.post(f"{BASE}/api/box", json={"ap": {"ssid": "SOLUNA-STAGE", "psk": "openair2026", "band": "a", "on": True}})
            j = await r.json()
            a_env = open(os.path.join(ETC, "agent.env")).read()
            psk = open(os.path.join(ETC, "ap.psk")).read().strip()
            check("AP: agent.env + ap.psk 更新", "SOLUNA_AP_SSID=SOLUNA-STAGE\n" in a_env and "SOLUNA_AP_BAND=a\n" in a_env
                  and psk == "openair2026" and j["status"]["ap"]["ssid"] == "SOLUNA-STAGE" and j["status"]["ap"]["psk"] == "openair2026")
            check("ap.psk は 0600", oct(os.stat(os.path.join(ETC, "ap.psk")).st_mode & 0o777) == "0o600")
            r = await s.post(f"{BASE}/api/box", json={"ap": {"psk": "short"}})
            check("短いAPパスワード → 400", r.status == 400)

            # ---- live input (standalone DJ入口): source.env + soluna-source enable/start, off → disable/stop
            r = await s.post(f"{BASE}/api/box", json={"source": {"on": True, "device": "hw:2,0", "lead": 0.08, "ch": "festival"}})
            j = await r.json()
            s_env = open(os.path.join(ETC, "source.env")).read()
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("source on → source.env + enable/restart soluna-source", r.status == 200 and "source:on" in j["applied"]
                  and "INPUT_DEVICE=hw:2,0\n" in s_env and "LEAD=0.08\n" in s_env
                  and ["systemctl", "enable", "soluna-source"] in cmds and ["systemctl", "restart", "soluna-source"] in cmds, s_env)
            check("status.source 反映 + inputs 一覧あり", j["status"]["source"]["device"] == "hw:2,0" and "inputs" in j["status"])
            r = await s.post(f"{BASE}/api/box", json={"source": {"on": False}})
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("source off → disable/stop", ["systemctl", "stop", "soluna-source"] in cmds and ["systemctl", "disable", "soluna-source"] in cmds)

            # ---- hostname
            r = await s.post(f"{BASE}/api/box", json={"hostname": "Stage Box #2"})
            j = await r.json()
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("hostname → 正規化して hostnamectl", "hostname:stagebox2" in j["applied"]
                  and ["hostnamectl", "set-hostname", "stagebox2"] in cmds, str(j["applied"]))

            # ---- wifi scan/join (dry)
            r = await s.get(f"{BASE}/api/box/wifi")
            check("wifi scan 200", r.status == 200 and "networks" in await r.json())
            r = await s.post(f"{BASE}/api/box/wifi", json={"ssid": "Venue", "psk": "pw12345678"})
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("wifi join → nmcli connect", r.status == 200 and ["nmcli", "dev", "wifi", "connect", "Venue", "password", "pw12345678"] in cmds)

            # ---- actions
            r = await s.post(f"{BASE}/api/box/action", json={"action": "regen-token"})
            j = await r.json()
            tok_file = open(os.path.join(APP, "admin-token")).read().strip()
            check("regen-token → 新トークン(32字)・admin-token/server.env 更新", len(j.get("token", "")) == 32 and tok_file == j["token"]
                  and f"SOLUNA_ADMIN={j['token']}" in open(os.path.join(ETC, "server.env")).read())
            r = await s.post(f"{BASE}/api/box/action", json={"action": "reboot"})
            cmds = (await (await s.get(f"{BASE}/api/box/cmds")).json())["cmds"]
            check("reboot → systemctl reboot", r.status == 200 and ["systemctl", "reboot"] in cmds)
            r = await s.post(f"{BASE}/api/box/action", json={"action": "nope"})
            check("unknown action → 400", r.status == 400)
            r = await s.get(f"{BASE}/api/box/logs")
            check("logs 200 text", r.status == 200 and r.headers["Content-Type"].startswith("text/plain"))

            # ---- tone → the node that names this host gets {"t":"tone"}
            host = socket.gethostname()
            ws = await s.ws_connect(f"{BASE}/audio?role=listen&ch=festival&zone=A&host={host}")
            await asyncio.wait_for(ws.receive(), 3)                       # config
            r = await s.post(f"{BASE}/api/box/action", json={"action": "tone", "hz": 1000, "sec": 0.5})
            j = await r.json()
            got = None
            for _ in range(4):
                m = await asyncio.wait_for(ws.receive(), 3)
                d = json.loads(m.data)
                if d.get("t") == "tone":
                    got = d; break
            check("tone → 自箱ノードWSに {t:tone,hz:1000}", j["ok"] and j["nodes"] == 1 and got and got["hz"] == 1000.0 and got["sec"] == 0.5, str(got))
            await ws.close()
            r = await s.post(f"{BASE}/api/box/action", json={"action": "tone"})
            j = await r.json()
            check("tone: ノード不在 → ok=false+hint", j["ok"] is False and j.get("hint"))

    # ---- auth: SETUP_OPEN=0 → token required
    env2 = dict(env, SOLUNA_SETUP_OPEN="0")
    with ServerProc(env=env2, port=PORT + 1):
        B2 = f"http://127.0.0.1:{PORT + 1}"
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{B2}/api/box")
            check("SETUP_OPEN=0: トークン無し → 403", r.status == 403)
            r = await s.get(f"{B2}/api/box", headers={"x-soluna-admin": ADMIN})
            check("SETUP_OPEN=0: トークン有り → 200", r.status == 200)
            r = await s.get(f"{B2}/setup")
            check("/setup ページ自体は開ける(JSがトークンを求める)", r.status == 200)

    # ---- default = window mode: open right after boot, closed after the window
    env3 = dict(env); env3.pop("SOLUNA_SETUP_OPEN"); env3["SOLUNA_SETUP_WINDOW_S"] = "3600"
    with ServerProc(env=env3, port=PORT + 3):
        B = f"http://127.0.0.1:{PORT + 3}"
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{B}/api/box")
            j = await r.json()
            check("window(3600s): 起動直後はトークン無しでOK", r.status == 200 and j["setup_open"]["now"] is True and j["setup_open"]["mode"] == "window")
    env4 = dict(env3); env4["SOLUNA_SETUP_WINDOW_S"] = "0"
    with ServerProc(env=env4, port=PORT + 4):
        B = f"http://127.0.0.1:{PORT + 4}"
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{B}/api/box")
            check("window(0s): 窓が閉じたら403+案内", r.status == 403 and "reboot" in await r.text())
            r = await s.get(f"{B}/api/box", headers={"x-soluna-admin": ADMIN})
            j = await r.json()
            check("window(0s): トークンなら200・setup_open.now=false", r.status == 200 and j["setup_open"]["now"] is False)
            r = await s.post(f"{B}/api/box", json={"ap": {"security": "owe"}}, headers={"x-soluna-admin": ADMIN})
            j = await r.json()
            check("AP security=owe 保存", j["status"]["ap"]["security"] == "owe")

    # ---- not a box: nothing exposed
    with ServerProc(port=PORT + 2):
        B3 = f"http://127.0.0.1:{PORT + 2}"
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{B3}/setup")
            check("SOLUNA_BOX 無し → /setup 404", r.status == 404)
            r = await s.get(f"{B3}/hotspot-detect.html")
            check("SOLUNA_BOX 無し → captive 無効(404)", r.status == 404)
            r = await s.get(f"{B3}/api/box")
            check("SOLUNA_BOX 無し → /api/box 404", r.status == 404)

    print(f"\n== PASS {ok} / FAIL {fail} ==")
    sys.exit(1 if fail else 0)


asyncio.run(main())
