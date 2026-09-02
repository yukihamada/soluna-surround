"""
SOLUNA box control — the "plug in and a page opens" layer of a Raspberry Pi box.

  /welcome            captive-portal landing (phones open it by themselves when they join the
                      box's Wi-Fi "SOLUNA": 🎧 join the show / ⚙ set this box up)
  /setup              setup UI (setup.html): role, speaker zone/pos/gain/DAC + test tone,
                      Wi-Fi uplink (scan/join), the box's own AP (SSID/password/band),
                      hostname, token, logs, restart / update / reboot
  GET  /api/box       status + current settings
  POST /api/box       apply settings  {node:{…}, role:{…}, ap:{…}, hostname:…}
  GET  /api/box/wifi  scan            POST /api/box/wifi {ssid,psk}  join
  POST /api/box/action {action: tone|restart-node|restart-server|restart-agent|reboot|update|regen-token}
  GET  /api/box/logs

Enabled only with SOLUNA_BOX=1 (pi-setup.sh sets it). Cloud deploys never expose this.
Auth: x-soluna-admin token, OR (SOLUNA_SETUP_OPEN=1, default on a box) a client on the box's own
AP subnet / localhost — the AP is WPA2 with a generated PSK, so "you are on my Wi-Fi" is the key.
Privileged actions go through `sudo -n` (pi-setup.sh installs /etc/sudoers.d/soluna with exactly
those commands). SOLUNA_BOX_DRYRUN=1 records commands instead of running them (tests).

Captive portal: when SOLUNA_CAPTIVE=1 (or a second listener on SOLUNA_CAPTIVE_PORT, normally 80)
the OS connectivity probes (Apple hotspot-detect, Android generate_204, Windows ncsi, Firefox
canonical) get our landing page instead of "Success", so the phone pops the sheet up. agent.py
points every DNS name at the box while the AP is up (dnsmasq address=/#/<ip>).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
ETC = os.environ.get("SOLUNA_ETC", "/etc/soluna")
APP_DIR = os.environ.get("SOLUNA_APP", HERE)
DRYRUN = os.environ.get("SOLUNA_BOX_DRYRUN") == "1"
SETUP_OPEN = os.environ.get("SOLUNA_SETUP_OPEN", "1") == "1"
OPEN_NETS = tuple(p for p in os.environ.get("SOLUNA_SETUP_OPEN_FROM", "10.42.0.,127.0.0.1,::1").split(",") if p)
NODE_ENV, SERVER_ENV, AGENT_ENV = (os.path.join(ETC, n) for n in ("node.env", "server.env", "agent.env"))
FORCE_SERVER = os.path.join(ETC, "force-server")
AP_PSK = os.path.join(ETC, "ap.psk")
TOKEN_FILE = os.path.join(APP_DIR, "admin-token")
LAST_CMDS: list = []          # DRYRUN: what we would have run (tests read it via /api/box/cmds)

# OS connectivity probes. Anything but the expected body → the phone shows our page.
CAPTIVE_PATHS = {"/hotspot-detect.html", "/library/test/success.html", "/generate_204", "/gen_204",
                 "/connecttest.txt", "/ncsi.txt", "/redirect", "/canonical.html", "/success.txt",
                 "/check_network_status.txt", "/mobile/status.php", "/kindle-wifi/wifistub.html"}
CAPTIVE_HOSTS = ("captive.apple.com", "connectivitycheck", "clients3.google.com", "msftconnecttest",
                 "msftncsi", "detectportal.firefox.com", "nmcheck.gnome.org", "www.apple.com",
                 "play.googleapis.com", "spectrum.s3.amazonaws.com", "connectivity-check.ubuntu.com")


# ---- small helpers ------------------------------------------------------------------
def _sudo():
    return [] if (os.geteuid() == 0 or DRYRUN) else ["sudo", "-n"]


def run(cmd, timeout=25, privileged=False):
    """→ (rc, output). DRYRUN records instead of executing."""
    full = (_sudo() if privileged else []) + list(cmd)
    if DRYRUN:
        LAST_CMDS.append(full)
        return 0, ""
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:                       # noqa: BLE001
        return 1, str(e)


def read_env(path):
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return out


def write_file(path, text, mode=None):
    """Write via sudo tee when we don't own the target (root-owned /etc/soluna)."""
    os.makedirs(os.path.dirname(path), exist_ok=True) if DRYRUN or os.access(os.path.dirname(path), os.W_OK) else None
    if DRYRUN or os.access(path if os.path.exists(path) else os.path.dirname(path), os.W_OK):
        with open(path, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(path, mode)
        return True
    r = subprocess.run(_sudo() + ["tee", path], input=text, capture_output=True, text=True)
    if mode is not None:
        subprocess.run(_sudo() + ["chmod", oct(mode)[2:], path], capture_output=True)
    return r.returncode == 0


def remove_file(path):
    if not os.path.exists(path):
        return True
    if DRYRUN or os.access(os.path.dirname(path), os.W_OK):
        try:
            os.remove(path)
            return True
        except OSError:
            pass
    rc, _ = run(["rm", "-f", path], privileged=True)
    return rc == 0


def write_env(path, updates: dict, mode=None):
    env = read_env(path)
    for k, v in updates.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    body = "".join(f"{k}={v}\n" for k, v in env.items())
    return write_file(path, body, mode)


def local_ips():
    ips = set()
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips.update(out.split())
    except Exception:                            # noqa: BLE001
        pass
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except Exception:                            # noqa: BLE001
        pass
    return sorted(i for i in ips if i and not i.startswith("127."))


def ap_ip():
    """IP of our own hotspot (NetworkManager shared mode = 10.42.0.1)."""
    for ip in local_ips():
        if ip.startswith("10.42.0."):
            return ip
    return os.environ.get("SOLUNA_AP_IP", "10.42.0.1")


def cpu_temp():
    try:
        return round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except Exception:                            # noqa: BLE001
        return None


def audio_cards():
    rc, out = run(["aplay", "-l"], timeout=5)
    cards = []
    for line in out.splitlines():
        m = re.match(r"card (\d+): (\S+) \[(.*?)\], device (\d+): (.*?) \[", line)
        if m:
            cards.append({"id": int(m.group(1)), "name": m.group(2), "desc": m.group(3),
                          "hw": f"hw:{m.group(1)},{m.group(4)}"})
    return cards


def unit_state(unit):
    rc, out = run(["systemctl", "is-active", unit], timeout=5)
    return out.strip() or ("active" if DRYRUN else "unknown")


def wifi_state():
    rc, out = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"], timeout=6)
    wlan = {"device": "wlan0", "state": "unknown", "connection": None, "mode": None}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[1] == "wifi":
            wlan = {"device": parts[0], "state": parts[2], "connection": parts[3] or None,
                    "mode": "ap" if parts[3] == "soluna-ap" else ("client" if parts[2].startswith("connected") else None)}
            break
    return wlan


def wifi_scan():
    rc, out = run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "dev", "wifi", "list", "--rescan", "yes"], timeout=30)
    seen, nets = set(), []
    for line in out.splitlines():
        parts = line.rsplit(":", 3)
        if len(parts) == 4 and parts[0] and parts[0] not in seen:
            seen.add(parts[0])
            nets.append({"ssid": parts[0], "signal": int(parts[1] or 0), "security": parts[2], "freq": parts[3]})
    return sorted(nets, key=lambda n: -n["signal"])


# ---- status + apply -----------------------------------------------------------------
def status(ctx):
    node = read_env(NODE_ENV)
    agent = read_env(AGENT_ENV)
    server = read_env(SERVER_ENV)
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except Exception:                            # noqa: BLE001
        up = None
    return {
        "host": socket.gethostname(), "ips": local_ips(), "ap_ip": ap_ip(),
        "version": ctx.get("version"), "role": ctx["role"]() if ctx.get("role") else None,
        "uptime_s": up, "temp": cpu_temp(),
        "audio": audio_cards(),
        "services": {u: unit_state(f"soluna-{u}") for u in ("node", "server", "agent")},
        "node": {"zone": node.get("ZONE") or "", "pos": node.get("POS") or "C", "gain_db": float(node.get("GAIN_DB") or 0),
                 "device": node.get("DEVICE") or "auto", "ch": node.get("CH") or "festival",
                 "server": node.get("SERVER") or "auto", "pinned": node.get("PINNED") == "1"},
        "role_mode": ("server" if os.path.exists(FORCE_SERVER) else ("pinned" if node.get("PINNED") == "1" else "auto")),
        "ap": {"on": agent.get("SOLUNA_AP", "1") == "1", "ssid": agent.get("SOLUNA_AP_SSID", "SOLUNA"),
               "band": agent.get("SOLUNA_AP_BAND", "bg"), "psk": (open(AP_PSK).read().strip() if os.path.exists(AP_PSK) else None)},
        "wifi": wifi_state(),
        "port": server.get("PORT") or os.environ.get("PORT", "8900"),
        "token": (open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else None),
        "t": time.time(),
    }


def apply(body: dict):
    """Apply a settings payload. Returns list of things done."""
    done = []
    node = body.get("node") or {}
    if node:
        upd = {}
        if "zone" in node:
            upd["ZONE"] = str(node["zone"] or "").upper()[:2]
        if "pos" in node:
            upd["POS"] = str(node["pos"] or "C").upper()[:1]
        if "gain_db" in node:
            upd["GAIN_DB"] = round(max(-24.0, min(12.0, float(node["gain_db"] or 0))), 1)
        if "device" in node:
            upd["DEVICE"] = str(node["device"] or "auto")[:64]
        if "ch" in node:
            upd["CH"] = re.sub(r"[^A-Za-z0-9_-]", "", str(node["ch"] or "festival"))[:32] or "festival"
        write_env(NODE_ENV, upd)
        run(["systemctl", "restart", "soluna-node"], privileged=True)
        done.append("node")
    role = body.get("role") or {}
    if role.get("mode"):
        mode = role["mode"]
        if mode == "server":
            write_file(FORCE_SERVER, "1\n")
            write_env(NODE_ENV, {"SERVER": "ws://127.0.0.1:%s" % (read_env(SERVER_ENV).get("PORT") or "8900"), "PINNED": "0"})
        elif mode == "pinned":
            url = str(role.get("server") or "").strip()
            if not re.match(r"^wss?://[\w.\-:]+(/.*)?$", url):
                raise ValueError("server url must be ws://host:port")
            remove_file(FORCE_SERVER)
            write_env(NODE_ENV, {"SERVER": url, "PINNED": "1"})
        else:                                     # auto
            remove_file(FORCE_SERVER)
            write_env(NODE_ENV, {"SERVER": "auto", "PINNED": "0"})
        run(["systemctl", "restart", "soluna-agent"], privileged=True)
        run(["systemctl", "restart", "soluna-node"], privileged=True)
        done.append(f"role:{mode}")
    ap = body.get("ap") or {}
    if ap:
        upd = {}
        if "on" in ap:
            upd["SOLUNA_AP"] = "1" if ap["on"] else "0"
        if ap.get("ssid"):
            upd["SOLUNA_AP_SSID"] = re.sub(r"[^\x20-\x7e]", "", str(ap["ssid"]))[:32]
        if ap.get("band") in ("bg", "a"):
            upd["SOLUNA_AP_BAND"] = ap["band"]
        write_env(AGENT_ENV, upd)
        if ap.get("psk"):
            psk = str(ap["psk"])
            if not 8 <= len(psk) <= 63:
                raise ValueError("Wi-Fi password must be 8–63 characters")
            write_file(AP_PSK, psk + "\n", 0o600)
        run(["systemctl", "restart", "soluna-agent"], privileged=True)   # agent re-raises the AP with new settings
        done.append("ap")
    if body.get("hostname"):
        hn = re.sub(r"[^a-z0-9-]", "", str(body["hostname"]).lower())[:32].strip("-")
        if not hn:
            raise ValueError("hostname: a-z 0-9 -")
        run(["hostnamectl", "set-hostname", hn], privileged=True)
        run(["systemctl", "restart", "avahi-daemon"], privileged=True)
        done.append(f"hostname:{hn}")
    return done


def action(name: str, ctx):
    if name == "restart-node":
        run(["systemctl", "restart", "soluna-node"], privileged=True)
    elif name == "restart-server":
        run(["systemctl", "restart", "--no-block", "soluna-server"], privileged=True)
    elif name == "restart-agent":
        run(["systemctl", "restart", "soluna-agent"], privileged=True)
    elif name == "reboot":
        run(["systemctl", "reboot"], privileged=True)
    elif name == "update":
        # re-run the installer from GitHub in the background (keeps token/env; restarts units)
        cmd = ("curl -fsSL https://raw.githubusercontent.com/yukihamada/soluna-surround/master/tools/pi-setup.sh "
               "| SUDO_USER=%s bash > /var/log/soluna-update.log 2>&1" % (os.environ.get("USER") or "pi"))
        run(["bash", "-c", "nohup bash -c '%s' >/dev/null 2>&1 &" % cmd.replace("'", "'\\''")], privileged=True)
    elif name == "regen-token":
        import base64
        tok = base64.b64encode(os.urandom(24)).decode().translate(str.maketrans("", "", "+/=")).rstrip()[:32]
        write_file(TOKEN_FILE, tok, 0o600)
        write_env(SERVER_ENV, {"SOLUNA_ADMIN": tok}, 0o600)
        run(["systemctl", "restart", "--no-block", "soluna-server"], privileged=True)
        return {"token": tok}
    elif name == "tone":
        return None                                 # handled by the server (needs the node's WebSocket)
    else:
        raise ValueError("unknown action")
    return {}


def logs(n=120):
    rc, out = run(["journalctl", "-u", "soluna-node", "-u", "soluna-agent", "-u", "soluna-server",
                   "-n", str(n), "--no-pager", "-o", "short"], timeout=10, privileged=True)
    return out[-20000:]


# ---- aiohttp wiring -----------------------------------------------------------------
def _peer_ip(request):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return (peer[0] if peer else request.remote or "") or ""


def _authorized(request, admin_ok):
    if admin_ok(request):
        return True
    if not SETUP_OPEN:
        return False
    ip = _peer_ip(request)
    return any(ip == p or ip.startswith(p) for p in OPEN_NETS)


def _is_local_host(host):
    h = (host or "").split(":")[0].lower()
    return (not h or h in ("localhost", socket.gethostname().lower()) or h.endswith(".local")
            or h in local_ips() or h == ap_ip() or re.match(r"^\d+\.\d+\.\d+\.\d+$", h) is not None)


def captive_response(request):
    """None if this is not a connectivity probe; otherwise the response that pops the portal."""
    path = request.path
    host = request.headers.get("Host", "")
    probe = path in CAPTIVE_PATHS or any(k in host.lower() for k in CAPTIVE_HOSTS) or not _is_local_host(host)
    if not probe:
        return None
    target = f"http://{ap_ip()}/welcome"
    if path in ("/generate_204", "/gen_204"):             # Android: anything but 204 → "sign in to network"
        raise web.HTTPFound(target)
    if path.endswith(".txt"):                              # Windows NCSI expects exact text → give it a redirect
        raise web.HTTPFound(target)
    return web.FileResponse(os.path.join(HERE, "welcome.html"),
                            headers={"Cache-Control": "no-store", "X-SOLUNA-Captive": "1"})


def register(app, ctx):
    """ctx: {"version": str, "role": callable→str, "send_to_host": async fn(host, dict)→int, "admin_ok": fn(request)}"""
    admin_ok = ctx["admin_ok"]

    async def welcome(request):
        return web.FileResponse(os.path.join(HERE, "welcome.html"), headers={"Cache-Control": "no-store"})

    async def setup_page(request):
        return web.FileResponse(os.path.join(HERE, "setup.html"), headers={"Cache-Control": "no-store"})

    def need(request):
        if not _authorized(request, admin_ok):
            raise web.HTTPForbidden(text="setup: join the box's Wi-Fi or send x-soluna-admin")

    async def api_box(request):
        need(request)
        if request.method == "GET":
            return web.json_response(status(ctx))
        try:
            done = apply(await request.json())
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True, "applied": done, "status": status(ctx)})

    async def api_wifi(request):
        need(request)
        if request.method == "GET":
            return web.json_response({"networks": wifi_scan(), "wifi": wifi_state()})
        body = await request.json()
        ssid = str(body.get("ssid") or "")[:32]
        psk = str(body.get("psk") or "")
        if not ssid:
            raise web.HTTPBadRequest(text="ssid required")
        cmd = ["nmcli", "dev", "wifi", "connect", ssid] + (["password", psk] if psk else [])
        rc, out = run(cmd, timeout=45, privileged=True)
        return web.json_response({"ok": rc == 0, "out": out.strip()[-300:], "wifi": wifi_state()})

    async def api_action(request):
        need(request)
        body = await request.json()
        name = str(body.get("action") or "")
        try:
            res = action(name, ctx)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        if name == "tone":
            n = await ctx["send_to_host"](socket.gethostname(), {"t": "tone", "sec": float(body.get("sec") or 0.6),
                                                                  "hz": float(body.get("hz") or 880)})
            return web.json_response({"ok": n > 0, "nodes": n,
                                      "hint": None if n else "this box's node is not connected to this server"})
        return web.json_response({"ok": True, "action": name, **(res or {})})

    async def api_logs(request):
        need(request)
        return web.Response(text=logs(), content_type="text/plain")

    async def api_cmds(request):                 # DRYRUN only (tests)
        if not DRYRUN:
            raise web.HTTPNotFound()
        return web.json_response({"cmds": LAST_CMDS})

    app.add_routes([
        web.get("/welcome", welcome),
        web.get("/setup", setup_page),
        web.get("/api/box", api_box), web.post("/api/box", api_box),
        web.get("/api/box/wifi", api_wifi), web.post("/api/box/wifi", api_wifi),
        web.post("/api/box/action", api_action),
        web.get("/api/box/logs", api_logs),
        web.get("/api/box/cmds", api_cmds),
    ])


@web.middleware
async def captive_middleware(request, handler):
    """Serve the portal to connectivity probes (only meaningful on the box's own AP)."""
    if request.method == "GET" and not request.path.startswith(("/api", "/audio", "/status", "/health")):
        r = captive_response(request)
        if r is not None:
            return r
    return await handler(request)
