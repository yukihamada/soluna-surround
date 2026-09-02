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
             if wlan0 has no upstream (120 s grace), raise the Wi-Fi AP "SOLUNA" (open by default, firewalled)
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


AP_GRACE_S = float(os.environ.get("SOLUNA_AP_GRACE_S", "120"))    # 上流が消えてからAPを立てるまでの猶予
AP_RETRY_S = float(os.environ.get("SOLUNA_AP_RETRY_S", "600"))    # AP中、保存済み上流へ戻る試行の間隔
AP_RETRY_WAIT_S = float(os.environ.get("SOLUNA_AP_RETRY_WAIT_S", "45"))
DEFAULT_AP_PSK = "solunasound"   # wpa を選んだときの既定PSK(ルータのラベル方式: 既知・/setupで変更・pi-flash.sh は艦隊PSKを焼き込む)
# AP のセキュリティ: open(既定・パスワード無し。安全は「箱が閉じている」ことで担保=下の ap_firewall)
#                    wpa(PSK)・owe(Enhanced Open: 暗号化ありパスワード無し。対応端末のみ)
AP_SECURITY_DEFAULT = "open"
AP_ALLOWED_TCP = ("80", "8900")          # 箱が開けるのは Web/WS だけ。SSH(22)等はAPからは届かない
AP_ALLOWED_UDP = ("53", "67", "5353", "8901")   # DNS / DHCP / mDNS / 発見ビーコン


def ap_firewall(enable: bool, iface="wlan0"):
    """AP側の閘門: パスワード無しでも、箱の Web/WS 以外には触れず、上流へも抜けられない。
    nft があれば nft、無ければ iptables(-nft)。失敗は致命ではない(ログのみ)。"""
    if shutil.which("nft"):
        sh(["nft", "delete", "table", "inet", "soluna_ap"], timeout=5)
        if not enable:
            return
        rules = f"""table inet soluna_ap {{
  chain input {{ type filter hook input priority -10; policy accept;
    iifname "{iface}" ct state established,related accept
    iifname "{iface}" icmp type echo-request accept
    iifname "{iface}" icmpv6 type {{ echo-request, nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit }} accept
    iifname "{iface}" tcp dport {{ {", ".join(AP_ALLOWED_TCP)} }} accept
    iifname "{iface}" udp dport {{ {", ".join(AP_ALLOWED_UDP)} }} accept
    iifname "{iface}" drop
  }}
  chain forward {{ type filter hook forward priority -10; policy accept;
    iifname "{iface}" drop
    oifname "{iface}" ct state established,related accept
  }}
}}
"""
        try:
            r = subprocess.run(["nft", "-f", "-"], input=rules, capture_output=True, text=True, timeout=10)
            log("AP firewall: " + ("on (web/ws only, no forwarding)" if r.returncode == 0 else f"failed: {r.stderr.strip()[:120]}"))
        except Exception as e:                       # noqa: BLE001
            log(f"AP firewall failed: {e}")
        return
    ipt = shutil.which("iptables")
    if not ipt:
        log("AP firewall: no nft/iptables — AP is open and unfiltered")
        return
    for chain in ("SOLUNA_AP_IN", "SOLUNA_AP_FWD"):
        hook = "INPUT" if chain.endswith("IN") else "FORWARD"
        sh([ipt, "-D", hook, "-i", iface, "-j", chain], timeout=5)
        sh([ipt, "-F", chain], timeout=5); sh([ipt, "-X", chain], timeout=5)
    if not enable:
        return
    sh([ipt, "-N", "SOLUNA_AP_IN"], timeout=5)
    sh([ipt, "-A", "SOLUNA_AP_IN", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"], timeout=5)
    sh([ipt, "-A", "SOLUNA_AP_IN", "-p", "icmp", "-j", "ACCEPT"], timeout=5)
    for pt in AP_ALLOWED_TCP:
        sh([ipt, "-A", "SOLUNA_AP_IN", "-p", "tcp", "--dport", pt, "-j", "ACCEPT"], timeout=5)
    for pu in AP_ALLOWED_UDP:
        sh([ipt, "-A", "SOLUNA_AP_IN", "-p", "udp", "--dport", pu, "-j", "ACCEPT"], timeout=5)
    sh([ipt, "-A", "SOLUNA_AP_IN", "-j", "DROP"], timeout=5)
    sh([ipt, "-I", "INPUT", "-i", iface, "-j", "SOLUNA_AP_IN"], timeout=5)
    sh([ipt, "-N", "SOLUNA_AP_FWD"], timeout=5)
    sh([ipt, "-A", "SOLUNA_AP_FWD", "-j", "DROP"], timeout=5)
    sh([ipt, "-I", "FORWARD", "-i", iface, "-j", "SOLUNA_AP_FWD"], timeout=5)
    log("AP firewall: on via iptables (web/ws only, no forwarding)")


def saved_upstream_wifi():
    """NetworkManager に保存された、AP以外のWi-Fiプロファイル名一覧(会場LAN・テザリング等)。"""
    out = sh(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"], timeout=5)
    names = []
    for l in out.splitlines():
        p = l.split(":")
        if len(p) >= 2 and p[1] in ("802-11-wireless", "wifi") and p[0] != "soluna-ap":
            names.append(p[0])
    return names


def should_raise_ap(wlan, down_since, now_t, has_upstream_profiles, grace=AP_GRACE_S):
    """APを立てるべきか。wlan='connected'→No。'ap'→No(既に)。上流プロファイルが無ければ即Yes、
    あれば猶予(grace秒)だけ上流の復帰を待ってからYes(=テザリングの一瞬の断でAPに化けて迷子にならない)。"""
    if wlan in ("connected", "ap", "connecting"):
        return False
    if not has_upstream_profiles:
        return True
    return down_since is not None and (now_t - down_since) >= grace


def should_retry_upstream(ap_since, last_retry, now_t, has_upstream_profiles, every=AP_RETRY_S):
    """AP中、保存済み上流が復活したかを定期的に試すべきか(APを一時的に落として上流を待つ)。"""
    if not has_upstream_profiles or ap_since is None:
        return False
    ref = last_retry if last_retry is not None else ap_since
    return (now_t - ref) >= every


def ensure_ap_psk():
    """AP の PSK: 初回起動で生成(12文字・0600)。イメージ焼き込み時に同じ値を配れば全Piが同じSSIDに乗る。"""
    try:
        with open(AP_PSK) as f:
            p = f.read().strip()
            if len(p) >= 8:
                return p
    except FileNotFoundError:
        pass
    # 初期値は既知(ルータのラベル方式)。ランダムにすると上流が切れて箱がAPに化けた瞬間、誰も入れず迷子になる。
    p = (discover.read_env(AGENT_ENV).get("SOLUNA_AP_PSK") or DEFAULT_AP_PSK).strip()
    os.makedirs(ETC, exist_ok=True)
    with open(AP_PSK, "w") as f:
        f.write(p + "\n")
    os.chmod(AP_PSK, 0o600)
    log(f"AP psk initialised ({'from agent.env' if p != DEFAULT_AP_PSK else 'default — change it in /setup'})")
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
        now_t = time.time()
        if st == "connected":          # 上流Wi-Fi(会場LAN/テザリング)に乗っている → APは立てない
            self.wlan_down_since = None
            self.ap_since = None
            return
        upstream = saved_upstream_wifi()
        if st in ("ap",):
            self.ap_since = getattr(self, "ap_since", None) or now_t
            cur = sh(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "soluna-ap"], timeout=5).strip()
            if cur != self.ap_ssid or ensure_ap_psk() != (self.ap_psk or ensure_ap_psk()):
                log(f"AP settings changed ({cur} → {self.ap_ssid}) — re-raising")
            elif should_retry_upstream(self.ap_since, getattr(self, "ap_last_retry", None), now_t, bool(upstream)):
                # 上流が戻っていないか定期的に試す: APを一時停止→上流の自動接続を待つ→戻らなければAP再開
                self.ap_last_retry = now_t
                log(f"AP: trying saved upstream {upstream} for {AP_RETRY_WAIT_S:.0f}s")
                sh(["nmcli", "con", "down", "soluna-ap"], timeout=15)
                ap_firewall(False)
                t_end = now_t + AP_RETRY_WAIT_S
                tried = set()
                while time.time() < t_end:
                    if wlan_state() == "connected":
                        log("AP: upstream is back — staying on it")
                        self.ap_since = None
                        return
                    # 自動再接続を待つだけでは戻らないことがある(手動downやautoconnect-blocked)→ 明示的に上げる
                    for prof in upstream:
                        if prof not in tried:
                            tried.add(prof)
                            sh(["nmcli", "con", "up", prof], timeout=25)
                            break
                    time.sleep(3)
                log("AP: upstream still gone — re-raising AP")
            else:
                return
        else:
            self.wlan_down_since = getattr(self, "wlan_down_since", None) or now_t
            if not should_raise_ap(st, self.wlan_down_since, now_t, bool(upstream)):
                return                 # 猶予中: テザリング/会場Wi-Fiの復帰を待つ
        self.ap_psk = ensure_ap_psk()
        band = self.agent_env.get("SOLUNA_AP_BAND", "bg")
        # キャプティブポータル: APにつないだ端末の全DNSを箱に向ける → OSの接続確認が /welcome を開く
        try:
            os.makedirs("/etc/NetworkManager/dnsmasq-shared.d", exist_ok=True)
            with open("/etc/NetworkManager/dnsmasq-shared.d/soluna-captive.conf", "w") as f:
                f.write("# SOLUNA box captive portal: every name resolves to the box while the AP is up\n"
                        "address=/#/10.42.0.1\n")
        except Exception as e:                       # noqa: BLE001
            log(f"captive dnsmasq conf failed: {e}")
        sec = (self.agent_env.get("SOLUNA_AP_SECURITY") or AP_SECURITY_DEFAULT).lower()
        sh(["nmcli", "con", "delete", "soluna-ap"], timeout=10)
        # nmcli の hotspot サブコマンドは必ずWPAを付けるので、接続を自分で組む(open/owe/wpa を選べる)
        cmd = ["nmcli", "con", "add", "type", "wifi", "ifname", "wlan0", "con-name", "soluna-ap",
               "autoconnect", "no", "ssid", self.ap_ssid,
               "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
               "ipv4.method", "shared", "ipv6.method", "disabled"]
        if sec == "wpa":
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", self.ap_psk]
        elif sec == "owe":
            cmd += ["wifi-sec.key-mgmt", "owe"]
        out = sh(cmd, timeout=30, check=True)
        sh(["nmcli", "con", "modify", "soluna-ap", "802-11-wireless.ap-isolation", "1"], timeout=10)  # 端末同士は見えない
        sh(["nmcli", "con", "up", "soluna-ap"], timeout=30, check=True)
        ap_firewall(True)                # パスワード無しでも: 箱の Web/WS だけ・上流へは抜けない・SSH不可
        self.ap_since = time.time()
        self.ap_security = sec
        log(f"Wi-Fi AP up: ssid={self.ap_ssid} security={sec}{' psk='+self.ap_psk if sec=='wpa' else ''} band={band} ({out.strip()[:80]})")

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
