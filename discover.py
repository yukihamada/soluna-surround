#!/usr/bin/env python3
"""
SOLUNA Sound — zero-config discovery shared by agent.py (Pi supervisor) and play.py (--server auto).

Two independent paths, either one is enough:
  * mDNS   `_soluna._tcp` via avahi-browse / avahi-publish-service (Raspberry Pi OS ships avahi)
  * beacon UDP broadcast on port 8901: {"soluna":1,"role":"server|candidate","port":8900,
           "host":..,"eth":0|1,"up":<uptime minutes>}   every 2 s — works without any mDNS.

Nothing here needs pip. Everything is best-effort: a missing binary or a firewall never raises.
"""
import json, os, shutil, socket, subprocess, time
from urllib.request import Request, urlopen

BEACON_PORT = int(os.environ.get("SOLUNA_BEACON_PORT", "8901"))
NODE_ENV = os.environ.get("SOLUNA_NODE_ENV", "/etc/soluna/node.env")
SERVICE = "_soluna._tcp"


# ---- env files ---------------------------------------------------------------
def read_env(path):
    """KEY=VALUE per line (systemd EnvironmentFile shape). Missing file → {}."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


# ---- beacon ------------------------------------------------------------------
def beacon_socket(port=BEACON_PORT, bind=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)   # 同一ホストで agent+play が同時に聞ける
    except (AttributeError, OSError):
        pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if bind:
        s.bind(("", port))
    return s


def broadcast_addrs():
    """各インターフェースのブロードキャストアドレス(Linux: ip -j addr)。取れなければ 255.255.255.255。"""
    addrs = set()
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=2).stdout
        for itf in json.loads(out or "[]"):
            for a in itf.get("addr_info", []):
                if a.get("broadcast"):
                    addrs.add(a["broadcast"])
    except Exception:
        pass
    addrs.add("255.255.255.255")
    extra = os.environ.get("SOLUNA_BEACON_DEST")            # tests / odd networks: unicast targets
    if extra:
        addrs.update(x.strip() for x in extra.split(",") if x.strip())
    return sorted(addrs)


def make_beacon(role, port=8900, host=None, eth=0, up=0, extra=None):
    b = {"soluna": 1, "role": role, "port": int(port),
         "host": host or socket.gethostname(), "eth": int(bool(eth)), "up": int(up), "t": time.time()}
    if extra:
        b.update(extra)
    return b


def parse_beacon(data, addr=None):
    """bytes → dict or None. 壊れたJSON/他プロトコルのUDPは黙って捨てる。"""
    try:
        b = json.loads(data.decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(b, dict) or b.get("soluna") != 1:
        return None
    if b.get("role") not in ("server", "candidate", "node"):
        return None
    try:
        b["port"] = int(b.get("port", 8900))
    except Exception:
        return None
    b["host"] = str(b.get("host") or "?")[:64]
    b["eth"] = int(bool(b.get("eth", 0)))
    try:
        b["up"] = int(b.get("up", 0))
    except Exception:
        b["up"] = 0
    if addr:
        b["ip"] = addr[0]
    return b


def send_beacon(sock, beacon, port=BEACON_PORT, dests=None):
    data = json.dumps(beacon).encode()
    for d in (dests or broadcast_addrs()):
        try:
            sock.sendto(data, (d, port))
        except OSError:
            pass


def listen_beacons(sock, seconds, want=("server", "candidate")):
    """seconds の間UDPを聞き、host ごとに最新ビーコンを返す(自分自身も含む: 呼び手が除く)。"""
    seen = {}
    end = time.monotonic() + seconds
    sock.settimeout(0.25)
    while time.monotonic() < end:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        b = parse_beacon(data, addr)
        if b and b["role"] in want:
            seen[b["host"]] = b
    return list(seen.values())


# ---- mDNS (avahi) -------------------------------------------------------------
def mdns_browse(seconds=3):
    """avahi-browse -rtp _soluna._tcp → [{"host","ip","port","txt"}]. avahi無し→[]。"""
    if not shutil.which("avahi-browse"):
        return []
    try:
        out = subprocess.run(["avahi-browse", "-rtp", SERVICE], capture_output=True, text=True,
                             timeout=seconds + 3).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        # =;wlan0;IPv4;SOLUNA soluna-node-1;_soluna._tcp;local;soluna-node-1.local;172.20.10.11;8900;"prio=..."
        p = line.split(";")
        if len(p) >= 9 and p[0] == "=" and p[2] == "IPv4":
            try:
                found.append({"host": p[6].replace(".local", ""), "ip": p[7], "port": int(p[8]),
                              "txt": p[9] if len(p) > 9 else "", "role": "server", "via": "mdns"})
            except ValueError:
                pass
    return found


def mdns_publish(host, port=8900, txt=""):
    """バックグラウンドで広告し続ける Popen を返す(呼び手が terminate)。avahi無し→None。"""
    if not shutil.which("avahi-publish-service"):
        return None
    try:
        return subprocess.Popen(["avahi-publish-service", f"SOLUNA {host}", SERVICE, str(port), txt],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


# ---- health -------------------------------------------------------------------
def health(ip, port=8900, timeout=1.5):
    """GET /health → dict or None."""
    try:
        with urlopen(Request(f"http://{ip}:{port}/health", headers={"User-Agent": "soluna-discover"}),
                     timeout=timeout) as r:
            d = json.loads(r.read().decode())
            return d if d.get("ok") else None
    except Exception:
        return None


def discover_servers(seconds=4.0, sock=None, exclude_host=None):
    """mDNS + beacon を合わせ、/health が返る server だけを返す。"""
    own = sock is None
    if own:
        try:
            sock = beacon_socket()
        except OSError:
            sock = None
    cands = {}
    for s in mdns_browse(min(3, seconds)):
        cands[s["host"]] = s
    if sock is not None:
        for b in listen_beacons(sock, seconds, want=("server",)):
            cands.setdefault(b["host"], b)
    if own and sock is not None:
        sock.close()
    out = []
    for c in cands.values():
        if exclude_host and c["host"] == exclude_host:
            continue
        ip = c.get("ip")
        if not ip:
            continue
        h = health(ip, c.get("port", 8900))
        if h:
            c = dict(c); c["health"] = h
            out.append(c)
    return out


def resolve_server(spec="auto", wait=True, log=print):
    """play.py --server auto: node.env の SERVER が具体的ならそれ、無ければ発見(見つかるまで待つ)。"""
    if spec and spec != "auto":
        return spec
    env = read_env(NODE_ENV)
    s = env.get("SERVER", "")
    if s and s != "auto":
        return s
    while True:
        found = discover_servers(4.0)
        if found:
            best = sorted(found, key=lambda c: (c.get("via") != "mdns", c["host"]))[0]
            url = f"ws://{best['ip']}:{best.get('port', 8900)}"
            log(f"[discover] server found: {best['host']} → {url}")
            return url
        if not wait:
            return None
        log("[discover] no SOLUNA server yet — listening (beacon :%d, mDNS %s)" % (BEACON_PORT, SERVICE))
        time.sleep(2)


if __name__ == "__main__":
    import sys
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    for s in discover_servers(secs):
        print(json.dumps(s))
