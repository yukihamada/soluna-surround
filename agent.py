#!/usr/bin/env python3
"""
SOLUNA Sound — Pi supervisor (systemd `soluna-agent`). Flash, power on, done.

Every Pi runs this. It makes the boxes find each other and keep the show alive with no config:

  DISCOVER   listen for a server (mDNS `_soluna._tcp` + UDP beacon :8901) for ~6 s
  NODE       a healthy server exists → point soluna-node at it, keep a warm copy of the show
             (GET /api/state every 10 s → standby-state.json), watch its /health
  ELECTION   no server → announce candidacy for 3 s; the best box wins
             (Ethernet up > longest uptime > lowest hostname); others yield
  SERVER     start soluna-server, publish mDNS + beacon, restore the standby copy if fresh
             (< 10 min) so cues / zones / light / setlist position resume; node → localhost;
             if wlan0 has no upstream, raise a Wi-Fi AP "SOLUNA" (psk /etc/soluna/ap.psk)
  HEAL       server /health fails 5× → back to DISCOVER (→ election, ≈15–20 s takeover);
             own server fails 3× → restart it; node service down → start it;
             audio card list changes → restart node; every action is logged to journal
  REPORT     POST /api/nodes/report every 5 s (host, ip, role, temp, load, disk, audio, ap…)
             → /admin NODES panel, where zones are assigned to boxes (pushed live as {"t":"assign"}).

Markers / env (all optional):
  /etc/soluna/force-server        always be the server, never yield (pi-server-setup.sh writes it)
  /etc/soluna/node.env  PINNED=1  SERVER was given explicitly → agent does not retarget the node
  /etc/soluna/agent.env SOLUNA_AP=0|1 (default 1)  SOLUNA_AP_BAND=bg|a  SOLUNA_AP_SSID=SOLUNA
Pure stdlib. Logic (decide / prio_key / parse) is side-effect free and unit-tested in tests/test_mesh.py.
"""
import json, os, random, shutil, socket, subprocess, sys, time
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover  # noqa: E402

VERSION = "v7"
ETC = os.environ.get("SOLUNA_ETC", "/etc/soluna")
DATA = os.environ.get("SOLUNA_DATA_DIR", "/opt/soluna/data")
NODE_ENV = os.path.join(ETC, "node.env")
SERVER_ENV = os.path.join(ETC, "server.env")
AGENT_ENV = os.path.join(ETC, "agent.env")
FORCE_SERVER = os.path.join(ETC, "force-server")
AP_PSK = os.path.join(ETC, "ap.psk")
STANDBY = os.path.join(DATA, "standby-state.json")
PORT = int(os.environ.get("PORT", "8900"))
SERVER_GRACE_S = float(os.environ.get("SOLUNA_SERVER_GRACE_S", "20"))   # サーバ再起動後、健康判定を保留する秒数
DISCOVER_S = float(os.environ.get("SOLUNA_DISCOVER_S", "6"))
CANDIDATE_S = float(os.environ.get("SOLUNA_CANDIDATE_S", "3"))
STANDBY_MAX_AGE = 600.0


def log(msg):
    print(f"[agent] {msg}", flush=True)


# ---- pure logic (tested) -------------------------------------------------------
def prio_key(b):
    """Election order: Ethernet link up > longer uptime (minutes) > lowest hostname.
    Returns a tuple that compares 'higher = better'."""
    host = str(b.get("host") or "")
    neg_host = tuple(255 - ord(c) for c in host) + (256,)     # 'a' beats 'b'; 'a' beats 'ab'
    return (int(bool(b.get("eth", 0))), int(b.get("up", 0)), neg_host)


def decide(servers, candidates, me, force_server=False):
    """What should this box be right now?
    servers    : healthy servers seen (list of beacon/mdns dicts)
    candidates : other boxes announcing candidacy (beacons, role=candidate)
    me         : my own beacon dict (host/eth/up)
    → ("server", None) | ("node", server) | ("wait", stronger_candidate)"""
    if force_server:
        return ("server", None)
    if servers:
        best = max(servers, key=prio_key)
        return ("node", best)
    others = [c for c in candidates if c.get("host") != me.get("host")]
    stronger = [c for c in others if prio_key(c) > prio_key(me)]
    if stronger:
        return ("wait", max(stronger, key=prio_key))
    return ("server", None)


def pick_server_url(server):
    return f"ws://{server['ip']}:{server.get('port', PORT)}"


# ---- host facts ---------------------------------------------------------------
def sh(cmd, timeout=15, check=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            log(f"cmd failed {cmd}: {r.stderr.strip()[:200]}")
        return r.stdout
    except Exception as e:
        log(f"cmd error {cmd}: {e}")
        return ""


def uptime_min():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]) // 60)
    except Exception:
        return 0


def eth_up():
    for itf in ("eth0", "end0", "enp0s3", "usb0"):
        try:
            with open(f"/sys/class/net/{itf}/operstate") as f:
                if f.read().strip() == "up":
                    return True
        except Exception:
            pass
    return False


def my_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def load1():
    try:
        return round(os.getloadavg()[0], 2)
    except Exception:
        return None


def disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / 1e6)
    except Exception:
        return None


def audio_cards():
    """`aplay -l` の card 行(外付けDACの有無を見る)。alsa無し→''。"""
    if not shutil.which("aplay"):
        return ""
    return "\n".join(l for l in sh(["aplay", "-l"], timeout=5).splitlines() if l.startswith("card"))


def audio_device_name(cards):
    for l in cards.splitlines():
        low = l.lower()
        if "usb" in low or "hifiberry" in low or "i2s" in low or "iqaudio" in low or "dac" in low:
            return l.split("[", 1)[-1].split("]", 1)[0] if "[" in l else l
    return cards.splitlines()[0].split("[", 1)[-1].split("]", 1)[0] if cards else None


def unit_active(unit):
    return sh(["systemctl", "is-active", unit], timeout=5).strip() == "active"


def systemctl(*args):
    log("systemctl " + " ".join(args))
    return sh(["systemctl", *args], timeout=30)


def write_env(path, updates):
    env = discover.read_env(path)
    env.update({k: str(v) for k, v in updates.items()})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for k, v in env.items():
            f.write(f"{k}={v}\n")
    os.replace(tmp, path)
    return env


def ensure_ap_psk():
    """AP の PSK: 初回起動で生成(12文字・0600)。イメージ焼き込み時に同じ値を配れば全Piが同じSSIDに乗る。"""
    try:
        with open(AP_PSK) as f:
            p = f.read().strip()
            if len(p) >= 8:
                return p
    except FileNotFoundError:
        pass
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    p = "".join(random.choice(alphabet) for _ in range(12))
    os.makedirs(ETC, exist_ok=True)
    with open(AP_PSK, "w") as f:
        f.write(p + "\n")
    os.chmod(AP_PSK, 0o600)
    log("generated AP psk")
    return p


def wlan_state():
    """nmcli: wlan0 の状態 → 'connected' | 'disconnected' | 'ap' | 'unavailable' | ''"""
    out = sh(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "dev"], timeout=5)
    for l in out.splitlines():
        p = l.split(":")
        if p and p[0] == "wlan0":
            if len(p) > 2 and p[2] == "soluna-ap":
                return "ap"
            return p[1] if len(p) > 1 else ""
    return ""


class Agent:
    def __init__(self):
        self.host = socket.gethostname()
        self.role = "discover"
        self.server = None            # dict of the server we follow (node mode)
        self.server_url = None
        self.fail = 0
        self.mdns_proc = None
        self.sock = None
        self.last_report = 0.0
        self.last_snapshot = 0.0
        self.cards = audio_cards()
        self.agent_env = discover.read_env(AGENT_ENV)
        self.ap_psk = None
        self.ap_ssid = self.agent_env.get("SOLUNA_AP_SSID", "SOLUNA")
        self.force = os.path.exists(FORCE_SERVER)
        self.admin = discover.read_env(SERVER_ENV).get("SOLUNA_ADMIN", "")

    # ---- beacon helpers ----
    def me(self, role):
        return discover.make_beacon(role, PORT, self.host, eth_up(), uptime_min(),
                                    extra={"ver": VERSION})

    def beacon(self, role):
        if self.sock is not None:
            discover.send_beacon(self.sock, self.me(role))

    def open_sock(self):
        if self.sock is None:
            try:
                self.sock = discover.beacon_socket()
            except OSError as e:
                log(f"beacon socket failed: {e}")
                self.sock = None

    # ---- state transitions ----
    def become_node(self, server):
        url = pick_server_url(server)
        if self.role == "server":
            self.leave_server()
        self.role, self.server, self.server_url, self.fail = "node", server, url, 0
        env = discover.read_env(NODE_ENV)
        if env.get("PINNED") == "1":
            log(f"node target pinned to {env.get('SERVER')} (PINNED=1) — following {url} for health only")
        elif env.get("SERVER") != url:
            write_env(NODE_ENV, {"SERVER": url})
            systemctl("restart", "soluna-node")
            log(f"node → {server.get('host')} {url}")
        elif not unit_active("soluna-node"):
            systemctl("start", "soluna-node")

    def become_server(self):
        self.role, self.server, self.fail = "server", None, 0
        self.server_url = f"ws://127.0.0.1:{PORT}"
        if not unit_active("soluna-server"):
            self.last_restart = time.time(); systemctl("start", "soluna-server")
        # wait for /health
        for _ in range(30):
            if discover.health("127.0.0.1", PORT):
                break
            time.sleep(0.5)
        self.restore_standby()
        env = discover.read_env(NODE_ENV)
        if env.get("PINNED") != "1" and env.get("SERVER") != self.server_url:
            write_env(NODE_ENV, {"SERVER": self.server_url})
            systemctl("restart", "soluna-node")
        elif not unit_active("soluna-node"):
            systemctl("start", "soluna-node")
        if self.mdns_proc is None:
            self.mdns_proc = discover.mdns_publish(self.host, PORT, f"prio={eth_up()},{uptime_min()}")
        self.ensure_ap()
        log(f"★ SERVER mode ({self.host} {my_ip()}:{PORT})")

    def leave_server(self):
        if self.mdns_proc:
            try:
                self.mdns_proc.terminate()
            except Exception:
                pass
            self.mdns_proc = None
        if not self.force:
            systemctl("stop", "soluna-server")

    def restore_standby(self):
        try:
            age = time.time() - os.path.getmtime(STANDBY)
            if age > STANDBY_MAX_AGE:
                log(f"standby copy too old ({age:.0f}s) — not restored")
                return
            with open(STANDBY) as f:
                body = f.read().encode()
            req = Request(f"http://127.0.0.1:{PORT}/api/state", data=body, method="POST",
                          headers={"content-type": "application/json", "x-soluna-admin": self.admin})
            with urlopen(req, timeout=5) as r:
                log(f"show restored from standby copy ({age:.0f}s old): {r.read()[:120]!r}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"standby restore failed: {e}")

    def snapshot(self):
        """node mode: サーバのショー状態を10秒ごとに控える(自分が引き継ぐ時に使う)。"""
        if not self.server or not self.admin:
            return
        try:
            ip, port = self.server["ip"], self.server.get("port", PORT)
            req = Request(f"http://{ip}:{port}/api/state", headers={"x-soluna-admin": self.admin})
            with urlopen(req, timeout=3) as r:
                data = r.read()
            os.makedirs(DATA, exist_ok=True)
            with open(STANDBY + ".tmp", "wb") as f:
                f.write(data)
            os.replace(STANDBY + ".tmp", STANDBY)
        except Exception as e:
            log(f"snapshot failed: {e}")

    def ensure_ap(self):
        if self.agent_env.get("SOLUNA_AP", "1") != "1" or not shutil.which("nmcli"):
            return
        st = wlan_state()
        if st in ("ap",):
            return
        if st == "connected":          # 上流Wi-Fi(会場LAN/テザリング)に乗っている → APは立てない
            return
        self.ap_psk = ensure_ap_psk()
        band = self.agent_env.get("SOLUNA_AP_BAND", "bg")
        sh(["nmcli", "con", "delete", "soluna-ap"], timeout=10)
        out = sh(["nmcli", "dev", "wifi", "hotspot", "ifname", "wlan0", "con-name", "soluna-ap",
                  "ssid", self.ap_ssid, "password", self.ap_psk], timeout=30, check=True)
        sh(["nmcli", "con", "modify", "soluna-ap", "connection.autoconnect", "yes",
            "802-11-wireless.band", band], timeout=10)
        log(f"Wi-Fi AP up: ssid={self.ap_ssid} band={band} ({out.strip()[:80]})")

    # ---- healing ----
    def heal_local(self):
        cards = audio_cards()
        if cards != self.cards:
            log(f"audio cards changed:\n{cards or '(none)'}")
            self.cards = cards
            systemctl("restart", "soluna-node")
        if not unit_active("soluna-node"):
            systemctl("start", "soluna-node")
        if self.role == "server":
            if discover.health("127.0.0.1", PORT):
                self.fail = 0
            elif time.time() - getattr(self, "last_restart", 0.0) < SERVER_GRACE_S:
                pass                                   # 起動中(Pi 4で数秒〜10秒)は数えない
            else:
                self.fail += 1
                log(f"own server unhealthy ({self.fail})")
                if self.fail >= 5:                     # 2秒間隔×5=10秒連続で落ちていたら上げ直す
                    self.last_restart = time.time()
                    systemctl("restart", "--no-block", "soluna-server")
                    self.fail = 0
            if self.mdns_proc is not None and self.mdns_proc.poll() is not None:
                self.mdns_proc = discover.mdns_publish(self.host, PORT)
            self.ensure_ap()

    def watch_server(self):
        """node mode: 2秒ごとに /health。5連敗で DISCOVER へ(→ 選挙)。"""
        if discover.health(self.server["ip"], self.server.get("port", PORT)):
            self.fail = 0
            return True
        self.fail += 1
        log(f"server {self.server.get('host')} unhealthy ({self.fail}/5)")
        if self.fail >= 5:
            log("server lost → discover / election")
            self.role, self.server = "discover", None
            return False
        return True

    def report(self):
        url = self.server_url or f"ws://127.0.0.1:{PORT}"
        http = "http://" + url.split("://", 1)[1]
        body = {"host": self.host, "ip": my_ip(), "role": self.role, "up_min": uptime_min(),
                "temp": cpu_temp(), "load": load1(), "disk_free_mb": disk_free_mb(),
                "audio": audio_device_name(self.cards), "node": sh(["systemctl", "is-active", "soluna-node"], timeout=5).strip(),
                "server": sh(["systemctl", "is-active", "soluna-server"], timeout=5).strip(),
                "eth": int(eth_up()), "agent": VERSION}
        if self.role == "server" and wlan_state() == "ap" and self.ap_psk:
            body["ap"] = {"ssid": self.ap_ssid, "psk": self.ap_psk}
        try:
            req = Request(f"{http}/api/nodes/report", data=json.dumps(body).encode(), method="POST",
                          headers={"content-type": "application/json"})
            urlopen(req, timeout=3).read()
        except Exception as e:
            log(f"report failed: {e}")

    # ---- main loop ----
    def run(self):
        log(f"SOLUNA agent {VERSION} host={self.host} force_server={self.force}")
        self.open_sock()
        while True:
            try:
                self.step()
            except Exception as e:
                log(f"step error: {e!r}")
                time.sleep(2)

    def step(self):
        if self.role == "discover":
            if self.force:
                self.become_server()
                return
            secs = DISCOVER_S + random.uniform(-2, 2)
            servers = discover.discover_servers(max(2.0, secs), sock=self.sock, exclude_host=self.host)
            act, tgt = decide(servers, [], self.me("candidate"))
            if act == "node":
                self.become_node(tgt)
                return
            # election: announce candidacy, listen for stronger boxes
            self.role = "candidate"
            end = time.monotonic() + CANDIDATE_S
            cands = []
            while time.monotonic() < end:
                self.beacon("candidate")
                if self.sock is not None:
                    cands += discover.listen_beacons(self.sock, 1.0, want=("server", "candidate"))
            servers = [c for c in cands if c["role"] == "server" and c["host"] != self.host
                       and discover.health(c["ip"], c.get("port", PORT))]
            act, tgt = decide(servers, [c for c in cands if c["role"] == "candidate"], self.me("candidate"))
            if act == "node":
                self.become_node(tgt)
            elif act == "wait":
                log(f"yield to stronger candidate {tgt.get('host')}")
                self.role = "discover"
                time.sleep(2)
            else:
                self.become_server()
            return

        # steady state: node or server
        t = time.time()
        if self.role == "node":
            if not self.watch_server():
                return
            if t - self.last_snapshot > 10:
                self.snapshot(); self.last_snapshot = t
            if self.sock is not None:
                # 他の候補が誤って立たないよう、フォロー中のサーバ情報を中継はしない(サーバ自身が広告)
                pass
        else:
            self.beacon("server")
            # 2台目のサーバが見えたら prio の低い方が降りる(スプリットブレイン回避)
            if self.sock is not None:
                others = [b for b in discover.listen_beacons(self.sock, 0.5, want=("server",))
                          if b["host"] != self.host]
                if others and not self.force:
                    best = max(others, key=prio_key)
                    if prio_key(best) > prio_key(self.me("server")) and discover.health(best["ip"], best.get("port", PORT)):
                        log(f"another server {best['host']} outranks me → stepping down to node")
                        self.become_node(best)
                        return
        self.heal_local()
        if t - self.last_report > 5:
            self.report(); self.last_report = t
        time.sleep(2)


if __name__ == "__main__":
    Agent().run()
